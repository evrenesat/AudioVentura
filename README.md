# AudioVentura

AudioVentura is a private web application for generating music with ACE-Step.
It supports original songs and covers made from a public YouTube source. The
intended audience for this repository is the operator and coding agents.

The system is split into separate processes because they have different trust
and hardware requirements:

- the controller owns the private UI, SQLite state, and job orchestration;
- the transfer service moves audio through short-lived signed URLs;
- Home Ingest downloads and prepares YouTube audio on the home network;
- Runpod or SaladCloud runs the GPU worker.

The controller does not run media tools or inference. GPU providers never
receive YouTube, SSH, SFTP, home-network, or controller credentials.

## Current state

- Runpod and SaladCloud implement the same provider interface.
- Each job stores its provider before submission. Existing jobs always resume
  through that provider; changing the default does not migrate active work.
- SaladCloud infrastructure and the model-inclusive worker image are deployed.
  Live acceptance is currently blocked by Salad capacity: a queued job did not
  receive an instance. See [the Salad runbook](docs/SALAD.md).
- Provider selection is an administrator setting. A browser provider selector
  and fal.ai support are future work. The provider contract already models
  prompt-to-audio, audio-to-audio, and explicit feature capabilities.
- The quality-evaluation CLI is intentionally quarantined until ordinary
  original and cover generation are stable.

## Repository map

```text
src/ace_service/       controller, transfer service, persistence, providers
runpod_worker/         shared ACE-Step runtime and Runpod entry point
home_ingest/           private YouTube/media preparation service
deploy/salad/          Salad worker wrapper, image, and infrastructure tool
docs/                  operator and provider runbooks
plans/                 completed and active implementation plans
tests/                 controller and integration-style contract tests
```

Start with:

- [Architecture](ARCHITECTURE.md) for boundaries and invariants.
- [Operations](docs/OPERATIONS.md) for setup, migration, backup, and recovery.
- [Security](docs/SECURITY.md) for exposed surfaces and secret handling.
- [Runpod](docs/RUNPOD.md) or [SaladCloud](docs/SALAD.md) for provider details.
- [Development log](DEVLOG.md) for chronological implementation decisions.

The quality campaign contract, historical baseline, research brief, incident
records, and plans are supporting evidence. They are not setup instructions.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- SQLite on durable local storage
- `yt-dlp`, `ffmpeg`, and `ffprobe` on the Home Ingest host only
- a compatible NVIDIA GPU provider for ACE-Step inference

Install the controller environment from the repository root:

```text
uv sync --frozen
```

Install Home Ingest separately:

```text
cd home_ingest
uv sync --frozen
```

## Configuration

Copy `.env.example` into deployment-managed configuration outside Git. The
application uses environment variables; it does not load or manage secrets
from the repository.

Important groups are:

- `ACE_SERVICE_*`: controller bind address, data root, authentication, and
  public hostname;
- `ACE_TRANSFER_*`: public signed-transfer origin, limits, and token lifetime;
- `ACE_HOME_INGEST_*`: private Home Ingest endpoint and bearer token;
- `INFERENCE_PROVIDER`: `runpod` or `salad` for newly created jobs;
- `RUNPOD_*`: Runpod API, endpoint, polling, and timeout settings;
- `SALAD_*`: Salad API, organization, project, queue, container group, and
  timeout settings;
- `ACESTEP_*` and `RUNPOD_WORKER_RUNTIME_IDENTITY`: pinned model and worker
  identity recorded with jobs and outputs.

Placeholder credentials are rejected. Never commit `.env`, API keys, database
files, generated audio, private fixtures, or capability URLs.

## Local processes

Run the private controller:

```text
uv run python -m ace_service
```

Run the public transfer app as a separate process using the same data root:

```text
uv run python -m ace_service.transfer_main
```

Run Home Ingest from its own directory:

```text
cd home_ingest
uv run python -m ace_home_ingest
```

Default binds are:

- controller: `127.0.0.1:8000`;
- transfer service: `127.0.0.1:8001`;
- Home Ingest: `127.0.0.1:8100`.

Do not expose these ports directly. The controller belongs behind the private
tailnet. The public proxy may forward only the signed transfer paths described
in [the operations runbook](docs/OPERATIONS.md).

## Database migrations

Normal startup does not migrate an existing database. Inspect and upgrade it
explicitly after taking a verified backup:

```text
uv run python -m ace_service migrate-status \
  --database /srv/ace-service/data/service.db

uv run python -m ace_service migrate-upgrade \
  --database /srv/ace-service/data/service.db
```

Do not retry an incomplete migration. Restore the verified backup and
investigate first.

## Verification

Run the full local matrix from the repository root:

```text
uv run pytest -q tests runpod_worker/tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src runpod_worker

cd home_ingest
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Some quality-campaign tests use a private fixture outside Git. If its frozen
retention deadline has passed, those tests fail closed by design. Record that
separately from product regressions; do not weaken the validation to make an
expired fixture pass.

Provider contract tests use mocks and do not prove GPU availability, billing,
image startup, public transfer routing, or scale-to-zero behavior. Perform the
live acceptance checks in the provider runbook after any deployment change.

## Working rules

- Use `uv` for Python commands.
- Store timestamps in UTC.
- Keep application data under the configured data root.
- Keep controller, Home Ingest, transfer, and GPU-worker responsibilities
  separate.
- Never retry an uncertain provider submission with a new job. Resume the
  persisted provider reference or fail closed at its deadline.
- Update `ARCHITECTURE.md`, the relevant runbook, and `DEVLOG.md` when behavior
  or deployment state changes.

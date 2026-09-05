# AudioVentura

AudioVentura is a private web application for generating music with ACE-Step.
It supports a source-first workflow: a user can ingest a YouTube source or one
audio/video upload, play the canonical source, choose a backend-valid range,
and create a remix. The intended audience for this repository is the operator
and coding agents.

The system is split into separate processes because they have different trust
and hardware requirements:

- the controller owns the private UI, SQLite state, and job orchestration;
- the transfer service moves raw and prepared audio through short-lived signed
  URLs;
- Home Ingest downloads, probes, and prepares YouTube or uploaded media on the
  home network;
- the private sequential MIDI mock renders deterministic corpus entries on p100;
- Runpod or SaladCloud runs the optional cloud GPU worker; a manually managed
  ACE Node can run the same worker persistently on one private local or rented
  GPU; reviewed fal.ai Model APIs run as controller-pulled managed backends.

The controller does not run media tools or inference. GPU providers never
receive YouTube, SSH, SFTP, home-network, or controller credentials.

## Current state

- Runpod and SaladCloud implement the same provider interface.
- Each job stores its provider before submission. Existing jobs always resume
  through that provider; changing the default does not migrate active work.
- SaladCloud infrastructure and the model-inclusive worker image are deployed.
  Live acceptance is currently blocked by Salad capacity: a queued job did not
  receive an instance. See [the Salad runbook](docs/SALAD.md).
- Provider selection is an administrator-configured, per-job backend choice.
  The Original form lists reviewed text-to-music backends; Cover / Remix lists
  compatible audio transform, inpaint, and outpaint backends. See [the Fal
  runbook](docs/FAL.md) before enabling paid endpoints.
- Create remix first asks for one reviewed source-capable backend and stores it
  as source intent while Home Ingest prepares the media. The final remix form
  revalidates the choice and is the only place that binds a job to an immutable
  provider/backend snapshot.
- `mock/midi-sequential` is an opt-in, MP3-only integration backend. It ignores
  creative parameters, consumes one MIDI cursor entry per variation, and never
  replaces a real-provider default or acts as fallback.
- `node/ace-step-v15-xl-turbo` is an opt-in persistent ACE Node backend. It is
  disabled by default and supports exactly one Linux x86_64/NVIDIA CUDA host
  or one Apple Silicon arm64/MPS+MLX host. See [the ACE Node runbook](docs/ACE-NODE.md)
  before supplying a private node URL and token.
- `ailocals/ace-step-v15-xl-turbo` is an opt-in universal-worker backend. A
  single enrolled ailocals Mac client leases queued submissions through the
  outbound `api/ailocals/v1` worker API; jobs keep existing provider
  semantics, one inference attempt, and transfer-based audio delivery. Enable
  with `AILOCALS_ENABLED=1` plus the backend ID in `INFERENCE_ENABLED_BACKENDS`,
  then manage enrollment tokens under Local workers. The shared wire contract
  is vendored at `contracts/ailocals-v1/`.
- A ready source is published as one canonical stereo 48 kHz 192 kbps MP3
  before remix submission and is added to its project playlist exactly once.
  Completed MP3 variations are published after output verification. FLAC/WAV
  results retain their lossless primary download and enter the library only
  after a verified MP3 playback derivative is ready.
- The persistent player owns one global audio element and keeps its queue,
  playback position, shuffle, repeat, and rate across same-origin navigation.
- A root-scoped unified service worker supports push and a browser-local offline
  player. Complete verified MP3 bodies are content-addressed and shared across
  duplicate entries, playlists, and the local `Played tracks` owner.
- Online playback starts immediately and caches one complete MP3 in the
  background. The Offline screen offers read-only saved snapshots, explicit
  Keep/Refresh/Retry/Cancel/Remove controls, progress, quota/persistence state,
  and the trusted-device warning. Root and `/beta/` browser storage namespaces
  are separate.
- Queued and in-flight jobs can be cancelled when the persisted provider state
  permits it. Project deletion is available only after every job is terminal;
  it records a bounded audit summary before path-verifying and removing the
  project tree, including legacy and lossless output files.
- The quality-evaluation CLI is intentionally quarantined until ordinary
  original and cover generation are stable.
- Managed Salad and RunPod capacity use durable database leases. Browser
  notifications are opt-in Web Push; Fal remains inference-only and never
  enters capacity management.

## Repository map

```text
src/ace_service/       controller, transfer service, persistence, providers,
                       media library, templates, and player assets
runpod_worker/         shared ACE-Step runtime and Runpod entry point
src/ace_node/           separately deployed persistent ACE Node service
home_ingest/           private YouTube/media preparation service
midi_mock_backend/     private deterministic MIDI-to-MP3 test service
deploy/salad/          Salad worker wrapper, image, and infrastructure tool
deploy/node/macos/     native arm64 menu-bar app and release builders
docs/                  operator and provider runbooks
plans/                 completed and active implementation plans
tests/                 controller and integration-style contract tests
```

Start with:

- [Architecture](ARCHITECTURE.md) for boundaries and invariants.
- [Operations](docs/OPERATIONS.md) for setup, migration, backup, and recovery.
- [Security](docs/SECURITY.md) for exposed surfaces and secret handling.
- [ACE Node](docs/ACE-NODE.md) for the persistent private GPU option.
- [Runpod](docs/RUNPOD.md) or [SaladCloud](docs/SALAD.md) for optional cloud provider details.
- [Development log](DEVLOG.md) for chronological implementation decisions.

The quality campaign contract, historical baseline, research brief, incident
records, and plans are supporting evidence. They are not setup instructions.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- SQLite on durable local storage
- `yt-dlp`, `ffmpeg`, and `ffprobe` on the Home Ingest host only
- a compatible NVIDIA GPU provider for cloud ACE-Step inference, or the
  optional ACE Node environment on one supported private GPU host
- Xcode command-line tools and an Apple Silicon Mac for the optional native
  menu-bar app
- FluidSynth, the GM soundfont, and `lameenc` only on the optional mock host

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
  public hostname. `ACE_SERVICE_INCOMING_DIRECTORY_MODE` defaults to `0700`;
  the isolated beta controller sets it to `0770` for its restricted SFTP
  group while keeping the data root and all other directories private;
- `ACE_TRANSFER_*`: public signed-transfer origin, limits, and token lifetime.
  New direct/source/clip/derivative transfers use v2 capabilities; the raw
  source and canonical MP3 limits are exactly 536,870,912 bytes (512 MiB).
- `ACE_DIRECT_UPLOAD_MAX_BYTES`, `ACE_CANONICAL_SOURCE_MAX_BYTES`, and
  `ACE_ASSET_DOWNLOAD_MAX_OPENS`: direct upload/canonical limits and bounded
  Home Ingest download retries;
- `ACE_HOME_INGEST_*`: private Home Ingest endpoint and bearer token;
- `MOCK_BASE_URL`, `MOCK_TOKEN`, `MOCK_POLL_INTERVAL_SECONDS`, and
  `MOCK_*_TIMEOUT_SECONDS`: private mock endpoint credentials and bounded
  controller timeouts; enabling `mock/midi-sequential` requires a non-placeholder
  token and a private URL;
- `INFERENCE_ENABLED_BACKENDS` and `DEFAULT_*_BACKEND`: exact backend IDs and
  mode-specific defaults for new jobs;
- `FAL_KEY` and `FAL_*`: optional reviewed fal.ai catalog, queue, CDN, and
  retention settings;
- `INFERENCE_PROVIDER`: legacy coarse-provider compatibility for existing
  deployment configuration;
- `RUNPOD_*`: Runpod API, endpoint, polling, and timeout settings;
- `SALAD_*`: Salad API, organization, project, queue, container group, and
  timeout settings;
- `SALAD_CAPACITY_EXPECTED_FINGERPRINT` and
  `RUNPOD_CAPACITY_EXPECTED_FINGERPRINT`: reviewed immutable capacity
  fingerprints derived from the pinned secret-free v1 fixture; omitting one
  disables that capacity manager. `capacity-preflight --once` compares live
  provider identity read-only before deployment;
- `WEB_PUSH_VAPID_PUBLIC_KEY`, `WEB_PUSH_VAPID_PRIVATE_KEY`,
  `WEB_PUSH_VAPID_SUBJECT`, `WEB_PUSH_ALLOWED_ENDPOINT_ORIGINS`, and
  `WEB_PUSH_SEND_TIMEOUT_SECONDS`: optional browser notification settings;
  the private key stays only in deployment-managed configuration;
- `ACESTEP_*` and `RUNPOD_WORKER_RUNTIME_IDENTITY`: pinned model and worker
  identity recorded with jobs and outputs.
- `ACE_NODE_BASE_URL`, `ACE_NODE_TOKEN`, and
  `ACE_NODE_*_TIMEOUT_SECONDS`: private ACE Node controller connection. Keep
  `node/ace-step-v15-xl-turbo` out of `INFERENCE_ENABLED_BACKENDS` until a
  real node passes the authenticated readiness and hardware acceptance gate.

Keep-warm is not an environment setting. After schema v9 migration, the
authenticated dashboard stores the exact global value in SQLite, defaulting to
15 minutes. Enable notifications from the explicit dashboard button; the
service worker is scoped to the configured root path.

Placeholder credentials are rejected. Never commit `.env`, API keys, database
files, generated audio, private fixtures, or capability URLs.

## Local processes

Run the private controller:

```text
uv run python -m ace_service
```

The deployment watchdog can run dead-man reconciliation without starting a
second controller:

```text
uv run python -m ace_service capacity-reconcile --once
```

Use `uv run python -m ace_service capacity-preflight --once` for an inspection
only identity check. It never changes a provider floor.

Run the public transfer app as a separate process using the same data root:

```text
uv run python -m ace_service.transfer_main
```

Run Home Ingest from its own directory:

```text
cd home_ingest
uv run python -m ace_home_ingest
```

Run the optional mock service from its own directory after staging its
immutable corpus archive and manifest:

```text
cd midi_mock_backend
uv run python -m ace_midi_mock serve
```

Run an ACE Node only on its separately prepared target host using the launcher
and service templates in [docs/ACE-NODE.md](docs/ACE-NODE.md). The node's
heavyweight runtime is resolved only by the separate `deploy/node/` uv project
and lock; the normal controller `uv sync --frozen` environment does not
install or import torch, ACE-Step, MLX, nano-vllm, or node model weights.

The native Apple Silicon menu-bar supervisor is built from a clean committed
checkout. A development app can be assembled without downloading the runtime:

```text
cd deploy/node/macos
./build-app.sh --development
```

The embedded runtime needs at least 30 GiB free before its download/build, and
first-run model preparation needs at least 55 GiB free (70 GiB recommended).
Models are never included in the app or DMG. See [the ACE Node runbook](docs/ACE-NODE.md)
for the runtime, setup, signing, and acceptance gates.

Default binds are:

- controller: `127.0.0.1:8000`;
- transfer service: `127.0.0.1:8001`;
- Home Ingest: `127.0.0.1:8100`.
- mock service: `127.0.0.1:8200` by default; beta deployment uses p100 `:8201`.

Do not expose these ports directly. The controller belongs behind the private
tailnet. The public proxy may forward only the signed transfer paths described
in [the operations runbook](docs/OPERATIONS.md). The controller never runs
`ffmpeg`/`ffprobe`; all media probing and transcoding stays in Home Ingest.

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

Schema v12 adds the nullable source backend preference to the v11 source
assets, alongside the existing v2 capabilities, provenance, backend-frozen
clip bounds, and MP3 derivative tasks. The migration is additive; v11 source
rows retain a `NULL` preference and no source, media, job, or provider state is
backfilled. See the [operations runbook](docs/OPERATIONS.md) for backup,
rehearsal, and rollback sequencing. The current expected schema is v12.

## Browser verification

Install the checked-in browser test dependencies and both supported engines:

```text
uv sync --frozen
uv run playwright install --with-deps chromium firefox
uv run pytest -q tests/e2e --browser chromium --browser firefox
```

The browser suite uses a disposable loopback server, real local
ffmpeg/ffprobe, real v2 transfer streaming, and a non-paid provider stub. It
checks the `/beta` root-path contract, source backend selection and persistence,
source upload and playlist ordering, one global player across soft navigation,
mobile target sizes, deletion, and cancellation. It also checks notification
UI states, the unified worker, content-addressed offline caching, exact
full/range playback, cleanup, quota errors, refresh, and the offline shell in
Chromium and Firefox. Use a secure
context (HTTPS or localhost) for service workers; Firefox desktop supports
offline playback but does not provide manifest-based installation. Chromium
Android is the installation target. The protected beta mock acceptance script
is separate and requires protected credentials plus a caller-approved YouTube
URL; see [Operations](docs/OPERATIONS.md).

## Offline playback

Open the authenticated Offline page while online to inspect browser-local
owners. Starting an eligible MP3 online saves it in the current playlist
context, or in `Played tracks` when no playlist context exists. `Keep offline`
fetches and validates the complete ordered playlist snapshot, estimates missing
bytes, and downloads each distinct MP3 once. Duplicate playlist entries remain
distinct in playback order while sharing the stored body.

Offline snapshots are read-only. Reconnect to refresh a server playlist after a
rename, reorder, append, or removal. A successful server media, project, or
playlist deletion invalidates matching local references as the browser returns
to the app. Remove one owner to release only its references; shared bytes remain
until the final owner is removed. Browser site-data controls are the emergency
full reset. Use only a trusted browser profile because cached titles and audio
are not encrypted.

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

cd ../midi_mock_backend
uv sync --frozen
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

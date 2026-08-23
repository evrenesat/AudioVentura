# Operations

This runbook covers the controller, transfer service, Home Ingest, database,
and routine recovery. Provider-specific image and infrastructure work is in
[Runpod](RUNPOD.md) and [SaladCloud](SALAD.md).

## Deployment layout

The normal deployment uses:

- one Hetzner service account for the controller and transfer processes;
- one durable private data root, normally `/srv/ace-service/data`;
- one private Home Ingest service on the home network;
- one or more explicitly enabled inference backends for new jobs;
- a private proxy for the UI and a public HTTPS proxy for signed transfers.

Application code and deployment configuration are separate. Do not store
secrets, database files, generated audio, or logs in the checkout.

## Controller installation

Create the data root with private ownership and install the locked environment:

```text
sudo install -d -o ace-service -g ace-service -m 0700 /srv/ace-service/data
uv sync --frozen
```

Use `.env.example` as a field list, not as a production file. At minimum set:

```text
ACE_SERVICE_DATA_ROOT
ACE_SERVICE_USERNAME
ACE_SERVICE_PASSWORD
ACE_SERVICE_PUBLIC_HOSTNAME
ACE_TRANSFER_PUBLIC_BASE_URL
ACE_HOME_INGEST_BASE_URL
ACE_HOME_INGEST_TOKEN
INFERENCE_PROVIDER
```

For Runpod, also set `RUNPOD_API_KEY` and `RUNPOD_ENDPOINT_ID`. For Salad, set
`SALAD_API_KEY`, `SALAD_ORGANIZATION`, `SALAD_PROJECT`, and the tracked queue
and container-group names. Keep credentials for any provider that still owns a
nonterminal historical job.

For fal.ai, first follow [the Fal runbook](FAL.md). Add only audited
`fal/<endpoint-id>` values to `INFERENCE_ENABLED_BACKENDS`, set the two mode
defaults to enabled IDs, and keep `FAL_KEY` available while any nonterminal Fal
job remains. Fal prices are informational and never gate submission.

Start the controller:

```text
uv run python -m ace_service
```

Start the transfer service as a separate process with the same configuration:

```text
uv run python -m ace_service.transfer_main
```

The controller binds to `127.0.0.1:8000` and the transfer service to
`127.0.0.1:8001` by default.

## Reverse proxies

Expose the controller only through Tailscale Serve or another private proxy.
The public proxy may forward only:

```text
/transfer/v1/source/*  ->  127.0.0.1:8001
/transfer/v1/output/*  ->  127.0.0.1:8001
```

Reject every other path before it reaches the transfer app. Never publish
ports 8000 or 8001 directly.

Disable access logging for the transfer paths. If the proxy cannot do that,
redact the final path segment before persistence. The path contains a bearer
capability; do not store the full token, a prefix, or a hash prefix.

For a subpath deployment, set `ACE_SERVICE_ROOT_PATH` to the exact public
prefix, for example `/beta`. The private proxy must strip that prefix while
setting the ASGI root path consistently. Do not derive it from untrusted
forwarding headers.

## Home Ingest

Install the separate package on the home host:

```text
cd home_ingest
uv sync --frozen
uv run python -m ace_home_ingest
```

The host needs current `yt-dlp`, `ffmpeg`, and `ffprobe` binaries. Configure
the values in `home_ingest/.env.example`, including the bearer token and the
restricted SFTP identity.

The SFTP account should:

- use key-only authentication;
- have no interactive shell;
- be restricted to the controller's `incoming/` directory;
- accept only UUID-derived job paths.

Home Ingest binds to `127.0.0.1:8100` by default. Make it reachable only over
the private tailnet. Its authenticated `GET /healthz` is the basic readiness
check.

Temporary job directories are removed after success or failure. Startup and
periodic cleanup remove old orphan directories. Debug artifact retention is
off by default and must remain time-bounded if enabled.

## Startup order

1. Mount the durable data volume and verify its ownership and free space.
2. Start the transfer app and public proxy.
3. Start Home Ingest and check its authenticated `/healthz`.
4. Start the controller. It checks the schema, takes the data-root lock,
   recovers durable jobs, and runs cleanup before accepting new work.
5. Check controller `/healthz` and authenticated `/readyz`.
6. Inspect the configured provider without submitting paid work.

Only one controller worker may own a data root.

## Provider selection

`INFERENCE_ENABLED_BACKENDS` is a comma-separated allowlist of exact backend
IDs. `DEFAULT_ORIGINAL_BACKEND` and `DEFAULT_COVER_BACKEND` must be enabled and
mode-compatible. The selected backend is copied onto a new job before enqueue.
Do not change provider or backend fields on existing jobs.

When changing the default:

1. keep the old provider credentials available;
2. confirm all provider resource names and image identities;
3. restart the controller with the new default;
4. submit one bounded acceptance job;
5. confirm output integrity and provider scale-to-zero behavior;
6. retain rollback configuration until old jobs are terminal.

The controller never uses one provider as an implicit fallback for a persisted
job. A temporary status error is not permission to resubmit.

## Database migration

Normal startup never upgrades an existing database. Inspect it first:

```text
uv run python -m ace_service migrate-status \
  --database /srv/ace-service/data/service.db
```

Before an upgrade:

1. stop the controller and transfer app;
2. make a SQLite API backup, not a live filesystem copy;
3. run `PRAGMA integrity_check` on the source and backup;
4. keep the backup outside the data root being upgraded;
5. run the explicit upgrade;
6. rerun status and application readiness checks.

```text
uv run python -m ace_service migrate-upgrade \
  --database /srv/ace-service/data/service.db
```

The upgrade uses a sidecar lock and durable attempt marker. If it reports an
incomplete or failed migration, do not retry it. Restore the verified backup.

Before rolling a schema-2 deployment back to a schema-1 controller, run:

```text
ACE_SERVICE_DATA_ROOT=/srv/ace-service/data \
uv run python -m ace_service.rollback_readiness
```

Any nonzero or indeterminate result means the schema-2 controller and worker
must remain active.

## Backups

Back up these items together:

- `service.db` through SQLite's backup API;
- completed files under `outputs/`;
- any retained source files under `incoming/`;
- deployment configuration through the private configuration repository or
  secret store.

Do not copy a live SQLite file with plain `cp`. Do not include transfer tokens,
API keys, or private fixture media in diagnostic archives.

After restoring, verify database integrity, output path containment, recorded
file size and SHA-256, schema version, and service-account ownership before
starting either application process.

## Cleanup and retention

Controller cleanup runs at startup and on a configured interval. It may:

- remove stale `.part` files;
- expire or revoke transfer capabilities;
- prune old capability records;
- remove non-retained terminal cover sources.

It never removes completed outputs. Generated audio currently requires manual
operator retention decisions.

Logs rotate under the data root with private permissions and UTC timestamps.
They contain bounded job, phase, error-code, timing, and byte-count metadata.
They must not contain prompts, lyrics, credentials, provider response bodies,
or capability URLs.

## Job recovery

After a controller restart, durable jobs are recovered from SQLite:

- queued jobs may continue toward submission;
- jobs with a provider reference resume polling that exact reference;
- a submission nonce without a provider reference becomes an uncertain
  submission and is never resubmitted automatically;
- terminal jobs remain terminal;
- missing or conflicting output evidence fails closed.

For a job that appears stuck:

1. record its application job ID and persisted provider;
2. inspect controller logs using the job ID, without printing request bodies;
3. inspect the exact provider job ID through the provider console or API;
4. verify transfer-service availability and output storage free space;
5. wait for the durable deadline unless the provider proves a terminal state;
6. do not create a replacement job to work around uncertain status.

Provider status shown as “deployment status (inferred)” describes worker
infrastructure, not an authoritative assignment to the job.

## Live acceptance

Local tests do not prove deployment behavior. After controller, worker image,
provider configuration, or proxy changes, run one bounded cold job and record:

- provider and external job ID;
- submitted, queued, provisioning, starting, and running timestamps;
- image pull or model initialization evidence when available;
- output byte size and SHA-256 agreement;
- actual GPU and immutable image/model identities;
- queue delay, execution time, and approximate cost when available;
- time until the provider returns to zero workers.

For covers, also verify Home Ingest, source size/hash validation, signed source
download, and source cleanup. Keep source URLs and capability URLs out of the
record.

The optional paid browser smoke in `tests/live_paid_ui_e2e.py` is excluded from
normal test discovery. It requires protected credentials and an explicit
`--allow-paid` flag. Do not run it as a routine test.

## Quality campaign

The quality-evaluation CLI is currently quarantined. Do not run
`python -m ace_service.quality_eval` in production. Its separate fixture,
database, scoring, budget, and teardown contract remains documented in
[QUALITY-EVALUATION.md](QUALITY-EVALUATION.md) for later reactivation.

## Verification before deployment

From the repository root:

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

Also run the focused provider commands in [RUNPOD.md](RUNPOD.md) or
[SALAD.md](SALAD.md), plus `shellcheck deploy/salad/entrypoint.sh` for Salad
image changes.

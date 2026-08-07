# Operations runbook

This runbook describes the first-release deployment split. The controller and
transfer service run on Hetzner, the home agent is a separate private service,
and the Runpod worker is the only inference runtime.

## Hetzner setup

Create a dedicated service account and a private data root, then install the
controller from a clean checkout with Python 3.12 and `uv`:

```text
sudo install -d -o ace-service -g ace-service -m 0700 /srv/ace-service/data
uv sync --frozen
```

Set the required values in a deployment-only `.env` file. At minimum configure
`ACE_SERVICE_DATA_ROOT`, `ACE_SERVICE_USERNAME`, `ACE_SERVICE_PASSWORD`,
`ACE_HOME_INGEST_BASE_URL`, `ACE_HOME_INGEST_TOKEN`, `RUNPOD_API_KEY`,
`RUNPOD_ENDPOINT_ID`, and an HTTPS `ACE_TRANSFER_PUBLIC_BASE_URL`. Placeholder
credentials are rejected at startup. Keep `.env`, `service.db`, generated
audio, and logs outside source control.

The controller binds to `127.0.0.1:8000`; start it with:

```text
uv run python -m ace_service
```

Run the transfer app as a separate process using the same data root and
credentials. It binds to `127.0.0.1:8001` and exposes only the two
`/transfer/v1/` capability route families. The database and filesystem must be
on durable storage and included in backups:

```text
uv run python -m ace_service.transfer_main
```

Do not delete `outputs/` during cleanup: completed outputs are retained by
design.

## Tailscale and public HTTPS

Join Hetzner and the home server to the same tailnet. Expose only the
controller port through Tailscale Serve (or an equivalent private reverse
proxy). The home agent should be reachable by its tailnet hostname from the
controller, not from the public Internet.

The public proxy must terminate trusted HTTPS and forward only:

```text
/transfer/v1/source/*  ->  127.0.0.1:8001
/transfer/v1/output/*  ->  127.0.0.1:8001
```

Reject every other path at the proxy. A DNS hostname is preferred; a trusted
IP-address certificate is acceptable if certificate renewal is automated.
Never publish ports 8000 or 8001 directly. Restrict SSH by the operator's
administration policy. Prefer tailnet SFTP for the home upload; no public SFTP
listener is required. The proxy must disable access logging for
`/transfer/v1/*`, or redact the final path segment before persisting access
records, so capability-bearing request paths are never stored. Do not persist
a shortened or hashed prefix of the live capability token either.

## Home ingest setup

On the home server, install the independent package and configure
`ACE_HOME_INGEST_TOKEN`, `ACE_SFTP_HOST`, `ACE_SFTP_USERNAME`,
`ACE_SFTP_PRIVATE_KEY`, and `ACE_SFTP_REMOTE_ROOT`:

```text
cd home_ingest
uv sync --frozen
uv run python -m ace_home_ingest
```

The agent binds to localhost by default. It needs current `yt-dlp`, `ffprobe`,
and `ffmpeg` binaries. Update `yt-dlp` deliberately and rerun the home test
suite after updates. The restricted SFTP account should be key-only, have no
shell, and be confined to the Hetzner `incoming/` root. The uploaded filename
is always the UUID-derived `incoming/<job-id>/source.mp3.part`.

Home temporary job directories are removed on success and failure. Startup and
periodic cleanup removes orphan directories older than
`ACE_ORPHAN_AGE_SECONDS` (one day by default); explicit debug retention is
bounded by `ACE_HOME_DEBUG_RETENTION_SECONDS`.

## Runpod deployment

Build and push the pinned worker for an amd64 target:

```text
docker build --platform linux/amd64 -f runpod_worker/Dockerfile -t <registry>/ace-step-worker:<tag> .
docker push <registry>/ace-step-worker:<tag>
```

Create a queue-based Serverless Flex endpoint with `workersMin=0`,
`workersMax=1`, one RTX 4090-class 24 GB GPU, a 30-second idle timeout, and a
1200-second execution timeout. Provide only the worker checkpoint path and
`ACE_TRANSFER_ALLOWED_HOST`. Do not provide Runpod API, home-ingest, SFTP,
SSH, Tailscale, or controller credentials to the worker.

Model checkpoints are the pinned ACE-Step v0.1.8 set documented in
`docs/RUNPOD.md`. Keep model caching either in the image or on the documented
Runpod network volume; do not download weights per request. The network volume
is an optional fallback when baking or caching the checkpoints in the worker
image is not reliable in the selected region.

## Startup order and cleanup

1. Mount the Hetzner data volume and verify its ownership/mode.
2. Start the transfer app and its HTTPS proxy.
3. Start the controller; it initializes SQLite, acquires the data-root lock,
   recovers durable jobs, and runs cleanup before accepting work.
4. Start the home agent and verify its authenticated `/healthz` endpoint.
5. Confirm the controller `/healthz` and authenticated `/readyz` routes.

The controller logs to a UTC rotating file under `logs/` with private `0600`
permissions. `ACE_LOG_MAX_BYTES`, `ACE_LOG_BACKUP_COUNT`, and `ACE_LOG_LEVEL`
control rotation and verbosity. Cleanup runs at startup and every
`ACE_CLEANUP_INTERVAL_SECONDS` seconds. It removes stale `.part` files older
than `ACE_CLEANUP_STALE_AFTER_SECONDS`, expires/revokes terminal capabilities,
prunes old capability records after `ACE_TRANSFER_RECORD_RETENTION_SECONDS`,
and removes non-retained terminal cover sources. It never removes completed
outputs. Logs contain job/stage/error/timing metadata but redact credentials,
authorization headers, capability URLs, prompts, and lyrics.

## Backups and diagnosis

Back up `service.db` with SQLite-aware file/database tooling and the
`outputs/` directory together. Do not restore a database without its matching
output files. A missing output fails closed at the playback route. Preserve
recent rotated logs when diagnosing a failure, but treat them as sensitive.

Useful local checks are:

```text
uv run pytest -q
uv run ruff check src tests home_ingest runpod_worker
uv run ruff format --check src tests home_ingest runpod_worker
uv run mypy src/ace_service
cd home_ingest && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

For a failed cover, inspect the controller error code, home log stage, SFTP
file size/checksum, and the source capability status in SQLite. For an output
failure, check the output `.part` cleanup, capability expiry, and the worker's
bounded upload metadata. Never paste bearer tokens, API keys, capability URLs,
lyrics, or private keys into an issue or log message.

## Smoke and acceptance procedure

Run these checks against the deployed services, recording UTC timestamps and
the non-secret endpoint/image identifiers:

1. With workers at zero, generate a 20-second original; record cold start,
   queue delay, execution time, upload, playback, and return-to-zero time.
2. Generate a second variation within the 30-second idle window and record
   warm startup timing.
3. Request two and then four original variations; verify distinct Runpod IDs,
   serialized execution, and retained earlier outputs after any failure.
4. Submit an approved YouTube URL; verify YouTube, `yt-dlp`, `ffprobe`, and
   `ffmpeg` activity on the home host, plus matching SFTP size/checksum.
5. Complete a cover; verify Runpod sees only the signed HTTPS source URL and
   metadata, then verify upload and playback.
6. Stop home ingest; verify originals still submit and covers fail safely.
7. Restart the controller during Runpod execution; verify the persisted cloud
   ID is polled and no second `/run` request is created.
8. Exercise an expired capability, public non-tailnet UI access, valid public
   transfer access, cleanup, and completed-output retention.
9. Compare Runpod execution/billing data and record approximate cents per
   representative 20-second, 180-second, and cover job.

Record the endpoint ID, worker image digest, ACE-Step tag/model names, GPU,
cold/warm timings, initialization time, execution times, idle-cost observation,
and peak VRAM in `docs/RUNPOD.md`. Do not claim live acceptance from the local
mocked test suite.

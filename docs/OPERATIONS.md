# Operations

This runbook covers the controller, transfer service, Home Ingest, database,
and routine recovery. Provider-specific image and infrastructure work is in
[Runpod](RUNPOD.md) and [SaladCloud](SALAD.md).

## Deployment layout

The normal deployment uses:

- one Hetzner service account for the controller and transfer processes;
- one durable private data root, normally `/srv/ace-service/data`;
- one private Home Ingest service on the home network;
- one private sequential MIDI mock service on the home network when enabled;
- one or more explicitly enabled inference backends for new jobs;
- a private proxy for the UI and a public HTTPS proxy for signed transfers.

Application code and deployment configuration are separate. Do not store
secrets, database files, generated audio, or logs in the checkout.

## Isolated beta

The media-library/player foundation is deployed first to the isolated beta
surface managed in the evreniops repository. Beta uses a separate release
link, systemd units, environment file, data root, database, media/trash tree,
and rollback snapshot:

| Surface | Beta | Production |
| --- | --- | --- |
| Controller | `127.0.0.1:8010`, `https://player.evren.io/beta/` | `127.0.0.1:8000` |
| Transfer | `127.0.0.1:8011`, `/beta-transfer/transfer/v1/{source\|output}/<token>` and `/beta-transfer/asset-transfer/v2/{upload\|download}/<token>` | `127.0.0.1:8001` |
| Home Ingest | p100 `:8101`, beta-only restricted SFTP | p100 `:8100` |
| MIDI mock | p100 `:8201`, opt-in | p100 `:8200`, opt-in |
| Data | `/srv/ace-service-beta/data` | `/srv/ace-service/data` |
| Config | `/etc/audioventura/beta.env` | `/etc/audioventura/controller.env` |

Run the beta playbook from the managed evreniops checkout only after the
product commit is pinned in that playbook. It snapshots the beta database and
runtime definitions once per deploy, runs the explicit migration, activates
the immutable release atomically, and reloads nginx only after configuration
validation. It never stops or rewrites the production services. Beta enables
only the reviewed bounded Fal smoke path, disables capacity fingerprints and
Web Push, and uses separate p100 Home Ingest and SFTP identities. The
sequential MIDI mock is opt-in and does not become a default.

```text
cd /root/code/evreniops/infra/ansible
./ops deploy-audioventura-beta -e audioventura_beta_mode=deploy
./ops deploy-audioventura-beta -e audioventura_beta_mode=verify
```

If beta activation fails, the playbook restores its recorded snapshot. For an
operator-selected rollback, use the exact snapshot path printed by the deploy
and keep production untouched:

```text
./ops deploy-audioventura-beta -e audioventura_beta_mode=rollback \
  -e audioventura_beta_rollback_snapshot=/opt/audioventura/beta-rollback/<UTC-snapshot>
```

The beta URL, deployed product revision, deployment-repository revision, and
verification result belong in the handoff record. Never call the beta result
production approval; production requires a separate explicit approval after
manual beta testing.

## Private home services and MIDI mock

The companion evreniops playbook deploys the isolated beta Home Ingest,
restricted beta SFTP account, and opt-in sequential MIDI mock as a paired
snapshot. It stages the exact corpus archive and canonical manifest outside
Git, validates the pinned FluidSynth and General MIDI soundfont packages, and
keeps beta and production state roots, credentials, cursors, and service users
separate.

```text
cd /root/code/evreniops/infra/ansible
./ops deploy-audioventura-home-services \
  -e audioventura_home_target=beta \
  -e audioventura_home_mode=deploy \
  -e audioventura_product_commit=<product-commit>
./ops deploy-audioventura-home-services \
  -e audioventura_home_target=beta \
  -e audioventura_home_mode=verify \
  -e audioventura_product_commit=<product-commit>
```

The beta controller talks to Home Ingest on p100 `:8101` through its private
tailnet address. New source, clip, and derivative operations use the beta
`/beta-transfer/asset-transfer/v2/...` capability prefix; legacy cover
recovery may still use the beta-only chrooted SFTP account and v1 route. The
mock listens on p100 `:8201`, requires its own bearer token, and accepts only
the exact `mock/midi-sequential` backend. It receives source metadata for
contract coverage but never follows a source URL. A service
rollback restores the paired mock cursor/state, Home Ingest state, controller
overlay, and beta SFTP mapping from one explicit snapshot:

```text
./ops deploy-audioventura-home-services \
  -e audioventura_home_target=beta \
  -e audioventura_home_mode=rollback \
  -e audioventura_home_rollback_snapshot=/opt/audioventura-midi-mock/rollback/<UTC-snapshot>
```

The p100 snapshot path is the operator-facing input; the playbook derives the
matching Hetzner beta `home-services` snapshot from the same UTC identity.

Keep the corpus and generated state on the service hosts. Do not add the ZIP,
manifest, generated MP3s, SQLite state, or environment files to either Git
repository.

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

Keep `ACE_SERVICE_INCOMING_DIRECTORY_MODE=0700` for normal deployments. The
isolated beta controller sets it to `0770` so its restricted SFTP group can
deliver into `incoming/`; the controller data root and all other directories
remain private.

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
The public proxy may forward only these exact route families:

```text
/transfer/v1/source/*  ->  127.0.0.1:8001
/transfer/v1/output/*  ->  127.0.0.1:8001
/asset-transfer/v2/upload/<token>    ->  127.0.0.1:8001
/asset-transfer/v2/download/<token>  ->  127.0.0.1:8001
```

The v2 upload location alone accepts a raw body up to exactly 512 MiB and has
request/response buffering disabled. The v2 download location is separately
allow-listed and revalidated by the transfer app; it does not receive the
large-body nginx exception. Beta uses the same locations below the
`/beta-transfer` prefix and reaches port `8011`, never production port `8001`.

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

Home Ingest exposes bearer-authenticated v2 operations for source preparation,
clip staging, and FLAC/WAV playback derivatives:

```text
POST /v2/prepare-source
POST /v2/prepare-clip
POST /v2/prepare-playback-derivative
```

These operations accept only controller-issued transfer URLs matching the
configured scheme, host, port, and exact v2 route. They stream to private
temporary directories, verify size and SHA-256, use the first audio stream,
strip metadata, and normalize to stereo 48 kHz 192 kbps MP3. Uploaded media is
not duration-limited at ingestion; byte, subprocess, and network timeouts are
the limits. The legacy YouTube/SFTP endpoint remains for persisted recovery
only.

Temporary job directories are removed after success or failure. Startup and
periodic cleanup remove old orphan directories. Debug artifact retention is
off by default and must remain time-bounded if enabled.

## Startup order

1. Mount the durable data volume and verify its ownership and free space.
2. Start the transfer app and public proxy.
3. Start Home Ingest and check its authenticated `/healthz`.
4. When the mock is enabled, start the matching p100 mock unit and check its
   authenticated `/healthz`; it must report the reviewed corpus identity.
5. Start the controller. It checks the schema, takes the data-root lock,
   recovers durable jobs, and runs cleanup before accepting new work.
6. Check controller `/healthz` and authenticated `/readyz`.
7. Inspect the configured provider without submitting paid work.

Only one controller worker may own a data root.

## Provider selection

`INFERENCE_ENABLED_BACKENDS` is a comma-separated allowlist of exact backend
IDs. `DEFAULT_ORIGINAL_BACKEND` and `DEFAULT_COVER_BACKEND` must be enabled and
mode-compatible. The selected backend is copied onto a new job before enqueue.
Do not change provider or backend fields on existing jobs.

To use the deterministic backend, explicitly include
`mock/midi-sequential`, set both defaults as desired, and provide the private
mock URL and token. It is MP3-only, supports both built-in form modes and
features, consumes one corpus cursor index per accepted nonce, and does not
use Home Ingest source bytes. It is never an implicit fallback for a real
provider and is not enabled by the normal defaults.

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

Schema v11 adds source assets, signed asset-transfer capabilities, source
provenance, backend-frozen clip fields, and derivative tasks to the v10 media
library. The migration is additive: existing v10 output/media/playlists remain
unchanged, and an existing experimental `kind='source'` row fails closed
instead of being guessed into the new ownership model. Run the explicit
v10-to-v11 rehearsal and confirm `older_version` → upgrade →
`exact_expected` before deployment. Keep the backup and database on the same
recovery record. Never start a v10 release against a v11 database.

New data is stored below the configured root as `uploads/<source-uuid>/`,
`library/sources/<source-uuid>/source.mp3`,
`library/generated/<media-uuid>/playback.mp3`, and
`incoming/<job-uuid>/source.mp3`. All source/upload/derivative capabilities
are revoked when their owning operation resolves; controller cleanup removes
safe abandoned parts and retries only the durable operation that owns them.

Project deletion uses the committed audit as its recovery marker. It first
reconciles library media through the normal tombstone transition, then moves
unpublished legacy or FLAC/WAV outputs into
`trash/project-outputs/<project-id>/` after path, MIME, size, and SHA-256
verification. Output rows remain until the project transaction commits. A
retry or startup cleanup recognizes deterministic moves and purges the
project-scoped trash after commit. Do not manually remove an active library
file or bypass the repository deletion transition.

For the media tree, verify `outputs/`, `library/`, and `trash/` are all below
the configured data root. A pending media deletion may be reconciled by
cleanup; purge is allowed only after the database state and trash path agree.
Do not manually remove an active library file or bypass the repository
deletion transition.

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
- completed files under `outputs/` and `library/`;
- any retained source/upload/clip files under `uploads/` and `incoming/`;
- deployment configuration through the private configuration repository or
  secret store.

Do not copy a live SQLite file with plain `cp`. Do not include transfer tokens,
API keys, or private fixture media in diagnostic archives.

After restoring, verify database integrity, every output/library/source path
is contained below the configured root, recorded file size and SHA-256, schema
version, and service-account ownership before starting either application
process.

## Cleanup and retention

Controller cleanup runs at startup and on a configured interval. It may:

- remove stale `.part` files;
- expire or revoke transfer capabilities;
- prune old capability records;
- reconcile consumed direct uploads and retryable source/derivative state;
- remove non-retained terminal cover sources and safe raw upload remnants;
- reconcile pending/deleted library items and purge only their deterministic
  trash paths after the delayed deletion policy permits it.

It never removes active completed outputs. A deleted library item remains
represented as a tombstone in job/project history, while its media file is
removed only through the private trash reconciliation path.

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

## Non-paid mock smoke test

When the beta mock is explicitly enabled, run the checked-in live harness with
an operator-approved YouTube URL to cover both the normal Home Ingest route and
the direct mock route. The harness verifies cursor order, repeated polling,
MP3 headers, range/full download hashes, requested form fields, and bounded
metadata without printing credentials, prompts, lyrics, or capability URLs:

```text
ACE_SERVICE_USERNAME=<protected-value> \
ACE_SERVICE_PASSWORD=<protected-value> \
MOCK_TOKEN=<protected-value> \
uv run python tests/live_beta_mock_e2e.py \
  --base-url https://player.evren.io/beta \
  --mock-base-url http://100.103.69.9:8201 \
  --youtube-url <operator-approved-url> \
  --expected-revision <product-commit>
```

Do not use this harness against production. A real-provider acceptance remains
separate and requires its own explicit spend approval.

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

For the source-ingest beta, use the disposable browser gate before the
protected non-paid live smoke:

```text
uv run playwright install --with-deps chromium firefox
uv run pytest -q tests/e2e --browser chromium --browser firefox
```

Then run the protected beta mock smoke with the exact
`mock/midi-sequential` backend. It must verify the beta URL, authenticated
source playback/download, v2 upload/download routing, a bounded subrange,
source/result playlist ordering, byte/hash evidence, raw/clip cleanup, and
active-source deletion refusal. Keep the smoke result free of credentials,
capability tokens, provider response bodies, prompts, and lyrics. If a
lossless fixture is needed, use a local deterministic test fixture; do not
make a paid request merely to obtain WAV or FLAC.

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
uv run pytest -q tests/e2e --browser chromium --browser firefox
uv run ruff check .
uv run ruff format --check .
uv run mypy src runpod_worker

cd home_ingest
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

For the standalone mock, also run from `midi_mock_backend/`:

```text
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Also run the focused provider commands in [RUNPOD.md](RUNPOD.md) or
[SALAD.md](SALAD.md), plus `shellcheck deploy/salad/entrypoint.sh` for Salad
image changes.

## Keep-warm, watchdog, and notifications

Schema v9 adds the database-owned keep-warm setting, capacity leases, and the
Web Push outbox. Back up SQLite and the protected environment before the
explicit migration sequence. Confirm `migration-status` reports
`exact_expected` after `migrate-upgrade`; application startup never migrates.

The dashboard setting is authoritative. It accepts only `0, 60, 120, 180,
300, 600, 900, 1800, 2700, 3600, 7200, 10800, 14400` seconds and defaults to
900. Zero skips new retention. Capacity managers are enabled only when their
reviewed `*_CAPACITY_EXPECTED_FINGERPRINT` is present and matches read-only
provider inspection. Never substitute an environment keep-warm value.

The controller reconciles every 15 seconds. A separate systemd timer invokes
`capacity-reconcile --once` every minute as a dead-man path. Both paths inspect
before acting, keep one worker maximum, wait for actual zero workers before
marking release complete, and retry only cost-reducing actions after an outage.
The deployment also runs `capacity-preflight --once`, which derives the
expected digest from the reviewed fixture and compares live immutable identity
read-only before service activation. The watchdog exits 2 for drift, malformed
or missing provider identity, and confirmed release overdue; it exits 1 while
release is still being observed or a transient/provider-work condition remains.
`release_overdue` is degraded readiness and requires operator attention; it is
not evidence that the provider has reached zero.

For VAPID rotation, generate a new P-256 key pair offline, replace the private
key only in the protected environment, update the public key and exact HTTPS
origin allow-list, restart the controller, and have browsers re-enable
notifications. Rotation invalidates existing subscriptions; do not log keys or
subscription endpoints.

If release is overdue, stop paid acceptance, inspect the exact provider
resource read-only, and use the provider-specific manual release procedure
only after proving no controller/provider work remains. Keep the controller and
watchdog running for idempotent cost-reducing retries. Never submit a replacement
generation to diagnose a release problem. Restore both providers to confirmed
zero before handoff.

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

## Quality campaign dry-run and gate

The quality campaign is quarantined during the usability recovery. Its
executable entrypoint (`python -m ace_service.quality_eval`) and the
ordinary-submission maintenance gate are disabled with a `TODO`: re-enable
after ordinary original and cover generation is stable. The campaign store,
evaluators, profiles, and campaign data remain in place and unit-testable;
do not run any campaign CLI mode in this recovery.

When the campaign is re-enabled, run the Checkpoint 3 dry-run from the
application release with the private fixture mounted:

```text
uv run python -m ace_service.quality_eval \
  --manifest /srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json \
  --dry-run
```

The output reports the ordered minimum/maximum jobs and paid attempts, the
worst-case reservation when a fresh rate catalog is supplied, and explicit
reasons for any inadmissible stage. It does not contact Runpod. Do not run
`--execute` until the operator has recorded the separate remote-change
authorization named in the quality plan, a read-only billing boundary probe,
fresh official Flex-rate evidence for every eligible GPU, the exact worker
digest/rollback target, and the rendered authenticated edge rule.

The campaign database is private and separate from `service.db`. Create its
SQLite-API backup with the operator action before any paid window:

```text
uv run python -m ace_service.quality_eval \
  --manifest /srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json \
  --campaign-id <opaque-id> --backup /srv/ace-service/backups/<opaque-id>.sqlite3
```

The operator must retain the active maintenance gate after a crash or uncertain
Runpod state. Restore ordinary submissions only after every opaque sample is
terminal or explicitly reconciled, the endpoint reports zero active workers,
the UTC window is closed, and the edge rollback guard is recorded and
verified. The rollback-readiness command is deliberately not a cleanup command;
nonzero or indeterminate output requires the dual-version controller/worker to
remain active.

### Score lifecycle and deterministic advancement

Export, import, and finalization all enforce exact current scoreable-set
coverage: every incumbent/candidate/corrected-controls sample of the stage
must be completed with output evidence, and the frozen sheet's `sample_order`
and pair memberships must equal the current campaign state exactly. A sample
declared after export — planned, completed, failed, or uncertain — or any
stale/duplicate pair makes import and finalization reject the sheet
deterministically; finalization also re-requires complete output evidence at
freeze time.

After both screening sheets are finalized, the operator advances with:

```text
uv run python -m ace_service.quality_eval \
  --manifest /srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json \
  --campaign-db <campaign-db> --campaign-id <opaque-id> --advance --confirm
```

`--advance` requires both `--campaign-id` and the fresh explicit `--confirm`
flag before it opens or mutates the campaign database; without `--confirm` it
returns the bounded blocked exit and creates no event, sample, or campaign
state. It accepts no finalist input: it derives per-task-type rankings from
the two finalized sheets with the frozen severe-artifact rule and the exact
cutoff-tie rule — a score-equivalence group that crosses the two-finalist
cutoff is excluded in its entirety, so a three-way tie for first advances
none, an exact two-way tie for first advances both, and a tie spanning
positions two and three advances only an untied first-place candidate — then
persists the exact finalist set and rankings as a durable `screening_advanced`
event (repeating the identical confirmed invocation is idempotent; a
conflicting set fails closed), materializes the confirmation cases (seed-one
aliases reuse the completed screening samples; seeds two and three are new
payable rows), and moves the campaign to `awaiting_confirmation`. Confirmation
execution then runs with
`--execute --stage confirmation`, which submits only the durable planned
payable samples; the exact-fingerprint seed-one aliases are never submitted or
charged twice.

### Recovery: status, reconciliation, and verified teardown

When a campaign window, maintenance gate, or pending submission intent is
open, ordinary score, advancement, decision, and execute actions are rejected;
`--backup` and read-only `--status` remain available. The operator inspects
and resolves the interrupted state with three bounded CLI actions:

```text
uv run python -m ace_service.quality_eval \
  --manifest <manifest> --campaign-db <campaign-db> --campaign-id <opaque-id> --status

uv run python -m ace_service.quality_eval \
  --manifest <manifest> --campaign-db <campaign-db> --campaign-id <opaque-id> \
  --reconcile --confirm

uv run python -m ace_service.quality_eval \
  --manifest <manifest> --campaign-db <campaign-db> --campaign-id <opaque-id> \
  --verified-teardown --confirm
```

`--status` opens only an existing campaign database and reports bounded opaque
campaign, window, gate, reservation, and per-sample state plus whether product
linkage and zero-worker evidence are complete; it never prints URLs, prompts,
lyrics, capabilities, listener mappings, credentials, or raw provider bodies.

All four recovery actions (`--status`, `--backup`, `--reconcile`, and
`--verified-teardown`) dispatch before the fixture manifest is loaded, hashed,
or rebuilt: they run from the frozen campaign/sample/submission-intent state
in the campaign database, so a missing, removed, or corrupted manifest never
blocks status, backup, reconciliation, or teardown while the maintenance gate
is open. The `--manifest` argument is still required and validated for
dry-run, execute, advancement, score-sheet, and decision modes, whose
semantics depend on the frozen fixture.

Every recovery action also validates the named campaign before acting: an
unknown `--campaign-id` returns the bounded blocked result before any backup
file, product database engine, controller worker, Home Ingest client, or
Runpod client is created. `--backup` may still copy the whole SQLite database,
but only after the named campaign is proven to exist — the ID is an operator
target guard, not a per-campaign export filter. `--verified-teardown` returns
`not_needed` only for a known campaign with no active gate belonging to it;
an active gate owned by a different campaign is rejected, never closed and
never reported as success.

`--reconcile` first settles the exact pre-intent crash boundary: a frozen
sample still `planned` with exactly one open compute reservation, no
`submitted_at_utc`, no submission intent, and no product-job link proves no
remote submission or product job existed, so it is atomically recorded as
proven unsubmitted and its reservation settled at zero, with an auditable
`pre_intent_reconciled` event — no product job is created and no provider is
contacted. Any contradictory evidence for that boundary (a submitted
timestamp, duplicate reservations, non-compute reservations) fails closed;
samples with a job link or a submission intent belong to the intent-present
phase below. It then resumes only the campaign's frozen UUID-linked product
jobs through the ordinary controller polling/transfer boundary. Each pending
submission intent completes its product-row/campaign linkage with the
preassigned product UUID (either crash order, never a second product job),
and every submitted/running/uncertain sample is driven to terminal or
uncertain evidence. Unknown or conflicting product rows are refused and no
new sample is admitted; repeating the action is idempotent.

`--verified-teardown` settles no reservation by assumption. It obtains the
validated Runpod `/health` evidence for the authorized endpoint and closes
the exact open window only when every submitted sample is terminal, every
reservation is financially resolved (settled, or conservatively retained for
a terminal unknown-cost attempt), and the provider reports zero active
workers and zero queued/in-progress jobs; otherwise it retains the
maintenance gate and returns a bounded blocked result. A conservatively
retained reservation never becomes an executed-attempt estimate: the full
immutable original `reserved_micro_usd` keeps counting in later budget and
admission totals, and only provider-observed zero plus a terminal sample lets
verified teardown close it. In-flight/uncertain attempts stay `unresolved`
with their full reservation and keep teardown and rollback blocked even with
provider-zero evidence. Terminal identity is immutable: failed, cancelled,
unsubmitted, and completed attempts reject conflicting later status, output,
GPU, execution, reason, or estimate evidence; only an uncertain attempt may
advance to a compatible terminal outcome, and a completed sample with
missing cost inputs may fill them in place only when any supplied output
path, GPU, execution, reason, or status matches the recorded evidence,
rejecting conflicts before any cost/reservation mutation. The campaign
database (schema v3) constrains reservations to exactly `open`,
`unresolved`, `conservatively_retained`, and `settled`, migrates v1/v2
stores to v3 as one atomic unit (a rejected migration leaves the source
schema version, objects, rows, reservation state, timestamps, and storage
child links unchanged), and fails closed on any unknown reservation state
before status, admission, teardown, recovery, or rollback can omit it. Zero-at-rest is provider-observed and fail-closed:
no product-job, sample, timing, or empty-response inference ever clears the
gate, and genuinely `open` or `unresolved` reservations still block it.

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

## Prepared SaladCloud deployment

SaladCloud is prepared as the first alternate inference backend, but it is not
an active controller path until the provider-abstraction migration is deployed.
The image, queue-worker wrapper, scale-to-zero desired state, immutable build
receipt, provisioning command, and zero-at-rest checks are in
`docs/SALAD.md`. Do not create paid Salad jobs from the infrastructure tooling;
job submission remains controller-owned.

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

## Schema migration and cost display

The database schema is versioned by an ordered migration runner. Normal
application startup never migrates: it creates the foundation tables only and
refuses to serve unless the schema is at the exact expected version.

```text
uv run python -m ace_service migrate-status --database /srv/ace-service/data/service.db
uv run python -m ace_service migrate-upgrade --database /srv/ace-service/data/service.db
```

`migrate-status` opens the database read-only and prints only a non-secret
path hash and the state (unversioned legacy, exact expected version, older,
unknown/newer, incomplete started/failed, missing, non-database, corrupt).
`migrate-upgrade` holds an exclusive sidecar lock
(`service.db.migration.lock`), commits a durable attempt marker in a short
transaction, then applies the additive schema in a separate exclusive
transaction. A crash or failure leaves a visible incomplete marker and the
next upgrade refuses; restore the verified pre-upgrade backup instead of
retrying. Before upgrading an existing deployment, create a SQLite-API backup
and run `PRAGMA integrity_check` on both the source and the copy, then
exercise legacy reads on the migrated copy before starting the new release.

The `billing-sync` operator command and its Runpod billing client were removed
from this release; historical `billing_observations`, `billing_projections`,
`submission_quotes`, rate-catalog, and calibration rows remain readable data
and are preserved by every migration. Cost display is now a read-only
informational calculation at the fixed `USD 0.50/GPU-hour` rate, computed on
read from the latest three completed attempt durations of the matching kind
(separate original and cover histories). Every label applies one half-up
rounding at the final four-decimal USD display boundary, and the visible
request total follows the selected variation count. It never approves,
delays, rejects, retries, or cancels generation, and no
quote/calibration/rate record is created for it. With no matching completed
history it shows a clearly labeled 60-second seed (`USD 0.0083` per
variation).

Populate `gpu_rate_catalog` and `runtime_calibrations` only from accepted,
timestamped operator evidence. Calibration lookup requires exact task mode,
profile, model/runtime identity, GPU class, duration mode/band, and output
count; out-of-band durations are not extrapolated. Set
`RUNPOD_WORKER_RUNTIME_IDENTITY` to the deployed worker image's immutable
`sha256:<64 hex>` digest whenever `ACE_ELIGIBLE_GPU_IDS` is configured. Startup
rejects a missing or malformed digest, and browser fields cannot override it.
This is an exact deployment/release identity, not `worker-schema-v2` or another
compatibility protocol label. Version reuse is permitted only for an exact
repeat. These records no longer gate or quote generation: the active cost
display is the fixed-rate read-only estimate described above, and the
historical rate/calibration rows stay readable for audit.

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

The repeatable paid browser smoke is deliberately excluded from normal pytest
discovery. Run it only on the target with `/etc/audioventura/controller.env`
loaded into the process and an explicit two-submission authorization:

```text
python tests/live_paid_ui_e2e.py \
  --allow-paid --max-paid-submissions 2 \
  --youtube-url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --prompt 'approved prompt'
```

It stops on the first failure, reports only non-secret job/project identities,
and covers authenticated pages, both duration controls, initial cover ingest,
completed-output continuation without a YouTube field, status polling,
projects, playback, and bounded downloads.

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

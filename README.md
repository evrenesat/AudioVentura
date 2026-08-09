# ACE Service

The controller is a private FastAPI web app for original-song and YouTube-cover
jobs. It binds to `127.0.0.1:8000`, uses HTTP Basic plus same-site CSRF for the
browser UI, persists jobs in SQLite, and serves completed audio only through
authenticated controller routes. The separate transfer app remains the only
publicly proxied surface and binds to `127.0.0.1:8001`.

Run the controller locally with configured credentials using:

```text
uv run python -m ace_service
```

The controller defaults to the public origin root. To publish it below a
prefix-stripping reverse proxy, configure the exact public prefix explicitly:

```text
ACE_SERVICE_ROOT_PATH=/beta uv run python -m ace_service
```

The proxy must remove `/beta` before forwarding while setting the ASGI request
root path consistently. The service never derives this prefix from request
headers. An empty `ACE_SERVICE_ROOT_PATH` preserves root deployment; `/`,
trailing or repeated slashes, dot segments, URLs, query/fragment content,
backslashes, and control characters are rejected at startup.

The schema is versioned and never migrated at startup. Use the explicit
commands with an explicit resolved database path (read-only status first,
then offline upgrade under an exclusive sidecar lock after a verified backup):

```text
uv run python -m ace_service migrate-status --database /path/to/service.db
uv run python -m ace_service migrate-upgrade --database /path/to/service.db
```

Schema v6 adds lightweight projects and backfills every historical job into
its own same-type project without rewriting generation, output, attempt,
billing, or transfer evidence. The private UI exposes `/projects` and one
server-rendered project page per project. A compatible schema-v2 job can
prefill the existing original or cover form; submitting the reviewed form
always creates a new job version in that project and never retries or mutates
the source job. Cover continuations require fresh rights confirmation and the
ordinary home-ingest/duration-confirmation flow.

Runpod billing is an operator-only boundary with no browser route:

```text
uv run python -m ace_service billing-sync \
  --database /path/to/service.db --start 2026-08-08T00:00:00Z --end 2026-08-09T00:00:00Z
```

The offline-verified adapter targets the documented
`https://rest.runpod.io/v1/billing` REST host. Provider totals remain
unavailable until interval semantics are separately proven; network-volume
evidence is account-wide and excluded from AudioVentura endpoint totals.
Submission quotes require fresh rates for every eligible GPU and one exact
persisted runtime-calibration match. `RUNPOD_WORKER_RUNTIME_IDENTITY` pins the
deployed worker image as an exact `sha256:<64 hex>` release identity; it is
server configuration and never browser input. An empty or different-runtime
calibration catalog truthfully produces `calibration_missing`.

Operational deployment, Tailscale/proxy policy, cleanup, backups, and live
acceptance are documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).

The detailed deployment and distributed-runtime handoff follows below.

# aflow Handoff Plan: Hetzner + Home Ingest + Runpod Flex ACE-Step Service

Build the first usable release of a private music-generation service with a deliberately split runtime:

1. **Hetzner VM is the permanent control plane and web application.**
2. **Home server is the YouTube/media-ingest node.** Every `yt-dlp`, `ffprobe`, and `ffmpeg` operation runs there so YouTube requests originate from the residential/home connection. Runpod encodes generated MP3 output in-process with LAME; Hetzner performs no media processing.
3. **Runpod Serverless Flex is the only ACE-Step inference backend in v1.** The MacBook/MLX path is deferred.
4. **Runpod never receives YouTube credentials, SSH keys, SFTP credentials, or direct access to the home network.**
5. **Large audio never travels inside the Runpod `/run` JSON payload.** Hetzner exposes narrowly scoped, short-lived HTTPS capability URLs for source download and result upload.

The first release supports two workflows:

1. Generate an original song from creative instructions, optional lyrics, optional musical metadata, and one to four sequential variations.
2. Generate a cover or stylistic reinterpretation from a single public YouTube video after the home server downloads and prepares the source audio.

Jobs remain the unit of queueing, execution, status, outputs, quotes, billing,
and transfer capabilities. Projects only group same-type jobs for naming,
continuation, and version comparison; they do not add execution state.

New jobs use a strict version-2 worker payload. Original requests choose
`direct`, `enhance`, or `auto-compose` prompting and either model-selected
duration (`auto`, sent as `-1.0`) or an explicit 10-600 second custom value.
The final caption and lyrics are bounded at 511 and 4095 characters. Cover
requests expose ACE-Step's independent `audio_cover_strength` and
`cover_noise_strength` controls, retain the probed source duration, and stage
for browser confirmation before any Runpod submission. The staged cover page
can confirm or cancel preparation, and asynchronous status polling reloads it
once when confirmation becomes available. Two to four cover variations run
sequentially; a supplied seed advances deterministically. Duration prose is
accepted only when bounded numeric seconds/minutes match an explicit custom
duration; it never changes the structured value.

Immediately before starting a schema-v1 controller rollback, run the local
read-only gate against the configured database:

```text
ACE_SERVICE_DATA_ROOT=/srv/ace-service/data uv run python -m ace_service.rollback_readiness
```

The command exits zero only when no nonterminal or malformed schema-v2 state
or unconfirmed cover staging is present. A nonzero or indeterminate result
means the v2-capable controller and worker must remain active.

Checkpoint 3 quality comparisons are local operator actions, separate from
browser requests and the product database. Validate the fixed private fixture
without contacting Runpod with:

```text
uv run python -m ace_service.quality_eval \
  --manifest /srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json \
  --dry-run
```

Paid execution remains blocked until the campaign has fresh rate and billing
boundary evidence plus the separately authorized worker, controller, and edge
rollback artifacts.

The private quality campaign keeps two distinct identities for every executed
sample: the opaque campaign sample ID (for example `s-…`) that keys blinded
score sheets, aliases, and reservations, and the product job ID, which is a
generated UUID stored in the product database and durably linked to the
campaign sample by the campaign store. No campaign sample ID is ever used as a
worker `job_id`; the strict worker schema validates every job ID as a UUID.
The strict v1/v2 compatibility smokes are expanded into complete worker
envelopes and validated end-to-end against the real worker parser
(`runpod_worker.schemas.WorkerRequest`) before any execution window opens.
Score-sheet export, import, and finalization all enforce exact current
scoreable-sample-set and pair coverage: a sample or pair declared after export
causes import and finalization to reject the stale sheet even after that
sample completes. The operator CLI persists deterministic screening
advancement (`--advance --confirm`) from the two finalized screening sheets —
the fresh explicit confirmation is mandatory before any campaign mutation,
and a score-equivalence group that crosses the two-finalist cutoff is excluded
in its entirety — and materializes the confirmation cases that
`--execute --stage confirmation` then submits. Every executed sample gets a
preassigned UUID product job that is crash-recoverable through a durable
campaign submission intent, and windows close only on provider-observed
zero-worker/zero-work Runpod `/health` evidence; `--status`, `--reconcile`,
and `--verified-teardown` expose bounded recovery actions. Recovery actions
run from frozen campaign/sample/submission-intent state and never load the
external fixture manifest, so status, backup, reconciliation, and verified
teardown stay usable even when the manifest is missing or corrupted.
Every recovery action validates `--campaign-id` against the campaign
database first: an unknown campaign is blocked before any backup file,
product engine, controller worker, Home Ingest client, or Runpod client is
created, and verified teardown rejects an active maintenance gate that
belongs to a different campaign. Terminal attempts whose attributable
compute is unknown keep their full original reservation counted in budget
totals as `conservatively_retained` (never invented as executed compute)
and may be closed by verified teardown only after provider-observed zero
work is proven; in-flight/uncertain attempts stay `unresolved` with their
full reservation and keep teardown and rollback blocked, and a failed,
cancelled, unsubmitted, or completed terminal identity can never be
rewritten into conflicting later evidence — only an uncertain attempt may
advance to a compatible terminal outcome and only a completed sample with
missing cost inputs may fill them in place, requiring any supplied output
path, GPU, execution, reason, or status to match the recorded evidence and
rejecting conflicts before any cost/reservation mutation. The campaign
database schema (v3) constrains reservations to exactly `open`,
`unresolved`, `conservatively_retained`, and `settled`, and migrates v1/v2
stores to v3 as one atomic unit — a rejected migration leaves the source
schema version, objects, rows, reservation state, timestamps, and storage
child links unchanged — while refusing any unknown reservation state before
status, admission, teardown, recovery, or rollback could omit it; confirmed
`--reconcile` additionally settles the exact
crash state that committed a reservation but never persisted a submission
intent as proven unsubmitted, creating no product job and calling no
provider.

### Runtime Architecture

```text
Trusted browser / phone
        |
        | Tailscale HTTPS
        v
+----------------------------------------------------+
| Hetzner VM                                         |
|                                                    |
| FastAPI controller/UI on 127.0.0.1:8000           |
|   - auth + CSRF                                    |
|   - SQLite                                         |
|   - job state machine                              |
|   - one controller worker                         |
|   - Runpod API client                              |
|   - persistent source/output storage               |
|                                                    |
| Public transfer app on 127.0.0.1:8001             |
|   - ONLY signed /transfer/v1/* routes              |
|   - source GET for Runpod                          |
|   - generated-output PUT from Runpod               |
|                                                    |
| Caddy/Nginx on public HTTPS                        |
|   - forwards ONLY /transfer/v1/* to :8001          |
|   - binds public interface                         |
+-------------------+----------------+---------------+
                    |                ^
      Tailscale API |                | short-lived HTTPS
                    v                | capability URLs
+--------------------------------+   |
| Home server                    |   |
|                                |   |
| private ingest agent           |   |
| - YouTube metadata             |   |
| - yt-dlp audio download        |   |
| - ffprobe validation           |   |
| - ffmpeg normalization         |   |
| - SFTP upload to Hetzner       |   |
+--------------------------------+   |
                                     |
                                     v
                           +-------------------------+
                           | Runpod Serverless Flex  |
                           |                         |
                           | custom ACE-Step worker  |
                           | RTX 4090 24 GB target   |
                           | workersMin = 0          |
                           | workersMax = 1          |
                           | batch_size = 1          |
                           | XL Turbo + 1.7B LM      |
                           +-------------------------+
```

### Why This Split Exists

- Hetzner is lightweight enough for the control plane. It does no ML inference and no audio transcoding.
- YouTube access stays on the home connection. Cloud/datacenter IP reputation cannot break the main controller or force media download from Hetzner.
- Home is the only runtime that invokes `ffmpeg` or `ffprobe`; Runpod's generated-output MP3 path uses in-process LAME and the Hetzner control plane performs no media processing.
- Runpod receives only clean generation parameters and, for covers, a temporary HTTPS URL to a prepared source file.
- The MacBook and home server are absent from original-song generation. A home-server outage disables only new YouTube cover ingestion.

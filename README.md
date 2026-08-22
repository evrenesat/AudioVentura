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
the source job. A cover continuation is available only from a completed
schema-v2 MP3 output. It requires fresh rights confirmation, integrity-checks
and stages that durable output locally, and never contacts YouTube again.

Cost display is a read-only informational calculation at the fixed
`USD 0.50/GPU-hour` rate: the original and cover forms show the latest three
completed attempt durations of the matching kind (service-wide history),
their average, and an approximate per-request estimate (average times
variation count), or a clearly labeled 60-second seed (`USD 0.0083` per
variation) when no matching completed history exists. Each label applies one
half-up rounding at the final four-decimal USD display boundary from the raw
value, and the visible request total follows the selected variation count
(both forms default to one and allow 1–4). Estimates are computed
on read, are never persisted, and never approve, delay, reject, or cancel
generation. Historical `submission_quotes`, rate catalogs, calibrations, and
billing observations remain readable data but are no longer captured or
consulted by the active flow. `RUNPOD_WORKER_RUNTIME_IDENTITY` pins the
deployed worker image as an exact `sha256:<64 hex>` release identity; it is
server configuration and never browser input.

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

Jobs remain the unit of queueing, execution, status, outputs, attempts, and
transfer capabilities. Projects only group same-type jobs for naming,
continuation, and version comparison; they do not add execution state.

New jobs use a strict version-2 worker payload. Original requests choose
`direct`, `enhance`, or `auto-compose` prompting and either model-selected
duration (`auto`, sent as `-1.0`) or an explicit 10-600 second custom value.
The final caption and lyrics are bounded at 511 and 4095 characters. Cover
requests expose ACE-Step's independent `audio_cover_strength` and
`cover_noise_strength` controls and choose either the probed source duration
or an explicit 10-600 second custom target. The measured source duration and
generation target remain separate durable values. The
initial rights checkbox is the only authorization: after the home server
prepares the source, the controller atomically persists the finalized source,
checksum, size, and duration with `cover_staging.status=confirmed` and
continues through the serialized Runpod path in the same pass — no second
confirmation click. Legacy rows that durably committed
`cover_staging.status=awaiting_confirmation` keep the authenticated one-time
confirm/cancel route. One to four cover variations run
sequentially; a supplied seed advances deterministically. Duration prose is
accepted only when bounded numeric seconds/minutes match an explicit custom
duration; it never changes the structured value.

The opt-in `tests/live_paid_ui_e2e.py` smoke submits exactly one new YouTube
cover and one local-output continuation, one variation each. It is excluded
from normal pytest discovery, requires protected credentials plus
`--allow-paid`, and enforces an exact two-submission budget.

Immediately before starting a schema-v1 controller rollback, run the local
read-only gate against the configured database:

```text
ACE_SERVICE_DATA_ROOT=/srv/ace-service/data uv run python -m ace_service.rollback_readiness
```

The command exits zero only when no nonterminal or malformed schema-v2 state
or unconfirmed cover staging is present. A nonzero or indeterminate result
means the v2-capable controller and worker must remain active.

Checkpoint 3 quality comparisons are local operator actions, separate from
browser requests and the product database. The quality campaign is currently
quarantined: its executable entrypoint (`python -m ace_service.quality_eval`)
and the ordinary-submission maintenance gate are disabled with a `TODO`
(re-enable after ordinary original and cover generation is stable), while the
campaign store, evaluators, profiles, and unit-testable implementation remain
in place. Do not attempt to run the campaign CLI in this recovery.

The private quality campaign keeps two distinct identities for every executed
sample: the opaque campaign sample ID (for example `s-…`) that keys blinded
score sheets, aliases, and reservations, and the product job ID, which is a
generated UUID stored in the product database and durably linked to the
campaign sample by the campaign store. No campaign sample ID is ever used as a
worker `job_id`; the strict worker schema validates every job ID as a UUID.
The strict v1/v2 compatibility smokes are expanded into complete worker
envelopes and validated end-to-end against the real worker parser
(`runpod_worker.schemas.WorkerRequest`) before any execution window opens.

The Runpod worker now loads checkpoints only from one revision-pinned Hugging
Face cached-model snapshot. Production must provide `ACE_WORKER_MODEL_REPO`,
the exact 40-hex `ACE_WORKER_MODEL_REVISION`, release tag, manifest SHA-256,
and cache root; the worker validates the complete 25,253,688,079-byte manifest
inventory before importing ACE-Step and has no network-volume checkpoint
fallback. `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are image defaults.
Nonterminal status polling exposes named evidence-backed phases and elapsed
time, without presenting a completion percentage.
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

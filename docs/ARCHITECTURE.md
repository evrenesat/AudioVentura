# Architecture

## Controller deployment prefix boundary

`ACE_SERVICE_ROOT_PATH` is the controller's trusted ASGI public-path boundary.
It defaults to empty for root deployment and may be set to one validated
absolute prefix such as `/beta`. A reverse proxy strips that prefix before
forwarding; FastAPI retains the unprefixed route table and uses the configured
`root_path` only when generating browser-visible paths. HTML navigation, form
actions, redirects, status polling, static assets, media, and downloads are
all resolved through named routes on the current request, so generated paths
contain the prefix exactly once. Request headers never select the prefix.

This boundary applies only to the private controller/UI. The separately
constructed public transfer app and its signed `/transfer/v1/*` capabilities
are unchanged.

## Checkpoint 2 controls and compatibility

New controller jobs persist a strict schema-v2 normalized request in the
existing JSON columns. The request contains an immutable `profile_id`, the
complete resolved ACE-Step parameters, explicit prompt and duration modes, and
the exact effective caption/lyrics. `fast-beta-v1` is the only UI default;
`quality-v1` remains an unselected grid candidate. Schema-v1 normalized rows
are not rewritten and continue through the legacy worker mapping.

Originals use `direct`, `enhance`, or `auto-compose` prompt flags. `auto`
duration sends ACE-Step `-1.0`; `custom` requires 10-600 seconds. Bounded
numeric seconds/minutes in prose are checked against matching custom seconds;
conflicting or vague duration language is rejected without mutating the
structured value. The final caption/lyrics remain bounded at 511/4095
characters without truncation.

Cover preparation is one-submit: home ingest probes and persists the
source duration, the controller enters `staging` and immediately consumes the
already-persisted initial rights confirmation in the same transaction, so the
durable row is `cover_staging.status=confirmed` before any Runpod capability
is issued. Source mode uses the measured source duration as the ACE-Step
target; custom mode preserves a separate explicit 10-600 second target.
Covers retain independent `audio_cover_strength` and `cover_noise_strength` values,
run one to four variations sequentially (both forms default to one), and
persist the returned effective seed and bounded worker result metadata,
including output duration/tolerance and build identity, in existing JSON
fields. Only legacy rows whose durable state is exactly
`cover_staging.status=awaiting_confirmation` may be confirmed or cancelled
once through the authenticated CSRF-protected UI; its prepared source is
removed after the failed state commits. New rows never commit the awaiting
state and never render or require the route. Status polling reloads the
detail page once when a legacy confirmation form becomes available.
Enhance and auto-compose use bounded LM lyrics as effective lyrics only when
the submitted lyrics are empty; supplied lyrics remain exact. The controller
projects the same bounded profile, input/effective values, generated metadata,
resolved parameters, output evidence, and worker identity onto the output
record, while keeping queue and execution timing on the attempt record.

Immediately before starting a schema-v1 controller rollback, operators run
`ACE_SERVICE_DATA_ROOT=/srv/ace-service/data uv run python -m
ace_service.rollback_readiness`. The local read-only check reports bounded job
IDs, statuses, and schema classifications. It exits nonzero for every
nonterminal or malformed schema-v2 row, including unconfirmed cover staging;
legacy/unknown historical rows and valid terminal rows do not block. A
nonzero or indeterminate result requires retaining the v2-capable controller
and worker.

## Checkpoint 3 quality campaign boundary

Quality evaluation is an operator process, not a browser feature. The
`ace_service.quality_eval` module validates the private fixed fixture,
constructs the frozen ordered decision tree, and uses the existing durable
controller queue/transfer adapter for any explicitly authorized execution.
Its campaign SQLite database is deliberately separate from the product
database because the ordered production migration runner is a Checkpoint 4
concern. The old release does not need to understand the campaign tables.

The campaign is quarantined during the usability recovery: the executable
entrypoint (`python -m ace_service.quality_eval`) and the
ordinary-submission maintenance gate are commented out with a `TODO`
(re-enable after ordinary original and cover generation is stable). The
campaign store, reservations, runtime observations, append-only provider
billing evidence, blinded sample mappings, score sheets, execution windows,
durable submission intents, and the maintenance-gate primitives remain
implemented, importable, and unit-tested, and their data stays readable.
Ordinary `/create`, `/cover`, and cover-confirmation mutations no longer
consult the gate; authenticated reads are unaffected. The rollback readiness
command checks both the existing v2 job lifecycle and the campaign store:
active windows, in-flight/uncertain samples, unresolved reservations, corrupt
state, or a missing verified edge guard block a rollback to a controller that
lacks the durable gate.

Worker payload contracts are validated end-to-end from the campaign store to
the strict worker schema. The compatibility smokes and every ordinary
screening/confirmation sample are normalized into complete schema-v1 or
schema-v2 envelopes, stored as `normalized_request_json`, and only then pass
through `ControllerWorker._default_payload` (job ID, task type, variation
index) and the submission boundary (transfer capabilities, submission nonce,
cover source) to `runpod_worker.schemas.WorkerRequest.from_mapping`. A
malformed fixture therefore fails at the real parser before any completion
can hide it. Product job IDs are generated UUIDs, never the opaque campaign
sample IDs; the product UUID is preassigned before either database commit and
a bounded campaign submission intent (sample ID, reservation ID, exact
product UUID, and a non-sensitive fingerprint of the frozen request) is
persisted before the product row exists. Recovery creates or validates the
product row against that frozen intent, so either crash order recovers one
UUID job, one campaign link, and one reservation, and remote submission never
starts before both durable records agree. `mark_sample_submitted` durably
links each opaque sample to its UUID job ID and settles the matching
reservation.

Teardown is evidence-backed and fail-closed: the executable path parses the
real Runpod `/health` contract (bounded non-negative worker counts and
`jobs.inQueue`/`jobs.inProgress` work counts) and closes an execution window
only with immutable, timestamped zero-at-rest evidence for the authorized
endpoint, stored on the window record. While a window, gate, or pending
submission intent is open, the operator uses the read-only `--status` action
and the confirmed `--reconcile`/`--verified-teardown` actions; ordinary
score, advancement, decision, and execute actions are rejected until recovery
completes. These recovery actions (plus `--backup`) dispatch from frozen
campaign/sample/submission-intent state in the campaign database and never
load, hash, or rebuild the external fixture manifest, so an unavailable
manifest cannot block the recovery path. Every recovery action validates the
named campaign before acting, and `--verified-teardown` rejects an active
gate belonging to a different campaign.

Reservations distinguish four lifecycle states. `open` means reserved but not
yet resolved; `unresolved` means an in-flight/uncertain reservation whose
outcome is still unknown (uncertain/in-flight work is never financially
resolved: it keeps its full immutable reservation counted in admission
totals and continues to block teardown and rollback); `settled` carries a
final estimate (an immutable executed-attempt estimate, zero for
proven-never-submitted work, or zero for a proven-not-started cancellation);
and `conservatively_retained` marks a durably terminal attempt (failed,
cancelled with unknown start, or completed without cost evidence) whose
attributable compute is unknown. A conservatively
retained reservation keeps its full immutable original
`reserved_micro_usd` counted in every later admission/budget total — recovery
can never lower committed spend — without presenting the amount as estimated
or billed compute. Verified teardown treats it as financially resolved only
after the sample is durably terminal and provider-observed zero evidence
passes; genuinely `open` or `unresolved` reservations still block teardown,
rollback readiness, and ordinary mutations.

Terminal identity and evidence are immutable and fail closed. Once a
`failed`, `cancelled`, `unsubmitted`, or completed terminal record exists, a
later conflicting status, output, GPU, execution, reason, or estimate is
rejected instead of rewriting it. The only allowed advances are a narrowly
compatible uncertain-to-terminal outcome (uncertain work may later
reconcile to completed, failed, or cancelled, preserving prior compatible
identity) and a completed sample with missing cost inputs filling those
inputs in place while remaining `completed`, requiring any supplied output
path, GPU, execution, reason, or status to match the recorded evidence and
rejecting conflicts before any cost/reservation mutation; exact repeats
stay idempotent. The campaign schema (v3) enforces the four reservation
states with a SQLite `CHECK`, and migrates v1/v2 stores to v3 as one atomic
unit — any rejected migration leaves the source schema version, objects,
rows, reservation state, timestamps, and storage child links unchanged. Any
existing database containing an unknown or corrupt reservation state is
refused before it can serve status, admission, teardown, recovery, or
rollback, and committed-spend, teardown, campaign-status/recovery, and
rollback-readiness paths fail closed even if a foreign state is injected
after open.

Campaign billing stores the raw provider amount and immutable fetch evidence.
Runpod endpoint responses are treated as USD only through the versioned source
contract because they omit a currency field. Network-volume responses without a
volume identifier remain account-wide evidence and are excluded from the
service endpoint total. Native UTC buckets are never shifted or prorated into
local-day claims.

## Checkpoint 4 cost ledger and billing reconciliation

The product schema is versioned by the ordered migration runner in
`ace_service/migrations.py` (current version 6). `migrate-status` is
read-only (path hash + state only); `migrate-upgrade` is the only schema
mutation, runs under an exclusive sidecar lock, commits a durable attempt
marker before the additive DDL, and never auto-retries a crash/incomplete
marker. Normal startup refuses every state except the exact expected version;
`initialize_database()` stays a foundation creator.

Schema v6 adds `projects` and indexed `jobs.project_id` membership. Each
project has one immutable job type and a bounded editable title. The explicit
v5-to-v6 migration creates one project per historical job using the job ID as
the project ID and derives a bounded title from the sanitized source title,
prompt, or type label. It does not rewrite historical job, output, attempt,
quote, billing, or transfer evidence.

Projects are a presentation and grouping boundary, not a generation state
machine. Jobs remain the sole queueing, execution, output, failure, attempt,
and transfer unit. Authenticated `/projects` lists projects by latest
activity; `/projects/{id}` renders jobs newest-first with their existing
authenticated media/download routes. Rename is the only project mutation and
uses the existing Basic-auth and CSRF boundary. `GET /jobs/{id}/continue`
reconstructs editable defaults only from a complete schema-v2 request and the
stored source/job fields. The existing create POST validates the edited values
again and derives same-project membership from the continuation source; it
always creates a new job. Cover continuation requires a completed schema-v2
MP3 output with measured duration evidence. The controller verifies its
recorded path, size, checksum, type, and physical containment; copies it
atomically into the new job's incoming source; commits confirmed staging; and
enters the ordinary serialized cloud queue without home ingest or YouTube.

The ledger adds five record families, all through the existing SQLAlchemy
session/repository boundary: immutable one-to-one `submission_quotes`
(secret-free fingerprint, exact micro-USD, allow-listed unavailable reasons
paired with null amounts), immutable `variation_attempts` execution-cost
evidence (`pending`/`unavailable`/`complete`, half-up formula enforced
server-side, conflicts rejected before mutation), append-only
`billing_observations` with separate sha256 value and fetch-event identities
plus current `billing_projections` upsert, the versioned `gpu_rate_catalog`, and immutable
`runtime_calibrations` matched on task/profile/model/runtime/GPU/duration/output
dimensions without extrapolation. The runtime dimension comes only from the
controller's pinned `sha256` worker-image digest, not the browser or worker
schema compatibility version. Catalog, quote, and attempt snapshots retain the
validated exact hourly-USD text alongside derived micro-USD. Eligible-rate
selection compares those exact decimal tokens before the chosen token is used
once for half-up quote calculation; integer micro-USD derivatives are never
selection keys, and money never passes through binary floats.

Billing observations remain append-only readable history with their value
and fetch-event identities; the `billing-sync` operator command and its
Runpod billing client were removed from the active release, so no new
provider billing fetch or projection update is possible. Historical
`submission_quotes`, rate-catalog rows, and calibrations stay readable but
are no longer captured: quote capture was removed from the normal submission
control flow (original creation and cover confirmation), and no rate,
calibration, quote, or billing record is consulted or created by the active
flow.

Terminal polling still records attempt evidence from Runpod `executionTime`
provenance, resolves the worker-reported GPU through the server-owned alias
map, and snapshots the trusted rate; failed attempts count only positive
execution time, zero requires durable never-started proof, and unknown/stale
rates record explicit unavailable reasons. These attempt-cost columns are the
input to the active cost display.

Cost display is a read-only informational calculation computed on read at
the fixed `USD 0.50/GPU-hour` rate (exact integer/rational arithmetic, no
binary float). The original and cover forms query the latest three completed
attempt durations of the matching kind (service-wide history, joined
`VariationAttempt` to `Job`, filtered to `status=completed`, non-null
`completed_at`, and non-null non-negative `execution_ms`, ordered by
`completed_at DESC, VariationAttempt.id DESC`, limited to three). Raw
numerators are carried through each sample, the average (sum of the raw
numerators divided by the sample count), and the per-request total (the
unrounded average times the variation count, or the unrounded 60-second seed
with no history); every label applies `ROUND_HALF_UP` exactly once at the
final four-decimal USD display boundary from the raw rational value. The
server also computes the request label for every supported variation count
(original 1-4, cover 2-4) and the form binds the visible total to the
selected value with no client-side money arithmetic. With no matching
history a clearly labeled 60-second seed (`USD 0.0083` per variation) is
shown. The estimate is never persisted
and has no admission or generation-control role; any query/render failure
omits it while generation continues unchanged.

## Checkpoint 9 operations

The controller and home agent configure UTC rotating file logs below their
private data roots. A handler-level redaction filter removes credentials,
authorization values, capability URLs, and prompt/lyrics fields before a
record reaches disk. Operational records use bounded job/stage/component,
stable error-code, exception-class, byte-count, and elapsed-time fields.

Controller cleanup runs once before the worker accepts jobs and periodically
thereafter. It removes stale `.part` files, expires and prunes old capability
records, revokes capabilities for terminal jobs, and removes non-retained
terminal cover sources. Home cleanup removes orphan temporary job directories.
Neither cleanup path deletes completed outputs.

## Checkpoint 8 private web UI

The main controller app is created by `ace_service.app.create_app` and binds
only to the configured loopback host. It disables API documentation, requires
constant-time HTTP Basic authentication on controller routes, and bootstraps
an HttpOnly same-site CSRF cookie for unsafe browser form posts. A middleware
adds a restrictive CSP, no-sniff, no-referrer, frame-deny, and no-store headers
to authenticated HTML, JSON, and media responses.

`/create` and `/cover` validate the existing original/cover request models,
persist jobs before enqueueing them on the single controller worker, and do not
block original requests on home-ingest readiness. `/jobs` and the dashboard
show durable progress and recent history. `/jobs/{id}/status` is a bounded
JSON polling surface used by the mobile-friendly detail page.

The project workspace adds no Runpod call, worker payload, cover-ingest
shortcut, billing rule, or transfer capability behavior. It composes the
existing durable job and authenticated output views only; the separately
constructed public transfer application remains unchanged.

Playback and downloads resolve an output by database ID, enforce controlled
audio MIME types, reject traversal and every symlink component, and verify the
recorded size and SHA-256 before returning a `FileResponse`. The public
transfer app is still a distinct FastAPI instance and has no UI, health, or
media routes.

`/healthz` checks the process, SQLite, and writable data layout. `/readyz`
reports controller/database, Runpod, home-ingest, and public-transfer
components separately; a home outage degrades readiness without preventing
original-song submission. The home service exposes an authenticated `/healthz`
probe for this status display. Each external readiness probe has a separate
five-second application deadline, so an accepted but unresponsive service is
reported as unavailable without inheriting the long timeout used by cover
preparation or generation.

## Checkpoint 7 cover workflow

`CoverRequest` validates one approved single-video YouTube URL, required rights
confirmation, bounded style/guidance text, optional replacement lyrics,
source/custom duration, and the independent ACE cover controls before
`create_cover_job` persists the metadata-only request.
The serialized controller worker changes a queued cover to `ingesting`, calls
the bearer-authenticated home-ingest endpoint over the configured private
network, and waits for the metadata response after SFTP has uploaded
`incoming/<job-id>/source.mp3.part`.

Hetzner verifies the home-reported positive byte size and SHA-256 against the
`.part` file, atomically renames it to `source.mp3`, and persists the returned
title, canonical URL, duration, checksum, and size. The controller then runs
one database transaction that finalizes the normalized source duration,
transitions `ingesting -> staging`, and reuses `confirm_cover_job` to consume
the already-persisted initial rights confirmation, so the durable row carries
`cover_staging.status=confirmed` plus `confirmed_at` before any external
submission. A missing `rights_confirmation_at` fails the cover closed before
the transaction. Only after that commit does the worker issue one
source-download capability and one output-upload capability and submit the
cover payload to Runpod. The payload contains the composed style caption,
optional exact lyrics, independent cover controls, source checksum/size,
measured source duration, resolved target duration, and signed URLs; it
contains no audio bytes or YouTube
credentials. Legacy rows that durably committed
`cover_staging.status=awaiting_confirmation` keep the authenticated one-time
confirm/cancel staging page, which never enqueues without confirmation; the
prepared source is removed after cancellation commits.

For a continuation, the selected completed MP3 output replaces the home-ingest
step. Its measured output duration becomes the new source duration while the
new request may independently select a custom target. Failed jobs, incomplete
jobs, non-MP3 outputs, missing files, and mismatched size/checksum evidence are
not reusable and cannot cross the Runpod boundary.

After a valid output is accepted, or after a terminal cover failure, issued
capabilities are revoked. Non-retained source files are removed from Hetzner
after the terminal state is durable. Startup recovery can advance only a
final source whose persisted size and checksum still match the file; it polls
a persisted Runpod ID without resubmitting it.

## Checkpoint 6 home-server ingest

`home_ingest/` is a separately deployed Python service for the home server.
It binds to localhost, authenticates the private
`POST /v1/prepare-youtube-cover` route with the shared home-ingest bearer
token, and exposes no documentation or catch-all routes. It is the only
runtime allowed to contact YouTube or invoke `yt-dlp`, `ffprobe`, and
`ffmpeg`.

The agent validates one HTTPS YouTube video URL, performs a metadata-only
yt-dlp inspection before an audio-only download, and writes only
`download.<ext>` below a UUID job directory. A progress hook and final stat
enforce the source byte limit. ffprobe requires finite duration and an audio
stream before ffmpeg normalizes the source to a 48 kHz stereo, 192 kbps CBR
MP3. The resulting `source.mp3.part` is atomically renamed locally, probed
again, compared with the metadata duration using a bounded tolerance, and
hashed before upload.

SFTP uses a dedicated key and a deterministic destination under the configured
incoming root:

    incoming/<job-id>/source.mp3.part

The job ID is parsed as a UUID and no title or prompt reaches either local or
remote filenames. The response is emitted only after SFTP reports a matching
remote byte size and contains metadata, byte size, and SHA-256 for the
controller's subsequent Hetzner-side finalization. Raw and canonical home
files are removed on success and failure by default; explicit debug retention
is bounded by a configured expiry and pruned on later requests.

Runpod does not process source media and contains no `ffmpeg` or `ffprobe`
runtime dependency. For a requested MP3, ACE-Step writes a temporary 48 kHz
PCM WAV and the worker encodes that file to the requested MP3 in-process with
the pinned LAME encoder. Requested WAV and FLAC outputs retain their native
ACE-Step save formats. Hetzner performs no media processing.

## Checkpoint 5 original-song workflow

Original-song requests are validated at the controller boundary and persisted
as a normalized, metadata-only Runpod request before the job is enqueued. The
description is trimmed, creative lyrics are preserved, prompt mode and
duration mode are explicit, and instrumental requests cannot carry non-empty
lyrics. `auto` sends model-selected duration `-1.0`; `custom` requires 10-600
seconds. A supplied seed advances by one per serialized variation; each
variation remains an independent Runpod job.

Before each original variation crosses the cloud submission boundary, the
controller issues a new short-lived output-upload capability bound to
`<job-id>/variation-XX.<format>`. The worker payload contains only bounded
generation metadata, the current variation seed, the nonce, and that capability
URL; it contains no audio bytes and no `variation_count` field. Output records
retain deterministic paths, checksums, and the bounded generation projection,
while the variation attempt stores the complete bounded worker result and any
Runpod queue-delay/execution timing returned by the API. The existing single
controller queue submits variations serially, so a later failure leaves
earlier durable outputs available.

## Checkpoint 4 controller orchestration

The controller owns one POSIX advisory lock per configured data root and one
`asyncio.Queue[str]` serviced by one coroutine. Enqueue deduplication is
in-process; the lock prevents a second controller process from polling or
submitting the same SQLite-backed jobs. Runpod submission is serialized across
all jobs and variations.

Parent jobs use the lifecycle transitions defined by the handoff plan. An
original job's `variation_attempts` rows hold the individual variation status,
submission nonce, Runpod ID, result metadata, and terminal error. This keeps a
multi-variation parent active while each variation is submitted and polled in
order. A nonce is committed before `/run`; a returned Runpod ID is committed
immediately after acceptance. Nonce-only attempts are failed as uncertain on
restart and are never submitted again.

Startup recovery runs while the singleton lock is held and before the worker
accepts new enqueues. Queued jobs are enqueued, interrupted ingestion is
advanced only when its canonical source is fully verified, and persisted
Runpod IDs are polled without resubmission. Missing IDs and uncertain
submissions become stable terminal errors. Completed Runpod status is accepted
only after a deterministic output file and matching durable output record pass
size, path, and SHA-256 validation. Legacy rows may recover from a valid local
output after Runpod status retention expires; schema-v2 rows additionally
require already-persisted validated completion metadata, an effective integer
seed, and worker identity, and are never completed from the output file alone.

## Checkpoint 3 boundary

The controller's Runpod adapter is an asynchronous metadata-only client for
the queue endpoint at /v2/<endpoint-id>. It uses separate connect, read,
write, and pool timeouts, sends the API key only in an authorization header,
validates response IDs and states, and maps Runpod's queue/running/terminal
states to cloud_queued, generating, completed, and failed. The adapter never
carries audio in a Runpod JSON body.

Submission persistence is deliberately ordered: a fresh nonce and the
pre-submit cloud_queued record are committed before /run; the returned Runpod
job ID is committed immediately after acceptance. A nonce-only record on
restart is marked uncertain_cloud_submission and is never submitted again.
Records with a persisted Runpod ID resume polling instead.

The transfer application is a separate FastAPI app with documentation and
OpenAPI disabled. It binds to 127.0.0.1:8001 and exposes only:

    GET /transfer/v1/source/{token}
    PUT /transfer/v1/output/{token}

Capability tokens are random 256-bit values; only their SHA-256 hashes are
stored. Capabilities bind a job, direction, relative storage path, extension,
byte limit, and UTC expiry. Source GETs are repeatable during the TTL. Output
PUTs stream to a deterministic .part file, hash while receiving, fsync before
atomic rename, and transactionally create the durable output record while
consuming the capability. Identical completed retries are idempotent and
conflicting retries cannot replace the accepted output.

The public proxy may forward only /transfer/v1/source/* and
/transfer/v1/output/* to 127.0.0.1:8001. It must reject every other path at
the proxy before the private controller/UI is reached. The proxy uses
publicly trusted HTTPS; this checkpoint does not claim a live Runpod transfer
test.

## Checkpoint 1 boundary

The initial controller foundation is a synchronous Python package under
`src/ace_service`. It owns typed configuration, a private data-root layout,
and durable SQLite records for jobs, generated outputs, and short-lived
transfer capabilities.

`config.py` resolves every persistent path below `ACE_SERVICE_DATA_ROOT`,
rejects wildcard application binds and unconfigured credential placeholders,
and requires the public transfer URL to use HTTPS. Credential placeholders
cannot be enabled through deployment settings.
`db.py` creates a SQLAlchemy 2 synchronous engine with SQLite WAL, foreign-key
enforcement, and a busy timeout. `models.py` stores UTC-aware timestamps and
the state needed for future Runpod submission recovery. `repository.py` keeps
token plaintext out of the database; callers receive a capability token only
when it is issued.

This checkpoint has no network clients, FastAPI routes, media-processing
commands, Runpod code, home-ingest code, or inference dependencies. Later
checkpoints may consume these records through the repository while preserving
the boundary that the controller does not run `yt-dlp`, `ffmpeg`, or
`ffprobe`.

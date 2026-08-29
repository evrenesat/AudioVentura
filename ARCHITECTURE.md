# Architecture

This is the authoritative architecture document for AudioVentura. Provider
deployment details belong in their runbooks, and chronological decisions
belong in `DEVLOG.md`.

## System boundaries

```text
Private browser
      |
      | tailnet HTTPS
      v
+---------------------- Hetzner ----------------------+
| Controller/UI :8000                                 |
| - HTTP Basic and CSRF                               |
| - SQLite and durable files                          |
| - one serialized orchestration loop                 |
| - Home Ingest and provider clients                  |
|                                                     |
| Transfer app :8001                                  |
| - signed v1 source GET / output PUT                 |
| - signed v2 raw asset upload / download             |
+---------------------+-------------------------------+
                      | short-lived HTTPS capabilities
          +-----------+-----------+
          |                       |
          v                       v
+-------------------+   +-----------------------------+
| Home Ingest       |   | Inference providers         |
| - YouTube         |   | - Runpod endpoint           |
| - yt-dlp          |   | - Salad Job Queue           |
| - ffmpeg/ffprobe  |   | - fal.ai Model API queues   |
| - restricted SFTP |   |                             |
+-------------------+   | shared ACE-Step runtime     |
                        +-----------------------------+
```

The beta and production Home Ingest and sequential mock instances run on the
private p100 home host, outside the Hetzner controller process:

```text
Hetzner controller :8010/:8000
        | private HTTPS + restricted SFTP
        v
p100 home host
  Home Ingest :8101 (beta) / :8100 (production)
  MIDI mock   :8201 (beta) / :8200 (production)
```

The boundaries are deliberate:

- The controller is the durable control plane. It does not execute media
  tools or load inference models.
- Home Ingest is the only process allowed to contact YouTube or execute
  `yt-dlp`, `ffmpeg`, and `ffprobe`.
- The sequential MIDI mock is a separate private backend. It receives bounded
  job metadata and source capability metadata, but never downloads or uses a
  source recording.
- The transfer app is the only public data-bearing application surface. It has
  no UI or general API routes. The controller exposes one secret-free static
  bootstrap, `notification-worker.js`, publicly because browser-managed service
  worker installation cannot depend on page-level Basic Auth; the fixed
  `/offline-shell` and `/manifest.webmanifest` are public for the same reason.
  Controller UI, configuration, subscription, and user-data routes remain
  authenticated.
- GPU workers receive generation metadata and short-lived transfer URLs. They
  do not receive controller, YouTube, SSH, SFTP, or home-network credentials.
- Audio bytes never travel in a provider API request or result body.

## Components

### Controller

`src/ace_service/app.py` creates the authenticated FastAPI application.
`src/ace_service/web.py` owns browser routes and response views.
`src/ace_service/worker.py` owns the single durable orchestration loop.

The controller:

- validates original and cover forms;
- creates projects, source assets, jobs, variation attempts, outputs, and
  capabilities;
- stores state in SQLite through `repository.py`;
- publishes verified canonical source MP3s before any remix submission;
- validates and stages backend-bounded source clips without running media tools;
- prepares provider-neutral inference requests;
- polls the provider that owns each persisted attempt;
- verifies worker metadata against the uploaded output;
- publishes verified MP3 variations into the media library;
- exposes completed audio only through authenticated media routes;
- serves the library, playlist, player, and cancellation views without
  exposing provider payloads or audio bytes in JSON;
- serves bounded queue-v2 metadata and the fixed offline shell/manifest;
- serves the unified push/offline worker without embedding user state.

One process-level lock protects a data root from two controller workers. One
in-process queue serializes jobs and variations. This is a personal service,
not a horizontally scaled controller.

### Transfer service

`src/ace_service/transfers.py` creates a separate FastAPI app. The legacy v1
contract remains available for persisted job recovery:

```text
GET /transfer/v1/source/{token}
PUT /transfer/v1/output/{token}
```

New browser, Home Ingest, clip, and derivative operations use the separate v2
contract:

```text
PUT /asset-transfer/v2/upload/{token}
GET /asset-transfer/v2/download/{token}
```

Both versions stream bytes without media processing. v2 capabilities bind an
owner, purpose, direction, namespace, exact UUID-derived path, MIME/extension,
size/hash expectations, and expiry. Uploads are raw bounded PUTs; downloads
are revalidated immediately before streaming and allow only bounded retries.

Capabilities are bound to one job, direction, path, extension, byte limit,
and expiry. Only a SHA-256 hash of the random token is stored. Source downloads
may be repeated during their lifetime. Output uploads are streamed to a
private `.part` file, hashed, fsynced, and atomically renamed before the
database records completion.

### Home Ingest

`home_ingest/` is an independently installed private service. It is the only
component that contacts YouTube or runs `yt-dlp`, `ffprobe`, and `ffmpeg`.
Source, clip, and playback-derivative v2 calls use bearer-authenticated
requests and signed transfer URLs. The service downloads one input, probes the
actual audio streams, normalizes the first audio stream to stereo 48 kHz
192 kbps MP3 with metadata removed, and uploads the result through v2.

The v1 YouTube/SFTP path remains only for persisted legacy-job recovery:

```text
incoming/<job-id>/source.mp3.part
```

Uploaded containers are accepted based on real `ffprobe`/`ffmpeg` capability,
not filename or browser MIME. There is no source-duration ceiling at ingest;
byte and timeout limits remain. A continuation from an existing generated MP3
reuses the verified local file and does not contact YouTube again.

### Sequential MIDI mock

`midi_mock_backend/` is a standalone, opt-in provider backend. Its only
external input is the controller's schema-v2 job metadata and output-upload
capability. It claims one cursor index in the reviewed immutable MIDI corpus
inside the same SQLite transaction as the durable job row, so a submission
nonce is idempotent and a failed render still consumes its index.

The worker extracts one MIDI member into a private directory, invokes the
pinned FluidSynth binary with an explicit argument vector, and encodes the
stereo 44.1 kHz PCM stream with in-process LAME at 192 kbps. It enforces a
600-second render ceiling, one render at a time, bounded output bytes, process
groups, cancellation, and cleanup. It returns only bounded job/result metadata;
the corpus archive, source URLs, prompts, lyrics, and output bytes do not cross
the provider API.

### GPU worker

`runpod_worker/` contains the shared ACE-Step execution boundary and the
Runpod entry point. `deploy/salad/worker_api.py` wraps the same handler with a
small local HTTP service for Salad's Job Queue Worker.

The runtime loads one pinned ACE-Step source revision and one pinned model
bundle before accepting work. It accepts worker schema 1 for old Runpod jobs
and schema 2 for current jobs. New provider-neutral submissions use schema 2.
Runpod supplies the exact aggregate Hugging Face revision through its
datacenter-independent cached-model facility; no customer network volume is
attached to the endpoint.

For each request the worker:

1. validates the complete bounded request;
2. downloads and verifies cover audio when present;
3. runs one ACE-Step generation with `batch_size=1`;
4. validates duration and output metadata;
5. uploads the audio through the signed output capability;
6. returns bounded metadata only;
7. removes private temporary files on success or failure.

MP3 output uses in-process LAME. The GPU image does not need `ffmpeg` or
`ffprobe` for generated output.

## Job model

A project groups jobs of one type for naming, continuation, and comparison. It
does not own execution state. A job is the unit of user intent. A variation
attempt is the unit of provider submission. An output is the verified file
created by a completed variation.

The main job states are:

```text
original: queued -> cloud_queued -> generating -> completed | failed | cancelled

cover/remix: queued -> ingesting -> staging
                    -> cloud_queued -> generating -> completed | failed | cancelled
```

Source assets have their own durable flow (`awaiting_upload`, `uploaded`,
`queued`, `preparing`, `ready`, `failed`, or `cancelled`). A source reaches
`ready` only after Home Ingest's canonical MP3 has been received and verified;
the project playlist gets that source before a remix job can be submitted.
Each remix stores the active source media ID, full-source offsets, selected
clip duration, and an immutable backend capability snapshot. Full-range clips
use a verified local copy; subranges are prepared by Home Ingest. Provider
submission is blocked until staging commits.

One to four variations run sequentially. A supplied seed advances
deterministically for later variations. Status shown in the UI is evidence,
not a control signal.

Cancellation is a persisted request, not a browser-side assumption. The
controller records the request before the worker examines it, then records one
of the bounded outcomes `cancelled`, `too_late`, `unsupported`, or `failed`.
The worker checks before submission and at external-await boundaries; provider
cancellation is attempted only by the worker that owns the persisted provider
reference. A too-late result leaves the job running, while a confirmed
cancellation is terminal and never enters the publication seam.

## Media library and playlists

`MediaItem` is the user-facing identity of a source or generated track and
`MediaFile` is its verified representation. A source item points to one
`SourceAsset`; a generated item points to one `Output`. Source items always
have one canonical MP3 in `library/sources/<uuid>/source.mp3` and enter their
project playlist on publication.

Native MP3 completions publish immediately. FLAC/WAV completions first create a
logical generated item with a primary lossless download in `outputs/` and one
pending `mp3_playback` derivative task. The item enters library queries and
playlists only after Home Ingest verifies the canonical MP3 at
`library/generated/<uuid>/playback.mp3`. Repeated completion or derivative
callbacks are idempotent. Existing v10 rows are not backfilled. Custom
playlists preserve explicit positions, allow duplicate entries (each position
has its own entry identity), and use a two-phase reorder when moving an item
across positions.

Library playback and download resolve a database media-file ID, verify the
active file is below the configured library root, reject symlink components,
and recheck extension, MIME type, size, and SHA-256 before streaming. Deletion
first marks the item pending, atomically moves its file below the private trash
root, and then marks it deleted. Startup cleanup reconciles pending/deleted
rows and purges only after the repository state permits it; the UI renders a
deleted-output tombstone rather than a stale playback link.

Project deletion records its redacted audit before file work begins. It runs
the same media-item deletion state machine, then path-verifies every project
`Output` that has no `MediaItem` (including legacy MP3 and FLAC/WAV outputs).
Those files move to deterministic project-scoped trash while their output rows
remain durable; the project row is deleted only after all media items and
outputs are accounted for. A retry recognizes an already-completed move, and
startup cleanup uses the audit to finish a deletion or purge trash left after
the project commit. No output is promoted into the library for deletion.

The browser shell keeps one `<audio>` element in the persistent layout. Its
queue is filled from safe same-origin media metadata, not provider responses,
and its state is held in browser storage across soft navigation and reload.
Source upload starts at the authenticated source picker, not the library. The
queue contains no audio bytes or capability URLs.

### Offline browser subsystem

Offline playback is browser-local state, not a controller database feature. The
existing public `/notification-worker.js` registration is the single unified
service worker for push and offline behavior. It precaches only the fixed,
secret-free `/offline-shell`, static assets, icons, and root-aware manifest.
The authenticated `/offline` page uses the same markup and reconstructs saved
playlists after load. Normal authenticated HTML is never placed in the shell
cache.

Every worker derives a normalized namespace from its registration scope. The
production scope is `/` and explicitly excludes `/beta/`; the beta scope is
`/beta/`. IndexedDB uses `audioventura-offline:<scope-key>:v1`, while Cache
Storage uses `audioventura:<scope-key>:media:v1` and a separate versioned shell
cache. A worker update replaces only old shell caches in its own scope and
preserves compatible content-addressed media.

The browser coordinator keeps playlist snapshots, ordered duplicate entries,
SHA-256 blob metadata, URL mappings, leases, and `[playlist, hash]` ownership
references in IndexedDB. Complete verified `audio/mpeg` response bodies live
once in Cache Storage at a synthetic SHA-256 key. A blob is removed only after
its final owner reference is cleared and reconciliation confirms it is not
shared by another playlist or `Played tracks`.

Queue schema v2 supplies only bounded MP3 metadata, exact size, verified hash,
revision, and root-aware media/download paths. Online playback begins normally;
the player separately caches a complete body in the background. Explicit
playlist caching downloads each unique hash at bounded concurrency with quota,
persistence, progress, cancellation, retry, and refresh states. Offline
snapshots are read-only and use the same player controls and single global
`<audio>` element.

The worker serves mapped cached media as a full `200` or a synthesized single
range `206`; invalid or multiple ranges return `416`. It validates origin,
scope, mapping, MIME, size, and SHA identity before serving. Missing or
corrupt bodies become retryable local state and fall through to the network
only when connectivity is available. Cached titles and audio are readable to
anyone with access to the trusted browser profile.

## Provider boundary

`src/ace_service/providers/base.py` defines the shared contract:

- `submit(request) -> ProviderJobRef`
- `status(ref) -> ProviderStatus`
- `result(ref) -> InferenceResult`
- `cancel(ref) -> CancelOutcome`
- `health() -> ProviderHealth`
- `materialize_artifact(ref, artifact) -> ProviderArtifact`

Capabilities declare supported modes, request features, worker schemas, and
cancellation behavior. The current providers are:

- `RunpodProvider`, which adapts the existing Runpod queue API;
- `SaladProvider`, which adapts Salad Job Queues and enriches pending or
  retrying-running status with bounded container-group lifecycle evidence. A
  ready instance restores job-scoped `RUNNING`; terminal states are never
  enriched.
- `FalProvider`, which adapts one reviewed catalog descriptor and uses
  controller-pulled CDN artifacts rather than the worker upload contract.
- `MockProvider`, which adapts the private `mock/midi-sequential` backend.

Deployment management is not part of this interface. Queue creation, GPU
selection, autoscaling, image credentials, and container-group changes remain
provider-specific operator work.

The interface models prompt-to-audio and audio-to-audio plus individual request
features. Each selectable audio backend advertises reviewed finite source and
output duration bounds. `BackendId` is persisted beside the coarse provider
name; every provider operation checks both values. A static reviewed Fal
catalog maps finite product fields to endpoint-specific JSON and is never
replaced by live discovery data at runtime. New jobs persist the selected
backend and descriptor snapshot before enqueue; variations remain sequential
and never fall back to another backend.

The mock backend advertises every built-in request feature and both modes while
remaining MP3-only. Its deterministic corpus timing is intentionally exempt
from the normal requested-duration completion check; real providers retain the
existing duration policy.

### Managed capacity and notifications

`src/ace_service/capacity/` is a sibling boundary to inference providers. Its
registry contains only explicitly fingerprint-pinned Salad and RunPod
resources; Fal and other managed APIs have no capacity manager. The controller
reconciles one provider-local worker floor at a time, never raises a provider
maximum above one, and refuses immutable resource or deployment drift. The
complete secret-free identity payloads and pinned v1 digests live in
`capacity/fingerprint_fixtures.json`; the preflight command compares those
digests with live provider metadata without mutating floors.
Before provider submission, managed capacity must be ready. A warming worker
causes a bounded retry before nonce creation, so provider TTL starts only after
capacity is available.

`controller_settings` stores the global keep-warm seconds after migration 9.
`capacity_leases` stores last activity, the durable deadline, action fencing,
release evidence, and bounded error state. Provider mutations happen outside
SQL transactions and are followed by read-after-write inspection. The
controller and systemd watchdog both use the fenced action lease, so a stale
actor cannot commit a later state over a newer action.

Verified parent completion and managed lifecycle transitions insert one
deduplicated event into `notification_events`. The generation-start event is
inserted at the worker seam only after the exact persisted backend resolves to
a configured capacity manager. Active Web Push subscriptions
are fanned out to `notification_deliveries`; the bounded dispatcher retries
independently of job submission and capacity release. Payloads contain only
safe copy, an event key, and a same-origin path. The authenticated worker route
is emitted under the configured root path and never receives provider or job
secrets.

## Durable provider ownership

The controller stores the provider name and external job ID on each job and
variation attempt. Before calling `submit`, it commits a unique submission
nonce and the pre-submit state. After a successful response, it immediately
stores the returned provider reference.

This creates three recovery cases:

1. No nonce: the variation has not crossed the submission boundary.
2. Nonce and provider reference: resume polling the same provider job.
3. Nonce without provider reference: submission is uncertain; never submit a
   replacement automatically.

Changing `INFERENCE_PROVIDER` affects only new jobs. Old Runpod and Salad jobs
must remain reconcilable through their persisted provider. Provider adapters
do not retry submission.

Temporary provider errors keep the attempt nonterminal until its durable
deadline. Not-found behavior is provider-specific. At the deadline the
controller requests cancellation when supported, but running work may be too
late to cancel.

## Status model

Providers map their native states into:

```text
queued, provisioning, starting, running,
succeeded, failed, cancelled, unknown
```

The persisted progress envelope may include a short provider-neutral message,
a normalized `0..1` value, and a detail scope of `job` or `deployment`.
Deployment evidence is labelled as inferred in the UI because a container
group instance is not authoritative proof of assignment to one queue job.
Raw provider responses, states, and reasons are not exposed through this UI
surface.

Worker progress uses named phases such as source download, generation,
finalization, and output upload. Unknown internals are displayed as waiting;
the controller does not invent a completion percentage.

## Request and output integrity

Worker schema 2 carries a UUID job ID, submission nonce, variation index,
prompt mode, bounded caption and lyrics, resolved model controls, output
format, and signed transfer descriptions. Covers also carry source size,
SHA-256, format, and measured duration.

Completion is accepted only when provider metadata and the uploaded file agree
on job identity, nonce, variation, bytes, SHA-256, schema, model, and runtime
identity. A provider success without a verified upload remains nonterminal
because the upload and status observations may arrive in either order.

Completed outputs are stored below the configured output root with recorded
size and digest. Authenticated playback revalidates the path, type, size, and
SHA-256 before streaming.

Fal jobs are the managed-provider exception to the worker upload contract. The
controller persists the exact reviewed `fal/<endpoint-id>` backend and catalog
snapshot, submits one asynchronous queue request, and on completion exchanges
the key for a short-lived CDN token. It streams the declared audio result into
the private output root with redirects disabled, exact CDN host allowlisting,
size/content-type bounds, SHA-256, fsync, and an atomic rename. Fal result URLs,
prompts, lyrics, and keys never enter durable logs or the browser.

## Persistence and migrations

SQLite is the source of truth for projects, jobs, attempts, outputs,
capabilities, media items/files, playlists, deletion audits, and retained
historical cost records. Files and database state must live on the same
durable deployment data root and be backed up together.

Schema changes use explicit ordered migrations. Application startup refuses
an existing database that is not at the exact expected schema. It never
silently upgrades production data. A migration failure leaves a durable
incomplete marker; recovery is by restoring the verified pre-upgrade backup,
not retrying the partial upgrade.

Schema v11 adds source assets, asset-transfer v2 capabilities, source
provenance, remix clip fields, and derivative tasks to the v10 library. The
ordered v10-to-v11 migration is additive and refuses existing experimental
`kind='source'` rows rather than guessing their ownership. It does not backfill
historical outputs. SQLite constraints/triggers plus repository validation
protect provenance, capability ownership, and derivative readiness;
publication remains an application-level seam because it requires verified
file evidence and idempotent source/output keys.

The quality campaign uses a separate private database and fixture. Its CLI and
ordinary-submission maintenance gate are currently quarantined. The campaign
implementation remains in the source tree because its fail-closed accounting,
recovery, and evaluation contracts are still tested.

## Security and failure rules

- The private UI uses HTTP Basic authentication and same-site CSRF tokens.
- The public proxy forwards only the v1 recovery routes and the v2 upload /
  download routes; only v2 upload locations permit 512 MiB raw bodies.
- Secrets stay in deployment configuration and are redacted from logs.
- Capability-bearing paths are not written to application or proxy access
  logs.
- Paths are resolved below configured roots and symlink traversal fails closed;
  library playback applies the same checks to its database-selected file.
- Library deletion is a state machine (`active -> pending -> deleted`) with a
  private, mode-restricted trash location and delayed purge reconciliation.
- Cleanup may remove expired capabilities, stale partial files, temporary
  uploads, cover clips, and eligible library-trash files. It never silently
  removes an active completed output or published source.
- A malformed provider response, missing evidence, unknown state, or stale
  migration is an error. The system does not infer success or safe teardown.

See [Security](docs/SECURITY.md) for the exposed-surface checklist and
[Operations](docs/OPERATIONS.md) for deployment and recovery procedures.

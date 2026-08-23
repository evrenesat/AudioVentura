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
| - signed source GET                                 |
| - signed output PUT                                 |
+---------------------+-------------------------------+
                      | short-lived HTTPS capabilities
          +-----------+-----------+
          |                       |
          v                       v
+-------------------+   +-----------------------------+
| Home Ingest       |   | Inference provider          |
| - YouTube         |   | - Runpod endpoint           |
| - yt-dlp          |   | - Salad Job Queue           |
| - ffmpeg/ffprobe  |   | - future compatible adapter |
| - restricted SFTP |   |                             |
+-------------------+   | shared ACE-Step runtime     |
                        +-----------------------------+
```

The boundaries are deliberate:

- The controller is the durable control plane. It does not execute media
  tools or load inference models.
- Home Ingest is the only process allowed to contact YouTube or execute
  `yt-dlp`, `ffmpeg`, and `ffprobe`.
- The transfer app is the only public application surface. It has no UI or
  general API routes.
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
- creates projects, jobs, variation attempts, outputs, and capabilities;
- stores state in SQLite through `repository.py`;
- prepares provider-neutral inference requests;
- polls the provider that owns each persisted attempt;
- verifies worker metadata against the uploaded output;
- exposes completed audio only through authenticated media routes.

One process-level lock protects a data root from two controller workers. One
in-process queue serializes jobs and variations. This is a personal service,
not a horizontally scaled controller.

### Transfer service

`src/ace_service/transfers.py` creates a separate FastAPI app with only:

```text
GET /transfer/v1/source/{token}
PUT /transfer/v1/output/{token}
```

Capabilities are bound to one job, direction, path, extension, byte limit,
and expiry. Only a SHA-256 hash of the random token is stored. Source downloads
may be repeated during their lifetime. Output uploads are streamed to a
private `.part` file, hashed, fsynced, and atomically renamed before the
database records completion.

### Home Ingest

`home_ingest/` is an independently installed private service. The controller
sends it a job UUID and public YouTube URL. It downloads one source, validates
duration and size, normalizes it to MP3, and uploads it through a restricted
SFTP account as:

```text
incoming/<job-id>/source.mp3.part
```

The controller validates and atomically finalizes that file before issuing a
provider source capability. A continuation from an existing generated output
reuses the verified local file and does not contact YouTube again.

### GPU worker

`runpod_worker/` contains the shared ACE-Step execution boundary and the
Runpod entry point. `deploy/salad/worker_api.py` wraps the same handler with a
small local HTTP service for Salad's Job Queue Worker.

The runtime loads one pinned ACE-Step source revision and one pinned model
bundle before accepting work. It accepts worker schema 1 for old Runpod jobs
and schema 2 for current jobs. New provider-neutral submissions use schema 2.

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
original: queued -> cloud_queued -> generating -> completed | failed

cover:    queued -> ingesting -> staging
                -> cloud_queued -> generating -> completed | failed
```

One to four variations run sequentially. A supplied seed advances
deterministically for later variations. Status shown in the UI is evidence,
not a control signal.

## Provider boundary

`src/ace_service/providers/base.py` defines the shared contract:

- `submit(request) -> ProviderJobRef`
- `status(ref) -> ProviderStatus`
- `result(ref) -> InferenceResult`
- `cancel(ref) -> CancelOutcome`
- `health() -> ProviderHealth`

Capabilities declare supported modes, request features, worker schemas, and
cancellation behavior. The current providers are:

- `RunpodProvider`, which adapts the existing Runpod queue API;
- `SaladProvider`, which adapts Salad Job Queues and enriches pending status
  with container-group lifecycle evidence.

Deployment management is not part of this interface. Queue creation, GPU
selection, autoscaling, image credentials, and container-group changes remain
provider-specific operator work.

The interface intentionally models prompt-to-audio and audio-to-audio plus
individual request features. A future fal.ai adapter can therefore declare a
smaller capability set without weakening the common job contract. The UI does
not yet offer provider selection or hide unsupported fields; new jobs use the
administrator-configured default.

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

## Persistence and migrations

SQLite is the source of truth for projects, jobs, attempts, outputs,
capabilities, and retained historical cost records. Files and database state
must live on the same durable deployment data root and be backed up together.

Schema changes use explicit ordered migrations. Application startup refuses
an existing database that is not at the exact expected schema. It never
silently upgrades production data. A migration failure leaves a durable
incomplete marker; recovery is by restoring the verified pre-upgrade backup,
not retrying the partial upgrade.

The quality campaign uses a separate private database and fixture. Its CLI and
ordinary-submission maintenance gate are currently quarantined. The campaign
implementation remains in the source tree because its fail-closed accounting,
recovery, and evaluation contracts are still tested.

## Security and failure rules

- The private UI uses HTTP Basic authentication and same-site CSRF tokens.
- The public proxy forwards only the two transfer route families.
- Secrets stay in deployment configuration and are redacted from logs.
- Capability-bearing paths are not written to application or proxy access
  logs.
- Paths are resolved below configured roots and symlink traversal fails closed.
- Cleanup may remove expired capabilities, stale partial files, and temporary
  cover sources. It never removes completed outputs.
- A malformed provider response, missing evidence, unknown state, or stale
  migration is an error. The system does not infer success or safe teardown.

See [Security](docs/SECURITY.md) for the exposed-surface checklist and
[Operations](docs/OPERATIONS.md) for deployment and recovery procedures.

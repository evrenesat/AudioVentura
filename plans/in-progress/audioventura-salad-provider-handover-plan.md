# AudioVentura Salad provider abstraction handoff plan

## 1. Objective and fixed starting point

Implement a durable multi-provider inference boundary with SaladCloud as the
first alternate provider, retain Runpod as a fully working provider, and make
new-job provider choice persistent so switching the default cannot orphan an
already submitted job. Integrate the outstanding Runpod status-resilience plan
into the generic orchestration path rather than implementing two competing
polling state machines.

Start from product commit `0fe5489`. That commit already provides and verifies:

- the Salad queue-worker HTTP adapter and fail-closed readiness supervisor;
- the current immutable ACE-Step v0.1.8 bundle-2 model layer;
- a private GHCR amd64 image at
  `ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:e20eceb01df99d129bd379a545aaf80f02b54c5294a48ba0e4ca424c111e279a`;
- 20 compressed layers totaling `28,979,321,976` bytes, below the conservative
  Salad limit of `35,000,000,000` bytes;
- queue/container-group desired state in `deploy/salad/deployment.json` and
  secret-free provisioning in `deploy/salad/saladctl.py`;
- p100 root storage expanded online from 100 GiB to 180 GiB and recorded in
  Evreniops commit `95c8d68`.

Do not rebuild the model layer unless worker source changes require a final
image. If a final image is needed, use direct `docker buildx ... --push` with
cache reuse; never use `--load` for this 29 GB compressed image.

Remote Salad creation is gated only on the operator-provided organization and
project slugs. The API key exists at `/root/salad_api_key`, contains only the
key, and must never enter Git, logs, process arguments, test output, or docs.

## 2. Scope and non-goals

This plan implements:

1. provider-neutral request, reference, lifecycle, result, cancellation,
   health, capability, and error contracts;
2. persisted provider choice and external ID on jobs and variation attempts;
3. an adapter around the existing Runpod client;
4. a Salad Job Queue provider using the documented public HTTP API;
5. one provider registry so active jobs always use their persisted provider;
6. provider-neutral polling resilience, including every invariant in
   `plans/in-progress/audioventura-runpod-status-resilience-fix-plan.md`;
7. controller configuration/readiness/UI language needed to run Salad without
   adding a browser provider selector yet;
8. offline tests, documentation, deployment, one cold cover, and one local
   continuation using the bounded paid fixture.

Do not implement a fal.ai client or UI selector in this version. Do not rename
`runpod_worker`; it is a legacy-named shared ACE-Step runtime until a separate
low-risk rename. Do not expose deployment operations (`scale`, `deploy`, GPU
selection, logs, revisions) on the runtime provider interface. Do not put
Salad organization, project, queue, container-group, registry, instance, or
autoscaler concepts into generation code or persisted request parameters.

Do not retry provider submission automatically. A committed submission nonce
with no persisted provider ID remains `uncertain_cloud_submission` and fails
closed on recovery. Salad metadata may aid manual reconciliation but does not
make a lost POST response safe to repeat.

## 3. Provider contract decisions

Create `src/ace_service/providers/AGENTS.md` for the provider boundary and the
following modules:

```text
src/ace_service/providers/
  __init__.py
  base.py
  registry.py
  runpod.py
  salad.py
```

`base.py` owns these immutable types and protocols:

```python
class ProviderName(StrEnum):
    RUNPOD = "runpod"
    SALAD = "salad"

class InferenceMode(StrEnum):
    PROMPT_TO_AUDIO = "prompt_to_audio"
    AUDIO_TO_AUDIO = "audio_to_audio"

class RequestFeature(StrEnum):
    PROMPT = "prompt"
    LYRICS = "lyrics"
    SOURCE_AUDIO = "source_audio"
    CUSTOM_DURATION = "custom_duration"
    BPM = "bpm"
    KEY = "key"
    TIME_SIGNATURE = "time_signature"
    LANGUAGE = "language"
    INSTRUMENTAL = "instrumental"
    COVER_STRENGTH = "cover_strength"
    PROMPT_MODE = "prompt_mode"

class ProviderPhase(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"
    TOO_LATE = "too_late"
    UNSUPPORTED = "unsupported"

class DetailScope(StrEnum):
    JOB = "job"
    DEPLOYMENT = "deployment"

class ProviderErrorKind(StrEnum):
    TRANSIENT = "transient"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    REJECTED = "rejected"
```

Add frozen, slotted dataclasses:

- `ProviderCapabilities(name, modes, request_features, accepts_worker_schema,
  supports_pending_cancel, supports_running_cancel, not_found_after_deadline_is_terminal)`;
- `InferenceRequest(application_job_id, variation_index, submission_nonce,
  mode, requested_features, worker_payload, execution_timeout_ms,
  queue_timeout_ms)`; copy/freeze the payload at construction;
- `ProviderJobRef(provider, external_id)` with bounded provider-specific ID
  validation and a provider-match guard;
- `ProviderStatus(phase, message=None, progress=None, provider_state=None,
  provider_reason=None, detail_scope=DetailScope.JOB)`; progress is `0.0..1.0`;
- `InferenceResult(metadata)`; the mapping is bounded and copied. It is the
  provider's small terminal metadata, never audio bytes;
- `ProviderHealth(ok, message, queued_jobs=None, running_instances=None)`.

Add bounded exceptions `ProviderError` and `ProviderJobNotComplete`.
`ProviderError` carries only kind, operation, optional HTTP status, and a safe
message. It must never retain response bodies, response objects, headers,
API keys, prompts, lyrics, or capability URLs.

The async `InferenceProvider` protocol exposes only:

```python
capabilities: ProviderCapabilities
async def submit(request: InferenceRequest) -> ProviderJobRef: ...
async def status(ref: ProviderJobRef) -> ProviderStatus: ...
async def result(ref: ProviderJobRef) -> InferenceResult: ...
async def cancel(ref: ProviderJobRef) -> CancelOutcome: ...
async def health() -> ProviderHealth: ...
```

Runpod and Salad declare both inference modes and every current ACE-Step
request feature. A future fal provider can declare prompt-to-audio and/or
audio-to-audio independently and omit unsupported request features. Add
`unsupported_features(capabilities, request)` so a future UI and the server
can use the same capability decision; no HTML control is added now.

`registry.py` maps `ProviderName` to configured provider instances, rejects
duplicates, provides `get(name)`, and exposes a default provider. Never fall
back silently: if a job names an unconfigured provider, startup/readiness or
job processing fails closed with a bounded configuration error.

## 4. Durable schema and rollback compatibility

Inspect before editing:

```text
src/ace_service/models.py
src/ace_service/migrations.py
src/ace_service/repository.py
tests/test_migrations.py
tests/test_persistence.py
```

Advance the application schema by one ordered additive migration. Add nullable
columns:

```text
jobs.inference_provider VARCHAR(32)
jobs.current_provider_job_id VARCHAR(128)
jobs.provider_result_json JSON

variation_attempts.inference_provider VARCHAR(32)
variation_attempts.provider_job_id VARCHAR(128)
variation_attempts.provider_result_json JSON

outputs.inference_provider VARCHAR(32)
outputs.provider_job_id VARCHAR(128)
```

Backfill transactionally:

- every historical job gets `inference_provider='runpod'`;
- generic current/result fields copy the legacy Runpod fields when present;
- attempts and outputs with a Runpod job ID get provider `runpod` and the same
  generic ID; attempts without an ID inherit the parent job provider;
- reject conflicting partially populated values instead of overwriting them.

Keep `current_runpod_job_id`, `runpod_job_id`, and `runpod_result_json` for one
rollback window. Generic repository functions are authoritative after the
migration. When the provider is Runpod, mirror generic writes to legacy fields;
when it is Salad, legacy fields remain null. Reads prefer generic fields and
fall back to legacy Runpod fields only for an unmigrated compatibility object.

Replace provider-shaped repository operations with:

```text
prepare_variation_submission(..., inference_provider)
persist_variation_provider_job_ref(..., ProviderJobRef)
set_variation_provider_result(...)
```

Retain thin legacy wrapper functions only where existing tests or rollback code
need them; wrappers must call the generic functions and cannot implement a
second state machine. Persist the selected provider when the web route creates
the job, before enqueue. New jobs use the registry default. Changing the
default later never changes an existing job or attempt.

Migration tests must cover a production-shaped v6 database, exact backfill,
empty IDs, mixed terminal/nonterminal rows, idempotent status checks, and
preservation of all legacy IDs/results/evidence.

## 5. Runpod adapter and resilience integration

Keep `src/ace_service/runpod_client.py` as the strict low-level transport.
Implement `providers/runpod.py` as the only translator between Runpod shapes
and the generic contract:

- submission calls the existing client once and returns a Runpod ref;
- `IN_QUEUE -> QUEUED`, `IN_PROGRESS -> RUNNING`, `COMPLETED -> SUCCEEDED`,
  `FAILED/TIMED_OUT -> FAILED`, and `CANCELLED -> CANCELLED`;
- result performs a fresh validated status call, requires `COMPLETED`, and
  returns its mapping;
- cancellation maps the exact validated Runpod `CANCELLED` acknowledgement to
  `CancelOutcome.CANCELLED`;
- health wraps the existing endpoint health contract;
- Runpod worker-count enrichment stays in this adapter and reports only
  provider-evidenced queued/initializing language.

Implement every still-open invariant from
`audioventura-runpod-status-resilience-fix-plan.md` in this generic path:

1. Give low-level `RunpodAPIError` a bounded `status_code: int | None`.
2. Translate transport, HTTP 408/429/5xx, and malformed status/result bodies to
   bounded provider uncertainty. Do not catch unrelated programming errors.
3. A status/result uncertainty before the durable deadline leaves job,
   attempt, provider ref, last progress, transfers, and cover source unchanged.
4. Use `VariationAttempt.started_at + inference_job_timeout_seconds`; never
   use `updated_at`.
5. Preserve the current validated-output recovery first. Schema v2 completes
   only with correlated persisted completion metadata plus validated output.
6. At/after deadline, cancel the exact persisted ref. Only `CANCELLED` permits
   local timeout failure. `TOO_LATE`, `UNSUPPORTED`, or cancellation uncertainty
   keeps the attempt active and reconciling.
7. Only a provider whose capabilities set
   `not_found_after_deadline_is_terminal=True` may turn a post-deadline 404
   into provider expiry. Set it true for Runpod and false for Salad.
8. No status/result failure may call generic task failure, revoke transfers,
   remove source, or submit a replacement.
9. Add per-process consecutive poll-error backoff keyed by product job ID:
   `max(base_poll,1)`, double, cap 60 seconds, clear on a contract-valid status,
   result, terminal transition, or stop. Durable correctness never depends on
   the counter.
10. Log only product job ID, provider name, operation, exception class, optional
    status, count, and next delay.

Do not implement the resilience behavior twice in the Runpod adapter and
controller. The adapter classifies; the controller owns durable deadlines,
retry scheduling, cancellation authority, and cleanup ordering.

## 6. Salad provider

Implement `providers/salad.py` with the existing `httpx` dependency rather
than adding the alpha SDK to the controller runtime. Use only documented API
paths under:

```text
/organizations/{organization}/projects/{project}/queues/{queue}/jobs
```

Use the `Salad-Api-Key` header, bounded connect/read/write/pool timeouts, no
automatic transport retries, and a maximum JSON response size of 1 MiB.
Validate organization/project/queue/container-group names with Salad's DNS
pattern and job IDs as UUIDs.

Submission body:

```json
{
  "input": "<the copied worker_payload object>",
  "metadata": {
    "application_job_id": "<UUID>",
    "variation_index": 1,
    "submission_nonce": "<opaque nonce>",
    "worker_schema_version": 2
  }
}
```

Do not send a webhook. Validate the response ID, echoed metadata, and
`pending` state. A mismatched ID/metadata or malformed body is an invalid
response, not a second-submission trigger.

Status mapping is exact:

```text
pending   -> QUEUED (then optional deployment enrichment)
running   -> RUNNING
succeeded -> SUCCEEDED
failed    -> FAILED
cancelled -> CANCELLED
anything else -> UNKNOWN, nonterminal
```

`result()` performs a fresh GET, requires `succeeded`, validates `output` as a
bounded mapping, and returns it as `InferenceResult`. Audio remains in the
signed output upload, so the queue response stays metadata-only.

`cancel()` first gets the exact job. For `pending`, issue one DELETE and require
HTTP 202, returning `CANCELLED`; subsequent reconciliation confirms the
terminal state. For `running`, return `TOO_LATE`. For terminal states, return
`CANCELLED` only for `cancelled`, otherwise `TOO_LATE`. Never kill or scale a
container group to cancel a job, because Salad retries interrupted workers.

For a pending job only, query the configured container group's instances. With
the enforced max replica count of one, enrich status only when the response is
unambiguous:

```text
no instance                 QUEUED        Waiting for worker
allocating                  PROVISIONING  Waiting for GPU
downloading                 PROVISIONING  Downloading worker image + 0..1 progress
creating                    STARTING      Starting worker
running and not ready       STARTING      Initializing ACE-Step
running and ready           QUEUED        Worker ready
```

Set `detail_scope=DEPLOYMENT`. If more than one relevant instance exists,
instance JSON is malformed, or enrichment is unavailable, return the valid job
status without invented detail; enrichment failure must not make job status
unavailable.

Salad health GETs the configured queue and container group. Scale-to-zero with
an empty queue is healthy. Do not expose registry credentials or infrastructure
mutation through this provider.

Add mocked tests for every method/state, mismatched metadata/IDs, body limits,
secret redaction, 404/429/5xx classification, pending/running cancellation,
result-before-upload and upload-before-result arrival orders, deployment-scope
enrichment, and enrichment failure fallback. No test contacts Salad.

## 7. Controller configuration, construction, and UI

Inspect and update:

```text
src/ace_service/config.py
src/ace_service/app.py
src/ace_service/web.py
src/ace_service/worker.py
src/ace_service/templates/dashboard.html
src/ace_service/templates/job_detail.html
tests/test_config.py
tests/test_app.py
tests/test_web.py
tests/test_worker.py
```

Add settings with deployment aliases and bounded validation:

```text
INFERENCE_PROVIDER                 runpod|salad, default runpod
INFERENCE_JOB_TIMEOUT_SECONDS      default 7200; accept old Runpod alias
SALAD_API_KEY
SALAD_ORGANIZATION
SALAD_PROJECT
SALAD_QUEUE_NAME                   default audioventura-jobs
SALAD_CONTAINER_GROUP_NAME         default audioventura-ace-step-v1
SALAD_POLL_INTERVAL_SECONDS        default 2
SALAD_CONNECT_TIMEOUT_SECONDS      default 5
SALAD_READ_TIMEOUT_SECONDS         default 30
SALAD_WRITE_TIMEOUT_SECONDS        default 30
SALAD_POOL_TIMEOUT_SECONDS         default 5
```

Conditional deployable validation requires Salad settings when Salad is the
default and Runpod settings when Runpod is configured. Production construction
must also register Runpod when valid Runpod credentials exist so old Runpod
jobs remain reconcilable after the default changes to Salad. Tests may inject
a registry directly.

Change `ControllerWorker` to accept `ProviderRegistry`, not `RunpodWorkerClient`.
Keep a temporary `runpod_client=` compatibility keyword only if required to
avoid rewriting unrelated tests at once; it must construct a one-provider
registry internally and be marked for removal.

At submission, read the job's persisted provider, derive mode from job type,
derive requested features from its normalized request, create one
`InferenceRequest`, validate capabilities, submit once, and persist the exact
returned ref. At poll/result/cancel, re-read the persisted ref before and after
every external await and require it to be unchanged before committing.

Change readiness component naming to `inference_provider` and include the
active provider's safe label. Preserve a compatibility `runpod_api` field only
if an existing deployment probe requires it; it cannot determine readiness
when Salad is active. Job detail renders the persisted provider and generic
phase/result timing. Replace new user-facing hard-coded “Runpod” text with
“cloud provider” or the safe provider display name. Historical billing/evidence
field names may remain during the rollback window.

Do not add a form selector in this checkpoint. The persisted provider field,
registry, and `ProviderCapabilities` are the exact seam for a later selector:
the server will validate a requested provider and the browser will hide or
disable controls from `request_features`. Never rely on browser hiding for
validation.

## 8. Worker retry and idempotency contract

Keep the schema-v2 worker payload and signed data path. The output PUT route is
already idempotent for identical bytes while the consumed capability remains
unexpired and returns HTTP 409 for conflicting bytes. Add/retain an integration
test proving a Salad retry after a successful upload can upload the same bytes
and return correlated completion metadata without duplicating the output.

Salad may retry an interrupted job up to three times. Stable application job
ID, variation index, submission nonce, expected output path, and upload
capability must remain identical across provider retries. The controller does
not create a second transfer or provider job. Do not add permanent object-store
credentials to the worker.

Running Salad jobs cannot be cancelled by the queue API. This version returns
`TOO_LATE` and continues reconciliation. A future provider-neutral cooperative
cancel capability may be added around ACE-Step stage boundaries, but do not
claim instantaneous cancellation of the current blocking inference call.

## 9. Checkpoint sequence and verification

### Checkpoint A — contracts, migration, and adapters

Implement Sections 3–6 plus focused tests. Expected observations:

- generic types contain no Salad deployment mutation methods;
- v6 data migrates without losing or changing a Runpod ID/result;
- Runpod and Salad map to the same finite lifecycle;
- no mocked submission is called more than once;
- Salad requests contain no audio bytes or infrastructure credentials.

Run:

```text
uv run pytest -q tests/test_migrations.py tests/test_persistence.py \
  tests/test_runpod_client.py tests/test_providers.py tests/test_salad_provider.py
uv run ruff check src/ace_service/providers src/ace_service/runpod_client.py \
  tests/test_providers.py tests/test_salad_provider.py tests/test_migrations.py \
  tests/test_persistence.py tests/test_runpod_client.py
uv run ruff format --check src/ace_service/providers src/ace_service/runpod_client.py \
  tests/test_providers.py tests/test_salad_provider.py tests/test_migrations.py \
  tests/test_persistence.py tests/test_runpod_client.py
uv run mypy src/ace_service/providers src/ace_service/runpod_client.py
git diff --check
```

### Checkpoint B — generic orchestration and status resilience

Implement Sections 5, 7, and 8 in controller/repository/web code. Replace the
incorrect test that terminally fails schema-v2 on one status exception. Add the
full deterministic matrix from the existing resilience plan for both a Runpod
provider and representative Salad outcomes.

Expected observations:

- an HTTP 500/status parse failure leaves the exact provider ref active;
- restart never resubmits a nonce or ref;
- deadline cancellation requires `CancelOutcome.CANCELLED`;
- Salad running cancellation returns too-late and remains active;
- Runpod post-deadline 404 may expire, Salad 404 may not;
- backoff reaches but never exceeds 60 seconds and resets on valid status;
- validated output plus correlated schema-v2 result completes in either order;
- unrelated controller `ValueError` still becomes `controller_task_error`.

Run:

```text
uv run pytest -q tests/test_worker.py tests/test_web.py tests/test_app.py \
  tests/test_config.py tests/test_runpod_client.py tests/test_providers.py \
  tests/test_salad_provider.py tests/test_transfers.py
uv run ruff check src/ace_service tests/test_worker.py tests/test_web.py \
  tests/test_app.py tests/test_config.py tests/test_transfers.py
uv run ruff format --check src/ace_service tests/test_worker.py tests/test_web.py \
  tests/test_app.py tests/test_config.py tests/test_transfers.py
uv run mypy src
git diff --check
```

### Checkpoint C — docs and complete offline verification

Update `README.md`, `ARCHITECTURE.md`, `docs/ARCHITECTURE.md`,
`docs/OPERATIONS.md`, `docs/SALAD.md`, `DEVLOG.md`, `.env.example`, and any
service example environment. Document per-job provider ownership, generic
status uncertainty, Salad cancellation limits, scale-to-zero, exact secrets,
rollback compatibility, and the future capability-driven selector seam.

Run the complete required matrix:

```text
uv run pytest -q tests runpod_worker/tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src runpod_worker
(cd home_ingest && uv run pytest -q)
(cd home_ingest && uv run ruff check .)
(cd home_ingest && uv run ruff format --check .)
(cd home_ingest && uv run mypy src)
shellcheck deploy/salad/entrypoint.sh
docker buildx build --check -f deploy/salad/Dockerfile .
git diff --check
```

All focused/new tests must pass. If a known unrelated date-sensitive private
fixture fails, reproduce it on `0fe5489`, record the exact baseline, and do not
weaken or skip new coverage.

Commit the implementation as one reviewed accomplishment after all checkpoints
pass, using a message such as:

```text
feat/inference-providers: add Salad job orchestration
```

## 10. Deployment and live acceptance (primary agent)

The implementation worker stops after the clean commit and reports exact test
results. The primary agent performs external deployment.

1. Obtain exact Salad organization/project slugs. Read `/root/salad_api_key`
   only into an environment variable. Resolve a read-only GHCR package token
   without printing it.
2. Run `saladctl.py inspect`; require no conflicting queue/group.
3. Apply the reviewed amd64 digest. Re-inspect exact image, queue connection,
   probes, batch priority, GPU IDs, autoscaler, `replicas=0`, empty queue, and
   no pending change. Do not submit a dummy paid job.
4. Read-only reconcile the 2026-08-23 Runpod incident before deployment: inspect
   product job `56270787-2460-4633-982d-45c9f759f558` and provider job
   `0d6f2011-9cc6-4d79-849c-367689ecd8f5-e2`. Do not submit a replacement. If
   the exact provider job is unexpectedly still nonterminal, report its state
   and obtain the existing plan's explicit cancellation authority before
   cancelling only that ID.
5. Add provider-neutral/Salad secrets and default-provider configuration to
   the private Evreniops deployment repo. Preserve Runpod credentials so active
   or historical Runpod refs remain reconcilable. Run its tests and commit.
6. Back up production SQLite, run migration status, stop the controller only
   for the offline migration, upgrade one schema version, deploy the reviewed
   product commit, and verify transfer/home services are unchanged.
7. Verify authenticated readiness reports Salad healthy at scale zero and new
   job creation persists provider `salad` before submission.
8. Run exactly the existing bounded paid fixture in
   `tests/live_paid_ui_e2e.py`:
   - source `https://www.youtube.com/watch?v=Z7OwQ5c8Jv8`;
   - prompt `Energetic acoustic pop-rock track, strummed acoustic guitar, strong dance groove, festive live-performance energy. Preserve the source melody, hook, tempo, rhythmic drive, and high energy.`;
   - one approximately 60-second cover;
   - one approximately 60-second continuation that reuses the first local
     output and never contacts YouTube;
   - exact budget two paid submissions for this acceptance run; never exceed
     the user's overall authorization of ten.
9. Record cold lifecycle timestamps: submission, autoscaler observation,
   allocation, download start/end and progress, creating, readiness, job
   running, inference, upload, success, and return to zero. Record provider
   status/evidence, not inferred billing claims.
10. Verify both outputs are authenticated, nonempty, within configured byte
   bounds, correlated to completion metadata, and attached to the same project
   continuation chain. Smoke every UI route covered by the test.
11. Wait for empty queue, `replicas=0`, no running instances, and stable
    controller readiness. If a job is uncertain, do not submit a replacement;
    reconcile the same persisted provider ID using the generic status rules.

Final acceptance requires both paid jobs complete, the second source is local,
Runpod production resources remain untouched, Salad returns to zero, all
services are healthy, and no key/capability/prompt/lyrics leaked into tracked
files or logs.

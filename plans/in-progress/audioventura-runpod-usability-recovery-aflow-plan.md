# AudioVentura Runpod Generation Usability Recovery

## Summary

Restore production song generation by correcting the live Runpod worker-template contract, proving the already-queued production song completes, and leaving a small tested operator check that detects this exact image/environment mismatch before another deployment. Reduce new-generation latency and spend by defaulting both generation forms to one variation, and remove the redundant post-extraction cover confirmation so one explicit submit continues automatically after validated YouTube preparation. Preserve the deployed application release, immutable worker image, model volume, one-at-a-time execution, scale-to-zero, existing queued job, security boundaries, and the current provider-cost envelope. Region and GPU generation are not product requirements: any compatible capacity may be used if it can eventually finish and does not exceed that envelope.

## Git Tracking

- Plan Branch: `aflow/audioventura-usability-recovery-20260810`
- Pre-Handoff Base HEAD: `ea668675c014e07ad011a81de7473cd62a469d2f`
- Last Reviewed HEAD:
- Review Log:

## Live Incident Identity

- Product checkout: `/root/code/audioventura-usability-recovery-20260810`
- Production operations checkout: `/root/code/evreniops-audioventura-deploy`
- Pinned inventory: `/root/code/evreniops-audioventura-deploy/infra/ansible/inventory/audioventura.yml`
- Product release: `ea668675c014e07ad011a81de7473cd62a469d2f`
- Runpod endpoint: `p1t6aef0dlpz5e`, version `8`
- Runpod template: `37lrt6ox2k`
- Immutable worker image: `ghcr.io/evrenesat/audioventura-ace-step-worker@sha256:103886d62e65235db96f6f02a4049ffdee74a80e7b1ffee7f055c2e421b17436`
- Existing product job: `c63a2910-76a8-4cf3-bf84-05062bc4e68d`
- Existing Runpod request: `d5c279ab-9807-4247-8e0c-37409ffbf314-e2`
- Recovery image identity value: `sha256:103886d62e65235db96f6f02a4049ffdee74a80e7b1ffee7f055c2e421b17436`

## Proven Cause

- Live Runpod health reports `jobs.inQueue=1`, `jobs.inProgress=0`, zero idle/running/ready/initializing/throttled workers, and one unhealthy worker.
- The live template is pinned to the immutable image above and its environment keys are only `ACE_TRANSFER_ALLOWED_HOST` and `ACE_WORKER_CHECKPOINTS_DIR`.
- `runpod_worker.runtime.initialize_runtime()` calls `validate_worker_image_digest(os.environ.get("ACE_WORKER_IMAGE_DIGEST"))` before CUDA/model initialization and intentionally raises `WorkerInitializationError` when it is absent.
- Therefore the live worker is contractually unable to initialize. Runpod capacity is not the primary incident cause; changing region or GPU class cannot repair a worker that exits before CUDA/model initialization.
- The product records trusted GPU rates and quotes but has no hard production per-song or hourly-price gate. For this recovery, the effective cost ceiling is fail-closed: do not admit a GPU class priced above the highest currently allowed class, and do not broaden the allow-list without fresh trusted price and compatibility evidence.

## Done Means

- The live template contains `ACE_WORKER_IMAGE_DIGEST` equal to the digest portion of its existing immutable image reference, without changing or exposing any other environment value.
- Endpoint/template identity, model volume, CUDA floor, GPU list, scaler, min/max workers, idle timeout, concurrency, transfer host, and checkpoints path remain unchanged except for the provider's expected version/rolling-release metadata. This first repair deliberately avoids a capacity-policy change because the initialization defect is already sufficient to explain the outage.
- The existing queued Runpod request is not cancelled, duplicated, retried, or replaced and reaches `COMPLETED` through exactly one worker.
- Product job `c63a2910-76a8-4cf3-bf84-05062bc4e68d` reaches `completed`, has one verified non-empty output, and its authenticated job/media surfaces are usable.
- After completion and the 30-second idle timeout, provider health reports zero queued/in-progress jobs and zero active paid workers.
- A checked-in read-only preflight detects a missing/mismatched image-digest environment contract without printing secrets; focused and full regression checks pass.
- New original and cover forms default to one variation while retaining the explicit 1-4 choice.
- A newly submitted cover performs validated home extraction, captures its server-owned cost quote, and proceeds into the existing serialized cloud queue without a second confirmation click. Rights confirmation remains required on the initial form, and detected duration remains visible in job status/detail.

## Critical Invariants

- Never print, copy, persist, or place the Runpod API key, Basic Auth password, full protected environment, capability URL, source URL, prompt, lyrics, or template environment values in commands, logs, evidence, commits, or plan updates.
- Load protected production configuration only on the pinned deployment target. Authenticate provider requests with an `Authorization` header, never a query parameter.
- Preserve `workersMin=0`, `workersMax=1`, `idleTimeout=30`, `scalerType=REQUEST_COUNT`, `scalerValue=1`, `gpuCount=1`, `minCudaVersion=12.8`, the current GPU types, template ID, network volume `bgh5crlzt8`, image reference, and endpoint ID during the deterministic template repair.
- Treat region, data-center location, GPU age, and GPU generation as non-semantic. If the corrected worker contract later proves a distinct capacity shortage, a follow-up capacity change may prefer any slower/older compatible serverless GPU with sufficient VRAM and runtime support, but must use a fresh trusted hourly rate, must not exceed the highest fresh rate in the current allowed pool, and must not weaken scale-to-zero or one-at-a-time execution.
- Do not submit another generation. The existing queued request is the only paid functional proof.
- Do not cancel or retry the existing request unless it becomes terminal failed and a later owner decision explicitly authorizes a paid replacement.
- Do not modify the dirty primary checkout `/root/code/audioventura`; work only in the clean AFlow worktree.
- Do not change the product release, controller/transfer services, nginx, database schema, worker image, model weights, GPU pool, cost model, or deployment repository in this run. A later GPU-pool change requires separate evidence that the corrected template still faces capacity pressure and that the candidate remains within the effective ceiling.
- Every provider mutation must be a compare-and-swap style operation: re-read identities and current configuration, refuse drift, snapshot the exact prior bounded shape in memory, mutate only the missing key, re-read and verify, and retain a precise inverse operation for rollback.
- Removing the second cover confirmation must not remove initial rights confirmation, URL/duration validation, source limits, server-owned quote capture, CSRF/authentication, serialized submission, or durable uncertain-submission protection.

## Forbidden Implementations

- No second endpoint, template, network volume, pod, controller, AFlow run, song request, or worker concurrency.
- No `workersMin=1`, no broadened or more-expensive GPU pool in this run, no lower CUDA floor, no image tag, no mutable image, no model download, and no paid synthetic smoke.
- No Runpod console automation, secret export to p100, query-string authentication, raw response dumping, or shell tracing.
- No broad refactor, UI redesign, retry loop, queue bypass, fake completed status, manual database completion, or direct output fabrication.
- No deletion or destructive replacement of provider resources.

## Checkpoints

### [ ] Checkpoint 1: Add a fail-closed worker-template contract preflight

**Goal:**

- Add one small read-only operator preflight that proves the configured endpoint template's immutable image and required image-identity environment key agree, with bounded secret-free output.

**Context:**

- Run: `git rev-parse --show-toplevel && git status --short --branch`
- Inspect: `AGENTS.md`, `runpod_worker/AGENTS.md`, `runpod_worker/Dockerfile`, `runpod_worker/runtime.py`, `docs/RUNPOD.md`, `docs/OPERATIONS.md`, `src/ace_service/runpod_client.py`, existing test patterns
- Preserve: runtime fail-closed image validation and all controller/worker boundaries

**Scope:**

- May create or modify: one narrowly named operator module under `src/ace_service/`, one focused test file under `tests/`, `docs/RUNPOD.md`, `docs/OPERATIONS.md`, `DEVLOG.md`
- Must not touch: production credentials, templates, UI, database models/migrations, generation payloads, worker runtime semantics, `home_ingest/`, deployment repository
- Constraints: standard library plus existing `httpx`; dependency-injected transport or pure parser for tests; no provider mutation code in this checkpoint

**Steps:**

- [ ] Implement a bounded parser/preflight that accepts only the endpoint/template fields needed to compare endpoint ID, template ID, immutable `imageName`, and environment-key presence/value for `ACE_WORKER_IMAGE_DIGEST`.
- [ ] Require an OCI image reference ending in `@sha256:<64 lowercase hex>` and require the environment value to equal `sha256:<same digest>` exactly.
- [ ] Return a small structured result with safe identities, booleans, and stable reason codes. Never return environment values other than the already-public image digest, and never retain or render auth headers/raw bodies.
- [ ] Add tests for exact success; missing key; mismatched digest; mutable tag; malformed response; oversized/unexpected structures; secret-like unrelated environment values proving they never appear in output or exceptions.
- [ ] Document the production preflight and the rule that API authentication must use the header and protected configuration stays on the target.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run pytest -q tests/test_runpod_template_preflight.py`
- Run: `uv run ruff check src/ace_service tests/test_runpod_template_preflight.py`
- Run: `uv run ruff format --check src/ace_service tests/test_runpod_template_preflight.py`
- Run: `git diff --check`
- Observe: fixtures containing unrelated secret values never disclose them in results or failures.

**Done When:**

- The preflight detects the live incident cause from bounded provider metadata and is safe to run with production responses.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and changed files stay within scope.

**Blockers:**

- Stop if the preflight would require exposing or persisting protected values.
- Stop if unrelated dirty files appear in the clean worktree.

### [ ] Checkpoint 2: Make one variation and one-submit covers the default UX

**Goal:**

- Default new original and cover generations to one variation and make a successful YouTube extraction continue automatically into the existing cloud-submission path without a redundant confirmation screen.

**Context:**

- Inspect: `src/ace_service/schemas.py`, `src/ace_service/web.py`, `src/ace_service/worker.py`, `src/ace_service/repository.py`, `src/ace_service/templates/original_form.html`, `src/ace_service/templates/cover_form.html`, `src/ace_service/templates/job_detail.html`, `tests/test_web.py`, `tests/test_cover_workflow.py`, `tests/test_worker.py`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`
- Existing intent: the second cover confirmation delayed paid submission until extraction revealed duration. Owner direction replaces that two-click policy with one explicit initial submit while retaining all server-side gates.

**Scope:**

- May modify: schemas, web/controller orchestration, cover staging helpers, generation templates, focused tests, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `DEVLOG.md`
- Must preserve: explicit initial rights confirmation, one approved YouTube video validation, home-ingest isolation, duration/source-size limits, server-owned quote creation before Runpod submission, one-at-a-time execution, capability/nonce/idempotence protections, status visibility, and continuation/edit behavior
- Must not mutate: production data, provider resources, deployment configuration, worker image/runtime, billing evidence schema, authentication/CSRF, or existing terminal jobs

**Steps:**

- [ ] Change `CoverRequest.variation_count` to default `1` with range `1..4`; change `_cover_form_values` and the cover form selector to default `1` and offer `1..4`. Keep the original form's already-correct default `1` and add an explicit regression proving both forms default to one.
- [ ] Preserve initial rights confirmation as the single user authorization. Update cover-form copy so it says validated extraction continues automatically; remove copy that promises a later confirmation.
- [ ] Refactor submission-quote capture only as needed so the controller worker can invoke the same server-owned quote path after successful extraction without constructing a web request or duplicating cost logic.
- [ ] After home ingest validates identity, duration, size, and source persistence, atomically finalize the cover staging record, mark it confirmed by the automatic policy, capture exactly one submission quote, commit, and continue through the existing `_submit_variation(job_id, 1)` path. Do not enqueue a second job or create a second product record.
- [ ] Keep the authenticated confirmation endpoint only as a backward-compatible escape hatch for pre-deployment rows already durably awaiting confirmation; new submissions must never require or render that form. Do not auto-submit arbitrary historical staged rows during startup recovery.
- [ ] Keep detected duration and preparation/generation status visible on the job detail/status responses. Remove the new-flow confirmation/cancellation UI only after automatic submission has durably crossed the same server-owned gates.
- [ ] Add focused tests proving: both defaults are one; explicit 2-4 still validate and serialize; successful cover preparation auto-confirms, creates one quote, and submits exactly once; failed extraction submits nothing; restart/duplicate processing does not double-submit; initial rights/URL/duration/source limits remain enforced; legacy awaiting-confirmation rows retain the compatibility endpoint; continuation/edit defaults and preserved values remain correct.
- [ ] Update architecture, operations, and DEVLOG wording from the two-step cover policy to the one-submit automatic policy.

**Dependencies:**

- Checkpoint 1.

**Verification:**

- Run: `uv run pytest -q tests/test_web.py tests/test_cover_workflow.py tests/test_worker.py tests/test_costs.py`
- Run: `uv run ruff check src/ace_service tests`
- Run: `uv run ruff format --check src/ace_service tests`
- Run: `uv run mypy src`
- Run: `git diff --check`
- Observe: no test creates more than one provider submission for one cover job, and failure before finalized extraction creates none.

**Done When:**

- One initial cover submit flows from validated extraction into generation without another click, with one variation by default and all security, cost-evidence, and idempotence gates intact.
- Every completed step is validated against code, tests, or observable behavior.

**Blockers:**

- Stop if automatic continuation cannot reuse the existing quote and serialized submission boundaries without weakening idempotence or creating a duplicate paid request.
- Stop if unrelated dirty files appear in the clean worktree.

### [ ] Checkpoint 3: Repair the live template and prove the existing song completes

**Goal:**

- Apply the one-field reversible provider repair, let the already-queued song complete, and prove the endpoint returns to zero workers at rest.

**Context:**

- Read and follow `/root/code/evreniops-audioventura-deploy/infra/services/audioventura/AGENTS.md`.
- Operate from p100 through the pinned inventory and load `/etc/audioventura/controller.env` only on `audioventura_beta` with become.
- Use the official REST control plane `https://rest.runpod.io/v1` and queue API `https://api.runpod.ai/v2`; use header authentication.
- The template-update API is `PATCH /v1/templates/37lrt6ox2k` or its documented `/update` synonym. Preserve all required current template fields and existing environment values; add only `ACE_WORKER_IMAGE_DIGEST`.

**Scope:**

- May mutate: only template `37lrt6ox2k` by adding the exact image-digest environment key; provider-generated endpoint rolling/version state; the normal existing job/worker/output state caused by request `d5c279ab-9807-4247-8e0c-37409ffbf314-e2`
- May write: one sanitized incident/recovery record under `docs/operations/` and the plan's Git Tracking/Review Log
- Must not mutate: endpoint semantic configuration, template image/commands/other env values, worker count settings, job request, application database directly, deployment checkout, production release, services, nginx, model volume, GPU pool

**Steps:**

- [ ] Re-read endpoint, template, health, queued request, product job, current release, and service states. Refuse unless all identities and the proven-cause fingerprint still match, including absent `ACE_WORKER_IMAGE_DIGEST`, immutable image digest, one queued request, and no active worker.
- [ ] Construct the template update from the live bounded template object while retaining every existing required field and environment entry in memory. Add exactly `ACE_WORKER_IMAGE_DIGEST=sha256:103886d62e65235db96f6f02a4049ffdee74a80e7b1ffee7f055c2e421b17436`. Do not print request bodies or environment values.
- [ ] PATCH once. Require HTTP 200, re-read the template, run the new preflight, and prove no unrelated bounded field or environment-key set changed. Record only safe identity, before/after key names, provider version, and timestamps.
- [ ] If the endpoint does not roll automatically, issue at most one documented idempotent endpoint PATCH that preserves every semantic field and references the same template solely to trigger the provider's rolling release; require a version change or a new initializing/ready worker. Do not repeat an unchanged repair fingerprint.
- [ ] Poll the existing request at a bounded cadence. Require transition from `IN_QUEUE` to `IN_PROGRESS`/`RUNNING` to `COMPLETED`; capture delay/execution time, actual GPU, output byte count/hash, and immutable runtime identity only through existing validated product/provider contracts.
- [ ] If the corrected template passes its contract preflight but the request remains queued with no unhealthy worker, classify that as a separate capacity incident. Record fresh stock and hourly-rate evidence without treating location or GPU generation as requirements. Do not mutate the GPU pool in this run; produce a focused follow-up whose candidate set contains only compatible serverless classes at or below the highest fresh hourly rate already admitted by the current pool.
- [ ] Verify product job `c63a2910-76a8-4cf3-bf84-05062bc4e68d` becomes `completed`, exactly one non-empty output exists, and authenticated job/media access succeeds without exposing credentials or downloading user media into the repository.
- [ ] After the 30-second idle timeout, require `inQueue=0`, `inProgress=0`, `idle=0`, and `running=0`. Verify release symlink and controller/transfer/nginx remain correct.
- [ ] If the template mutation causes a new regression before the existing job starts, apply the precise inverse once by removing only the added key and verify restoration. If a worker remains unhealthy after the key is proven correct, do not guess: preserve the safe changed fingerprint and stop on the exact worker-startup/logging blocker.
- [ ] Add a sanitized recovery record with cause, exact repaired contract, provider/release identities, existing-job functional proof, capacity observation, and zero-at-rest evidence. Never include source/prompt/lyrics/capabilities/auth or raw bodies.

**Dependencies:**

- Checkpoints 1 and 2.

**Verification:**

- Run: `uv run pytest -q tests/test_runpod_template_preflight.py tests/test_runpod_client.py runpod_worker/tests/test_runtime.py`
- Run: `uv run pytest -q`
- Run: `uv run ruff check .`
- Run: `uv run mypy src`
- Run: `git diff --check`
- Live observe: existing Runpod request `COMPLETED`; product job completed with one verified output; root, `/beta/`, and the job route retain expected auth denial unauthenticated; release/services unchanged; endpoint returns to zero active workers and zero queued/in-progress jobs.

**Done When:**

- Production can generate a real song, proven by the already-paid existing request, and the worker scales back to zero.
- The deterministic configuration cause is prevented by a tested preflight and documented without secrets.
- Every completed step is validated against code, tests, and live observable behavior.

**Blockers:**

- Stop immediately on provider identity/config drift, multiple queued jobs, an active unknown worker, template/image mismatch beyond the missing key, or any need to expose secrets.
- Stop and report exact evidence if the corrected template still creates an unhealthy worker; worker logs are then required before any image/model/GPU change. If it instead stays queued with no unhealthy worker, report the separate capacity fingerprint and a cost-bounded candidate list independent of region or GPU generation.

## Final Verification

- Run all checkpoint verification commands from the clean AFlow worktree.
- Confirm the diff contains only the preflight, its tests, narrow operations documentation, DEVLOG entry, sanitized recovery record, and synchronized plan state.
- Confirm one AFlow controller, one original queued Runpod request, one worker at most, no duplicate paid submission, unchanged production release, and zero workers/jobs at rest.
- Request final whole-result review from the configured Sol-medium reviewer before completion.

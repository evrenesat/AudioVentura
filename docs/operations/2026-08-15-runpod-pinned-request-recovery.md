# Runpod pinned-request recovery record — 2026-08-15 (Checkpoint 1)

This is a closed historical incident record, not a current recovery procedure.

Scope: Checkpoint 1 of the AudioVentura usability-recovery plan — inspect or
recover only the already-paid pinned Runpod request. No new, replacement,
retry, duplicate, synthetic, or cover request was authorized or submitted.
This record contains only IDs, booleans, hashes, counts, and bounded fields;
no secrets, capability URLs, source URLs, prompts, or lyrics.

## Identity

| Item | Value |
|---|---|
| Product job | `c63a2910-76a8-4cf3-bf84-05062bc4e68d` (cover, `variation_count=2`) |
| Variation attempt | id `7`, `variation_index=1` (the only attempt for the job) |
| Runpod request | `d5c279ab-9807-4247-8e0c-37409ffbf314-e2` |
| Endpoint | `p1t6aef0dlpz5e` (controller `RUNPOD_ENDPOINT_ID` matches) |
| Template | `37lrt6ox2k` |
| Worker image | `ghcr.io/evrenesat/audioventura-ace-step-worker@sha256:103886d62e65235db96f6f02a4049ffdee74a80e7b1ffee7f055c2e421b17436` (unchanged) |
| Deployed release | `ea668675c014e07ad011a81de7473cd62a469d2f` (unchanged) |
| Network volume | `bgh5crlzt8` |

## Secret-free snapshot (read-only, on `audioventura_beta`, two reads 3 min apart, identical)

- Template env **key set**: exactly `ACE_TRANSFER_ALLOWED_HOST`,
  `ACE_WORKER_CHECKPOINTS_DIR`. Per-value sha256 prefixes (equality checks
  only): `7d300ae1792ebc92`, `c1ff0983d0c16081` — stable across reads.
  `ACE_WORKER_IMAGE_DIGEST` is **absent** (the known defect is present).
- Template semantic fields: `containerDiskInGb=30`, `dockerEntrypoint=null`,
  `dockerStartCmd=null`, `ports=["8888/http","22/tcp"]`,
  `volumeMountPath=/workspace`, `isServerless=true`, readme empty.
- Endpoint semantic fields: `version=8` (unchanged from planning),
  `templateId=37lrt6ox2k`, `networkVolumeId=bgh5crlzt8`, `workersMin=0`,
  **`workersMax=0` (pinned contract is `1` — unexplained semantic drift)**,
  `idleTimeout=30`, `scalerType=REQUEST_COUNT`, `scalerValue=1`,
  `gpuCount=1`, `gpuTypeIds=[NVIDIA L4, NVIDIA A100-SXM4-80GB, NVIDIA RTX PRO
  6000 Blackwell Server Edition, NVIDIA B200, NVIDIA GeForce RTX 4090]`,
  `minCudaVersion=12.8`, `executionTimeoutMs=1200000`, `workers=[]`.
- Health (documented `workers`/`jobs` contract): `workers.idle=0`,
  `workers.running=0`, `jobs.inQueue=0`, `jobs.inProgress=0`; all additional
  reported lifecycle categories zero (`initializing/ready/throttled/unhealthy`);
  `completed=3`, `failed=1`, `retried=0`. No malformed/obsolete shape accepted.
- Request status: `GET /v2/{endpoint}/status/{pinned-request}` → **HTTP 404**
  on both reads. All 6 other historical request IDs also 404 (provider
  retention expiry; all are >=5 days old). No live request exists at the
  provider.
- Services: `audioventura-controller.service`, `audioventura-transfer.service`,
  `nginx.service` all active/running (PIDs bounded, unchanged).
- Controller env key **names** (24 keys) unchanged between reads; values never
  read out or persisted.

## Cross-reference proof (production DB, read-only, WAL)

- Job ↔ attempt ↔ request: exactly one `VariationAttempt` (id 7, variation 1)
  for the job; its `runpod_job_id` and the job's `current_runpod_job_id` both
  equal the pinned request ID — exact in both directions.
- Request-ID claims: exactly one attempt and exactly one job claim the pinned
  ID; no `outputs` row claims it; no other job/attempt claims it.
- Active work: no other queued/in-progress product job or attempt; no other
  queued/in-progress provider request; no unknown worker (endpoint `workers=[]`
  reconciled with health counts).
- Nonce presence: attempt and job both have a persisted submission nonce
  (bounded boolean). Attempt `evidence_status=unavailable`.

## Failure timeline (controller log, bounded lines)

- `2026-08-10T03:20:51Z` submit → `runpod_job_id=d5c279ab-…-e2` (elapsed 306 ms).
- `03:20:53Z`–`05:20:49Z` polled every ~2 s; provider status stayed `IN_QUEUE`
  the entire window (never IN_PROGRESS/COMPLETED/FAILED).
- `05:20:51Z` (exactly 7200 s = `runpod_job_timeout_seconds`) →
  `error_code=controller_task_error` (`ValueError`); job and attempt durably
  FAILED, `evidence_status=unavailable`.
- Consistent with the known defect: the template lacks `ACE_WORKER_IMAGE_DIGEST`
  and `runpod_worker/runtime.py` rejects startup before model initialization.

## Branch decision

**STOP — verify only, no mutation.** The plan gate requires `IN_QUEUE` plus
exact endpoint/template semantics plus exactly one missing digest key to
permit repair. Observed: request missing (404) at the provider; product state
terminal non-success (FAILED); endpoint `workersMax=0` vs pinned `1`
(unexplained semantic drift, not provider-version-generated — version still
8). No repair, no poll (nothing to poll), no inverse, no completion claim.

## Mutation budget

- Forward template update: **0** (never invoked).
- Inverse update: **0**.
- Endpoint PATCH / worker-count change / run / runsync / cancel / retry /
  purge: **none** (none invoked).
- Bounded changed-field list: **empty** — no provider, product, deployment, or
  release change was made.

## Proof gaps (recorded as incomplete, not claimed)

- Pinned request completion: **not achieved** — job is terminal FAILED with
  zero outputs (`outputs` table empty); provider no longer retains the request
  (404); no output artifact or media surface exists to verify.
- Endpoint `workersMax` drift cause: **unknown** — recorded, not repaired
  (worker-count changes are outside Checkpoint 1).
- Provider's current disposition of the pinned request: **unknown** (404;
  retention expiry is the consistent explanation for all historical IDs).

## Follow-up needed (owner decision)

A new paid functional proof requires a separate explicit owner instruction and
plan amendment (this plan authorizes none). Any recovery of this identity must
start from the recorded terminal state; the template digest-key defect remains
present and is the likely cause of the failed request.

## Verification (run from the execution worktree)

- `uv run pytest -q runpod_worker/tests/test_runtime.py tests/test_runpod_client.py` — see results recorded alongside this record.
- `git diff --check` — clean.
- Full-suite baseline comparison: `docs/CHECKPOINT-1-BASELINE.md` records
  `112 passed, 6 failed` (pre-existing Docker-only `lameenc` failures) for the
  full suite; Checkpoint 1's verification subset is the two files above.

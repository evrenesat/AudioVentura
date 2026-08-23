# AudioVentura Runpod status-resilience fix plan

## 1. Objective

Fix the production defect observed on 2026-08-23: one transient HTTP 500 from
Runpod's status endpoint caused a schema-v2 AudioVentura job to become
`controller_task_error` while the provider job remained `IN_QUEUE`.

After this change, a persisted provider job ID remains the authoritative
ownership boundary. AudioVentura must not mark the attempt terminal, revoke its
transfer capabilities, remove its prepared source, or submit a replacement
while that provider job may still execute. It may become terminal only after a
validated provider terminal response, an acknowledged cancellation, or a
post-deadline 404 that proves the provider TTL removed the job.

This is a focused controller reliability change. Do not change the ACE-Step
model, worker image, Hugging Face repository, generation parameters, duration
UI, home-ingest behavior, provider, or deployment topology.

## 2. Confirmed defect and production evidence

- Product job: `56270787-2460-4633-982d-45c9f759f558`.
- Provider job: `0d6f2011-9cc6-4d79-849c-367689ecd8f5-e2`.
- Runpod returned `IN_QUEUE` with zero workers until 2026-08-23 00:08:41 UTC.
- At 00:08:43 UTC, `GET status/<job-id>` returned HTTP 500 once.
- `RunpodClient` raised `RunpodAPIError`, but `_poll_variation()` converted the
  unavailable schema-v2 result into `ValueError`; `process_job()` then called
  `_persist_task_failure()` and committed `controller_task_error`.
- The provider job stayed `IN_QUEUE`, creating an orphan. The local terminal
  transition revoked the cover's signed transfers, so a later worker could
  consume GPU time but could no longer download/upload successfully.
- Existing `tests/test_worker.py::test_schema_v2_status_expiry_does_not_complete_from_output_alone`
  currently asserts this incorrect terminal behavior and must be replaced.

## 3. Fixed design decisions

### Provider error classification

In `src/ace_service/runpod_client.py`, make `RunpodAPIError` carry a bounded
`status_code: int | None` attribute. Transport errors use `None`; non-2xx HTTP
responses use the integer status. Never retain or expose the response body,
headers, credentials, or capability URLs.

Do not add automatic retries inside `_request_json()`: a generic retry there
would also affect non-idempotent submission. The controller owns status and
cancellation retries; `/run` submission retains its existing nonce/no-duplicate
boundary and is never automatically repeated.

Treat both `RunpodAPIError` and `RunpodResponseError` as provider-status
uncertainty when raised by `status()`. Unexpected non-Runpod exceptions remain
controller defects and continue through `_persist_task_failure()`; do not hide
programming, persistence, or invariant errors behind retries.

### Durable lifetime and terminal authority

Use `VariationAttempt.started_at` plus
`ServiceSettings.runpod_job_timeout_seconds` as the controller's total attempt
deadline. `started_at` is already set when the nonce is prepared, so it covers
submission, queueing, initialization, and execution and survives restart.
Never use `updated_at`, because normal progress polling changes it.

No schema migration or new job status is required. Keep uncertain jobs in
their existing `cloud_queued` or `generating` state. The last persisted progress
envelope remains the last confirmed provider/worker evidence; a polling error
must not invent a new lifecycle phase or refresh its `observed_at` timestamp.

Before the deadline:

- a status transport error, HTTP error (including an early 404), or malformed
  provider body leaves job, attempt, evidence, transfers, and source unchanged;
- the same persisted Runpod job ID is polled again;
- no submit or cancel call occurs;
- a later valid response resumes the ordinary state machine.

At or after the deadline:

- first preserve the existing local-output recovery: legacy output may use the
  existing bounded recovery, while schema v2 still requires both persisted,
  validated completion metadata and a validated uploaded output;
- if status is successfully terminal, process that response normally;
- if status is nonterminal or unavailable, call `cancel()` for the exact
  persisted provider job ID;
- only an exact validated `CANCELLED` response allows local failure with stable
  code `runpod_job_timeout` and message
  `Cloud generation exceeded its configured lifetime and was cancelled.`;
- if cancellation is unavailable, malformed, or returns a non-cancelled race
  result, keep the job nonterminal and continue reconciliation with backoff;
- if status returns HTTP 404 at or after the deadline, treat the provider job
  as TTL-expired, record stable code `runpod_job_expired`, and fail locally
  without an additional cancel request. An early 404 remains uncertain.

On acknowledged cancellation or post-deadline expiry, record terminal attempt
evidence as unavailable with `worker_no_evidence`, revoke active capabilities,
commit the attempt and parent failure atomically, then remove a cover source.
Do not clean up before terminal provider evidence exists.

### Poll-error backoff

Add per-process consecutive status-error tracking to `ControllerWorker`, keyed
by product job ID. It affects scheduling only, not durable correctness:

- first failure: retry after `max(poll_interval_seconds, 1)` seconds;
- later consecutive failures: double that delay up to 60 seconds;
- cap the counter so exponentiation is bounded;
- clear the counter and delay override after any contract-valid status response;
- clear entries when a job becomes terminal or the controller stops;
- restart may reset the delay, because the persisted ID, state, start time, and
  deadline—not the in-memory counter—provide correctness.

Modify `_run()` to use the job-specific delay returned/recorded by the polling
path instead of always sleeping the base interval. Do not sleep inside a
database transaction. Preserve single-controller and serialized-job behavior.

### Logging and UI behavior

Log one bounded warning per failed poll containing only product job ID,
operation, exception class, optional HTTP status, consecutive failure count,
and next delay. Log cancellation attempts and whether acknowledgement remains
uncertain. Never log response bodies, source URLs, prompts, lyrics, tokens, or
signed capabilities.

The UI continues to show the last confirmed lifecycle phase and its original
observation time. It must not show `Failed` for transient polling errors. Do not
add a percentage or claim that a model is loading. A separate future change may
add durable provider-connectivity diagnostics; it is outside this fix.

## 4. Implementation sequence

### Checkpoint 1: Encode provider errors without changing behavior

Inspect:

- `src/ace_service/runpod_client.py`
- `tests/test_runpod_client.py`

Implement:

1. Give `RunpodAPIError` a constructor and read-only `status_code` attribute.
2. Populate it for every non-2xx response; leave it `None` for `httpx.HTTPError`.
3. Keep messages bounded and secret-free, and do not attach the response.
4. Preserve current parsing and `RunpodResponseError` behavior.

Verify with mocked HTTP only:

- HTTP 500 and 404 retain their numeric status;
- connection/read failures retain `None`;
- exception text and attributes contain no API key or response body;
- status normalization and cancellation contract tests still pass;
- submission is called once even when its response fails.

Commands:

```bash
uv run pytest -q tests/test_runpod_client.py
uv run ruff check src/ace_service/runpod_client.py tests/test_runpod_client.py
uv run ruff format --check src/ace_service/runpod_client.py tests/test_runpod_client.py
uv run mypy src/ace_service/runpod_client.py
git diff --check
```

### Checkpoint 2: Make status uncertainty nonterminal and bounded

Inspect:

- `src/ace_service/worker.py`
- `src/ace_service/repository.py`
- `src/ace_service/models.py`
- `src/ace_service/state.py`
- `tests/test_worker.py`

Implement:

1. Refactor only the status-exception branch of `_poll_variation()` so existing
   validated-output recovery runs first, then applies the deadline rules above.
2. Add small private helpers for deadline calculation, acknowledged timeout
   cancellation, terminal timeout/expiry persistence, and poll-error backoff.
   Re-read job and attempt after each external call and require the same
   nonterminal attempt and provider job ID before committing.
3. Apply the same deadline cancellation to successful nonterminal
   `IN_QUEUE`/`IN_PROGRESS` responses; a provider that remains healthy but
   never finishes must not bypass the configured total lifetime.
4. Preserve normal terminal-result validation, multi-variation sequencing,
   local-output recovery, attempt evidence, and source cleanup ordering.
5. Catch `asyncio.CancelledError` separately and re-raise it from status and
   cancellation paths.
6. Add bounded per-job backoff to `_run()` without changing durable state.
7. Do not add a database column, migration, status enum, resubmission path, or
   second controller worker.

Replace the incorrect schema-v2 expiry test and add deterministic tests for:

- one HTTP 500 before deadline leaves job/attempt active, preserves transfers
  and source, retains the provider ID, and performs no submit/cancel;
- a valid status after that error resumes processing and clears backoff;
- repeated errors survive a controller restart without resubmission;
- schema-v2 output without completion metadata remains active before deadline;
- validated schema-v2 metadata plus output still completes during status
  unavailability;
- a successful nonterminal status at the deadline triggers one exact cancel;
- acknowledged cancel produces `runpod_job_timeout`, revokes transfers only
  afterward, removes cover source, and never resubmits;
- failed/unknown cancellation keeps the job nonterminal and retries the same ID;
- HTTP 404 before deadline remains active; 404 at/after deadline produces
  `runpod_job_expired` without cancellation;
- a cancel/completion race does not overwrite a completed result or create a
  second provider job;
- an unrelated `ValueError` still becomes `controller_task_error`;
- consecutive error delays cap at 60 seconds and reset after success.

Use direct `process_job()` calls or a controlled fake clock for nonterminal
tests; never call `wait_idle()` on a deliberately persistent retry loop.

Commands:

```bash
uv run pytest -q tests/test_worker.py tests/test_runpod_client.py
uv run ruff check src/ace_service tests/test_worker.py tests/test_runpod_client.py
uv run ruff format --check src/ace_service tests/test_worker.py tests/test_runpod_client.py
uv run mypy src
git diff --check
```

### Checkpoint 3: Document and review the contract

Update only behavior affected by this fix:

- `README.md`: provider polling is retryable; terminal local failure requires
  provider terminal/cancel/expiry evidence.
- `ARCHITECTURE.md` and `docs/ARCHITECTURE.md`: persisted provider ID ownership,
  last-confirmed progress, deadline, and cancellation boundary.
- `docs/OPERATIONS.md`: how to identify and reconcile a provider job whose
  application status is uncertain; require exact ID and provider evidence.
- `DEVLOG.md`: production incident, root cause, and fix decision.

Review the complete diff for these invariants:

- no status-poll failure can call `_persist_task_failure()`;
- no automatic `/run` retry exists;
- no source/capability cleanup precedes provider terminal evidence;
- deadline calculation is UTC and restart-stable;
- no provider body or secret is logged/persisted;
- legacy and schema-v2 completion recovery remain distinct.

Run the repository verification. The private quality fixture's expired
retention deadline is a known unrelated failure source; compare against current
baseline and require every focused/new test to pass. Do not weaken or skip a
new test to accommodate that fixture.

```bash
uv run pytest -q tests runpod_worker/tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src runpod_worker
(cd home_ingest && uv run pytest -q)
(cd home_ingest && uv run ruff check .)
(cd home_ingest && uv run ruff format --check .)
(cd home_ingest && uv run mypy src)
git diff --check
```

Commit the product change using the repository convention, for example:

```text
fix/runpod-status: retain jobs across transient polling failures
```

### Checkpoint 4: Reconcile current state and deploy through Evreniops

This checkpoint changes external state. Before execution, confirm authorization
to cancel the exact orphan if it is still nonterminal and to deploy the reviewed
product commit. Do not submit a replacement job during reconciliation.

1. Read production release identity, services, database job/attempt state,
   exact provider status for
   `0d6f2011-9cc6-4d79-849c-367689ecd8f5-e2`, complete endpoint health, v2
   worker inventory, and `workersMin`.
2. If that exact provider job is still `IN_QUEUE`/`IN_PROGRESS`, cancel only
   that ID and require a validated `CANCELLED` response. If it is already
   terminal or returns the documented post-TTL 404, record that evidence and
   do not cancel another ID.
3. Require `workersMin=0`, zero queued/in-progress jobs, and zero workers. Use
   the existing fingerprint-gated Evreniops helper if restoration is needed;
   do not directly broaden GPU types, change model references, attach a volume,
   or delete storage in this fix.
4. In `/root/code/evreniops-audioventura-deploy`, change only the exact product
   commit pins in:
   - `infra/ansible/playbooks/deploy_audioventura.yml`
   - `infra/services/audioventura/assert_contract.py`
   - `infra/services/audioventura/README.md`
5. Run local contract tests and Ansible syntax check, then the documented
   `--check --diff`. Stop on provider work, an unexpected database migration,
   secret output, or unrelated infrastructure change.
6. Deploy once, record the emitted rollback snapshot, and run
   `audioventura_mode=verify`. Verify authenticated UI/status reads and the
   exact deployed Git identity without submitting paid work.
7. Re-read provider health and require zero-at-rest. Roll back only with the
   snapshot emitted by this deployment if service, schema, or authenticated
   UI verification fails.

Evreniops local verification:

```bash
ansible-playbook --syntax-check \
  -i infra/ansible/inventory/audioventura.yml \
  infra/ansible/playbooks/deploy_audioventura.yml
python3 infra/services/audioventura/assert_contract.py
python3 infra/services/audioventura/test_configure_runpod.py
git diff --check
```

Do not run a paid smoke solely to prove HTTP-500 handling; mocked fault tests
are the acceptance evidence for that branch. A later capacity/provider smoke
may reuse the existing paid E2E harness and remaining owner-authorized test
budget, but it is a separate operation and must start from provider zero-at-rest.

## 5. Acceptance criteria

1. A single or repeated Runpod status 500 cannot make an otherwise active job
   `failed` or revoke/remove its transfer/source state.
2. Restart polls the same persisted Runpod ID and never submits another job.
3. The UI remains nonterminal and shows only the last confirmed phase/time.
4. A configured lifetime overrun initiates exact-ID cancellation.
5. Local terminal timeout occurs only after cancellation acknowledgement;
   cancellation uncertainty remains nonterminal and recoverable.
6. A post-deadline 404 is handled as provider TTL expiry; an early 404 is not.
7. Valid completion evidence wins over status unavailability exactly as before,
   with schema-v2 metadata requirements preserved.
8. Focused tests, static checks, deployment contract checks, and authenticated
   verification pass; no new provider submission is required.
9. Production ends with the reviewed commit active, both services healthy,
   the known orphan reconciled, and Runpod at zero workers/work.

## 6. Stop conditions

Stop and report rather than improvising if:

- Runpod reports the known job running or completed with new output while its
  AudioVentura row is already terminal;
- cancellation returns an undocumented body/status or cannot be confirmed;
- another unknown queued/running provider job or worker exists;
- implementation requires automatic submission retry or loses the original
  persisted provider ID;
- a migration appears necessary despite the no-schema design above;
- deployment preflight finds unrelated database, release, endpoint, model,
  image, volume, or secret drift;
- full-suite failures exceed the documented expired-fixture baseline.

## 7. LLM-agent work estimate

This is one medium, focused implementation task plus one separately gated
operations task:

- controller/client change and focused tests: approximately 2–4 agent
  implementation/review iterations;
- documentation and full verification: approximately 1 additional iteration;
- state reconciliation and deployment: one short operations iteration after
  explicit authorization and provider-zero evidence.

The main uncertainty is provider cancellation/404 race behavior, not code
volume. The plan removes that uncertainty from local correctness by refusing a
terminal transition until provider evidence is validated.

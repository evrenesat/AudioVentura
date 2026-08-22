# AudioVentura MVP Usability Recovery

## Status

Operational recovery completed on 2026-08-22 through Checkpoint 4. The reviewed
release is live, the Runpod boundary is generation-capable and scale-to-zero,
and every non-paying acceptance check passed. A newly paid original/cover smoke
was not submitted and remains a separately authorized acceptance action, not an
unfinished implementation step.

This is the only authoritative plan for this recovery in this worktree. A
legacy file named
`plans/backups/audioventura-runpod-usability-recovery-aflow-plan-v01.md` exists
only as a staged change in the older dirty checkout; it is history, not an input
or dependency. Do not execute or import v01.

This file records decisions only. It does not authorize implementation, provider mutation, deployment, paid generation, commit, or push.

**Paid-action boundary:** no new, replacement, retry, duplicate, synthetic, original, or cover Runpod request is authorized by this plan. The only paid request that may be inspected or allowed to continue is pinned request `d5c279ab-9807-4247-8e0c-37409ffbf314-e2`, linked to product job `c63a2910-76a8-4cf3-bf84-05062bc4e68d`. Do not invoke any generation submission endpoint, including through a browser or smoke test. If that request cannot prove the required live behavior, record the proof gap and stop. A later paid proof requires a separate explicit owner instruction and plan amendment.

## 1. MVP Objective

An authenticated user on `player.evren.io` can start an original song or YouTube cover, see honest progress, and play or download the result. Existing product features remain unless this plan explicitly says otherwise.

Guard/SRE owns keeping that conversation-to-generation pipeline usable: observe, test, identify material failures, create focused fix plans, guard approved execution, deploy approved fixes, and live-verify. It does not own new product features, generalized infrastructure, or speculative hardening.

## 2. Owner Decisions

1. Build the smallest usable recovery. Follow KISS and lean development.
2. Do not remove existing features merely because they are outside the active MVP.
3. Preserve inactive feature code and data. Comment out only executable or normal-flow entrypoints and add a nearby `TODO` or `FIXME` explaining when to re-enable them.
4. Exception: billing-sync may be removed completely: CLI, client, sync-only config, tests, and operational docs. Do not drop historical database tables/columns.
5. Cost information is approximate and informational only. It must never approve, reject, delay, retry, or cancel generation.
6. Use `USD 0.50/GPU-hour`.
7. Keep separate original and cover histories. Use the latest three completed individual attempts of the matching kind.
8. Show those three individual costs and their average. Estimate a request as `matching average x variation count`.
9. With no matching history, clearly label and use a 60-second seed: `0.50 x 60 / 3600 = USD 0.0083` per variation. Do not persist synthetic history or build calibration machinery.
10. Preserve quality-campaign code/data, but comment out its executable entrypoint and normal submission-gate entrypoint. Add a `TODO`: re-enable after ordinary original and cover generation is stable.
11. Default both forms to one variation while preserving explicit 1-4.
12. A cover uses one initial submit with rights confirmation. After existing validation/extraction, continue through the existing serialized path without a second confirmation click.
13. Preserve authentication, CSRF, transfer security, source limits, projects/continuation, status/output, one-at-a-time Runpod execution, and scale-to-zero.
14. Review only concrete, reachable, material regressions. Speculative reviewer suggestions do not expand scope.
15. The existing pinned Runpod request is the only authorized paid functional proof. Never cancel, retry, replace, duplicate, or supplement it in this plan.

## 3. Now / Later

### Now

1. Revalidate and repair the known worker-template contract only if its fingerprint still matches.
2. Prove the existing queued request completes without replacement.
3. Remove billing-sync.
4. Quarantine quality-campaign entrypoints while preserving implementation.
5. Use the simple informational cost estimate.
6. Default to one variation and remove the second cover confirmation for new submissions.
7. Deploy approved work and verify non-submitting authenticated surfaces on `player.evren.io`; use only the pinned existing request for paid live evidence.
8. Return Runpod to zero workers and zero queued/in-progress work at rest.

### Later

1. Re-enable quality campaigns after ordinary generation is stable.
2. Exact billing, live rate catalogs, calibration, ledgers, budgets, cost ceilings, and admin cost settings.
3. Generalized Runpod preflight/recovery, observability platforms, dashboards, or retry orchestration.
4. GPU-pool, image, model, schema, permission-framework, or queue changes not proven necessary by the MVP.
5. UI redesigns and new generation features.
6. Any new paid Runpod smoke or functional request.

Later work requires a separate owner decision and plan.

## 4. Git and Live Identity

- Plan Branch: `aflow-audioventura-runpod-usability-recovery-aflow-plan-20260815-212058`
- Pre-Handoff Base HEAD: `82e5d681712d7040b5e132455f5f9637ffdd27c8`
- Consolidation started at: `82e5d68`
- Execution worktree: `/root/code/worktrees/aflow-audioventura-runpod-usability-recovery-aflow-plan-20260815-212058`
- Do not implement from the older dirty checkout `/root/code/audioventura-usability-recovery-20260810`.
- Do not modify dirty `/root/code/audioventura`.
- Operations checkout: `/root/code/evreniops-audioventura-deploy`
- Planning-time operations HEAD: `336c87901f53c619c34a044bc43ed19412c07d91`; re-read and refuse unexplained drift before use.
- Last Reviewed HEAD: reviewed through `cp3 v01`
- Review Log: `2026-08-15` plan-only audits tightened paid-action, identity, state-branching, template-update/rollback budgets, cover crash recovery, deployment quiescence, verification, checkout, and deployment constraints; no code, live system, provider, commit, or push action was performed. `2026-08-15` Checkpoint 1 review approved the bounded no-mutation stop: the pinned provider request is no longer retained, the linked product job/attempt are terminal failed with no output, endpoint `workersMax=0` is unexplained drift, focused tests pass, and no paid replacement or recovery success is claimed. `2026-08-15` `cp1 v02` reconciled the four conditional stop-branch outcomes with AFlow's completed-checkpoint invariant; no recovery action or success is claimed. `2026-08-15` Checkpoint 2 worktree-fallback review left the checkpoint unapproved: request totals are computed only for initial default GETs, so explicit variation changes show a stale total and continuation/validation forms omit the estimate, while four-decimal labels truncate instead of applying the required final half-up rounding. The focused repair was routed to `audioventura-runpod-usability-recovery-aflow-plan-cp03-v01.md`. The 109-test non-expired focused slice, Ruff check, mypy, and diff check passed; the exact quality suite reproduced only the pre-existing private-fixture retention-deadline failures, and the full format check reproduced pre-existing HEAD formatting drift plus one touched test line. No live, provider, deployment, paid Runpod, commit, or push action was performed. `2026-08-15` Checkpoint 2 worktree-fallback re-review approved `cp2 v01`: selector-bound request totals now cover initial, continuation, and validation paths; exact raw-rational labels apply one final four-decimal half-up boundary; billing-sync stays removed; quality entrypoints stay quarantined; and estimates remain read-only and non-gating. The 115-test focused slice passed. The full current suite had 341 passes and the same 96 private-fixture retention-deadline failures reproduced on pristine `cp1 v02`; root Ruff, format, and mypy checks and all home-ingest checks passed. No live, provider, deployment, or paid Runpod action was performed. `2026-08-15` Checkpoint 3 worktree-fallback review approved `cp3 v01`: both forms default to one while preserving explicit 1-4 and continuation values; new covers durably commit finalized source metadata and confirmed staging before the existing serialized nonce/submission path; legacy awaiting-confirmation rows remain gated; and crash/restart paths fail closed without duplicate submission. The 155-test focused slice and 337-test non-quality suite passed. The full current suite had 349 passes and the same 96 private-fixture retention-deadline failures; root Ruff, format, and mypy checks and all home-ingest checks passed. No live, provider, deployment, or paid Runpod action was performed.

Planning-time incident evidence, which must be re-read before any action:

- Endpoint `p1t6aef0dlpz5e`, observed version `8`; template `37lrt6ox2k`
- Image `ghcr.io/evrenesat/audioventura-ace-step-worker@sha256:103886d62e65235db96f6f02a4049ffdee74a80e7b1ffee7f055c2e421b17436`
- Deployed product release `ea668675c014e07ad011a81de7473cd62a469d2f`
- Network volume `bgh5crlzt8`; `workersMin=0`; `workersMax=1`; `idleTimeout=30`; `scalerType=REQUEST_COUNT`; `scalerValue=1`; `gpuCount=1`; `minCudaVersion=12.8`
- Expected pre-repair template environment key set: `ACE_TRANSFER_ALLOWED_HOST` and `ACE_WORKER_CHECKPOINTS_DIR` only. The only allowed post-repair addition is `ACE_WORKER_IMAGE_DIGEST`; preserve the two existing values without recording them.
- Product job `c63a2910-76a8-4cf3-bf84-05062bc4e68d`
- Runpod request `d5c279ab-9807-4247-8e0c-37409ffbf314-e2`
- Observed defect: immutable template image lacked `ACE_WORKER_IMAGE_DIGEST`; startup rejects this before model initialization.

The job, its single relevant variation attempt, and the Runpod request ID must cross-reference one another before any recovery. Endpoint version `8` is planning-time evidence, not permission to accept semantic drift: record provider-generated version changes, but stop unless every semantic field still matches the pinned contract.

Load protected production configuration only on `audioventura_beta` through the pinned inventory. Use authorization headers, never query parameters. Never enable shell tracing or expose API keys, Basic Auth, full environments, capability/source URLs, prompts, lyrics, or raw provider bodies.

## 5. Architecture Boundaries

1. Controller, home-ingest, transfer, and Runpod worker stay separate.
2. Controller does not invoke media tools or inference directly.
3. The existing database is durable state. Add no parallel queue, ledger, scheduler, or campaign engine.
4. Reuse existing jobs, attempts, serialization, nonce/idempotence, and status paths. Preserve historical submission-quote schema/data, but do not keep quote capture as a generation prerequisite or invent new quote persistence for the simplified estimate.
5. Cost display reads completed attempt duration from the local service database and has no control-flow authority. The service has shared Basic Auth and no user entity, so history is service-wide; do not invent an account/user partition.
6. Quarantined quality code stays importable/testable but unreachable from normal submission and direct module execution.
7. No feature deletion except billing-sync.
8. No migration or dependency unless existing code cannot meet the requirement. Stop and show the exact gap before adding one.
9. Preserve min workers 0, max 1, idle timeout 30, request-count scaling/value 1, one GPU, CUDA floor 12.8, current GPU allow-list, network volume `bgh5crlzt8`, immutable image, and transfer restrictions.
10. No endpoint, template, image, GPU-pool, volume, deployment-repository, schema, controller/transfer service, nginx, or database mutation is allowed in Checkpoint 1 except the one exact template environment-key addition and its separately bounded exact inverse described there.
11. No checkpoint may submit a new Runpod request. Live POSTs that could enqueue generation are forbidden even when authenticated and even if described as smoke tests.
12. A read-only observation may establish only what it directly proves. It must not convert an unknown, missing, stale, or contradictory provider/product field into a default or inferred success.

## 6. Checkpoints

### [x] Checkpoint 1: Recover the already-paid request

**Reviewed outcome:** The identity-gated inspection reached the required stop branch before mutation. The pinned request cannot be recovered under this authorization because it is absent from the provider, its linked product state is terminal failed with no output, and endpoint semantics have drifted. The conditional repair, polling, and output-verification branches below were therefore closed without execution; no recovery claim is made.

**Objective:** Inspect or recover only the already-paid pinned request, using at most one exact forward template repair and, only under the stated rollback gate, one exact inverse.

**Inspect:** `AGENTS.md`, `runpod_worker/AGENTS.md`, `runpod_worker/runtime.py`, `src/ace_service/{models,repository,runpod_client}.py`, `docs/{RUNPOD,OPERATIONS}.md`, `/root/code/evreniops-audioventura-deploy/infra/services/audioventura/AGENTS.md`, `/root/code/evreniops-audioventura-deploy/infra/ansible/inventory/audioventura.yml`, and bounded live endpoint/template/health/request/job/release/service state.

**Steps:**

- [x] From the execution worktree, prove the branch/HEAD/status and preserve all pre-existing changes. From the operations checkout, prove its HEAD/status, read its nearest guidance, and use only the pinned inventory target.
- [x] Record a secret-free snapshot of endpoint/template IDs, provider version, immutable image digest, environment key names, scaler/worker/GPU/CUDA/volume settings, queue counts, request state, linked product job/attempt state, deployed release, and service state. Compare protected values only in memory and record booleans or hashes, never raw values/bodies.
- [x] Prove product job `c63a2910-76a8-4cf3-bf84-05062bc4e68d`, exactly one matching `VariationAttempt`, and Runpod request `d5c279ab-9807-4247-8e0c-37409ffbf314-e2` cross-reference exactly in both directions. Record the matching attempt ID/index, persisted request ID and nonce presence as bounded values; inventory every attempt for the job; and prove no other job/attempt claims the pinned request ID. Zero or multiple matches, a conflicting parent/current request ID, or an unexpected attempt is a hard stop. Prove no other queued/in-progress provider request and no unknown worker exists.
- [x] Branch on the re-read state without weakening the identity gate: `COMPLETED` means verify only; a recognized nonterminal progressing state means poll only; an exact expected digest key means poll only; and only `IN_QUEUE` plus exact endpoint/template semantics plus one missing digest key permits repair. Any terminal non-success, missing/unknown request state, mismatched or duplicate digest key, malformed environment, identity/linkage drift, unexplained semantic drift, or extra work forbids mutation and completion claims.
- [x] Close the conditional repair-fingerprint branch without constructing a write payload: the request is missing, product state is terminal failed, and endpoint semantics drifted, so the hard-stop gate forbids repair.
- [x] Close the conditional template-update branch without invoking it. Forward and inverse update counts are both zero; no endpoint or worker-count mutation occurred.
- [x] Close the conditional polling branch without polling: the pinned request returned HTTP 404 on both bounded reads and there is no recognized nonterminal provider request to observe. No run, runsync, retry, cancel, purge, or replacement endpoint was called.
- [x] Record output verification as unachieved: the linked product job and attempt are terminal failed, the outputs table is empty, and no authenticated metadata/media output exists to verify. This is a proof gap and terminal blocker, not a completion claim.
- [x] After the 30-second idle timeout, verify the endpoint reports zero queued/in-progress work and zero workers in every reported lifecycle category, including unhealthy/unknown categories; reconcile category counts with any returned worker inventory. Re-read the release and controller/transfer/nginx service state, and prove the before/after request-ID inventory contains no new ID.
- [x] The mutation budget is one forward template update plus, only when the forward update is definitely known to have applied, at most one exact inverse update. Use the inverse only if the added key causes a new attributable regression before the pinned request starts; remove only that key and prove the original fingerprint is restored. Never inverse-update an uncertain write or a running/completed request, and never retry either operation. Any further repair requires a focused plan.
- [x] Write one sanitized `docs/operations/` recovery record. Do not add generalized preflight/tooling or modify the deployment repository without a separate, evidence-backed plan amendment.

**Verification:**

- `uv run pytest -q runpod_worker/tests/test_runtime.py tests/test_runpod_client.py`
- `git diff --check`
- Existing request is `COMPLETED`; output is usable; release/services are unchanged; Runpod is zero at rest across every reported queue/worker category; no new request ID exists; every bounded changed field is listed. If any item is unprovable, record it as incomplete rather than claiming recovery.

**Stop:** identity/linkage drift, multiple or unknown jobs/workers, terminal non-success of the pinned request, a new paid request requirement, secret disclosure, inability to classify an uncertain template update, or failure beyond the known contract. Leave the pinned request untouched and record a focused blocker; do not guess at endpoint/image/model/GPU/queue changes.

### [x] Checkpoint 2: Simplify active boundaries and costs

**Objective:** Remove billing-sync, quarantine quality entrypoints, and make costs a read-only calculation.

**Inspect:** `src/ace_service/{__main__,app,billing_client,campaign,config,costs,models,quality_eval,quality_profiles,repository,web,worker}.py`, related billing/cost/quality/web/worker tests, templates that render estimates, `README.md`, `docs/{ARCHITECTURE,OPERATIONS,QUALITY-EVALUATION}.md`, and `DEVLOG.md`. Run `rg -n "billing|quality.*campaign|quality.*gate|CampaignStore|cost|quote|execution_ms" src tests docs README.md DEVLOG.md`.

**Steps:**

- [x] Remove only billing-sync executable surface: the `billing-sync` parser/dispatch/helper, `src/ace_service/billing_client.py`, sync-only settings, its focused tests, dead imports, and operational instructions. Add a regression that CLI help has no billing-sync command. Preserve all historical database tables/columns/rows and any cost primitive still used by generation history.
- [x] Disable the ordinary submission maintenance-gate calls in `src/ace_service/web.py` and any separately discovered executable campaign runner/entrypoint by commenting only those calls/entrypoints with a nearby `TODO` naming the re-enable condition. Preserve `src/ace_service/campaign.py`, quality evaluators/profiles, campaign data, and unit-testable code. Do not weaken auth, CSRF, or ordinary enqueue serialization.
- [x] Query service-wide persisted history by joining `VariationAttempt` to `Job`. For the matching `JobType`, include only attempts with `status=completed`, a non-null `completed_at`, and a non-null non-negative `execution_ms`; order by `completed_at DESC, VariationAttempt.id DESC`; limit to three. One variation attempt is one sample. Do not use failed/pending attempts, job-level elapsed time, persisted historical rates/estimates, or one job as one aggregate sample.
- [x] Recompute each displayed sample at the fixed exact rate `500_000 micro-USD/GPU-hour` using integer/rational arithmetic equivalent to `execution_ms / 3_600_000 x USD 0.50`; binary float must not enter cost arithmetic. Show all available samples when there are one to three. Compute the average from the sum of the raw sample numerators divided by the sample count, not from already rounded display strings, and never blend the seed into partial history.
- [x] With no matching samples, use exactly `60_000 ms` as the unrounded seed and label its displayed per-variation value `USD 0.0083`; never persist it as history. Compute request estimate from the unrounded average/seed times variation count, then round once with the existing centralized half-up/display convention. Round individual sample and average values only at their final display boundary; do not feed those rounded values back into the request estimate.
- [x] Compute the estimate on read and label every sample, average, and request estimate approximate/informational. Historical `submission_quotes`, rate catalogs, calibrations, billing reconciliation, and attempt evidence remain inert/readable data but have no active admission or generation-control role. Do not create new quote/calibration/rate records for this display.
- [x] Remove quote capture and quality/cost gate calls from normal submission control flow. Estimation/query/rendering failure must yield a bounded `estimate unavailable` display (or omit only the estimate) while generation continues unchanged; it must never roll back, delay, reject, retry, or cancel a job.
- [x] Disconnect live-rate, calibration, billing-reconciliation, cost-ceiling, and quality-gate calls/imports/config requirements from normal submission only after `rg` identifies every caller. Preserve their implementation and historical data unless an item is proved billing-sync-only; do not delete code still needed to read existing records.
- [x] Update concise docs for active and quarantined behavior.

**Verification:**

- Test separate original/cover histories, deterministic tie ordering, completed-only filtering, null-duration filtering, 1/2/3/more samples, individual costs, average-without-seed, no-history seed, multiply-before-rounding, and database/render failure not blocking submission.
- Prove normal submissions cannot call the campaign maintenance gate and no executable campaign runner remains reachable; quality implementation unit tests still pass.
- Prove billing-sync is absent from CLI help, executable code, settings, and operational docs while historical migration/model coverage still passes.
- `uv run pytest -q tests/test_costs.py tests/test_quality_campaign.py tests/test_quality_eval.py tests/test_web.py tests/test_worker.py`
- `uv run ruff check src/ace_service tests`
- `uv run ruff format --check src/ace_service tests`
- `uv run mypy src`
- `git diff --check`
- Material review only for concrete regressions.

**Stop:** migration, new service/dependency, background estimator, live-rate fetch, calibration, budget gate, generalized refactor, quality deletion, historical schema/data loss, or any estimate/quote/quality failure that can alter generation control flow.

### [x] Checkpoint 3: Make normal generation one-submit

**Objective:** Preserve choices while minimizing the ordinary original/cover path.

**Inspect:** `src/ace_service/{schemas,web,worker,cover,repository,models}.py`, `src/ace_service/templates/{original_form,cover_form,job_detail}.html`, and `tests/{test_web,test_cover_workflow,test_original_workflow,test_worker}.py`. Trace `create_cover -> _prepare_cover -> confirm_cover_job -> _submit_variation` and startup recovery before editing.

**Steps:**

- [x] Set both request-model defaults, server-side missing-form defaults, and HTML selected values to one. Preserve validation and selectors for explicit 1-4, submitted values after validation errors, and continuation/edit values from existing jobs.
- [x] Keep the initial rights checkbox as the only user authorization for a new cover. Preserve authentication, CSRF, allow-listed YouTube identity, canonical URL checks, duration/byte limits, home-ingest isolation, SFTP/capability boundaries, and source hash/metadata validation.
- [x] After successful extraction, use one database transaction to persist canonical source metadata, checksum/size, finalized normalized duration, transition `INGESTING -> STAGING`, and reuse `confirm_cover_job` so existing `cover_staging.status=confirmed` plus `confirmed_at` durably consumes the already-persisted initial rights confirmation. Require a non-null `rights_confirmation_at`. Commit this entire state before any external Runpod submission, then continue directly through the existing serialized `_submit_variation` path without constructing a web request or capturing a legacy submission quote. Do not add a column, migration, new staging status, or parallel flow marker.
- [x] Reuse the existing per-variation nonce, durable attempt, and uncertain-submission recovery. One initial form POST creates one product job; each variation crosses the nonce boundary once and can receive at most one provider request ID. Never retry a nonce-only uncertain submission.
- [x] Keep the authenticated confirmation endpoint only for legacy rows whose existing durable state is exactly `JobStatus.STAGING` plus `cover_staging.status=awaiting_confirmation`; retain auth, CSRF, single-use transition, and cancel behavior for those rows. New rows must never commit that state or render/require the route. Startup may enqueue a staged cover only when its durable state is already `cover_staging.status=confirmed`; it must leave legacy `awaiting_confirmation` rows untouched.
- [x] Define crash behavior at each boundary: before the staging/confirmation transaction commits, no Runpod submission is allowed and an unrecoverable `INGESTING` row follows the existing bounded fail-closed recovery without repeating home extraction; after that commit but before Runpod submit, startup resumes the confirmed staged row; after the nonce commit, uncertain submission recovery never resubmits. Do not infer source metadata from a file after a rollback, and do not auto-confirm an awaiting legacy row.
- [x] On extraction/validation/persistence failure, submit nothing and expose the existing bounded failure. On enqueue/submission uncertainty, preserve the existing fail-closed state and do not auto-retry. If the all-or-nothing staging/confirmation transaction cannot be implemented with the existing schema and state machine, stop with the exact gap rather than adding a migration.
- [x] Keep detected duration, preparation/generation state, attempts, and outputs honest in HTML/JSON. Remove new-flow confirm/cancel UI only after the durable new-flow state exists; update `README.md`, `docs/{ARCHITECTURE,OPERATIONS}.md`, and `DEVLOG.md` where behavior changes.

**Verification:**

- With fake home-ingest and fake Runpod transports only, test defaults, explicit 2-4, validation-value retention, one-submit success, the exact atomic confirmed-staging state, crash before and after that commit, extraction failure, persistence failure, restart/duplicate/nonce uncertainty, legacy awaiting-confirmation compatibility, rights/source limits, status honesty, and projects/continuation. Assert no fake submit occurs before the confirmed-staging commit and no path submits the same nonce twice. No test may contact Runpod or submit a paid request.
- `uv run pytest -q tests/test_web.py tests/test_cover_workflow.py tests/test_original_workflow.py tests/test_worker.py tests/test_costs.py`
- Repeat Checkpoint 2 static checks and material review.

**Stop:** any bypass of durable staging, commit-before-external-call, serialization, auth, CSRF, rights, source validation, transfer isolation, or idempotence; inability to distinguish new auto-approved rows from legacy staging rows; or deletion/reinterpretation of legacy data.

### [x] Checkpoint 4: Deploy and verify the usable pipeline

**Objective:** Deploy the approved recovery, verify non-submitting live surfaces on `player.evren.io`, and use only the pinned request's existing artifact for paid live evidence.

**Executed outcome (2026-08-22):** The current owner instruction authorized
deployment and bringing the service to a usable state. Fresh preflight proved
the original pinned request had no artifact, but also found the endpoint safely
idle and generation-disabled by `workersMax=0` plus the known missing immutable
worker digest. A focused evreniops workflow added only the exact digest and
restored `workersMax=1` with `workersMin=0`; it submitted no job. Reviewed
product commit `107d35e0cfd2a9ccc878038b9860b8a4f391c3f5` was then deployed
transactionally with rollback snapshot
`/opt/audioventura/rollback/20260822T172446Z`. Authenticated GET-only UI,
existing output media/download, all internal readiness components, schema v6
integrity, provider zero-at-rest, and repeated no-change checks passed. A newly
paid original/cover generation remains intentionally unexecuted and unclaimed.

**Preconditions:** Checkpoints 1-3 and the full handoff verification below pass; no unresolved material finding; an exact reviewed product commit exists; the operations checkout is clean at the revalidated expected HEAD; the previous release and rollback identity are recorded; deployment itself is separately authorized. Establish an owner-confirmed exclusive no-submission window covering the final state read, deployment, verification, and rollback decision. Immediately before deployment, read the production service database on the pinned target and prove it has no unapproved job/attempt in a state that startup can enqueue or poll; also prove Runpod has no request or worker beyond the completed pinned identity. If the window or either proof is unavailable, do not deploy. No paid smoke is authorized or permitted.

**Steps:**

- [x] In `/root/code/evreniops-audioventura-deploy`, read the nearest guidance and deployment README. The current workflow hard-pins the old product SHA in `infra/ansible/playbooks/deploy_audioventura.yml`, `infra/services/audioventura/assert_contract.py`, and `infra/services/audioventura/README.md`; make one separately reviewed operations diff that changes only those release-identity references to the exact reviewed product commit. Do not weaken the equality assertions or use an extra variable to bypass them.
- [x] Run, from the operations checkout, `ansible-playbook --syntax-check -i infra/ansible/inventory/audioventura.yml infra/ansible/playbooks/deploy_audioventura.yml`, `python3 infra/services/audioventura/assert_contract.py`, and `git diff --check`. Require success and an identity-only operations diff.
- [x] Re-read production release/services, every nonterminal product job/attempt, the complete bounded provider request-ID inventory, and Runpod zero-at-rest state within the exclusive window. Run the documented Ansible `--check --diff` command with the protected become-password file. Inspect its bounded diff; stop on secret output, unexpected host/file/service/schema changes, any submit-capable product state, or any generation activity.
- [x] After separate deployment approval, run the documented deployment command once. Record the exact new release, previous release, emitted rollback snapshot, migration status, service/nginx checks, and bounded changed resources. Do not mutate provider configuration during deployment.
- [x] Run the documented `audioventura_mode=verify` playbook. Verify unauthenticated denial and authenticated GETs for root, `/beta/`, original/cover forms, projects, the pinned completed job status/detail, and its existing media/download surfaces without saving media in the repository.
- [x] From GET responses only, verify defaults 1 and choices 1-4; separate last-three costs, averages, multiplication, labels; honest statuses; projects/continuation; and retained auth/CSRF/security headers. Use deployed-code identity plus offline tests/static inspection—not a newly created cover—to prove that new-flow detail pages cannot render the second-confirmation UI and quality entrypoints are quarantined. Verify CLI help has no billing-sync.
- [x] Do not POST either generation form, the legacy cover-confirm route, a continuation route, or any API that can enqueue/retry/cancel Runpod work. Live auto-submit behavior for a newly created original/cover remains intentionally unproven under this authorization; record that limitation instead of claiming it passed.
- [x] Re-read the pinned request, complete bounded request-ID inventory, endpoint, and every nonterminal product job/attempt. Verify no new request ID or submit-capable product state appeared and Runpod remains at zero across all queue and worker categories. End the exclusive window only after this proof or after verified rollback.
- [x] On a material deployment regression, use only the rollback snapshot emitted by this deployment and the documented rollback command, then verify restoration. Create a focused fix plan; add no feature or infrastructure.

**Verification:** exact project and operations commands above; bounded GET-only live evidence; final release/service/provider re-read; explicit request-ID inventory showing no new request; final material review.

**Done:** the reviewed release is deployed; non-submitting authenticated surfaces are usable; defaults/estimates are live-proven; the one-submit behavior and new-flow confirmation-UI absence are offline-proven against the exact deployed commit; billing-sync is gone; quality is quarantined; the pinned existing output remains usable; no other feature is deleted; production is healthy; no new request ID exists; and Runpod is zero at rest. New original/cover paid execution is explicitly unauthorized and not part of live proof.

## 7. Required Handoff Verification

Run from the execution worktree after each implementation checkpoint that changes product code and again before review. All commands must exit zero; compare test counts/warnings with `docs/CHECKPOINT-1-BASELINE.md` and explain any difference rather than silently accepting it.

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

Then inspect `git status --short`, the complete scoped diff, deleted files, migration/schema changes, generation-call sites, and all new/changed tests. Require no `.env`, database, generated audio, credentials, raw provider bodies, or unrelated pre-existing changes in the diff. Review only concrete reachable material defects.

## 8. Final Acceptance

1. [ ] Known contract repaired only after exact revalidation.
2. [ ] Existing request completed or exact material blocker recorded; no cancel, retry, replacement, duplicate, or new paid request.
3. [ ] Billing-sync fully removed without dropping historical database data.
4. [ ] Quality implementation remains; executable/normal-flow entrypoints are commented with `TODO`.
5. [ ] Original and cover each show latest three costs and average at `USD 0.50/GPU-hour`.
6. [ ] Estimates are approximate and cannot control generation.
7. [ ] Both forms default to one and allow 1-4.
8. [ ] New covers need one rights-confirmed submit and no second confirmation; the exact deployed code has offline crash-boundary proof, while new-cover live execution remains unauthorized/unproven.
9. [ ] Auth, CSRF, transfer, source limits, serialization, projects/continuation, status, and media retain test coverage and GET-only live verification where possible.
10. [ ] No speculative framework, service, queue, ledger, dashboard, retry system, or feature entered recovery.
11. [ ] Review is limited to material, reachable defects.
12. [ ] The pinned existing output and non-submitting live surfaces are proven; new-generation live proof is recorded as unauthorized/unproven; Runpod returns to zero at rest with no new request ID.

## 9. Handoff Rule

Ask instead of guessing whenever live identity, behavior, paid action, or scope differs. A discovered issue is not permission to fix adjacent code. Record it numerically, explain its MVP effect, and stop or create a narrow follow-up plan after owner approval.

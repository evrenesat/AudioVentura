# AudioVentura Project Continuation Usability

## Summary

Turn the current create-once job flow into a small project workspace. Every generation belongs to one project, an existing result can prefill the matching form, and submitting that form creates a new project version through the unchanged generation pipeline. Add a simple project page for revision history and output comparison. Keep the design server-rendered, authenticated, CSRF-protected, and deliberately free of automatic retries, pipeline changes, or speculative workflow features.

## Git Tracking

- Plan Branch: `aflow-audioventura-project-continuation-usability-aflow--20260809-122707`
- Pre-Handoff Base HEAD: `c1f795811e652396259a4d147e6348ca84ab5641`
- Last Reviewed HEAD: `fe88eba11da76048736ce0ada584a02b70893fc7`
- Review Log: Checkpoint 1 approved through `cp1 v01` from the current-worktree fallback because no checkpoint commit boundary existed before review. The required 48-test migration/persistence suite, scoped Ruff check, and diff check passed. The full 371-test suite had one unrelated pre-existing date-sensitive cost-rate failure that reproduced in isolation; 370 tests passed. Checkpoint 2 was initially rejected after `cp1 v01` because POST accepted structurally incomplete schema-v2 sources and compatible job-detail HTML did not expose the computed continuation URL. The focused repair was approved through `cp2 v01` from the current-worktree fallback: the 10-test continuation slice, 59 original/cover/worker regressions, complete 24-test web suite, scoped Ruff check, and diff check passed. The full suite passed 377 tests and reproduced the same unrelated date-sensitive cost-evidence failure; repository-wide mypy still stops on the pre-existing duplicate `conftest` module mapping. Checkpoint 3 was approved through `cp3 v01` from the current-worktree fallback because no Checkpoint 3 commit boundary existed before review. The 75-test web/migration/persistence slice, full 381-test suite, repository-wide Ruff check, source mypy, and diff check passed. All planned checkpoints are approved. aflow-review-final approved the full accumulated handoff through `cp3 v01`; no fix plan was created. Final verification passed: `uv run pytest -q tests/test_web.py tests/test_migrations.py tests/test_persistence.py`, `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`, and `git diff --check`.

## Done Means

- Existing production jobs appear as one-project/one-version records after the explicit schema migration.
- Creating an original or cover automatically creates its project; continuing a compatible job creates another job in the same project.
- The continuation form is prefilled from the stored normalized request, is editable, and passes through the existing request validators, quote logic, cover confirmation, queue, transfer, and Runpod paths unchanged.
- A project page shows its versions and playable/downloadable outputs in one place; the user can rename the project and continue from a chosen compatible version.
- The dashboard, job history, existing job URLs, status polling, media/download URLs, root-path prefix, auth, CSRF, and legacy job rendering keep working.
- Migration, repository, route, template, security, and full regression tests pass.

## Critical Invariants

- One project has exactly one immutable job type: `original` or `cover`; a continuation cannot change type or move a job between projects.
- Every newly created job has a project. Migration v6 backfills every existing job into its own project without changing job, output, attempt, billing, transfer, or normalized-request data.
- A continuation always creates a new job. It never mutates, retries, resets, or deletes the source job or its outputs.
- The server derives the target project from an authenticated `continue_from_job_id`, verifies type and compatibility, and rejects missing, malformed, cross-type, or schema-incompatible sources. A hidden project ID is never accepted as authority.
- Reused values are presentation defaults only. POST requests still pass through `OriginalSongRequest` or `CoverRequest`; cover rights must be confirmed again and cover source ingestion/confirmation runs normally.
- No continuation action submits automatically. The user must review the form and press the existing generation action.
- All browser paths remain named-route/root-path aware. Unsafe mutations remain Basic-authenticated and CSRF-protected.
- Project features add no external service calls and make no change to Runpod payloads, worker schemas, generation order, billing rules, or transfer capability logic.

## Forbidden Implementations

- Do not introduce a client-side SPA, frontend framework, websocket, event bus, generic workflow engine, draft autosave, background project synchronizer, or new external dependency.
- Do not copy generation or enqueue logic into project routes; reuse the existing validated create routes and worker path.
- Do not treat query strings or hidden project IDs as trusted ownership links.
- Do not rewrite historical normalized requests or outputs during migration.
- Do not auto-retry failed jobs, resubmit paid work, reuse old transfer capabilities, or skip cover preparation/confirmation.
- Do not add project deletion, sharing, collaboration, permissions tiers, tags, folders, search, pagination machinery, or selected-output state in this handoff.
- Do not change the Runpod worker, home-ingest service, quality campaign, billing model, deployment repository, or public transfer API.

## Checkpoints

### [x] Checkpoint 1: Durable projects and job membership

**Goal:**

- Add the smallest durable project model and migration needed to group generation jobs without changing generation state.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `AGENTS.md`, `src/ace_service/models.py`, `src/ace_service/repository.py`, `src/ace_service/migrations.py`, `tests/test_migrations.py`, `tests/test_persistence.py`
- Preserve: current jobs, outputs, attempts, quotes, billing evidence, transfers, and explicit migration safety behavior

**Scope:**

- May create or modify: `src/ace_service/models.py`, `src/ace_service/repository.py`, `src/ace_service/migrations.py`, `tests/test_migrations.py`, `tests/test_persistence.py`
- Must not touch: `runpod_worker/`, `home_ingest/`, quality-campaign behavior, transfer API behavior, deployment files
- Constraints: add one `projects` table and one `jobs.project_id` membership column; do not add parent graphs, drafts, selections, or generic metadata stores

**Steps:**

- [x] Add a `Project` ORM record with UUID string ID, immutable `job_type`, bounded editable `title`, `created_at`, and `updated_at`; add `Job.project_id` plus explicit relationships and an index.
- [x] Add repository operations to create/get/list/rename a project, list its jobs newest-first, and resolve a continuation source. Creation must validate the project/job type match and update project activity when a new job is added.
- [x] Extend `create_original_job` and `create_cover_job` with an optional validated project membership path while preserving all existing call sites: no project supplied creates one atomically; a supplied project must exist and match the job type.
- [x] Add explicit schema migration v6. Create `projects`, add `jobs.project_id`, backfill each existing job into a project whose ID equals the job ID and whose bounded title is derived from source title, prompt, or the job-type label, then add the membership index. Preserve all existing rows byte-for-byte outside the new table/column.
- [x] Extend migration validation and failure/rollback tests for fresh databases, v5-to-v6 upgrades, idempotent exact-current status, complete backfill, and unchanged historical job/output/attempt evidence.

**Dependencies:**

- None.

**Verification:**

- Run: `uv run pytest -q tests/test_migrations.py tests/test_persistence.py`
- Run: `uv run ruff check src/ace_service/models.py src/ace_service/repository.py src/ace_service/migrations.py tests/test_migrations.py tests/test_persistence.py`
- Observe: every new and migrated job resolves to exactly one same-type project; migration failure remains fail-closed and no existing generation data changes.

**Done When:**

- Project persistence and v6 migration behavior are covered by positive, negative, and preservation tests.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if the current production schema is not exact v5 or contains job rows that cannot be backfilled deterministically.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 2: Safe continuation through existing forms

**Goal:**

- Let a user open an existing compatible job, prefill the matching form, edit it, and submit a new version in the same project.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `src/ace_service/web.py`, `src/ace_service/schemas.py`, `src/ace_service/templates/original_form.html`, `src/ace_service/templates/cover_form.html`, `tests/test_web.py`
- Preserve: current validators, submission quote capture, cover ingest/confirmation, queue semantics, CSRF, auth, campaign maintenance gate, and `/beta` root-path generation

**Scope:**

- May create or modify: `src/ace_service/web.py`, the two existing form templates, `tests/test_web.py`
- Must not touch: worker payload construction, Runpod client, background worker, transfer routes, media verification, billing formulas
- Constraints: one GET continuation route plus reuse of the current POST create routes; use stored schema-v2 normalized requests as defaults only

**Steps:**

- [x] Add `GET /jobs/{job_id}/continue`, authenticated like the current UI. Load the job, require a valid project, exact schema v2, and matching task type, then render the existing original or cover form with values reconstructed from its stored `generation` and project fields.
- [x] Put only `continue_from_job_id` in the form. On POST, resolve that job server-side, verify compatibility and type, then pass its project into the existing create transaction. Reject stale, missing, malformed, cross-type, or incompatible sources with bounded 404/409 behavior and no job creation.
- [x] Preserve editable values on validation errors, including the continuation source. Labels should say `Generate new version` when continuing and retain current labels for new projects.
- [x] Reconfirm cover rights on every continuation; do not pre-check the rights box. A continued cover must use the ordinary home-ingest, detected-duration confirmation, quote, and Runpod path.
- [x] Expose a named, root-path-safe continuation URL in the job view only when the source is compatible; keep legacy/incompatible jobs readable without offering a broken action.
- [x] Add route tests for exact prefill fields, user edits overriding defaults, same-project version creation, new-project creation, validation redisplay, cover reconfirmation, auth/CSRF, malformed legacy data, cross-type tampering, campaign gating, and `/beta` URLs.

**Dependencies:**

- Checkpoint 1.

**Verification:**

- Run: `uv run pytest -q tests/test_web.py -k 'continue or project or root_path or csrf or campaign'`
- Run: `uv run pytest -q tests/test_original_workflow.py tests/test_cover_workflow.py tests/test_worker.py`
- Observe: no GET or validation failure enqueues work; one confirmed POST creates exactly one new job in the source project and follows the existing pipeline.

**Done When:**

- Original and cover continuation work from stored schema-v2 requests without mutating the source job.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if a required form value cannot be reconstructed from the authoritative normalized request without guessing.
- Stop and report if unrelated dirty files make change ownership ambiguous.

### [x] Checkpoint 3: Simple project workspace and revision comparison

**Goal:**

- Give each project one clear server-rendered page where the user can see versions, play outputs, rename the project, and continue from a chosen compatible version.

**Context:**

- Run: `git rev-parse --show-toplevel`
- Inspect: `src/ace_service/templates/base.html`, `src/ace_service/templates/dashboard.html`, `src/ace_service/templates/jobs.html`, `src/ace_service/templates/job_detail.html`, `src/ace_service/static/app.css`, `src/ace_service/web.py`, `tests/test_web.py`, `docs/ARCHITECTURE.md`, `README.md`, `DEVLOG.md`
- Preserve: job history as an operational view, existing job-detail status polling, accessible HTML, authenticated media/downloads, and mobile layout

**Scope:**

- May create or modify: `src/ace_service/templates/projects.html`, `src/ace_service/templates/project_detail.html`, existing private UI templates, `src/ace_service/static/app.css`, `src/ace_service/web.py`, `tests/test_web.py`, `docs/ARCHITECTURE.md`, `README.md`, `DEVLOG.md`
- Owner-authorized ship-mode verification repair: `tests/test_costs.py` may make its trusted-rate fixture relative to the real test clock, and `tests/__init__.py` may be added solely to make pytest/mypy resolve one module identity.
- Must not touch: generation pipeline, public transfer app, worker services, deployment configuration
- Constraints: server-rendered HTML and current CSS only; no new JavaScript state layer or frontend dependency

The verification repair changes no product behavior and must remain limited to the two proven baseline defects. AFlow must not stop again solely because those previously out-of-scope test files are now part of this owner-authorized checkpoint scope.

**Steps:**

- [x] Add authenticated `GET /projects` and `GET /projects/{project_id}` routes. List projects by latest activity; project detail groups jobs newest-first and outputs under their version with existing verified media/download URLs.
- [x] Add CSRF-protected `POST /projects/{project_id}/rename` with trimmed bounded title validation and redirect back to the project. Do not add deletion or sharing.
- [x] Add `Projects` navigation, project links on job cards/details, and a prominent `Continue this version` action for compatible jobs. Keep operational job status and timing available without letting metadata dominate the project page.
- [x] Make the project page useful on a phone: stacked version cards, native audio controls, concise request summary, visible status/error, and no horizontal overflow. Reuse current classes where possible; add only narrowly needed CSS.
- [x] Document the project/job boundary, migration v6, continuation semantics, and operator migration command. Record explicitly that this handoff does not alter Runpod, cover ingestion, billing, or transfer behavior.
- [x] Add HTML/route tests for project ordering, version grouping, output links, rename success/failure, auth/CSRF, missing IDs, root-path prefixes, escaping, and legacy job readability.

**Dependencies:**

- Checkpoints 1 and 2.

**Verification:**

- Run: `uv run pytest -q tests/test_web.py tests/test_migrations.py tests/test_persistence.py`
- Run: `uv run pytest -q`
- Run: `uv run ruff check .`
- Run: `uv run mypy src`
- Run: `git diff --check`
- Observe: test-suite typing remains separate cleanup debt; it must not block this product checkpoint after source typing, full pytest, and focused behavior checks pass.
- Observe: a migrated historical job is reachable through Projects; a new generation and its continuation appear as two versions on one project page with working audio/download links.

**Done When:**

- The project workspace provides continuation and comparison without adding unrelated product or pipeline behavior.
- Documentation matches the implemented routes, persistence, migration, and preserved boundaries.
- Every completed step is validated against code, tests, or observable behavior.
- Verification passes and the changed files remain within scope.
- Before handoff, run `git status --short`, `git diff --name-only`, and `git diff --stat`.

**Blockers:**

- Stop and report if the project page would require bypassing existing verified media/download routes or duplicating generation state.
- Stop and report if unrelated dirty files make change ownership ambiguous.

## Behavioral Acceptance Tests

1. Given a v5 database with original, cover, completed, and failed jobs, when migration v6 runs, then every job belongs to one same-type project, existing IDs and evidence remain unchanged, and the migration reaches exact-current state.
2. Given a new original submission with no continuation source, when it is accepted, then one project and one job are committed and the ordinary enqueue path runs once.
3. Given a compatible original job, when the user opens Continue, then every supported form field is prefilled from the stored normalized request; editing and submitting creates one new job in the same project while the source job stays unchanged.
4. Given a compatible cover job, when the user opens Continue, then the source/style/settings are prefilled but rights are unchecked; submission requires fresh rights confirmation and follows normal home ingestion and duration confirmation.
5. Given a legacy, malformed, missing, or cross-type continuation source, when GET or POST is attempted, then the request fails with bounded 404/409 behavior, creates no row, and enqueues nothing.
6. Given a project with multiple jobs and outputs, when its page opens, then versions are newest-first, each output uses the existing authenticated media/download URL, and any failed version shows its user-facing failure without hiding successful versions.
7. Given a valid rename, when the CSRF-protected POST succeeds, then only the bounded project title changes. Missing auth, missing CSRF, blank/oversized title, or unknown project changes nothing.
8. Given deployment under `/beta`, when dashboard, projects, project detail, continuation forms, jobs, media, and downloads render, then every generated browser path contains the prefix exactly once.

## Plan-to-Verification Matrix

- Project persistence and automatic membership: repository tests plus fresh/v5 migration tests.
- Historical preservation and fail-closed migration: migration snapshots and failure-marker regressions.
- Original and cover continuation: focused web tests plus existing original/cover/worker suites.
- No automatic or duplicate paid submission: enqueue call-count and GET/validation-failure tests.
- Project workspace, rename, version/output grouping: authenticated route and rendered-HTML tests.
- Auth, CSRF, tamper rejection, escaping, root-path safety: negative web tests and existing security/root-path coverage.
- Pipeline and service boundaries unchanged: full test suite, Ruff, mypy, diff scope inspection, and documentation review.

## Assumptions And Defaults

- A project is a lightweight grouping of jobs, not a new generation state machine. Jobs remain the sole unit of queueing, execution, output, failure, billing, and transfer state.
- Each migrated historical job becomes its own project because no reliable earlier grouping exists.
- Project type never changes. Continuing a version always creates the same job type.
- Default title is derived deterministically from the stored sanitized source title, prompt, or `Original song`/`Cover`; it is trimmed to the declared bound and can be renamed later.
- Projects and jobs are retained; deletion and archival are excluded.
- Comparing versions means seeing their request summary, status, and playable outputs together. Selecting a canonical output, drafts, sharing, tags, folders, and collaboration are future decisions and are not implied as complete.
- The implementation starts from product release commit `c1f795811e652396259a4d147e6348ca84ab5641` or a verified descendant containing it.

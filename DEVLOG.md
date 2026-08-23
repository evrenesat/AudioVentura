# Development Log

## 2026-08-23 (Salad live-contract and retry-safety corrections)

- Corrected Container Engine lookup paths, queue-depth parsing, official
  resource-name boundaries, and the Job Queue Worker log-level variable.
- Schema-v2 unseeded submissions now freeze a cryptographically derived seed
  from the durable job, variation, and submission nonce so provider retries
  produce the same output; explicit seed progression is unchanged.
- Restored cover-source cleanup after validated local-output recovery commits.
- Made infrastructure apply idempotent and drift-safe, then published the
  corrected model-inclusive worker as immutable amd64 digest
  `sha256:86612d071ffca1d27cd532c6b0d0ff459b6867e25eed1761730f151cd2b45bf5`
  (28,979,322,061 compressed bytes). No Salad resource or paid job was created.

## 2026-08-23 (durable inference providers)

- Added provider-neutral capabilities, requests, refs, lifecycle, results,
  cancellation, health, bounded errors, and an explicit registry. Runpod is
  wrapped by an adapter; Salad Job Queues are implemented with bounded HTTP
  responses and deployment-scope cold-start enrichment.
- Advanced the product schema to v7 with additive provider provenance and a
  transactional Runpod backfill while retaining legacy columns for one
  rollback window. Runpod writes mirror legacy fields; Salad writes do not.
- Moved submission, polling, result, deadline cancellation, and transient
  backoff to persisted provider refs. A single status failure no longer
  terminally fails schema-v2 work, and submission is never retried.
- Added provider-neutral readiness/UI language and Salad configuration. No
  live provider call, deployment, paid request, or external mutation occurred
  in this implementation checkpoint.

## 2026-08-23 (SaladCloud infrastructure gate)

- Expanded p100's thin-provisioned root disk from 100 GiB to 180 GiB so the
  immutable model-inclusive worker could be assembled without altering the
  current controller, home-ingest, or Runpod deployments.
- Added a Salad Job Queue HTTP wrapper around the existing isolated ACE-Step
  handler, fail-closed model/readiness startup, a pinned Salad queue-worker
  binary, scale-to-zero desired-state tooling, and focused offline tests.
- Built and privately pushed the bundle-2 worker to GHCR. The amd64 manifest is
  `sha256:e20eceb01df99d129bd379a545aaf80f02b54c5294a48ba0e4ca424c111e279a`;
  its 20 compressed layers total 28,979,321,976 bytes, below SaladCloud's
  documented 35 GB image limit. No Salad queue, container group, or paid job
  was created because the organization/project slugs were not locally
  discoverable.

## 2026-08-22 (revision-pinned cached-model product runtime)

- Tightened cold-start UI language to the provider evidence boundary. A
  queued job with no worker now says Runpod has not allocated a GPU; once the
  provider reports an initializing worker, the UI says only that the cloud
  worker is initializing. Image download and model-cache preparation are not
  attributed individually until Runpod exposes a worker and its logs.
- Added a fail-closed paid-smoke recovery mode that waits on an explicitly
  named initial job and permits exactly one continuation submission. A long
  provider queue can no longer force a duplicate paid YouTube submission.
- Live worker logs proved bundle revision 1 contained two DiT Python files
  from the upstream model repository that differed from the pinned ACE-Step
  v0.1.8 runtime. ACE-Step therefore attempted an auto-sync into Runpod's
  read-only cached snapshot and failed. Bundle revision 2 replaces exactly
  those files with the pinned runtime copies and updates the manifest total;
  the worker's fail-closed byte receipt now matches that immutable release.
- Replaced the Runpod worker's network-volume checkpoint fallback with a
  fail-closed Hugging Face cached-snapshot contract: exact repo/commit/tag and
  manifest identities, fixed upstream revisions and ACE-Step source, complete
  four-component inventory and size validation, cache-contained symlinks, and
  offline model-library defaults are required before model initialization.
- Added aggregate bundle identity to bounded worker completion metadata and
  advisory Runpod progress updates at source transfer, generation,
  finalization, and output upload boundaries. Progress delivery failures do
  not affect generation.
- Added provider-evidenced UI phases for cache/capacity wait, worker startup,
  source transfer, generation, finalization, and upload. The controller stores
  monotonic nonterminal phase state in the existing attempt JSON, preserves it
  across restarts, replaces it at terminal completion, and shows elapsed time
  without invented percentages.
- Added offline tests for revision/ref refusal, manifest and inventory drift,
  unsafe links, legacy-volume refusal, exact progress parsing/persistence,
  initialization health evidence, terminal replacement, and UI polling. This
  product-side implementation pass contacted no provider and ran no paid
  generation before deployment.

## 2026-08-22 (cover continuation and duration recovery)

- Diagnosed production job `543b832e-342e-45b3-a8a6-861d45b28c1c`: the cover
  continuation copied only the historical YouTube URL and request parameters,
  so it created a fresh ingest and failed before any Runpod attempt.
- Cover continuation now requires a completed schema-v2 MP3 output with
  measured duration evidence, verifies its path/size/checksum, stages it
  locally for the new job, and commits confirmed staging without contacting
  home ingest or YouTube. Failed/no-output jobs no longer expose continuation.
- Cover requests now expose source/custom duration in the UI and preserve the
  measured source duration separately from the 10-600 second generation
  target through the worker payload and status view.
- Added an explicit two-submission paid UI smoke that exercises one initial
  YouTube cover and one local-output continuation while keeping credentials in
  the protected target environment.

## 2026-08-15 (usability recovery worktree, Checkpoint 3 execution)

Recovery Checkpoint 3 (make normal generation one-submit) from the
`aflow-audioventura-runpod-usability-recovery-aflow-plan-20260815-212058`
worktree; no deployment, provider contact, paid request, or commit was
performed, and all changes are left uncommitted for review.

- Both request models, server-side missing-form defaults, and HTML selected
  values now default to one variation; explicit 1-4 validation, submitted
  values after a 422 re-render, and continuation/edit values from existing
  jobs are preserved. The cover form selector, estimate labels, and request
  totals now cover counts 1-4 with correct singular/plural wording.
- New covers are one-submit: `_prepare_cover` requires the durably persisted
  initial rights confirmation (non-null `rights_confirmation_at`, failing
  closed otherwise), then in one database transaction persists the canonical
  source metadata/checksum/size, finalizes the normalized source duration,
  transitions `INGESTING -> STAGING`, and reuses `confirm_cover_job` so the
  committed state is `cover_staging.status=confirmed` plus `confirmed_at`
  before any Runpod submission. Only after that commit does it continue
  through the existing serialized `_submit_variation` path (per-variation
  nonce, durable attempt, at most one provider request ID). No column,
  migration, new staging status, or parallel flow marker was added, and no
  web request or legacy submission quote is constructed.
- The authenticated `/cover/{job_id}/confirm` and `/cancel` routes remain
  exactly as before (auth, CSRF, single-use transition) and are now reachable
  only for legacy rows whose durable state is `JobStatus.STAGING` plus
  `cover_staging.status=awaiting_confirmation`; new rows never commit that
  state and never render the confirm/cancel UI. Startup recovery still
  enqueues only `confirmed` staged rows, leaves legacy awaiting rows
  untouched, fails uncommitted `INGESTING` rows closed without repeating home
  extraction, and never resubmits a nonce-only uncertain submission.
- Added fake-transport tests: default 1 / explicit 2-4 / validation-value
  retention, one-submit success, the exact atomic confirmed-staging state
  (status, both timestamps, canonical source metadata and finalized duration
  observed by the fake Runpod at submit time), crash before and after the
  confirmed-staging commit, extraction and persistence failure, restart and
  nonce uncertainty, legacy awaiting-confirmation compatibility, missing
  rights fail-closed, and absence of the second-confirmation UI on
  new-flow detail pages. No test contacts Runpod or submits a paid request.

## 2026-08-15 (usability recovery worktree, Checkpoint 2 execution)

Recovery Checkpoint 2 (simplify active boundaries and costs) from the
`aflow-audioventura-runpod-usability-recovery-aflow-plan-20260815-212058`
worktree; no deployment, provider contact, paid request, or commit was
performed, and all changes are left uncommitted for review.

- Removed the `billing-sync` executable surface: the CLI parser/dispatch/
  helper, `ace_service/billing_client.py`, the sync-only settings
  (`ACE_BILLING_*`, `PRICE_MAX_AGE_HOURS`) in `config.py`/`.env.example`, its
  focused test file, and the operational instructions in
  `README.md`/`docs/OPERATIONS.md`/`docs/ARCHITECTURE.md`. A CLI regression
  (`tests/test_main_cli.py`) proves `billing-sync` is absent from help and
  rejected as an unknown command. Historical `billing_observations`,
  `billing_projections`, `submission_quotes`, rate-catalog, and calibration
  rows stay readable and migration-covered.
- Quarantined the quality campaign: the ordinary-submission maintenance gate
  calls in `web.py` (create original, create cover, cover confirmation) and
  the `quality_eval` module entrypoint are commented with a `TODO`
  (re-enable after ordinary original and cover generation is stable). The
  campaign store, evaluators, profiles, and unit-testable implementation
  remain intact; `python -m ace_service.quality_eval` is now a no-op.
- Cost display is now a read-only informational calculation at the fixed
  `USD 0.50/GPU-hour` rate (exact integer/rational arithmetic, no binary
  float). The original and cover forms show the latest three completed
  attempt durations of the matching kind, their average, and an approximate
  per-request estimate (unrounded average times variation count). Every
  label applies one `ROUND_HALF_UP` at the final four-decimal USD display
  boundary from the raw rational value, and the visible request total is
  bound to the selected variation count (original 1-4, cover 2-4). With no
  history a clearly labeled 60-second seed (`USD 0.0083`) is shown. The
  estimate is computed on read, never persisted, and any failure omits it
  without affecting generation.
- Quote capture and cost/quality gate calls were removed from normal
  submission control flow; the preserved quote machinery stays importable and
  unit-tested (runtime-identity binding, idempotence, conflict rejection).

Environmental note: the private quality fixture retention deadline
(`2026-08-15T11:20:46Z`) passed, so manifest-dependent quality tests fail
with `fixture retention deadline has passed` even on pristine `HEAD`
(verified via `git archive`); non-manifest quality tests and all other suites
pass.

### Checkpoint 2 review repair (cp03-v01, same worktree)

Review of the Checkpoint 2 cost display found two defects, repaired here
without any commit, deployment, provider contact, or paid request:

- The four-decimal labels were derived from integer micro-USD amounts and
  truncated with `ROUND_DOWN` (`120_000 ms` and the two-variation 60-second
  seed both displayed `USD 0.0166` instead of `USD 0.0167`). A single exact
  display helper (`format_exact_usd_half_up`) now applies `ROUND_HALF_UP`
  exactly once at the final `0.0001` USD boundary from raw
  numerator/denominator values for every sample, average, seed, and request
  label; integer micro-USD fields stay only for preserved callers and are
  never fed back into a label.
- The visible “This request” total was fixed to the initially selected
  count and omitted entirely on continuation and validation-error renders.
  The server now computes request labels for every supported variation count
  and renders them as per-option `data-request-text` attributes; a tiny
  self-hosted script (`static/estimate_selector.js`, CSP-compatible) swaps
  the label on selection with zero client-side money arithmetic. Continuation
  and 422 renders now supply the matching estimate through one
  `_form_estimate` helper, and a history/estimate failure still omits only
  the estimate.


## 2026-08-09 (AudioVentura project workspace, Checkpoint 3 execution)

Added authenticated project list/detail pages, CSRF-protected bounded rename,
same-project version comparison, native audio playback/download links, and
project navigation from existing job views. The mobile layout stacks versions
and keeps status, failures, request summaries, operational job links, and
compatible continuation actions visible without adding client-side state.
Documented schema v6 backfill and the project/job boundary. This work changes
no Runpod payload or call, cover ingestion/confirmation, billing rule, public
transfer route, worker service, or deployment configuration. Verification is
recorded in the Checkpoint 3 review handoff; no deployment or checkpoint commit
was performed during implementation.

Ship-mode verification repair made the trusted-rate test clock-relative instead
of letting its fixed date expire. Full pytest and source mypy remain release
gates. The repository's pre-existing test typing debt is tracked separately and
does not block this server-rendered usability change; no product safety or
billing check was disabled.

## 2026-08-09 (AudioVentura beta subpath, Checkpoint 2 execution)

Added the validated `ACE_SERVICE_ROOT_PATH` ASGI setting and converted the
private controller's browser contract to request-scoped named-route URLs.
Configured `/beta` deployments now keep navigation, forms, redirects, status
polling, static assets, media, downloads, and staged-cover actions below the
prefix exactly once, while the empty default preserves root-path behavior.
Transfer-app construction and signed transfer routes remain unchanged. Added
focused validation and full browser-contract regressions. Checkpoint
verification passed with 37 focused and 365 full-suite tests (five existing
framework deprecation warnings); compileall and diff-check passed. No
deployment, external service call, secret, generated media commit, or
checkpoint commit was performed.

## 2026-08-09 (Checkpoint 4 preserved-v4 retry repair, cp04 v05)

Preserved v4 endpoint and account-wide network-volume observations now retain
their original row identity on exact post-v5 retries, including non-current
historical values. Compatibility matching is limited to the complete native
bucket, complete evidence, and canonical UTC fetch time; it neither hydrates
nor rewrites immutable observation evidence. Production-shaped A@t1/B@t2
migration regressions prove repeated retries preserve both rows, freshness,
projection values, and endpoint/network separation. No live or paid provider
request, deployment, external message, production-default change, home-ingest
change, commit, or checkpoint sign-off was performed. Verification: focused
CP4 matrix = 180 passed; full controller/worker matrix = 411 passed (five
existing framework deprecation warnings); direct offline billing probes = 9
passed; retained CP4 regressions = 17 passed (one existing framework
deprecation warning). Ruff, format, mypy, and diff-check passed.

## 2026-08-08 (Checkpoint 4 repair overlay, cp04 v04)

Billing exact-repeat detection is now relative to the current endpoint or
account-wide network-volume projection. Separate value and fetch-event sha256
identities preserve A@t1 -> B@t2 -> A@t3 as three immutable changes while
making retries of each changed event idempotent; the retained A@t1, identical
A@t3, older B@t2 sequence still stores only A@t1/B@t2 and projects A@t3. The
ordered additive v4-to-v5 migration adds nullable identity columns and an index
without rewriting existing observations, rolls back injected failures, and
refuses automatic retry after its durable failure marker. No live or paid
provider request, deployment, external message, production-default change,
home-ingest change, commit, or checkpoint sign-off was performed. Verification:
focused CP4 matrix = 179 passed; full controller/worker matrix = 410 passed
(five existing framework deprecation warnings); direct v04 offline probe groups
= 7 passed; retained targeted regressions = 15 passed (one existing framework
deprecation warning). Ruff, format, mypy, and diff-check passed.

## 2026-08-08 (Checkpoint 4 repair overlay, cp04 v03)

Focused repair of the three cp04-v02 review findings. Eligible GPU selection
now compares validated exact decimal hourly-rate tokens, including colliding
integer derivatives. Newer checksum-identical billing fetches advance only
projection freshness, so older changed endpoint and network-volume evidence
cannot regress the current projection while history remains deduplicated and
append-only. Acceptance-time calibration matching now uses the server-owned,
startup-validated pinned worker image digest instead of a worker schema label;
a different digest yields `calibration_missing` and browser input cannot
override it. No live or paid provider request, deployment, external message,
production-default change, home-ingest change, commit, or checkpoint sign-off
was performed. Verification: focused CP4 matrix = 174 passed; full
controller/worker matrix = 406 passed (five existing framework deprecation
warnings); explicit v03 offline probes = 15 passed. Ruff, format, mypy, and
diff-check passed.

## 2026-08-08 (Checkpoint 4 repair overlay, cp04 v02)

Focused repair after independent rejection of cp04 v01; no live provider
request, deployment, spending, external message, production-default change,
home-ingest change, commit, or Checkpoint 4 sign-off.

- Corrected the offline Runpod adapter to `rest.runpod.io` and the documented
  endpoint/network-volume shapes. Strict parsing, decimal JSON, 1 MiB/row
  bounds, grouping/duplicate checks, and fail-closed pagination behavior remain;
  persisted observations now carry actual response bytes and bounded documented
  storage evidence.
- Network-volume summaries now read the latest one-row-per-native-bucket
  projection while append-only history remains immutable. Changed and
  out-of-order observations cannot double count or regress the current value.
- Status-loss completion records explicit unavailable attempt evidence in the
  same transaction, preserving durable worker model/image metadata. Newly
  terminal CP4 paths no longer leave `pending`; migrated legacy rows retain
  their documented pending semantics.
- Added immutable versioned `runtime_calibrations`, exact dimension matching
  without extrapolation, model-sensitive quote fingerprints, and exact hourly
  USD text in catalog/quote/attempt snapshots. Catalog and calibration version
  conflicts reject before mutation; exact repeats remain idempotent. No product
  calibration observations were manufactured, so an empty catalog yields
  `calibration_missing`.
- Verification: focused CP4 matrix = 154 passed; full controller/worker matrix
  = 399 passed (five existing framework deprecation warnings); 18 targeted
  offline contract/state probes passed. Ruff check, Ruff format check, mypy,
  and diff-check passed. No live or paid provider evidence was collected.

## 2026-08-08 (Checkpoint 4 implementation worktree, cp04 v01)

Checkpoint 4 implementation (durable cost ledger and billing reconciliation)
in the focused `cp04-v01` worktree; no deployment, provider contact, spending,
production-default change, commit, or Checkpoint 4 sign-off. Checkpoint 3 and
its tests remain untouched.

- Ordered SQLite migration runner (`ace_service/migrations.py`):
  `CURRENT_SCHEMA_VERSION = 4`, read-only `migrate-status` (path hash + state
  only; distinguishes unversioned legacy, exact, older, unknown/newer,
  incomplete started/failed, missing, non-database, corrupt), and offline
  `migrate-upgrade` under an exclusive sidecar `.migration.lock` flock. A
  short transaction commits the durable `migration_started` marker, then a
  separate exclusive transaction applies additive CP4 DDL (CREATE TABLE IF
  NOT EXISTS + conditional `ALTER TABLE ADD COLUMN`) and records the
  completed version; a crash leaves a visible incomplete marker and upgrade
  refuses to guess past it. Normal startup (`create_app` production path)
  calls `ensure_schema_readiness` and refuses every state except the exact
  expected version; `initialize_database()` remains a foundation creator.
- Cost-domain persistence (`models.py`, `repository.py`): immutable
  `SubmissionQuote` (one-to-one job key, secret-free fingerprint, exact
  micro-USD, allow-listed unavailable reason codes with CHECK pairing),
  `VariationAttempt` execution-cost evidence columns (`pending`/`unavailable`/
  `complete`, unavailable reasons bounded), append-only `BillingObservation`
  with sha256 checksum idempotence plus current `BillingProjection` upsert,
  versioned `GpuRateCatalog` with `PRICE_MAX_AGE_HOURS=24`, and the singleton
  `BillingLease`. `record_attempt_evidence` enforces the immutable state
  machine (exact-repeat idempotent, unavailable→complete fills missing
  inputs, all conflicts rejected before any mutation, estimate must equal the
  centralized half-up formula).
- Operator-only billing boundary (`ace_service/billing_client.py`): strict
  endpoint/network-volume parsers (bounded array, allow-listed keys,
  duplicate/undocumented/overflow rejection, decimal-aware JSON, USD as
  server contract value), sync `RunpodBillingClient`, database singleton
  lease with stale recovery, read-only boundary probe, and the
  `python -m ace_service billing-sync` command that refuses a non-exact
  schema. No browser route calls billing and no in-process scheduler exists.
- Quotes are captured server-side in the same transaction that accepts a
  generation (original creation and cover confirmation; never from the form,
  never for unconfirmed staging). Terminal polling records immutable attempt
  evidence from Runpod `executionTime` provenance, resolved GPU aliases, and
  the rate catalog; unknown/stale rates and missing timing record explicit
  unavailable reasons, and zero is never invented.
- Verification: focused CP4 matrix `tests/test_persistence.py
  tests/test_worker.py tests/test_runpod_client.py tests/test_costs.py
  tests/test_migrations.py tests/test_billing_sync.py` = 147 passed; full
  `tests runpod_worker/tests` = 392 passed; `ruff check`, `ruff format
  --check`, `mypy src runpod_worker`, and `git diff --check` all clean.
  Review hardening: production startup preflights an existing database and
  refuses before the foundation creator could add tables to a legacy schema
  (regression test proves a refused legacy DB stays byte-for-byte untouched);
  the failed-poll path records evidence before the terminal transition so the
  transaction is all-or-nothing; the read-only status connection percent-encodes
  the path in the SQLite URI; the lease singleton bootstraps race-free with
  `INSERT OR IGNORE`.
  No paid, live Runpod, deployment, or external-message evidence is claimed.

## 2026-08-08 (evidence-completion and atomic-migration repair overlay, cp03-v10)

Focused, non-checkpoint repair of the two reviewed cp03 defects on top of
the accepted cp03-v09 work; no deployment, provider contact, spending,
production-default change, commit, or Checkpoint 3 sign-off.

- Completed-unavailable evidence completion is compatible and immutable: a
  completed sample may gain only missing authoritative GPU/execution/rate
  cost inputs while remaining `completed`. A later call supplying a
  conflicting output path, GPU, execution value, reason, estimate, or status
  is rejected with `CampaignGateError` before any sample, reservation,
  event, or timestamp mutation — the reviewed job-a→job-b output-identity
  probe is reproduced as a regression with a before/after snapshot proving
  no mutation. Exact repeats stay idempotent, and uncertain-to-compatible
  terminal transitions are unchanged.
- The ordered v1/v2-to-v3 migration is one rollback-safe unit:
  `_validate_existing` preflights foreign reservation states before any
  mutation, then runs the v1-to-v2 additions, the v2-to-v3 reservation
  rebuild/copy with its four-state `CHECK`, `PRAGMA user_version=3`, final
  schema/column and reservation-state validation, and
  `PRAGMA foreign_key_check` inside one transaction with `PRAGMA
  foreign_keys` managed outside it. Any rejection rolls the whole unit back
  and restores the source schema version, objects, rows, reservation state,
  timestamps, child foreign keys, and temporary-table state exactly.
- Added direct regressions for conflicting completed output identity,
  conflicting GPU/reason evidence, corrupt v1 no-mutation, failure after
  the v1-to-v2 statements with full rollback, and successful v1/v2-to-v3
  migrations preserving every reservation field and
  `storage_artifacts.reservation_id` child link with an empty
  `PRAGMA foreign_key_check`. All local tests and static checks pass
  without Runpod contact; no live campaign, paid baseline, quality result,
  endpoint teardown, deployment, or default promotion is claimed.

## 2026-08-08 (reservation-state integrity repair overlay, cp03-v09)

Focused, non-checkpoint repair of the three reviewed state-integrity defects
on top of the accepted cp03-v08 work; no deployment, provider contact,
spending, production-default change, commit, or Checkpoint 3 sign-off.

- Uncertain/in-flight work stays `unresolved`: `record_terminal_execution`
  with `status="uncertain"` now leaves the reservation `unresolved` with its
  full immutable `reserved_micro_usd` counted in admission totals and no
  final estimate, unavailable reason, or settlement timestamp; teardown and
  rollback remain blocked even with provider-zero evidence.
  `conservatively_retained` is now reachable only for durably terminal
  unknown-cost attempts (`failed`, cancelled with unknown start/cost,
  `completed` without authoritative cost evidence); unsubmitted and
  proven-not-started cancellations still settle at zero.
- Terminal identity and evidence are immutable and fail closed: a `failed`,
  `cancelled`, `unsubmitted`, or completed terminal sample rejects any later
  conflicting status, output, GPU, execution, reason, or estimate. The only
  allowed advances are uncertain-to-compatible-terminal (completed, failed,
  or cancelled, preserving prior compatible identity) and
  completed-unavailable-to-completed-with-authoritative-cost; exact repeats
  remain idempotent. The reviewed failed-retained-to-completed rewrite probe
  is reproduced as a regression and rejected with the retained amount and
  state unchanged.
- Campaign schema advanced to v3: `reservations.state` is constrained by a
  SQLite `CHECK` to exactly `open`, `unresolved`, `conservatively_retained`,
  and `settled`; the v1-to-v2 and new v2-to-v3 migrations run in order and
  copy every reservation verbatim (IDs, links, amounts, timestamps,
  estimates, reasons). Existing databases with unknown/corrupt/newer states
  are refused without mutation, and committed-spend, teardown,
  campaign-status/recovery, and rollback-readiness paths fail closed even
  when a foreign state is injected after open.
- Added direct regressions for uncertain state, immutable terminal
  transitions, v1/v2-to-v3 migration and schema rejection, unknown-state
  fail-closed behavior, and eval-pipeline coverage, while retaining the full
  provider-zero, pre-intent, campaign-identity, manifest-independent
  recovery, UUID/fingerprint, parser, score, and production-default matrix.
  All local tests and static checks pass without Runpod contact; no live
  campaign, paid baseline, quality result, endpoint teardown, deployment, or
  default promotion is claimed.

## 2026-08-08

- Recorded Checkpoint 1's application/deployment heads, private network
  bindings, v1 schema and queue-recovery behavior, current `create_all()`
  startup limitation, SQLite backup/restore rehearsal, and pre-edit test
  baseline in `docs/CHECKPOINT-1-BASELINE.md`.
- Added the fixed-input, blinded listener contract and USD 5 hard campaign
  guard in `docs/QUALITY-EVALUATION.md`. Stored the CC0 fixture manifest and
  honest no-run baseline result under the private data root; no evaluation
  media or paid inference state is tracked in Git.
- Re-ran the full Checkpoint 1 verification after the approved baseline
  evidence: the six known Docker-only `lameenc` test failures remain isolated,
  while Ruff, format, mypy, and all home-ingest checks pass. Revalidated the
  private fixture hash, no-run result shape, and `0600` permissions without
  making a Runpod call.
- Implemented the focused Checkpoint 2 recovery: immutable v2 profiles and
  prompt modes, explicit original duration, independent cover controls,
  source-duration staging/confirmation, strict dual-version worker parsing,
  fail-closed ACE constructor checks, sequential reproducible variations, and
  bounded versioned result metadata in existing JSON fields.
- Repaired the v2 lifecycle overlay: staged covers can cancel safely, polling
  reveals confirmation asynchronously, pinned ACE LM metadata and effective
  captions persist without rewriting lyrics, status-loss recovery requires
  validated v2 evidence, worker images require immutable digests, and matching
  numeric duration prose is accepted without changing structured seconds.
- Added the read-only schema-v1 rollback gate with fail-closed v2 lifecycle
  classification, and corrected pinned LM lyric truth for empty-input
  Enhance/auto-compose requests while preserving supplied lyrics exactly.
- Unified bounded result projection so profile, generated metadata, resolved
  parameters, output evidence, and worker identity survive on outputs in both
  result-before-upload and normal arrival orders; queue/execution timing stays
  on attempts.
- Added `lameenc==1.8.4` to the development group so the local worker contract
  suite uses the same in-process MP3 encoder already installed by the worker
  image; production controller dependencies remain media-free.
- Implemented the Checkpoint 3 private quality campaign boundary: a versioned
  SQLite campaign store, exact micro-USD accounting, append-only Runpod
  billing observations, fixed-fixture dry-run CLI, durable submission gate,
  blinded score sheets, deterministic screening/confirmation gates, and
  rollback-edge checks. Endpoint USD is recorded as a source-contract value
  because the response omits currency; network-volume rows without a volume ID
  remain account-wide and are never allocated to AudioVentura.
- Completed the executable Checkpoint 3 campaign path: `--execute` now creates
  ordinary durable controller jobs through the repository job factory and
  drives them one at a time through the controller's own queue/transfer
  machinery, awaiting terminal evidence before the next reservation and
  tearing the window down at zero workers. Confirmation reuses the completed
  screening-seed sample by exact fingerprint (never resubmitting or recharging
  it); score-sheet export/finalization require complete output evidence per
  stage; the `--decision` command unblinds the complete matched pairs, applies
  the frozen promotion gate, and persists an immutable quality decision that
  is idempotent and fails closed on conflict; backup refuses a missing
  campaign database before creating it; and only completed attempts are ever
  billed, so failed/uncertain work stays unavailable and keeps the teardown
  gate closed.
- Repaired the strict compatibility contract for the Checkpoint 3 fallback
  (`cp03-v05`): the strict-v1 and strict-v2 smokes are now expanded into
  complete worker envelopes (legacy v1 generation, or v2 generation plus
  `profile_id` and worker-safe resolved parameters) and validated end-to-end
  through `ControllerWorker._default_payload` and
  `runpod_worker.schemas.WorkerRequest.from_mapping`; campaign covers carry
  both cover controls and `target_style` in the generation. Product job IDs
  are now generated UUIDs distinct from opaque campaign sample IDs, with the
  campaign store durably linking `sample_id → job_id` through
  `mark_sample_submitted`; reservations settle on the repeated call.
- Enforced exact current scoreable coverage at score-sheet import and
  finalization: a new planned or later-completed scoreable sample, or a stale
  pair membership, makes the frozen export partial and is rejected
  deterministically at both transitions.
- Added the deterministic operator advancement action (`--advance`): it
  requires both finalized screening sheets, ranks candidates per task type
  with the frozen severe-artifact/tie/top-two rules, persists the exact
  finalist set as a durable `screening_advanced` event (idempotent retries,
  conflicting sets fail closed), materializes confirmation alias/payable
  cases, and lets `--execute --stage confirmation` consume only the durable
  planned payable samples while exact-fingerprint seed-one aliases stay
  non-payable.

## 2026-08-07

- Repaired the Runpod output boundary: removed the worker image's `ffmpeg`
  package, pinned `lameenc==1.8.4`, and changed MP3 generation to encode a
  temporary PCM WAV in-process while preserving requested output metadata and
  cleanup guarantees.
- Started the Hetzner controller foundation with typed settings, SQLite
  persistence, path-safe records, and baseline tests.
- Corrected configuration validation to require HTTPS for public transfers and
  reject credential placeholders without a runtime bypass.
- Added durable variation attempts, explicit controller transitions, singleton
  data-root locking, serialized Runpod orchestration, and restart recovery.
- Added the isolated home-ingest agent with strict YouTube URL validation,
  bounded yt-dlp download, local ffprobe/ffmpeg canonicalization, restricted
  SFTP `.part` upload, checksum metadata, and failure cleanup.
- Connected cover jobs to home-ingest, verified SFTP source finalization,
  signed Runpod source/output capabilities, cover payload mapping, restart
  polling, and non-retained source cleanup.
- Added the authenticated mobile-friendly controller UI with Basic auth,
  same-site CSRF, job forms/history/status polling, readiness banners,
  authenticated playback/download routes, and media containment/checksum
  validation. Kept the UI app separate from the public transfer app and added
  an authenticated home-ingest health probe.
- Added Checkpoint 9 operational hardening: private UTC rotating logs with
  credential/capability/prompt redaction, startup and periodic controller and
  home cleanup, terminal capability revocation, bounded source retention, and
  deployment/acceptance runbooks.

## 2026-08-08 (Checkpoint 3 repair overlay)

- Made `--advance` require the fresh explicit `--confirm` before any campaign
  database open or mutation; an unconfirmed invocation returns the bounded
  blocked exit with no event, sample, alias, or status change, and an
  identical confirmed retry stays idempotent because the frozen
  `screening_advanced` record must match both `finalists` and `rankings`.
- Corrected the frozen cutoff-tie rule in `rank_screening_candidates`: a
  score-equivalence group that crosses the `maximum_finalists` boundary is
  excluded in its entirety, so a three-way tie for first advances none, an
  exact two-way tie for first advances both, and a tie spanning positions two
  and three advances only the untied first-place candidate; deterministic
  candidate-ID ordering applies only after eligibility is decided.
- Made product-job creation and campaign linkage crash-recoverable: the
  product UUID is preassigned before either database commit, a bounded
  campaign submission intent (sample ID, reservation ID, exact product UUID,
  non-sensitive SHA-256 request fingerprint, source URL) is persisted before
  the product row, and recovery creates or validates the product row against
  that frozen intent so either crash order recovers exactly one UUID job, one
  campaign link, and one reservation before any remote submission starts.
- Added bounded Runpod `/health` parsing (`workers.idle`/`running` and
  `jobs.inQueue`/`inProgress` as non-negative integers with missing/boolean/
  negative/oversized/unknown structures rejected) and made teardown
  evidence-backed and fail-closed: `close_execution_window` now requires
  immutable timestamped zero-at-rest provider evidence for the authorized
  endpoint, stores it on the window record, and retains the maintenance gate
  on malformed, unavailable, or nonzero evidence; exception/finally paths
  never clear the gate merely by reaching `finally`.
- Corrected the pinned provider-health contract to Runpod's documented
  `jobs.inQueue`/`jobs.inProgress` fields (the earlier invented
  `jobs.queued`/`jobs.running` shape is now rejected rather than accepted as
  evidence), replaced every synthetic health fixture with representative
  documented bodies, and proved successful execution and verified teardown
  close only on the real camel-case zero-work response.
- Made the recovery-only CLI actions independent of the external fixture
  manifest: `--status`, `--backup`, `--reconcile`, and `--verified-teardown`
  dispatch from frozen campaign/sample/submission-intent state before the
  manifest is loaded, hashed, or rebuilt, so fixture expiry, removal, or
  corruption can no longer block the exact recovery actions that must remain
  available while the maintenance gate is open; the manifest is still loaded
  and validated for dry-run, execute, advancement, score-sheet, and decision
  modes, whose semantics depend on it. Added CLI regressions proving status,
  backup, reconciliation, and verified teardown succeed with missing and
  malformed manifests and keep every fail-closed refusal (missing database,
  unknown campaign, phantom product rows, unresolved samples, nonzero or
  malformed provider health).
- Added the bounded recovery-only CLI actions `--status` (read-only),
  `--reconcile` (confirmed, idempotent, refuses unknown product rows), and
  `--verified-teardown` (confirmed, settles nothing by assumption), and
  reject ordinary score, advancement, decision, and execute actions while a
  window, gate, or pending submission intent is open; backup and status stay
  available. Campaign schema advanced to v2 with an automatic v1 migration.
- Extended the campaign/eval/Runpod test suites with confirm-gate, tie-rule,
  crash-order recovery, health-contract, teardown fail-closed, status,
  reconciliation, and gated-action coverage; all local tests pass without
  Runpod contact. Checkpoint 3 remains unchecked pending independent review
  and separately authorized live evidence.

## 2026-08-08 (bounded recovery repair overlay)

Focused, non-checkpoint repair of three bounded recovery/safety failures on
top of the cp03-v07 work; no deployment, provider contact, spending,
production-default change, or commit. Checkpoint 3 remains unchecked.

- Conservative retained reservation state: terminal attempts whose
  attributable compute is unknown (`failed`, `cancelled` with unknown start,
  `completed` without cost evidence) are now recorded as
  `conservatively_retained` instead of `unresolved`. The immutable original
  `reserved_micro_usd` keeps counting in every admission/budget total (a
  retained reservation still consumes admission headroom), is never presented
  as an executed-attempt estimate or invoice value, and verified teardown may
  close it only after the sample is durably terminal and provider-observed
  zero evidence passes. Genuinely `open`/`unresolved` reservations still
  block teardown and rollback readiness; completed-with-evidence attempts
  stay `settled` at their immutable estimate, proven-never-submitted work
  stays zero, and proven-not-started cancellations stay zero.
- Pre-intent crash reconciliation: confirmed `--reconcile` now first settles
  the exact state produced after reservation commit but before the
  submission-intent commit — a frozen `planned` sample with exactly one open
  compute reservation, no `submitted_at_utc`, no submission intent, and no
  product-job link — atomically recording it as proven unsubmitted and
  settling the reservation at zero with a `pre_intent_reconciled` audit
  event, creating no product job and calling no provider. Any contradictory
  evidence (submitted timestamp, duplicate or non-compute reservations) fails
  closed; intent-present and job-linked states remain owned by the existing
  UUID/fingerprint crash recovery, which is unchanged and still idempotent.
- Campaign identity guards: `--status`, `--backup`, `--reconcile`, and
  `--verified-teardown` all validate the named campaign before acting. An
  unknown `--campaign-id` is blocked before any backup file, product engine,
  controller worker, Home Ingest client, or Runpod client is created;
  `--verified-teardown` rejects an active maintenance gate belonging to a
  different campaign instead of closing it or reporting `not_needed`, and
  returns `not_needed` only for a known campaign with no active gate.
- Added direct regressions for all three repairs (conservative retention in
  budget totals and teardown, pre-intent exact-match/contradiction/idempotent
  recovery, and unknown-campaign/gate-mismatch refusals) while retaining the
  full existing matrix: missing/malformed/hash-invalid manifests,
  `jobs.inQueue`/`jobs.inProgress` provider-zero parsing and obsolete-shape
  rejection, crash-order UUID recovery, fingerprint conflicts, unresolved
  work, endpoint mismatch, bounded health counts, tie rules, and score
  coverage. All local tests and static checks pass without Runpod contact;
  no live campaign, paid baseline, quality result, endpoint teardown,
  deployment, or default promotion is claimed.

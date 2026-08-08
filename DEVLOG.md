# Development Log

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

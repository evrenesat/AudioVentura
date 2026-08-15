# Quality evaluation contract

This contract freezes the Checkpoint 1 evaluation inputs and scoring rules.
It is an operator-only, fixed-input comparison contract. It does not authorize
a paid Runpod campaign or change production defaults.

> **Quarantine notice (usability recovery):** the quality campaign is
> quarantined. Its executable entrypoint (`python -m ace_service.quality_eval`)
> and the ordinary-submission maintenance gate are disabled with a `TODO`
> (re-enable after ordinary original and cover generation is stable). The
> campaign store, evaluators, profiles, campaign data, and this contract stay
> intact and readable; nothing here is executed in the recovery.

## Private fixture

The fixture is described by
`/srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json`. The
downloaded media and all evaluation copies remain under the configured private
data root with restrictive permissions. The manifest records the stable fixture
ID, canonical source page, CC0 proof, retrieval time, local SHA-256, source
duration, fixed clip boundaries, prompts, lyrics, seeds, rubric version, and
retention deadline. No media or source URL is committed to Git, logged, or
written to billing records.

The baseline result is a separate machine-readable file at
`/srv/ace-service/data/evaluations/quality-fixture-v1/baseline-result.json`.
It records the current resolved v1 parameter shape and explicitly records that
live scores, GPU timing, and cost are unavailable until an authorized worker
run. Null is used for an unmeasured field; no score or cost is invented.

## Cases and fixed inputs

The manifest has one fixed cover case using the 20-second source clip and one
fixed original case using the same target duration. Each case freezes its
prompt, lyrics (or explicit no-lyrics value), output format, and seed list.
Screening uses the first seed. Confirmation, if authorized, uses exactly the
three manifest seeds. A comparison changes one declared profile/control at a
time and holds fixture, clip, prompt, lyrics, duration, output format, and seed
constant.

The worker's resolved parameter object is captured in the private result for
every executed sample. A generated random seed becomes immutable when the
worker returns it. The result stores execution time, actual GPU, model/profile,
and estimated execution cost separately from any provider billing bucket.

## Anchored listener rubric

Listeners assign an integer from 1 through 5. They may use the intermediate
anchors as written; comments must describe the sound only and must not contain
listener names or other personal data.

Melody retention:

1. The source melody is absent or unrecognizable.
2. Only isolated fragments remain and the main contour is substantially lost.
3. The main melody is recognizable with noticeable omissions or changes.
4. The melody is clear and mostly complete with minor changes.
5. The melody is immediately recognizable, coherent, and retained throughout.

Prompt/style adherence:

1. The requested style or prompt is not perceptible.
2. A few requested traits appear, but the result is dominated by another style.
3. The central style is present but inconsistent or generic.
4. Most requested traits are clear and sustained.
5. The result strongly and consistently realizes the requested style and brief.

Development:

1. The piece is static, unfinished, or collapses into disconnected fragments.
2. There is little progression and repeated material feels accidental.
3. There is a usable arc with limited contrast or weak transitions.
4. Sections develop with clear contrast and mostly purposeful transitions.
5. The arrangement has a compelling, complete arc with purposeful development.

Vocal/lyric adherence (when vocals or lyrics are applicable):

1. Vocals/lyrics are absent, unintelligible, or unrelated.
2. A few words or vocal gestures are recognizable, but most are unusable.
3. The broad lyric/vocal content is understandable with material errors.
4. Lyrics and vocal character are clear with only minor errors.
5. Lyrics are consistently intelligible and the vocal delivery fits the brief.

Artifacts (higher is cleaner):

1. Severe clipping, warbling, noise, broken audio, or other artifacts dominate.
2. Frequent severe artifacts interrupt listening or make sections unusable.
3. Noticeable artifacts occur but the piece remains broadly usable.
4. Minor artifacts are occasional and do not distract from the piece.
5. Clean, stable audio with no distracting generation artifacts.

Ending quality:

1. The result cuts off, loops, or ends as a clear generation failure.
2. The ending is abrupt or incomplete and requires repair.
3. The ending is acceptable but generic, weak, or slightly truncated.
4. The ending feels intentional with only a minor weakness.
5. The ending is clearly resolved, intentional, and well matched to the piece.

For an instrumental case, vocal/lyric adherence is recorded as `not_applicable`
and omitted from that case's primary-score denominator. The cover primary score
is the mean of melody retention, prompt/style adherence, development,
vocal/lyric adherence when applicable, and ending quality. The original primary
score omits melody retention. Artifacts remain a separate safety dimension.

## Blinding and decision gate

The campaign creates cryptographically random opaque sample IDs and stores the
sample-to-case/profile mapping in a private operator file. IDs must not encode
job type, model, profile, seed, or ordering. Each listener receives only the
opaque ID and audio. The mapping remains hidden until both listeners have
submitted complete score sets; missing scores, a tie, or listener disagreement
means no promotion.

A candidate is eligible only if, across the three confirmation seeds:

- its mean primary score improves by at least `0.5` on the 1–5 scale;
- neither listener's mean regresses by more than `0.5` on any rubric dimension;
- the count of severe artifacts (artifact score `<= 2`) does not increase; and
- both listeners prefer the candidate in at least two of three paired tests.

The cover and original decisions are independent. Unblinding never changes the
production default automatically; a separate reviewed configuration change is
required.

## Hard campaign budget

All campaign amounts are integer micro-USD. The immutable campaign ceiling is
`5_000_000` micro-USD (USD 5.00). Before paid execution, the runner computes a
conservative reservation for every ordered case using the highest eligible
Flex rate and measured runtime range. It must reject the campaign before any
remote call if projected cumulative spend would exceed the ceiling.

The runner stops accepting new cases at `4_500_000` micro-USD (USD 4.50),
leaving USD 0.50 for initialization, idle time, and provider rounding. If
fetched campaign-period provider billing reaches `5_000_000` micro-USD, it
stops immediately and never resumes automatically. This campaign guard is
independent of product submission quotes and runtime calibration.

Per-sample `execution_ms × trusted_rate` is an estimated attributable compute
amount, not an invoice charge. Endpoint billing buckets, reconciliation delta,
and storage are separate fields when available; none is allocated to a song.
Rates are fixed-decimal strings and the one half-up rounding operation occurs
only after multiplication.

## Retention and result shape

Evaluation media copies are deleted after both score sets are final and no later
than the manifest retention deadline (seven days by default). An explicit
operator retention decision is required to retain a public-domain or user-owned
copy beyond that deadline.

Each campaign result follows this bounded shape:

```json
{
  "result_schema": "quality-evaluation-result-v1",
  "campaign_id": "opaque-campaign-id",
  "fixture_id": "quality-fixture-v1",
  "status": "not_run|running|complete|failed",
  "budget": {
    "ceiling_micro_usd": 5000000,
    "admission_stop_micro_usd": 4500000,
    "projected_micro_usd": null,
    "spent_provider_micro_usd": null,
    "decision": "not_admitted|admitted|stopped|complete"
  },
  "samples": [
    {
      "opaque_sample_id": "random-id",
      "case_id": "cover-baseline",
      "seed": 1729,
      "profile_id": "baseline-v1",
      "resolved_parameters": {},
      "actual_gpu": null,
      "execution_ms": null,
      "estimated_compute_micro_usd": null,
      "listener_scores": {
        "listener_a": null,
        "listener_b": null
      }
    }
  ]
}
```

The `not_run` baseline uses null measurements and a reason code rather than
pretending that a local mock or an unauthenticated endpoint probe is a quality
or cost observation.

## Checkpoint 3 operator controls

The local comparison command is `uv run python -m ace_service.quality_eval`.
It has no HTTP route and its `--dry-run` path does not import or contact an
inference client. It validates the frozen manifest and private media hash,
expands the compatibility smokes, incumbent samples, cover grid, staged
original/cover conditionals, and confirmation bounds, then reports minimum and
maximum job/attempt counts. A dry-run with no fresh official Flex-rate catalog
is intentionally successful but reports `admissible: false`; it never invents
a reservation.

Campaign state lives in the dedicated
`$ACE_SERVICE_DATA_ROOT/evaluations/quality-campaign.sqlite3` database (or the
explicit `ACE_EVALUATION_CAMPAIGN_DATABASE` path). It is separate from
`service.db`, uses a versioned schema, transactions, bounded JSON, private
permissions, and a SQLite-API backup command. A restart expires only stale
leases. It retains active maintenance gates, open reservations, and uncertain
samples until an operator reconciles them. Operator `--status`, `--backup`,
`--reconcile`, and `--verified-teardown` run from this frozen durable state
alone: they do not load, hash, or rebuild the external fixture manifest, so
fixture expiry, removal, or corruption cannot block the recovery actions that
must remain available while the maintenance gate is open.

Execution requires a campaign ID, an explicit confirmation flag, fresh
server-owned official Flex-rate evidence for every eligible GPU, proven
Runpod billing interval semantics, and a separate authorization record naming
the application commit, worker digest, endpoint/template, evaluation models,
rollback target, blocked enqueue routes, and ceiling. The campaign opens a
durable one-at-a-time window that blocks `POST /create`, `POST /cover`, and
`POST /cover/{job_id}/confirm`; authenticated reads remain available. The
operator bypass is scoped to the matching campaign ID. A failed fetch never
lowers committed spend, and the window is cleared only after reconciliation,
provider-observed zero workers, and verified teardown.

Terminal attempts whose attributable compute is unknown are recorded as
`conservatively_retained` reservations, distinct from an executed-attempt
estimate (`settled`), an in-flight/uncertain reservation (`unresolved`), and
the never-submitted or proven-not-started zero cases (also `settled`, at
zero). A conservatively retained reservation keeps its full immutable
original `reserved_micro_usd` in every later admission/budget total — so
recovery can never lower committed spend — but is never presented as an
estimate, an invoice value, or billed compute. Uncertain/in-flight work is
not terminal for financial purposes: it stays `unresolved` with its full
immutable reservation counted in admission totals and continues to block
teardown and rollback, and only a later compatible terminal record (or a
completed-unavailable record gaining authoritative cost inputs) may advance
it — failed, cancelled, unsubmitted, and completed terminal identities are
immutable and reject conflicting later status, output, GPU, execution,
reason, or estimate evidence, and a completed-unavailable record may gain
authoritative cost inputs only when any supplied output path, GPU,
execution, reason, or status matches the recorded evidence, rejecting
conflicts before any cost/reservation mutation. Verified teardown treats
conservatively retained reservations as financially resolved only after the
sample is durably terminal and provider-observed zero evidence passes;
genuinely open/unresolved reservations and nonterminal samples still block
teardown and rollback. The campaign schema (v3) constrains reservation
states to the four declared values with a SQLite `CHECK`, migrates v1/v2
stores to v3 as one atomic unit without losing IDs, links, amounts,
timestamps, estimates, or reasons or any `storage_artifacts` child link (a
rejected migration leaves the source database unchanged), and fails closed
whenever an unknown or corrupt reservation state is observed at open or in
committed-spend, teardown, campaign-status/recovery, and rollback-readiness
paths.

`--execute` now constructs the real executable submission path: it creates
ordinary durable controller jobs in `service.db` through the repository job
factory (`ace_service.repository.create_job`), storing complete strict worker
v1/v2 envelopes for the compatibility smokes (full legacy generation, or v2
generation plus profile and resolved parameters) and validating every stored
envelope end-to-end through `ControllerWorker._default_payload` and the real
`runpod_worker.schemas.WorkerRequest` parser. The product UUID is preassigned
before either database commit and the campaign store persists a bounded
submission intent (sample ID, reservation ID, exact product UUID, and a
non-sensitive SHA-256 fingerprint of the frozen request) before the product
row exists; recovery accepts an existing product row only when its job type,
immutable normalized request, variation count, source semantics, and output
format match the intent exactly, so either crash order recovers one UUID job,
one campaign link, and one reservation and never submits remote work before
both durable records agree. Product job IDs remain distinct from the opaque
campaign sample IDs that key the campaign store; `mark_sample_submitted`
durably links each opaque sample to its UUID job ID. Each job is driven
through the controller's own `ControllerWorker.process_job` queue/transfer
machinery (Runpod client plus the signed-transfer path), waits for terminal
evidence before the next sample, and only then tears the window down at
provider-observed zero workers: `_teardown` fetches and validates the real
Runpod `/health` contract (workers `idle`/`running` and jobs `inQueue`/
`inProgress` as bounded non-negative integers, matching the documented
response; the obsolete `jobs.queued`/`jobs.running` shape is rejected) and
closes the window only with immutable, timestamped zero-at-rest evidence for
the authorized endpoint; malformed, unavailable, or nonzero evidence retains
the maintenance gate, and reaching Python `finally` never clears the gate by
itself.
The `--stage screening|confirmation` selector runs the matching stage;
confirmation covers the two new payable seeds and never resubmits the reused
screening-seed sample. `--cover-source-url` supplies the canonical fixture
source for cover jobs, and `--terminal-timeout-ms` bounds how long one job may
stay uncertain before the campaign fails closed. A failed, cancelled, or
uncertain attempt is never billed, even when worker evidence arrives later;
only completed attempts with authoritative GPU/execution/rate evidence receive
an immutable `estimated_compute` snapshot, and an unresolved reservation keeps
the teardown gate active until an operator reconciles it. While a window,
gate, or pending submission intent is open, `--status` and `--backup` remain
available and `--reconcile`/`--verified-teardown` (both confirmed) resume
frozen UUID-linked jobs and close the window on complete evidence; ordinary
score, advancement, decision, and execute actions are rejected until recovery
completes.

The screening seed is confirmation seed one. A confirmation case whose
exact fingerprint matches a completed screening sample reuses that sample
(recorded as an opaque alias) instead of paying to regenerate it; the alias
never creates a second reservation or submission. Reuse is refused for failed
or uncertain screening samples, for role/seed/stage mismatches, and for any
other executed fingerprint (contamination), so duplicate charging is
impossible.

Endpoint billing rows are stored as append-only observations. Runpod's endpoint
response does not contain a currency field, so the parser stores `USD` as the
explicit versioned source-contract value rather than as provider-returned data.
Network-volume rows without a volume identifier are retained as account-wide
evidence and are not presented or included as service spend. Provider totals
remain unavailable until fixtures and a read-only live probe prove the exact
half-open interval behavior.

Score-sheet exports contain only randomized opaque IDs, the anchored rubric,
and opaque pair choices, scoped per stage (`--stage screening|confirmation`).
Technical settings and the hidden mapping stay in the private campaign store.
Imports reject unknown/duplicate samples, altered rubrics, out-of-range
values, missing pair preferences, and partial finalization. Export and
finalization both require the complete generated-output evidence state: every
incumbent/candidate/corrected-controls sample of that stage must be terminal
with a recorded output path, so a planned, in-flight, failed, or output-less
sample rejects the sheet deterministically. Import and finalization also
enforce exact current scoreable coverage: the frozen sheet's `sample_order`
must equal the current scoreable sample-ID set and its pair memberships must
match the current candidate/incumbent pair structure. A scoreable sample
declared after export — even one that later completes — or any stale or
duplicate pair makes import and finalization reject the stale sheet, so a
partial decision set can never be frozen. The backup command refuses a
missing or just-created campaign database before any SQLite open, so a typo
cannot produce an empty backup.

The operator advances from screening to confirmation with the `--advance`
action after both screening sheets are finalized. It requires the fresh
explicit `--confirm` flag (without it the action returns the bounded blocked
exit before opening or mutating the campaign database) and accepts no finalist
input: per task type it derives the eligible candidates from the two finalized
sheets with `rank_screening_candidates` (complete sets only, severe-artifact
rule, deterministic candidate-ID ordering applied only after eligibility is
decided, and the exact cutoff-tie rule: a score-equivalence group that crosses
the two-finalist cutoff is excluded in its entirety, so a three-way tie for
first advances none, an exact two-way tie for first advances both, and a tie
spanning positions two and three advances only the untied first-place
candidate), persists the exact finalist set and rankings as a durable
`screening_advanced` event (an identical confirmed retry is idempotent; a
conflicting finalist set or ranking fails closed), materializes the
confirmation cases, and moves the campaign to
`awaiting_confirmation`. Seed-one confirmation cases reuse the completed
screening samples by exact fingerprint; seeds two and three are new payable
rows that `--execute --stage confirmation` submits through the durable
controller path while the aliases remain non-payable.

`--decision` validates both finalized stages, unblinds the complete matched
three-seed pairs (screening scores supply confirmation seed one), applies the
frozen promotion gate per finalist, and persists one immutable
`quality_decisions` record whose ID hashes the exact fixture, listener IDs,
seeds per task, profiles, models, sample fingerprints, and both sheet hashes.
Repeating the same finalization is idempotent; any conflicting decision for
the same campaign fails closed. The comparison code never changes
`fast-beta-v1` or any production default.

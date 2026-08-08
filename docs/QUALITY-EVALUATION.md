# Quality evaluation contract

This contract freezes the Checkpoint 1 evaluation inputs and scoring rules.
It is an operator-only, fixed-input comparison contract. It does not authorize
a paid Runpod campaign or change production defaults.

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

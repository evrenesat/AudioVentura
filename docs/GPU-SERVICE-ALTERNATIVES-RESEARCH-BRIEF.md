# Research brief: scale-to-zero GPU alternatives to Runpod

This is the historical research assignment that led to the SaladCloud work.
Current provider state is documented in `ARCHITECTURE.md` and `docs/SALAD.md`.

## Assignment

Research current GPU-as-a-service platforms that could replace Runpod for
AudioVentura's ACE-Step inference worker. Recommend a primary provider and one
fallback, supported by current official documentation and pricing.

The service is personal and used sporadically but continuously. Optimize first
for **no unavoidable recurring storage or idle-GPU cost**, then reliability,
observability, and execution cost. A long cold start is acceptable if the API
exposes enough truthful lifecycle information for the UI to explain the wait.
Do not treat a marketing claim of “serverless” as proof of scale-to-zero.

Date every price and platform-limit finding. Cite primary provider sources and
mark anything that requires sales/support confirmation.

## Workload to run unchanged where practical

- One Linux amd64 OCI worker image, pulled by immutable GHCR digest. Private
  registry authentication is required.
- Python 3.11, PyTorch 2.10 with CUDA 12.8, and revision-pinned ACE-Step v0.1.8.
- One public, immutable Hugging Face repository containing all model files at
  a pinned commit: about **25.25 GB** across a 4B DiT, 1.7B language model,
  0.6B embedding model, and VAE.
- Current worker image is large: approximately 7.9 GB compressed and 22.7 GB
  unpacked. Image optimization is possible, but report relevant image and
  ephemeral-disk limits rather than assuming it.
- One GPU per job, at least 20 GB VRAM; 24 GB or more is preferred. The exact
  GPU is flexible if it can load the bundle and generate correctly.
- Batch size 1. Requested music duration is 10–600 seconds; the initial target
  is roughly 60 seconds. A job may execute for up to 20 minutes.
- The worker downloads prepared source audio from a short-lived signed HTTPS
  URL and uploads output to another signed URL. It needs outbound HTTPS but no
  YouTube, SSH, home-network, or permanent object-store credentials.
- The controller needs asynchronous submit, status, result, and cancel APIs.
  Deployment must be programmable through an API, CLI, or stable IaC surface
  so Evreniops can pin, deploy, verify, and roll back exact revisions.

Changing the inference model, lowering output quality, requiring an always-on
GPU, or redesigning the entire application is out of scope for the initial
comparison. License/repackaging analysis is also out of scope for this private
personal deployment.

## Hard acceptance requirements

1. **Real zero-at-rest behavior:** no GPU instance when idle and no mandatory
   warm replica. List every fixed monthly charge, including endpoint,
   container, model-cache, volume, snapshot, registry, and minimum-spend fees.
2. **No dedicated persistent model volume preferred:** the provider must be
   able to fetch the pinned public Hugging Face bundle or use a shared/build
   cache. Explain cache scope, retention, eviction, regions, prewarming, and
   whether cached bytes incur recurring charges.
3. **Bounded billing:** identify whether image/model download, provisioning,
   initialization, queue time, and teardown are billed, and from which event
   GPU metering starts. Separate GPU, CPU, RAM, disk, egress, and request fees.
4. **Sufficient runtime limits:** at least a 20-minute execution timeout, a
   queue/request lifetime of at least two hours, enough ephemeral disk for the
   unpacked image, model bundle, temporary input, and output, and compatible
   NVIDIA drivers for the CUDA stack.
5. **Private immutable deployment:** exact OCI digest, secrets/environment
   configuration, outbound HTTPS, concurrency one, and safe scale-to-zero.
6. **Observable lifecycle:** programmatic worker/allocation state, queue state,
   initialization logs, container logs, timestamps, terminal error reason, and
   cancellation. State exactly what the API cannot reveal. Application-level
   progress callbacks alone do not solve an opaque pre-container wait.
7. **Capacity:** credible access to suitable 20–24+ GB GPUs without requiring
   a reserved instance. Explain regional constraints, quotas, stockouts, and
   whether selecting several GPU types improves allocation.

The observability requirement is material: on 22 August 2026 the current
Runpod endpoint remained queued for more than 45 minutes with zero workers,
even after its minimum-worker setting was temporarily raised. The provider API
reported only `IN_QUEUE`; it exposed neither model-cache transfer progress nor
a definitive allocation-block reason. An alternative does not need instant
cold starts, but it should let AudioVentura distinguish capacity wait, image
pull, model staging, container initialization, and model loading wherever the
platform actually knows those states.

## Required comparison

Evaluate at least five credible platforms. Include both managed inference
platforms and serverless-container GPU services where appropriate. Do not
include a platform in the final shortlist unless official sources confirm
custom-container support, a suitable GPU, and scale-to-zero—or clearly label a
required paid proof-of-concept when documentation is inconclusive.

For every candidate report:

- supported GPU types/VRAM and CUDA/driver constraints;
- true minimum replicas and all recurring minimum costs;
- cold-start architecture, immutable-model caching, cache charges, and
  expected behavior after long idle periods;
- maximum image size, ephemeral disk, queue lifetime, execution timeout,
  concurrency, cancellation, and retry semantics;
- API-visible phases, logs, metrics, webhooks/polling, and error detail;
- private GHCR and Hugging Face support;
- region/capacity behavior and quota onboarding;
- API/CLI/IaC maturity and migration implications for the existing controller;
- current per-second/minute price for each usable GPU plus non-GPU charges;
- estimated cost of one cold 60-second generation and ten sporadic generations,
  showing assumptions and billed cold-start time separately.

Use a compact comparison table, followed by evidence and caveats that do not
fit in the table. Explicitly reject attractive-looking options that fail a
hard requirement.

## Deliverable and recommendation

Return:

1. A ranked shortlist with one recommended provider and one fallback.
2. A plain-language explanation of why each beats or fails Runpod for this
   particular sporadic workload—not a generic GPU-cloud overview.
3. A migration sketch identifying controller adapter changes, worker/image
   changes, deployment automation, secrets, status mapping, and rollback.
4. A small paid proof-of-concept plan for unresolved claims. Propose the exact
   test count and maximum budget, but do not create accounts or spend money.
5. A list of unknowns phrased as concrete provider support questions.

The proof of concept should ultimately demonstrate one fresh YouTube-cover job
and one continuation from its existing generated file. The continuation must
reuse the local output and must not contact YouTube. Record queue, allocation,
image/model staging, initialization, inference, upload, teardown, zero-at-rest
evidence, wall time, billed time, and cost. Verify generated duration, output
download, cancellation/error handling, and behavior after a genuinely cold
idle period.

## Decision rule

Prefer the service with the lowest realistic annual cost for sporadic use that
still provides truthful lifecycle evidence and repeatable deployment. Faster
warm execution is secondary. A slower service with zero fixed monthly storage
and strong allocation/startup observability may outrank a faster but opaque or
region-pinned service.

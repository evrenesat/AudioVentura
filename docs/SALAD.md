# SaladCloud

SaladCloud is the first alternate inference backend. The controller submits
schema-2 metadata to a Salad Job Queue. A scale-to-zero container group runs
the same ACE-Step handler used by Runpod.

## Current deployment status

The image, queue, and container group are deployed. The earlier conclusion
that a corrected job received no allocation for 54 minutes was wrong: the
instances endpoint stayed empty, but system logs later proved allocation,
image download, and `Instance Starting` at 09:58. Operator cancellation at
10:01 was premature.

Live acceptance is still in progress and is not yet complete. The current
manual workflow keeps one worker available across an interactive cover and its
continuation, then explicitly restores zero-at-rest after all queue work is
terminal.

## Runtime shape

```text
Controller
    | Salad Job Queue API
    v
Salad queue
    | queue autoscaler: 0 -> 1
    v
GPU container
    | Salad HTTP Job Queue Worker
    v
local worker_api.py -> shared ACE-Step handler
```

`deploy/salad/worker_api.py` accepts the queue worker's direct JSON body and
wraps it for the shared handler. `/ready` remains unavailable until CUDA and
all models are initialized. The queue worker starts only after readiness
passes.

Audio still uses controller-issued HTTPS capabilities. The Salad queue request
and response contain metadata only.

## Immutable image receipt

```text
Base worker digest:
  sha256:0310fef73053113f0060bc4861e7b682156f375159386473356a4dc4c9850846

Model repository:
  evrenesat/audioventura-ace-step-v0.1.8
Model revision:
  88b8c7fa089446b53382c1040037492463430bed
Model bundle tag:
  av-v0.1.8-bundle-2
Model manifest SHA-256:
  39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc
Model inventory:
  29 files, 25,253,680,505 bytes

Salad queue worker:
  v0.7.0
Deployable amd64 image digest:
  sha256:16d09990275aa9e261d427be48817c035ceddc0ea75a18498d62a74abdacbf53
Compressed image size:
  28,979,331,889 bytes
```

The model bundle is a stable image layer. Salad downloads container layers
before the runtime starts, avoiding a separate model download after startup.
The image remains below Salad's 35 GB compressed-image limit.

## Build

Use BuildKit and push directly. Do not use `--load`; importing this image into
the classic Docker store duplicates tens of gigabytes locally.

```text
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --push \
  -f deploy/salad/Dockerfile \
  -t ghcr.io/evrenesat/audioventura-ace-step-salad-worker:<immutable-tag> .
```

Resolve the amd64 manifest digest and sum its layer sizes. Do not provision an
image larger than 35,000,000,000 compressed bytes.

## Desired infrastructure

`deploy/salad/deployment.json` is the tracked input for:

- queue `audioventura-jobs`;
- container group `audioventura-ace-step-v2`;
- 8 vCPU, 32 GiB RAM, and 8 GiB shared memory;
- compatible 24+ GiB GPU classes;
- startup, readiness, and liveness probes;
- medium priority;
- zero minimum and one maximum replica;
- desired queue length 1 and 15-second autoscaler polling.

Infrastructure management stays outside the provider interface.
`deploy/salad/saladctl.py` can inspect or apply this desired state. `apply` is
idempotent for a matching deployment, stops on drift, and does not create queue
jobs or delete resources automatically.

For the initial manual-only interactive workflow, start a capacity session
before submitting work:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
uv run python deploy/salad/saladctl.py session-start \
  --organization <organization> \
  --project <project>
```

`session-start` verifies the exact tracked queue and group, preserves the full
tracked queue-autoscaler configuration except for `min_replicas=1`, and sets
desired replicas to one. It does not require registry credentials and does not
change the image, priority, GPU/resources, probes, or queue jobs. Wait for the
worker to become ready, then submit the cover and any continuation while the
session remains active.

After every recent queue job is terminal and queue length is exactly zero,
restore zero-at-rest:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
uv run python deploy/salad/saladctl.py session-stop \
  --organization <organization> \
  --project <project>
```

`session-stop` refuses to mutate capacity when the queue or its bounded recent
job list cannot prove there is no pending/running work. On success it restores
the tracked `min_replicas=0` and desired replicas zero. Both session commands
are idempotent and return only bounded, secret-free status. A future automated
idle lease belongs in deployment/capacity management, not in
`InferenceProvider`.

Inspect first:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
uv run python deploy/salad/saladctl.py inspect \
  --organization <organization> \
  --project <project>
```

Apply only a reviewed immutable digest:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
GHCR_USERNAME=<username> \
GHCR_TOKEN=<read-package-token> \
uv run python deploy/salad/saladctl.py apply \
  --organization <organization> \
  --project <project> \
  --image-ref ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:<digest>
```

The API key and private-registry credentials are environment-only values. Do
not place them in Git, shell output, image layers, or incident records.

## Controller configuration

Set:

```text
INFERENCE_PROVIDER=salad
SALAD_API_KEY=<secret>
SALAD_ORGANIZATION=<organization>
SALAD_PROJECT=<project>
SALAD_QUEUE_NAME=audioventura-jobs
SALAD_CONTAINER_GROUP_NAME=audioventura-ace-step-v2
```

Keep working Runpod credentials during the rollback window so persisted Runpod
jobs remain reconcilable.

Pending Salad jobs can be cancelled. Running jobs report `too_late`. Salad
404s are not terminal by assumption. The controller retains and polls the same
durable queue UUID until terminal evidence or its deadline policy resolves the
attempt.

While a job is pending, or while a Salad retry still reports the queue job as
`running`, the provider first inspects the single container-group instance. If
that endpoint is empty or unusable, it may use only a bounded, post-job system
lifecycle event as a fallback. Allocation, image download, and startup remain
deployment-level inferred status. A ready instance restores job-scoped
`RUNNING`; terminal states are never enriched. Image pull progress is shown
only when the instances endpoint supplies a valid fraction; logs never invent
a percentage or readiness.

## Verification

Run the focused local checks:

```text
uv run pytest -q tests/test_salad_worker.py tests/test_salad_infra.py \
  tests/test_salad_provider.py
uv run ruff check deploy/salad tests/test_salad_worker.py \
  tests/test_salad_infra.py tests/test_salad_provider.py
uv run ruff format --check deploy/salad tests/test_salad_worker.py \
  tests/test_salad_infra.py tests/test_salad_provider.py
uv run mypy --follow-imports=skip deploy/salad
shellcheck deploy/salad/entrypoint.sh
```

Complete the current live acceptance before claiming Salad ready: record queue
delay, allocation, image download, startup, readiness, inference, upload,
continuation behavior, completion, and the explicit return to zero replicas.
Confirm the output digest and immutable runtime identities.

# SaladCloud deployment boundary

SaladCloud is the first alternate inference backend. The controller provider
registry submits metadata-only schema-v2 requests to Job Queues. This module
owns only the Salad image, local queue-worker adapter, and infrastructure
desired state; provider submission and durable job ownership stay in the
controller.

## Immutable image receipt

- Base worker: `ghcr.io/evrenesat/audioventura-ace-step-worker@sha256:0310fef73053113f0060bc4861e7b682156f375159386473356a4dc4c9850846`
- Model repo: `evrenesat/audioventura-ace-step-v0.1.8`
- Model commit: `88b8c7fa089446b53382c1040037492463430bed`
- Model tag: `av-v0.1.8-bundle-2`
- Model manifest SHA-256: `39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc`
- Checkpoint inventory: 29 files, 25,253,680,505 bytes
- Salad HTTP Job Queue Worker: `v0.7.0`, archive SHA-256
  `074a329cf6462e77fc7b72100f59d8a690831456d9420186a834a8f30634c9e4`
- Private GHCR tag: `ghcr.io/evrenesat/audioventura-ace-step-salad-worker:infra-b46ab36-20260823`
- Deployable amd64 digest: `sha256:16d09990275aa9e261d427be48817c035ceddc0ea75a18498d62a74abdacbf53`
- Image config: `sha256:aec1c56ea33dd071a888508dbe5dc2b5ce7d5ae254199c083a96ee38527faa1c`
- Compressed amd64 layers: 20 layers, 28,979,331,889 bytes
- Salad 35 GB margin: 6,020,668,111 bytes

The image keeps model transfer in an immutable layer so Salad performs it
before billed container execution. `deploy/salad/worker_api.py` converts the
Job Queue Worker's direct JSON request body into the existing schema-v2
`{"input": ...}` handler event. `/ready` stays unavailable until CUDA and all
ACE-Step models initialize; the queue worker starts only after that endpoint
passes. Large audio still uses the controller's signed HTTPS capabilities and
the Salad job response contains metadata only.
`ACE_WORKER_IMAGE_DIGEST` carries the bare immutable manifest digest (without
the registry/repository prefix), matching the controller's runtime identity
and calibration contract.

## Build and size verification

Use BuildKit and push directly; do not use `--load` for this image because a
classic Docker import duplicates tens of gigabytes locally.

```text
docker buildx build --platform linux/amd64 --provenance=false --push \
  -f deploy/salad/Dockerfile \
  -t ghcr.io/evrenesat/audioventura-ace-step-salad-worker:<immutable-tag> .
```

Resolve the pushed manifest and sum `.layers[].size`. Require a conservative
decimal total no greater than 35,000,000,000 bytes before provisioning.

## Desired remote state

`deploy/salad/deployment.json` defines queue `audioventura-jobs` and container
group `audioventura-ace-step-v2` with low priority, 8 vCPU, 32 GiB RAM,
compatible 24+ GiB GPU classes, startup/readiness/liveness probes, and:

```text
replicas=0
min_replicas=0
max_replicas=1
desired_queue_length=1
polling_period=15
```

The Salad API key is read only from `SALAD_API_KEY`. Private GHCR credentials
are read only from `GHCR_USERNAME` and `GHCR_TOKEN` during creation; never put
them in Git, command output, or the image. Organization and project slugs are
explicit because Salad's public API cannot enumerate them from an API key.

Inspect before applying:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
uv run python deploy/salad/saladctl.py inspect \
  --organization <organization> --project <project>
```

Apply only the reviewed amd64 digest. The command creates no queue jobs:

```text
SALAD_API_KEY="$(< /root/salad_api_key)" \
GHCR_USERNAME=<username> GHCR_TOKEN=<read-package-token> \
uv run python deploy/salad/saladctl.py apply \
  --organization <organization> --project <project> \
  --image-ref ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:16d09990275aa9e261d427be48817c035ceddc0ea75a18498d62a74abdacbf53
```

`apply` is idempotent when the existing queue and container group match the
tracked desired state. It stops on drift and never replaces or deletes a
resource automatically; the write-only GHCR registry password is the sole
field that cannot be read back for comparison.

After creation, require the exact image digest/config, `replicas=0`, an empty
queue, no instances, and no pending change before controller deployment. A
cold live job is the first authorized action that should scale the group to
one; after completion, observe the queue empty and the group return to zero.

## Controller contract

Set `INFERENCE_PROVIDER=salad` plus `SALAD_API_KEY`, `SALAD_ORGANIZATION`, and
`SALAD_PROJECT`. Keep valid Runpod credentials during the rollback window so
old Runpod references remain reconcilable after the default changes.

Pending jobs can be cancelled. Running jobs return `too_late`, and the
controller keeps polling the same durable UUID. Salad 404 responses are never
terminal by assumption. Status uncertainty retains the exact job, transfers,
source, progress, and provider reference; provider submission is never retried.

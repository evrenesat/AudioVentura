# Runpod

Runpod is one implementation of AudioVentura's inference-provider contract.
`runpod_worker/` contains the isolated ACE-Step runtime and Runpod Serverless
entry point.

## Worker image

Build the amd64 image from the repository root and push an immutable tag:

```text
docker build --platform linux/amd64 \
  -f runpod_worker/Dockerfile \
  -t <registry>/audioventura-ace-step-worker:<immutable-tag> .

docker push <registry>/audioventura-ace-step-worker:<immutable-tag>
```

Resolve the pushed digest and deploy by digest. Set `ACE_WORKER_IMAGE_DIGEST`
inside the worker to that immutable digest. The controller's
`RUNPOD_WORKER_RUNTIME_IDENTITY` must identify the same release.

The image contains pinned ACE-Step v0.1.8 source and runtime dependencies. It
does not contain model weights. The endpoint selects the exact aggregate
Hugging Face repository revision through Runpod cached models. Runpod exposes
that platform cache at:

```text
/runpod-volume/huggingface-cache/hub
```

Startup requires the configured model repository, 40-character revision,
bundle tag, manifest SHA-256, and complete 25.25 GB file inventory. Missing or
changed files fail initialization. Offline mode is enabled; the worker does
not download models or fall back to another checkpoint path.

Do not attach a customer network volume to the endpoint. Network volumes pin
workers to one data center even though the cached-model path is also rooted at
`/runpod-volume`.

## Endpoint settings

The intended personal scale-to-zero shape is:

```text
workersMin: 0
workersMax: 1
gpuCount: 1
GPU memory: at least 24 GB
GPU choices: RTX 5090, RTX 4090, L4, RTX A6000
idleTimeout: 30 seconds
executionTimeout: 1200 seconds
FlashBoot: enabled when available
```

Set `ACE_TRANSFER_ALLOWED_HOST` to the exact public transfer hostname. The
worker rejects signed source and output URLs for another host.

Do not add controller, Home Ingest, YouTube, SFTP, SSH, Tailscale, or Runpod
submission credentials to the worker environment.

## Request contract

The provider API receives JSON metadata only. Current requests use worker
schema 2; schema 1 remains accepted for old persisted jobs.

A request contains:

- UUID application job and submission identities;
- variation index and bounded generation controls;
- resolved prompt, model, duration, seed, and output settings;
- an output-upload capability;
- for covers, a source-download capability plus source bytes and SHA-256.

Unknown fields and schema versions are rejected. Each request forces
`batch_size=1` and produces exactly one output.

The result contains bounded metadata such as effective prompt/lyrics, resolved
parameters, seed, duration evidence, model identity, image digest, GPU, and
available Runpod timing. It never contains audio bytes, private paths,
capability URLs, tensors, or debug payloads.

## Audio handling

For a cover, the worker downloads the prepared MP3 to a private temporary
directory and verifies its size and SHA-256 before inference. Original jobs do
not download a source.

WAV and FLAC use ACE-Step's matching save format. MP3 is encoded at 192 kbps
with pinned `lameenc` from a temporary PCM WAV. The worker image does not use
`ffmpeg` or `ffprobe`.

The worker uploads with `Content-Length`, byte-count, and SHA-256 headers. All
temporary source and output files are removed after success or failure.

## Controller behavior

`RunpodProvider` translates Runpod queue states into the shared lifecycle. It
may use endpoint health to report that a worker is initializing. A worker
progress phase can describe source download, generation, finalization, or
output upload.

For a managed Runpod backend, capacity retention must report an idle ready
worker before the controller commits the submission nonce. Warming capacity is
a transient retry and does not consume the provider job TTL. A zero keep-warm
setting releases capacity after work; it does not bypass pre-submission
readiness.

Before `/run`, the controller commits a submission nonce. It stores the
returned Runpod ID immediately. A nonce without an ID is uncertain and is not
resubmitted. An existing ID always resumes polling.

Pending cancellation is supported. Running cancellation is treated as too
late. A not-found result becomes terminal only under the controller's bounded
Runpod recovery rules; it is not permission to submit another job.

## Automated keep-warm lease

The managed capacity adapter is enabled only with the reviewed
`RUNPOD_CAPACITY_EXPECTED_FINGERPRINT`. Its fingerprint covers the exact
endpoint identity, GPU count, and immutable deployment contract. Read-only
inspection refuses drift, `workersMax` values other than one, active provider
work, or more than one observed worker.

The lease changes only `workersMin`: retain is a partial PATCH to one and
release is a partial PATCH to zero. It never changes `workersMax`, GPU
selection, image, timeout, or endpoint deployment settings. A lost PATCH
response is resolved by inspection, not a second generation submission. The
controller waits for observed zero workers/jobs after the desired-zero write;
nonzero capacity after the bounded grace period is durable `release_overdue`
state and degraded readiness.

Before enabling the adapter, perform the documented read-only preflight and a
partial-PATCH canary only while the endpoint has no queue or active jobs. Keep
the existing RunPod configuration repair tool available for emergency recovery.

## Verification

The local worker contract suite does not require CUDA or model weights:

```text
uv run pytest -q runpod_worker/tests
uv run ruff check runpod_worker
uv run ruff format --check runpod_worker
```

After an image, model snapshot, endpoint, or transfer change, run a cold live
acceptance job and verify:

1. the expected image and model identities are reported;
2. model initialization completes without network download;
3. one original and one prepared cover complete;
4. uploaded bytes and SHA-256 agree with controller records;
5. queue and execution timing are recorded when Runpod supplies them;
6. the endpoint returns to zero workers after the idle timeout.

Keep API keys, source URLs, and capability URLs out of the acceptance record.

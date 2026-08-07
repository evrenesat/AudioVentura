# Runpod ACE-Step worker

Checkpoint #2 packages a queue-based Serverless Flex worker around the official
ACE-Step v0.1.8 source baseline. The tag resolves to commit
`dce621408bee8c31b4fcf4811682eb9359e1bc94` in `ace-step/ACE-Step-1.5`.

## Image and model layout

Build from the repository root with an amd64 builder:

```text
docker build --platform linux/amd64 -f runpod_worker/Dockerfile -t <registry>/ace-step-worker:cp2 .
docker push <registry>/ace-step-worker:cp2
```

The image contains the pinned ACE-Step code, CUDA runtime dependencies, the
Runpod SDK, and the worker package at `/opt/runpod_worker`. The Docker command
starts it with `python -m runpod_worker.handler`, so all worker imports use the
package layout. The pinned checkout's vendored
`acestep/third_parts/nano-vllm` package is installed alongside ACE-Step in the
same pip dependency-resolution command; no unrelated or unpinned `nano-vllm`
distribution is used. The image deliberately does not download model weights
during a cold start. Configure `ACE_WORKER_CHECKPOINTS_DIR` to the
endpoint cached-model path or a Runpod network volume containing these
directories:

```text
<checkpoints>/
  acestep-v15-xl-turbo/
  acestep-5Hz-lm-1.7B/
  Qwen3-Embedding-0.6B/
  vae/
```

The worker checks for weight artifacts in every directory before importing the
ACE-Step handlers. Missing or incomplete checkpoints fail initialization; the
worker never falls back to CPU inference or repeated per-request downloads.

## Endpoint settings

Use a queue endpoint with the first-release limits:

```text
workersMin: 0
workersMax: 1
gpuCount: 1
GPU: NVIDIA GeForce RTX 4090 24 GB
idleTimeout: 30 seconds
executionTimeout: 1200 seconds
FlashBoot: enabled
```

Set `ACE_TRANSFER_ALLOWED_HOST` to the hostname of the public HTTPS transfer
app. When it is set, both source and output capabilities must use that exact
host. The worker accepts no credentials for Runpod, SFTP, SSH, Tailscale, or
YouTube.

## Request and result boundary

Runpod receives a small JSON object with `schema_version`, UUID-like job and
submission identifiers, one bounded generation description, an optional
prepared-source capability, and one output-upload capability. Capability URLs
must be HTTPS and use `/transfer/v1/`.

For a cover job, the worker streams the prepared MP3 to a private temporary
file, verifies its declared byte count and SHA-256, and passes that local file
to ACE-Step. Original jobs do not fetch a source. Both paths construct ACE-Step
parameters in the handler but use the process-global model objects initialized
before `runpod.serverless.start()`.

Every request forces `GenerationConfig.batch_size=1`. The worker requires
exactly one generated file, streams it to the signed output capability with
`Content-Length`, `X-ACE-Output-Bytes`, and `X-ACE-Output-SHA256`, and removes
all source/output temporary files in a temporary-directory scope. The Runpod
result contains only identifiers, output size/checksum/format, controlled
seed/sample-rate metadata, and non-secret worker GPU metadata; it never
contains audio bytes, base64 audio, a local path, or a permanent media URL.

The worker accepts `mp3`, `flac`, and `wav` output requests. WAV and FLAC use
the corresponding ACE-Step save format. For MP3, ACE-Step saves a temporary
48 kHz PCM WAV and the worker uses the pinned `lameenc==1.8.4` library to
encode 192 kbps MP3 in-process before upload. The image contains no `ffmpeg`
or `ffprobe` executable; source-media `yt-dlp`/`ffprobe`/`ffmpeg` work remains
exclusive to home-ingest, and Hetzner performs no media processing.

## Local verification

The mocked contract suite runs without CUDA or ACE-Step weights:

```text
uv run pytest -q runpod_worker/tests
uv run ruff check runpod_worker
uv run ruff format --check runpod_worker
```

GPU acceptance remains a deployment check: cold start the worker, generate
20-second and 180-second originals, generate one prepared-MP3 cover, record
initialization/queue/execution/peak-VRAM metrics, and confirm the endpoint
returns to zero workers after idle timeout.

## Deployment acceptance record

Fill this table only after live testing. The local contract tests do not prove
GPU capacity, network transfer, billing, or scale-to-zero behavior.

| Value | Recorded result |
|---|---|
| Endpoint ID | pending live deployment |
| Worker image digest | pending live deployment |
| ACE-Step tag | `v0.1.8` / pinned commit above |
| DiT / LM models | `acestep-v15-xl-turbo` / `acestep-5Hz-lm-1.7B` |
| GPU selection | RTX 4090 24 GB target; record actual |
| Cold start / initialization | pending live measurement |
| 20-second original | pending live measurement |
| 180-second original | pending live measurement |
| Representative cover | pending live measurement |
| 30-second idle cost | pending live measurement |
| Peak VRAM | pending live measurement |

For each live job also record UTC time, job type, song/source duration, cold or
warm state, queue delay, execution time, generated byte size, and approximate
cost. Keep API keys, capability URLs, and source URLs out of this record.

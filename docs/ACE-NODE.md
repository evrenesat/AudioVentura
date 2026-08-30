# ACE Node

ACE Node is a separately deployed, persistent provider for
`node/ace-step-v15-xl-turbo`. It runs the existing strict schema-2
`runpod_worker` handler on exactly one Linux x86_64/NVIDIA CUDA GPU or one
Apple Silicon arm64/MPS+MLX machine. It is not a cloud provisioner, a public
endpoint, or a CPU fallback.

The controller sends bounded metadata and short-lived signed source/output
capabilities over a private authenticated HTTP connection. Audio bytes move
only through the existing `player.evren.io` transfer capabilities. The node
keeps job identity and terminal metadata in SQLite, while full worker input,
capability URLs, and active result details remain memory-only.

## Configuration

Create one mode-600 environment file outside Git. Set every placeholder to a
host-specific value; do not copy secrets into this repository:

```text
ACE_NODE_CHECKOUT=/opt/audioventura
ACE_NODE_VENV=/opt/audioventura-ace-node/venv
ACE_NODE_APPLICATION_REVISION=<40-character-committed-revision>
ACE_NODE_LISTEN_HOST=127.0.0.1
ACE_NODE_LISTEN_PORT=8210
ACE_NODE_TOKEN=<long-random-bearer-token>
ACE_NODE_DATA_ROOT=/var/lib/audioventura/ace-node
ACE_NODE_ACCELERATOR=auto
ACE_TRANSFER_ALLOWED_HOST=player.evren.io
ACE_WORKER_HF_CACHE_ROOT=/var/lib/audioventura/ace-node/huggingface-cache
ACE_WORKER_MODEL_REPO=evrenesat/audioventura-ace-step-v0.1.8
ACE_WORKER_MODEL_REVISION=88b8c7fa089446b53382c1040037492463430bed
ACE_WORKER_MODEL_TAG=av-v0.1.8-bundle-2
ACE_WORKER_MODEL_MANIFEST_SHA256=39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc
ACE_NODE_JOB_TIMEOUT_SECONDS=1800
ACE_NODE_MAX_OUTPUT_BYTES=268435456
```

The node token is independent of controller and provider credentials. The
controller uses the same private base URL and token through
`ACE_NODE_BASE_URL`, `ACE_NODE_TOKEN`, and the four `ACE_NODE_*_TIMEOUT_SECONDS`
settings. Leave `node/ace-step-v15-xl-turbo` out of
`INFERENCE_ENABLED_BACKENDS` until a real machine has passed the hardware
acceptance gate. Node is disabled by default.

The exact model bundle and ACE-Step source are fixed by the values above and
by commit `dce621408bee8c31b4fcf4811682eb9359e1bc94`. The local deployment
receipt is derived from the committed application revision and `uv.lock`; a
dirty checkout, branch name, `latest` tag, or missing receipt is rejected.

## Common preparation

On the target machine, install the host driver/runtime first, create the
external virtual environment, and install the opt-in dependency group:

```text
cd /opt/audioventura
UV_PROJECT_ENVIRONMENT=/opt/audioventura-ace-node/venv uv sync --group node --python 3.12
```

Use the target environment's Python path as `ACE_NODE_VENV`; it must not be
the controller's normal environment. Export a read-only Hugging Face token
only for preparation, then remove it from the shell:

```text
export HF_TOKEN=<deployment-managed-token>
/opt/audioventura-ace-node/venv/bin/python -m ace_node.model_bundle prepare
unset HF_TOKEN
```

Preparation downloads the pinned snapshot, validates the exact 29-file
manifest and aggregate byte count, and leaves no token in node SQLite or
worker metadata. Startup is offline after the validated cache is present.

Check authenticated readiness from the controller host or the node itself:

```text
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${ACE_NODE_TOKEN}" \
  "http://127.0.0.1:8210/healthz"
```

Health remains available as `initializing`, `ready`, or `failed`. Submissions
are accepted only at `ready`; one job runs at a time, pending jobs may be
cancelled, and running work is never force-killed. On process recovery,
queued/running rows become `failed` with `worker_restarted` and are never
automatically resubmitted.

## Linux NVIDIA host

Use Linux x86_64 with one visible NVIDIA GPU. Verify the host before model
preparation and refuse to proceed if CUDA is unavailable or more than one GPU
is visible:

```text
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
/opt/audioventura-ace-node/venv/bin/python -c \
  "import torch; assert torch.cuda.is_available() and torch.cuda.device_count() == 1"
```

Set `ACE_NODE_ACCELERATOR=cuda` for an explicit cloud/rental deployment, or
leave `auto` to select CUDA on this platform. Bind the service to loopback or
the exact private Tailscale address; do not open port 8210 globally. The node
does not provision, wake, scale, or delete a rented GPU.

After substituting the unit placeholders with the clean checkout, service
user/group, private data/cache paths, and the same environment-file path:

```text
sudo install -o root -g root -m 0644 \
  deploy/node/linux/audioventura-ace-node.service \
  /etc/systemd/system/audioventura-ace-node.service
sudo systemctl daemon-reload
sudo systemctl enable --now audioventura-ace-node
sudo systemctl status audioventura-ace-node
sudo journalctl -u audioventura-ace-node -f
sudo systemctl stop audioventura-ace-node
```

For rollback, stop the unit, check out an exact reviewed commit in a clean
directory, rebuild the external node environment if its lock changed, update
`ACE_NODE_APPLICATION_REVISION`, and start the unit again. Remove the node
backend from controller selection first; retain credentials until all persisted
node jobs are terminal. Never delete historical node rows or outputs.

## macOS Apple Silicon host

Use macOS arm64 on Apple Silicon. Verify MPS and the MLX packages in the
opt-in environment; Intel Macs are rejected:

```text
uname -m
/opt/audioventura-ace-node/venv/bin/python -c \
  "import mlx, mlx_lm, torch; assert torch.backends.mps.is_available()"
```

Set `ACE_NODE_ACCELERATOR=mps`. The runtime selects `device=mps`, the MLX LM
backend, and the MLX DiT path with no CPU offload or torch compile. Keep the
Mac awake and powered for the manually managed service; launchd does not
prevent sleep and no desktop-control automation is part of this service.

After replacing the plist paths with the clean checkout, absolute env file,
and private log directory:

```text
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/io.evren.audioventura.ace-node.plist"
launchctl print "gui/$(id -u)/io.evren.audioventura.ace-node"
launchctl kickstart -k "gui/$(id -u)/io.evren.audioventura.ace-node"
launchctl bootout "gui/$(id -u)/io.evren.audioventura.ace-node"
```

The commands above are operator instructions only; this repository change
does not install or control launchd. Tailscale binding and rollback follow the
Linux rules. A Mac may be unavailable or asleep, so controller readiness and
job timeout settings must reflect the owner's operating schedule.

## Acceptance gate

Before enabling the backend, manually verify one original and one source-audio
job, a warm second job, authenticated health, output size/hash/duration, and a
controlled restart. Confirm the runtime receipt, model manifest identity,
serial execution, pending cancellation, and `worker_restarted` recovery. Keep
the node disabled in beta and production until an actual healthy host passes
that gate and the owner tests the beta controller.

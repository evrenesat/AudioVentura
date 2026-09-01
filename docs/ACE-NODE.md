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
ACE_NODE_RUNTIME_LOCK_PATH=/opt/audioventura/deploy/node/uv.lock
ACE_NODE_LISTEN_HOST=127.0.0.1
ACE_NODE_LISTEN_PORT=8210
ACE_NODE_TOKEN=<long-random-bearer-token>
ACE_NODE_SUPERVISOR_TOKEN=<different-long-random-drain-token>
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
receipt is derived from the committed application revision and
`deploy/node/uv.lock`; a dirty checkout, branch name, `latest` tag, or missing
receipt is rejected. The controller's root `uv.lock` intentionally excludes
the heavyweight node graph so its web/runtime dependencies remain isolated.

## Common preparation

On the target machine, install the host driver/runtime first, create the
external virtual environment, and install the separate node deployment
project:

```text
cd /opt/audioventura
UV_PROJECT_ENVIRONMENT=/opt/audioventura-ace-node/venv \
  uv --project deploy/node sync --frozen --python 3.12
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

### Native menu-bar app

The preferred Apple Silicon path is the native app under
`deploy/node/macos/`. It is an arm64-only menu-bar utility with product name
`AudioVentura ACE Node`, bundle ID `io.evren.audioventura.ace-node`, executable
`AudioVenturaACEWorker`, and minimum macOS 14.0. A development build is
ad-hoc signed and local to this Mac. A downloadable release requires the
Developer ID, notarization, and Gatekeeper checks described below.

Build from a clean committed checkout:

```text
cd deploy/node/macos
./build-runtime.sh --relocatable       # requires at least 30 GiB free
./build-app.sh --release
./build-dmg.sh --release
./notarize-dmg.sh \
  ../../../dist/macos/AudioVentura-ACE-Node-<version>-arm64.dmg
./verify-release.sh \
  ../../../dist/macos/AudioVentura-ACE-Node-<version>-arm64.dmg
```

For a source-only local smoke build, use `./build-app.sh --development`; it
does not download the runtime. Install a completed notarized DMG by dragging
`AudioVentura ACE Node.app` to `/Applications`. The DMG contains no model
weights. The first launch opens Setup when no validated receipt exists. Enter
the read-only Hugging Face token in the SecureField, keep at least 55 GiB free
(70 GiB recommended), and choose Download and verify. Preparation is resumable
and writes a setup receipt only after the pinned 29-file, 25,253,680,505-byte
manifest validates. The token is cleared and never written to disk.

The menu item and popover show `Setup required`, initialization phases,
`Ready`, running/queued counts, `Model unloaded`, `Draining`, `Remote
unavailable`, or `Failed`.
`Restart Worker (drain)` waits for the queue to empty before replacing the
child. `Force Restart` is explicit and records queued/running work as
`worker_restarted` through normal worker recovery; it is not an automatic
resubmission. Settings can select a 1-240 minute model idle timeout, defaulting
to 15 minutes. Applying it drains active work before restarting the child; the
node then unloads model memory after that idle period and reloads automatically
for the next job. Launch at Login is opt-in and uses `SMAppService.mainApp`.

The app derives these private paths with `FileManager`:

```text
~/Library/Application Support/AudioVentura/ACE Node/node.sqlite3
~/Library/Application Support/AudioVentura/ACE Node/state/{setup,worker}.json
~/Library/Application Support/AudioVentura/ACE Node/models/<revision>/
~/Library/Caches/AudioVentura/ACE Node/model-download/
~/Library/Logs/AudioVentura/ACE Node/ace-node.log
```

Tailscale discovery uses only the fixed executable locations in the source,
requires `TAILSCALE_BE_CLI=1`, and accepts exactly one `100.64.0.0/10` IPv4
address. The worker never falls back to loopback or a wildcard bind. If
Tailscale is temporarily unavailable after launch, the app keeps the worker
resident and shows Remote unavailable; a changed valid address is adopted only
after the old worker drains successfully. Logs rotate at 10 MiB with five
retained files and private permissions.

For an update, finish or stop current work, install the newer app, and let it
download into a new versioned model directory. The old validated directory is
kept until the new one is ready; remove older versions only through the
explicit Setup action. For rollback, disable the node backend in beta, stop
the worker, reinstall the prior app, and retain the node database and model
directories until controller jobs and output evidence are terminal. Uninstall
removes only `/Applications/AudioVentura ACE Node.app`; state and models are
separate explicit cleanup actions.

## Acceptance gate

Before enabling the backend, manually verify one original and one source-audio
job, a warm second job, authenticated health, output size/hash/duration, and a
controlled restart. Confirm the runtime receipt, model manifest identity,
serial execution, pending cancellation, and `worker_restarted` recovery. Keep
the node disabled in beta and production until an actual healthy host passes
that gate and the owner tests the beta controller.

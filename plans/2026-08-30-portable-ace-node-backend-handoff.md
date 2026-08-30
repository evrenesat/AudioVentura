# Portable ACE Node backend handoff

Date: 2026-08-30 UTC

Status: ready for implementation

## Objective

Replace AudioVentura's unusable interactive dependency on cold serverless GPU
allocation with one persistent, provider-neutral ACE Node service. The node
must run the existing pinned ACE-Step worker contract on either:

- Linux x86_64 with one NVIDIA CUDA GPU; or
- macOS arm64 on Apple Silicon with MPS/MLX.

The controller continues to exchange metadata and signed transfer
capabilities only. The node downloads source audio through a signed capability,
runs ACE-Step outside the controller, uploads the result through a signed
capability, and returns bounded metadata. The same node service can therefore
run on a user's Mac, a local Linux workstation, or a manually started persistent
rented Linux GPU without another provider adapter.

## Evidence and diagnosis

### Live application evidence

At 2026-08-30 19:57 UTC:

- beta was deployed at product revision
  `f0d76b58e4c58f0762b8737763974cfb21b27279`;
- production remained unchanged at
  `c16d140dd4616296e25e5a1203b2d35f4dbdc96c`;
- beta job `5d99315e-2534-4a04-8165-98d116cba772` was cancelled after
  63 minutes on Salad without entering inference;
- beta job `c71b3893-6ce8-4713-b5fd-361d837f8dba` failed after exactly
  two hours on RunPod with `provider_job_expired`;
- both attempts had null GPU, model identity, runtime identity, and execution
  time, proving that neither reached the ACE-Step handler;
- the focused provider/capacity/worker suite passed `191` tests, so the
  application-side contracts reproduce independently of live allocation.

The warm-path evidence is materially different: the existing Salad acceptance
record reports a 60-second cover in about 12 seconds and a continuation in about
10 seconds on an RTX 3090. Generation is fast enough for interactive use once a
runtime is resident.

### Salad evidence

The deployed Salad artifact is not a small function image:

- model inventory: `25,253,680,505` bytes;
- compressed OCI image: `28,979,331,889` bytes;
- selected group: medium priority, one of five 24+ GiB consumer GPU classes;
- previous measured cold pulls: about 80 minutes and 47.5 minutes.

The 2026-08-30 job caused allocations and downloads at 13:18 and 13:22. The
replacement reached `Instance Starting` at 14:20 and `Instance Running` at
14:25, four minutes after the application job had already been cancelled. With
queue length zero and autoscaler minimum zero, the group nevertheless retained
one desired replica. That instance remained until a medium-priority interruption
at 18:46, after which Salad allocated another node and began downloading the
same image again at 18:47.

This matches Salad's documented architecture: nodes have variable consumer
network throughput, images are distributed to allocated nodes, and smaller
images are the primary way to improve startup. Salad explicitly calls a single
download slower than two minutes per GB a slow node. A 28.98 GB image therefore
cannot be treated as an instant scale-to-zero artifact even when it eventually
works.

References:

- <https://docs.salad.com/container-engine/explanation/core-concepts/service-performance>
- <https://docs.salad.com/container-engine/how-to-guides/troubleshooting>
- <https://docs.salad.com/container-engine/explanation/infrastructure-platform/container-registries>

### RunPod evidence

The endpoint is already configured with the main documented cold-start
optimizations:

- FlashBoot enabled;
- no network volume or data-center pin;
- one maximum worker and one GPU per worker;
- queue-delay scaler at two seconds;
- 30-second idle timeout;
- four GPU types: RTX 5090, RTX 4090, L4, and RTX A6000;
- a separate RunPod cached model at the documented
  `/runpod-volume/huggingface-cache/hub` path;
- `RUNPOD_INIT_TIMEOUT=1800`.

Despite that, the failed request never received a machine. The REST worker
record still has an empty machine object and desired state `EXITED`; the health
endpoint simultaneously reports one initializing worker. That worker record was
created on 2026-08-26 and is therefore a stale/ghost lifecycle record, not proof
that the 2026-08-30 job entered a billed GPU. The provider did not prepare a
usable cached-model host before the two-hour request TTL expired. The exact
internal cache/scheduler defect cannot be proven without RunPod support, but the
failure boundary is unambiguously before container execution.

RunPod's current guidance says active workers eliminate cold starts, while
cached-model hosts can still be delayed while the model is prepared. It also
defines delay time as image initialization plus model loading. Those mechanisms
can improve serverless availability, but they cannot make this endpoint a
reliable interactive dependency while the provider exposes inconsistent worker
state.

References:

- <https://docs.runpod.io/serverless/endpoints/endpoint-configurations>
- <https://docs.runpod.io/serverless/endpoints/model-caching>
- <https://docs.runpod.io/serverless/development/optimization>

### Disabled pre-warm path

The repository contains and tests a guarded capacity controller, but beta has
neither `RUNPOD_CAPACITY_EXPECTED_FINGERPRINT` nor
`SALAD_CAPACITY_EXPECTED_FINGERPRINT`. Both managers were therefore disabled.
The two paid jobs used raw scale-to-zero submission instead of a preflight that
retains one worker and waits for readiness.

Enabling those fingerprints would prevent a provider job from consuming its TTL
while capacity warms. It would not fix Salad's 47.5-80 minute artifact pull,
RunPod's ghost worker/cache placement, or the observed Salad failure to return
to zero. It is a safety improvement, not the primary product solution.

### Local runtime feasibility

The exact pinned upstream ACE-Step commit
`dce621408bee8c31b4fcf4811682eb9359e1bc94` already supports both target
platforms. Its dependency markers include CUDA PyTorch for Linux x86_64 and
PyTorch plus MLX/MLX-LM for macOS arm64. Its Mac launcher selects the MLX LM
backend, and its generation handler contains native MLX DiT/VAE paths. This is
not a proposed third-party fork or model change.

References:

- <https://github.com/ace-step/ACE-Step-1.5/blob/main/README.md>
- <https://raw.githubusercontent.com/ace-step/ACE-Step-1.5/dce621408bee8c31b4fcf4811682eb9359e1bc94/pyproject.toml>
- <https://raw.githubusercontent.com/ace-step/ACE-Step-1.5/dce621408bee8c31b4fcf4811682eb9359e1bc94/start_api_server_macos.sh>

## Decisions

1. **Do not make cold RunPod Serverless or Salad the default interactive
   backend.** Keep their adapters and credentials only for persisted-job
   reconciliation and explicitly authorized experiments.
2. **Implement `node/ace-step-v15-xl-turbo` now.** `node` is a first-class
   `ProviderName`, not a mock alias and not a special path inside the
   controller.
3. **Use one HTTP job protocol for local and rented machines.** A manually
   started persistent rented Linux GPU is simply another ACE Node deployment.
   Provider provisioning is deliberately outside this plan.
4. **Support exactly Linux/NVIDIA/CUDA and macOS/Apple-Silicon/MLX.** Reject
   CPU-only Linux, Intel macOS, Windows, AMD/ROCm, and multi-GPU execution for
   this checkpoint. Those are separate compatibility projects.
5. **Reuse the exact worker schema and model receipt.** Keep ACE-Step v0.1.8,
   the XL turbo DiT, 1.7B LM, aggregate model revision, manifest SHA-256, and
   25.25 GB byte inventory unchanged. Do not use a smaller/different model to
   make acceptance pass.
6. **Run one resident model runtime and a serial queue.** Maximum concurrency is
   one. Model initialization occurs once per node process, before submissions
   are accepted.
7. **Keep capability URLs and creative payloads out of durable node state.** The
   node durably stores identity, state, safe error code, and terminal bounded
   result metadata. Accepted but incomplete jobs become terminal
   `worker_restarted` after a process restart; the controller never invents a
   replacement submission.
8. **Allow pending cancellation only.** A queued node job can be cancelled. A
   running ACE-Step call returns `too_late`; do not pretend that Python thread
   cancellation can safely stop CUDA/MLX inference.
9. **Expose initialization honestly.** The node HTTP service stays alive while
   models initialize. Authenticated health returns `initializing`, `ready`, or
   `failed`; submit returns 503 unless ready. Do not block socket creation for
   the full model load and do not report ready early.
10. **Private transport only.** The controller accepts an ACE Node base URL only
    on loopback, a non-global IP, or an exact `.ts.net` host. Every API route is
    bearer authenticated. Public source/output transfer still uses the existing
    signed `player.evren.io` capabilities.
11. **Do not provision or rent hardware in this implementation task.** Hardware
    spend and the first target host require a separate owner action. Deliver the
    code, deterministic acceptance harness, service templates, and exact
    hardware commands first.
12. **Production remains unchanged.** Deploy the committed controller revision
    to beta with the node backend disabled unless a real node is already healthy.
    Production activation requires the repository's normal manual beta test and
    explicit approval.

## Observable completion criteria

Implementation is complete when all of the following are true:

- `ProviderName.NODE` and backend `node/ace-step-v15-xl-turbo` implement the
  existing `InferenceProvider` contract without provider-specific controller
  branching.
- A bearer-authenticated ACE Node service accepts schema-2 jobs, processes one
  at a time with the existing worker handler, uploads through the existing
  capability, exposes status/result/cancel/health, and survives ordinary
  controller polling and node-service restarts without duplicate work.
- Runtime selection preserves the existing CUDA/vLLM path byte-for-byte in
  behavior and adds a fail-closed MPS/MLX path for Apple Silicon.
- Unit tests exercise Linux CUDA selection, macOS MLX selection, unsupported
  platforms, initialization failure, idempotent nonce handling, serial
  execution, pending cancellation, running cancellation, restart recovery,
  upload success/failure, bounded responses, authentication, and controller
  integration.
- A deterministic fake runtime completes an end-to-end controller → node →
  signed upload → controller result test without CUDA, MLX, paid providers, or
  audio bytes in provider request/response bodies.
- Linux systemd and macOS launchd templates plus an operator runbook describe
  installation, immutable model preparation, private networking, startup,
  health, logs, shutdown, upgrade, and rollback.
- The full repository verification matrix has no new failures. Expired private
  quality-fixture failures are recorded separately and not weakened.
- The exact committed application revision is deployed to beta, beta regression
  smoke tests pass, the node selector is absent unless a real healthy node is
  configured, and production still points to its pre-plan revision.

## Scope and ownership

Primary repository: `/root/code/audioventura`

Deployment repository, only if beta wiring is needed:
`/root/code/evreniops`

Before editing, run:

```text
pwd
hostname
git status --short --branch
rg --files -g AGENTS.md -g '!**/.git/**'
```

Read the root instructions and every nearer `AGENTS.md` for files touched. If
the deployment repository is changed, read its complete instruction chain
first. Preserve all unrelated user changes. Do not merge, deploy production,
delete provider resources, create paid jobs, create a rented machine, or publish
anything publicly.

## Target architecture

```text
AudioVentura controller
  -> private bearer-authenticated ACE Node metadata API
     -> durable serial job state + in-memory active capability envelope
        -> existing runpod_worker schema/handler
           -> pinned ACE-Step runtime (CUDA/vLLM or MPS/MLX)
           -> signed source download from controller (covers only)
           -> signed result upload to controller
  <- bounded status/result metadata
```

The name `runpod_worker` remains for this checkpoint to avoid a risky package
rename across RunPod and Salad. Treat it as the existing shared generation
runtime despite the legacy name. A later no-behavior-change refactor may rename
it after node acceptance.

## Files to inspect before implementation

```text
AGENTS.md
README.md
ARCHITECTURE.md
DEVLOG.md
pyproject.toml
uv.lock
.gitignore

src/ace_service/providers/AGENTS.md
src/ace_service/providers/base.py
src/ace_service/providers/mock.py
src/ace_service/providers/registry.py
src/ace_service/app.py
src/ace_service/config.py
src/ace_service/worker.py
src/ace_service/web.py
src/ace_service/models.py
src/ace_service/repository.py
src/ace_service/migrations.py

runpod_worker/AGENTS.md
runpod_worker/runtime.py
runpod_worker/handler.py
runpod_worker/schemas.py
runpod_worker/source_audio.py
runpod_worker/audio_output.py
runpod_worker/transfer_client.py
runpod_worker/tests/

deploy/salad/download_model.py
deploy/salad/worker_api.py
midi_mock_backend/src/ace_midi_mock/app.py
midi_mock_backend/src/ace_midi_mock/db.py
midi_mock_backend/src/ace_midi_mock/worker.py

tests/test_mock_provider.py
tests/test_app.py
tests/test_config.py
tests/test_providers.py
tests/test_worker.py
tests/test_web.py
tests/integration/test_source_pipeline.py
```

## API and data contract

### Controller configuration

Add these controller settings:

```text
ACE_NODE_BASE_URL=https://<private-node>.ts.net
ACE_NODE_TOKEN=<secret>
ACE_NODE_CONNECT_TIMEOUT_SECONDS=5
ACE_NODE_READ_TIMEOUT_SECONDS=30
ACE_NODE_WRITE_TIMEOUT_SECONDS=30
ACE_NODE_POOL_TIMEOUT_SECONDS=5
```

The node backend is constructed when it appears in the union of selectable
backend IDs and persisted nonterminal backend IDs. A removed node backend must
therefore remain reconcilable until its jobs are terminal, matching RunPod,
Salad, Fal, and mock behavior.

Do not add the node to the repository's default enabled backend list. It becomes
selectable only through explicit deployment configuration.

### Node service configuration

Add service settings under the existing configured data-root discipline:

```text
ACE_NODE_LISTEN_HOST=<private address or loopback>
ACE_NODE_LISTEN_PORT=8210
ACE_NODE_TOKEN=<secret>
ACE_NODE_DATA_ROOT=<absolute path>
ACE_NODE_ACCELERATOR=auto|cuda|mps
ACE_TRANSFER_ALLOWED_HOST=player.evren.io
ACE_WORKER_HF_CACHE_ROOT=<absolute Hugging Face hub cache>
ACE_WORKER_MODEL_REPO=evrenesat/audioventura-ace-step-v0.1.8
ACE_WORKER_MODEL_REVISION=88b8c7fa089446b53382c1040037492463430bed
ACE_WORKER_MODEL_TAG=av-v0.1.8-bundle-2
ACE_WORKER_MODEL_MANIFEST_SHA256=39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc
ACE_NODE_JOB_TIMEOUT_SECONDS=1800
ACE_NODE_MAX_OUTPUT_BYTES=<same controller limit>
```

`auto` resolves only to CUDA on Linux x86_64 or MPS on macOS arm64. An explicit
accelerator must match the detected platform. Unsupported combinations fail
health with a safe configuration code.

### Node HTTP surface

Every route requires `Authorization: Bearer <ACE_NODE_TOKEN>` and returns at
most 64 KiB JSON:

```text
GET  /healthz
POST /v1/jobs
GET  /v1/jobs/{uuid}
GET  /v1/jobs/{uuid}/result
POST /v1/jobs/{uuid}/cancel
```

`POST /v1/jobs` accepts exactly:

```json
{
  "schema_version": 2,
  "application_job_id": "<uuid>",
  "variation_index": 1,
  "submission_nonce": "<uuid>",
  "input": {"schema_version": 2},
  "source": null,
  "result_upload": {"url": "<signed capability>", "max_bytes": 1}
}
```

The complete `input` remains the current strict worker payload. Validate its
schema with `WorkerRequest.from_mapping` before claiming the submission. Do not
log or durably persist the input, source capability, output capability, prompt,
or lyrics.

Submission idempotency key is the tuple `(application_job_id,
variation_index, submission_nonce)`. Repeating that tuple returns the same node
job. Reusing a nonce with different immutable identity is HTTP 409. Capacity is
one running job; additional jobs remain queued.

Node states are exactly:

```text
queued -> running -> succeeded
                  -> failed
queued -> cancelled
queued/running at process recovery -> failed(worker_restarted)
```

Store terminal result metadata only after the signed upload succeeds. Status
may expose a bounded safe `error_code`; it must never expose an exception
message, path, URL, prompt, lyrics, token, request body, or provider response.

## Sequential implementation steps

### 1. Freeze regression evidence

Run and record before editing:

```text
git status --short --branch
git rev-parse HEAD
uv run pytest -q tests/test_runpod_client.py tests/test_runpod_capacity.py \
  tests/test_salad_provider.py tests/test_salad_capacity.py \
  tests/test_salad_worker.py tests/test_salad_infra.py runpod_worker/tests
```

Expected current focused result: `191 passed`. Do not query or mutate either
paid provider during implementation.

### 2. Make the existing worker runtime platform-aware

Modify `runpod_worker/runtime.py` and focused runtime tests.

- Add a small immutable accelerator configuration object containing device,
  accelerator label, LM backend, MLX DiT flag, offload flags, and measurable
  memory bytes.
- Preserve current cloud defaults by setting `ACE_NODE_ACCELERATOR=cuda` in
  the RunPod and Salad image environments. Existing images must still fail
  closed without CUDA.
- CUDA initialization remains `device="cuda"`, LM backend `vllm`, no compile,
  no offload, no MLX.
- MPS initialization requires Darwin arm64 and
  `torch.backends.mps.is_available()`. Use `device="mps"`, LM backend `mlx`,
  `use_mlx_dit=True`, no torch compile, no quantization, and no CPU offload.
- Do not silently fall back between CUDA, MPS, or CPU.
- Keep the exact model manifest and file containment validation.
- Extend bounded completion metadata with a platform/runtime kind if needed,
  while preserving every current schema-2 RunPod/Salad field and parser.
- A local runtime receipt must be an immutable SHA-256 derived from the exact
  application commit plus `uv.lock`; document that the legacy
  `image_digest` field carries this deployment receipt for a non-containerized
  node. Never use `latest`, a branch name, or the dirty working tree.

Do not rename `runpod_worker` in this checkpoint.

### 3. Add the ACE Node service

Create:

```text
src/ace_node/__init__.py
src/ace_node/__main__.py
src/ace_node/config.py
src/ace_node/db.py
src/ace_node/app.py
src/ace_node/worker.py
src/ace_node/model_bundle.py
src/ace_node/AGENTS.md
tests/test_node_app.py
tests/test_node_db.py
tests/test_node_worker.py
tests/test_node_runtime.py
```

Add `src/ace_node` and the existing `runpod_worker` package to the Hatch wheel,
plus an `ace-node` console script. Put heavyweight ACE-Step/platform packages in
an opt-in `node` dependency group; the controller's normal environment must not
install torch, ACE-Step, MLX, nano-vllm, audio tooling, or model weights.

Pin the upstream ACE-Step Git source to
`dce621408bee8c31b4fcf4811682eb9359e1bc94`. Ensure `uv.lock` resolves both
Linux x86_64 and Darwin arm64. Pin any git subdirectory dependency such as
nano-vllm to the same upstream commit. Do not resolve from upstream `main`.

Refactor `deploy/salad/download_model.py` only as needed to share the immutable
bundle constants and manifest validation; do not change the deployed Salad
receipt. `python -m ace_node.model_bundle prepare` downloads the one exact
snapshot into the configured cache, validates all 29 files and exact aggregate
bytes, and then permits offline startup. The Hugging Face token is read from the
environment for preparation only and is never persisted.

Use SQLite under `ACE_NODE_DATA_ROOT`. Initialize with WAL, foreign keys, a
bounded busy timeout, UTC timestamps, and an explicit schema version. Add no
audio blob or capability column. Recovery transactionally marks nonterminal
rows failed with `worker_restarted` before accepting new work.

The service starts its model initializer in a background thread. Health remains
available during initialization. Only a ready runtime starts the serial worker
queue or accepts submissions. Model initialization failure leaves the HTTP
service alive with safe failed health; it does not retry in a tight loop.

### 4. Add the controller provider

Create:

```text
src/ace_service/providers/node.py
tests/test_node_provider.py
```

Update:

```text
src/ace_service/providers/base.py
src/ace_service/providers/__init__.py
src/ace_service/app.py
src/ace_service/config.py
src/ace_service/web.py
tests/test_app.py
tests/test_config.py
tests/test_providers.py
tests/test_web.py
```

Add `ProviderName.NODE` and backend
`node/ace-step-v15-xl-turbo`. Capabilities match the current RunPod/Salad
ACE-Step feature set, result delivery is `worker_upload`, pending cancel is
true, running cancel is false, and a 404 is never assumed terminal.

The HTTP adapter follows the bounded error/body/timeout rules in
`providers/mock.py` but remains a separate real provider. Validate exact UUIDs,
echoed application identity, state names, status/result transitions, maximum
body size, and backend/provider identity. Never retain an HTTP response body in
an exception.

Add the UI label `ACE Node · ACE-Step 1.5 XL Turbo`. It is real audio, supports
both original and source-audio workflows, and must not inherit mock-only UI
language or behavior.

No database migration should be needed because provider/backend columns are
already bounded strings without a SQL enum constraint. Prove this with a
migration test that opens a current database containing a node job. If a hidden
constraint is found, add one ordered additive migration rather than mutating an
old migration.

### 5. Add deterministic end-to-end proof

Add an injectable fake model runtime that is available only to tests. It must
consume a schema-2 request, download a signed source when present, create one
small deterministic MP3 fixture through the same handler/output seam, upload it
through the controller capability, and return normal bounded worker metadata.

Exercise the real controller provider, node HTTP routes, node DB/queue, transfer
routes, output validation, controller reconciliation, and media publication in
one integration test. Assert that:

- provider API request and response JSON contain no audio bytes;
- capability URLs never enter node SQLite or logs;
- the controller stores exactly one external node job ID;
- repeating a poll or same-nonce submit does not render/upload twice;
- output size/SHA-256/duration agree at both ends;
- cancellation and restart cases cannot create a replacement output.

Do not add a CPU ACE-Step fallback to make this test pass.

### 6. Add operator packaging and documentation

Create:

```text
deploy/node/AGENTS.md
deploy/node/run-node.sh
deploy/node/linux/audioventura-ace-node.service
deploy/node/macos/io.evren.audioventura.ace-node.plist
docs/ACE-NODE.md
```

The shell entry point reads one explicit environment-file path, refuses a dirty
or revision-mismatched checkout, activates the opt-in node environment, and
executes one process. Do not embed credentials or home-directory assumptions in
service templates. Use placeholders and document their exact substitution.

Linux instructions cover NVIDIA driver/CUDA visibility, model preparation,
systemd start/stop/status/logs, Tailscale binding, health, and checkout rollback.
macOS instructions cover Apple Silicon validation, MLX import/MPS checks,
model preparation, launchd bootstrap/bootout/status/logs, sleep/power caveats,
Tailscale binding, and checkout rollback. Do not control the user's desktop or
install either service during this task.

Update authoritative docs without duplication:

- `README.md`: add ACE Node setup/verification entry points and clearly label
  RunPod/Salad as optional cloud providers.
- `ARCHITECTURE.md`: add the separately deployed persistent node boundary and
  metadata/capability flow.
- `DEVLOG.md`: record the decision and implementation evidence.
- `docs/RUNPOD.md` and `docs/SALAD.md`: link to the decision/ACE Node runbook and
  state that scale-to-zero is not the default interactive path.

### 7. Verification

Run focused checks first:

```text
uv run pytest -q tests/test_node_app.py tests/test_node_db.py \
  tests/test_node_worker.py tests/test_node_runtime.py \
  tests/test_node_provider.py tests/integration/test_node_pipeline.py

uv run pytest -q tests/test_runpod_client.py tests/test_runpod_capacity.py \
  tests/test_salad_provider.py tests/test_salad_capacity.py \
  tests/test_salad_worker.py tests/test_salad_infra.py runpod_worker/tests
```

Then run the full required matrix from `README.md`:

```text
uv run pytest -q tests runpod_worker/tests
uv run ruff check .
uv run ruff format --check .
uv run mypy src runpod_worker

cd home_ingest
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src

cd ../midi_mock_backend
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Also verify packaging without model initialization:

```text
uv lock --check
uv build
uv run --group node python -c "import ace_node, runpod_worker"
shellcheck deploy/node/run-node.sh
git diff --check
```

On Linux p100, do not import torch or initialize ACE-Step in the controller's
normal environment. Prove this with a subprocess/import test or installed
distribution inspection. Record any expired private quality-fixture failures as
the known baseline, with zero new product failures.

### 8. Commit and beta deployment

Review the complete diff, then commit with the repository format, for example:

```text
feat/<branch>: add portable ACE node backend
```

Push only the private repository branch required by the existing beta deploy
workflow. Deploy that exact commit to beta. Do not configure
`node/ace-step-v15-xl-turbo` in `INFERENCE_ENABLED_BACKENDS` unless an actual
node has passed authenticated `ready` health and the model/runtime receipt
matches this plan.

With node disabled, run beta regression smoke for login, backend inventories,
source ingest, existing deterministic mock generation, playback, and provider
status pages. Expected result: no node selector and no change to current
production-visible behavior. Report:

- beta URL;
- exact product revision;
- exact deployment revision, if changed;
- exact production revision (unchanged);
- node activation state (`disabled: no accepted hardware host` unless a real
  node was provided);
- test results and known fixture failures.

Ask the owner to choose/provide the first hardware target after the code is
ready:

- an Apple Silicon Mac reachable privately; or
- a manually started persistent Linux/NVIDIA rental.

Hardware activation is a separate acceptance gate. It must run one original and
one source-audio job, verify runtime/model receipts and output playback, exercise
a warm second job, restart recovery, and controlled shutdown. Only after the
owner manually tests beta may a separately approved revision be deployed to
production.

## Rollback

Code rollback is additive:

1. remove `node/ace-step-v15-xl-turbo` from beta selectable/default backend
   settings;
2. retain node credentials only while any persisted node job is nonterminal;
3. stop the node service after the queue is empty;
4. point beta to the prior application release;
5. do not rewrite or delete historical node job/output rows.

RunPod and Salad adapters remain available for historical reconciliation. No
provider endpoint or container group is deleted by this plan.

## Explicit exclusions

- no production deployment;
- no paid generation;
- no cloud GPU provisioning, provider migration, or provider deletion;
- no automatic wake-on-LAN, Mac sleep prevention, or desktop control;
- no CPU-only, Intel Mac, Windows, AMD/ROCm, or multi-GPU runtime;
- no ACE-Step version/model/quality change;
- no public ACE Node endpoint;
- no audio bytes in controller/provider API JSON;
- no automatic retry that can duplicate a paid or local generation;
- no removal of RunPod/Salad recovery code.

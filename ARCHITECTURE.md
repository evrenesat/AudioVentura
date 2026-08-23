# Architecture

AudioVentura is split across three explicit trust and runtime boundaries:

1. The Hetzner controller serves the authenticated UI, stores durable SQLite
   state, coordinates jobs, and exposes short-lived audio transfer URLs.
2. Home Ingest performs YouTube access and media preparation on the private
   home node. Cover continuation reuses a verified local generated output and
   does not contact YouTube again.
3. A selected inference provider executes ACE-Step inference. Runpod remains
   the active production path. The prepared SaladCloud path uses a managed job
   queue and a scale-to-zero GPU container group around the same isolated
   runtime; workers load one immutable model bundle and never receive home
   credentials.

The controller records provider-backed queue and worker phases; the worker
reports structured source-transfer, generation, finalization, and upload
phases. Unknown provider internals are represented as waiting rather than an
invented percentage or sub-step.

The existing progress envelope may also carry a bounded provider-neutral
message, normalized 0..1 progress, and job/deployment scope. The UI labels
deployment-scoped details as inferred because Container Engine instance state
is not an authoritative per-job assignment. Raw provider state, reasons, and
payloads are not persisted in this status surface.

Provider ownership is durable per job and variation attempt. The controller
owns deadlines, status uncertainty, cancellation, and cleanup; adapters only
translate provider APIs into one finite lifecycle. Transient status/result
failures keep the exact persisted reference and never trigger resubmission.
Capabilities are the server-authoritative seam for a later provider selector
and fal.ai fallback with unsupported request controls disabled in the UI.

See [the detailed architecture](docs/ARCHITECTURE.md),
[operations](docs/OPERATIONS.md), [Runpod runtime](docs/RUNPOD.md), and the
[prepared SaladCloud boundary](docs/SALAD.md).

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

See [the detailed architecture](docs/ARCHITECTURE.md),
[operations](docs/OPERATIONS.md), [Runpod runtime](docs/RUNPOD.md), and the
[prepared SaladCloud boundary](docs/SALAD.md).

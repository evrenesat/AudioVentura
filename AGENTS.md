# ACE Service Repository Guidance

This repository contains the private ACE-Step controller and its separately
deployed Home Ingest and GPU-worker components. Keep those runtime boundaries
explicit: the controller may call provider metadata APIs, but it must not run
media tools, load inference models, or put audio bytes in provider API bodies.

Use `uv` for Python commands. Keep application data under the configured data
root, store timestamps in UTC, and do not commit `.env` files or generated
audio/database state. Run the full Checkpoint #1 verification commands before
handing work to review.

Keep documentation ownership simple:

- `README.md` is the operator and coding-agent entry point.
- `ARCHITECTURE.md` is the authoritative current architecture.
- `docs/OPERATIONS.md` is the general deployment and recovery runbook.
- `docs/RUNPOD.md` and `docs/SALAD.md` contain provider-specific operations.
- `docs/SECURITY.md` is the exposed-surface and secret-handling checklist.
- `DEVLOG.md`, plans, baseline records, and incident records are historical;
  do not treat them as current setup instructions.

Prefer short, direct language. Do not duplicate architecture or runbook text
across files; link to the authoritative document instead.

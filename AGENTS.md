# ACE Service Repository Guidance

This repository contains the private ACE-Step controller and its separately
deployed Home Ingest and GPU-worker components. Keep those runtime boundaries
explicit: the controller may call provider metadata APIs, but it must not run
media tools, load inference models, or put audio bytes in provider API bodies.

Use `uv` for Python commands. Keep application data under the configured data
root, store timestamps in UTC, and do not commit `.env` files or generated
audio/database state. Run the full Checkpoint #1 verification commands before
handing work to review.

After changing anything in this repository:

1. Deploy the exact committed revision to the beta environment.
2. Test that beta deployment yourself, including the changed behavior.
3. Tell the user the beta URL and exact deployed revision, and ask the user to
   test it manually.
4. Only after the user's beta test, ask for explicit approval to deploy the
   tested revision to production. Never infer production approval from approval
   to change, commit, push, or deploy beta.

Always state which environment and exact revision is currently deployed. Keep
beta and production labels explicit in plans, progress updates, and handoffs.

Beta, staging, and test environments must reproduce production behavior by
default. Differences are allowed only when they prevent real cost, destructive
external effects, or unsafe access to production state. Keep those exceptions
narrow and preserve the same user-visible behavior with isolated resources or
deterministic fakes. Ask the user before planning any other capability gap; do
not silently treat a pre-production environment as a reduced product.

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

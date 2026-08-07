# ACE Service Repository Guidance

This repository contains the private ACE-Step controller and its separately
deployed home-ingest and Runpod components. Keep those runtime boundaries
explicit: the controller foundation must not invoke media tools or contact
external inference services.

Use `uv` for Python commands. Keep application data under the configured data
root, store timestamps in UTC, and do not commit `.env` files or generated
audio/database state. Run the full Checkpoint #1 verification commands before
handing work to review.

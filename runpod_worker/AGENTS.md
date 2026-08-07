# Runpod worker module

This directory owns the isolated ACE-Step v0.1.8 Runpod Serverless worker.
Keep it metadata-only at the Runpod API boundary: source and generated audio
must move through short-lived HTTPS capability URLs, and the result body must
contain only bounded metadata.

Model objects are process-global and must be initialized before the Runpod SDK
starts accepting jobs. Do not add YouTube, home-ingest, SFTP, SSH, Tailscale,
or controller database credentials here. Keep temporary audio under the
worker-created private temporary directory and preserve cleanup on every
failure path.

Run local checks from the repository root with:

```text
uv run pytest -q runpod_worker/tests
uv run ruff check runpod_worker
uv run ruff format --check runpod_worker
```

# Salad deployment module

This directory owns the SaladCloud-specific worker wrapper, immutable image
assembly, and safe infrastructure administration. Keep Salad queue, container
group, GPU, registry, and autoscaler concepts out of the shared inference
runtime and controller provider contract.

Reuse `runpod_worker` only as the current shared ACE-Step runtime boundary.
Keep job payloads metadata-only and preserve the signed HTTPS source/output
transfer contract. Never add Salad, GitHub, Hugging Face, or controller secrets
to tracked files or image layers.

Pin external binaries, base images, model revisions, and final deployment
images immutably. Infrastructure apply operations must be idempotent and must
stop on drift instead of deleting or replacing resources automatically.

Run focused checks from the repository root:

```text
uv run pytest -q tests/test_salad_worker.py tests/test_salad_infra.py
uv run ruff check deploy/salad tests/test_salad_worker.py tests/test_salad_infra.py
uv run ruff format --check deploy/salad tests/test_salad_worker.py tests/test_salad_infra.py
uv run mypy --follow-imports=skip deploy/salad
shellcheck deploy/salad/entrypoint.sh
```

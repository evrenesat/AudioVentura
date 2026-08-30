# ACE Node service

This package owns the separately deployed, bearer-authenticated ACE Node
process. It may import the shared `runpod_worker` package, but heavyweight
PyTorch/ACE-Step/MLX dependencies must remain lazy and must never be imported
by the controller environment. Keep capability URLs and creative payloads in
memory only; SQLite stores bounded identity, state, safe errors, and terminal
result metadata.

The service supports one serial job and exactly Linux x86_64/CUDA or macOS
arm64/MPS+MLX. Run focused checks from the repository root with:

```text
uv run pytest -q tests/test_node_app.py tests/test_node_db.py \
  tests/test_node_worker.py tests/test_node_runtime.py
```

"""Prepare and validate the exact immutable ACE-Step model snapshot."""

from __future__ import annotations

import importlib
import json
import os

from runpod_worker.runtime import resolve_checkpoint_paths

from .config import NodeSettings


def prepare(settings: NodeSettings | None = None) -> dict[str, object]:
    """Download one pinned snapshot, then run the shared strict validator."""

    resolved = settings or NodeSettings()
    try:
        snapshot_download = importlib.import_module("huggingface_hub").snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub is required only for model preparation") from exc
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    snapshot_download(
        repo_id=resolved.worker_model_repo,
        revision=resolved.worker_model_revision,
        cache_dir=str(resolved.worker_hf_cache_root),
        token=token,
    )
    paths = resolve_checkpoint_paths(resolved.worker_hf_cache_root)
    return {
        "status": "ok",
        "repo": resolved.worker_model_repo,
        "revision": resolved.worker_model_revision,
        "manifest_sha256": resolved.worker_model_manifest_sha256,
        "checkpoints": str(paths.root),
    }


def main() -> None:
    print(json.dumps(prepare(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

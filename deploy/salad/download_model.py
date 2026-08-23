"""Download and validate the one immutable AudioVentura model snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

MODEL_REPO = "evrenesat/audioventura-ace-step-v0.1.8"
MODEL_REVISION = "88b8c7fa089446b53382c1040037492463430bed"
MODEL_MANIFEST_SHA256 = "39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc"
MODEL_TOTAL_BYTES = 25_253_680_505
MODEL_CACHE_ROOT = Path("/opt/audioventura-model/huggingface-cache/hub")
REQUIRED_DIRECTORIES = (
    "checkpoints/Qwen3-Embedding-0.6B",
    "checkpoints/acestep-5Hz-lm-1.7B",
    "checkpoints/acestep-v15-xl-turbo",
    "checkpoints/vae",
)


def _manifest_files(value: Any) -> dict[str, int]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("model manifest file inventory is malformed")
    result: dict[str, int] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "object_identity"}:
            raise RuntimeError("model manifest file inventory is malformed")
        path = entry.get("path")
        size = entry.get("size")
        identity = entry.get("object_identity")
        if (
            not isinstance(path, str)
            or not path.startswith("checkpoints/")
            or ".." in Path(path).parts
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(identity, str)
            or not identity
            or path in result
        ):
            raise RuntimeError("model manifest file inventory is malformed")
        result[path] = size
    return result


def _actual_files(snapshot: Path) -> dict[str, int]:
    model_root = MODEL_CACHE_ROOT.resolve()
    result: dict[str, int] = {}
    for path in sorted((snapshot / "checkpoints").rglob("*")):
        if path.is_dir():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(model_root):
            raise RuntimeError("model snapshot file escapes the cache root")
        result[path.relative_to(snapshot).as_posix()] = resolved.stat().st_size
    return result


def main() -> None:
    snapshot = Path(
        snapshot_download(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            cache_dir=MODEL_CACHE_ROOT,
        )
    )
    if snapshot.name != MODEL_REVISION:
        raise RuntimeError("downloaded model revision does not match the pinned commit")
    manifest_bytes = (snapshot / "bundle-manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MODEL_MANIFEST_SHA256:
        raise RuntimeError("downloaded model manifest digest does not match")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise RuntimeError("downloaded model manifest is malformed")
    if manifest.get("total_bytes") != MODEL_TOTAL_BYTES:
        raise RuntimeError("downloaded model total does not match")
    if manifest.get("required_directories") != list(REQUIRED_DIRECTORIES):
        raise RuntimeError("downloaded model directory contract does not match")
    for relative in REQUIRED_DIRECTORIES:
        if not (snapshot / relative).is_dir():
            raise RuntimeError("downloaded model is missing a required directory")
    expected = _manifest_files(manifest.get("files"))
    actual = _actual_files(snapshot)
    if expected != actual or sum(actual.values()) != MODEL_TOTAL_BYTES:
        raise RuntimeError("downloaded model file inventory does not match")
    print(json.dumps({"bytes": MODEL_TOTAL_BYTES, "files": len(actual), "status": "ok"}))


if __name__ == "__main__":
    main()

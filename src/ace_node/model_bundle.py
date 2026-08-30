"""Prepare and validate the exact immutable ACE-Step model snapshot."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from runpod_worker.runtime import (
    MODEL_BUNDLE_FILE_COUNT,
    MODEL_BUNDLE_TOTAL_BYTES,
    WorkerInitializationError,
    resolve_checkpoint_paths,
)

from .config import NodeSettings

_STAGES = frozenset({"starting", "downloading", "verifying", "complete", "failed"})


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, WorkerInitializationError):
        return "model_manifest_invalid"
    return "model_download_failed"


@dataclass
class _ProgressReporter:
    """Aggregate tqdm bars without exposing paths, filenames, or exceptions."""

    sink: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    downloaded_bytes: int = 0
    completed_files: int = 0
    total_bytes: int = MODEL_BUNDLE_TOTAL_BYTES
    total_files: int = MODEL_BUNDLE_FILE_COUNT
    _last_emit: float = field(default=-float("inf"), init=False)
    _bars: dict[int, tuple[int, int | None, bool]] = field(default_factory=dict, init=False)

    def emit(self, stage: str, *, safe_error_code: str | None = None, force: bool = False) -> None:
        if stage not in _STAGES:
            raise ValueError("invalid model progress stage")
        now = self.clock()
        if not force and now - self._last_emit < 0.25:
            return
        event = {
            "stage": stage,
            "downloaded_bytes": max(0, self.downloaded_bytes),
            "total_bytes": self.total_bytes,
            "completed_files": max(0, self.completed_files),
            "total_files": self.total_files,
            "safe_error_code": safe_error_code,
        }
        sink_line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        self.sink(sink_line)
        self._last_emit = now

    def register(self, bar: Any) -> None:
        total = getattr(bar, "total", None)
        parsed_total = int(total) if isinstance(total, (int, float)) and total >= 0 else None
        unit = str(getattr(bar, "unit", ""))
        is_bytes = unit.lower() in {"b", "byte", "bytes"}
        self._bars[id(bar)] = (int(getattr(bar, "n", 0) or 0), parsed_total, is_bytes)

    def update(self, bar: Any) -> None:
        previous, total, is_bytes = self._bars.get(
            id(bar),
            (
                0,
                None,
                str(getattr(bar, "unit", "")).lower() in {"b", "byte", "bytes"},
            ),
        )
        current = max(previous, int(getattr(bar, "n", 0) or 0))
        if is_bytes:
            self.downloaded_bytes += max(0, current - previous)
        self._bars[id(bar)] = (current, total, is_bytes)
        self.emit("downloading")

    def finish(self, bar: Any) -> None:
        self.update(bar)
        previous, total, _ = self._bars.get(id(bar), (0, None, False))
        if total is None or previous >= total:
            self.completed_files = min(self.total_files, self.completed_files + 1)
        self.emit("downloading")


def _tqdm_class(reporter: _ProgressReporter) -> type[Any]:
    """Return a tqdm.auto-compatible class for Hugging Face Hub."""

    tqdm_type = importlib.import_module("tqdm.auto").tqdm

    class DownloadProgress(tqdm_type):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("reporter", None)
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            reporter.register(self)

        def update(self, n: int = 1) -> Any:
            result = super().update(n)
            reporter.update(self)
            return result

        def close(self) -> None:
            reporter.finish(self)
            super().close()

    return DownloadProgress


def _set_model_environment(settings: NodeSettings) -> None:
    # The preparation child is deliberately configured from typed settings so
    # validation uses the same pinned repo/revision as the resident worker.
    os.environ.update(settings.model_environment())


def prepare(
    settings: NodeSettings | None = None,
    *,
    ndjson: bool = False,
    emit: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Download one pinned snapshot, then run the shared strict validator."""

    resolved = settings or NodeSettings()
    _set_model_environment(resolved)
    reporter = _ProgressReporter(emit or (lambda line: print(line, flush=True)))
    if ndjson:
        reporter.emit("starting", force=True)
    try:
        if ndjson:
            reporter.emit("downloading", force=True)
        snapshot_download = importlib.import_module("huggingface_hub").snapshot_download
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        progress_class = _tqdm_class(reporter)
        kwargs: dict[str, object] = {
            "repo_id": resolved.worker_model_repo,
            "revision": resolved.worker_model_revision,
            "cache_dir": str(resolved.worker_hf_cache_root),
            "token": token,
            "tqdm_class": progress_class,
        }
        snapshot_download(**kwargs)
        if ndjson:
            reporter.emit("verifying", force=True)
        paths = resolve_checkpoint_paths(resolved.worker_hf_cache_root)
        result: dict[str, object] = {
            "status": "ok",
            "repo": resolved.worker_model_repo,
            "revision": resolved.worker_model_revision,
            "manifest_sha256": resolved.worker_model_manifest_sha256,
            "checkpoints": str(paths.root),
        }
        if ndjson:
            reporter.emit("complete", force=True)
        return result
    except Exception as exc:
        if ndjson:
            reporter.emit("failed", safe_error_code=_safe_error_code(exc), force=True)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("prepare",), default="prepare")
    parser.add_argument("--ndjson", action="store_true", help="emit bounded progress events")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = prepare(ndjson=args.ndjson)
    except Exception:
        if not args.ndjson:
            print(json.dumps({"status": "failed", "safe_error_code": "model_preparation_failed"}))
        return 1
    if not args.ndjson:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

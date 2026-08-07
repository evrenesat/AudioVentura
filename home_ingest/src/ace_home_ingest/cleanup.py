"""Cleanup for orphaned home-ingest job directories."""

from __future__ import annotations

import logging
import shutil
import time

from .config import HomeIngestSettings

LOGGER = logging.getLogger(__name__)


def prune_orphan_job_directories(settings: HomeIngestSettings, *, now: float | None = None) -> int:
    """Remove only old, UUID-named job directories below the private temp root."""

    root = settings.paths.temporary
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - settings.orphan_age_seconds
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            LOGGER.warning(
                "orphan cleanup failed stage=cleanup exception_class=%s",
                type(exc).__name__,
                extra={"component": "home_ingest"},
            )
    LOGGER.info(
        "orphan cleanup complete stage=cleanup removed_directories=%d",
        removed,
        extra={"component": "home_ingest"},
    )
    return removed

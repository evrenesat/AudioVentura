from __future__ import annotations

import os
from pathlib import Path

from ace_home_ingest.cleanup import prune_orphan_job_directories
from ace_home_ingest.config import HomeIngestSettings


def test_prune_orphan_job_directories_removes_only_old_directories(tmp_path: Path) -> None:
    settings = HomeIngestSettings(data_root=tmp_path / "data", token="home-secret")
    settings.ensure_data_layout()
    old_directory = settings.paths.temporary / "old-job"
    fresh_directory = settings.paths.temporary / "fresh-job"
    old_directory.mkdir()
    fresh_directory.mkdir()
    old_timestamp = 1_000.0
    os.utime(old_directory, (old_timestamp, old_timestamp))

    removed = prune_orphan_job_directories(
        settings, now=old_timestamp + settings.orphan_age_seconds + 1
    )

    assert removed == 1
    assert not old_directory.exists()
    assert fresh_directory.exists()

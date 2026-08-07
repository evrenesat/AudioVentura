from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ace_home_ingest.config import HomeIngestSettings


def settings_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "data_root": tmp_path / "agent-data",
        "token": "home-secret",
        "sftp_host": "hetzner.tailnet.ts.net",
        "sftp_username": "ace-incoming",
        "sftp_private_key": tmp_path / "key",
    }


def test_settings_create_private_layout(tmp_path: Path) -> None:
    settings = HomeIngestSettings(**settings_kwargs(tmp_path))
    paths = settings.ensure_data_layout()
    assert paths.temporary.is_dir()
    assert all(path.is_relative_to(settings.data_root) for path in paths.all_directories)
    assert (settings.data_root.stat().st_mode & 0o777) == 0o700


def test_wildcard_bind_and_placeholder_token_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        HomeIngestSettings(**settings_kwargs(tmp_path), host="0.0.0.0")
    kwargs = settings_kwargs(tmp_path)
    kwargs["token"] = "change-me"
    with pytest.raises(ValidationError, match="placeholder"):
        HomeIngestSettings(**kwargs)

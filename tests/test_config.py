from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ace_service.config import ServiceSettings


def _settings_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "data_root": tmp_path / "data",
        "service_password": "real-password",
        "home_ingest_token": "real-home-token",
        "runpod_api_key": "real-runpod-key",
        "runpod_endpoint_id": "real-endpoint-id",
    }


def test_settings_resolve_and_create_private_layout(tmp_path: Path) -> None:
    settings = ServiceSettings(**_settings_kwargs(tmp_path))

    assert settings.service_root_path == ""
    assert settings.data_root.is_absolute()
    paths = settings.ensure_data_layout()
    assert all(path.is_relative_to(settings.data_root) for path in paths.all_directories)
    assert all(path.is_dir() for path in paths.all_directories)
    assert paths.database.parent == settings.data_root
    assert (paths.root.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize(
    "field_name",
    ("service_password", "home_ingest_token", "runpod_api_key", "runpod_endpoint_id"),
)
def test_secret_placeholders_are_rejected(field_name: str, tmp_path: Path) -> None:
    kwargs = _settings_kwargs(tmp_path)
    kwargs[field_name] = "change-me"

    with pytest.raises(ValidationError, match=field_name):
        ServiceSettings(**kwargs)


def test_wildcard_bind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        ServiceSettings(**_settings_kwargs(tmp_path), host="0.0.0.0")


def test_runtime_cannot_enable_placeholder_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("allow_test_placeholders", "true")
    kwargs = _settings_kwargs(tmp_path)
    kwargs["service_password"] = "change-me"

    with pytest.raises(ValidationError, match="service_password"):
        ServiceSettings(**kwargs)


def test_transfer_public_base_url_requires_https_and_normalizes(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="https"):
        ServiceSettings(
            **_settings_kwargs(tmp_path), transfer_public_base_url="http://transfer.test"
        )

    settings = ServiceSettings(
        **_settings_kwargs(tmp_path),
        transfer_public_base_url="https://transfer.test/",
    )
    assert settings.transfer_public_base_url == "https://transfer.test"


def test_service_root_path_accepts_environment_prefix_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACE_SERVICE_ROOT_PATH", "/beta")

    settings = ServiceSettings(**_settings_kwargs(tmp_path))

    assert settings.service_root_path == "/beta"


@pytest.mark.parametrize(
    "value",
    (
        "/",
        "beta",
        "/beta/",
        "/beta//nested",
        "/beta/./nested",
        "/beta/../nested",
        "/beta?mode=test",
        "/beta#fragment",
        "/beta\\nested",
        "/beta\n",
        "https://player.example/beta",
    ),
)
def test_service_root_path_rejects_unsafe_or_unnormalized_values(
    value: str, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError, match="service root path"):
        ServiceSettings(**_settings_kwargs(tmp_path), service_root_path=value)

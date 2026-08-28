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
    assert (paths.incoming.stat().st_mode & 0o777) == 0o700


def test_incoming_directory_mode_can_allow_the_restricted_sftp_group(tmp_path: Path) -> None:
    settings = ServiceSettings(**_settings_kwargs(tmp_path), incoming_directory_mode="0770")

    paths = settings.ensure_data_layout()

    assert settings.incoming_directory_mode == "0770"
    assert (paths.root.stat().st_mode & 0o777) == 0o700
    assert (paths.incoming.stat().st_mode & 0o777) == 0o770


@pytest.mark.parametrize("mode", ("0777", "0702", "700", "not-octal"))
def test_incoming_directory_mode_rejects_world_access_or_invalid_octal(
    mode: str, tmp_path: Path
) -> None:
    with pytest.raises(ValidationError, match="INCOMING_DIRECTORY_MODE"):
        ServiceSettings(**_settings_kwargs(tmp_path), incoming_directory_mode=mode)


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


def test_salad_default_requires_scoped_credentials_and_accepts_runpod_as_secondary(
    tmp_path: Path,
) -> None:
    kwargs = _settings_kwargs(tmp_path)
    kwargs.update(
        inference_provider="salad",
        salad_api_key="real-salad-key",
        salad_organization="audio-org",
        salad_project="audio-project",
    )
    settings = ServiceSettings(**kwargs)
    assert settings.inference_provider == "salad"
    assert settings.salad_queue_name == "audioventura-jobs"

    kwargs["salad_organization"] = "Not DNS"
    with pytest.raises(ValidationError, match="DNS-compatible"):
        ServiceSettings(**kwargs)


@pytest.mark.parametrize("name", ("a", "1a", "a-", "a" * 64))
@pytest.mark.parametrize("field_name", ("salad_queue_name", "salad_container_group_name"))
def test_salad_resource_names_enforce_official_boundaries(
    field_name: str, name: str, tmp_path: Path
) -> None:
    kwargs = _settings_kwargs(tmp_path)
    kwargs[field_name] = name

    with pytest.raises(ValidationError, match="DNS-compatible"):
        ServiceSettings(**kwargs)


@pytest.mark.parametrize("name", ("ab", "a-1", "a" * 63))
def test_salad_resource_names_accept_official_boundaries(name: str, tmp_path: Path) -> None:
    settings = ServiceSettings(
        **_settings_kwargs(tmp_path),
        salad_queue_name=name,
        salad_container_group_name=name,
    )

    assert settings.salad_queue_name == name
    assert settings.salad_container_group_name == name


def test_generic_timeout_accepts_runpod_environment_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RUNPOD_JOB_TIMEOUT_SECONDS", "321")
    settings = ServiceSettings(**_settings_kwargs(tmp_path))
    assert settings.inference_job_timeout_seconds == 321


def test_mock_backend_requires_private_non_placeholder_configuration(tmp_path: Path) -> None:
    kwargs = _settings_kwargs(tmp_path)
    kwargs.update(
        inference_enabled_backends="runpod/ace-step-v15-xl-turbo,mock/midi-sequential",
        default_original_backend="runpod/ace-step-v15-xl-turbo",
        default_cover_backend="runpod/ace-step-v15-xl-turbo",
        mock_base_url="http://100.103.69.9:8201",
        mock_token="beta-mock-token",
    )
    settings = ServiceSettings(**kwargs)
    assert "mock/midi-sequential" in settings.enabled_backend_ids

    with pytest.raises(ValidationError, match="mock_token"):
        ServiceSettings(**{**kwargs, "mock_token": "change-me"})
    with pytest.raises(ValidationError, match="private p100"):
        ServiceSettings(**{**kwargs, "mock_base_url": "https://8.8.8.8:8201"})


def test_mock_configuration_is_not_required_when_backend_is_disabled(tmp_path: Path) -> None:
    settings = ServiceSettings(**_settings_kwargs(tmp_path), mock_token="change-me")
    assert "mock/midi-sequential" not in settings.enabled_backend_ids

"""Owner web surface tests for the ailocals Local workers section."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database


def _ailocals_settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        data_root=tmp_path / "service-data",
        service_password="test-password",
        home_ingest_token="test-home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
        mock_base_url="http://mock.ts.net",
        mock_token="test-mock-token",
        ailocals_enabled=True,
        ailocals_environment="beta",
        inference_enabled_backends=("mock/midi-sequential,ailocals/ace-step-v15-xl-turbo"),
        default_original_backend="mock/midi-sequential",
        default_cover_backend="mock/midi-sequential",
    )


@pytest.fixture
def ailocals_web_app(tmp_path: Path):
    settings = _ailocals_settings(tmp_path)
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        settings,
        session_factory=factory,
        provider_registry=None,
        home_ingest_client=None,
        worker=None,
    )
    yield app, factory
    engine.dispose()


def _auth(client: TestClient) -> tuple[str, str]:
    del client
    return ("change-me", "test-password")


def _csrf(client: TestClient, path: str) -> str:
    response = client.get(path, auth=_auth(client))
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None, "csrf token must render on the page"
    return match.group(1)


def test_local_workers_page_lists_enrolled_workers(ailocals_web_app) -> None:
    app, _factory = ailocals_web_app
    with TestClient(app) as client:
        page = client.get("/local-workers", auth=_auth(client))
        assert page.status_code == 200
        assert "Local workers" in page.text
        assert "beta" in page.text
        assert "No local workers enrolled yet" in page.text

        csrf = _csrf(client, "/local-workers")
        created = client.post(
            "/local-workers/enrollments",
            data={"csrf_token": csrf},
            auth=_auth(client),
        )
        assert created.status_code == 200
        assert "One-time enrollment token" in created.text  # token shown once
        listed = client.get("/local-workers", auth=_auth(client))
        assert "One-time enrollment token" not in listed.text  # never echoed again

    service = app.state.ailocals_service
    assert service is not None and service.list_workers() == []


def test_enrollment_token_and_revoke_flow(ailocals_web_app) -> None:
    app, _factory = ailocals_web_app
    with TestClient(app) as client:
        csrf = _csrf(client, "/local-workers")
        created = client.post(
            "/local-workers/enrollments",
            data={"csrf_token": csrf},
            auth=_auth(client),
            follow_redirects=False,
        )
        assert created.status_code == 303
        token_page = client.get(created.headers["location"], auth=_auth(client))
        match = re.search(r'id="enrollment-token-value" readonly value="([^"]+)"', token_page.text)
        assert match is not None, "enrollment token must render once"
        assert "enrollment_token=" in created.headers["location"]

        # Enroll through the common API using the displayed token.
        enroll = client.post(
            "/api/ailocals/v1/enroll",
            json={
                "protocol_version": "ailocals.v1",
                "worker_name": "UI Fixture Mac",
                "software_version": "0.1.0",
                "capabilities": [
                    {
                        "id": "music.ace-step.v1",
                        "category": "music",
                        "parameters": {
                            "worker_schema": 2,
                            "model_bundle_revision": "fixture-bundle-1",
                            "manifest_sha256": "a" * 64,
                            "accelerator": "mps",
                            "formats": ["mp3"],
                        },
                    }
                ],
            },
            headers={"X-Ailocals-Enrollment-Token": match.group(1)},
        )
        assert enroll.status_code == 201, enroll.text

        page = client.get("/local-workers", auth=_auth(client))
        assert "UI Fixture Mac" in page.text
        revoke_match = re.search(r'action="(/local-workers/workers/[^/]+/revoke)"', page.text)
        assert revoke_match is not None
        csrf = _csrf(client, "/local-workers")
        revoked = client.post(
            revoke_match.group(1),
            data={"csrf_token": csrf},
            auth=_auth(client),
        )
        assert revoked.status_code == 200
        assert "Revoked" in revoked.text

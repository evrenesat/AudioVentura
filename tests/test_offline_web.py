from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import MediaFileState, OutputFormat
from ace_service.repository import add_playlist_entry, create_custom_playlist
from tests.test_media_library import _publish_track
from tests.test_web import FakeHome, FakeRunpod, FakeWorker, _auth, _csrf


@pytest.fixture
def media_web_app(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    worker = FakeWorker()
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    yield app, factory, worker
    engine.dispose()


def test_public_offline_shell_has_no_private_state(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        job, _, _ = _publish_track(session, settings, job_id="offline-secret-track")
        session.commit()

    with TestClient(app) as client:
        shell = client.get("/offline-shell")
        assert shell.status_code == 200
        assert "Offline" in shell.text
        assert "offline-secret-track" not in shell.text
        assert job.project.title not in shell.text
        assert 'name="csrf-token"' not in shell.text
        assert "notifications-config" not in shell.text
        assert "data-offline-storage-message" in shell.text
        assert 'rel="manifest"' in shell.text
        assert "Create original" not in shell.text
        assert "Create remix" not in shell.text
        assert client.get("/offline").status_code == 401
        assert client.get("/offline", auth=_auth(client)).status_code == 200


def test_manifest_is_root_aware_and_contains_installable_shell_icons(settings) -> None:
    beta_settings = ServiceSettings(**{**settings.model_dump(), "service_root_path": "/beta"})
    engine = create_database_engine(beta_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        beta_settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            manifest = client.get("/beta/manifest.webmanifest")
            assert manifest.status_code == 200
            assert manifest.headers["cache-control"] == "no-cache"
            payload = manifest.json()
            assert payload["scope"] == "/beta/"
            assert payload["start_url"] == "/beta/"
            assert payload["display"] == "standalone"
            assert payload["prefer_related_applications"] is False
            assert [icon["sizes"] for icon in payload["icons"]] == ["192x192", "512x512"]
            assert all(icon["src"].startswith("/beta/static/") for icon in payload["icons"])

            shell = client.get("/beta/offline-shell")
            assert shell.status_code == 200
            assert 'href="/beta/manifest.webmanifest"' in shell.text
            assert 'href="/beta/static/app.css"' in shell.text
    finally:
        engine.dispose()


def test_queue_v2_preserves_duplicate_entries_and_stable_revision(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        job, _, item = _publish_track(session, settings, job_id="offline-duplicate-track")
        playlist = create_custom_playlist(session, "Offline duplicates")
        first = add_playlist_entry(session, playlist.id, item.id)
        duplicate = add_playlist_entry(session, playlist.id, item.id)
        session.commit()
        playlist_id = playlist.id
        first_id = first.id
        duplicate_id = duplicate.id
        media_file = item.files[0]

    with TestClient(app) as client:
        response = client.get(f"/player/queue/playlist/{playlist_id}", auth=_auth(client))
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema_version"] == 2
        assert payload["context"]["playlist_kind"] == "custom"
        assert len(payload["context"]["revision"]) == 64
        assert [entry["queue_entry_id"] for entry in payload["items"]] == [
            first_id,
            duplicate_id,
        ]
        assert {entry["sha256"] for entry in payload["items"]} == {media_file.sha256}
        assert {entry["media_file_id"] for entry in payload["items"]} == {media_file.id}
        original_revision = payload["context"]["revision"]

        csrf = _csrf(client, f"/playlists/{playlist_id}")
        renamed = client.post(
            f"/playlists/{playlist_id}/rename",
            auth=_auth(client),
            data={"csrf_token": csrf, "title": "Renamed offline duplicates"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        refreshed = client.get(f"/player/queue/playlist/{playlist_id}", auth=_auth(client))
        assert refreshed.json()["context"]["revision"] != original_revision
        assert refreshed.json()["items"][0]["mime_type"] == "audio/mpeg"


def test_playlist_delete_redirect_carries_offline_invalidation_marker(
    media_web_app, settings
) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        _, _, item = _publish_track(session, settings, job_id="offline-delete-playlist")
        playlist = create_custom_playlist(session, "Delete offline playlist")
        add_playlist_entry(session, playlist.id, item.id)
        session.commit()
        playlist_id = playlist.id

    with TestClient(app) as client:
        token = _csrf(client, f"/playlists/{playlist_id}")
        deleted = client.post(
            f"/playlists/{playlist_id}/delete",
            auth=_auth(client),
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert deleted.headers["location"] == (
            f"/playlists?offline_invalidate=playlist%3A{playlist_id}"
        )


def test_offline_queue_rejects_invalid_playback_metadata(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        _, _, item = _publish_track(session, settings, job_id="offline-invalid-metadata")
        playlist = create_custom_playlist(session, "Invalid queue")
        add_playlist_entry(session, playlist.id, item.id)
        item.files[0].state = MediaFileState.QUARANTINED
        session.commit()
        playlist_id = playlist.id

    with TestClient(app) as client:
        response = client.get(f"/player/queue/playlist/{playlist_id}", auth=_auth(client))
        assert response.status_code == 422
        assert "verified MP3" in response.json()["detail"]


def test_media_identity_headers_are_strong_and_exact(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        _, _, item = _publish_track(session, settings, job_id="offline-media-headers")
        session.commit()
        media_file = item.files[0]
        payload = (settings.paths.outputs / media_file.relative_path).read_bytes()

    with TestClient(app) as client:
        response = client.get(f"/media/library/{media_file.id}", auth=_auth(client))
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"].startswith("audio/mpeg")
        assert response.headers["content-length"] == str(len(payload))
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["etag"] == f'"sha256-{hashlib.sha256(payload).hexdigest()}"'
        assert response.headers["cache-control"] == "private, no-store"

        invalid = client.get(
            f"/media/library/{media_file.id}",
            auth=_auth(client),
            headers={"Range": f"bytes={len(payload)}-"},
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == f"bytes */{len(payload)}"

        assert media_file.format is OutputFormat.MP3


def test_unified_worker_preserves_push_contract_and_scope_isolation(media_web_app) -> None:
    app, _, _ = media_web_app
    with TestClient(app) as client:
        source = client.get("/notification-worker.js")
        assert source.status_code == 200
        body = source.text
        assert 'const APP_SHELL_VERSION = "v2"' in body
        assert 'self.addEventListener("install"' in body
        assert 'self.addEventListener("activate"' in body
        assert 'self.addEventListener("fetch"' in body
        assert 'self.addEventListener("push"' in body
        assert 'self.addEventListener("notificationclick"' in body
        assert 'url.pathname.startsWith("/beta/")' in body
        assert 'data.type === "activate"' in body
        assert 'data.type === "offline-mode"' in body
        assert "cache.addAll(shellUrls())" in body
        assert "bytes */${size}" in body
        assert "Content-Range" in body
        assert "arrayBuffer" not in body

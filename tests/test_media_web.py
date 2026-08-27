from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ace_service.app import create_app
from ace_service.db import create_database_engine, create_session_factory, initialize_database
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


def test_library_queue_media_range_and_download_routes(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        job, _, item = _publish_track(session, settings, job_id="web-library-job")
        playlist = create_custom_playlist(session, "Web mix")
        add_playlist_entry(session, playlist.id, item.id)
        session.commit()
        media_file_id = item.files[0].id
        payload = (settings.paths.outputs / item.files[0].relative_path).read_bytes()

    with TestClient(app) as client:
        assert client.get("/library").status_code == 401
        library = client.get("/library", auth=_auth(client))
        assert library.status_code == 200
        assert item.title in library.text
        assert "Add to playlist" in library.text

        queue = client.get("/player/queue/library", auth=_auth(client))
        assert queue.status_code == 200
        assert queue.headers["cache-control"] == "no-store"
        assert set(queue.json()) == {"items"}
        assert queue.json()["items"][0] == {
            "id": item.id,
            "title": item.title,
            "project_id": job.project_id,
            "project_title": job.project.title,
            "duration_seconds": None,
            "media_url": f"/media/library/{media_file_id}",
            "download_url": f"/files/library/{media_file_id}/download",
        }

        playlist_queue = client.get(f"/player/queue/playlist/{playlist.id}", auth=_auth(client))
        assert playlist_queue.status_code == 200
        assert [entry["id"] for entry in playlist_queue.json()["items"]] == [item.id]

        ranged = client.get(
            f"/media/library/{media_file_id}",
            auth=_auth(client),
            headers={"Range": "bytes=0-0"},
        )
        assert ranged.status_code == 206
        assert ranged.content == payload[:1]
        assert ranged.headers["content-type"].startswith("audio/mpeg")
        assert ranged.headers["content-disposition"].startswith("inline")
        assert ranged.headers["content-range"] == f"bytes 0-0/{len(payload)}"

        download = client.get(f"/files/library/{media_file_id}/download", auth=_auth(client))
        assert download.status_code == 200
        assert download.content == payload
        assert download.headers["content-disposition"].startswith("attachment")


def test_library_media_rename_and_delete_leave_job_tombstone(media_web_app, settings) -> None:
    app, factory, _ = media_web_app
    with factory() as session:
        job, _, item = _publish_track(session, settings, job_id="web-delete-job")
        session.commit()
        media_file_id = item.files[0].id
        job_id = job.id

    with TestClient(app) as client:
        csrf = _csrf(client, "/library")
        renamed = client.post(
            f"/media/{item.id}/rename",
            auth=_auth(client),
            data={"csrf_token": csrf, "title": "Renamed web track"},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert client.get("/library", auth=_auth(client)).text.find("Renamed web track") >= 0

        deleted = client.post(
            f"/media/{item.id}/delete",
            auth=_auth(client),
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert client.get(f"/media/library/{media_file_id}", auth=_auth(client)).status_code == 404
        assert client.get("/player/queue/library", auth=_auth(client)).json()["items"] == []

        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert "Deleted audio" in detail.text
        assert f"/media/library/{media_file_id}" not in detail.text

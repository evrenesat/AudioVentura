from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from ace_home_ingest.app import create_app
from ace_home_ingest.config import HomeIngestSettings
from ace_home_ingest.media import IngestError, PreparedSource, VideoMetadata
from ace_home_ingest.uploader import SFTPUploadError

JOB_ID = UUID("123e4567-e89b-12d3-a456-426614174000")


def run(awaitable):
    return asyncio.run(awaitable)


def make_settings(tmp_path: Path) -> HomeIngestSettings:
    return HomeIngestSettings(data_root=tmp_path / "data", token="home-secret")


class RecordingUploader:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Path, str]] = []
        self.fail = fail

    def upload(self, local_path: Path, job_id: str) -> str:
        self.calls.append((local_path, job_id))
        if self.fail:
            raise SFTPUploadError()
        return f"/srv/incoming/{job_id}/source.mp3.part"


async def request(app, method: str, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://home.test") as client:
        return await client.request(method, path, **kwargs)


def test_api_requires_bearer_and_has_no_public_docs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings, uploader=RecordingUploader())
    payload = {
        "job_id": str(JOB_ID),
        "url": "https://www.youtube.com/watch?v=abc123",
    }
    assert run(request(app, "POST", "/v1/prepare-youtube-cover", json=payload)).status_code == 401
    assert run(request(app, "GET", "/docs")).status_code == 404
    assert run(request(app, "GET", "/openapi.json")).status_code == 404


def test_api_returns_only_verified_metadata_and_cleans_up_on_success(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    uploader = RecordingUploader()

    async def fake_prepare(url: str, job_directory: Path, **kwargs: object) -> PreparedSource:
        job_directory.mkdir(parents=True)
        path = job_directory / "source.mp3"
        path.write_bytes(b"prepared")
        return PreparedSource(
            VideoMetadata("abc123", "Safe title", "https://www.youtube.com/watch?v=abc123", 12.0),
            path,
            8,
            "a" * 64,
        )

    service_app = create_app(settings, uploader=uploader)
    service = service_app.state.service
    service.prepare_source_fn = fake_prepare
    response = run(
        request(
            service_app,
            "POST",
            "/v1/prepare-youtube-cover",
            headers={"Authorization": "Bearer home-secret"},
            json={"job_id": str(JOB_ID), "url": "https://www.youtube.com/watch?v=abc123"},
        )
    )
    assert response.status_code == 200
    assert response.json() == {
        "job_id": str(JOB_ID),
        "video_id": "abc123",
        "title": "Safe title",
        "canonical_url": "https://www.youtube.com/watch?v=abc123",
        "duration_seconds": 12.0,
        "prepared_format": "mp3",
        "prepared_bytes": 8,
        "prepared_sha256": "a" * 64,
    }
    assert uploader.calls[0][1] == str(JOB_ID)
    assert not settings.paths.job_temporary(str(JOB_ID)).exists()


@pytest.mark.parametrize(
    "failure",
    [
        IngestError("ffprobe_failed", "probe failed"),
        SFTPUploadError(),
    ],
)
def test_api_cleans_up_after_prepare_or_upload_failure(
    tmp_path: Path, failure: IngestError
) -> None:
    settings = make_settings(tmp_path)
    uploader = RecordingUploader(fail=isinstance(failure, SFTPUploadError))

    async def fake_prepare(url: str, job_directory: Path, **kwargs: object) -> PreparedSource:
        job_directory.mkdir(parents=True)
        (job_directory / "source.mp3").write_bytes(b"prepared")
        if not isinstance(failure, SFTPUploadError):
            raise failure
        return PreparedSource(
            VideoMetadata("abc123", "Safe title", "https://www.youtube.com/watch?v=abc123", 12.0),
            job_directory / "source.mp3",
            8,
            "a" * 64,
        )

    service_app = create_app(settings, uploader=uploader)
    service = service_app.state.service
    service.prepare_source_fn = fake_prepare
    response = run(
        request(
            service_app,
            "POST",
            "/v1/prepare-youtube-cover",
            headers={"Authorization": "Bearer home-secret"},
            json={"job_id": str(JOB_ID), "url": "https://www.youtube.com/watch?v=abc123"},
        )
    )
    assert response.status_code in {502}
    assert not settings.paths.job_temporary(str(JOB_ID)).exists()

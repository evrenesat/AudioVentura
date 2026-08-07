from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobType, TransferDirection, TransferStatus, utc_now
from ace_service.repository import (
    get_output_by_path,
    get_transfer_by_token,
    issue_transfer_capability,
)
from ace_service.transfers import (
    _receive_output,
    create_transfer_app,
    issue_transfer_url,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine, create_session_factory(engine)


def _path(url: str) -> str:
    return urlsplit(url).path


async def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transfer.test") as client:
        return await client.request(method, path, **kwargs)


def test_source_download_repeats_and_public_app_has_no_other_routes(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.COVER, job_id="job-source")
            source = settings.paths.incoming / job.id / "source.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source mp3")
            issued = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.SOURCE_DOWNLOAD,
                expected_relative_path=f"{job.id}/source.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                token="source-token",
            )
            session.commit()
        app = create_transfer_app(settings, session_factory=factory)
        first = _run(_request(app, "GET", _path(issued.url)))
        second = _run(_request(app, "GET", _path(issued.url)))
        assert first.status_code == second.status_code == 200
        assert first.content == second.content == b"source mp3"
        assert first.headers["content-type"] == "audio/mpeg"
        assert _run(_request(app, "GET", "/")).status_code == 404
        assert _run(_request(app, "GET", "/docs")).status_code == 404
        assert _run(_request(app, "GET", "/openapi.json")).status_code == 404
    finally:
        engine.dispose()


def test_source_rejects_size_symlink_and_wrong_direction(settings, tmp_path: Path) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.COVER, job_id="job-source")
            source = settings.paths.incoming / job.id / "source.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"too large")
            oversized = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.SOURCE_DOWNLOAD,
                expected_relative_path=f"{job.id}/source.mp3",
                expected_extension=".mp3",
                max_bytes=3,
                token="oversized-source",
            )
            outside = tmp_path / "outside.mp3"
            outside.write_bytes(b"outside")
            link = settings.paths.incoming / job.id / "link.mp3"
            link.symlink_to(outside)
            escaped = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.SOURCE_DOWNLOAD,
                expected_relative_path=f"{job.id}/link.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                token="escaped-source",
            )
            wrong_direction = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.OUTPUT_UPLOAD,
                expected_relative_path=f"{job.id}/source.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                token="wrong-direction",
            )
            session.commit()
        app = create_transfer_app(settings, session_factory=factory)
        assert _run(_request(app, "GET", _path(oversized.url))).status_code == 413
        assert _run(_request(app, "GET", _path(escaped.url))).status_code == 404
        token = wrong_direction.url.rsplit("/", 1)[-1]
        assert _run(_request(app, "GET", f"/transfer/v1/source/{token}")).status_code == 404
    finally:
        engine.dispose()


def test_output_upload_is_atomic_and_retries_are_idempotent_or_conflicting(settings) -> None:
    payload = b"generated mp3"
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-output")
            issued = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.OUTPUT_UPLOAD,
                expected_relative_path=f"{job.id}/variation-01.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                token="output-token",
            )
            session.commit()
        app = create_transfer_app(settings, session_factory=factory)
        path = _path(issued.url)
        accepted = _run(
            _request(
                app,
                "PUT",
                path,
                content=payload,
                headers={"X-ACE-Output-SHA256": hashlib.sha256(payload).hexdigest()},
            )
        )
        assert accepted.status_code == 200
        final_path = settings.paths.outputs / "job-output" / "variation-01.mp3"
        assert final_path.read_bytes() == payload
        assert not final_path.with_name("variation-01.mp3.part").exists()
        with factory() as session:
            output = get_output_by_path(
                session, job_id="job-output", relative_path="job-output/variation-01.mp3"
            )
            capability = get_transfer_by_token(session, "output-token")
            assert output is not None
            assert capability is not None
            assert capability.status is TransferStatus.CONSUMED
        identical = _run(_request(app, "PUT", path, content=payload))
        assert identical.status_code == 200
        conflicting = _run(_request(app, "PUT", path, content=b"different mp3"))
        assert conflicting.status_code == 409
        assert final_path.read_bytes() == payload
        assert not final_path.with_name("variation-01.mp3.part").exists()
    finally:
        engine.dispose()


def test_expired_consumed_output_replay_is_rejected_without_mutation(settings) -> None:
    payload = b"generated mp3"
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-expired-output")
            issued = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.OUTPUT_UPLOAD,
                expected_relative_path=f"{job.id}/variation-01.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                token="expired-output-token",
            )
            session.commit()
        app = create_transfer_app(settings, session_factory=factory)
        path = _path(issued.url)
        accepted = _run(_request(app, "PUT", path, content=payload))
        assert accepted.status_code == 200

        final_path = settings.paths.outputs / "job-expired-output" / "variation-01.mp3"
        accepted_bytes = final_path.read_bytes()
        with factory() as session:
            capability = get_transfer_by_token(session, "expired-output-token")
            assert capability is not None
            assert capability.status is TransferStatus.CONSUMED
            consumed_at = capability.consumed_at
            assert consumed_at is not None
            capability.expires_at = utc_now() - timedelta(seconds=1)
            session.commit()

        replay = _run(_request(app, "PUT", path, content=payload))
        assert replay.status_code == 404
        assert final_path.read_bytes() == accepted_bytes == payload
        assert not final_path.with_name("variation-01.mp3.part").exists()
        with factory() as session:
            capability = get_transfer_by_token(session, "expired-output-token")
            assert capability is not None
            assert capability.status is TransferStatus.CONSUMED
            assert capability.consumed_at == consumed_at
    finally:
        engine.dispose()


def test_oversized_and_interrupted_uploads_leave_no_part(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-output")
            issued = issue_transfer_url(
                session,
                settings,
                job_id=job.id,
                direction=TransferDirection.OUTPUT_UPLOAD,
                expected_relative_path=f"{job.id}/output.mp3",
                expected_extension=".mp3",
                max_bytes=3,
                token="small-output",
            )
            session.commit()
        app = create_transfer_app(settings, session_factory=factory)
        path = _path(issued.url)
        response = _run(_request(app, "PUT", path, content=b"1234"))
        assert response.status_code == 413
        final_path = settings.paths.outputs / "job-output" / "output.mp3"
        assert not final_path.exists()
        assert not final_path.with_name("output.mp3.part").exists()

        messages = iter(
            [
                {"type": "http.request", "body": b"12", "more_body": True},
            ]
        )

        async def receive():
            try:
                return next(messages)
            except StopIteration as exc:
                raise RuntimeError("interrupted request") from exc

        request = Request(
            {
                "type": "http",
                "method": "PUT",
                "path": path,
                "headers": [],
                "query_string": b"",
                "server": ("transfer.test", 80),
                "client": ("127.0.0.1", 1),
            },
            receive,
        )
        with pytest.raises(HTTPException):
            _run(
                _receive_output(
                    factory,
                    settings,
                    issued.capability.id,
                    "small-output",
                    final_path,
                    3,
                    request,
                )
            )
        assert not final_path.with_name("output.mp3.part").exists()
    finally:
        engine.dispose()


def test_capability_rejects_traversal_and_expiry(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            from ace_service.repository import create_job

            job = create_job(session, job_type=JobType.COVER)
            with pytest.raises(ValueError):
                issue_transfer_url(
                    session,
                    settings,
                    job_id=job.id,
                    direction=TransferDirection.SOURCE_DOWNLOAD,
                    expected_relative_path="../escape.mp3",
                    expected_extension=".mp3",
                    max_bytes=100,
                )
            expired = issue_transfer_capability(
                session,
                job_id=job.id,
                direction=TransferDirection.SOURCE_DOWNLOAD,
                expected_relative_path=f"{job.id}/source.mp3",
                expected_extension=".mp3",
                max_bytes=100,
                expires_at=utc_now() - timedelta(seconds=1),
                token="expired",
            )
            session.commit()
        assert expired.capability.token_sha256 != "expired"
        app = create_transfer_app(settings, session_factory=factory)
        assert _run(_request(app, "GET", "/transfer/v1/source/expired")).status_code == 404
    finally:
        engine.dispose()

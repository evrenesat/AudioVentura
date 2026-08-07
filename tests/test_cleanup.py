from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ace_service.app import create_app
from ace_service.cleanup import cleanup_controller
from ace_service.db import create_session_factory
from ace_service.models import JobStatus, JobType, TransferDirection, TransferStatus
from ace_service.repository import (
    create_job,
    get_transfer_by_token,
    issue_transfer_capability,
    transition_job,
)


def test_controller_cleanup_reclaims_stale_files_and_terminal_state(
    settings, session, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    settings.cleanup_stale_after_seconds = 86_400
    settings.transfer_record_retention_seconds = 60
    settings.retain_cover_source = False
    settings.ensure_data_layout()

    stale_part = settings.paths.incoming / "stale" / "source.mp3.part"
    fresh_part = settings.paths.incoming / "fresh" / "source.mp3.part"
    stale_part.parent.mkdir(parents=True)
    fresh_part.parent.mkdir(parents=True)
    stale_part.write_bytes(b"stale")
    fresh_part.write_bytes(b"fresh")
    old_mtime = (now - timedelta(days=2)).timestamp()
    os.utime(stale_part, (old_mtime, old_mtime))
    completed_output = settings.paths.outputs / "completed" / "variation-01.mp3"
    completed_output.parent.mkdir(parents=True)
    completed_output.write_bytes(b"retained output")

    cover = create_job(session, job_type=JobType.COVER)
    cover_source = settings.paths.incoming / cover.id / "source.mp3"
    cover_source.parent.mkdir(parents=True)
    cover_source.write_bytes(b"cover")
    transition_job(session, cover.id, JobStatus.FAILED)

    original = create_job(session, job_type=JobType.ORIGINAL)
    issued = issue_transfer_capability(
        session,
        job_id=original.id,
        direction=TransferDirection.OUTPUT_UPLOAD,
        expected_relative_path=f"{original.id}/variation-01.mp3",
        expected_extension="mp3",
        max_bytes=1024,
        expires_at=now - timedelta(days=2),
        token="expired-cleanup-token",
    )
    active = issue_transfer_capability(
        session,
        job_id=cover.id,
        direction=TransferDirection.SOURCE_DOWNLOAD,
        expected_relative_path=f"{cover.id}/source.mp3",
        expected_extension="mp3",
        max_bytes=1024,
        expires_at=now + timedelta(days=1),
        token="revoked-cleanup-token",
    )
    session.commit()

    factory = create_session_factory(session.get_bind())
    report = cleanup_controller(settings, factory, now=now)

    assert report.stale_part_files == 1
    assert report.expired_capabilities == 1
    assert report.revoked_capabilities == 1
    assert report.removed_cover_sources == 1
    assert not stale_part.exists()
    assert fresh_part.exists()
    assert not cover_source.exists()
    assert completed_output.exists()

    with factory() as check:
        assert get_transfer_by_token(check, issued.token) is None
        assert get_transfer_by_token(check, active.token) is not None
        active_capability = get_transfer_by_token(check, active.token)
        assert active_capability is not None
        assert active_capability.status is TransferStatus.REVOKED


def test_controller_lifespan_schedules_and_cancels_periodic_cleanup(settings) -> None:
    from ace_service.db import create_database_engine, initialize_database

    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=object(),
        home_ingest_client=object(),
        worker=object(),
    )

    from fastapi.testclient import TestClient

    with TestClient(app):
        task = app.state.cleanup_task
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    assert task.done()
    engine.dispose()

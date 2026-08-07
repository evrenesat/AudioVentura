from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from ace_service.db import create_database_engine, initialize_database
from ace_service.models import (
    JobStatus,
    JobType,
    OutputFormat,
    TransferDirection,
    TransferStatus,
    utc_now,
)
from ace_service.repository import (
    consume_transfer,
    create_job,
    create_output,
    get_active_transfer,
    get_job,
    get_transfer_by_token,
    issue_transfer_capability,
    revoke_transfer,
)
from ace_service.schemas import normalize_relative_path, resolve_relative_path


def test_sqlite_initialization_enables_required_pragmas(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    engine.dispose()


def test_sqlite_round_trip_for_job_output_and_transfer(session) -> None:
    job = create_job(
        session,
        job_type=JobType.ORIGINAL,
        prompt="warm analog synth",
        output_format=OutputFormat.MP3,
        variation_count=2,
        normalized_request_json={"prompt": "warm analog synth", "variations": 2},
    )
    output = create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=0,
        relative_path=f"outputs/{job.id}/variation-01.mp3",
        mime_type="audio/mpeg",
        byte_size=1234,
        sha256="A" * 64,
        seed_metadata_json={"seed": 17},
    )
    issued = issue_transfer_capability(
        session,
        job_id=job.id,
        direction=TransferDirection.OUTPUT_UPLOAD,
        expected_relative_path=output.relative_path,
        expected_extension="mp3",
        max_bytes=10000,
        expires_at=utc_now() + timedelta(hours=1),
        token="test-token",
    )
    session.commit()

    loaded_job = get_job(session, job.id)
    loaded_transfer = get_transfer_by_token(session, "test-token")
    assert loaded_job is not None
    assert loaded_job.status is JobStatus.QUEUED
    assert loaded_job.normalized_request_json == {"prompt": "warm analog synth", "variations": 2}
    assert loaded_job.created_at.tzinfo is not None
    assert loaded_transfer is not None
    assert loaded_transfer.token_sha256 != "test-token"
    assert issued.token == "test-token"

    consumed = consume_transfer(session, issued.capability.id)
    assert consumed.status is TransferStatus.CONSUMED
    assert get_active_transfer(session, "test-token") is None


def test_transfer_expiry_and_revocation(session) -> None:
    job = create_job(session, job_type=JobType.COVER)
    expired = issue_transfer_capability(
        session,
        job_id=job.id,
        direction=TransferDirection.SOURCE_DOWNLOAD,
        expected_relative_path="incoming/source.mp3",
        expected_extension=".mp3",
        max_bytes=100,
        expires_at=utc_now() - timedelta(seconds=1),
        token="expired-token",
    )
    assert get_active_transfer(session, "expired-token") is None
    assert expired.capability.status is TransferStatus.EXPIRED

    revoked = issue_transfer_capability(
        session,
        job_id=job.id,
        direction=TransferDirection.SOURCE_DOWNLOAD,
        expected_relative_path="incoming/source-2.mp3",
        expected_extension=".mp3",
        max_bytes=100,
        expires_at=utc_now() + timedelta(hours=1),
        token="revoked-token",
    )
    revoke_transfer(session, revoked.capability.id)
    assert get_active_transfer(session, "revoked-token") is None


@pytest.mark.parametrize(
    "value", ["../escape.mp3", "/absolute.mp3", "a/../../escape", "C:\\escape.mp3"]
)
def test_relative_path_traversal_is_rejected(tmp_path, value: str) -> None:
    with pytest.raises(ValueError):
        normalize_relative_path(value)
    with pytest.raises(ValueError):
        resolve_relative_path(tmp_path, value)


def test_relative_path_resolution_stays_under_root(tmp_path) -> None:
    resolved = resolve_relative_path(tmp_path, "jobs/one/output.mp3")
    assert resolved == (tmp_path / "jobs/one/output.mp3").resolve()

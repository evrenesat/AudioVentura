from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import (
    JobStatus,
    JobType,
    OutputFormat,
    TransferDirection,
    TransferStatus,
    utc_now,
)
from ace_service.repository import (
    check_schema_v1_rollback_readiness,
    consume_transfer,
    create_job,
    create_output,
    get_active_transfer,
    get_job,
    get_transfer_by_token,
    issue_transfer_capability,
    revoke_transfer,
)
from ace_service.rollback_readiness import main as rollback_readiness_main
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


def _logical_database_snapshot(database: Path) -> tuple[str, ...]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return tuple(connection.iterdump())


def _v2_request(
    job_type: JobType, *, staging: dict[str, object] | None = None
) -> dict[str, object]:
    value: dict[str, object] = {"schema_version": 2, "task_type": job_type.value}
    if staging is not None:
        value["cover_staging"] = staging
    return value


def test_schema_v1_rollback_check_empty_database_is_read_only(settings, capsys) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    database = settings.paths.database
    before = _logical_database_snapshot(database)
    try:
        assert rollback_readiness_main(["--database", str(database)]) == 0
        output = capsys.readouterr()
        assert output.out == "rollback=safe blockers=0\n"
        assert output.err == ""
        assert _logical_database_snapshot(database) == before
    finally:
        engine.dispose()


def test_schema_v1_rollback_check_ignores_terminal_v2_and_historical_rows(settings, capsys) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    database = settings.paths.database
    try:
        with create_session_factory(engine)() as session:
            terminal = create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id="terminal-v2",
                normalized_request_json=_v2_request(JobType.ORIGINAL),
            )
            terminal.status = JobStatus.COMPLETED
            create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id="legacy-v1",
                normalized_request_json={"schema_version": 1},
            )
            unknown = create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id="unknown-history",
                normalized_request_json={"schema_version": 99},
            )
            unknown.status = JobStatus.GENERATING
            session.commit()
        before = _logical_database_snapshot(database)

        assert rollback_readiness_main(["--database", str(database)]) == 0
        output = capsys.readouterr().out
        assert "job_id=terminal-v2 status=completed schema=v2 classification=terminal" in output
        assert "job_id=legacy-v1 status=queued schema=v1 classification=legacy_or_unknown" in output
        assert "job_id=unknown-history status=generating schema=unknown" in output
        assert output.endswith("rollback=safe blockers=0\n")
        assert _logical_database_snapshot(database) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.QUEUED,
        JobStatus.INGESTING,
        JobStatus.STAGING,
        JobStatus.CLOUD_QUEUED,
        JobStatus.GENERATING,
    ],
)
def test_schema_v1_rollback_check_blocks_every_nonterminal_v2_status(
    settings, capsys, status: JobStatus
) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    database = settings.paths.database
    try:
        with create_session_factory(engine)() as session:
            job = create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id=f"v2-{status.value}",
                normalized_request_json=_v2_request(JobType.ORIGINAL),
            )
            job.status = status
            session.commit()
        before = _logical_database_snapshot(database)

        assert rollback_readiness_main(["--database", str(database)]) == 1
        output = capsys.readouterr().out
        assert (
            f"job_id=v2-{status.value} status={status.value} "
            "schema=v2 classification=nonterminal_v2"
        ) in output
        assert output.endswith("rollback=not-safe blockers=1\n")
        assert _logical_database_snapshot(database) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("staging_status", "classification"),
    [
        ("awaiting_confirmation", "unconfirmed_v2_cover_staging"),
        ("confirmed", "nonterminal_v2"),
    ],
)
def test_schema_v1_rollback_check_identifies_cover_staging_state(
    settings, capsys, staging_status: str, classification: str
) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    database = settings.paths.database
    try:
        with create_session_factory(engine)() as session:
            job = create_job(
                session,
                job_type=JobType.COVER,
                job_id=f"cover-{staging_status}",
                normalized_request_json=_v2_request(
                    JobType.COVER,
                    staging={"status": staging_status, "staged_at": "2026-08-08T00:00:00+00:00"},
                ),
            )
            job.status = JobStatus.STAGING
            session.commit()
        before = _logical_database_snapshot(database)

        assert rollback_readiness_main(["--database", str(database)]) == 1
        output = capsys.readouterr().out
        assert f"schema=v2 classification={classification}" in output
        assert output.endswith("rollback=not-safe blockers=1\n")
        assert _logical_database_snapshot(database) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "normalized",
    [
        {"schema_version": 2, "task_type": "cover", "cover_staging": {"status": "bad"}},
        {
            "schema_version": 2,
            "task_type": "original",
            "cover_staging": {"status": "confirmed"},
        },
        {"schema_version": 2, "task_type": "cover", "cover_staging": "bad"},
        {"schema_version": 2, "task_type": "cover", "cover_staging": None},
    ],
)
def test_schema_v1_rollback_check_fails_closed_for_malformed_v2_lifecycle(
    settings, capsys, normalized: dict[str, object]
) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    database = settings.paths.database
    try:
        job_type = JobType.COVER if normalized.get("task_type") == "cover" else JobType.ORIGINAL
        with create_session_factory(engine)() as session:
            job = create_job(
                session,
                job_type=job_type,
                job_id="malformed-v2",
                normalized_request_json=normalized,
            )
            job.status = JobStatus.COMPLETED
            session.commit()
        before = _logical_database_snapshot(database)

        assert rollback_readiness_main(["--database", str(database)]) == 1
        output = capsys.readouterr().out
        assert "schema=v2 classification=malformed_v2_lifecycle" in output
        assert output.endswith("rollback=not-safe blockers=1\n")
        assert _logical_database_snapshot(database) == before
    finally:
        engine.dispose()


def test_schema_v1_rollback_repository_result_is_bounded_and_read_only(session) -> None:
    job = create_job(
        session,
        job_type=JobType.ORIGINAL,
        normalized_request_json=_v2_request(JobType.ORIGINAL),
    )
    result = check_schema_v1_rollback_readiness(session)
    assert result.safe is False
    assert result.blockers[0].job_id == job.id
    assert result.blockers[0].classification == "nonterminal_v2"

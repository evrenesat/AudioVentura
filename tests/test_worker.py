from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping

import pytest

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus, JobType
from ace_service.repository import (
    create_job,
    create_output,
    get_job,
    get_variation_attempt,
    persist_variation_runpod_job_id,
    prepare_variation_submission,
    transition_job,
    transition_variation_attempt,
)
from ace_service.runpod_client import RunpodError, RunpodState, RunpodStatusResult
from ace_service.worker import ControllerWorker


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine, create_session_factory(engine)


class _FakeRunpod:
    def __init__(self, settings, factory, *, fail_status: bool = False) -> None:
        self.settings = settings
        self.factory = factory
        self.fail_status = fail_status
        self.submissions: list[str] = []
        self.status_calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def submit(
        self, payload: Mapping[str, object], execution_timeout_ms: int, ttl_ms: int
    ) -> str:
        assert execution_timeout_ms > 0
        assert ttl_ms > 0
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            variation_index = int(payload["variation_index"])
            job_id = str(payload["job_id"])
            runpod_job_id = f"runpod-{variation_index}"
            self.submissions.append(runpod_job_id)
            self._write_output(job_id, variation_index)
            return runpod_job_id
        finally:
            self.active -= 1

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        self.status_calls.append(runpod_job_id)
        if self.fail_status:
            raise RunpodError("status expired")
        return RunpodStatusResult(
            job_id=runpod_job_id,
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
        )

    def _write_output(self, job_id: str, variation_index: int) -> None:
        payload = f"generated-{variation_index}".encode()
        relative_path = f"{job_id}/variation-{variation_index:02d}.mp3"
        path = self.settings.paths.outputs / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        with self.factory() as session:
            create_output(
                session,
                job_id=job_id,
                variation_index=variation_index,
                result_index=0,
                runpod_job_id=f"runpod-{variation_index}",
                relative_path=relative_path,
                mime_type="audio/mpeg",
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            session.commit()


def test_four_variations_are_serialized_and_each_gets_one_runpod_job(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id="job-four",
                variation_count=4,
                normalized_request_json={"prompt": "serialized"},
            )
            session.commit()
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-four")
            assert job is not None
            assert job.status is JobStatus.COMPLETED
            attempts = [get_variation_attempt(session, job.id, index) for index in range(1, 5)]
            assert all(attempt is not None for attempt in attempts)
            assert [attempt.runpod_job_id for attempt in attempts if attempt is not None] == [
                "runpod-1",
                "runpod-2",
                "runpod-3",
                "runpod-4",
            ]
        assert fake.submissions == ["runpod-1", "runpod-2", "runpod-3", "runpod-4"]
        assert fake.max_active == 1
    finally:
        engine.dispose()


def test_enqueue_deduplicates_and_restart_polls_without_resubmission(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-restart")
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "persisted-runpod", submission_nonce=nonce
            )
            session.commit()
        fake = _FakeRunpod(settings, factory)
        fake._write_output("job-restart", 1)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            assert worker.enqueue("job-restart") is False
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-restart")
            assert job is not None and job.status is JobStatus.COMPLETED
        assert fake.submissions == []
        assert fake.status_calls == ["persisted-runpod"]
    finally:
        engine.dispose()


def test_uncertain_submission_fails_without_automatic_resubmission(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-uncertain")
            prepare_variation_submission(session, job.id, 1)
            session.commit()
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-uncertain")
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.error_code == "uncertain_cloud_submission"
        assert fake.submissions == []
    finally:
        engine.dispose()


def test_restart_recovery_handles_interrupted_ingest_and_missing_cloud_id(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            interrupted = create_job(session, job_type=JobType.COVER, job_id="job-ingest")
            transition_job(session, interrupted.id, JobStatus.INGESTING)
            missing = create_job(session, job_type=JobType.ORIGINAL, job_id="job-missing")
            transition_job(session, missing.id, JobStatus.CLOUD_QUEUED)
            session.commit()
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            interrupted = get_job(session, "job-ingest")
            missing = get_job(session, "job-missing")
            assert interrupted is not None and interrupted.error_code == "ingest_interrupted"
            assert missing is not None and missing.error_code == "missing_runpod_job_id"
        assert fake.submissions == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("initial_status", "missing_metadata", "expected_error"),
    [
        (JobStatus.INGESTING, "source_byte_size", "ingest_interrupted"),
        (JobStatus.INGESTING, "source_sha256", "ingest_interrupted"),
        (JobStatus.STAGING, "source_byte_size", "controller_task_error"),
        (JobStatus.STAGING, "source_sha256", "controller_task_error"),
    ],
)
def test_restart_rejects_final_source_without_verification_metadata(
    settings, initial_status, missing_metadata, expected_error
) -> None:
    engine, factory = _database(settings)
    job_id = f"job-{initial_status.value}-{missing_metadata}"
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.COVER, job_id=job_id)
            transition_job(session, job.id, JobStatus.INGESTING)
            if initial_status is JobStatus.STAGING:
                transition_job(session, job.id, JobStatus.STAGING)
            source = settings.paths.incoming / job.id / "source.mp3"
            source_data = b"final-named but unverified source"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(source_data)
            if missing_metadata != "source_byte_size":
                job.source_byte_size = len(source_data)
            if missing_metadata != "source_sha256":
                job.source_sha256 = hashlib.sha256(source_data).hexdigest()
            session.commit()
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.error_code == expected_error
        assert fake.submissions == []
    finally:
        engine.dispose()


def test_verified_staging_source_continues_to_cloud(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.COVER, job_id="job-staged")
            transition_job(session, job.id, JobStatus.INGESTING)
            transition_job(session, job.id, JobStatus.STAGING)
            source = settings.paths.incoming / job.id / "source.mp3"
            source_data = b"prepared source"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(source_data)
            job.source_byte_size = len(source_data)
            job.source_sha256 = hashlib.sha256(source_data).hexdigest()
            session.commit()
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-staged")
            assert job is not None and job.status is JobStatus.COMPLETED
        assert fake.submissions == ["runpod-1"]
    finally:
        engine.dispose()


def test_second_controller_cannot_acquire_same_data_root(settings) -> None:
    engine, factory = _database(settings)
    try:
        fake = _FakeRunpod(settings, factory)

        async def scenario() -> None:
            first = ControllerWorker(settings, factory, fake)
            second = ControllerWorker(settings, factory, fake)
            await first.start()
            try:
                with pytest.raises(RuntimeError):
                    await second.start()
            finally:
                await first.stop()

        _run(scenario())
    finally:
        engine.dispose()


def test_status_expiry_recovers_from_valid_local_upload(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-stale")
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "stale-runpod", submission_nonce=nonce
            )
            transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
            transition_job(session, job.id, JobStatus.GENERATING)
            session.commit()
        fake = _FakeRunpod(settings, factory, fail_status=True)
        fake._write_output("job-stale", 1)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-stale")
            attempt = get_variation_attempt(session, "job-stale", 1)
            assert job is not None and job.status is JobStatus.COMPLETED
            assert attempt is not None
            assert attempt.runpod_result_json == {
                "recovery_note": "runpod_status_unavailable_after_output"
            }
    finally:
        engine.dispose()

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import timedelta

import pytest

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus, JobType, utc_now
from ace_service.providers.base import (
    CancelOutcome,
    InferenceMode,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    ProviderStatus,
    RequestFeature,
)
from ace_service.providers.registry import ProviderRegistry
from ace_service.repository import (
    create_job,
    create_original_job,
    create_output,
    create_variation_attempt,
    get_job,
    get_variation_attempt,
    persist_variation_provider_job_ref,
    persist_variation_runpod_job_id,
    prepare_variation_submission,
    set_variation_progress,
    set_variation_runpod_result,
    sum_terminal_attempt_estimates,
    transition_job,
    transition_variation_attempt,
)
from ace_service.runpod_client import RunpodError, RunpodHealth, RunpodState, RunpodStatusResult
from ace_service.schemas import OriginalSongRequest
from ace_service.worker import ControllerWorker


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine, create_session_factory(engine)


class _GenericSalad:
    capabilities = ProviderCapabilities(
        ProviderName.SALAD,
        frozenset(InferenceMode),
        frozenset(RequestFeature),
        frozenset({2}),
        True,
        False,
        False,
    )

    def __init__(self, *, error: bool = False, cancel: CancelOutcome = CancelOutcome.TOO_LATE):
        self.error = error
        self.cancel_outcome = cancel
        self.cancelled: list[str] = []

    async def submit(self, request):
        raise AssertionError("poll tests do not submit")

    async def status(self, ref):
        if self.error:
            raise ProviderError(ProviderErrorKind.TRANSIENT, "status", "temporary")
        return ProviderStatus(ProviderPhase.QUEUED)

    async def result(self, ref):
        raise AssertionError("queued jobs have no result")

    async def cancel(self, ref):
        self.cancelled.append(ref.external_id)
        return self.cancel_outcome

    async def health(self):
        return ProviderHealth(True, "ready")


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


class _CompletionEvidenceRunpod:
    def __init__(self, result: dict[str, object], *, fail_status: bool) -> None:
        self.result = result
        self.fail_status = fail_status
        self.submissions: list[str] = []

    async def submit(
        self, payload: Mapping[str, object], execution_timeout_ms: int, ttl_ms: int
    ) -> str:
        raise AssertionError("completion-evidence tests must not resubmit")

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        if self.fail_status:
            raise RunpodError("status expired")
        return RunpodStatusResult(
            job_id=runpod_job_id,
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
            result=self.result,
        )


def _completion_evidence(
    *, job_id: str, submission_nonce: str, variation_index: int, output_bytes: bytes
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "job_id": job_id,
        "submission_nonce": submission_nonce,
        "variation_index": variation_index,
        "status": "uploaded",
        "profile_id": "fast-beta-v1",
        "input": {"caption": "correlation test", "lyrics": ""},
        "effective": {"caption": "correlation test", "lyrics": ""},
        "resolved_parameters": {"seed": 123},
        "generated_metadata": {"bpm": 120},
        "output": {
            "bytes": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
            "effective_seed": 123,
            "seed": 123,
        },
        "worker": {
            "ace_tag": "v0.1.8",
            "dit_model": "test-model",
            "lm_model": "test-lm",
            "image_digest": "sha256:" + "d" * 64,
            "gpu": "test-gpu",
            "model_bundle": {
                "repo": "evrenesat/audioventura-ace-step-v0.1.8",
                "revision": "6f196b2c116474c43a96fc8331ebcd2057e18eef",
                "tag": "av-v0.1.8-bundle-1",
                "manifest_sha256": "c" * 64,
            },
        },
    }


def test_variation_progress_is_bounded_monotonic_and_terminal_result_replaces_it(
    session,
) -> None:
    job = create_original_job(
        session,
        OriginalSongRequest(description="progress persistence"),
        job_id="job-progress-persistence",
    )
    _, attempt, _ = prepare_variation_submission(session, job.id, 1)
    set_variation_progress(session, attempt.id, "cloud_wait")
    set_variation_progress(session, attempt.id, "generation")
    set_variation_progress(session, attempt.id, "worker_running")
    session.commit()
    session.expire_all()

    persisted = get_variation_attempt(session, job.id, 1)
    assert persisted is not None
    assert persisted.runpod_result_json["phase"] == "generation"
    set_variation_runpod_result(session, persisted.id, {"schema_version": 2, "output": {}})
    assert persisted.runpod_result_json == {"schema_version": 2, "output": {}}


class _PhaseRunpod:
    def __init__(self, result: RunpodStatusResult, *, initializing: int = 0) -> None:
        self.result = result
        self.initializing = initializing
        self.health_calls = 0

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        assert runpod_job_id == "runpod-progress"
        return self.result

    async def health(self) -> RunpodHealth:
        self.health_calls += 1
        return RunpodHealth(
            details={
                "workers": {
                    "idle": 0,
                    "running": 0,
                    "initializing": self.initializing,
                    "unhealthy": 0,
                },
                "jobs": {"inQueue": 1, "inProgress": 0},
            }
        )


@pytest.mark.parametrize(
    ("result", "initializing", "expected_phase", "health_calls"),
    [
        (
            RunpodStatusResult(
                job_id="runpod-progress",
                category=RunpodState.CLOUD_QUEUED,
                raw_status="IN_QUEUE",
            ),
            0,
            "cloud_wait",
            1,
        ),
        (
            RunpodStatusResult(
                job_id="runpod-progress",
                category=RunpodState.CLOUD_QUEUED,
                raw_status="IN_QUEUE",
            ),
            1,
            "worker_initializing",
            1,
        ),
        (
            RunpodStatusResult(
                job_id="runpod-progress",
                category=RunpodState.GENERATING,
                raw_status="IN_PROGRESS",
            ),
            0,
            "worker_running",
            0,
        ),
        (
            RunpodStatusResult(
                job_id="runpod-progress",
                category=RunpodState.GENERATING,
                raw_status="IN_PROGRESS",
                progress={
                    "kind": "audioventura_progress_v1",
                    "phase": "source_download",
                    "sequence": 10,
                },
            ),
            0,
            "source_download",
            0,
        ),
    ],
)
def test_controller_persists_evidence_backed_phase(
    settings,
    result: RunpodStatusResult,
    initializing: int,
    expected_phase: str,
    health_calls: int,
) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(description="phase orchestration"),
                job_id="job-phase-orchestration",
            )
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "runpod-progress", submission_nonce=nonce
            )
            session.commit()
        fake = _PhaseRunpod(result, initializing=initializing)
        worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)  # type: ignore[arg-type]

        _run(worker._poll_variation("job-phase-orchestration", 1))

        with factory() as session:
            attempt = get_variation_attempt(session, "job-phase-orchestration", 1)
            assert attempt is not None
            assert attempt.runpod_result_json["phase"] == expected_phase
        assert fake.health_calls == health_calls
    finally:
        engine.dispose()


def _create_completion_evidence_case(
    settings, factory, *, result_before_upload: bool, mismatch_field: str | None = None
) -> tuple[str, dict[str, object], bytes]:
    job_id = "job-completion-correlation"
    output_bytes = b"correlated output"
    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(description="correlation test", seed=123),
            job_id=job_id,
        )
        _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
        persist_variation_runpod_job_id(
            session, attempt.id, "correlation-runpod", submission_nonce=nonce
        )
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        transition_job(session, job.id, JobStatus.GENERATING)
        session.commit()

    result = _completion_evidence(
        job_id=job_id,
        submission_nonce=nonce,
        variation_index=1,
        output_bytes=output_bytes,
    )
    if mismatch_field == "job_id":
        result["job_id"] = "another-job"
    elif mismatch_field == "submission_nonce":
        result["submission_nonce"] = "another-nonce"
    elif mismatch_field == "variation_index":
        result["variation_index"] = 2
    elif mismatch_field in {"bytes", "sha256"}:
        output = result["output"]
        assert isinstance(output, dict)
        output = dict(output)
        output[mismatch_field] = int(output["bytes"]) + 1 if mismatch_field == "bytes" else "b" * 64
        result["output"] = output
    elif mismatch_field == "model_revision":
        worker = result["worker"]
        assert isinstance(worker, dict)
        model_bundle = worker["model_bundle"]
        assert isinstance(model_bundle, dict)
        model_bundle["revision"] = "MAIN"
    if result_before_upload:
        with factory() as session:
            attempt = get_variation_attempt(session, job_id, 1)
            assert attempt is not None
            set_variation_runpod_result(session, attempt.id, result)
            session.commit()

    output_path = settings.paths.outputs / job_id / "variation-01.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)
    with factory() as session:
        create_output(
            session,
            job_id=job_id,
            variation_index=1,
            result_index=0,
            runpod_job_id="correlation-runpod",
            relative_path=f"{job_id}/variation-01.mp3",
            mime_type="audio/mpeg",
            byte_size=len(output_bytes),
            sha256=hashlib.sha256(output_bytes).hexdigest(),
        )
        session.commit()
    return job_id, result, output_bytes


@pytest.mark.parametrize(
    "mismatch_field",
    ["job_id", "submission_nonce", "variation_index", "bytes", "sha256", "model_revision"],
)
@pytest.mark.parametrize("result_before_upload", [False, True])
def test_schema_v2_completion_evidence_mismatch_fails_closed(
    settings, mismatch_field: str, result_before_upload: bool
) -> None:
    engine, factory = _database(settings)
    try:
        job_id, result, _ = _create_completion_evidence_case(
            settings,
            factory,
            result_before_upload=result_before_upload,
            mismatch_field=mismatch_field,
        )

        runpod = _CompletionEvidenceRunpod(result, fail_status=result_before_upload)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, runpod, poll_interval_seconds=0)
            if result_before_upload:
                await worker._poll_variation(job_id, 1)
            else:
                await worker.start()
                await worker.wait_idle()
                await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, 1)
            expected = JobStatus.GENERATING if result_before_upload else JobStatus.FAILED
            assert job is not None and job.status is expected
            assert attempt is not None and attempt.status is expected
            output = job.outputs[0]
            assert output.seed_metadata_json is None
            assert output.generation_metadata_json is None
        assert runpod.submissions == []
    finally:
        engine.dispose()


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
        assert fake.status_calls == ["persisted-runpod", "persisted-runpod"]
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
            attempt = get_variation_attempt(session, job.id, 1)
            assert attempt is not None and attempt.status is JobStatus.FAILED
            assert attempt.evidence_status == "unavailable"
            assert attempt.unavailable_reason == "worker_no_evidence"
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
            assert missing is not None and missing.error_code == "missing_provider_job_id"
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
                "recovery_note": "provider_status_unavailable_after_output"
            }
            assert attempt.status is JobStatus.COMPLETED
            assert attempt.evidence_status == "unavailable"
            assert attempt.unavailable_reason == "timing_unavailable"
            summary = sum_terminal_attempt_estimates(
                session,
                interval_start=attempt.completed_at - timedelta(seconds=1),
                interval_end=attempt.completed_at + timedelta(seconds=1),
            )
            assert summary.partial_coverage and summary.attempts_without_cost == 1
    finally:
        engine.dispose()


def test_status_expiry_recovery_removes_completed_cover_source(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(session, job_type=JobType.COVER, job_id="cover-stale")
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "stale-cover-runpod", submission_nonce=nonce
            )
            transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
            transition_job(session, job.id, JobStatus.GENERATING)
            source = settings.paths.incoming / job.id / "source.mp3"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"prepared cover source")
            session.commit()
        fake = _FakeRunpod(settings, factory, fail_status=True)
        fake._write_output("cover-stale", 1)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            completed = get_job(session, "cover-stale")
            assert completed is not None and completed.status is JobStatus.COMPLETED
        assert not source.exists()
    finally:
        engine.dispose()


def test_default_payload_freezes_unseeded_schema_v2_submission_identity(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(
                    description="retry stable generation", seed=None, variation_count=2
                ),
                job_id="stable-seed-job",
            )
            first = create_variation_attempt(session, job_id=job.id, variation_index=1)
            second = create_variation_attempt(session, job_id=job.id, variation_index=2)
            first.submission_nonce = "nonce-a"
            second.submission_nonce = "nonce-a"
            session.flush()

            first_payload = ControllerWorker._default_payload(job, first)
            repeated_payload = ControllerWorker._default_payload(job, first)
            second_payload = ControllerWorker._default_payload(job, second)
            first.submission_nonce = "nonce-b"
            changed_nonce_payload = ControllerWorker._default_payload(job, first)

        first_seed = first_payload["generation"]["seed"]
        assert isinstance(first_seed, int) and 0 <= first_seed <= 2_147_483_647
        assert repeated_payload["generation"]["seed"] == first_seed
        assert first_payload["resolved_parameters"]["seed"] == first_seed
        assert second_payload["generation"]["seed"] != first_seed
        assert changed_nonce_payload["generation"]["seed"] != first_seed
    finally:
        engine.dispose()


def test_schema_v2_status_uncertainty_keeps_exact_ref_active(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(description="random seed recovery", seed=None),
                job_id="job-v2-missing-result",
            )
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "v2-missing-result", submission_nonce=nonce
            )
            transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
            transition_job(session, job.id, JobStatus.GENERATING)
            session.commit()
        fake = _FakeRunpod(settings, factory, fail_status=True)
        fake._write_output("job-v2-missing-result", 1)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker._poll_variation("job-v2-missing-result", 1)

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-v2-missing-result")
            attempt = get_variation_attempt(session, "job-v2-missing-result", 1)
            assert job is not None and job.status is JobStatus.GENERATING
            assert job.current_provider_job_id == "v2-missing-result"
            assert attempt is not None and attempt.status is JobStatus.GENERATING
            assert attempt.provider_job_id == "v2-missing-result"
            assert attempt.runpod_result_json is None
            assert attempt.evidence_status == "pending"
        assert fake.submissions == []
    finally:
        engine.dispose()


def test_schema_v2_status_expiry_uses_persisted_completion_metadata(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(description="random seed recovery", seed=None),
                job_id="job-v2-persisted-result",
            )
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "v2-persisted-result", submission_nonce=nonce
            )
            transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
            transition_job(session, job.id, JobStatus.GENERATING)
            session.commit()
        fake = _FakeRunpod(settings, factory, fail_status=True)
        output_bytes = b"generated-1"
        with factory() as session:
            job = get_job(session, "job-v2-persisted-result")
            attempt = get_variation_attempt(session, "job-v2-persisted-result", 1)
            assert job is not None and attempt is not None and attempt.submission_nonce is not None
            resolved = job.normalized_request_json["resolved_parameters"]
            set_variation_runpod_result(
                session,
                attempt.id,
                {
                    "schema_version": 2,
                    "job_id": job.id,
                    "submission_nonce": attempt.submission_nonce,
                    "variation_index": 1,
                    "status": "uploaded",
                    "profile_id": "fast-beta-v1",
                    "input": {"caption": "random seed recovery", "lyrics": ""},
                    "effective": {
                        "caption": "random seed recovery",
                        "lyrics": "[verse] generated by planner",
                    },
                    "resolved_parameters": dict(resolved),
                    "generated_metadata": {
                        "caption": "random seed recovery",
                        "lyrics": "[verse] generated by planner",
                        "bpm": 120,
                    },
                    "output": {
                        "bytes": len(output_bytes),
                        "sha256": hashlib.sha256(output_bytes).hexdigest(),
                        "requested_seed": None,
                        "effective_seed": 123456,
                        "seed": 123456,
                        "duration_seconds": 1.0,
                        "target_duration_seconds": None,
                        "duration_tolerance_seconds": None,
                        "duration_within_tolerance": True,
                    },
                    "worker": {
                        "ace_tag": "v0.1.8",
                        "dit_model": "test-model",
                        "lm_model": "test-lm",
                        "image_digest": "sha256:" + "d" * 64,
                        "gpu": "test-gpu",
                        "model_bundle": {
                            "repo": "evrenesat/audioventura-ace-step-v0.1.8",
                            "revision": "6f196b2c116474c43a96fc8331ebcd2057e18eef",
                            "tag": "av-v0.1.8-bundle-1",
                            "manifest_sha256": "c" * 64,
                        },
                    },
                },
            )
            session.commit()
        # Exercise the result-before-upload arrival order: the signed upload
        # is finalized only after completion metadata is durable.
        fake._write_output("job-v2-persisted-result", 1)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, "job-v2-persisted-result")
            attempt = get_variation_attempt(session, "job-v2-persisted-result", 1)
            assert job is not None and job.status is JobStatus.COMPLETED
            assert attempt is not None and attempt.status is JobStatus.COMPLETED
            assert attempt.evidence_status == "unavailable"
            assert attempt.unavailable_reason == "timing_unavailable"
            assert attempt.model_identity == "test-model"
            assert attempt.runtime_image_identity == "sha256:" + "d" * 64
            assert attempt.runpod_result_json["output"]["effective_seed"] == 123456
            assert (
                attempt.runpod_result_json["recovery_note"]
                == "provider_status_unavailable_after_output"
            )
            output = job.outputs[0]
            assert output.seed_metadata_json == {
                "requested_seed": None,
                "effective_seed": 123456,
                "seed": 123456,
            }
            assert output.generation_metadata_json["profile_id"] == "fast-beta-v1"
            assert output.generation_metadata_json["generated_metadata"]["bpm"] == 120
            assert output.generation_metadata_json["resolved_parameters"] == dict(resolved)
            assert output.generation_metadata_json["worker"]["gpu"] == "test-gpu"
            assert "runpod_queue_delay_ms" not in output.generation_metadata_json
        assert fake.submissions == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("cancel_outcome", "expected_status"),
    [
        (CancelOutcome.TOO_LATE, JobStatus.CLOUD_QUEUED),
        (CancelOutcome.CANCELLED, JobStatus.FAILED),
    ],
)
def test_salad_deadline_requires_confirmed_pending_cancel(
    settings, cancel_outcome: CancelOutcome, expected_status: JobStatus
) -> None:
    settings.inference_job_timeout_seconds = 1
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_job(
                session,
                job_type=JobType.ORIGINAL,
                job_id="salad-deadline",
                inference_provider=ProviderName.SALAD,
            )
            _, attempt, nonce = prepare_variation_submission(
                session, job.id, 1, inference_provider=ProviderName.SALAD
            )
            persist_variation_provider_job_ref(
                session,
                attempt.id,
                ProviderJobRef(ProviderName.SALAD, "11111111-1111-4111-8111-111111111111"),
                submission_nonce=nonce,
            )
            attempt.started_at = utc_now() - timedelta(seconds=2)
            session.commit()
        provider = _GenericSalad(cancel=cancel_outcome)
        registry = ProviderRegistry([provider], default=ProviderName.SALAD)
        worker = ControllerWorker(settings, factory, registry, poll_interval_seconds=0)
        _run(worker._poll_variation("salad-deadline", 1))
        with factory() as session:
            job = get_job(session, "salad-deadline")
            assert job is not None and job.status is expected_status
        assert provider.cancelled == ["11111111-1111-4111-8111-111111111111"]
    finally:
        engine.dispose()


def test_provider_error_backoff_caps_and_valid_status_resets(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            create_job(session, job_type=JobType.ORIGINAL, job_id="backoff")
            session.commit()
        worker = ControllerWorker(
            settings,
            factory,
            _FakeRunpod(settings, factory),
            poll_interval_seconds=0,
        )
        error = ProviderError(ProviderErrorKind.TRANSIENT, "status", "temporary")
        for _ in range(10):
            worker._record_poll_error("backoff", ProviderName.RUNPOD, error)
        assert worker._poll_delays["backoff"] == 60
        worker._clear_poll_error("backoff")
        assert "backoff" not in worker._poll_error_counts
        assert "backoff" not in worker._poll_delays
    finally:
        engine.dispose()

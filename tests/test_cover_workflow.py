from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.home_ingest import HomeIngestClient, HomeIngestError, PreparedCoverSource
from ace_service.models import JobStatus, TransferStatus
from ace_service.repository import (
    confirm_cover_job,
    create_cover_job,
    create_output,
    finalize_cover_job_duration,
    get_job,
    get_variation_attempt,
    persist_variation_runpod_job_id,
    prepare_variation_submission,
    transition_job,
)
from ace_service.runpod_client import RunpodState, RunpodStatusResult
from ace_service.schemas import CoverRequest
from ace_service.worker import ControllerWorker

JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine, create_session_factory(engine)


def _source_metadata(job_id: str, payload: bytes = b"prepared source") -> PreparedCoverSource:
    return PreparedCoverSource(
        job_id=job_id,
        video_id="abc123",
        title="Safe title",
        canonical_url="https://www.youtube.com/watch?v=abc123",
        duration_seconds=42.0,
        prepared_format="mp3",
        prepared_bytes=len(payload),
        prepared_sha256=hashlib.sha256(payload).hexdigest(),
    )


class _FakeHome:
    def __init__(self, settings, *, failure: Exception | None = None, payload=b"prepared source"):
        self.settings = settings
        self.failure = failure
        self.payload = payload
        self.checksum_override: str | None = None
        self.calls: list[dict[str, object]] = []

    async def prepare(
        self,
        *,
        job_id: str,
        url: str,
        max_duration_seconds: int,
        max_source_bytes: int,
    ) -> PreparedCoverSource:
        self.calls.append(
            {
                "job_id": job_id,
                "url": url,
                "max_duration_seconds": max_duration_seconds,
                "max_source_bytes": max_source_bytes,
            }
        )
        if self.failure is not None:
            raise self.failure
        source_path = self.settings.paths.incoming / job_id / "source.mp3.part"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(self.payload)
        result = _source_metadata(job_id, self.payload)
        if self.checksum_override is not None:
            return PreparedCoverSource(
                job_id=result.job_id,
                video_id=result.video_id,
                title=result.title,
                canonical_url=result.canonical_url,
                duration_seconds=result.duration_seconds,
                prepared_format=result.prepared_format,
                prepared_bytes=result.prepared_bytes,
                prepared_sha256=self.checksum_override,
            )
        return result


class _CoverRunpod:
    def __init__(self, settings, factory, transfer_app, *, failed: bool = False) -> None:
        self.settings = settings
        self.factory = factory
        self.transfer_app = transfer_app
        self.failed = failed
        self.payloads: list[dict[str, object]] = []
        self.submissions: list[str] = []
        self.status_calls: list[str] = []

    async def submit(
        self, payload: Mapping[str, object], execution_timeout_ms: int, ttl_ms: int
    ) -> str:
        assert execution_timeout_ms > 0
        assert ttl_ms > 0
        copied = dict(payload)
        self.payloads.append(copied)
        variation_index = int(copied["variation_index"])
        runpod_job_id = f"cover-runpod-{variation_index}"
        self.submissions.append(runpod_job_id)
        source = copied["source"]
        assert isinstance(source, Mapping)
        result_upload = copied["result_upload"]
        assert isinstance(result_upload, Mapping)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.transfer_app),
            base_url="https://transfer.example.invalid",
        ) as client:
            source_response = await client.get(urlsplit(str(source["url"])).path)
            assert source_response.status_code == 200
            assert source_response.content == b"prepared source"
            if not self.failed:
                output = b"generated cover"
                response = await client.put(
                    urlsplit(str(result_upload["url"])).path,
                    content=output,
                    headers={
                        "Content-Length": str(len(output)),
                        "X-ACE-Output-SHA256": hashlib.sha256(output).hexdigest(),
                    },
                )
                assert response.status_code == 200
        return runpod_job_id

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        self.status_calls.append(runpod_job_id)
        if self.failed:
            return RunpodStatusResult(
                job_id=runpod_job_id,
                category=RunpodState.FAILED,
                raw_status="FAILED",
            )
        variation_index = int(runpod_job_id.rsplit("-", 1)[1])
        payload = self.payloads[variation_index - 1]
        generation = payload["generation"]
        assert isinstance(generation, Mapping)
        resolved = payload["resolved_parameters"]
        assert isinstance(resolved, Mapping)
        output_bytes = b"generated cover"
        result = {
            "schema_version": 2,
            "job_id": payload["job_id"],
            "submission_nonce": payload["submission_nonce"],
            "variation_index": variation_index,
            "status": "uploaded",
            "profile_id": payload["profile_id"],
            "input": {"caption": generation["prompt"], "lyrics": generation["lyrics"]},
            "effective": {"caption": generation["prompt"], "lyrics": generation["lyrics"]},
            "resolved_parameters": dict(resolved),
            "output": {
                "bytes": len(output_bytes),
                "sha256": hashlib.sha256(output_bytes).hexdigest(),
                "requested_seed": generation.get("seed"),
                "effective_seed": generation.get("seed") or 20_000 + variation_index,
                "seed": generation.get("seed") or 20_000 + variation_index,
                "duration_seconds": 42.0,
                "target_duration_seconds": 42.0,
                "duration_tolerance_seconds": 2.0,
                "duration_within_tolerance": True,
            },
            "worker": {
                "ace_tag": "v0.1.8",
                "dit_model": "test-model",
                "lm_model": "test-lm",
                "image_digest": "sha256:" + "c" * 64,
                "gpu": "test-gpu",
                "model_bundle": {
                    "repo": "evrenesat/audioventura-ace-step-v0.1.8",
                    "revision": "6f196b2c116474c43a96fc8331ebcd2057e18eef",
                    "tag": "av-v0.1.8-bundle-1",
                    "manifest_sha256": "c" * 64,
                },
            },
        }
        return RunpodStatusResult(
            job_id=runpod_job_id,
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
            result=result,
        )


def _create_cover(settings, factory, *, job_id: str = JOB_ID, **kwargs: object) -> str:
    request = CoverRequest(
        youtube_url="https://www.youtube.com/watch?v=abc123",
        target_style="dreamy synthwave",
        rights_confirmation=True,
        **kwargs,
    )
    with factory() as session:
        job = create_cover_job(session, request, job_id=job_id)
        session.commit()
        return job.id


def test_cover_request_composes_prompt_and_omits_empty_lyrics() -> None:
    request = CoverRequest(
        youtube_url="https://www.youtube.com/watch?v=abc123",
        target_style="  dreamy synthwave  ",
        remix_guidance="  wider drums  ",
        lyrics="   ",
        audio_cover_strength=0.4,
        cover_noise_strength=0.2,
        rights_confirmation=True,
    )

    assert request.effective_prompt == "dreamy synthwave\n\nwider drums"
    assert request.lyrics is None
    normalized = request.to_normalized_request_json()
    assert normalized["schema_version"] == 2
    assert normalized["generation"]["audio_cover_strength"] == 0.4
    assert normalized["generation"]["cover_noise_strength"] == 0.2
    assert normalized["resolved_parameters"]["cover_noise_strength"] == 0.2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"youtube_url": "https://www.youtube.com/playlist?list=abc"},
        {"youtube_url": "http://www.youtube.com/watch?v=abc123"},
        {"rights_confirmation": False},
        {"target_style": "  "},
        {"remix_guidance": "longer remix"},
    ],
)
def test_cover_request_rejects_unsafe_or_unconfirmed_requests(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "youtube_url": "https://www.youtube.com/watch?v=abc123",
        "target_style": "dreamy synthwave",
        "rights_confirmation": True,
    }
    values.update(kwargs)
    with pytest.raises(ValidationError):
        CoverRequest(**values)


def test_cover_flow_stages_source_downloads_it_and_cleans_up_after_success(settings) -> None:
    engine, factory = _database(settings)
    try:
        job_id = _create_cover(
            settings,
            factory,
            audio_cover_strength=0.55,
            cover_noise_strength=0.25,
            variation_count=2,
        )
        home = _FakeHome(settings)
        from ace_service.transfers import create_transfer_app

        transfer_app = create_transfer_app(settings, session_factory=factory)
        runpod = _CoverRunpod(settings, factory, transfer_app)

        async def prepare() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                runpod,
                home_ingest_client=home,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(prepare())

        assert home.calls == [
            {
                "job_id": job_id,
                "url": "https://www.youtube.com/watch?v=abc123",
                "max_duration_seconds": 600,
                "max_source_bytes": settings.transfer_max_source_bytes,
            }
        ]
        # One submit per variation; preparation and confirmation are part of
        # the same worker pass, so the cover never waits for a second click.
        assert len(runpod.payloads) == 2
        payload = runpod.payloads[0]
        assert payload["task_type"] == "cover"
        assert payload["source"]["format"] == "mp3"
        assert payload["source"]["bytes"] == len(b"prepared source")
        assert payload["generation"]["audio_cover_strength"] == 0.55
        assert payload["generation"]["cover_noise_strength"] == 0.25
        assert payload["generation"]["duration"] == 42.0
        assert payload["resolved_parameters"]["duration"] == 42.0
        assert len(runpod.status_calls) == 2
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.COMPLETED
            assert job.rights_confirmation_at is not None
            assert job.normalized_request_json["generation"]["audio_cover_strength"] == 0.55
            assert job.normalized_request_json["generation"]["cover_noise_strength"] == 0.25
            assert job.normalized_request_json["generation"]["duration"] == 42.0
            # The new flow commits exactly the confirmed staging state:
            # never `awaiting_confirmation`, always `confirmed` with both
            # timestamps and the canonical source metadata in one transaction.
            staging = job.normalized_request_json["cover_staging"]
            assert staging["status"] == "confirmed"
            assert isinstance(staging.get("confirmed_at"), str)
            assert isinstance(staging.get("staged_at"), str)
            assert job.sanitized_source_title == "Safe title"
            assert job.source_url == "https://www.youtube.com/watch?v=abc123"
            assert job.source_byte_size == len(b"prepared source")
            assert job.source_sha256 == hashlib.sha256(b"prepared source").hexdigest()
            assert job.source_duration == 42.0
            assert job.normalized_request_json["source_duration_seconds"] == 42.0
            assert job.normalized_request_json["resolved_target_duration_seconds"] == 42.0
            assert [transfer.status for transfer in job.transfers].count(
                TransferStatus.CONSUMED
            ) == 2
            assert [transfer.status for transfer in job.transfers].count(
                TransferStatus.REVOKED
            ) == 2
        assert not (settings.paths.incoming / job_id).exists()
        output = settings.paths.outputs / job_id / "variation-01.mp3"
        assert output.read_bytes() == b"generated cover"
        assert (settings.paths.outputs / job_id / "variation-02.mp3").is_file()
    finally:
        engine.dispose()


def test_cover_submit_never_precedes_confirmed_staging_commit(settings) -> None:
    """Prove the Runpod boundary is crossed only after `confirmed` is durable."""

    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        home = _FakeHome(settings)
        from ace_service.transfers import create_transfer_app

        transfer_app = create_transfer_app(settings, session_factory=factory)

        class _StagingAwareRunpod(_CoverRunpod):
            async def submit(
                self, payload: Mapping[str, object], execution_timeout_ms: int, ttl_ms: int
            ) -> str:
                # A fresh session observes the exact durable state at the
                # moment the fake Runpod would accept the request.
                with factory() as session:
                    job = get_job(session, str(payload["job_id"]))
                    assert job is not None
                    staging = dict((job.normalized_request_json or {}).get("cover_staging") or {})
                    assert staging.get("status") == "confirmed", (
                        "Runpod submit must never precede the confirmed-staging commit"
                    )
                    assert staging.get("confirmed_at"), "confirmed_at must be durable"
                    assert staging.get("status") != "awaiting_confirmation"
                    assert job.source_sha256 is not None
                    assert job.source_byte_size is not None
                    assert job.source_duration == 42.0
                    assert job.status is not JobStatus.STAGING
                return await super().submit(payload, execution_timeout_ms, ttl_ms)

        runpod = _StagingAwareRunpod(settings, factory, transfer_app)

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                runpod,
                home_ingest_client=home,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        assert len(runpod.payloads) == 1
        assert len(runpod.submissions) == 1
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.COMPLETED
            assert job.normalized_request_json["cover_staging"]["status"] == "confirmed"
    finally:
        engine.dispose()


def test_cover_crash_before_confirmed_staging_commit_fails_closed(settings) -> None:
    """A crash before the metadata/staging/confirmation transaction commits
    must never submit and must fail closed without repeating home extraction."""

    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            session.commit()
        # The prepared file exists (finalize_cover_source ran) but the
        # transaction with canonical metadata never committed.
        source = settings.paths.incoming / job_id / "source.mp3"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"prepared source")

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("crash-before-commit cover must not reach Runpod")

            async def status(self, runpod_job_id):
                raise AssertionError("crash-before-commit cover must not poll Runpod")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.FAILED
            assert job.error_code == "ingest_interrupted"
            # No source metadata was inferred from the file after rollback.
            assert job.source_sha256 is None
            assert job.source_byte_size is None
            assert job.source_url == "https://www.youtube.com/watch?v=abc123"
        # Terminal cleanup removed the prepared source.
        assert not (settings.paths.incoming / job_id).exists()
    finally:
        engine.dispose()


def test_cover_confirmed_staging_resumes_submission_after_restart(settings) -> None:
    """After the confirmed-staging commit but before submit, startup resumes
    the staged cover through the ordinary serialized path."""

    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        source = b"prepared source"
        source_path = settings.paths.incoming / job_id / "source.mp3"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            confirm_cover_job(session, job.id)
            job.source_byte_size = len(source)
            job.source_sha256 = hashlib.sha256(source).hexdigest()
            session.commit()

        from ace_service.transfers import create_transfer_app

        transfer_app = create_transfer_app(settings, session_factory=factory)
        runpod = _CoverRunpod(settings, factory, transfer_app)

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                runpod,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.COMPLETED
            assert job.normalized_request_json["cover_staging"]["status"] == "confirmed"
        assert runpod.submissions == ["cover-runpod-1"]
        assert len(runpod.payloads) == 1
    finally:
        engine.dispose()


def test_legacy_awaiting_confirmation_cover_is_left_untouched_on_startup(settings) -> None:
    """Legacy durable `awaiting_confirmation` rows must never auto-confirm
    and must never be submitted by startup."""

    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        source = b"prepared source"
        source_path = settings.paths.incoming / job_id / "source.mp3"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            job.source_byte_size = len(source)
            job.source_sha256 = hashlib.sha256(source).hexdigest()
            session.commit()

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("awaiting cover must not auto-submit")

            async def status(self, runpod_job_id):
                raise AssertionError("awaiting cover must not poll")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.STAGING
            assert job.normalized_request_json["cover_staging"]["status"] == (
                "awaiting_confirmation"
            )
        # The legacy row and its prepared source stay in place for the
        # authenticated one-time confirmation/cancel route.
        assert source_path.exists()
    finally:
        engine.dispose()


def test_cover_missing_rights_confirmation_fails_closed_before_submit(settings) -> None:
    """The persisted initial rights confirmation is mandatory; without it the
    cover fails closed before any Runpod submission."""

    engine, factory = _database(settings)
    try:
        request = CoverRequest(
            youtube_url="https://www.youtube.com/watch?v=abc123",
            target_style="dreamy synthwave",
            rights_confirmation=True,
        )
        with factory() as session:
            job = create_cover_job(session, request, job_id="job-no-rights")
            job.rights_confirmation_at = None
            session.commit()
            job_id = job.id
        home = _FakeHome(settings)

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("rights-missing cover must not reach Runpod")

            async def status(self, runpod_job_id):
                raise AssertionError("rights-missing cover must not poll Runpod")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                home_ingest_client=home,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.FAILED
            assert job.error_code == "rights_confirmation_missing"
            staging = (job.normalized_request_json or {}).get("cover_staging") or {}
            assert staging.get("status") != "confirmed"
    finally:
        engine.dispose()


def test_cover_uncertain_submission_is_never_resubmitted(settings) -> None:
    """After the nonce commit, an uncertain submission is failed closed and
    the same nonce is never submitted twice."""

    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        source = b"prepared source"
        source_path = settings.paths.incoming / job_id / "source.mp3"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            confirm_cover_job(session, job.id)
            job.source_byte_size = len(source)
            job.source_sha256 = hashlib.sha256(source).hexdigest()
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            assert nonce
            assert attempt.submission_nonce == nonce
            session.commit()

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("uncertain cover must not be resubmitted")

            async def status(self, runpod_job_id):
                raise AssertionError("uncertain cover must not poll")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.FAILED
            assert job.error_code == "uncertain_cloud_submission"
            attempt = get_variation_attempt(session, job_id, 1)
            assert attempt is not None and attempt.status is JobStatus.FAILED
            assert attempt.submission_nonce is not None
            assert attempt.runpod_job_id is None
    finally:
        engine.dispose()


def test_cover_home_unavailable_fails_without_runpod_submission(settings) -> None:
    engine, factory = _database(settings)
    try:
        job_id = _create_cover(
            settings,
            factory,
        )
        home = _FakeHome(
            settings,
            failure=HomeIngestError("home_ingest_unavailable", "home is offline"),
        )

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("cover must not reach Runpod")

            async def status(self, runpod_job_id):
                raise AssertionError("cover must not poll Runpod")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                home_ingest_client=home,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.error_code == "home_ingest_unavailable"
    finally:
        engine.dispose()


def test_cover_source_checksum_mismatch_fails_before_runpod(settings) -> None:
    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        payload = b"prepared source"
        home = _FakeHome(settings, payload=payload)
        home.checksum_override = "0" * 64

        class NoRunpod:
            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                raise AssertionError("invalid source must not reach Runpod")

            async def status(self, runpod_job_id):
                raise AssertionError("invalid source must not poll Runpod")

        async def scenario() -> None:
            worker = ControllerWorker(
                settings,
                factory,
                NoRunpod(),
                home_ingest_client=home,
                poll_interval_seconds=0,
            )
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.error_code == "source_integrity_mismatch"
        assert not (settings.paths.incoming / job_id).exists()
    finally:
        engine.dispose()


def test_cover_restart_polls_persisted_runpod_id_without_resubmission(settings) -> None:
    engine, factory = _database(settings)
    try:
        job_id = _create_cover(settings, factory)
        source = b"prepared source"
        source_path = settings.paths.incoming / job_id / "source.mp3"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(source)
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            job.variation_count = 1
            job.source_byte_size = len(source)
            job.source_sha256 = hashlib.sha256(source).hexdigest()
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            confirm_cover_job(session, job.id)
            _, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            submission_nonce = nonce
            resolved_parameters = dict(job.normalized_request_json["resolved_parameters"])
            persist_variation_runpod_job_id(
                session, attempt.id, "persisted-cover-runpod", submission_nonce=nonce
            )
            output = b"generated cover"
            output_path = settings.paths.outputs / job_id / "variation-01.mp3"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(output)
            create_output(
                session,
                job_id=job_id,
                variation_index=1,
                result_index=0,
                runpod_job_id="persisted-cover-runpod",
                relative_path=f"{job_id}/variation-01.mp3",
                mime_type="audio/mpeg",
                byte_size=len(output),
                sha256=hashlib.sha256(output).hexdigest(),
            )
            session.commit()

        class PollOnlyRunpod:
            submissions = 0

            async def submit(self, payload, execution_timeout_ms, ttl_ms):
                self.submissions += 1
                raise AssertionError("restart must not resubmit the cover")

            async def status(self, runpod_job_id):
                assert runpod_job_id == "persisted-cover-runpod"
                return RunpodStatusResult(
                    job_id=runpod_job_id,
                    category=RunpodState.COMPLETED,
                    raw_status="COMPLETED",
                    result={
                        "schema_version": 2,
                        "job_id": job_id,
                        "submission_nonce": submission_nonce,
                        "variation_index": 1,
                        "status": "uploaded",
                        "profile_id": "fast-beta-v1",
                        "input": {"caption": "dreamy synthwave", "lyrics": ""},
                        "effective": {"caption": "dreamy synthwave", "lyrics": ""},
                        "resolved_parameters": resolved_parameters,
                        "output": {
                            "bytes": len(output),
                            "sha256": hashlib.sha256(output).hexdigest(),
                            "requested_seed": None,
                            "effective_seed": 101,
                            "seed": 101,
                            "duration_seconds": 42.0,
                            "target_duration_seconds": 42.0,
                            "duration_tolerance_seconds": 2.0,
                            "duration_within_tolerance": True,
                        },
                        "worker": {
                            "ace_tag": "v0.1.8",
                            "dit_model": "test-model",
                            "lm_model": "test-lm",
                            "image_digest": "sha256:" + "c" * 64,
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

        runpod = PollOnlyRunpod()

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, runpod, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.COMPLETED
            first = get_variation_attempt(session, job_id, 1)
            assert first is not None and first.status is JobStatus.COMPLETED
            assert get_variation_attempt(session, job_id, 1).runpod_job_id == (
                "persisted-cover-runpod"
            )
        assert runpod.submissions == 0
        assert not source_path.exists()
    finally:
        engine.dispose()


def test_home_ingest_client_maps_metadata_and_safe_errors(settings) -> None:
    response_body = {
        "job_id": str(UUID(JOB_ID)),
        "video_id": "abc123",
        "title": "Safe title",
        "canonical_url": "https://www.youtube.com/watch?v=abc123",
        "duration_seconds": 42.0,
        "prepared_format": "mp3",
        "prepared_bytes": 15,
        "prepared_sha256": "a" * 64,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-home-token"
        assert request.url.path == "/v1/prepare-youtube-cover"
        return httpx.Response(200, json=response_body)

    async def scenario() -> PreparedCoverSource:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://home.test"
        )
        home = HomeIngestClient(settings, http_client=client)
        result = await home.prepare(
            job_id=JOB_ID,
            url="https://www.youtube.com/watch?v=abc123",
            max_duration_seconds=600,
            max_source_bytes=100,
        )
        await client.aclose()
        return result

    result = _run(scenario())
    assert result.video_id == "abc123"

    async def unavailable() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, json={"detail": "offline"})
            ),
            base_url="https://home.test",
        )
        home = HomeIngestClient(settings, http_client=client)
        with pytest.raises(HomeIngestError, match="rejected") as error:
            await home.prepare(
                job_id=JOB_ID,
                url="https://www.youtube.com/watch?v=abc123",
                max_duration_seconds=600,
                max_source_bytes=100,
            )
        assert error.value.code == "home_ingest_unavailable"
        await client.aclose()

    _run(unavailable())

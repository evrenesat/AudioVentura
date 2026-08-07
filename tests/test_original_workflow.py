from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus, TransferDirection
from ace_service.repository import (
    create_original_job,
    create_output,
    get_job,
    get_variation_attempt,
)
from ace_service.runpod_client import RunpodState, RunpodStatusResult
from ace_service.schemas import OriginalSongRequest
from ace_service.worker import ControllerWorker


def _run(awaitable):
    return asyncio.run(awaitable)


def _database(settings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine, create_session_factory(engine)


def test_original_request_normalizes_description_and_omits_unsupplied_options() -> None:
    request = OriginalSongRequest(
        description="  warm analog synth  ",
        lyrics="[verse] preserve this exactly\n",
        vocal_language=" en ",
        variation_count=2,
    )

    assert request.description == "warm analog synth"
    assert request.lyrics == "[verse] preserve this exactly\n"
    assert request.vocal_language == "en"
    assert request.output_format.value == "mp3"

    payload = request.to_normalized_request_json()
    assert payload == {
        "schema_version": 1,
        "generation": {
            "prompt": "warm analog synth",
            "lyrics": "[verse] preserve this exactly\n",
            "instrumental": False,
            "vocal_language": "en",
            "output_format": "mp3",
        },
        "source": None,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"description": "  "},
        {"description": "ok", "instrumental": True, "lyrics": "[verse] vocals"},
        {"description": "valid prompt", "time_signature": 5},
        {"description": "valid prompt", "seed": 2_147_483_647, "variation_count": 2},
        {"description": "valid prompt", "key_scale": "  "},
    ],
)
def test_original_request_rejects_invalid_combinations(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OriginalSongRequest(**kwargs)


def test_original_job_persists_normalized_request_before_queueing(settings) -> None:
    engine, factory = _database(settings)
    try:
        request = OriginalSongRequest(
            description="bright piano house",
            duration=30,
            bpm=124,
            key_scale="C major",
            time_signature=4,
            seed=17,
            variation_count=2,
        )
        with factory() as session:
            job = create_original_job(session, request)
            session.commit()
            job_id = job.id

        with factory() as session:
            persisted = get_job(session, job_id)
            assert persisted is not None
            assert persisted.status is JobStatus.QUEUED
            assert persisted.prompt == "bright piano house"
            assert persisted.variation_count == 2
            assert persisted.normalized_request_json == {
                "schema_version": 1,
                "generation": {
                    "prompt": "bright piano house",
                    "instrumental": False,
                    "vocal_language": "en",
                    "duration": 30.0,
                    "bpm": 124,
                    "key_scale": "C major",
                    "time_signature": 4,
                    "seed": 17,
                    "output_format": "mp3",
                },
                "source": None,
            }
    finally:
        engine.dispose()


class _RecordingRunpod:
    def __init__(self, settings, factory, *, fail_variation: int | None = None) -> None:
        self.settings = settings
        self.factory = factory
        self.fail_variation = fail_variation
        self.payloads: list[dict[str, object]] = []
        self.active = 0
        self.max_active = 0

    async def submit(
        self, payload: Mapping[str, object], execution_timeout_ms: int, ttl_ms: int
    ) -> str:
        assert execution_timeout_ms > 0
        assert ttl_ms > 0
        copied = dict(payload)
        self.payloads.append(copied)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            variation_index = int(copied["variation_index"])
            job_id = str(copied["job_id"])
            runpod_job_id = f"runpod-{variation_index}"
            if variation_index != self.fail_variation:
                self._write_output(job_id, variation_index, runpod_job_id)
            return runpod_job_id
        finally:
            self.active -= 1

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        variation_index = int(runpod_job_id.rsplit("-", 1)[1])
        if variation_index == self.fail_variation:
            return RunpodStatusResult(
                job_id=runpod_job_id,
                category=RunpodState.FAILED,
                raw_status="FAILED",
            )
        return RunpodStatusResult(
            job_id=runpod_job_id,
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
            result={"worker": {"model": "test-model", "gpu": "test-gpu"}},
            delay_ms=7,
            execution_ms=11,
        )

    def _write_output(self, job_id: str, variation_index: int, runpod_job_id: str) -> None:
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
                runpod_job_id=runpod_job_id,
                relative_path=relative_path,
                mime_type="audio/mpeg",
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            session.commit()


def test_original_variations_use_fresh_capabilities_and_progress_seed(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(
                    description="serialized variations", seed=41, variation_count=3
                ),
            )
            session.commit()
            job_id = job.id
        fake = _RecordingRunpod(settings, factory)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())

        assert [payload["variation_index"] for payload in fake.payloads] == [1, 2, 3]
        assert fake.max_active == 1
        assert [
            payload["generation"]["seed"]
            for payload in fake.payloads
            if isinstance(payload["generation"], Mapping)
        ] == [41, 42, 43]
        assert all("variation_count" not in payload for payload in fake.payloads)
        assert all(isinstance(payload.get("result_upload"), Mapping) for payload in fake.payloads)
        upload_urls = [
            payload["result_upload"]["url"]
            for payload in fake.payloads
            if isinstance(payload["result_upload"], Mapping)
        ]
        assert len(set(upload_urls)) == 3

        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.status is JobStatus.COMPLETED
            attempts = [get_variation_attempt(session, job_id, index) for index in (1, 2, 3)]
            assert all(attempt is not None for attempt in attempts)
            assert attempts[0] is not None
            assert attempts[0].runpod_result_json == {
                "worker": {"model": "test-model", "gpu": "test-gpu"},
                "runpod_queue_delay_ms": 7,
                "runpod_execution_ms": 11,
            }
            capabilities = list(job.transfers)
            assert len(capabilities) == 3
            assert {capability.direction for capability in capabilities} == {
                TransferDirection.OUTPUT_UPLOAD
            }
            assert {capability.expected_relative_path for capability in capabilities} == {
                f"{job_id}/variation-01.mp3",
                f"{job_id}/variation-02.mp3",
                f"{job_id}/variation-03.mp3",
            }
    finally:
        engine.dispose()


def test_later_original_failure_preserves_earlier_output(settings) -> None:
    engine, factory = _database(settings)
    try:
        with factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(description="partial success", variation_count=3),
            )
            session.commit()
            job_id = job.id
        fake = _RecordingRunpod(settings, factory, fail_variation=2)

        async def scenario() -> None:
            worker = ControllerWorker(settings, factory, fake, poll_interval_seconds=0)
            await worker.start()
            await worker.wait_idle()
            await worker.stop()

        _run(scenario())

        assert [payload["variation_index"] for payload in fake.payloads] == [1, 2]
        with factory() as session:
            job = get_job(session, job_id)
            first = get_variation_attempt(session, job_id, 1)
            second = get_variation_attempt(session, job_id, 2)
            third = get_variation_attempt(session, job_id, 3)
            assert job is not None and job.status is JobStatus.FAILED
            assert first is not None and first.status is JobStatus.COMPLETED
            assert second is not None and second.status is JobStatus.FAILED
            assert third is None
            assert (settings.paths.outputs / job_id / "variation-01.mp3").is_file()
    finally:
        engine.dispose()

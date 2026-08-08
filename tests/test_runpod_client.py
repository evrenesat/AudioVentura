from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from ace_service.models import JobStatus, JobType
from ace_service.repository import create_job, get_job, prepare_runpod_submission
from ace_service.runpod_client import (
    RunpodAPIError,
    RunpodClient,
    RunpodResponseError,
    RunpodState,
    RunpodSubmissionCoordinator,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def test_runpod_client_maps_all_statuses_and_sends_policy() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v2/endpoint/run":
            return httpx.Response(200, json={"id": "job-123", "status": "IN_QUEUE"})
        if request.url.path == "/v2/endpoint/status/job-123":
            return httpx.Response(
                200,
                json={"id": "job-123", "status": "RUNNING", "delayTime": 12},
            )
        raise AssertionError(request.url)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://runpod.test/v2/endpoint/", transport=transport
        ) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            job_id = await client.submit({"submission_nonce": "nonce"}, 1200, 7200)
            assert job_id == "job-123"
            result = await client.status(job_id)
            assert result.category is RunpodState.GENERATING
            assert result.status == "generating"

    _run(scenario())
    assert seen[0].headers["authorization"] == "Bearer secret-key"
    assert b'"executionTimeout":1200' in seen[0].content
    assert b'"ttl":7200' in seen[0].content


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("IN_QUEUE", RunpodState.CLOUD_QUEUED),
        ("IN_PROGRESS", RunpodState.GENERATING),
        ("RUNNING", RunpodState.GENERATING),
        ("COMPLETED", RunpodState.COMPLETED),
        ("FAILED", RunpodState.FAILED),
        ("TIMED_OUT", RunpodState.FAILED),
        ("CANCELLED", RunpodState.FAILED),
    ],
)
def test_runpod_status_normalization(raw_status: str, expected: RunpodState) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "job-123", "status": raw_status})
        )
        async with httpx.AsyncClient(base_url="https://runpod.test", transport=transport) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            result = await client.status("job-123")
            assert result.category is expected

    _run(scenario())


def test_runpod_api_and_malformed_errors_do_not_echo_secret() -> None:
    async def scenario() -> None:
        def api_failure(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(500, text="Bearer secret-key leaked")

        async with httpx.AsyncClient(
            base_url="https://runpod.test", transport=httpx.MockTransport(api_failure)
        ) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            with pytest.raises(RunpodAPIError) as error:
                await client.health()
            assert "secret-key" not in str(error.value)

        malformed = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "job-123", "status": "UNKNOWN"})
        )
        async with httpx.AsyncClient(base_url="https://runpod.test", transport=malformed) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            with pytest.raises(RunpodResponseError):
                await client.status("job-123")

    _run(scenario())


def test_cancel_requires_cancelled_response() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "job-123", "status": "CANCELLED"})
        )
        async with httpx.AsyncClient(base_url="https://runpod.test", transport=transport) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            result = await client.cancel("job-123")
            assert result.category is RunpodState.FAILED

    _run(scenario())


class _FakeRunpod:
    def __init__(self, factory) -> None:
        self.factory = factory
        self.submissions = 0
        self.status_calls: list[str] = []

    async def submit(self, payload: Mapping[str, object], **kwargs: int) -> str:
        del kwargs
        self.submissions += 1
        with self.factory() as session:
            job = get_job(session, "job-123")
            assert job is not None
            assert job.current_submission_nonce == payload["submission_nonce"]
            assert job.current_runpod_job_id is None
        return "runpod-123"

    async def status(self, runpod_job_id: str):
        self.status_calls.append(runpod_job_id)
        return "polled"


def test_submission_nonce_is_committed_before_submit_and_id_immediately_after(session) -> None:
    job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-123")
    session.commit()
    factory = session.bind
    assert factory is not None
    from ace_service.db import create_session_factory

    session_factory = create_session_factory(factory)
    fake = _FakeRunpod(session_factory)
    coordinator = RunpodSubmissionCoordinator(fake, session_factory)  # type: ignore[arg-type]

    runpod_job_id = _run(
        coordinator.submit(
            job.id,
            {"prompt": "test"},
            execution_timeout_ms=1200,
            ttl_ms=7200,
        )
    )
    assert runpod_job_id == "runpod-123"
    with session_factory() as check:
        loaded = get_job(check, job.id)
        assert loaded is not None
        assert loaded.current_submission_nonce
        assert loaded.current_runpod_job_id == "runpod-123"
        assert loaded.status is JobStatus.CLOUD_QUEUED


def test_restart_polls_persisted_id_without_resubmission(session) -> None:
    job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-123")
    prepare_runpod_submission(session, job.id)
    job.current_runpod_job_id = "runpod-123"
    session.commit()
    from ace_service.db import create_session_factory

    session_factory = create_session_factory(session.bind)
    fake = _FakeRunpod(session_factory)
    coordinator = RunpodSubmissionCoordinator(fake, session_factory)  # type: ignore[arg-type]
    assert _run(coordinator.poll_persisted_job(job.id)) == "polled"
    assert fake.submissions == 0
    assert fake.status_calls == ["runpod-123"]


def test_uncertain_recovery_fails_without_automatic_resubmission(session) -> None:
    job = create_job(session, job_type=JobType.ORIGINAL, job_id="job-123")
    prepare_runpod_submission(session, job.id)
    session.commit()
    from ace_service.db import create_session_factory

    session_factory = create_session_factory(session.bind)
    fake = _FakeRunpod(session_factory)
    coordinator = RunpodSubmissionCoordinator(fake, session_factory)  # type: ignore[arg-type]
    assert coordinator.recover_uncertain() == [job.id]
    with session_factory() as check:
        recovered = get_job(check, job.id)
        assert recovered is not None
        assert recovered.status is JobStatus.FAILED
        assert recovered.error_code == "uncertain_cloud_submission"
        assert recovered.current_runpod_job_id is None
    assert fake.submissions == 0


def _health_details(
    *,
    idle: int = 0,
    running: int = 0,
    queued: int = 0,
    in_progress: int = 0,
) -> dict[str, object]:
    # Representative documented Runpod /health body.  Only the workers block
    # and the inQueue/inProgress job counts are required; the remaining job
    # fields are tolerated provider contract.
    return {
        "workers": {"idle": idle, "running": running},
        "jobs": {
            "completed": 0,
            "failed": 0,
            "inProgress": in_progress,
            "inQueue": queued,
            "retried": 0,
        },
    }


def _parse(**counts: int):
    from ace_service.runpod_client import EndpointWorkerCounts, RunpodHealth, parse_worker_counts

    counts_obj = parse_worker_counts(RunpodHealth(details=_health_details(**counts)))
    assert isinstance(counts_obj, EndpointWorkerCounts)
    return counts_obj


def test_parse_health_zero_workers_is_zero_at_rest() -> None:
    counts = _parse()
    assert counts.active == 0
    assert counts.has_pending_work is False


def test_parse_health_active_and_pending_work() -> None:
    idle = _parse(idle=1)
    assert idle.active == 1
    running = _parse(running=1)
    assert running.active == 1
    queued = _parse(queued=2)
    assert queued.active == 0 and queued.has_pending_work is True
    in_progress = _parse(in_progress=1)
    assert in_progress.has_pending_work is True
    both = _parse(idle=2, running=3, queued=1, in_progress=4)
    assert both.active == 5
    assert both.queued == 1 and both.in_progress == 4


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"workers": {"idle": 0}},
        {"jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": "zero", "jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": {"idle": 0, "running": 0}, "jobs": []},
        {"workers": {"idle": None, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": {"idle": True, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": {"idle": -1, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": {"idle": 0, "running": 10_001}, "jobs": {"inQueue": 0, "inProgress": 0}},
        {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": "0"}},
        {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": -2}},
        {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 99_999_999, "inProgress": 0}},
        # The obsolete invented snake/lowercase shape is never provider evidence.
        {"workers": {"idle": 0, "running": 0}, "jobs": {"queued": 0, "running": 0}},
        {
            "workers": {"idle": 0, "running": 0},
            "jobs": {"inQueue": 0, "inProgress": 0, "queued": 0},
        },
        {
            "workers": {"idle": 0, "running": 0},
            "jobs": {"inQueue": 0, "inProgress": 0, "running": 0},
        },
    ],
)
def test_parse_health_rejects_malformed_structures(details: dict[str, object]) -> None:
    from ace_service.runpod_client import RunpodHealth, RunpodResponseError, parse_worker_counts

    with pytest.raises(RunpodResponseError):
        parse_worker_counts(RunpodHealth(details=details))


def test_health_endpoint_returns_parsable_zero_at_rest() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "workers": {"idle": 0, "running": 0},
                    "jobs": {
                        "completed": 0,
                        "failed": 0,
                        "inProgress": 0,
                        "inQueue": 0,
                        "retried": 0,
                    },
                },
            )
        )
        async with httpx.AsyncClient(base_url="https://runpod.test", transport=transport) as http:
            client = RunpodClient(
                "secret-key", "endpoint", base_url="https://runpod.test", http_client=http
            )
            from ace_service.runpod_client import parse_worker_counts

            counts = parse_worker_counts(await client.health())
            assert counts.active == 0 and counts.has_pending_work is False

    _run(scenario())

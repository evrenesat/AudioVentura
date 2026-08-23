from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from ace_service.providers.base import (
    CancelOutcome,
    DetailScope,
    InferenceMode,
    InferenceRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    RequestFeature,
)
from ace_service.providers.salad import SaladProvider

JOB_ID = str(uuid4())
JOB_CREATED = "2026-08-23T10:00:00+00:00"
META = {
    "application_job_id": "app-job",
    "variation_index": 1,
    "submission_nonce": "nonce",
    "worker_schema_version": 2,
}


def _request() -> InferenceRequest:
    return InferenceRequest(
        "app-job",
        1,
        "nonce",
        InferenceMode.PROMPT_TO_AUDIO,
        frozenset({RequestFeature.PROMPT}),
        {"schema_version": 2, "job_id": "app-job"},
        10_000,
        20_000,
    )


def _provider(handler) -> SaladProvider:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.salad.com/api/public/organizations/org/projects/project/",
    )
    return SaladProvider("secret-key", "org", "project", "queue", "group", http_client=client)


def test_submit_is_single_metadata_only_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = __import__("json").loads(request.content)
        assert body == {"input": {"schema_version": 2, "job_id": "app-job"}, "metadata": META}
        assert "secret-key" not in request.content.decode()
        return httpx.Response(201, json={"id": JOB_ID, "status": "pending", "metadata": META})

    async def scenario() -> None:
        provider = _provider(handler)
        assert await provider.submit(_request()) == ProviderJobRef(ProviderName.SALAD, JOB_ID)
        await provider._client.aclose()

    asyncio.run(scenario())
    assert calls == 1


@pytest.mark.parametrize(
    ("state", "phase"),
    [
        ("pending", ProviderPhase.QUEUED),
        ("running", ProviderPhase.RUNNING),
        ("succeeded", ProviderPhase.SUCCEEDED),
        ("failed", ProviderPhase.FAILED),
        ("cancelled", ProviderPhase.CANCELLED),
        ("future", ProviderPhase.UNKNOWN),
    ],
)
def test_status_maps_finite_lifecycle(state: str, phase: ProviderPhase) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instances"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"id": JOB_ID, "status": state})

    async def scenario() -> None:
        provider = _provider(handler)
        assert (await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))).phase is phase
        await provider._client.aclose()

    asyncio.run(scenario())


def test_pending_status_uses_unambiguous_deployment_progress() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/instances"):
            return httpx.Response(
                200, json={"items": [{"state": "downloading", "pulling_progress": 63}]}
            )
        return httpx.Response(
            200, json={"id": JOB_ID, "status": "pending", "create_time": JOB_CREATED}
        )

    async def scenario() -> None:
        provider = _provider(handler)
        status = await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert (status.phase, status.progress, status.detail_scope) == (
            ProviderPhase.PROVISIONING,
            0.63,
            DetailScope.DEPLOYMENT,
        )
        await provider._client.aclose()

    asyncio.run(scenario())
    assert paths == [
        f"/api/public/organizations/org/projects/project/queues/queue/jobs/{JOB_ID}",
        "/api/public/organizations/org/projects/project/containers/group/instances",
    ]


@pytest.mark.parametrize(
    ("event_name", "phase", "message", "instance_status_code"),
    [
        ("Instance Allocated", ProviderPhase.PROVISIONING, "Allocated GPU", 500),
        (
            "Instance Downloading",
            ProviderPhase.PROVISIONING,
            "Downloading worker image",
            200,
        ),
        ("Instance Starting", ProviderPhase.STARTING, "Starting worker", 200),
        ("Instance Running", ProviderPhase.STARTING, "Initializing ACE-Step", 200),
    ],
)
def test_pending_status_falls_back_to_current_system_log_lifecycle(
    event_name: str, phase: ProviderPhase, message: str, instance_status_code: int
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith(f"/jobs/{JOB_ID}"):
            return httpx.Response(
                200, json={"id": JOB_ID, "status": "pending", "create_time": JOB_CREATED}
            )
        if request.url.path.endswith("/instances"):
            return httpx.Response(instance_status_code, json={"items": []})
        if request.url.path.endswith("/system-logs"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "event_name": "Container Group Started",
                            "event_time": "2026-08-23T10:02:00Z",
                        },
                        {
                            "event_name": event_name,
                            "event_time": "2026-08-23T10:01:00Z",
                            "instance_id": "private-instance-id",
                            "machine_id": "private-machine-id",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    async def scenario() -> None:
        provider = _provider(handler)
        status = await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert (status.phase, status.message, status.progress, status.detail_scope) == (
            phase,
            message,
            None,
            DetailScope.DEPLOYMENT,
        )
        assert "private-instance-id" not in repr(status)
        assert "private-machine-id" not in repr(status)
        await provider._client.aclose()

    asyncio.run(scenario())
    assert paths == [
        f"/api/public/organizations/org/projects/project/queues/queue/jobs/{JOB_ID}",
        "/api/public/organizations/org/projects/project/containers/group/instances",
        "/api/public/organizations/org/projects/project/containers/group/system-logs",
    ]


def test_pending_status_ignores_pre_job_system_log_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/jobs/{JOB_ID}"):
            return httpx.Response(
                200, json={"id": JOB_ID, "status": "pending", "create_time": JOB_CREATED}
            )
        if request.url.path.endswith("/instances"):
            return httpx.Response(200, json={"instances": []})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "event_name": "Instance Downloading",
                        "event_time": "2026-08-23T09:59:59Z",
                    }
                ]
            },
        )

    async def scenario() -> None:
        provider = _provider(handler)
        status = await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert (status.phase, status.message, status.detail_scope) == (
            ProviderPhase.QUEUED,
            None,
            DetailScope.JOB,
        )
        await provider._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "logs"),
    [
        (500, {"items": []}),
        (200, {"items": "invalid"}),
        (200, {"items": ["invalid"]}),
        (
            200,
            {
                "items": [
                    {
                        "event_name": "Instance " + "x" * 255,
                        "event_time": "2026-08-23T10:01:00Z",
                    }
                ]
            },
        ),
        (
            200,
            {
                "items": [
                    {
                        "event_name": "Instance Downloading",
                        "event_time": "not-a-timestamp",
                    }
                ]
            },
        ),
        (
            200,
            {
                "items": [
                    {
                        "event_name": "Instance Downloading",
                        "event_time": "2026-08-23T10:01:00Z",
                    },
                    {
                        "event_name": "Instance Interrupted (Priority)",
                        "event_time": "2026-08-23T10:02:00Z",
                    },
                ]
            },
        ),
        (
            200,
            {
                "items": [
                    {
                        "event_name": "Instance Creating",
                        "event_time": "2026-08-23T10:01:00Z",
                    }
                ]
            },
        ),
    ],
)
def test_pending_status_keeps_queued_on_uncertain_system_logs(
    status_code: int, logs: object
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/jobs/{JOB_ID}"):
            return httpx.Response(
                200, json={"id": JOB_ID, "status": "pending", "create_time": JOB_CREATED}
            )
        if request.url.path.endswith("/instances"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(status_code, json=logs)

    async def scenario() -> None:
        provider = _provider(handler)
        status = await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert (status.phase, status.message, status.detail_scope) == (
            ProviderPhase.QUEUED,
            None,
            DetailScope.JOB,
        )
        await provider._client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("pulling_progress", (0.0, 0.19, 1.0))
def test_pending_status_accepts_live_fractional_pull_progress(pulling_progress: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instances"):
            return httpx.Response(
                200,
                json={
                    "instances": [{"state": "downloading", "pulling_progress": pulling_progress}]
                },
            )
        return httpx.Response(200, json={"id": JOB_ID, "status": "pending"})

    async def scenario() -> None:
        provider = _provider(handler)
        status = await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert (status.phase, status.progress, status.detail_scope) == (
            ProviderPhase.PROVISIONING,
            pulling_progress,
            DetailScope.DEPLOYMENT,
        )
        await provider._client.aclose()

    asyncio.run(scenario())


def test_health_uses_container_path_and_current_queue_length() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/queues/queue"):
            return httpx.Response(200, json={"current_queue_length": 7})
        if request.url.path.endswith("/containers/group"):
            return httpx.Response(200, json={"replicas": 1})
        return httpx.Response(404)

    async def scenario() -> None:
        provider = _provider(handler)
        health = await provider.health()
        assert (health.queued_jobs, health.running_instances) == (7, 1)
        await provider._client.aclose()

    asyncio.run(scenario())
    assert paths == [
        "/api/public/organizations/org/projects/project/queues/queue",
        "/api/public/organizations/org/projects/project/containers/group",
    ]


@pytest.mark.parametrize("name", ("a", "1a", "a-", "a" * 64))
def test_provider_rejects_names_outside_salad_contract(name: str) -> None:
    with pytest.raises(ValueError, match="queue name is invalid"):
        SaladProvider("secret-key", "org", "project", name, "group")


@pytest.mark.parametrize("name", ("ab", "a-1", "a" * 63))
def test_provider_accepts_names_within_salad_contract(name: str) -> None:
    provider = SaladProvider("secret-key", "org", "project", name, "group")

    asyncio.run(provider.aclose())


@pytest.mark.parametrize(
    ("state", "expected", "delete_calls"),
    [
        ("pending", CancelOutcome.CANCELLED, 1),
        ("running", CancelOutcome.TOO_LATE, 0),
        ("cancelled", CancelOutcome.CANCELLED, 0),
    ],
)
def test_cancel_semantics(state: str, expected: CancelOutcome, delete_calls: int) -> None:
    deletes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deletes
        if request.method == "DELETE":
            deletes += 1
            return httpx.Response(202)
        return httpx.Response(200, json={"id": JOB_ID, "status": state})

    async def scenario() -> None:
        provider = _provider(handler)
        assert await provider.cancel(ProviderJobRef(ProviderName.SALAD, JOB_ID)) is expected
        await provider._client.aclose()

    asyncio.run(scenario())
    assert deletes == delete_calls


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (404, ProviderErrorKind.NOT_FOUND),
        (429, ProviderErrorKind.TRANSIENT),
        (500, ProviderErrorKind.TRANSIENT),
        (400, ProviderErrorKind.REJECTED),
    ],
)
def test_safe_http_classification(status: int, kind: ProviderErrorKind) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="secret-key prompt lyrics")

    async def scenario() -> None:
        provider = _provider(handler)
        with pytest.raises(ProviderError) as captured:
            await provider.status(ProviderJobRef(ProviderName.SALAD, JOB_ID))
        assert captured.value.kind is kind
        assert "secret-key" not in str(captured.value)
        await provider._client.aclose()

    asyncio.run(scenario())

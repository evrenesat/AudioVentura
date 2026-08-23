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
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/instances"):
            return httpx.Response(
                200, json={"items": [{"state": "downloading", "pulling_progress": 63}]}
            )
        return httpx.Response(200, json={"id": JOB_ID, "status": "pending"})

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

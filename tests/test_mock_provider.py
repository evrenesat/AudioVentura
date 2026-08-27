from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ace_service.providers.base import (
    CancelOutcome,
    InferenceMode,
    InferenceRequest,
    ProviderName,
    RequestFeature,
)
from ace_service.providers.mock import BACKEND_ID, MockProvider


def test_mock_provider_maps_private_api_and_declares_all_features() -> None:
    requests: list[httpx.Request] = []
    external_id = "11111111-1111-4111-8111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/jobs":
            return httpx.Response(
                202,
                json={
                    "job_id": external_id,
                    "application_job_id": "22222222-2222-4222-8222-222222222222",
                    "variation_index": 1,
                    "submission_nonce": "33333333-3333-4333-8333-333333333333",
                    "status": "queued",
                },
            )
        if request.method == "GET" and request.url.path == f"/v1/jobs/{external_id}":
            return httpx.Response(200, json={"job_id": external_id, "status": "succeeded"})
        if request.method == "GET" and request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "queue_depth": 0, "running": False})
        if request.method == "POST" and request.url.path == f"/v1/jobs/{external_id}/cancel":
            return httpx.Response(200, json={"job_id": external_id, "status": "cancelled"})
        if request.method == "GET" and request.url.path == f"/v1/jobs/{external_id}/result":
            return httpx.Response(
                200,
                json={
                    "job_id": external_id,
                    "status": "succeeded",
                    "metadata": {"schema_version": 2},
                },
            )
        return httpx.Response(404)

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://mock.ts.net"
        )
        provider = MockProvider("https://mock.ts.net", "secret", http_client=client)
        request = InferenceRequest(
            "22222222-2222-4222-8222-222222222222",
            1,
            "33333333-3333-4333-8333-333333333333",
            InferenceMode.AUDIO_TO_AUDIO,
            frozenset(RequestFeature),
            {
                "schema_version": 2,
                "job_id": "22222222-2222-4222-8222-222222222222",
                "result_upload": {"url": "https://transfer.test/result", "max_bytes": 1000},
                "source": {"url": "https://transfer.test/source", "bytes": 10},
                "generation": {"prompt": "ignored", "lyrics": "ignored"},
            },
            1000,
            2000,
        )
        assert provider.capabilities.name is ProviderName.MOCK
        assert provider.capabilities.backend_id == BACKEND_ID
        assert provider.capabilities.request_features == frozenset(RequestFeature)
        assert provider.capabilities.native_formats == frozenset({"mp3"})
        assert provider.capabilities.enforces_requested_duration is False
        ref = await provider.submit(request)
        assert ref.backend_id == BACKEND_ID
        assert (await provider.status(ref)).phase.value == "succeeded"
        assert dict((await provider.result(ref)).metadata) == {"schema_version": 2}
        assert await provider.cancel(ref) is CancelOutcome.CANCELLED
        assert (await provider.health()).ok
        body = json.loads(requests[0].content)
        assert body["source"]["bytes"] == 10
        assert requests[0].headers["authorization"] == "Bearer secret"
        await provider.aclose()

    asyncio.run(scenario())


def test_mock_provider_rejects_public_or_pathful_endpoints() -> None:
    with pytest.raises(ValueError, match="private p100"):
        MockProvider("https://8.8.8.8:8200", "secret")
    with pytest.raises(ValueError, match="private HTTP"):
        MockProvider("https://mock.ts.net/api", "secret")

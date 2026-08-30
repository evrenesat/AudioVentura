from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobType
from ace_service.providers.base import (
    InferenceMode,
    InferenceRequest,
    ProviderError,
    ProviderName,
    RequestFeature,
)
from ace_service.providers.node import BACKEND_ID, NodeProvider
from ace_service.repository import create_job, get_job

APP = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"
JOB = "33333333-3333-4333-8333-333333333333"


def test_node_provider_translates_metadata_only_http_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/jobs":
            return httpx.Response(
                202,
                json={
                    "job_id": JOB,
                    "application_job_id": APP,
                    "variation_index": 1,
                    "submission_nonce": NONCE,
                    "status": "queued",
                },
            )
        if request.method == "GET" and request.url.path == f"/v1/jobs/{JOB}":
            return httpx.Response(200, json={"job_id": JOB, "status": "succeeded"})
        if request.method == "GET" and request.url.path == f"/v1/jobs/{JOB}/result":
            return httpx.Response(
                200, json={"job_id": JOB, "status": "succeeded", "metadata": {"schema_version": 2}}
            )
        if request.method == "POST" and request.url.path == f"/v1/jobs/{JOB}/cancel":
            return httpx.Response(
                200, json={"job_id": JOB, "status": "running", "outcome": "too_late"}
            )
        if request.method == "GET" and request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ready", "queue_depth": 0, "running": False})
        return httpx.Response(404)

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8210"
        )
        provider = NodeProvider("http://127.0.0.1:8210", "secret", http_client=client)
        request = InferenceRequest(
            APP,
            1,
            NONCE,
            InferenceMode.PROMPT_TO_AUDIO,
            frozenset(RequestFeature),
            {
                "schema_version": 2,
                "job_id": APP,
                "submission_nonce": NONCE,
                "variation_index": 1,
                "task_type": "original",
                "result_upload": {"url": "https://player.evren.io/transfer/v1/out", "max_bytes": 1},
            },
            1000,
            1000,
        )
        ref = await provider.submit(request)
        assert ref.provider is ProviderName.NODE and ref.backend_id == BACKEND_ID
        assert (await provider.status(ref)).phase.value == "succeeded"
        assert dict((await provider.result(ref)).metadata) == {"schema_version": 2}
        from ace_service.providers.base import CancelOutcome

        assert await provider.cancel(ref) is CancelOutcome.TOO_LATE
        assert (await provider.health()).ok
        body = json.loads(requests[0].content)
        assert "audio" not in json.dumps(body).lower()
        assert requests[0].headers["authorization"] == "Bearer secret"
        await provider.aclose()

    asyncio.run(scenario())


def test_controller_constructs_node_only_when_explicitly_enabled(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_root=tmp_path / "controller",
        service_password="service-secret",
        home_ingest_token="home-secret",
        runpod_api_key="change-me",
        runpod_endpoint_id="change-me",
        inference_provider="node",
        inference_enabled_backends=str(BACKEND_ID),
        default_original_backend=str(BACKEND_ID),
        default_cover_backend=str(BACKEND_ID),
        ace_node_base_url="http://127.0.0.1:8210",
        ace_node_token="node-secret",
    )
    engine = create_database_engine(settings)
    initialize_database(engine)
    app = create_app(settings, session_factory=create_session_factory(engine), worker=object())
    try:
        provider = app.state.provider_registry.get_persisted(BACKEND_ID)
        assert provider.capabilities.name is ProviderName.NODE
        assert app.state.provider_registry.default is ProviderName.NODE
    finally:

        async def close() -> None:
            for provider in app.state.provider_registry.providers:
                client = getattr(provider, "client", provider)
                aclose = getattr(client, "aclose", None)
                if aclose is not None:
                    await aclose()

        asyncio.run(close())
        engine.dispose()


def test_controller_database_round_trips_node_backend_without_migration(tmp_path: Path) -> None:
    settings = ServiceSettings(
        data_root=tmp_path / "controller",
        service_password="service-secret",
        home_ingest_token="home-secret",
        inference_provider="node",
        inference_enabled_backends=str(BACKEND_ID),
        default_original_backend=str(BACKEND_ID),
        default_cover_backend=str(BACKEND_ID),
        ace_node_base_url="http://127.0.0.1:8210",
        ace_node_token="node-secret",
    )
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        job = create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id=APP,
            inference_provider=ProviderName.NODE,
            inference_backend=BACKEND_ID,
            normalized_request_json={"schema_version": 2, "task_type": "original"},
            backend_snapshot_json={
                "backend_id": str(BACKEND_ID),
                "provider": ProviderName.NODE.value,
                "label": "ACE Node · ACE-Step 1.5 XL Turbo",
            },
        )
        session.commit()
        assert job.inference_backend == str(BACKEND_ID)
    engine.dispose()

    reopened = create_database_engine(settings)
    with create_session_factory(reopened)() as session:
        persisted = get_job(session, APP)
        assert persisted is not None
        assert persisted.inference_provider == ProviderName.NODE.value
        assert persisted.inference_backend == str(BACKEND_ID)
    reopened.dispose()


def test_node_provider_rejects_oversized_submission_without_http_call() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async def scenario() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8210"
        )
        provider = NodeProvider("http://127.0.0.1:8210", "secret", http_client=client)
        request = InferenceRequest(
            APP,
            1,
            NONCE,
            InferenceMode.PROMPT_TO_AUDIO,
            frozenset(RequestFeature),
            {
                "schema_version": 2,
                "job_id": APP,
                "submission_nonce": NONCE,
                "variation_index": 1,
                "task_type": "original",
                "result_upload": {"url": "https://player.evren.io/transfer/v1/out", "max_bytes": 1},
                "padding": "x" * 70_000,
            },
            1000,
            1000,
        )
        with pytest.raises(ProviderError, match="too large"):
            await provider.submit(request)
        assert requests == 0
        await provider.aclose()

    asyncio.run(scenario())

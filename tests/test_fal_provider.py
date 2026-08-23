from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ace_service.providers.base import (
    BackendOperation,
    GenerationRequest,
    InferenceMode,
    InferenceRequest,
    ProviderHealth,
    ProviderName,
    ProviderPhase,
)
from ace_service.providers.fal import FalProvider, FalQueueTransport, build_fal_payload
from ace_service.providers.fal_catalog import load_catalog
from ace_service.providers.registry import BackendRegistry
from ace_service.schemas import CoverRequest
from ace_service.web import _backend_choices, _readiness, _refresh_fal_health, _select_backend


def _request(
    *, generation: GenerationRequest, source: dict[str, str] | None = None
) -> InferenceRequest:
    return InferenceRequest(
        "job-fal",
        1,
        "nonce-fal",
        generation.mode,
        frozenset(),
        {},
        1_000,
        2_000,
        generation_request=generation,
        signed_source=source,
    )


def test_fal_queue_submit_status_and_result_are_endpoint_scoped() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")
    calls: list[httpx.Request] = []
    status_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Key test-fal-key"
        assert request.headers["x-fal-store-io"] == "0"
        lifecycle = json.loads(request.headers["x-fal-object-lifecycle-preference"])
        assert lifecycle["initial_acl"]["default"] == "forbid"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"sync_mode": False, "prompt": "cassette", "duration": 30}
            return httpx.Response(202, json={"request_id": "fal-request-1"})
        if request.url.path.endswith("/status"):
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                return httpx.Response(200, json={"status": "IN_PROGRESS", "progress": 0.5})
            return httpx.Response(200, json={"status": "COMPLETED"})
        return httpx.Response(200, json={"audio": {"url": "https://fal.media/audio.mp3"}})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = FalQueueTransport("test-fal-key", http_client=client)
        provider = FalProvider(descriptor, transport)
        generation = GenerationRequest(
            mode=InferenceMode.PROMPT_TO_AUDIO,
            prompt="cassette",
            duration_seconds=30,
        )
        ref = await provider.submit(_request(generation=generation))
        assert ref.provider is ProviderName.FAL
        assert ref.backend_id == descriptor.backend_id
        assert (await provider.status(ref)).phase is ProviderPhase.RUNNING
        result = await provider.result(ref)
        assert result.artifact is not None
        assert result.artifact.url == "https://fal.media/audio.mp3"
        assert len(calls) == 4
        await client.aclose()

    asyncio.run(scenario())


def test_fal_payload_maps_ace_audio_fields_and_never_accepts_bytes() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/fal-ai/ace-step/audio-to-audio")
    generation = GenerationRequest(
        mode=InferenceMode.AUDIO_TO_AUDIO,
        prompt="bright synthwave",
        lyrics="new lyrics",
        fields={"source_style": "original track", "source_lyrics": "old lyrics"},
    )
    payload = build_fal_payload(
        descriptor,
        _request(generation=generation, source={"audio_url": "https://controller.test/source.mp3"}),
    )
    assert payload["audio_url"] == "https://controller.test/source.mp3"
    assert payload["tags"] == "bright synthwave"
    assert payload["original_tags"] == "original track"
    assert payload["original_lyrics"] == "old lyrics"
    assert "data:" not in json.dumps(payload)

    with pytest.raises(ValueError, match="required"):
        build_fal_payload(
            descriptor,
            _request(
                generation=GenerationRequest(mode=InferenceMode.AUDIO_TO_AUDIO, prompt="style"),
                source={"audio_url": "https://controller.test/source.mp3"},
            ),
        )


def test_cover_contract_persists_ace_source_style_and_source_lyrics() -> None:
    request = CoverRequest(
        youtube_url="https://youtu.be/source123",
        target_style="bright synthwave",
        source_style="original acoustic ballad",
        source_lyrics="old words",
        rights_confirmation=True,
    )
    generation = request.to_normalized_request_json()["generation"]
    assert generation["source_style"] == "original acoustic ballad"
    assert generation["source_lyrics"] == "old words"


def test_edit_backend_invariants_are_server_side() -> None:
    catalog = load_catalog()
    app = SimpleNamespace(state=SimpleNamespace())
    client = httpx.AsyncClient()
    try:
        inpaint = FalProvider(
            catalog.by_backend_id("fal/fal-ai/ace-step/audio-inpaint"),
            FalQueueTransport("test-key", http_client=client),
        )
        app.state.provider_registry = BackendRegistry([inpaint])
        with pytest.raises(ValueError, match="inpaint region"):
            _select_backend(
                app,
                {
                    "backend": "fal/fal-ai/ace-step/audio-inpaint",
                    "target_style": "repair the chorus",
                    "start_seconds": "50",
                    "end_seconds": "10",
                    "output_format": "wav",
                },
                BackendOperation.AUDIO_INPAINT,
            )

        outpaint = FalProvider(
            catalog.by_backend_id("fal/fal-ai/ace-step/audio-outpaint"),
            FalQueueTransport("test-key", http_client=client),
        )
        app.state.provider_registry = BackendRegistry([outpaint])
        with pytest.raises(ValueError, match="outpaint"):
            _select_backend(
                app,
                {
                    "backend": "fal/fal-ai/ace-step/audio-outpaint",
                    "target_style": "extend the ending",
                    "before_seconds": "0",
                    "after_seconds": "0",
                    "output_format": "wav",
                },
                BackendOperation.AUDIO_OUTPAINT,
            )
    finally:
        asyncio.run(client.aclose())


def test_inactive_fal_endpoint_is_cached_out_of_choices_and_readiness() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"models": []})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = FalProvider(
            descriptor,
            FalQueueTransport("test-key", http_client=client),
        )
        app = SimpleNamespace(state=SimpleNamespace())
        app.state.provider_registry = BackendRegistry([provider])
        app.state.fal_health_cache = {}
        app.state.settings = SimpleNamespace(transfer_public_base_url="https://controller.test")
        app.state.runpod_client = None
        app.state.home_ingest_client = SimpleNamespace()

        await _refresh_fal_health(app)
        assert _backend_choices(app, BackendOperation.TEXT_TO_MUSIC) == []
        readiness = await _readiness(app, only={"inference_provider"})
        assert readiness["components"]["inference_provider"] == {
            "ok": False,
            "message": "Fal endpoint is not active",
        }
        await client.aclose()

    asyncio.run(scenario())


def test_readiness_preserves_negative_provider_health() -> None:
    class InactiveProvider:
        capabilities = SimpleNamespace(
            name=ProviderName.SALAD,
            backend_id="salad/test",
            operation=BackendOperation.TEXT_TO_MUSIC,
        )

        async def health(self) -> ProviderHealth:
            return ProviderHealth(False, "provider is warming up")

    async def scenario() -> None:
        app = SimpleNamespace(state=SimpleNamespace())
        app.state.provider_registry = BackendRegistry([InactiveProvider()])
        app.state.fal_health_cache = {}
        app.state.settings = SimpleNamespace(transfer_public_base_url="https://controller.test")
        app.state.runpod_client = None
        app.state.home_ingest_client = SimpleNamespace()
        readiness = await _readiness(app, only={"inference_provider"})
        assert readiness["components"]["inference_provider"] == {
            "ok": False,
            "message": "provider is warming up",
        }

    asyncio.run(scenario())

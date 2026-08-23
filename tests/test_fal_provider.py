from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ace_service.providers.base import (
    GenerationRequest,
    InferenceMode,
    InferenceRequest,
    ProviderName,
    ProviderPhase,
)
from ace_service.providers.fal import FalProvider, FalQueueTransport, build_fal_payload
from ace_service.providers.fal_catalog import load_catalog


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

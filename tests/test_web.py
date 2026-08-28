from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import ace_service.web as web_routes
from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.costs import FalPricingClient
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.migrations import migration_upgrade
from ace_service.models import Job, JobStatus, JobType, OutputFormat, SubmissionQuote
from ace_service.providers.base import (
    BackendOperation,
    InferenceMode,
    ProviderCapabilities,
    ProviderHealth,
    ProviderName,
    RequestFeature,
)
from ace_service.providers.fal import FalProvider, FalQueueTransport
from ace_service.providers.fal_catalog import load_catalog
from ace_service.providers.mock import MockProvider
from ace_service.providers.registry import ProviderRegistry
from ace_service.repository import (
    EvidenceConflictError,
    confirm_cover_job,
    create_cover_job,
    create_job,
    create_original_job,
    create_output,
    create_variation_attempt,
    finalize_cover_job_duration,
    get_job,
    get_submission_quote,
    set_variation_progress,
    transition_job,
    transition_variation_attempt,
    upsert_gpu_rate,
    upsert_runtime_calibration,
)
from ace_service.schemas import CoverRequest, OriginalSongRequest
from ace_service.transfers import create_transfer_app
from ace_service.web import capture_submission_quote
from ace_service.worker import ControllerWorker
from runpod_worker.schemas import WorkerRequest

RUNTIME_A = "sha256:" + "a" * 64
RUNTIME_B = "sha256:" + "b" * 64

ORIGINAL_BACKEND_INVENTORY = (
    (
        "fal.ai",
        (
            ("fal/fal-ai/ace-step", "fal.ai · ACE-Step", False),
            ("fal/fal-ai/lyria3", "fal.ai · Lyria 3", False),
            ("fal/fal-ai/lyria3/pro", "fal.ai · Lyria 3 Pro", False),
            ("fal/minimax/music-3", "fal.ai · MiniMax Music 3", False),
        ),
    ),
    ("mock", (("mock/midi-sequential", "Mock · Sequential MIDI → MP3", False),)),
    ("runpod", (("runpod/ace-step-v15-xl-turbo", "Runpod · ACE-Step 1.5 XL Turbo", True),)),
    ("salad", (("salad/ace-step-v15-xl-turbo", "Salad · ACE-Step 1.5 XL Turbo", False),)),
)
COVER_BACKEND_INVENTORY = (
    (
        "fal.ai",
        (
            (
                "fal/fal-ai/stable-audio-3/medium/audio-to-audio",
                "fal.ai · Stable Audio 3 Medium Audio to Audio",
                False,
            ),
            (
                "fal/fal-ai/stable-audio-3/medium/base/audio-to-audio",
                "fal.ai · Stable Audio 3 Medium Base Audio to Audio",
                False,
            ),
            (
                "fal/fal-ai/stable-audio-3/small/music/audio-to-audio",
                "fal.ai · Stable Audio 3 Small Music Audio to Audio",
                False,
            ),
            (
                "fal/fal-ai/stable-audio-3/small/music/base/audio-to-audio",
                "fal.ai · Stable Audio 3 Small Music Base Audio to Audio",
                False,
            ),
        ),
    ),
    ("mock", (("mock/midi-sequential", "Mock · Sequential MIDI → MP3", False),)),
    ("runpod", (("runpod/ace-step-v15-xl-turbo", "Runpod · ACE-Step 1.5 XL Turbo", False),)),
    ("salad", (("salad/ace-step-v15-xl-turbo", "Salad · ACE-Step 1.5 XL Turbo", True),)),
)


class _InventoryProvider:
    def __init__(self, name: ProviderName, backend_id: str) -> None:
        self.capabilities = ProviderCapabilities(
            name=name,
            modes=frozenset(InferenceMode),
            request_features=frozenset(RequestFeature),
            accepts_worker_schema=frozenset({2}),
            supports_pending_cancel=True,
            supports_running_cancel=False,
            not_found_after_deadline_is_terminal=True,
            backend_id=backend_id,
        )


class _InventoryFalProvider(FalProvider):
    def __init__(self, descriptor) -> None:
        super().__init__(descriptor, object())

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "ready")


def _selector_inventory(html: str) -> tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...]:
    class Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.in_backend_select = False
            self.group: str | None = None
            self.option: tuple[str, bool, list[str]] | None = None
            self.groups: list[tuple[str, tuple[tuple[str, str, bool], ...]]] = []
            self.options: list[tuple[str, str, bool]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "select" and values.get("name") == "backend":
                self.in_backend_select = True
            elif self.in_backend_select and tag == "optgroup":
                self.group = values.get("label")
                self.options = []
            elif self.in_backend_select and tag == "option":
                value = values.get("value")
                assert self.group is not None and value is not None
                self.option = (value, "selected" in values, [])

        def handle_data(self, data: str) -> None:
            if self.option is not None:
                self.option[2].append(data)

        def handle_endtag(self, tag: str) -> None:
            if not self.in_backend_select:
                return
            if tag == "option":
                assert self.option is not None
                value, selected, text = self.option
                self.options.append((value, "".join(text).strip(), selected))
                self.option = None
            elif tag == "optgroup":
                assert self.group is not None
                self.groups.append((self.group, tuple(self.options)))
                self.group = None
                self.options = []
            elif tag == "select":
                self.in_backend_select = False

    parser = Parser()
    parser.feed(html)
    parser.close()
    return tuple(parser.groups)


def _assert_selector_inventory(
    actual: tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...],
    expected: tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...],
) -> None:
    actual_ids = [value for _, options in actual for value, _, _ in options]
    assert len(actual_ids) == len(set(actual_ids))
    assert actual == expected


def _inventory_registry() -> ProviderRegistry:
    catalog = load_catalog()
    real_ids = [
        "runpod/ace-step-v15-xl-turbo",
        "salad/ace-step-v15-xl-turbo",
        "fal/fal-ai/lyria3",
        "fal/fal-ai/lyria3/pro",
        "fal/fal-ai/ace-step",
        "fal/minimax/music-3",
        "fal/fal-ai/stable-audio-3/medium/audio-to-audio",
        "fal/fal-ai/stable-audio-3/medium/base/audio-to-audio",
        "fal/fal-ai/stable-audio-3/small/music/audio-to-audio",
        "fal/fal-ai/stable-audio-3/small/music/base/audio-to-audio",
    ]
    providers: list[Any] = [
        _InventoryProvider(ProviderName.RUNPOD, real_ids[0]),
        _InventoryProvider(ProviderName.SALAD, real_ids[1]),
        _InventoryProvider(ProviderName.MOCK, "mock/midi-sequential"),
    ]
    providers.extend(
        _InventoryFalProvider(catalog.by_backend_id(backend_id)) for backend_id in real_ids[2:]
    )
    return ProviderRegistry(
        providers,
        defaults={
            BackendOperation.TEXT_TO_MUSIC: "runpod/ace-step-v15-xl-turbo",
            BackendOperation.AUDIO_TRANSFORM: "salad/ace-step-v15-xl-turbo",
        },
        selectable_backends=[*real_ids, "mock/midi-sequential"],
    )


def test_rendered_backend_selector_inventories_are_exact(web_app) -> None:
    app, _, _ = web_app
    app.state.provider_registry = _inventory_registry()
    with TestClient(app) as client:
        original_response = client.get("/create", auth=_auth(client))
        cover_response = client.get("/cover", auth=_auth(client))
    assert original_response.status_code == 200
    assert cover_response.status_code == 200
    _assert_selector_inventory(
        _selector_inventory(original_response.text), ORIGINAL_BACKEND_INVENTORY
    )
    _assert_selector_inventory(_selector_inventory(cover_response.text), COVER_BACKEND_INVENTORY)


def test_rendered_backend_selector_negative_cases_cannot_pass(web_app) -> None:
    app, _, _ = web_app
    app.state.provider_registry = _inventory_registry()
    with TestClient(app) as client:
        original_response = client.get("/create", auth=_auth(client))
        cover_response = client.get("/cover", auth=_auth(client))
    original = _selector_inventory(original_response.text)
    cover = _selector_inventory(cover_response.text)
    missing_runpod = tuple(group for group in original if group[0] != "runpod")
    with pytest.raises(AssertionError):
        _assert_selector_inventory(missing_runpod, ORIGINAL_BACKEND_INVENTORY)
    with pytest.raises(AssertionError):
        _assert_selector_inventory(original, COVER_BACKEND_INVENTORY)
    with pytest.raises(AssertionError):
        _assert_selector_inventory(cover, ORIGINAL_BACKEND_INVENTORY)


def test_builtin_backend_choices_expose_only_relevant_form_fields(web_app) -> None:
    app, _, _ = web_app
    original = web_routes._backend_choices(app, BackendOperation.TEXT_TO_MUSIC)
    cover = web_routes._backend_choices(app, BackendOperation.AUDIO_TRANSFORM)

    assert set(original[0]["fields"]) == {
        "lyrics",
        "instrumental",
        "prompt_mode",
        "vocal_language",
        "duration",
        "bpm",
        "key_scale",
        "time_signature",
        "seed",
    }
    assert set(cover[0]["fields"]) == {
        "lyrics",
        "audio_cover_strength",
        "cover_noise_strength",
        "duration",
        "seed",
    }


def test_mock_backend_accepts_all_built_in_form_fields_and_is_mp3_only(settings) -> None:
    values = settings.model_dump()
    values.update(
        inference_provider="mock",
        inference_enabled_backends="mock/midi-sequential",
        default_original_backend="mock/midi-sequential",
        default_cover_backend="mock/midi-sequential",
        mock_base_url="http://127.0.0.1:8201",
        mock_token="test-mock-token",
    )
    mock_settings = ServiceSettings(**values)
    engine = create_database_engine(mock_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"})),
        base_url="http://mock.ts.net",
    )
    provider = MockProvider("http://mock.ts.net", "test-mock-token", http_client=client)
    app = create_app(
        mock_settings,
        session_factory=factory,
        provider_registry=ProviderRegistry([provider], default=ProviderName.MOCK),
        home_ingest_client=FakeHome(),
        worker=FakeWorker(),
    )
    try:
        original = web_routes._backend_choices(app, BackendOperation.TEXT_TO_MUSIC)
        cover = web_routes._backend_choices(app, BackendOperation.AUDIO_TRANSFORM)
        assert [choice["label"] for choice in original] == ["Mock · Sequential MIDI → MP3"]
        assert [choice["label"] for choice in cover] == ["Mock · Sequential MIDI → MP3"]
        assert all(choice["native_formats"] == ["mp3"] for choice in (*original, *cover))
        expected_fields = {
            "prompt",
            "lyrics",
            "instrumental",
            "prompt_mode",
            "vocal_language",
            "duration",
            "bpm",
            "key_scale",
            "time_signature",
            "seed",
            "source_style",
            "source_lyrics",
            "audio_cover_strength",
            "cover_noise_strength",
            "strength",
            "start_seconds",
            "end_seconds",
            "before_seconds",
            "after_seconds",
        }
        assert set(original[0]["fields"]) == expected_fields
        assert set(cover[0]["fields"]) == expected_fields

        original_fields = {
            "backend": "mock/midi-sequential",
            "description": "all original controls",
            "lyrics": "lyrics",
            "instrumental": "false",
            "vocal_language": "en",
            "prompt_mode": "enhance",
            "duration_mode": "custom",
            "duration_seconds": "30",
            "bpm": "120",
            "key_scale": "D minor",
            "time_signature": "4",
            "seed": "7",
            "variation_count": "2",
            "output_format": "mp3",
        }
        selected, _snapshot = web_routes._select_backend(
            app, original_fields, BackendOperation.TEXT_TO_MUSIC
        )
        assert selected is provider

        cover_fields = {
            "backend": "mock/midi-sequential",
            "target_style": "bright synthwave",
            "remix_guidance": "keep the chorus wide",
            "lyrics": "new lyrics",
            "source_style": "acoustic",
            "source_lyrics": "old lyrics",
            "audio_cover_strength": "0.5",
            "cover_noise_strength": "0.2",
            "strength": "0.5",
            "start_seconds": "0",
            "end_seconds": "10",
            "before_seconds": "1",
            "after_seconds": "1",
            "duration_mode": "custom",
            "duration_seconds": "30",
            "seed": "8",
            "variation_count": "2",
            "output_format": "mp3",
        }
        selected, _snapshot = web_routes._select_backend(
            app, cover_fields, BackendOperation.AUDIO_TRANSFORM
        )
        assert selected is provider
    finally:
        asyncio.run(client.aclose())
        engine.dispose()


class FakeRunpod:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def health(self) -> None:
        if not self.available:
            raise RuntimeError("Runpod unavailable")

    async def submit(self, payload: Any, execution_timeout_ms: int, ttl_ms: int) -> str:
        del payload, execution_timeout_ms, ttl_ms
        return "runpod-job"

    async def status(self, runpod_job_id: str) -> Any:
        del runpod_job_id
        raise RuntimeError("not used by web tests")

    async def aclose(self) -> None:
        return None


class FakeHome:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def health(self) -> None:
        if not self.available:
            raise RuntimeError("home unavailable")

    async def aclose(self) -> None:
        return None


class StalledHealth:
    def __init__(self) -> None:
        self.cancelled = 0

    async def health(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def aclose(self) -> None:
        return None


class FakeWorker:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return True


@pytest.fixture
def web_app(settings: ServiceSettings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    worker = FakeWorker()
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    yield app, factory, worker
    engine.dispose()


def _auth(client: TestClient) -> tuple[str, str]:
    del client
    return ("change-me", "test-password")


def _csrf(client: TestClient, path: str = "/create") -> str:
    response = client.get(path, auth=_auth(client))
    assert response.status_code == 200
    token = client.cookies.get("ace_csrf")
    assert token
    return token


def _input_tag(html: str, name: str) -> str:
    match = re.search(rf'<input\b[^>]*\bname="{re.escape(name)}"[^>]*>', html)
    assert match is not None
    return match.group(0)


def _textarea_value(html: str, name: str) -> str:
    match = re.search(
        rf'<textarea\b[^>]*\bname="{re.escape(name)}"[^>]*>(.*?)</textarea>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def _selected_value(html: str, name: str) -> str:
    select_match = re.search(
        rf'<select\b[^>]*\bname="{re.escape(name)}"[^>]*>(.*?)</select>',
        html,
        re.DOTALL,
    )
    assert select_match is not None
    option_match = re.search(r'<option value="([^"]+)" selected[ >]', select_match.group(1))
    assert option_match is not None
    return option_match.group(1)


def _rich_original_request() -> OriginalSongRequest:
    return OriginalSongRequest(
        description="midnight strings over broken drums",
        lyrics="[verse]\nHold the line",
        vocal_language="fr",
        prompt_mode="enhance",
        duration_mode="custom",
        duration_seconds=42,
        bpm=111,
        key_scale="D minor",
        time_signature=3,
        seed=77,
        variation_count=3,
        output_format="flac",
    )


def test_auth_matrix_csrf_and_security_headers(web_app) -> None:
    app, _, _ = web_app
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", auth=("change-me", "wrong")).status_code == 401
        response = client.get("/", auth=_auth(client))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

        token = _csrf(client)
        original_form = client.get("/create", auth=_auth(client))
        assert 'value="en"' in _input_tag(original_form.text, "vocal_language")
        missing = client.post("/create", auth=_auth(client), data={"description": "A valid song"})
        assert missing.status_code == 403
        invalid = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": "wrong", "description": "A valid song"},
        )
        assert invalid.status_code == 403
        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": "A valid song",
                "vocal_language": "en",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"].startswith("/jobs/")


def test_generation_forms_explain_controls_and_use_audioventura_brand(web_app) -> None:
    app, _, _ = web_app
    with TestClient(app) as client:
        dashboard = client.get("/", auth=_auth(client))
        assert ">AudioVentura</a>" in dashboard.text
        assert "Make something worth replaying." not in dashboard.text

        original = client.get("/create", auth=_auth(client))
        assert "Choose the provider and model that will generate this song" in original.text
        assert "Target tempo in beats per minute" in original.text
        assert "Each variation is a separate paid inference" in original.text
        assert "FLAC and WAV preserve lossless audio" in original.text

        cover = client.get("/cover", auth=_auth(client))
        assert "Create a cover" in cover.text
        assert "Bring a source, change the weather." not in cover.text
        assert "The home server privately prepares" not in cover.text
        assert 'name="rights_confirmation"' not in cover.text
        assert "Start time of the source region to replace" in cover.text
        assert "numeric duration wording above must agree" in cover.text


def test_fal_cassette_rejects_unsupported_original_fields(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"models": [{"endpoint_id": descriptor.endpoint_id}]},
        )

    fal_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FalProvider(
        descriptor,
        FalQueueTransport("test-fal-key", http_client=fal_client),
    )
    worker = FakeWorker()
    app = create_app(
        settings,
        session_factory=factory,
        provider_registry=ProviderRegistry([provider]),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    try:
        with TestClient(app) as client:
            form = client.get(
                f"/create?backend={descriptor.backend_id}",
                auth=_auth(client),
            )
            assert form.status_code == 200
            assert 'value=""' in _input_tag(form.text, "vocal_language")
            token = client.cookies.get("ace_csrf")
            assert token
            response = client.post(
                "/create",
                auth=_auth(client),
                data={
                    "csrf_token": token,
                    "backend": str(descriptor.backend_id),
                    "description": "minimal cassette request",
                    "duration_mode": "custom",
                    "duration_seconds": "30",
                    "lyrics": "explicit user lyrics",
                    "instrumental": "true",
                    "bpm": "120",
                },
            )
            assert response.status_code == 422
            assert "lyrics is not supported by the selected backend" in response.text
            assert worker.enqueued == []
        with factory() as session:
            assert session.query(Job).count() == 0
    finally:
        asyncio.run(fal_client.aclose())
        engine.dispose()


def test_fal_backend_pricing_tracks_selected_model_duration_and_variations(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    catalog = load_catalog()
    descriptors = [
        catalog.by_backend_id("fal/fal-ai/ace-step"),
        catalog.by_backend_id("fal/minimax/music-3"),
    ]

    async def health_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        endpoint_id = request.url.params["endpoint_id"]
        return httpx.Response(200, json={"models": [{"endpoint_id": endpoint_id}]})

    async def pricing_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models/pricing"
        endpoint_id = request.url.params["endpoint_id"]
        unit_price, unit = {
            "fal-ai/ace-step": (0.0002, "seconds"),
            "minimax/music-3": (0.00125, "compute seconds"),
        }[endpoint_id]
        return httpx.Response(
            200,
            json={
                "prices": [
                    {
                        "endpoint_id": endpoint_id,
                        "unit_price": unit_price,
                        "unit": unit,
                    }
                ]
            },
        )

    fal_client = httpx.AsyncClient(transport=httpx.MockTransport(health_handler))
    pricing_http = httpx.AsyncClient(transport=httpx.MockTransport(pricing_handler))
    transport = FalQueueTransport("test-fal-key", http_client=fal_client)
    app = create_app(
        settings,
        session_factory=factory,
        provider_registry=ProviderRegistry(
            [FalProvider(descriptor, transport) for descriptor in descriptors]
        ),
        home_ingest_client=FakeHome(),
        worker=FakeWorker(),
    )
    app.state.fal_pricing = FalPricingClient("test-fal-key", ttl_seconds=60, client=pricing_http)
    try:
        with TestClient(app) as client:
            assert client.get("/backend-pricing").status_code == 401

            ace_form = client.get("/create?backend=fal/fal-ai/ace-step", auth=_auth(client))
            assert ace_form.status_code == 200
            assert "Fal account rate: ~$0.0002 per second" in ace_form.text
            assert "Estimated request total: ~$0.0120" in ace_form.text

            minimax_form = client.get("/create?backend=fal/minimax/music-3", auth=_auth(client))
            assert minimax_form.status_code == 200
            assert "Fal account rate: ~$0.00125 per compute second" in minimax_form.text
            assert "Published output-price reference: up to ~$0.1200" in minimax_form.text
            assert "account total depends on runtime usage" in minimax_form.text
            assert 'data-pricing-url="/backend-pricing"' in minimax_form.text

            ace_quote = client.get(
                "/backend-pricing",
                auth=_auth(client),
                params={
                    "backend": "fal/fal-ai/ace-step",
                    "duration_mode": "custom",
                    "duration_seconds": "90",
                    "variation_count": "3",
                },
            ).json()
            assert ace_quote["total"] == "0.0540"
            assert ace_quote["duration_seconds"] == "90"
            assert ace_quote["variation_count"] == 3

            minimax_quote = client.get(
                "/backend-pricing",
                auth=_auth(client),
                params={
                    "backend": "fal/minimax/music-3",
                    "duration_mode": "custom",
                    "duration_seconds": "90",
                    "variation_count": "3",
                },
            ).json()
            assert minimax_quote["total"] is None
            assert minimax_quote["reference_total"] == "0.5400"
    finally:
        asyncio.run(fal_client.aclose())
        asyncio.run(pricing_http.aclose())
        engine.dispose()


def test_continue_original_prefills_every_field_without_enqueue(web_app) -> None:
    app, factory, worker = web_app
    request = _rich_original_request()
    with factory() as session:
        source = create_original_job(session, request, job_id="original-prefill-source")
        session.commit()
        source_id = source.id

    with TestClient(app) as client:
        assert client.get(f"/jobs/{source_id}/continue").status_code == 401
        response = client.get(f"/jobs/{source_id}/continue", auth=_auth(client))
        assert response.status_code == 200
        assert _textarea_value(response.text, "description") == request.description
        assert _textarea_value(response.text, "lyrics") == request.lyrics
        assert 'value="fr"' in _input_tag(response.text, "vocal_language")
        assert 'value="42.0"' in _input_tag(response.text, "duration_seconds")
        assert 'value="111"' in _input_tag(response.text, "bpm")
        assert 'value="D minor"' in _input_tag(response.text, "key_scale")
        assert 'value="3"' in _input_tag(response.text, "time_signature")
        assert 'value="77"' in _input_tag(response.text, "seed")
        assert "checked" not in _input_tag(response.text, "instrumental")
        assert _selected_value(response.text, "prompt_mode") == "enhance"
        assert _selected_value(response.text, "duration_mode") == "custom"
        assert _selected_value(response.text, "variation_count") == "3"
        assert _selected_value(response.text, "output_format") == "flac"
        assert f'value="{source_id}"' in _input_tag(response.text, "continue_from_job_id")
        assert "Generate new version" in response.text
        status_body = client.get(f"/jobs/{source_id}/status", auth=_auth(client)).json()
        assert status_body["continue_url"] == f"/jobs/{source_id}/continue"
        detail = client.get(f"/jobs/{source_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert detail.text.count(f'href="/jobs/{source_id}/continue"') == 1
        assert detail.text.count("Continue this version") == 1
        assert worker.enqueued == []


def test_continue_original_auto_defaults_render_blank_and_submit(web_app) -> None:
    app, factory, worker = web_app
    source_request = OriginalSongRequest(description="plain continuation default")
    with factory() as session:
        source = create_original_job(session, source_request, job_id="original-auto-source")
        session.commit()
        source_id = source.id

    with TestClient(app) as client:
        response = client.get(f"/jobs/{source_id}/continue", auth=_auth(client))
        assert response.status_code == 200
        assert 'name="duration_seconds"' in response.text
        assert 'name="duration_seconds" inputmode="decimal" value=""' in response.text
        assert 'value="None"' not in response.text

        token = client.cookies.get("ace_csrf")
        assert token
        created = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "continue_from_job_id": source_id,
                "description": source_request.description,
                "duration_mode": "auto",
                "duration_seconds": "",
                "variation_count": "1",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert len(worker.enqueued) == 1


def test_continue_original_edits_create_one_same_project_version(web_app) -> None:
    app, factory, worker = web_app
    source_request = _rich_original_request()
    with factory() as session:
        source = create_original_job(session, source_request, job_id="original-edit-source")
        session.commit()
        source_id = source.id
        project_id = source.project_id

    with TestClient(app) as client:
        token = _csrf(client, f"/jobs/{source_id}/continue")
        response = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "continue_from_job_id": source_id,
                "profile_id": "fast-beta-v1",
                "description": "edited glass percussion",
                "lyrics": "new words",
                "vocal_language": "en",
                "prompt_mode": "direct",
                "duration_mode": "auto",
                "duration_seconds": "",
                "bpm": "98",
                "key_scale": "C major",
                "time_signature": "4",
                "seed": "91",
                "variation_count": "2",
                "output_format": "wav",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        new_id = response.headers["location"].rsplit("/", 1)[-1]
        assert new_id != source_id
        assert worker.enqueued == [new_id]

    with factory() as session:
        source = get_job(session, source_id)
        created = get_job(session, new_id)
        assert source is not None and created is not None
        assert source.normalized_request_json == source_request.to_normalized_request_json()
        assert created.project_id == project_id
        assert created.prompt == "edited glass percussion"
        assert (
            created.normalized_request_json["generation"]
            | {
                "prompt": "edited glass percussion",
                "lyrics": "new words",
                "bpm": 98,
                "output_format": "wav",
            }
            == created.normalized_request_json["generation"]
        )
        assert session.query(Job).count() == 2
        # Quote capture is disconnected from the acceptance transaction in the
        # usability recovery; no submission-quote record is created.
        assert get_submission_quote(session, new_id) is None


def test_continue_validation_preserves_edits_source_and_csrf(web_app) -> None:
    app, factory, worker = web_app
    with factory() as session:
        source = create_original_job(
            session,
            OriginalSongRequest(description="valid continuation source"),
            job_id="validation-source",
        )
        session.commit()
        source_id = source.id

    with TestClient(app) as client:
        assert (
            client.post(
                "/create",
                auth=_auth(client),
                data={"continue_from_job_id": source_id, "description": "valid edit"},
            ).status_code
            == 403
        )
        token = _csrf(client, f"/jobs/{source_id}/continue")
        invalid = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "continue_from_job_id": source_id,
                "description": "no",
            },
        )
        assert invalid.status_code == 422
        assert _textarea_value(invalid.text, "description") == "no"
        assert f'value="{source_id}"' in _input_tag(invalid.text, "continue_from_job_id")
        assert "Generate new version" in invalid.text
        assert worker.enqueued == []
    with factory() as session:
        assert session.query(Job).count() == 1


def test_project_new_submission_without_continuation_creates_new_project(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        response = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "brand new project"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        job_id = response.headers["location"].rsplit("/", 1)[-1]
        assert worker.enqueued == [job_id]
    with factory() as session:
        job = get_job(session, job_id)
        assert job is not None
        assert job.project.title == "brand new project"
        assert job.project.jobs == [job]


def test_projects_workspace_orders_versions_outputs_and_job_links(web_app) -> None:
    app, factory, worker = web_app
    with factory() as session:
        older = create_original_job(
            session,
            OriginalSongRequest(description="older separate project"),
            job_id="older-project-job",
        )
        first = create_original_job(
            session,
            OriginalSongRequest(description="initial mix prompt"),
            job_id="project-version-one",
        )
        second = create_original_job(
            session,
            OriginalSongRequest(description="revised mix prompt"),
            job_id="project-version-two",
            project=first.project_id,
        )
        older.project.updated_at = datetime(2026, 8, 7, tzinfo=UTC)
        first.project.updated_at = datetime(2026, 8, 9, tzinfo=UTC)
        first.created_at = datetime(2026, 8, 8, tzinfo=UTC)
        second.created_at = datetime(2026, 8, 9, tzinfo=UTC)
        first.status = JobStatus.COMPLETED
        second.status = JobStatus.FAILED
        second.user_facing_error = "This version failed clearly."
        output = create_output(
            session,
            job_id=first.id,
            variation_index=1,
            result_index=0,
            relative_path=f"{first.id}/variation-01.mp3",
            mime_type="audio/mpeg",
            byte_size=2048,
            sha256="b" * 64,
        )
        session.commit()
        project_id = first.project_id
        older_project_id = older.project_id
        output_id = output.id

    with TestClient(app) as client:
        assert client.get("/projects").status_code == 401
        projects = client.get("/projects", auth=_auth(client))
        assert projects.status_code == 200
        assert projects.text.index(f'href="/projects/{project_id}"') < projects.text.index(
            f'href="/projects/{older_project_id}"'
        )
        assert "2 versions" in projects.text

        detail = client.get(f"/projects/{project_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert detail.text.index("revised mix prompt") < detail.text.rindex("initial mix prompt")
        assert "Version 2" in detail.text and "Version 1" in detail.text
        assert "This version failed clearly." in detail.text
        assert f'src="/media/{output_id}"' in detail.text
        assert f'href="/files/{output_id}/download"' in detail.text
        assert detail.text.count("Continue this version") == 2
        assert f'href="/jobs/{first.id}"' in detail.text
        assert f'href="/jobs/{second.id}"' in detail.text

        history = client.get("/jobs", auth=_auth(client))
        job_detail = client.get(f"/jobs/{first.id}", auth=_auth(client))
        assert f'href="/projects/{project_id}"' in history.text
        assert f'href="/projects/{project_id}"' in job_detail.text
        assert worker.enqueued == []


def test_project_rename_is_bounded_escaped_authenticated_and_csrf_protected(
    web_app,
) -> None:
    app, factory, worker = web_app
    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(description="rename source project"),
            job_id="rename-project-job",
        )
        session.commit()
        project_id = job.project_id
        original_title = job.project.title

    with TestClient(app) as client:
        page = client.get(f"/projects/{project_id}", auth=_auth(client))
        assert page.status_code == 200
        token = client.cookies.get("ace_csrf")
        assert token
        assert (
            client.post(
                f"/projects/{project_id}/rename",
                data={"csrf_token": token, "title": "Unauthorized"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/projects/{project_id}/rename",
                auth=_auth(client),
                data={"title": "Missing CSRF"},
            ).status_code
            == 403
        )
        blank = client.post(
            f"/projects/{project_id}/rename",
            auth=_auth(client),
            data={"csrf_token": token, "title": "   "},
        )
        oversized = client.post(
            f"/projects/{project_id}/rename",
            auth=_auth(client),
            data={"csrf_token": token, "title": "x" * 161},
        )
        assert blank.status_code == 422
        assert oversized.status_code == 422
        assert "must not be empty" in blank.text
        assert "at most 160 characters" in oversized.text
        assert (
            client.post(
                "/projects/missing/rename",
                auth=_auth(client),
                data={"csrf_token": token, "title": "Valid title"},
            ).status_code
            == 404
        )
        renamed = client.post(
            f"/projects/{project_id}/rename",
            auth=_auth(client),
            data={"csrf_token": token, "title": "  <script>Renamed</script>  "},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert renamed.headers["location"] == f"/projects/{project_id}"
        rendered = client.get(f"/projects/{project_id}", auth=_auth(client))
        assert "&lt;script&gt;Renamed&lt;/script&gt;" in rendered.text
        assert "<script>Renamed</script>" not in rendered.text
        assert client.get("/projects/missing", auth=_auth(client)).status_code == 404
        assert worker.enqueued == []

    with factory() as session:
        renamed_job = get_job(session, "rename-project-job")
        assert renamed_job is not None
        assert original_title == "rename source project"
        assert renamed_job.project.title == "<script>Renamed</script>"
        assert session.query(Job).count() == 1


def test_migrated_legacy_job_is_reachable_and_readable_through_projects(
    settings, legacy_database_path
) -> None:
    job_id = "migrated-project-job"
    connection = sqlite3.connect(str(legacy_database_path))
    try:
        connection.execute(
            "INSERT INTO jobs (id, job_type, status, prompt, output_format, "
            "variation_count, created_at, updated_at) VALUES "
            "(?, 'original', 'completed', 'migrated historical prompt', 'mp3', 1, ?, ?)",
            (job_id, "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()
    migration_upgrade(str(legacy_database_path))
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            projects = client.get("/projects", auth=_auth(client))
            assert projects.status_code == 200
            assert f'href="/projects/{job_id}"' in projects.text
            detail = client.get(f"/projects/{job_id}", auth=_auth(client))
            assert detail.status_code == 200
            assert "migrated historical prompt" in detail.text
            assert f'href="/jobs/{job_id}"' in detail.text
            assert "Continue this version" not in detail.text
    finally:
        engine.dispose()


def test_continue_cover_reuses_completed_output_without_youtube_ingest(web_app) -> None:
    app, factory, worker = web_app
    source_request = CoverRequest(
        youtube_url="https://www.youtube.com/watch?v=abc123",
        target_style="dreamy synthwave",
        remix_guidance="wider drums",
        lyrics="replacement words",
        audio_cover_strength=0.41,
        cover_noise_strength=0.22,
        duration_mode="custom",
        duration_seconds=60,
        variation_count=1,
        seed=55,
        output_format="mp3",
        rights_confirmation=True,
    )
    source_bytes = b"completed reusable mp3 output"
    with factory() as session:
        source = create_cover_job(session, source_request, job_id="cover-prefill-source")
        transition_job(session, source.id, JobStatus.INGESTING)
        finalize_cover_job_duration(session, source.id, 28.0)
        transition_job(session, source.id, JobStatus.STAGING)
        confirm_cover_job(session, source.id)
        transition_job(session, source.id, JobStatus.CLOUD_QUEUED)
        transition_job(session, source.id, JobStatus.GENERATING)
        attempt = create_variation_attempt(session, job_id=source.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        transition_variation_attempt(session, attempt.id, JobStatus.COMPLETED)
        attempt.runpod_result_json = {"output": {"duration_seconds": 60.0}}
        relative_path = f"{source.id}/variation-01.mp3"
        output_path = app.state.settings.paths.outputs / relative_path
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(source_bytes)
        source_output = create_output(
            session,
            job_id=source.id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(source_bytes),
            sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
        transition_job(session, source.id, JobStatus.COMPLETED)
        session.commit()
        source_id = source.id
        source_output_id = source_output.id
        project_id = source.project_id

    with TestClient(app) as client:
        form = client.get(f"/jobs/{source_id}/continue", auth=_auth(client))
        assert form.status_code == 200
        assert 'name="youtube_url"' not in form.text
        assert _textarea_value(form.text, "target_style") == "dreamy synthwave"
        assert _textarea_value(form.text, "remix_guidance") == "wider drums"
        assert _textarea_value(form.text, "lyrics") == "replacement words"
        assert 'value="0.41"' in _input_tag(form.text, "audio_cover_strength")
        assert 'value="0.22"' in _input_tag(form.text, "cover_noise_strength")
        assert _selected_value(form.text, "duration_mode") == "custom"
        assert 'value="60.0"' in _input_tag(form.text, "duration_seconds")
        assert _selected_value(form.text, "variation_count") == "1"
        assert _selected_value(form.text, "output_format") == "mp3"
        assert 'name="rights_confirmation"' not in form.text
        assert worker.enqueued == []
        token = client.cookies.get("ace_csrf")
        assert token
        edited = {
            "csrf_token": token,
            "continue_from_job_id": source_id,
            "target_style": "acoustic chamber pop",
            "remix_guidance": "soft percussion",
            "lyrics": "edited lyrics",
            "audio_cover_strength": "0.5",
            "cover_noise_strength": "0.1",
            "duration_mode": "custom",
            "duration_seconds": "55",
            "variation_count": "1",
            "seed": "88",
            "output_format": "mp3",
        }
        created_response = client.post(
            "/cover", auth=_auth(client), data=edited, follow_redirects=False
        )
        assert created_response.status_code == 303
        new_id = created_response.headers["location"].rsplit("/", 1)[-1]
        assert worker.enqueued == [new_id]

        with factory() as session:
            created = get_job(session, new_id)
            source = get_job(session, source_id)
            assert created is not None and source is not None
            assert created.project_id == project_id
            assert created.status is JobStatus.STAGING
            assert created.source_url == "https://www.youtube.com/watch?v=abc123"
            assert created.source_duration == 60.0
            assert created.source_byte_size == len(source_bytes)
            assert created.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
            assert created.normalized_request_json["generation"]["target_style"] == (
                "acoustic chamber pop"
            )
            assert created.normalized_request_json["generation"]["duration_mode"] == "custom"
            assert created.normalized_request_json["resolved_target_duration_seconds"] == 55.0
            assert created.normalized_request_json["continuation_source"] == {
                "job_id": source_id,
                "output_id": source_output_id,
            }
            assert created.normalized_request_json["cover_staging"]["status"] == "confirmed"
            assert source.status is JobStatus.COMPLETED
            assert "continuation_source" not in source.normalized_request_json
            assert get_submission_quote(session, new_id) is None
            submission_nonce = str(uuid4())
            provider_attempt = create_variation_attempt(
                session, job_id=created.id, variation_index=1
            )
            provider_attempt.submission_nonce = submission_nonce
            session.flush()
            provider_payload = dict(ControllerWorker._default_payload(created, provider_attempt))
            assert created.normalized_request_json["continuation_source"] == {
                "job_id": source_id,
                "output_id": source_output_id,
            }
        provider_payload["submission_nonce"] = submission_nonce
        provider_payload["source"] = {
            "url": "https://transfer.example/transfer/v1/continuation-source",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "bytes": len(source_bytes),
            "format": "mp3",
        }
        provider_payload["result_upload"] = {
            "url": "https://transfer.example/transfer/v1/continuation-result",
            "max_bytes": 1024,
        }
        assert "continuation_source" not in provider_payload
        parsed_provider_payload = WorkerRequest.from_mapping(provider_payload)
        assert parsed_provider_payload.task_type == "cover"
        assert (app.state.settings.paths.incoming / new_id / "source.mp3").read_bytes() == (
            source_bytes
        )


def test_continue_rejects_missing_malformed_and_cross_type_sources(web_app) -> None:
    app, factory, worker = web_app
    with factory() as session:
        original = create_original_job(
            session,
            OriginalSongRequest(description="cross type source"),
            job_id="cross-type-source",
        )
        legacy = create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id="legacy-source",
            normalized_request_json={"schema_version": 1, "task_type": "original"},
        )
        malformed = create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id="malformed-source",
            normalized_request_json={
                "schema_version": 2,
                "task_type": "original",
                "profile_id": "fast-beta-v1",
                "generation": {"prompt": "incomplete"},
            },
        )
        session.commit()

    with TestClient(app) as client:
        assert client.get("/jobs/missing/continue", auth=_auth(client)).status_code == 404
        assert client.get(f"/jobs/{legacy.id}/continue", auth=_auth(client)).status_code == 409
        assert client.get(f"/jobs/{malformed.id}/continue", auth=_auth(client)).status_code == 409
        legacy_status = client.get(f"/jobs/{legacy.id}/status", auth=_auth(client)).json()
        assert "continue_url" not in legacy_status
        legacy_detail = client.get(f"/jobs/{legacy.id}", auth=_auth(client))
        malformed_detail = client.get(f"/jobs/{malformed.id}", auth=_auth(client))
        assert legacy_detail.status_code == 200
        assert malformed_detail.status_code == 200
        assert "Continue this version" not in legacy_detail.text
        assert "Continue this version" not in malformed_detail.text
        assert f"/jobs/{legacy.id}/continue" not in legacy_detail.text
        assert f"/jobs/{malformed.id}/continue" not in malformed_detail.text
        token = _csrf(client)
        valid_original = {"csrf_token": token, "description": "valid edited source"}
        for source_id, expected in (
            ("", 404),
            ("missing", 404),
            (legacy.id, 409),
            (malformed.id, 409),
        ):
            response = client.post(
                "/create",
                auth=_auth(client),
                data={**valid_original, "continue_from_job_id": source_id},
            )
            assert response.status_code == expected
        cross_type = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "continue_from_job_id": original.id,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "valid cover style",
                "rights_confirmation": "true",
            },
        )
        assert cross_type.status_code == 409
        assert worker.enqueued == []
    with factory() as session:
        assert session.query(Job).count() == 3
        assert session.query(SubmissionQuote).count() == 0


def test_continue_submission_cannot_reach_campaign_gate(web_app, monkeypatch) -> None:
    """The campaign maintenance gate is quarantined: even when it would raise,
    an ordinary original submission is accepted and enqueued unchanged."""
    app, factory, worker = web_app
    with factory() as session:
        source = create_original_job(
            session,
            OriginalSongRequest(description="campaign gated source"),
            job_id="campaign-source",
        )
        session.commit()
        source_id = source.id

    def blocked(_app) -> None:
        raise HTTPException(status_code=503, detail="maintenance")

    monkeypatch.setattr(web_routes, "_assert_public_enqueue_allowed", blocked)
    with TestClient(app) as client:
        token = _csrf(client, f"/jobs/{source_id}/continue")
        response = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "continue_from_job_id": source_id,
                "description": "valid campaign edit",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        new_id = response.headers["location"].rsplit("/", 1)[-1]
        assert worker.enqueued == [new_id]
    with factory() as session:
        assert session.query(Job).count() == 2


def test_form_shows_seed_estimate_without_history(web_app) -> None:
    app, _, _ = web_app
    with TestClient(app) as client:
        for path in ("/create", "/cover"):
            response = client.get(path, auth=_auth(client))
            assert response.status_code == 200
            assert "Approximate cost (informational only)" in response.text
            assert "USD 0.50/GPU-hour" in response.text
            assert "USD 0.0083" in response.text
            assert "60-second seed" in response.text


def test_form_estimate_uses_separate_original_and_cover_history(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        original = create_original_job(session, OriginalSongRequest(description="done original"))
        cover = create_cover_job(
            session,
            CoverRequest(
                youtube_url="https://www.youtube.com/watch?v=abc123",
                target_style="dreamy synthwave",
                rights_confirmation=True,
            ),
        )
        original_attempt = create_variation_attempt(session, job_id=original.id, variation_index=1)
        original_attempt.status = JobStatus.COMPLETED
        original_attempt.execution_ms = 60_000
        original_attempt.completed_at = datetime.now(UTC)
        cover_attempt = create_variation_attempt(session, job_id=cover.id, variation_index=1)
        cover_attempt.status = JobStatus.COMPLETED
        cover_attempt.execution_ms = 120_000
        cover_attempt.completed_at = datetime.now(UTC)
        session.commit()
    with TestClient(app) as client:
        original_page = client.get("/create", auth=_auth(client))
        assert "USD 0.0083" in original_page.text  # one original sample
        assert "60-second seed" not in original_page.text
        cover_page = client.get("/cover", auth=_auth(client))
        assert "USD 0.0167" in cover_page.text  # one cover sample at 120s
        assert "60-second seed" not in cover_page.text


def test_form_selector_binds_request_total_to_selected_count(web_app) -> None:
    app, _, _ = web_app
    with TestClient(app) as client:
        original_page = client.get("/create", auth=_auth(client))
        assert original_page.status_code == 200
        assert "1 variation: ~USD 0.0083" in original_page.text
        for text in (
            'data-request-text="1 variation: ~USD 0.0083"',
            'data-request-text="2 variations: ~USD 0.0167"',
            'data-request-text="3 variations: ~USD 0.0250"',
            'data-request-text="4 variations: ~USD 0.0333"',
        ):
            assert text in original_page.text
        cover_page = client.get("/cover", auth=_auth(client))
        assert cover_page.status_code == 200
        assert "1 variation: ~USD 0.0083" in cover_page.text
        for text in (
            'data-request-text="1 variation: ~USD 0.0083"',
            'data-request-text="2 variations: ~USD 0.0167"',
            'data-request-text="3 variations: ~USD 0.0250"',
            'data-request-text="4 variations: ~USD 0.0333"',
        ):
            assert text in cover_page.text


def test_cover_form_defaults_to_one_variation_and_persists_explicit_counts(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        page = client.get("/cover", auth=_auth(client))
        assert page.status_code == 200
        assert _selected_value(page.text, "variation_count") == "1"

        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None and job.variation_count == 1
            assert job.rights_confirmation_at is not None

        worker.enqueued.clear()
        token = _csrf(client, "/cover")
        created_four = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
                "variation_count": "4",
            },
            follow_redirects=False,
        )
        assert created_four.status_code == 303
        job_id_four = created_four.headers["location"].rsplit("/", 1)[-1]
        with factory() as session:
            job = get_job(session, job_id_four)
            assert job is not None and job.variation_count == 4


def test_new_flow_confirmed_cover_has_no_second_confirmation_ui(web_app) -> None:
    """A new-flow cover that durably committed `confirmed` staging must not
    render the legacy confirm/cancel UI and must reject the one-time route."""

    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            confirm_cover_job(session, job.id)
            session.commit()

        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert "data-cover-confirmation-form" not in detail.text
        assert "Confirm and generate" not in detail.text
        status_response = client.get(f"/jobs/{job_id}/status", auth=_auth(client))
        assert status_response.json()["cover_confirmation_status"] == "confirmed"

        confirm_token = client.cookies.get("ace_csrf")
        assert confirm_token
        replay = client.post(
            f"/cover/{job_id}/confirm",
            auth=_auth(client),
            data={"csrf_token": confirm_token},
        )
        assert replay.status_code == 409
        assert worker.enqueued == [job_id]


def test_continuation_forms_show_matching_request_estimate(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        original = create_original_job(
            session, _rich_original_request(), job_id="estimate-original"
        )
        cover = create_cover_job(
            session,
            CoverRequest(
                youtube_url="https://www.youtube.com/watch?v=abc123",
                target_style="dreamy synthwave",
                variation_count=4,
                rights_confirmation=True,
            ),
            job_id="estimate-cover",
        )
        transition_job(session, cover.id, JobStatus.INGESTING)
        finalize_cover_job_duration(session, cover.id, 42.0)
        transition_job(session, cover.id, JobStatus.STAGING)
        confirm_cover_job(session, cover.id)
        transition_job(session, cover.id, JobStatus.CLOUD_QUEUED)
        transition_job(session, cover.id, JobStatus.GENERATING)
        attempt = create_variation_attempt(session, job_id=cover.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        transition_variation_attempt(session, attempt.id, JobStatus.COMPLETED)
        attempt.runpod_result_json = {"output": {"duration_seconds": 42.0}}
        relative_path = f"{cover.id}/variation-01.mp3"
        output_bytes = b"estimate continuation output"
        output_path = app.state.settings.paths.outputs / relative_path
        output_path.parent.mkdir(parents=True)
        output_path.write_bytes(output_bytes)
        create_output(
            session,
            job_id=cover.id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(output_bytes),
            sha256=hashlib.sha256(output_bytes).hexdigest(),
        )
        transition_job(session, cover.id, JobStatus.COMPLETED)
        session.commit()
        original_id = original.id
        cover_id = cover.id
    with TestClient(app) as client:
        original_form = client.get(f"/jobs/{original_id}/continue", auth=_auth(client))
        assert original_form.status_code == 200
        assert _selected_value(original_form.text, "variation_count") == "3"
        assert "3 variations: ~USD 0.0250" in original_form.text
        cover_form = client.get(f"/jobs/{cover_id}/continue", auth=_auth(client))
        assert cover_form.status_code == 200
        assert _selected_value(cover_form.text, "variation_count") == "4"
        assert "4 variations: ~USD 0.0333" in cover_form.text


def test_422_rerender_retains_selection_and_shows_matching_estimate(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        rejected = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": "ab",  # below the 3-character minimum
                "variation_count": "4",
            },
        )
        assert rejected.status_code == 422
        assert _selected_value(rejected.text, "variation_count") == "4"
        assert "4 variations: ~USD 0.0333" in rejected.text
        assert worker.enqueued == []
        cover_token = _csrf(client, "/cover")
        rejected_cover = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": cover_token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "ab",
                "variation_count": "2",
            },
        )
        assert rejected_cover.status_code == 422
        assert _selected_value(rejected_cover.text, "variation_count") == "2"
        assert "2 variations: ~USD 0.0167" in rejected_cover.text
        assert worker.enqueued == []
        # A crafted out-of-range count is itself invalid; the 422 re-render
        # must omit the estimate instead of erroring on a missing per-count
        # label. Count 1 is now the valid default, so a cover rejected for an
        # unrelated field still keeps its estimate.
        invalid_count = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": cover_token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "variation_count": "5",
            },
        )
        assert invalid_count.status_code == 422
        assert "Approximate cost" not in invalid_count.text
        assert worker.enqueued == []
        single_default = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": cover_token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "ab",
                "variation_count": "1",
            },
        )
        assert single_default.status_code == 422
        assert _selected_value(single_default.text, "variation_count") == "1"
        assert "1 variation: ~USD 0.0083" in single_default.text
        assert worker.enqueued == []


def test_estimate_failure_omits_only_estimate_on_continuation_and_422(web_app, monkeypatch) -> None:
    app, factory, worker = web_app
    with factory() as session:
        source = create_original_job(
            session, _rich_original_request(), job_id="estimate-fail-source"
        )
        session.commit()
        source_id = source.id

    def explode(_session, **kwargs):
        raise RuntimeError("estimate database failure")

    monkeypatch.setattr(web_routes, "recent_completed_attempt_execution_ms", explode)
    with TestClient(app) as client:
        continued = client.get(f"/jobs/{source_id}/continue", auth=_auth(client))
        assert continued.status_code == 200
        assert "Approximate cost" not in continued.text
        assert _selected_value(continued.text, "variation_count") == "3"
        token = _csrf(client)
        rejected = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "ab", "variation_count": "4"},
        )
        assert rejected.status_code == 422
        assert "Approximate cost" not in rejected.text
        assert _selected_value(rejected.text, "variation_count") == "4"
        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "still generated"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert worker.enqueued == [accepted.headers["location"].rsplit("/", 1)[-1]]


def test_estimate_failure_omits_block_and_submission_continues(web_app, monkeypatch) -> None:
    app, factory, worker = web_app

    def explode(_session, **kwargs):
        raise RuntimeError("estimate database failure")

    monkeypatch.setattr(web_routes, "recent_completed_attempt_execution_ms", explode)
    with TestClient(app) as client:
        token = _csrf(client)
        form_page = client.get("/create", auth=_auth(client))
        assert form_page.status_code == 200
        assert "Approximate cost" not in form_page.text
        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "still generated"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert len(worker.enqueued) == 1
    with factory() as session:
        assert session.query(Job).count() == 1


def test_beta_root_path_keeps_complete_browser_contract_under_prefix(settings) -> None:
    beta_settings = ServiceSettings(**{**settings.model_dump(), "service_root_path": "/beta"})
    engine = create_database_engine(beta_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    worker = FakeWorker()
    app = create_app(
        beta_settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    payload = b"prefix-safe generated mp3"
    output_id: int
    original_job_id = "123e4567-e89b-12d3-a456-426614174001"
    relative_path = f"{original_job_id}/variation-01.mp3"
    output_path = beta_settings.paths.outputs / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    with factory() as session:
        original_job = create_original_job(
            session,
            OriginalSongRequest(description="prefix-safe continuation"),
            job_id=original_job_id,
        )
        output = create_output(
            session,
            job_id=original_job_id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        output_id = output.id
        original_project_id = original_job.project_id

    def assert_prefixed_browser_attributes(html: str) -> None:
        values = re.findall(r'(?:href|src|action)="([^"]+)"', html)
        assert values
        assert all(value.startswith("/beta/") or value == "/beta/" for value in values)
        assert all(not value.startswith("/beta/beta/") for value in values)

    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 401
            dashboard = client.get("/", auth=_auth(client))
            assert dashboard.status_code == 200
            assert dashboard.headers["cache-control"] == "no-store"
            assert "default-src 'self'" in dashboard.headers["content-security-policy"]
            assert_prefixed_browser_attributes(dashboard.text)
            history = client.get("/jobs", auth=_auth(client))
            assert history.status_code == 200
            assert_prefixed_browser_attributes(history.text)
            projects = client.get("/projects", auth=_auth(client))
            assert projects.status_code == 200
            assert f'href="/beta/projects/{original_project_id}"' in projects.text
            assert_prefixed_browser_attributes(projects.text)
            project_detail = client.get(f"/projects/{original_project_id}", auth=_auth(client))
            assert project_detail.status_code == 200
            assert f'action="/beta/projects/{original_project_id}/rename"' in project_detail.text
            assert f'src="/beta/media/{output_id}"' in project_detail.text
            assert f'href="/beta/files/{output_id}/download"' in project_detail.text
            assert_prefixed_browser_attributes(project_detail.text)

            original_form = client.get("/create", auth=_auth(client))
            cover_form = client.get("/cover", auth=_auth(client))
            assert 'action="/beta/create"' in original_form.text
            assert 'action="/beta/cover"' in cover_form.text
            assert_prefixed_browser_attributes(original_form.text)
            assert_prefixed_browser_attributes(cover_form.text)

            token = client.cookies.get("ace_csrf")
            assert token
            missing_csrf = client.post(
                "/create", auth=_auth(client), data={"description": "A valid song"}
            )
            assert missing_csrf.status_code == 403
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={"csrf_token": token, "description": "A valid song"},
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            assert re.fullmatch(r"/beta/jobs/[0-9a-f-]+", accepted.headers["location"])

            detail = client.get(f"/jobs/{original_job_id}", auth=_auth(client))
            assert f'data-status-url="/beta/jobs/{original_job_id}/status"' in detail.text
            assert f'src="/beta/media/{output_id}"' in detail.text
            assert f'href="/beta/files/{output_id}/download"' in detail.text
            assert detail.text.count(f'href="/beta/jobs/{original_job_id}/continue"') == 1
            assert detail.text.count("Continue this version") == 1
            assert f'href="/jobs/{original_job_id}/continue"' not in detail.text
            assert_prefixed_browser_attributes(detail.text)

            status_response = client.get(f"/jobs/{original_job_id}/status", auth=_auth(client))
            status_body = status_response.json()
            assert status_body["detail_url"] == f"/beta/jobs/{original_job_id}"
            assert status_body["status_url"] == f"/beta/jobs/{original_job_id}/status"
            assert status_body["continue_url"] == (f"/beta/jobs/{original_job_id}/continue")
            assert status_body["outputs"][0]["media_url"] == f"/beta/media/{output_id}"
            assert status_body["outputs"][0]["download_url"] == (
                f"/beta/files/{output_id}/download"
            )
            continued_form = client.get(f"/jobs/{original_job_id}/continue", auth=_auth(client))
            assert continued_form.status_code == 200
            assert 'action="/beta/create"' in continued_form.text
            assert f'name="continue_from_job_id" value="{original_job_id}"' in continued_form.text
            assert_prefixed_browser_attributes(continued_form.text)

            cover_token = client.cookies.get("ace_csrf")
            assert cover_token
            created_cover = client.post(
                "/cover",
                auth=_auth(client),
                data={
                    "csrf_token": cover_token,
                    "youtube_url": "https://www.youtube.com/watch?v=abc123",
                    "target_style": "dreamy synthwave",
                    "rights_confirmation": "true",
                },
                follow_redirects=False,
            )
            assert created_cover.status_code == 303
            assert created_cover.headers["location"].startswith("/beta/jobs/")
            cover_job_id = created_cover.headers["location"].rsplit("/", 1)[-1]
            with factory() as session:
                transition_job(session, cover_job_id, JobStatus.INGESTING)
                finalize_cover_job_duration(session, cover_job_id, 42.0)
                transition_job(session, cover_job_id, JobStatus.STAGING)
                session.commit()
            cover_detail = client.get(f"/jobs/{cover_job_id}", auth=_auth(client))
            assert f'action="/beta/cover/{cover_job_id}/confirm"' in cover_detail.text
            assert f'action="/beta/cover/{cover_job_id}/cancel"' in cover_detail.text
            assert_prefixed_browser_attributes(cover_detail.text)

            static = client.get("/beta/static/app.css", auth=_auth(client))
            assert static.status_code == 200
            assert static.headers["cache-control"] == "public, max-age=3600"
            assert client.get(f"/beta/media/{output_id}").status_code == 401
            assert client.get(f"/beta/media/{output_id}", auth=_auth(client)).content == payload
            download = client.get(f"/beta/files/{output_id}/download", auth=_auth(client))
            assert download.status_code == 200
            assert "attachment" in download.headers["content-disposition"]
    finally:
        engine.dispose()


def test_project_delete_route_removes_unpublished_output_file(web_app) -> None:
    app, factory, _ = web_app
    payload = b"legacy route output"
    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(
                description="route deletion output", output_format=OutputFormat.WAV
            ),
            job_id="route-delete-job",
        )
        job.status = JobStatus.COMPLETED
        relative_path = f"{job.id}/variation-01.wav"
        output_path = app.state.settings.paths.outputs / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)
        create_output(
            session,
            job_id=job.id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/wav",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        project_id = job.project_id
        project_title = job.project.title

    with TestClient(app) as client:
        token = _csrf(client, f"/projects/{project_id}")
        deleted = client.post(
            f"/projects/{project_id}/delete",
            auth=_auth(client),
            data={"csrf_token": token, "confirm_title": project_title},
            follow_redirects=False,
        )
        assert deleted.status_code == 303

    assert not output_path.exists()
    with factory() as session:
        assert get_job(session, job.id) is None


def test_form_validation_escaping_and_cover_rights(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        invalid_original = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "no"},
        )
        assert invalid_original.status_code == 422
        assert "description" in invalid_original.text

        cover_token = _csrf(client, "/cover")
        invalid_cover = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": cover_token,
                "youtube_url": "https://www.youtube.com/playlist?list=unsafe",
                "target_style": "synthwave",
                "rights_confirmation": "true",
            },
        )
        assert invalid_cover.status_code == 422

        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": "<script>alert('owned')</script>",
            },
            follow_redirects=False,
        )
        job_id = accepted.headers["location"].rsplit("/", 1)[-1]
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.prompt == "<script>alert('owned')</script>"
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert "&lt;script&gt;alert(&#39;owned&#39;)&lt;/script&gt;" in detail.text
        assert "<script>alert('owned')</script>" not in detail.text
        assert worker.enqueued == [job_id]


def test_cover_confirmation_gates_second_enqueue_and_displays_source_duration(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        assert worker.enqueued == [job_id]
        worker.enqueued.clear()
        initial_detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert initial_detail.status_code == 200
        assert "data-cover-confirmation-form" not in initial_detail.text

        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            session.commit()

        status_response = client.get(f"/jobs/{job_id}/status", auth=_auth(client))
        assert status_response.status_code == 200
        assert status_response.json()["cover_confirmation_status"] == "awaiting_confirmation"
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert "Detected source duration: 42.0 seconds" in detail.text
        assert detail.text.count("data-cover-confirmation-form") == 1
        static_status = client.get("/static/status.js", auth=_auth(client))
        assert "window.location.reload()" in static_status.text
        confirm_token = client.cookies.get("ace_csrf")
        assert confirm_token
        confirmed = client.post(
            f"/cover/{job_id}/confirm",
            auth=_auth(client),
            data={"csrf_token": confirm_token},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert worker.enqueued == [job_id]

        replay = client.post(
            f"/cover/{job_id}/confirm",
            auth=_auth(client),
            data={"csrf_token": confirm_token},
        )
        assert replay.status_code == 409
        assert worker.enqueued == [job_id]


def test_staged_cover_cancellation_is_authenticated_csrf_protected_and_single_use(
    web_app, settings
) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        worker.enqueued.clear()
        source = settings.paths.incoming / job_id / "source.mp3"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"prepared source")
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            job.source_byte_size = source.stat().st_size
            job.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            transition_job(session, job.id, JobStatus.STAGING)
            session.commit()

        assert client.post(f"/cover/{job_id}/cancel").status_code == 401
        assert (
            client.post(
                f"/cover/{job_id}/cancel",
                auth=_auth(client),
                data={"csrf_token": "wrong"},
            ).status_code
            == 403
        )
        cancelled = client.post(
            f"/cover/{job_id}/cancel",
            auth=_auth(client),
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
        assert worker.enqueued == []
        assert not source.exists()
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.error_code == "cover_staging_cancelled"
            assert job.user_facing_error == ("Cover preparation was cancelled before confirmation.")

        replay = client.post(
            f"/cover/{job_id}/cancel",
            auth=_auth(client),
            data={"csrf_token": token},
        )
        assert replay.status_code == 409
        assert worker.enqueued == []


@pytest.mark.parametrize(
    ("description", "duration_mode", "duration_seconds", "expected_status"),
    [
        ("a 30-second song", "custom", "30", 303),
        ("a 45-second song", "custom", "30", 422),
        ("a 30-second song", "auto", "", 422),
        ("make it longer", "custom", "30", 422),
        ("plain piano arrangement", "custom", "30", 303),
    ],
)
def test_original_form_duration_language_validation(
    web_app,
    description: str,
    duration_mode: str,
    duration_seconds: str,
    expected_status: int,
) -> None:
    app, _factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        response = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": description,
                "duration_mode": duration_mode,
                "duration_seconds": duration_seconds,
            },
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        if expected_status == 303:
            assert len(worker.enqueued) == 1


@pytest.mark.parametrize(
    ("target_style", "duration_mode", "duration_seconds", "expected_status"),
    [
        ("an energetic 60-second acoustic cover", "custom", "60", 303),
        ("an energetic 45-second acoustic cover", "custom", "60", 422),
        ("make this cover longer", "custom", "60", 422),
        ("plain energetic acoustic cover", "source", "", 303),
        ("plain energetic acoustic cover", "source", "60", 422),
        ("plain energetic acoustic cover", "custom", "", 422),
    ],
)
def test_cover_form_duration_validation(
    web_app,
    target_style: str,
    duration_mode: str,
    duration_seconds: str,
    expected_status: int,
) -> None:
    app, _factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        response = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": target_style,
                "duration_mode": duration_mode,
                "duration_seconds": duration_seconds,
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        assert len(worker.enqueued) == (1 if expected_status == 303 else 0)


def test_quote_runtime_identity_config_is_bounded_and_required(
    settings: ServiceSettings,
) -> None:
    with pytest.raises(ValueError, match="runpod_worker_runtime_identity is required"):
        ServiceSettings(
            data_root=settings.data_root,
            service_password="test-password",
            home_ingest_token="test-home-token",
            runpod_api_key="test-runpod-key",
            runpod_endpoint_id="test-endpoint",
            eligible_gpu_ids=["L40S"],
        )
    for invalid in ("worker-schema-v2", "sha256:abc", "sha256:" + "g" * 64):
        with pytest.raises(ValueError, match="exact sha256"):
            ServiceSettings(
                data_root=settings.data_root,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
                eligible_gpu_ids=["L40S"],
                runpod_worker_runtime_identity=invalid,
            )


def test_quote_capture_disconnected_but_preserved_machinery_stays_bounded(
    settings: ServiceSettings,
) -> None:
    runtime_settings = ServiceSettings(
        data_root=settings.data_root,
        service_password="test-password",
        home_ingest_token="test-home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
        eligible_gpu_ids=["L40S"],
        runpod_worker_runtime_identity=RUNTIME_A,
    )
    engine = create_database_engine(runtime_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    with factory() as session:
        upsert_gpu_rate(
            session,
            gpu_id="L40S",
            rate_micro_usd_per_hour=700_000,
            hourly_rate_usd="0.7000004",
            source="runpod_flex_api",
            calibration_version=1,
            captured_at=now,
        )
        upsert_runtime_calibration(
            session,
            version=1,
            task_mode="original",
            profile_id="fast-beta-v1",
            model_identity=runtime_settings.acestep_model,
            runtime_identity=RUNTIME_A,
            gpu_class="L40S",
            duration_mode="custom",
            duration_band_min_seconds=30.0,
            duration_band_max_seconds=30.0,
            output_count=1,
            execution_low_ms=20_531,
            execution_high_ms=20_531,
            evidence_source="accepted-local-measurement-v1",
            conservative_margin="0",
            captured_at=now,
        )
        session.commit()

    worker = FakeWorker()
    app = create_app(
        runtime_settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    try:
        with TestClient(app) as client:
            token = _csrf(client)
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={
                    "csrf_token": token,
                    "description": "plain piano arrangement",
                    "duration_mode": "custom",
                    "duration_seconds": "30",
                    # Browser input cannot override the server-owned identity.
                    "runpod_worker_runtime_identity": RUNTIME_B,
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            first_job_id = accepted.headers["location"].rsplit("/", 1)[-1]

            with factory() as session:
                first_job = get_job(session, first_job_id)
                assert first_job is not None
                # The acceptance transaction no longer captures a quote.
                assert get_submission_quote(session, first_job_id) is None
                # The preserved capture path uses only the configured
                # server-owned runtime identity; the browser field is ignored.
                first_quote = capture_submission_quote(app, session, first_job)
                assert first_quote is not None and first_quote.unavailable_reason_code is None
                assert first_quote.calibration_version == 1
                quote_id = first_quote.id
                captured_at = first_quote.captured_at
                repeated = capture_submission_quote(app, session, first_job)
                assert repeated is not None and repeated.id == quote_id
                assert repeated.captured_at == captured_at
                session.commit()

            changed_settings = ServiceSettings(
                data_root=settings.data_root,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
                eligible_gpu_ids=["L40S"],
                runpod_worker_runtime_identity=RUNTIME_B,
            )
            app.state.settings = changed_settings
            with factory() as session:
                first_job = get_job(session, first_job_id)
                assert first_job is not None
                with pytest.raises(EvidenceConflictError, match="conflicting submission quote"):
                    capture_submission_quote(app, session, first_job)
                session.rollback()
                unchanged = get_submission_quote(session, first_job_id)
                assert unchanged is not None
                assert (unchanged.id, unchanged.captured_at, unchanged.calibration_version) == (
                    quote_id,
                    captured_at,
                    1,
                )

            token = _csrf(client)
            missing = client.post(
                "/create",
                auth=_auth(client),
                data={
                    "csrf_token": token,
                    "description": "plain piano arrangement",
                    "duration_mode": "custom",
                    "duration_seconds": "30",
                    "runpod_worker_runtime_identity": RUNTIME_A,
                },
                follow_redirects=False,
            )
            assert missing.status_code == 303
            second_job_id = missing.headers["location"].rsplit("/", 1)[-1]
            with factory() as session:
                # The acceptance transaction creates no quote; the preserved
                # capture path still records the bounded unavailable reason.
                assert get_submission_quote(session, second_job_id) is None
                second_job = get_job(session, second_job_id)
                assert second_job is not None
                second_quote = capture_submission_quote(app, session, second_job)
                assert second_quote is not None
                assert second_quote.unavailable_reason_code == "calibration_missing"
                assert session.query(SubmissionQuote).count() == 2
    finally:
        engine.dispose()


def test_status_polling_and_timing_metadata(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=2)
        session.commit()
        job_id = job.id
    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}/status", auth=_auth(client))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["status_label"] == "Queued"
        assert body["variation_count"] == 2
        assert body["elapsed_seconds"] >= 0
        assert body["outputs"] == []
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert "data-status-url" in detail.text
        assert "/static/status.js" in detail.text


@pytest.mark.parametrize(
    ("phase", "phase_label"),
    [
        ("cloud_wait", "Waiting for the cloud provider"),
        ("worker_initializing", "Cloud worker initializing"),
    ],
)
def test_status_polling_exposes_named_phase_and_elapsed_time(
    web_app, phase: str, phase_label: str
) -> None:
    app, factory, _ = web_app
    with factory() as session:
        job = create_job(session, job_type=JobType.ORIGINAL)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_job(session, job.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        set_variation_progress(session, attempt.id, phase)
        session.commit()
        job_id = job.id

    with TestClient(app) as client:
        body = client.get(f"/jobs/{job_id}/status", auth=_auth(client)).json()
        assert body["phase"] == phase
        assert body["phase_label"] == phase_label
        assert body["phase_detail_label"] == phase_label
        assert body["phase_observed_at"].endswith("+00:00")
        assert body["provider_message"] is None
        assert body["provider_progress"] is None
        assert body["detail_scope"] is None
        assert body["elapsed_seconds"] >= 0

        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert 'id="job-phase"' in detail.text
        assert phase_label in detail.text
        static_status = client.get("/static/status.js", auth=_auth(client))
        assert "job.phase_detail_label || job.phase_label" in static_status.text
        assert "seconds elapsed" in static_status.text


def test_status_polling_exposes_inferred_provider_progress(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        job = create_job(
            session,
            job_type=JobType.ORIGINAL,
            inference_provider="salad",
        )
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_job(session, job.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        set_variation_progress(
            session,
            attempt.id,
            "worker_initializing",
            provider_message="Downloading worker image",
            provider_progress=0.19,
            detail_scope="deployment",
        )
        session.commit()
        job_id = job.id

    with TestClient(app) as client:
        body = client.get(f"/jobs/{job_id}/status", auth=_auth(client)).json()
        assert body["phase_label"] == "Cloud worker initializing"
        assert body["provider_message"] == "Downloading worker image"
        assert body["provider_progress"] == 0.19
        assert body["detail_scope"] == "deployment"
        assert body["phase_detail_label"] == (
            "Downloading worker image — 19% · Deployment status (inferred)"
        )

        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert "Downloading worker image — 19% · Deployment status (inferred)" in detail.text


def test_status_polling_ignores_malformed_persisted_provider_detail(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        job = create_job(session, job_type=JobType.ORIGINAL)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_job(session, job.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        malformed = {
            "kind": "audioventura_progress_v1",
            "phase": "worker_initializing",
            "sequence": 1,
            "observed_at": "2026-08-23T00:00:00+00:00",
            "provider_message": "unsafe\nmessage",
            "provider_progress": float("nan"),
            "detail_scope": ["deployment"],
        }
        attempt.provider_result_json = malformed
        attempt.runpod_result_json = malformed
        session.commit()
        job_id = job.id

    with TestClient(app) as client:
        body = client.get(f"/jobs/{job_id}/status", auth=_auth(client)).json()
        assert body["phase_detail_label"] == "Cloud worker initializing"
        assert body["provider_message"] is None
        assert body["provider_progress"] is None
        assert body["detail_scope"] is None


def test_readiness_reports_components_and_preserves_original_availability(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(available=False),
        home_ingest_client=FakeHome(available=False),
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            dashboard = client.get("/", auth=_auth(client))
            assert dashboard.status_code == 200
            assert "cloud provider is unavailable" in dashboard.text
            assert "Home ingest is unavailable" in dashboard.text
            ready = client.get("/readyz", auth=_auth(client))
            assert ready.status_code == 503
            body = ready.json()
            assert body["components"]["runpod_api"]["ok"] is False
            assert body["components"]["home_ingest"]["ok"] is False
    finally:
        engine.dispose()


def test_readiness_bounds_stalled_probes_without_gating_original_submission(
    settings, monkeypatch
) -> None:
    timeout = 0.05
    monkeypatch.setattr(web_routes, "_READINESS_PROBE_TIMEOUT_SECONDS", timeout)
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    runpod = StalledHealth()
    home = StalledHealth()
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=runpod,
        home_ingest_client=home,
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            for path in ("/", "/cover", "/readyz"):
                started = time.monotonic()
                response = client.get(path, auth=_auth(client))
                elapsed = time.monotonic() - started
                assert elapsed < 1
                assert response.status_code in {200, 503}

            ready = client.get("/readyz", auth=_auth(client))
            assert ready.status_code == 503
            body = ready.json()
            assert body["components"]["runpod_api"] == {
                "ok": False,
                "message": "unreachable",
            }
            assert body["components"]["home_ingest"] == {
                "ok": False,
                "message": "unreachable",
            }
            assert runpod.cancelled >= 1
            assert home.cancelled >= 1

            token = _csrf(client)
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={"csrf_token": token, "description": "A valid song"},
                follow_redirects=False,
            )
            assert accepted.status_code == 303
    finally:
        engine.dispose()


def test_authenticated_playback_download_and_symlink_escape(web_app, settings) -> None:
    app, factory, _ = web_app
    payload = b"valid generated mp3"
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    relative_path = f"{job_id}/variation-01.mp3"
    output_path = settings.paths.outputs / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    with factory() as session:
        create_job(session, job_type=JobType.ORIGINAL, job_id=job_id)
        output = create_output(
            session,
            job_id=job_id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        output_id = output.id
    with TestClient(app) as client:
        assert client.get(f"/media/{output_id}").status_code == 401
        media = client.get(f"/media/{output_id}", auth=_auth(client))
        assert media.status_code == 200
        assert media.content == payload
        assert media.headers["content-type"].startswith("audio/mpeg")
        download = client.get(f"/files/{output_id}/download", auth=_auth(client))
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]

        outside = Path(settings.data_root).parent / "not-an-output.mp3"
        outside.write_bytes(payload)
        output_path.unlink()
        output_path.symlink_to(outside)
        assert client.get(f"/media/{output_id}", auth=_auth(client)).status_code == 404
        outside.unlink()


def test_public_transfer_app_does_not_mount_private_ui(web_app, settings) -> None:
    _, factory, _ = web_app
    transfer_app = create_transfer_app(settings, session_factory=factory)
    with TestClient(transfer_app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/create").status_code == 404
        assert client.get("/jobs").status_code == 404
        assert client.get("/media/1").status_code == 404
        assert client.get("/healthz").status_code == 404

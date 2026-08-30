from __future__ import annotations

import base64
import hashlib
import math
import re
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from ace_home_ingest.app import create_app as create_home_app
from ace_home_ingest.config import HomeIngestSettings
from ace_home_ingest.transfer import BoundedTransferClient
from playwright.sync_api import Page, expect

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.home_ingest import HomeIngestClient
from ace_service.providers.base import (
    BackendId,
    BackendOperation,
    CancelOutcome,
    InferenceMode,
    InferenceRequest,
    InferenceResult,
    ProviderCapabilities,
    ProviderHealth,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    ProviderStatus,
    RequestFeature,
)
from ace_service.providers.registry import BackendRegistry
from ace_service.source_assets import SourceIngestCoordinator
from ace_service.transfers import create_transfer_app
from ace_service.worker import ControllerWorker


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_mp3(path: Path, *, frequency: float = 440.0) -> None:
    import lameenc

    sample_rate = 48_000
    pcm = bytearray()
    for index in range(sample_rate * 2):
        sample = int(12_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        pcm.extend(struct.pack("<h", sample))
    encoder = lameenc.Encoder()
    encoder.set_channels(1)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_out_sample_rate(sample_rate)
    encoder.set_bit_rate(128)
    encoder.set_quality(2)
    path.write_bytes(bytes(encoder.encode(bytes(pcm))) + bytes(encoder.flush()))


def _make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x96:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class _NoopUploader:
    def upload(self, local_path: Path, job_id: str) -> str:
        raise AssertionError(f"legacy uploader called for {local_path} and {job_id}")


class _NonPaidProvider:
    capabilities = ProviderCapabilities(
        name=ProviderName.RUNPOD,
        modes=frozenset({InferenceMode.AUDIO_TO_AUDIO}),
        request_features=frozenset(RequestFeature),
        accepts_worker_schema=frozenset({2}),
        supports_pending_cancel=True,
        supports_running_cancel=True,
        not_found_after_deadline_is_terminal=False,
        backend_id=BackendId("runpod/ace-step-v15-xl-turbo"),
        operation=BackendOperation.AUDIO_TRANSFORM,
        native_formats=frozenset({"mp3"}),
        enforces_requested_duration=False,
        source_duration_min_seconds=1,
        source_duration_max_seconds=600,
        output_duration_min_seconds=1,
        output_duration_max_seconds=600,
    )

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.records: dict[str, tuple[InferenceRequest, bytes, str]] = {}

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        upload = request.worker_payload.get("result_upload")
        source = request.worker_payload.get("source")
        if not isinstance(upload, dict) or not isinstance(source, dict):
            raise AssertionError("the controller did not provide bounded transfer metadata")
        upload_url = upload.get("url")
        source_url = source.get("url")
        if (
            not isinstance(upload_url, str)
            or "/beta-transfer/transfer/v1/output/" not in upload_url
            or not isinstance(source_url, str)
            or "/beta-transfer/transfer/v1/source/" not in source_url
        ):
            raise AssertionError("provider metadata escaped the beta transfer prefix")
        output = Path("/tmp") / f"audioventura-e2e-{request.application_job_id}.mp3"
        _write_mp3(output, frequency=523.25)
        payload = output.read_bytes()
        output.unlink()
        digest = hashlib.sha256(payload).hexdigest()
        response = await self.client.put(
            upload_url,
            content=payload,
            headers={"Content-Type": "audio/mpeg"},
        )
        if response.status_code != 200:
            raise AssertionError(f"fake provider output upload failed: {response.status_code}")
        external_id = f"e2e-{request.application_job_id}-{request.variation_index}"
        self.records[external_id] = (request, payload, digest)
        return ProviderJobRef(ProviderName.RUNPOD, external_id, self.capabilities.backend_id)

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        return ProviderStatus(ProviderPhase.SUCCEEDED, provider_state=ref.external_id)

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        request, payload, digest = self.records[ref.external_id]
        return InferenceResult(
            {
                "schema_version": 2,
                "job_id": request.application_job_id,
                "variation_index": request.variation_index,
                "submission_nonce": request.submission_nonce,
                "status": "uploaded",
                "output": {
                    "format": "mp3",
                    "bytes": len(payload),
                    "sha256": digest,
                    "duration_seconds": 2.0,
                    "effective_seed": 7,
                },
                "worker": {
                    "ace_tag": "e2e-nonpaid",
                    "dit_model": "none",
                    "lm_model": "none",
                    "image_digest": "sha256:e2e",
                    "gpu": "cpu",
                    "model_bundle": {
                        "repo": "local/audioventura",
                        "revision": "0" * 40,
                        "tag": "e2e",
                        "manifest_sha256": "0" * 64,
                    },
                },
            }
        )

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        del ref
        return CancelOutcome.CANCELLED

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "non-paid E2E provider ready")

    async def aclose(self) -> None:
        await self.client.aclose()


class _PrefixApp:
    def __init__(self, app: Any, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if path != self.prefix and not path.startswith(f"{self.prefix}/"):
            await self._not_found(send)
            return
        forwarded = dict(scope)
        internal_path = (
            path
            if self.prefix == "/beta" and path.startswith("/beta/static/")
            else path[len(self.prefix) :] or "/"
        )
        forwarded["path"] = internal_path
        forwarded["raw_path"] = internal_path.encode("utf-8")
        forwarded["root_path"] = self.prefix
        await self.app(forwarded, receive, send)

    @staticmethod
    async def _not_found(send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"not found"})


class _ComposedApp:
    def __init__(self, controller: Any, transfer: Any, home: Any) -> None:
        self.controller = _PrefixApp(controller, "/beta")
        self.transfer = _PrefixApp(transfer, "/beta-transfer")
        self.home = _PrefixApp(home, "/home")
        self._controller_app = controller
        self._transfer_app = transfer
        self._home_app = home

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            await self._not_found(send)
            return
        path = str(scope.get("path", ""))
        if path == "/beta" or path.startswith("/beta/"):
            await self.controller(scope, receive, send)
        elif path == "/beta-transfer" or path.startswith("/beta-transfer/"):
            await self.transfer(scope, receive, send)
        elif path == "/home" or path.startswith("/home/"):
            await self.home(scope, receive, send)
        else:
            await self._not_found(send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        message = await receive()
        if message.get("type") != "lifespan.startup":
            return
        try:
            async with self._controller_app.router.lifespan_context(self._controller_app):
                async with self._transfer_app.router.lifespan_context(self._transfer_app):
                    async with self._home_app.router.lifespan_context(self._home_app):
                        await send({"type": "lifespan.startup.complete"})
                        while True:
                            message = await receive()
                            if message.get("type") == "lifespan.shutdown":
                                await send({"type": "lifespan.shutdown.complete"})
                                return
        except BaseException as exc:
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": str(exc),
                }
            )

    @staticmethod
    async def _not_found(send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"not found"})


@dataclass(frozen=True, slots=True)
class _SourceE2EServer:
    base_url: str
    username: str
    password: str


@pytest.fixture
def source_e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_SourceE2EServer]:
    root = tmp_path_factory.mktemp("audioventura-source-e2e")
    port = _free_port()
    transfer_base = f"http://127.0.0.1:{port}/beta-transfer"
    settings = ServiceSettings(
        data_root=root / "controller-data",
        service_root_path="/beta",
        service_username="e2e-user",
        service_password="e2e-password",
        home_ingest_token="home-token",
        home_ingest_base_url=f"http://127.0.0.1:{port}/home",
        transfer_public_base_url="https://transfer.test",
        transfer_max_source_bytes=536_870_912,
        direct_upload_max_bytes=536_870_912,
        canonical_source_max_bytes=536_870_912,
        inference_provider="runpod",
        inference_enabled_backends="runpod/ace-step-v15-xl-turbo",
        default_original_backend="runpod/ace-step-v15-xl-turbo",
        default_cover_backend="runpod/ace-step-v15-xl-turbo",
        runpod_api_key="e2e-runpod-key",
        runpod_endpoint_id="e2e-endpoint",
    )
    settings.transfer_public_base_url = transfer_base
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    home_settings = HomeIngestSettings(
        data_root=root / "home-data",
        token="home-token",
        transfer_base_url=transfer_base,
        max_source_bytes=536_870_912,
        canonical_source_max_bytes=536_870_912,
        command_timeout_seconds=120,
        transfer_read_timeout_seconds=120,
        transfer_write_timeout_seconds=120,
        cleanup_interval_seconds=60,
    )
    home_transfer = BoundedTransferClient(home_settings)
    home_app = create_home_app(
        home_settings,
        uploader=_NoopUploader(),
        transfer_client=home_transfer,
    )
    controller_home = HomeIngestClient(settings)
    transfer_app = create_transfer_app(settings, session_factory=factory)
    provider = _NonPaidProvider()
    registry = BackendRegistry(
        [provider],
        defaults={BackendOperation.AUDIO_TRANSFORM: "runpod/ace-step-v15-xl-turbo"},
        selectable_backends=["runpod/ace-step-v15-xl-turbo"],
    )
    source_coordinator = SourceIngestCoordinator(settings, factory, controller_home)
    worker = ControllerWorker(
        settings,
        factory,
        registry,
        home_ingest_client=controller_home,
        poll_interval_seconds=0.05,
        home_ingest_semaphore=source_coordinator.home_ingest_semaphore,
    )
    controller_app = create_app(
        settings,
        session_factory=factory,
        provider_registry=registry,
        home_ingest_client=controller_home,
        worker=worker,
        source_coordinator=source_coordinator,
    )
    application = _ComposedApp(controller_app, transfer_app, home_app)
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="source-e2e-server", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    auth = (settings.service_username, settings.service_password)
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/beta/", auth=auth, timeout=0.5)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()
        raise RuntimeError("source E2E server did not become ready")
    try:
        yield _SourceE2EServer(base_url, settings.service_username, settings.service_password)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        engine.dispose()


def test_mobile_direct_upload_real_source_pipeline_and_playlist_order(
    page: Page,
    source_e2e_server: _SourceE2EServer,
    tmp_path: Path,
) -> None:
    page.set_viewport_size({"width": 412, "height": 915})
    credentials = f"{source_e2e_server.username}:{source_e2e_server.password}".encode()
    page.context.set_extra_http_headers(
        {"Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}"}
    )
    page.goto(f"{source_e2e_server.base_url}/beta/sources/new", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Create remix")).to_be_visible()
    expect(page.get_by_text("Bring one source into your private library")).to_be_visible()
    backend_select = page.locator("#backend")
    expect(page.locator('label[for="backend"]')).to_be_visible()
    expect(backend_select).to_have_attribute("required", "")
    expect(backend_select).to_have_attribute("form", "youtube-source-form")
    selected_backend = backend_select.input_value()
    assert selected_backend == "runpod/ace-step-v15-xl-turbo"
    backend_select.select_option(selected_backend)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("tab", name="Upload file").click()
    upload_panel = page.locator("#upload-panel")
    expect(upload_panel).to_be_visible()
    source_path = tmp_path / "browser-source.mp4"
    _make_video(source_path)
    page.locator("#upload-project-title").fill("Browser source project")
    page.locator("#source-file").set_input_files(str(source_path))
    expect(page.locator("#source-file")).to_be_visible()
    page.locator('#upload-panel input[name="rights_confirmation"]').check()
    page.get_by_role("button", name="Upload source").click()
    expect(page.locator("[data-source-progress]")).to_be_visible()
    expect(page.locator("[data-source-status-label]")).to_contain_text("Ready", timeout=60_000)
    assert "/asset-transfer/v2/upload/" not in page.content()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.goto(f"{source_e2e_server.base_url}/beta/projects", wait_until="domcontentloaded")
    page.locator("a.project-card").filter(has_text="Browser source project").click()
    expect(page.get_by_text("Original source")).to_be_visible()
    page.get_by_role("button", name="Play", exact=True).click()
    assert page.locator("#global-audio").get_attribute("src")
    page.locator(".source-card").get_by_role("link", name="Create remix").click()
    expect(page.get_by_role("heading", name="Remix browser-source")).to_be_visible()
    expect(page.locator("#backend")).to_have_value(selected_backend)
    expect(page.locator('label[for="backend"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#clip-start-seconds").fill("0.5")
    page.locator("#clip-end-seconds").fill("1.5")
    page.locator("#target_style").fill("minimal piano")
    expect(page.get_by_role("button", name="Generate remix")).to_be_enabled()
    page.get_by_role("button", name="Generate remix").click()
    expect(page).to_have_url(re.compile(r"/beta/jobs/"))
    expect(page.locator("#job-status")).to_contain_text("Completed", timeout=60_000)
    expect(
        page.get_by_role("button", name="Play Browser source project · Variation 1")
    ).to_be_visible()

    page.get_by_role("link", name="Playlists", exact=True).click()
    page.get_by_role("link", name="Browser source project", exact=True).first.click()
    entries = page.locator("[data-playlist-entry]")
    expect(entries).to_have_count(2)
    expect(entries.nth(0)).to_contain_text("Browser source project")
    expect(entries.nth(1)).to_contain_text("Variation 1")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    touch_sizes = page.locator(".button, .play-button").evaluate_all(
        "nodes => nodes.filter(node => node.getClientRects().length).map(node => { "
        "const box = node.getBoundingClientRect(); "
        "return [box.width, box.height]; })"
    )
    assert all(width >= 44 and height >= 44 for width, height in touch_sizes)

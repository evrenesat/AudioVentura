from __future__ import annotations

import base64
import hashlib
import math
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus
from ace_service.providers.base import (
    BackendId,
    BackendOperation,
    CancelOutcome,
    InferenceMode,
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
from ace_service.repository import (
    complete_variation_attempt,
    create_original_job,
    create_output,
    finalize_job_cancellation,
    get_job,
    prepare_variation_submission,
    publish_completed_variation_media,
    transition_job,
    transition_variation_attempt,
)
from ace_service.schemas import OriginalSongRequest

HOME_INGEST_SRC = Path(__file__).parents[2] / "home_ingest" / "src"
if str(HOME_INGEST_SRC) not in sys.path:
    sys.path.insert(0, str(HOME_INGEST_SRC))

E2E_BACKEND = BackendId("runpod/ace-step-v15-xl-turbo")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the sync Playwright session after tests that own asyncio loops.

    The official pytest-playwright fixtures use a synchronous dispatcher
    greenlet.  Once that session starts, its dispatcher loop remains visible
    to synchronous tests in the same pytest process.  Running browser tests
    last keeps the existing asyncio.run()-based unit tests independent while
    preserving normal collection for a dedicated E2E invocation.
    """

    e2e_items = [item for item in items if item.nodeid.startswith("tests/e2e/")]
    if not e2e_items:
        return
    e2e_ids = {id(item) for item in e2e_items}
    items[:] = [item for item in items if id(item) not in e2e_ids] + e2e_items


class FakeProvider:
    capabilities = ProviderCapabilities(
        ProviderName.RUNPOD,
        frozenset(InferenceMode),
        frozenset(RequestFeature),
        frozenset({1, 2}),
        True,
        True,
        True,
        E2E_BACKEND,
    )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "E2E provider ready")

    async def submit(self, request: Any) -> ProviderJobRef:
        return ProviderJobRef(ProviderName.RUNPOD, f"e2e-{request.application_job_id}", E2E_BACKEND)

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        return ProviderStatus(ProviderPhase.QUEUED, provider_state=ref.external_id)

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        del ref
        return InferenceResult({})

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        del ref
        return CancelOutcome.CANCELLED


class E2EWorker:
    def __init__(self, session_factory: Any, cancel_outcomes: dict[str, CancelOutcome]) -> None:
        self.session_factory = session_factory
        self.cancel_outcomes = cancel_outcomes

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is not None and job.cancel_requested_at is not None:
                outcome = self.cancel_outcomes.get(job_id, CancelOutcome.CANCELLED)
                finalize_job_cancellation(session, job_id, outcome)
                session.commit()
        return True


class BetaPathProxy:
    """Model the reverse proxy stripping /beta before the ASGI app sees it."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path == "/beta":
            internal_path = "/"
        elif path.startswith("/beta/static/"):
            # The production nginx contract forwards the nested static mount
            # with /beta intact because Starlette's root_path-aware mount
            # otherwise resolves it as a route miss.
            internal_path = path
        elif path.startswith("/beta/"):
            internal_path = path[len("/beta") :]
        else:
            await self._not_found(send)
            return
        forwarded = dict(scope)
        forwarded["path"] = internal_path
        forwarded["raw_path"] = internal_path.encode("utf-8")
        forwarded["root_path"] = "/beta"
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


@dataclass(frozen=True, slots=True)
class E2EServer:
    base_url: str
    username: str
    password: str
    factory: Any
    alpha_project_id: str
    beta_project_id: str
    alpha_media_id: str
    beta_media_id: str
    cancellable_job_id: str
    too_late_job_id: str
    too_late_project_id: str
    seed_track: Any


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_mp3(path: Path, *, frequency: float) -> None:
    import lameenc

    sample_rate = 48_000
    frames = sample_rate * 2
    pcm = bytearray()
    for index in range(frames):
        sample = int(12_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        pcm.extend(struct.pack("<h", sample))
    encoder = lameenc.Encoder()
    encoder.set_channels(1)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_out_sample_rate(sample_rate)
    encoder.set_bit_rate(128)
    encoder.set_quality(2)
    path.write_bytes(bytes(encoder.encode(bytes(pcm))) + bytes(encoder.flush()))


def _seed_track(
    factory: Any,
    settings: ServiceSettings,
    *,
    job_id: str,
    description: str,
    frequency: float,
) -> tuple[str, str]:
    relative_path = f"{job_id}/variation-01.mp3"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_mp3(path, frequency=frequency)
    payload = path.read_bytes()
    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(description=description),
            job_id=job_id,
        )
        _, attempt, _ = prepare_variation_submission(session, job.id, 1)
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        create_output(
            session,
            job_id=job.id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        complete_variation_attempt(session, attempt.id)
        session.commit()
        published = publish_completed_variation_media(session, job.id, 1)
        session.commit()
        return job.project_id, published[0].id


@pytest.fixture
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("audioventura-e2e")
    settings = ServiceSettings(
        data_root=root / "service-data",
        service_root_path="/beta",
        service_username="e2e-user",
        service_password="e2e-password",
        home_ingest_token="e2e-home-token",
        runpod_api_key="e2e-runpod-key",
        runpod_endpoint_id="e2e-endpoint",
    )
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    provider = FakeProvider()
    registry = BackendRegistry(
        [provider],
        default=ProviderName.RUNPOD,
        defaults={BackendOperation.TEXT_TO_MUSIC: E2E_BACKEND},
    )
    cancel_outcomes: dict[str, CancelOutcome] = {}
    worker = E2EWorker(factory, cancel_outcomes)
    app = create_app(
        settings,
        session_factory=factory,
        provider_registry=registry,
        home_ingest_client=provider,
        worker=worker,
    )
    alpha_project_id, alpha_media_id = _seed_track(
        factory,
        settings,
        job_id="123e4567-e89b-12d3-a456-426614174101",
        description="Alpha ambient composition",
        frequency=440,
    )
    beta_project_id, beta_media_id = _seed_track(
        factory,
        settings,
        job_id="123e4567-e89b-12d3-a456-426614174102",
        description="Beta ambient composition",
        frequency=523.25,
    )
    with factory() as session:
        cancellable = create_original_job(
            session,
            OriginalSongRequest(description="Cancellable queued composition"),
            job_id="123e4567-e89b-12d3-a456-426614174103",
        )
        too_late = create_original_job(
            session,
            OriginalSongRequest(description="Too late cloud composition"),
            job_id="123e4567-e89b-12d3-a456-426614174104",
        )
        transition_job(session, too_late.id, JobStatus.CLOUD_QUEUED)
        session.commit()
        cancellable_job_id = cancellable.id
        too_late_job_id = too_late.id
        too_late_project_id = too_late.project_id
    cancel_outcomes[too_late_job_id] = CancelOutcome.TOO_LATE

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            BetaPathProxy(app),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="audioventura-e2e-server", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
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
        thread.join(timeout=5)
        engine.dispose()
        raise RuntimeError("E2E uvicorn server did not become ready")

    result = E2EServer(
        base_url,
        settings.service_username,
        settings.service_password,
        factory,
        alpha_project_id,
        beta_project_id,
        alpha_media_id,
        beta_media_id,
        cancellable_job_id,
        too_late_job_id,
        too_late_project_id,
        lambda *, job_id, description, frequency: _seed_track(
            factory,
            settings,
            job_id=job_id,
            description=description,
            frequency=frequency,
        ),
    )
    try:
        yield result
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()


@pytest.fixture
def e2e_page(page: Any, e2e_server: E2EServer) -> Any:
    credentials = f"{e2e_server.username}:{e2e_server.password}".encode()
    token = base64.b64encode(credentials).decode("ascii")
    page.context.set_extra_http_headers({"Authorization": f"Basic {token}"})
    page.goto(f"{e2e_server.base_url}/beta/", wait_until="domcontentloaded")
    return page

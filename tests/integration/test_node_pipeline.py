from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ace_node.app import create_app
from ace_node.config import NodeSettings
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobType, TransferDirection
from ace_service.providers.base import InferenceMode, InferenceRequest, RequestFeature
from ace_service.providers.node import NodeProvider
from ace_service.repository import create_job, get_output_by_path
from ace_service.transfers import create_transfer_app, issue_transfer_url
from runpod_worker.audio_output import _encode_mp3, probe_audio_duration
from runpod_worker.schemas import ResultUpload, SourceInput
from runpod_worker.transfer_client import TransferClient

APP = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"


class FakeNodeRuntime:
    calls = 0

    def __init__(self, transfer_app: Any) -> None:
        self.transfer_app = transfer_app

    def execute(self, payload: dict[str, Any], _node_job_id: str) -> dict[str, Any]:
        type(self).calls += 1
        with tempfile.TemporaryDirectory(prefix="node-fixture-") as temporary_root:
            root = Path(temporary_root)
            fixture = root / "fixture.mp3"
            fixture.write_bytes(_encode_mp3(1, b"\0\0" * 48_000))
            transfer = TransferClient(
                opener=lambda request, timeout: _ASGITransferResponse.open(
                    self.transfer_app, request.method, request.full_url
                ),
                connection_factory=lambda _host, _port, timeout: _ASGIConnection(self.transfer_app),
            )
            source = payload.get("source")
            if isinstance(source, dict):
                transfer.download_source(SourceInput(**source), root / "source.mp3")
            upload = payload["result_upload"]
            if not isinstance(upload, dict):
                raise AssertionError("node fixture upload envelope is missing")
            uploaded = transfer.upload_output(ResultUpload(**upload), fixture)
            duration = probe_audio_duration(fixture)
        return {
            "schema_version": 2,
            "job_id": payload["job_id"],
            "submission_nonce": payload["submission_nonce"],
            "variation_index": payload["variation_index"],
            "status": "uploaded",
            "output": {
                "format": "mp3",
                "bytes": uploaded.bytes,
                "sha256": uploaded.sha256,
                "duration_seconds": duration,
                "effective_seed": 1,
            },
            "worker": {"runtime_kind": "fake"},
            "input": {"prompt": "must not be durable"},
            "capability_url": "https://player.evren.io/transfer/v1/secret",
        }


class _ASGITransferResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.headers = response.headers
        self.status = response.status_code
        self._content = response.content
        self._offset = 0

    @classmethod
    def open(cls, app: Any, method: str, url: str) -> _ASGITransferResponse:
        async def request() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="https://player.evren.io"
            ) as client:
                return await client.request(method, url)

        return cls(asyncio.run(request()))

    def __enter__(self) -> _ASGITransferResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content) - self._offset
        value = self._content[self._offset : self._offset + amount]
        self._offset += len(value)
        return value


class _ASGIConnection:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.path = "/"
        self.headers: dict[str, str] = {}
        self.body = bytearray()
        self.response: _ASGITransferResponse | None = None

    def putrequest(self, _method: str, path: str) -> None:
        self.path = path

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        return None

    def send(self, chunk: bytes) -> None:
        self.body.extend(chunk)

    def getresponse(self) -> _ASGITransferResponse:
        async def request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="https://player.evren.io"
            ) as client:
                return await client.put(self.path, content=bytes(self.body), headers=self.headers)

        self.response = _ASGITransferResponse(asyncio.run(request()))
        return self.response

    def close(self) -> None:
        return None


def test_controller_node_provider_to_real_node_api_and_db(tmp_path: Path) -> None:
    controller_settings = ServiceSettings(
        data_root=tmp_path / "controller",
        service_password="service-secret",
        home_ingest_token="home-secret",
        runpod_api_key="runpod-secret",
        runpod_endpoint_id="runpod-endpoint",
        transfer_public_base_url="https://player.evren.io",
        transfer_max_output_bytes=10_000_000,
    )
    engine = create_database_engine(controller_settings)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        create_job(session, job_type=JobType.ORIGINAL, job_id=APP)
        issued = issue_transfer_url(
            session,
            controller_settings,
            job_id=APP,
            direction=TransferDirection.OUTPUT_UPLOAD,
            expected_relative_path=f"{APP}/variation-01.mp3",
            expected_extension=".mp3",
            max_bytes=10_000_000,
            token="node-output-capability",
        )
        session.commit()
    transfer_app = create_transfer_app(controller_settings, session_factory=session_factory)
    settings = NodeSettings(
        data_root=tmp_path, token="secret", runtime_receipt="sha256:" + "a" * 64
    )
    node_app = create_app(settings, runtime_factory=lambda: FakeNodeRuntime(transfer_app))
    node_app.state.worker.start()
    assert node_app.state.worker.wait_ready() == "ready"

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=node_app)
        client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8210")
        provider = NodeProvider("http://127.0.0.1:8210", "secret", http_client=client)
        payload = {
            "schema_version": 2,
            "job_id": APP,
            "submission_nonce": NONCE,
            "variation_index": 1,
            "task_type": "original",
            "profile_id": "fast-beta-v1",
            "resolved_parameters": {
                "profile_id": "fast-beta-v1",
                "task_type": "original",
                "prompt_mode": "direct",
                "duration_mode": "auto",
                "duration": -1.0,
                "caption": "x",
                "lyrics": "",
                "seed": 1,
                "inference_steps": 8,
                "shift": 1.0,
                "lm_temperature": 0.85,
                "lm_cfg_scale": 2.0,
                "lm_top_k": 0,
                "lm_top_p": 0.9,
                "lm_negative_prompt": "NO USER INPUT",
                "thinking": False,
                "use_cot_metas": False,
                "use_cot_caption": False,
                "use_cot_language": False,
                "audio_cover_strength": 1.0,
                "cover_noise_strength": 0.0,
            },
            "generation": {
                "prompt": "x",
                "lyrics": "",
                "instrumental": False,
                "vocal_language": "en",
                "prompt_mode": "direct",
                "duration_mode": "auto",
                "duration_seconds": None,
                "duration": -1.0,
                "bpm": None,
                "key_scale": None,
                "time_signature": None,
                "seed": 1,
                "output_format": "mp3",
                "audio_cover_strength": 1.0,
                "cover_noise_strength": 0.0,
            },
            "source": None,
            "result_upload": {
                "url": issued.url,
                "max_bytes": 10_000_000,
            },
        }
        request = InferenceRequest(
            APP,
            1,
            NONCE,
            InferenceMode.PROMPT_TO_AUDIO,
            frozenset(RequestFeature),
            payload,
            1000,
            1000,
        )
        ref = await provider.submit(request)
        duplicate = await provider.submit(request)
        assert duplicate.external_id == ref.external_id
        for _ in range(100):
            status = await provider.status(ref)
            if status.phase.value == "succeeded":
                break
            await asyncio.sleep(0.01)
        result = await provider.result(ref)
        output = result.metadata["output"]
        assert isinstance(output, dict)
        output_path = controller_settings.paths.outputs / APP / "variation-01.mp3"
        assert output_path.is_file()
        assert output["bytes"] == output_path.stat().st_size
        assert output["sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()
        assert abs(float(output["duration_seconds"]) - probe_audio_duration(output_path)) < 0.001
        assert FakeNodeRuntime.calls == 1
        raw_db = (tmp_path / "node.sqlite3").read_bytes()
        assert b"capability" not in raw_db
        with session_factory() as session:
            output_row = get_output_by_path(
                session, job_id=APP, relative_path=f"{APP}/variation-01.mp3"
            )
            assert output_row is not None
            assert output_row.byte_size == output["bytes"]
            assert output_row.sha256 == output["sha256"]
        await provider.aclose()

    try:
        asyncio.run(scenario())
    finally:
        node_app.state.worker.stop()
        engine.dispose()

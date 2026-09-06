"""Route-level conformance tests for the ailocals.v1 completion surface."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ace_service.ailocals.protocol import (
    CAPABILITY_ACE,
    AilocalsError,
    LeaseRequestData,
    decode_enroll_request,
    error_envelope,
)
from ace_service.ailocals.routes import router as ailocals_router
from ace_service.ailocals.service import AilocalsWorkerService
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import Output
from ace_service.providers.ailocals import BACKEND_ID, AilocalsProvider
from ace_service.repository import create_original_job, prepare_variation_submission
from ace_service.schemas import OriginalSongRequest

JOB_ID = "11111111-1111-4111-8111-111111111111"


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        data_root=tmp_path / "service-data",
        service_password="test-password",
        home_ingest_token="test-home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
        ailocals_enabled=True,
        ailocals_environment="beta",
        inference_enabled_backends=("runpod/ace-step-v15-xl-turbo,ailocals/ace-step-v15-xl-turbo"),
        default_original_backend="runpod/ace-step-v15-xl-turbo",
        default_cover_backend="ailocals/ace-step-v15-xl-turbo",
    )


def _stub_builder(job: Any, attempt: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "job_id": job.id,
        "task_type": job.job_type.value,
        "variation_index": attempt.variation_index,
        "generation": {"prompt": "fixture prompt", "lyrics": "", "output_format": "mp3"},
        "resolved_parameters": {"seed": 7},
    }


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = _settings(tmp_path)
        self.engine = create_database_engine(self.settings)
        initialize_database(self.engine)
        self.factory = create_session_factory(self.engine)
        self.service = AilocalsWorkerService(self.factory, self.settings)
        self.service.payload_builder = _stub_builder
        self.app = FastAPI()
        self.app.state.settings = self.settings
        self.app.state.ailocals_service = self.service
        self.app.include_router(ailocals_router)

        @self.app.exception_handler(AilocalsError)
        async def ailocals_error_handler(request: Any, exc: AilocalsError) -> JSONResponse:
            del request
            return JSONResponse(
                status_code=exc.http_status, content=error_envelope(exc.code, exc.message)
            )

    def close(self) -> None:
        self.engine.dispose()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://ailocals.test",
        )

    def call(self, coroutine: Any) -> httpx.Response:
        return asyncio.run(coroutine)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _run() -> httpx.Response:
            async with self.client() as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_run())

    def enroll_worker(self) -> str:
        token, _ = self.service.create_enrollment()
        body = {
            "protocol_version": "ailocals.v1",
            "worker_name": "Fixture Mac",
            "software_version": "0.1.0",
            "capabilities": [
                {
                    "id": CAPABILITY_ACE,
                    "category": "music",
                    "parameters": {
                        "worker_schema": 2,
                        "model_bundle_revision": "fixture-bundle-1",
                        "manifest_sha256": "a" * 64,
                        "accelerator": "mps",
                        "formats": ["mp3"],
                    },
                }
            ],
        }
        name, version, capabilities = decode_enroll_request(body)
        outcome = self.service.enroll(token, name, version, capabilities)
        return outcome.worker_token

    def enqueue_job(self) -> tuple[str, str]:
        with self.factory() as session:
            job = create_original_job(
                session,
                OriginalSongRequest(description="completion fixture"),
                job_id=JOB_ID,
                inference_provider="ailocals",
                inference_backend=BACKEND_ID,
            )
            job.inference_provider = "ailocals"
            job.inference_backend = str(BACKEND_ID)
            _, attempt, nonce = prepare_variation_submission(
                session,
                job.id,
                1,
                inference_provider="ailocals",
                inference_backend=BACKEND_ID,
            )
            session.commit()
            return job.id, nonce


def _multipart_body(parts: list[tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "fixtureailocalsboundary"
    chunks: list[bytes] = []
    for name, payload, content_type in parts:
        chunks.append(b"--" + boundary.encode() + b"\r\n")
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n'.encode())
        chunks.append(f"Content-Type: {content_type}\r\n".encode())
        chunks.append(b"\r\n")
        chunks.append(payload + b"\r\n")
    chunks.append(b"--" + boundary.encode() + b"--\r\n")
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _result_metadata(job_id: str, nonce: str, output_bytes: bytes) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "job_id": job_id,
        "submission_nonce": nonce,
        "variation_index": 1,
        "status": "uploaded",
        "profile_id": "fast-beta-v1",
        "input": {"caption": "completion fixture", "lyrics": ""},
        "effective": {"caption": "completion fixture", "lyrics": ""},
        "resolved_parameters": {"seed": 7},
        "generated_metadata": {"bpm": 120},
        "output": {
            "bytes": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
            "effective_seed": 7,
            "seed": 7,
        },
        "worker": {
            "ace_tag": "v0.1.8",
            "dit_model": "test-model",
            "lm_model": "test-lm",
            "image_digest": "sha256:" + "d" * 64,
            "gpu": "test-gpu",
            "model_bundle": {
                "repo": "evrenesat/audioventura-ace-step-v0.1.8",
                "revision": "6f196b2c116474c43a96fc8331ebcd2057e18eef",
                "tag": "av-v0.1.8-bundle-1",
                "manifest_sha256": "c" * 64,
            },
        },
    }


def _commit_output(harness: Harness, job_id: str, output_bytes: bytes) -> None:
    with harness.factory() as session:
        session.add(
            Output(
                job_id=job_id,
                variation_index=1,
                result_index=0,
                relative_path=f"{job_id}/variation-01.mp3",
                mime_type="audio/mpeg",
                byte_size=len(output_bytes),
                sha256=hashlib.sha256(output_bytes).hexdigest(),
            )
        )
        session.commit()


def test_info_response_shape(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        response = harness.request("GET", "/api/ailocals/v1/info")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "protocol_version",
            "service_kind",
            "environment",
            "supported_capabilities",
            "limits",
        }
        assert body["protocol_version"] == "ailocals.v1"
        assert body["service_kind"] == "audioventura"
        assert body["environment"] == "beta"
        assert body["supported_capabilities"] == [CAPABILITY_ACE]
        assert body["limits"]["lease_seconds"] == 90
        assert body["limits"]["control_max_bytes"] == 262144
    finally:
        harness.close()


def test_worker_routes_require_credential(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        response = harness.request(
            "POST",
            "/api/ailocals/v1/presence",
            json={"protocol_version": "ailocals.v1", "capabilities": []},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        response = harness.request(
            "POST",
            "/api/ailocals/v1/lease",
            json={
                "protocol_version": "ailocals.v1",
                "capability_id": CAPABILITY_ACE,
                "wait_seconds": 0,
            },
        )
        assert response.status_code == 401
    finally:
        harness.close()


def test_lease_returns_hash_bound_payload_and_completes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        token = harness.enroll_worker()
        headers = {"X-Ailocals-Worker-Token": token}
        product_job_id, nonce = harness.enqueue_job()
        with harness.factory() as session:
            from ace_service.repository import get_job

            job = get_job(session, product_job_id)
            job.inference_provider = "ailocals"
            job.inference_backend = str(BACKEND_ID)
            session.commit()
        asyncio.run(
            AilocalsProvider(harness.service).submit(_provider_request(product_job_id, nonce))
        )

        response = harness.request(
            "POST",
            "/api/ailocals/v1/lease",
            json={
                "protocol_version": "ailocals.v1",
                "capability_id": CAPABILITY_ACE,
                "wait_seconds": 0,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        lease = response.json()
        assert set(lease) == {
            "protocol_version",
            "job_id",
            "attempt",
            "lease_token",
            "lease_expires_at",
            "deadline_at",
            "capability_id",
            "payload_encoding",
            "payload_base64",
            "payload_sha256",
        }
        decoded = base64.b64decode(lease["payload_base64"])
        assert hashlib.sha256(decoded).hexdigest() == lease["payload_sha256"]
        envelope = json.loads(decoded)
        assert envelope["schema_version"] == 2
        assert envelope["application_job_id"] == product_job_id
        assert envelope["submission_nonce"] == nonce
        assert envelope["input"]["submission_nonce"] == nonce
        assert envelope["result_upload"]["url"].startswith("https://")

        output_bytes = b"fixture-output-bytes"
        _commit_output(harness, product_job_id, output_bytes)
        metadata = _result_metadata(product_job_id, nonce, output_bytes)
        result_bytes = json.dumps(metadata).encode()
        body, content_type = _multipart_body(
            [
                (
                    "metadata",
                    json.dumps(
                        {
                            "protocol_version": "ailocals.v1",
                            "attempt": 1,
                            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                        }
                    ).encode(),
                    "application/json",
                ),
                ("result", result_bytes, "application/json"),
            ]
        )
        response = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease['job_id']}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease["lease_token"],
                "Content-Type": content_type,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"protocol_version": "ailocals.v1", "accepted": True}

        # A lost acknowledgement retries with identical bytes.
        repeat = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease['job_id']}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease["lease_token"],
                "Content-Type": content_type,
            },
        )
        assert repeat.status_code == 200

        # Different result bytes for the accepted attempt conflict.
        altered = dict(metadata)
        altered["effective"] = {"caption": "changed", "lyrics": ""}
        altered_bytes = json.dumps(altered).encode()
        body, content_type = _multipart_body(
            [
                (
                    "metadata",
                    json.dumps(
                        {
                            "protocol_version": "ailocals.v1",
                            "attempt": 1,
                            "result_sha256": hashlib.sha256(altered_bytes).hexdigest(),
                        }
                    ).encode(),
                    "application/json",
                ),
                ("result", altered_bytes, "application/json"),
            ]
        )
        conflict = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease['job_id']}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease["lease_token"],
                "Content-Type": content_type,
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "result_conflict"
    finally:
        harness.close()


def test_complete_rejects_missing_transfer_evidence(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        token = harness.enroll_worker()
        headers = {"X-Ailocals-Worker-Token": token}
        product_job_id, nonce = harness.enqueue_job()
        provider = AilocalsProvider(harness.service)
        ref = asyncio.run(provider.submit(_provider_request(product_job_id, nonce)))
        lease = harness.service.claim(
            harness.service.authenticate(token),
            LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0),
        )
        assert lease is not None and lease.job_id == ref.external_id

        output_bytes = b"uncommitted-output"
        metadata = _result_metadata(product_job_id, nonce, output_bytes)
        result_bytes = json.dumps(metadata).encode()
        body, content_type = _multipart_body(
            [
                (
                    "metadata",
                    json.dumps(
                        {
                            "protocol_version": "ailocals.v1",
                            "attempt": 1,
                            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                        }
                    ).encode(),
                    "application/json",
                ),
                ("result", result_bytes, "application/json"),
            ]
        )
        response = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease.job_id}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease.lease_token,
                "Content-Type": content_type,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
    finally:
        harness.close()


def test_complete_rejects_duplicate_and_unknown_parts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        token = harness.enroll_worker()
        headers = {"X-Ailocals-Worker-Token": token}
        product_job_id, nonce = harness.enqueue_job()
        asyncio.run(
            AilocalsProvider(harness.service).submit(_provider_request(product_job_id, nonce))
        )
        lease = harness.service.claim(
            harness.service.authenticate(token),
            LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0),
        )
        assert lease is not None
        metadata = {
            "protocol_version": "ailocals.v1",
            "attempt": 1,
            "result_sha256": "a" * 64,
        }
        body, content_type = _multipart_body(
            [
                ("metadata", json.dumps(metadata).encode(), "application/json"),
                ("metadata", json.dumps(metadata).encode(), "application/json"),
                ("result", b"{}", "application/json"),
            ]
        )
        response = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease.job_id}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease.lease_token,
                "Content-Type": content_type,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

        body, content_type = _multipart_body(
            [
                ("metadata", json.dumps(metadata).encode(), "application/json"),
                ("result", b"{}", "application/json"),
                ("artifact", b"not-allowed", "audio/mp4"),
            ]
        )
        response = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease.job_id}/complete",
            content=body,
            headers={
                **headers,
                "X-Ailocals-Lease-Token": lease.lease_token,
                "Content-Type": content_type,
            },
        )
        assert response.status_code == 400
    finally:
        harness.close()


def test_heartbeat_stale_attempt_is_lease_lost(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        token = harness.enroll_worker()
        headers = {"X-Ailocals-Worker-Token": token}
        product_job_id, nonce = harness.enqueue_job()
        asyncio.run(
            AilocalsProvider(harness.service).submit(_provider_request(product_job_id, nonce))
        )
        lease = harness.service.claim(
            harness.service.authenticate(token),
            LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0),
        )
        assert lease is not None
        response = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease.job_id}/heartbeat",
            json={"protocol_version": "ailocals.v1", "attempt": 2, "progress_percent": 10},
            headers={**headers, "X-Ailocals-Lease-Token": lease.lease_token},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "lease_lost"

        healthy = harness.request(
            "POST",
            f"/api/ailocals/v1/jobs/{lease.job_id}/heartbeat",
            json={"protocol_version": "ailocals.v1", "attempt": 1, "progress_percent": 10},
            headers={**headers, "X-Ailocals-Lease-Token": lease.lease_token},
        )
        assert healthy.status_code == 200
        assert healthy.json()["cancel_requested"] is False
    finally:
        harness.close()


def test_empty_lease_is_204(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        token = harness.enroll_worker()
        response = harness.request(
            "POST",
            "/api/ailocals/v1/lease",
            json={
                "protocol_version": "ailocals.v1",
                "capability_id": CAPABILITY_ACE,
                "wait_seconds": 0,
            },
            headers={"X-Ailocals-Worker-Token": token},
        )
        assert response.status_code == 204
        assert response.content == b""
    finally:
        harness.close()


def _provider_request(job_id: str, nonce: str) -> Any:
    from ace_service.providers.base import InferenceMode, InferenceRequest, RequestFeature

    payload: dict[str, Any] = {
        "schema_version": 2,
        "job_id": job_id,
        "task_type": "original",
        "variation_index": 1,
        "submission_nonce": nonce,
        "generation": {"prompt": "fixture prompt", "lyrics": "", "output_format": "mp3"},
        "resolved_parameters": {"seed": 7},
        "result_upload": {"max_bytes": 268435456},
    }
    return InferenceRequest(
        application_job_id=job_id,
        variation_index=1,
        submission_nonce=nonce,
        mode=InferenceMode.PROMPT_TO_AUDIO,
        requested_features=frozenset({RequestFeature.PROMPT}),
        worker_payload=payload,
        execution_timeout_ms=1200000,
        queue_timeout_ms=7200000,
    )

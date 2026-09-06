"""Tests for the durable ailocals universal-worker service."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ace_service.ailocals.protocol import (
    CAPABILITY_ACE,
    AilocalsError,
    ErrorCode,
    LeaseRequestData,
    PresenceEntry,
    PresenceState,
    decode_enroll_request,
    utc_now,
)
from ace_service.ailocals.service import AilocalsWorkerService
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus
from ace_service.providers.ailocals import BACKEND_ID, AilocalsProvider
from ace_service.providers.base import InferenceMode, InferenceRequest, RequestFeature
from ace_service.repository import create_original_job, prepare_variation_submission
from ace_service.schemas import OriginalSongRequest


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


def _stub_builder(job: Any, attempt: Any) -> Mapping[str, Any]:
    return {
        "schema_version": 2,
        "job_id": job.id,
        "task_type": job.job_type.value,
        "variation_index": attempt.variation_index,
        "generation": {"prompt": "fixture prompt", "lyrics": "", "output_format": "mp3"},
        "resolved_parameters": {"seed": 7},
    }


@pytest.fixture
def harness(tmp_path: Path):
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    service = AilocalsWorkerService(factory, settings)
    service.payload_builder = _stub_builder
    yield settings, factory, service
    engine.dispose()


def _enroll(service: AilocalsWorkerService) -> tuple[str, str]:
    token, _ = service.create_enrollment()
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
    worker_name, software_version, capabilities = decode_enroll_request(body)
    outcome = service.enroll(token, worker_name, software_version, capabilities)
    worker = service.authenticate(outcome.worker_token)
    return outcome.worker_token, worker.id


def _enqueue_ailocals_job(factory: Any) -> tuple[str, str]:
    """Create one product job with a prepared ailocals attempt; return ids."""

    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(description="universal worker fixture"),
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


def _provider_request(job_id: str, nonce: str) -> InferenceRequest:
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


def _submit(harness: Any, job_id: str, nonce: str) -> str:
    _, factory, service = harness
    provider = AilocalsProvider(service)
    ref = asyncio.run(provider.submit(_provider_request(job_id, nonce)))
    del factory
    return ref.external_id


def test_enroll_consumes_token_once_and_enforces_single_client(harness) -> None:
    _, factory, service = harness
    _worker_token, worker_id = _enroll(service)
    assert worker_id

    first_token, _ = service.create_enrollment()
    with pytest.raises(AilocalsError) as excinfo:
        service.enroll(
            first_token,
            "Second Mac",
            "0.1.0",
            _enroll_request_capabilities(),
        )
    assert excinfo.value.code is ErrorCode.CLIENT_ALREADY_ENROLLED
    with factory() as session:
        from ace_service.models import AilocalsEnrollment

        row = (
            session.query(AilocalsEnrollment)
            .filter(AilocalsEnrollment.token_hash != "")
            .order_by(AilocalsEnrollment.created_at.desc())
            .first()
        )
        assert row is not None and row.used_at is None


def _enroll_request_capabilities() -> tuple:
    from ace_service.ailocals.protocol import decode_capability_entry

    return (
        decode_capability_entry(
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
        ),
    )


def test_enroll_rejects_unknown_token_without_worker(harness) -> None:
    with pytest.raises(AilocalsError) as excinfo:
        service_enroll_with_token(harness[2], "b" * 43)
    assert excinfo.value.code is ErrorCode.ENROLLMENT_INVALID


def service_enroll_with_token(service: AilocalsWorkerService, token: str) -> None:
    service.enroll(token, "Fixture Mac", "0.1.0", _enroll_request_capabilities())


def test_claim_lease_heartbeat_fail_lifecycle(harness) -> None:
    _worker_token, _ = _enroll(harness[2])
    worker = harness[2].authenticate(_worker_token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)

    lease = harness[2].claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None
    assert lease.job_id == row_id
    assert lease.attempt == 1
    assert lease.capability_id == CAPABILITY_ACE
    assert lease.lease_token and lease.lease_expires_at is not None
    assert lease.payload_base64 and lease.payload_sha256

    # A second lease for the same worker while one is active is worker_busy.
    with pytest.raises(AilocalsError) as excinfo:
        harness[2].claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert excinfo.value.code is ErrorCode.WORKER_BUSY

    heartbeat = harness[2].heartbeat(worker, row_id, lease.lease_token, 1, 10)
    assert heartbeat.cancel_requested is False
    assert heartbeat.lease_expires_at is not None

    harness[2].fail(worker, row_id, lease.lease_token, 1, "resource_exhausted", True)
    row = _load_row(harness[1], row_id)
    assert row["state"] == "failed"
    assert row["error_code"] == "ailocals_resource_exhausted"

    # Terminal failure acknowledgement is idempotent for the same attempt.
    harness[2].fail(worker, row_id, lease.lease_token, 1, "resource_exhausted", True)


def test_claim_reports_cancel_requested_after_product_cancel(harness) -> None:
    service = harness[2]
    _worker_token, _ = _enroll(service)
    worker = service.authenticate(_worker_token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)
    lease = service.claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None

    with harness[1]() as session:
        from ace_service.models import VariationAttempt

        attempt = session.query(VariationAttempt).filter(VariationAttempt.job_id == job_id).one()
        attempt.status = JobStatus.CANCELLED
        session.commit()

    heartbeat = service.heartbeat(worker, row_id, lease.lease_token, 1, 0)
    assert heartbeat.cancel_requested is True


def test_heartbeat_rejects_foreign_worker_and_stale_attempt(harness) -> None:
    service = harness[2]
    first_token, _ = _enroll(service)
    first = service.authenticate(first_token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)
    lease = service.claim(first, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None

    # A revoked worker cannot authenticate; a wrong lease token is rejected.
    with pytest.raises(AilocalsError):
        service.heartbeat(first, row_id, "c" * 43, 1, 0)
    with pytest.raises(AilocalsError):
        service.heartbeat(first, row_id, lease.lease_token, 2, 0)


def test_presence_enforces_enrollment_subset(harness) -> None:
    service = harness[2]
    token, _ = _enroll(service)
    worker = service.authenticate(token)
    snapshot = (
        PresenceEntry(
            id=CAPABILITY_ACE,
            state=PresenceState.READY,
            accepting=True,
            active_jobs=0,
            reason=None,
        ),
    )
    server_time = service.presence(worker, snapshot)
    assert server_time is not None
    with pytest.raises(AilocalsError) as excinfo:
        service.presence(
            worker,
            (
                PresenceEntry(
                    id="tts.apple-speech.v1",
                    state=PresenceState.READY,
                    accepting=True,
                    active_jobs=0,
                    reason=None,
                ),
            ),
        )
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    stored = service.list_workers()[0]
    assert stored.presence_json["capabilities"][0]["id"] == CAPABILITY_ACE
    assert stored.last_seen_at is not None


def test_revoke_makes_owned_leases_terminal(harness) -> None:
    service = harness[2]
    token, worker_id = _enroll(service)
    worker = service.authenticate(token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)
    lease = service.claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None

    service.revoke(worker_id)
    with pytest.raises(AilocalsError):
        service.authenticate(token)
    row = _load_row(harness[1], row_id)
    assert row["state"] == "canceled"


def test_reap_expired_leases_without_regenerating(harness) -> None:
    service = harness[2]
    token, _ = _enroll(service)
    worker = service.authenticate(token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)
    lease = service.claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None
    with harness[1]() as session:
        from ace_service.models import AilocalsJob

        row = session.get(AilocalsJob, row_id)
        row.lease_expires_at = utc_now() - __import__("datetime").timedelta(seconds=1)
        session.commit()

    assert service.reap_expired_leases() == 1
    row = _load_row(harness[1], row_id)
    assert row["state"] == "failed"
    assert row["error_code"] == "ailocals_worker_lost"


def _load_row(factory: Any, row_id: str) -> dict[str, Any]:
    with factory() as session:
        from ace_service.models import AilocalsJob

        row = session.get(AilocalsJob, row_id)
        return {"state": row.state, "error_code": row.error_code}


def test_transfer_authority_rejects_cancelled_and_superseded(harness) -> None:
    from fastapi import HTTPException

    from ace_service.models import TransferCapability
    from ace_service.transfers import _ensure_ailocals_authority

    _worker_token, _ = _enroll(harness[2])
    worker = harness[2].authenticate(_worker_token)
    job_id, nonce = _enqueue_ailocals_job(harness[1])
    row_id = _submit(harness, job_id, nonce)
    lease = harness[2].claim(worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0))
    assert lease is not None

    with harness[1]() as session:
        linked = (
            session.query(TransferCapability)
            .filter(TransferCapability.ailocals_job_id == row_id)
            .all()
        )
        assert linked, "claim must issue transfer capabilities bound to the submission"
        for capability in linked:
            assert capability.submission_nonce == nonce
            _ensure_ailocals_authority(session, capability)

        legacy = (
            session.query(TransferCapability)
            .filter(TransferCapability.ailocals_job_id.is_(None))
            .first()
        )
        if legacy is not None:
            _ensure_ailocals_authority(session, legacy)

        with harness[1]() as cancel_session:
            from ace_service.models import AilocalsJob

            row = cancel_session.get(AilocalsJob, row_id)
            row.cancel_requested = True
            cancel_session.commit()
        with harness[1]() as verify_session:
            for capability in (
                verify_session.query(TransferCapability)
                .filter(TransferCapability.ailocals_job_id == row_id)
                .all()
            ):
                with pytest.raises(HTTPException) as excinfo:
                    _ensure_ailocals_authority(verify_session, capability)
                assert excinfo.value.status_code == 409

        with harness[1]() as supersede_session:
            from ace_service.models import AilocalsJob

            row = supersede_session.get(AilocalsJob, row_id)
            row.cancel_requested = False
            row.state = "failed"
            supersede_session.commit()
        with harness[1]() as verify_session:
            for capability in (
                verify_session.query(TransferCapability)
                .filter(TransferCapability.ailocals_job_id == row_id)
                .all()
            ):
                with pytest.raises(HTTPException):
                    _ensure_ailocals_authority(verify_session, capability)

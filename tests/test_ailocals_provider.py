"""Tests for the durable AilocalsProvider row adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from ace_service.ailocals.protocol import CAPABILITY_ACE, LeaseRequestData, decode_enroll_request
from ace_service.ailocals.service import AilocalsWorkerService
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import AilocalsJob
from ace_service.providers.ailocals import BACKEND_ID, AilocalsProvider
from ace_service.providers.base import (
    CancelOutcome,
    InferenceMode,
    InferenceRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderJobNotComplete,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    RequestFeature,
)
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


def _stub_builder(job: Any, attempt: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "job_id": job.id,
        "task_type": job.job_type.value,
        "variation_index": attempt.variation_index,
        "generation": {"prompt": "fixture prompt", "lyrics": "", "output_format": "mp3"},
        "resolved_parameters": {"seed": 7},
    }


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


def _prepare(tmp_path: Path) -> tuple[Any, AilocalsWorkerService, Any, str, str]:
    settings = _settings(tmp_path)
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    service = AilocalsWorkerService(factory, settings)
    service.payload_builder = _stub_builder
    with factory() as session:
        job = create_original_job(
            session,
            OriginalSongRequest(description="universal provider fixture"),
            inference_provider="ailocals",
            inference_backend=BACKEND_ID,
        )
        _, attempt, nonce = prepare_variation_submission(
            session,
            job.id,
            1,
            inference_provider="ailocals",
            inference_backend=BACKEND_ID,
        )
        session.commit()
        return factory, service, engine, job.id, nonce


def _enroll_ace(service: AilocalsWorkerService) -> Any:
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
    name, version, capabilities = decode_enroll_request(body)
    outcome = service.enroll(token, name, version, capabilities)
    return service.authenticate(outcome.worker_token)


def test_submit_is_idempotent_across_provider_instances(tmp_path: Path) -> None:
    factory, service, engine, product_job_id, nonce = _prepare(tmp_path)
    try:
        request = _provider_request(product_job_id, nonce)
        first_ref = asyncio.run(AilocalsProvider(service).submit(request))
        restarted_ref = asyncio.run(AilocalsProvider(service).submit(request))
        assert first_ref == restarted_ref
        assert first_ref.provider is ProviderName.AILOCALS
        first_ref.require_backend(BACKEND_ID)

        with factory() as session:
            rows = session.scalars(select(AilocalsJob)).all()
            assert len(rows) == 1
            assert rows[0].state == "queued"
            assert rows[0].queue_deadline_at is not None
            assert rows[0].execution_timeout_ms == 1200000
    finally:
        engine.dispose()


def test_submit_rejects_snapshot_conflict_for_same_nonce(tmp_path: Path) -> None:
    factory, service, engine, product_job_id, nonce = _prepare(tmp_path)
    try:
        request = _provider_request(product_job_id, nonce)
        asyncio.run(AilocalsProvider(service).submit(request))
        conflicting_payload = dict(request.worker_payload)
        conflicting_payload["generation"] = {
            "prompt": "changed prompt",
            "lyrics": "",
            "output_format": "mp3",
        }
        conflicting = InferenceRequest(
            application_job_id=product_job_id,
            variation_index=1,
            submission_nonce=nonce,
            mode=InferenceMode.PROMPT_TO_AUDIO,
            requested_features=frozenset({RequestFeature.PROMPT}),
            worker_payload=conflicting_payload,
            execution_timeout_ms=1200000,
            queue_timeout_ms=7200000,
        )
        with pytest.raises(ProviderError) as excinfo:
            asyncio.run(AilocalsProvider(service).submit(conflicting))
        assert excinfo.value.kind is ProviderErrorKind.REJECTED
    finally:
        engine.dispose()


def test_status_result_and_cancel_read_durable_rows(tmp_path: Path) -> None:
    factory, service, engine, product_job_id, nonce = _prepare(tmp_path)
    try:
        provider = AilocalsProvider(service)
        worker = _enroll_ace(service)
        ref = asyncio.run(provider.submit(_provider_request(product_job_id, nonce)))

        status = asyncio.run(provider.status(ref))
        assert status.phase is ProviderPhase.QUEUED

        lease = service.claim(
            worker, LeaseRequestData(capability_id=CAPABILITY_ACE, wait_seconds=0)
        )
        assert lease is not None
        status = asyncio.run(provider.status(ref))
        assert status.phase is ProviderPhase.PROVISIONING
        with pytest.raises(ProviderJobNotComplete):
            asyncio.run(provider.result(ref))

        outcome = asyncio.run(provider.cancel(ref))
        assert outcome is CancelOutcome.TOO_LATE
        with factory() as session:
            row = session.get(AilocalsJob, ref.external_id)
            assert row is not None and row.cancel_requested is True

        foreign = ProviderJobRef(ProviderName.NODE, ref.external_id, BACKEND_ID)
        with pytest.raises(ValueError):
            asyncio.run(provider.status(foreign))
    finally:
        engine.dispose()

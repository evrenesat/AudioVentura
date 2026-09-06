"""Durable universal-worker provider for the shared ailocals Mac client."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import func, select, update

from ace_service.ailocals.protocol import canonical_request_identity, utc_now
from ace_service.models import AilocalsJob
from ace_service.providers.base import (
    BackendId,
    CancelOutcome,
    InferenceMode,
    InferenceRequest,
    InferenceResult,
    ProviderArtifact,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderJobNotComplete,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    ProviderStatus,
    RequestFeature,
)

BACKEND_ID = BackendId("ailocals/ace-step-v15-xl-turbo")
TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
_STATE_PHASES = {
    "queued": ProviderPhase.QUEUED,
    "leased": ProviderPhase.PROVISIONING,
    "running": ProviderPhase.RUNNING,
    "succeeded": ProviderPhase.SUCCEEDED,
    "failed": ProviderPhase.FAILED,
    "canceled": ProviderPhase.CANCELLED,
}


class AilocalsProvider:
    """Read and write durable ailocals rows; never touch the network."""

    capabilities = ProviderCapabilities(
        name=ProviderName.AILOCALS,
        modes=frozenset({InferenceMode.PROMPT_TO_AUDIO, InferenceMode.AUDIO_TO_AUDIO}),
        request_features=frozenset(RequestFeature),
        accepts_worker_schema=frozenset({2}),
        supports_pending_cancel=True,
        supports_running_cancel=True,
        not_found_after_deadline_is_terminal=False,
        backend_id=BACKEND_ID,
        native_formats=frozenset({"mp3", "flac", "wav"}),
        adapter="ailocals",
        result_delivery="worker_upload",
        enforces_requested_duration=True,
        source_duration_min_seconds=1.0,
        source_duration_max_seconds=600.0,
        output_duration_min_seconds=1.0,
        output_duration_max_seconds=600.0,
    )

    def __init__(self, service: Any) -> None:
        self._service = service

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        identity = canonical_request_identity(dict(request.worker_payload))
        with self._service.session_factory() as session, session.begin():
            existing = session.scalar(
                select(AilocalsJob).where(
                    AilocalsJob.application_job_id == request.application_job_id,
                    AilocalsJob.variation_index == request.variation_index,
                    AilocalsJob.submission_nonce == request.submission_nonce,
                )
            )
            if existing is not None:
                self._require_same_snapshot(existing, identity)
                external_id = existing.id
            else:
                row = AilocalsJob(
                    id=str(uuid.uuid4()),
                    application_job_id=request.application_job_id,
                    variation_index=request.variation_index,
                    submission_nonce=request.submission_nonce,
                    backend_id=str(BACKEND_ID),
                    request_identity_sha256=identity,
                    state="queued",
                    attempt=0,
                    queue_deadline_at=utc_now() + timedelta(milliseconds=request.queue_timeout_ms),
                    execution_timeout_ms=request.execution_timeout_ms,
                )
                session.add(row)
                session.flush()
                external_id = row.id
        return ProviderJobRef(ProviderName.AILOCALS, external_id, BACKEND_ID)

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        self._require_ref(ref)
        row = self._row(ref.external_id, "status")
        phase = _STATE_PHASES.get(row.state, ProviderPhase.UNKNOWN)
        return ProviderStatus(phase, provider_state=row.state, provider_reason=row.error_code)

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        self._require_ref(ref)
        row = self._row(ref.external_id, "result")
        if row.state != "succeeded" or row.result_json is None:
            raise ProviderJobNotComplete()
        return InferenceResult(dict(row.result_json))

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        self._require_ref(ref)
        row = self._row(ref.external_id, "cancel")
        if row.state == "queued":
            self._update_active(row.id, {"state": "canceled", "cancel_requested": True})
            return CancelOutcome.CANCELLED
        if row.state in {"leased", "running"}:
            # Cooperative: the Mac observes the flag at its next heartbeat and
            # stops the owned ACE process. Late outputs remain reconcilable.
            self._update_active(row.id, {"cancel_requested": True})
            return CancelOutcome.TOO_LATE
        return CancelOutcome.TOO_LATE

    async def health(self) -> ProviderHealth:
        with self._service.session_factory() as session:
            queued = session.scalar(
                select(func.count()).select_from(AilocalsJob).where(AilocalsJob.state == "queued")
            )
            active = session.scalar(
                select(func.count())
                .select_from(AilocalsJob)
                .where(AilocalsJob.state.in_(("leased", "running")))
            )
        return ProviderHealth(True, "ailocals queue rows healthy", queued, active)

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact:
        self._require_ref(ref)
        return artifact

    # ------------------------------------------------------------------

    def _require_ref(self, ref: ProviderJobRef) -> None:
        ref.require_provider(ProviderName.AILOCALS)
        ref.require_backend(BACKEND_ID)

    @staticmethod
    def _require_same_snapshot(row: AilocalsJob, identity: str) -> None:
        if row.request_identity_sha256 != identity:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "queued universal submission no longer matches the job snapshot",
            )
        if row.state in TERMINAL_STATES:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "universal submission is already terminal",
            )

    def _row(self, external_id: str, operation: str) -> AilocalsJob:
        if not external_id or len(external_id) > 128:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "universal worker job ID is invalid",
            )
        with self._service.session_factory() as session:
            row = cast("AilocalsJob | None", session.get(AilocalsJob, external_id))
            if row is None:
                raise ProviderError(
                    ProviderErrorKind.NOT_FOUND,
                    operation,
                    "universal worker job is not known",
                )
            session.expunge(row)
            return row

    def _update_active(self, row_id: str, values: dict[str, Any]) -> None:
        with self._service.session_factory() as session, session.begin():
            session.execute(
                update(AilocalsJob)
                .where(
                    AilocalsJob.id == row_id,
                    AilocalsJob.state.not_in(tuple(TERMINAL_STATES)),
                )
                .values(**values, updated_at=utc_now())
            )

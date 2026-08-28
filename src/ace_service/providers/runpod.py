"""Runpod transport adapter for the provider-neutral contract."""

from __future__ import annotations

from ace_service.runpod_client import (
    RunpodAPIError,
    RunpodClient,
    RunpodError,
    RunpodResponseError,
    RunpodState,
    parse_worker_counts,
)

from .base import (
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

_FEATURES = frozenset(RequestFeature)
BACKEND_ID = BackendId("runpod/ace-step-v15-xl-turbo")


class RunpodProvider:
    capabilities = ProviderCapabilities(
        ProviderName.RUNPOD,
        frozenset(InferenceMode),
        _FEATURES,
        frozenset({1, 2}),
        True,
        False,
        True,
        BACKEND_ID,
        source_duration_min_seconds=1.0,
        source_duration_max_seconds=600.0,
        output_duration_min_seconds=1.0,
        output_duration_max_seconds=600.0,
    )

    def __init__(self, client: RunpodClient) -> None:
        self.client = client

    @staticmethod
    def _error(exc: Exception, operation: str) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, RunpodAPIError):
            status = exc.status_code
            kind = (
                ProviderErrorKind.NOT_FOUND
                if status == 404
                else (
                    ProviderErrorKind.TRANSIENT
                    if status is None or status in {408, 429} or status >= 500
                    else ProviderErrorKind.REJECTED
                )
            )
            return ProviderError(
                kind, operation, f"Runpod {operation} is unavailable", status_code=status
            )
        if isinstance(exc, RunpodResponseError):
            return ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Runpod {operation} response is invalid",
            )
        if isinstance(exc, RunpodError):
            return ProviderError(
                ProviderErrorKind.TRANSIENT,
                operation,
                f"Runpod {operation} is unavailable",
            )
        raise exc

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        try:
            external_id = await self.client.submit(
                request.worker_payload,
                execution_timeout_ms=request.execution_timeout_ms,
                ttl_ms=request.queue_timeout_ms,
            )
        except Exception as exc:
            raise self._error(exc, "submit") from exc
        return ProviderJobRef(ProviderName.RUNPOD, external_id, BACKEND_ID)

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        ref.require_provider(ProviderName.RUNPOD)
        ref.require_backend(BACKEND_ID)
        try:
            value = await self.client.status(ref.external_id)
        except Exception as exc:
            raise self._error(exc, "status") from exc
        phase = {
            RunpodState.CLOUD_QUEUED: ProviderPhase.QUEUED,
            RunpodState.GENERATING: ProviderPhase.RUNNING,
            RunpodState.COMPLETED: ProviderPhase.SUCCEEDED,
            RunpodState.FAILED: ProviderPhase.CANCELLED
            if value.raw_status == "CANCELLED"
            else ProviderPhase.FAILED,
        }[value.category]
        message = None
        if phase is ProviderPhase.QUEUED:
            message = "Waiting for worker"
            try:
                counts = parse_worker_counts(await self.client.health())
                if counts.initializing:
                    phase, message = ProviderPhase.STARTING, "Initializing ACE-Step"
            except Exception:
                pass
        progress_phase = str(value.progress["phase"]) if value.progress is not None else None
        return ProviderStatus(
            phase,
            message=message,
            provider_state=value.raw_status,
            provider_reason=progress_phase,
        )

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        ref.require_provider(ProviderName.RUNPOD)
        ref.require_backend(BACKEND_ID)
        try:
            value = await self.client.status(ref.external_id)
        except Exception as exc:
            raise self._error(exc, "result") from exc
        if value.category is not RunpodState.COMPLETED:
            raise ProviderJobNotComplete()
        metadata = dict(value.result or {})
        if value.delay_ms is not None:
            metadata["runpod_queue_delay_ms"] = value.delay_ms
        if value.execution_ms is not None:
            metadata["runpod_execution_ms"] = value.execution_ms
        return InferenceResult(metadata)

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        ref.require_provider(ProviderName.RUNPOD)
        ref.require_backend(BACKEND_ID)
        try:
            value = await self.client.cancel(ref.external_id)
        except Exception as exc:
            raise self._error(exc, "cancel") from exc
        return (
            CancelOutcome.CANCELLED if value.raw_status == "CANCELLED" else CancelOutcome.TOO_LATE
        )

    async def health(self) -> ProviderHealth:
        try:
            counts = parse_worker_counts(await self.client.health())
        except Exception as exc:
            raise self._error(exc, "health") from exc
        return ProviderHealth(True, "Runpod available", counts.queued, counts.active)

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact:
        ref.require_provider(ProviderName.RUNPOD)
        ref.require_backend(BACKEND_ID)
        return artifact

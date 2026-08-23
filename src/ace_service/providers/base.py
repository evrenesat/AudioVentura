"""Provider-neutral inference contracts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class ProviderName(StrEnum):
    RUNPOD = "runpod"
    SALAD = "salad"


class InferenceMode(StrEnum):
    PROMPT_TO_AUDIO = "prompt_to_audio"
    AUDIO_TO_AUDIO = "audio_to_audio"


class RequestFeature(StrEnum):
    PROMPT = "prompt"
    LYRICS = "lyrics"
    SOURCE_AUDIO = "source_audio"
    CUSTOM_DURATION = "custom_duration"
    BPM = "bpm"
    KEY = "key"
    TIME_SIGNATURE = "time_signature"
    LANGUAGE = "language"
    INSTRUMENTAL = "instrumental"
    COVER_STRENGTH = "cover_strength"
    PROMPT_MODE = "prompt_mode"


class ProviderPhase(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"
    TOO_LATE = "too_late"
    UNSUPPORTED = "unsupported"


class DetailScope(StrEnum):
    JOB = "job"
    DEPLOYMENT = "deployment"


class ProviderErrorKind(StrEnum):
    TRANSIENT = "transient"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    name: ProviderName
    modes: frozenset[InferenceMode]
    request_features: frozenset[RequestFeature]
    accepts_worker_schema: frozenset[int]
    supports_pending_cancel: bool
    supports_running_cancel: bool
    not_found_after_deadline_is_terminal: bool


def _bounded_mapping(value: Mapping[str, Any], *, label: str) -> Mapping[str, Any]:
    copied = deepcopy(dict(value))
    if len(copied) > 128:
        raise ValueError(f"{label} has too many fields")
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    application_job_id: str
    variation_index: int
    submission_nonce: str
    mode: InferenceMode
    requested_features: frozenset[RequestFeature]
    worker_payload: Mapping[str, Any]
    execution_timeout_ms: int
    queue_timeout_ms: int

    def __post_init__(self) -> None:
        if not self.application_job_id or len(self.application_job_id) > 128:
            raise ValueError("application job ID is invalid")
        if self.variation_index < 1 or not self.submission_nonce:
            raise ValueError("inference request identity is invalid")
        if self.execution_timeout_ms <= 0 or self.queue_timeout_ms <= 0:
            raise ValueError("inference request timeouts must be positive")
        object.__setattr__(
            self, "worker_payload", _bounded_mapping(self.worker_payload, label="payload")
        )


@dataclass(frozen=True, slots=True)
class ProviderJobRef:
    provider: ProviderName
    external_id: str

    def __post_init__(self) -> None:
        if not self.external_id or len(self.external_id) > 128:
            raise ValueError("provider job ID is invalid")
        if any(ord(char) < 33 or ord(char) > 126 for char in self.external_id):
            raise ValueError("provider job ID is invalid")

    def require_provider(self, provider: ProviderName) -> None:
        if self.provider is not provider:
            raise ValueError("provider job reference does not match provider")


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    phase: ProviderPhase
    message: str | None = None
    progress: float | None = None
    provider_state: str | None = None
    provider_reason: str | None = None
    detail_scope: DetailScope = DetailScope.JOB

    def __post_init__(self) -> None:
        if self.progress is not None and not 0.0 <= self.progress <= 1.0:
            raise ValueError("provider progress is out of bounds")
        for value in (self.message, self.provider_state, self.provider_reason):
            if value is not None and len(value) > 4096:
                raise ValueError("provider status text is too large")


@dataclass(frozen=True, slots=True)
class InferenceResult:
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _bounded_mapping(self.metadata, label="result"))


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    ok: bool
    message: str
    queued_jobs: int | None = None
    running_instances: int | None = None


class ProviderError(RuntimeError):
    """Bounded provider failure safe for logs and durable classification."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        operation: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.operation = operation[:32]
        self.status_code = status_code
        self.safe_message = message[:512]
        super().__init__(self.safe_message)


class ProviderJobNotComplete(ProviderError):
    def __init__(self, operation: str = "result") -> None:
        super().__init__(ProviderErrorKind.REJECTED, operation, "provider job is not complete")


class InferenceProvider(Protocol):
    capabilities: ProviderCapabilities

    async def submit(self, request: InferenceRequest) -> ProviderJobRef: ...
    async def status(self, ref: ProviderJobRef) -> ProviderStatus: ...
    async def result(self, ref: ProviderJobRef) -> InferenceResult: ...
    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome: ...
    async def health(self) -> ProviderHealth: ...


def unsupported_features(
    capabilities: ProviderCapabilities, request: InferenceRequest
) -> frozenset[RequestFeature]:
    unsupported = request.requested_features - capabilities.request_features
    if request.mode not in capabilities.modes:
        return frozenset(request.requested_features)
    return frozenset(unsupported)

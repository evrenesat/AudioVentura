"""Provider-neutral inference contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class ProviderName(StrEnum):
    RUNPOD = "runpod"
    SALAD = "salad"
    FAL = "fal"
    MOCK = "mock"
    NODE = "node"
    AILOCALS = "ailocals"


class BackendId(str):
    """Controller-owned backend identity, safe to persist and log."""

    MAX_LENGTH = 256

    def __new__(cls, value: str) -> BackendId:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > cls.MAX_LENGTH:
            raise ValueError("backend ID must be a non-empty string of at most 256 bytes")
        if any(ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("backend ID must contain printable ASCII only")
        return str.__new__(cls, value)


class BackendOperation(StrEnum):
    TEXT_TO_MUSIC = "text_to_music"
    AUDIO_TRANSFORM = "audio_transform"
    AUDIO_INPAINT = "audio_inpaint"
    AUDIO_OUTPAINT = "audio_outpaint"


class MediaKind(StrEnum):
    MUSIC = "music"
    MUSIC_AND_SFX = "music_and_sfx"
    SFX = "sfx"
    SPEECH = "speech"
    UTILITY = "utility"


class ResultDeliveryMode(StrEnum):
    WORKER_UPLOAD = "worker_upload"
    CONTROLLER_PULL = "controller_pull"


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
    NEGATIVE_PROMPT = "negative_prompt"
    GUIDANCE_SCALE = "guidance_scale"
    INFERENCE_STEPS = "inference_steps"
    PROMPT_EXPANSION = "prompt_expansion"
    SOURCE_STYLE = "source_style"
    INPAINT_REGION = "inpaint_region"
    OUTPAINT_EXTENSION = "outpaint_extension"


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
    backend_id: BackendId | str | None = None
    operation: BackendOperation | str | None = None
    media_kind: MediaKind | str = MediaKind.MUSIC
    result_delivery: ResultDeliveryMode | str = ResultDeliveryMode.WORKER_UPLOAD
    native_formats: frozenset[str] = frozenset({"mp3", "flac", "wav"})
    adapter: str | None = None
    enforces_requested_duration: bool = True
    source_duration_min_seconds: float | None = None
    source_duration_max_seconds: float | None = None
    output_duration_min_seconds: float | None = None
    output_duration_max_seconds: float | None = None

    def __post_init__(self) -> None:
        backend_id = self.backend_id
        if backend_id is None:
            backend_id = BackendId(f"{self.name.value}/default")
        object.__setattr__(self, "backend_id", BackendId(str(backend_id)))
        if self.operation is not None:
            object.__setattr__(self, "operation", BackendOperation(self.operation))
        object.__setattr__(self, "media_kind", MediaKind(self.media_kind))
        object.__setattr__(self, "result_delivery", ResultDeliveryMode(self.result_delivery))
        object.__setattr__(self, "native_formats", frozenset(self.native_formats))
        for label, value in (
            ("source_duration_min_seconds", self.source_duration_min_seconds),
            ("source_duration_max_seconds", self.source_duration_max_seconds),
            ("output_duration_min_seconds", self.output_duration_min_seconds),
            ("output_duration_max_seconds", self.output_duration_max_seconds),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{label} must be a finite positive number")
        for minimum, maximum, label in (
            (
                self.source_duration_min_seconds,
                self.source_duration_max_seconds,
                "source duration",
            ),
            (
                self.output_duration_min_seconds,
                self.output_duration_max_seconds,
                "output duration",
            ),
        ):
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"{label} minimum exceeds maximum")


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
    generation_request: GenerationRequest | None = None
    signed_source: Mapping[str, Any] | None = None

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
        if self.generation_request is not None and not isinstance(
            self.generation_request, GenerationRequest
        ):
            raise ValueError("generation request has an invalid type")
        if self.signed_source is not None:
            object.__setattr__(
                self, "signed_source", _bounded_mapping(self.signed_source, label="source")
            )


@dataclass(frozen=True, slots=True)
class ProviderJobRef:
    provider: ProviderName
    external_id: str
    backend_id: BackendId | str | None = None

    def __post_init__(self) -> None:
        if not self.external_id or len(self.external_id) > 128:
            raise ValueError("provider job ID is invalid")
        if any(ord(char) < 33 or ord(char) > 126 for char in self.external_id):
            raise ValueError("provider job ID is invalid")
        if self.backend_id is None:
            builtins = {
                ProviderName.RUNPOD: "runpod/ace-step-v15-xl-turbo",
                ProviderName.SALAD: "salad/ace-step-v15-xl-turbo",
                ProviderName.MOCK: "mock/midi-sequential",
                ProviderName.NODE: "node/ace-step-v15-xl-turbo",
                ProviderName.AILOCALS: "ailocals/ace-step-v15-xl-turbo",
            }
            object.__setattr__(
                self,
                "backend_id",
                BackendId(builtins.get(self.provider, f"{self.provider.value}/default")),
            )
        else:
            object.__setattr__(self, "backend_id", BackendId(str(self.backend_id)))

    def require_provider(self, provider: ProviderName) -> None:
        if self.provider is not provider:
            raise ValueError("provider job reference does not match provider")

    def require_backend(self, backend_id: BackendId | str) -> None:
        if self.backend_id != BackendId(str(backend_id)):
            raise ValueError("provider job reference does not match backend")


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Provider-neutral product request persisted independently of worker schema."""

    request_contract_version: int = 1
    mode: InferenceMode = InferenceMode.PROMPT_TO_AUDIO
    prompt: str | None = None
    lyrics: str | None = None
    instrumental: bool = False
    duration_seconds: float | None = None
    seed: int | None = None
    variation_count: int = 1
    output_format: str = "mp3"
    source_audio_url: str | None = None
    source_duration_seconds: float | None = None
    fields: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.request_contract_version != 1:
            raise ValueError("unsupported generation request contract")
        if self.variation_count < 1 or self.variation_count > 4:
            raise ValueError("variation count must be between 1 and 4")
        if self.output_format not in {"mp3", "flac", "wav"}:
            raise ValueError("unsupported output format")
        for value, label in ((self.prompt, "prompt"), (self.lyrics, "lyrics")):
            if value is not None and len(value) > 20_000:
                raise ValueError(f"{label} is too large")
        object.__setattr__(self, "mode", InferenceMode(self.mode))
        object.__setattr__(self, "fields", _bounded_mapping(self.fields, label="generation fields"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_contract_version": self.request_contract_version,
            "mode": self.mode.value,
            "prompt": self.prompt,
            "lyrics": self.lyrics,
            "instrumental": self.instrumental,
            "duration_seconds": self.duration_seconds,
            "seed": self.seed,
            "variation_count": self.variation_count,
            "output_format": self.output_format,
            "source_audio_url": self.source_audio_url,
            "source_duration_seconds": self.source_duration_seconds,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    """Bounded evidence for a result that the controller materialized locally."""

    url: str
    native_format: str
    content_type: str
    byte_size: int | None = None
    sha256: str | None = None
    seed: int | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.url or len(self.url) > 2048 or not self.url.startswith("https://"):
            raise ValueError("provider artifact URL is invalid")
        if self.native_format not in {"mp3", "flac", "wav"}:
            raise ValueError("provider artifact format is unsupported")
        if self.byte_size is not None and (self.byte_size <= 0 or self.byte_size > 1_073_741_824):
            raise ValueError("provider artifact size is invalid")
        if self.sha256 is not None and (
            len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("provider artifact checksum is invalid")


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
    artifact: ProviderArtifact | None = None

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

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact: ...


def unsupported_features(
    capabilities: ProviderCapabilities, request: InferenceRequest
) -> frozenset[RequestFeature]:
    unsupported = request.requested_features - capabilities.request_features
    if request.mode not in capabilities.modes:
        return frozenset(request.requested_features)
    if capabilities.operation is not None:
        expected_mode = (
            InferenceMode.PROMPT_TO_AUDIO
            if capabilities.operation is BackendOperation.TEXT_TO_MUSIC
            else InferenceMode.AUDIO_TO_AUDIO
        )
        if request.mode is not expected_mode:
            return frozenset(request.requested_features)
    return frozenset(unsupported)

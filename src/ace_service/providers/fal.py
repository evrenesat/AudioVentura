"""Raw Fal Model API queue transport and reviewed endpoint adapters."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import (
    BackendOperation,
    CancelOutcome,
    GenerationRequest,
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
    ResultDeliveryMode,
)
from .fal_catalog import CatalogDescriptor

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9._~/-]{1,256}$")
_CDN_HOSTS = frozenset({"storage.googleapis.com", "fal.media", "cdn.fal.ai"})
_SAFE_RESULT_FORMATS = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}


def _safe_request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise ProviderError(
            ProviderErrorKind.INVALID_RESPONSE, "response", "Fal request ID is invalid"
        )
    return value


def _safe_endpoint_id(value: str) -> str:
    if not _ENDPOINT_ID_RE.fullmatch(value) or ".." in value or value.startswith("/"):
        raise ValueError("Fal endpoint ID is invalid")
    return value


def _is_allowed_result_host(hostname: str | None) -> bool:
    """Accept reviewed Fal CDN hosts, including Fal's versioned media subdomains."""

    if hostname is None:
        return False
    host = hostname.rstrip(".").lower()
    return host in _CDN_HOSTS or host.endswith(".fal.media")


class FalQueueTransport:
    """One authenticated queue client shared by endpoint adapters."""

    def __init__(
        self,
        api_key: str,
        *,
        output_retention_seconds: int = 86_400,
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Fal API key must not be empty")
        if output_retention_seconds <= 0:
            raise ValueError("Fal output retention must be positive")
        self.api_key = api_key.strip()
        self.output_retention_seconds = output_retention_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
            )
        )
        self._token_client = self._client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Fal-Store-IO": "0",
            "x-app-fal-disable-fallback": "true",
            "X-Fal-Object-Lifecycle-Preference": json.dumps(
                {
                    "expiration_duration_seconds": self.output_retention_seconds,
                    "initial_acl": {"default": "forbid", "rules": []},
                },
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _url(endpoint_id: str, suffix: str = "") -> str:
        endpoint = _safe_endpoint_id(endpoint_id)
        return f"https://queue.fal.run/{endpoint}{suffix}"

    async def _request(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        expected: frozenset[int] = frozenset({200}),
    ) -> tuple[int, Any]:
        try:
            response = await self._client.request(
                method,
                url,
                headers=self._headers(),
                json=dict(json_body) if json_body is not None else None,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT, operation, f"Fal {operation} failed"
            ) from exc
        if response.status_code not in expected:
            status = response.status_code
            kind = (
                ProviderErrorKind.NOT_FOUND
                if status == 404
                else ProviderErrorKind.TRANSIENT
                if status in {408, 409, 429} or status >= 500
                else ProviderErrorKind.REJECTED
            )
            raise ProviderError(
                kind, operation, f"Fal {operation} request was rejected", status_code=status
            )
        if len(response.content) > 1_048_576:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Fal {operation} response is too large",
            )
        if not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Fal {operation} response is invalid",
            ) from exc

    async def submit(self, endpoint_id: str, payload: Mapping[str, Any]) -> str:
        _status, body = await self._request(
            "POST",
            self._url(endpoint_id),
            "submit",
            json_body=payload,
            expected=frozenset({200, 201, 202}),
        )
        if not isinstance(body, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "submit", "Fal submit response is invalid"
            )
        return _safe_request_id(body.get("request_id", body.get("requestId", body.get("id"))))

    async def status(self, endpoint_id: str, request_id: str) -> Mapping[str, Any]:
        request = _safe_request_id(request_id)
        _status, body = await self._request(
            "GET", self._url(endpoint_id, f"/requests/{request}/status"), "status"
        )
        if not isinstance(body, Mapping) or not isinstance(body.get("status"), str):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "status", "Fal status response is invalid"
            )
        return dict(body)

    async def result(self, endpoint_id: str, request_id: str) -> Mapping[str, Any]:
        request = _safe_request_id(request_id)
        _status, body = await self._request(
            "GET", self._url(endpoint_id, f"/requests/{request}"), "result"
        )
        if not isinstance(body, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "result", "Fal result response is invalid"
            )
        return dict(body)

    async def cancel(self, endpoint_id: str, request_id: str) -> CancelOutcome:
        request = _safe_request_id(request_id)
        try:
            status, _body = await self._request(
                "PUT",
                self._url(endpoint_id, f"/requests/{request}/cancel"),
                "cancel",
                expected=frozenset({200, 202}),
            )
            return CancelOutcome.CANCELLED if status == 200 else CancelOutcome.TOO_LATE
        except ProviderError as exc:
            if exc.status_code == 400:
                return CancelOutcome.TOO_LATE
            if exc.status_code == 404:
                return CancelOutcome.TOO_LATE
            raise

    async def health(self, endpoint_id: str) -> ProviderHealth:
        try:
            response = await self._client.get(
                "https://api.fal.ai/v1/models",
                params={"endpoint_id": endpoint_id, "status": "active", "limit": 1},
                headers={"Authorization": f"Key {self.api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT, "health", "Fal health check failed"
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT
                if response.status_code >= 500
                else ProviderErrorKind.REJECTED,
                "health",
                "Fal endpoint health check failed",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "health", "Fal health response is invalid"
            ) from exc
        models = body.get("models") if isinstance(body, Mapping) else None
        active = isinstance(models, list) and any(
            isinstance(item, Mapping) and item.get("endpoint_id") == endpoint_id for item in models
        )
        return ProviderHealth(
            active, "Fal endpoint active" if active else "Fal endpoint is not active"
        )

    async def cdn_token(self) -> str:
        try:
            response = await self._client.get(
                "https://rest.fal.ai/storage/auth/token",
                params={"storage_type": "fal-cdn-v3"},
                headers={"Authorization": f"Key {self.api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT, "cdn_token", "Fal CDN token request failed"
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "cdn_token",
                "Fal CDN token request failed",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "cdn_token", "Fal CDN token response is invalid"
            ) from exc
        token = body.get("token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "cdn_token", "Fal CDN token response is invalid"
            )
        return token


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[(\d+)\])?", part)
        if match is None or not isinstance(current, Mapping):
            return None
        current = current.get(match.group(1))
        if match.group(2) is not None:
            if not isinstance(current, list):
                return None
            index = int(match.group(2))
            if index >= len(current):
                return None
            current = current[index]
    return current


def _request_from_payload(request: InferenceRequest) -> GenerationRequest:
    if request.generation_request is not None:
        return request.generation_request
    payload = request.worker_payload
    source_value = payload.get("input")
    source: Mapping[str, Any] = source_value if isinstance(source_value, Mapping) else payload
    return GenerationRequest(
        mode=request.mode,
        prompt=source.get("prompt")
        if isinstance(source.get("prompt"), str)
        else source.get("caption"),
        lyrics=source.get("lyrics") if isinstance(source.get("lyrics"), str) else None,
        instrumental=bool(source.get("instrumental", False)),
        duration_seconds=source.get("duration")
        if isinstance(source.get("duration"), (int, float))
        else None,
        seed=source.get("seed") if isinstance(source.get("seed"), int) else None,
        variation_count=request.variation_index,
        output_format=source.get("output_format", "mp3")
        if isinstance(source.get("output_format", "mp3"), str)
        else "mp3",
        fields={
            key: value
            for key, value in source.items()
            if key
            not in {
                "prompt",
                "caption",
                "lyrics",
                "instrumental",
                "duration",
                "seed",
                "output_format",
            }
        },
    )


def build_fal_payload(
    descriptor: CatalogDescriptor,
    request: InferenceRequest,
) -> dict[str, Any]:
    """Map the finite product request to one reviewed endpoint contract."""

    generation = _request_from_payload(request)
    values: dict[str, Any] = dict(generation.fields)
    values.setdefault("prompt", generation.prompt)
    values.setdefault("lyrics", generation.lyrics)
    values.setdefault("duration", generation.duration_seconds)
    values.setdefault("seed", generation.seed)
    values.setdefault("instrumental", generation.instrumental)
    if request.signed_source:
        values.setdefault("source_audio", request.signed_source.get("audio_url"))
    payload: dict[str, Any] = {"sync_mode": False}
    for name, policy in descriptor.fields.items():
        value = values.get(name)
        if name == "instrumental" and value and policy.fal_name == "lyrics":
            value = "[inst]"
        if policy.required and (value is None or (isinstance(value, str) and not value.strip())):
            raise ValueError(f"{policy.ui_name} is required for the selected backend")
        if value is None:
            continue
        if policy.type == "number" and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError(f"{policy.ui_name} must be numeric")
        if policy.type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{policy.ui_name} must be an integer")
        if policy.type in {"string", "url"} and not isinstance(value, str):
            raise ValueError(f"{policy.ui_name} must be text")
        if policy.type == "url":
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or value.startswith("data:"):
                raise ValueError(f"{policy.ui_name} must be an HTTPS URL")
        if policy.minimum is not None and value < policy.minimum:
            raise ValueError(f"{policy.ui_name} is below its minimum")
        if policy.maximum is not None and value > policy.maximum:
            raise ValueError(f"{policy.ui_name} is above its maximum")
        payload[policy.fal_name] = value
    for value in payload.values():
        if (
            isinstance(value, (bytes, bytearray))
            or isinstance(value, Mapping)
            and any(isinstance(child, (bytes, bytearray)) for child in value.values())
        ):
            raise ValueError("Fal request must not contain audio bytes")
    return payload


class FalProvider:
    """One lightweight adapter for one immutable reviewed catalog descriptor."""

    def __init__(self, descriptor: CatalogDescriptor, transport: FalQueueTransport) -> None:
        self.descriptor = descriptor
        self.transport = transport
        mode = (
            InferenceMode.PROMPT_TO_AUDIO
            if descriptor.operation is BackendOperation.TEXT_TO_MUSIC
            else InferenceMode.AUDIO_TO_AUDIO
        )
        feature_names = set(RequestFeature)
        feature_names = {
            RequestFeature(name)
            for name in descriptor.fields
            if name in {item.value for item in RequestFeature}
        }
        self.capabilities = ProviderCapabilities(
            ProviderName.FAL,
            frozenset({mode}),
            frozenset(feature_names),
            frozenset(),
            True,
            True,
            True,
            descriptor.backend_id,
            descriptor.operation,
            descriptor.media_kind,
            ResultDeliveryMode.CONTROLLER_PULL,
            frozenset(descriptor.output.native_formats),
            descriptor.adapter,
        )

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        payload = build_fal_payload(self.descriptor, request)
        request_id = await self.transport.submit(self.descriptor.endpoint_id, payload)
        return ProviderJobRef(ProviderName.FAL, request_id, self.descriptor.backend_id)

    def _require_ref(self, ref: ProviderJobRef) -> None:
        ref.require_provider(ProviderName.FAL)
        ref.require_backend(self.descriptor.backend_id)

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        self._require_ref(ref)
        body = await self.transport.status(self.descriptor.endpoint_id, ref.external_id)
        state = body.get("status")
        if state == "IN_QUEUE":
            return ProviderStatus(ProviderPhase.QUEUED, "Waiting for Fal", provider_state=state)
        if state == "IN_PROGRESS":
            progress = body.get("progress")
            progress_value = float(progress) if isinstance(progress, (int, float)) else None
            return ProviderStatus(
                ProviderPhase.RUNNING, "Fal model is generating", progress_value, state
            )
        if state == "COMPLETED":
            if body.get("error") or body.get("error_type"):
                return ProviderStatus(
                    ProviderPhase.FAILED, "Fal model did not complete", provider_state=state
                )
            return ProviderStatus(ProviderPhase.SUCCEEDED, provider_state=state)
        raise ProviderError(
            ProviderErrorKind.INVALID_RESPONSE, "status", "Fal returned an unknown state"
        )

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        self._require_ref(ref)
        status = await self.status(ref)
        if status.phase is not ProviderPhase.SUCCEEDED:
            if status.phase is ProviderPhase.FAILED:
                raise ProviderError(ProviderErrorKind.REJECTED, "result", "Fal model failed")
            raise ProviderJobNotComplete()
        body = await self.transport.result(self.descriptor.endpoint_id, ref.external_id)
        value = _path_value(body, self.descriptor.output.result_path)
        url = value.get("url") if isinstance(value, Mapping) else value
        if not isinstance(url, str) or url.startswith("data:"):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "result", "Fal result audio URL is invalid"
            )
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not _is_allowed_result_host(parsed.hostname):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "result", "Fal result URL host is not allowed"
            )
        suffix = parsed.path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
        native_format = (
            suffix
            if suffix in self.descriptor.output.native_formats
            else self.descriptor.output.native_formats[0]
        )
        if suffix in {"mp3", "flac", "wav"} and suffix not in self.descriptor.output.native_formats:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "Fal result format is not allowed by the catalog",
            )
        raw_seed = (
            _path_value(body, self.descriptor.output.seed_path)
            if self.descriptor.output.seed_path
            else None
        )
        if raw_seed is not None and (isinstance(raw_seed, bool) or not isinstance(raw_seed, int)):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "Fal result seed metadata is invalid",
            )
        raw_duration = (
            _path_value(body, self.descriptor.output.duration_path)
            if self.descriptor.output.duration_path
            else None
        )
        if raw_duration is not None and (
            isinstance(raw_duration, bool)
            or not isinstance(raw_duration, (int, float))
            or not math.isfinite(float(raw_duration))
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "Fal result duration metadata is invalid",
            )
        artifact = ProviderArtifact(
            url=url,
            native_format=native_format,
            content_type=_SAFE_RESULT_FORMATS[native_format],
            seed=raw_seed,
            duration_seconds=float(raw_duration) if raw_duration is not None else None,
        )
        metadata: dict[str, Any] = {
            "fal_request_id": ref.external_id,
            "backend_id": str(self.descriptor.backend_id),
            "endpoint_id": self.descriptor.endpoint_id,
            "catalog_revision": self.descriptor.catalog_revision,
            "native_format": native_format,
        }
        return InferenceResult(metadata, artifact=artifact)

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        self._require_ref(ref)
        return await self.transport.cancel(self.descriptor.endpoint_id, ref.external_id)

    async def health(self) -> ProviderHealth:
        return await self.transport.health(self.descriptor.endpoint_id)

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact:
        self._require_ref(ref)
        return artifact

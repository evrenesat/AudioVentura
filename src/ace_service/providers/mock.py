"""Private bearer-authenticated adapter for the sequential MIDI backend."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx

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

BACKEND_ID = BackendId("mock/midi-sequential")
_MAX_BODY = 65_536
_FEATURES = frozenset(RequestFeature)
_PLACEHOLDERS = frozenset({"change-me", "changeme", "replace-me", "replace_me"})


class MockProvider:
    """Map the controller provider contract to the private mock HTTP API."""

    capabilities = ProviderCapabilities(
        name=ProviderName.MOCK,
        modes=frozenset(InferenceMode),
        request_features=_FEATURES,
        accepts_worker_schema=frozenset({2}),
        supports_pending_cancel=True,
        supports_running_cancel=True,
        not_found_after_deadline_is_terminal=False,
        backend_id=BACKEND_ID,
        native_formats=frozenset({"mp3"}),
        adapter="mock",
        enforces_requested_duration=False,
        source_duration_min_seconds=1.0,
        source_duration_max_seconds=600.0,
        output_duration_min_seconds=1.0,
        output_duration_max_seconds=600.0,
    )

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token.strip() or token.strip().lower() in _PLACEHOLDERS:
            raise ValueError("mock bearer token is missing or still a placeholder")
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("mock base URL must be a private HTTP(S) endpoint")
        if parsed.userinfo or parsed.query or parsed.fragment:
            raise ValueError("mock base URL must not contain credentials, query, or fragment")
        hostname = parsed.host.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if hostname != "localhost" and not hostname.endswith(".ts.net"):
                raise ValueError("mock base URL must resolve to a private p100 endpoint") from None
        else:
            if address.is_global:
                raise ValueError("mock base URL must resolve to a private p100 endpoint")
        self.base_url = str(parsed).rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
            ),
            follow_redirects=False,
            trust_env=False,
        )
        if http_client is not None:
            http_client.headers.update(
                {"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"}
            )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _external_id(value: Any, operation: str) -> str:
        try:
            parsed = UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, operation, "mock job ID is invalid"
            ) from exc
        return str(parsed)

    @staticmethod
    def _mapping(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, operation, "mock response is invalid"
            )
        return dict(value)

    async def _request(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        body: Mapping[str, Any] | None = None,
        expected: frozenset[int] = frozenset({200}),
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=dict(body) if body is not None else None,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT, operation, f"mock {operation} request failed"
            ) from exc
        if response.status_code not in expected:
            kind = (
                ProviderErrorKind.NOT_FOUND
                if response.status_code == 404
                else ProviderErrorKind.TRANSIENT
                if response.status_code in {408, 409, 429} or response.status_code >= 500
                else ProviderErrorKind.REJECTED
            )
            raise ProviderError(
                kind,
                operation,
                f"mock {operation} request failed",
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_BODY:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, operation, "mock response is too large"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, operation, "mock response is not JSON"
            ) from exc

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        payload = dict(request.worker_payload)
        if payload.get("schema_version") != 2:
            raise ProviderError(
                ProviderErrorKind.REJECTED, "submit", "mock accepts only worker schema version 2"
            )
        upload = payload.get("result_upload")
        if not isinstance(upload, Mapping):
            raise ProviderError(
                ProviderErrorKind.REJECTED, "submit", "mock submission is missing result upload"
            )
        body = {
            "schema_version": 2,
            "application_job_id": request.application_job_id,
            "variation_index": request.variation_index,
            "submission_nonce": request.submission_nonce,
            "input": payload,
            "source": payload.get("source"),
            "result_upload": dict(upload),
        }
        raw = await self._request("POST", "v1/jobs", "submit", body=body, expected=frozenset({202}))
        response = self._mapping(raw, "submit")
        external_id = self._external_id(response.get("job_id"), "submit")
        if (
            response.get("application_job_id") != request.application_job_id
            or response.get("variation_index") != request.variation_index
            or response.get("submission_nonce") != request.submission_nonce
            or response.get("status")
            not in {"queued", "running", "succeeded", "failed", "cancelled"}
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "submit",
                "mock submission response does not match the request",
            )
        return ProviderJobRef(ProviderName.MOCK, external_id, BACKEND_ID)

    async def _job(self, ref: ProviderJobRef, operation: str = "status") -> dict[str, Any]:
        ref.require_provider(ProviderName.MOCK)
        ref.require_backend(BACKEND_ID)
        external_id = self._external_id(ref.external_id, operation)
        body = self._mapping(
            await self._request("GET", f"v1/jobs/{external_id}", operation), operation
        )
        if self._external_id(body.get("job_id"), operation) != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, operation, "mock response job ID does not match"
            )
        return body

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        body = await self._job(ref)
        raw_state = body.get("status")
        phase = {
            "queued": ProviderPhase.QUEUED,
            "running": ProviderPhase.RUNNING,
            "succeeded": ProviderPhase.SUCCEEDED,
            "failed": ProviderPhase.FAILED,
            "cancelled": ProviderPhase.CANCELLED,
        }.get(raw_state if isinstance(raw_state, str) else "", ProviderPhase.UNKNOWN)
        return ProviderStatus(
            phase, provider_state=raw_state if isinstance(raw_state, str) else None
        )

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        ref.require_provider(ProviderName.MOCK)
        ref.require_backend(BACKEND_ID)
        external_id = self._external_id(ref.external_id, "result")
        body = self._mapping(
            await self._request("GET", f"v1/jobs/{external_id}/result", "result"), "result"
        )
        if self._external_id(body.get("job_id"), "result") != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "mock result job ID does not match",
            )
        if body.get("status") != "succeeded":
            raise ProviderJobNotComplete()
        metadata = body.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "result", "mock result metadata is invalid"
            )
        return InferenceResult(dict(metadata))

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        ref.require_provider(ProviderName.MOCK)
        ref.require_backend(BACKEND_ID)
        external_id = self._external_id(ref.external_id, "cancel")
        body = await self._request(
            "POST",
            f"v1/jobs/{external_id}/cancel",
            "cancel",
            expected=frozenset({200}),
        )
        response = self._mapping(body, "cancel")
        if self._external_id(response.get("job_id"), "cancel") != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "cancel",
                "mock cancel job ID does not match",
            )
        state = response.get("status")
        if state == "cancelled":
            return CancelOutcome.CANCELLED
        return CancelOutcome.TOO_LATE

    async def health(self) -> ProviderHealth:
        body = self._mapping(await self._request("GET", "healthz", "health"), "health")
        if body.get("status") != "ok":
            return ProviderHealth(False, "Mock MIDI backend is not ready")
        queued = body.get("queue_depth")
        running = body.get("running")
        if (
            isinstance(queued, bool)
            or not isinstance(queued, int)
            or queued < 0
            or not isinstance(running, bool)
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "health",
                "mock health response is malformed",
            )
        return ProviderHealth(
            True,
            "Mock MIDI backend available",
            queued,
            1 if running else 0,
        )

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact:
        ref.require_provider(ProviderName.MOCK)
        ref.require_backend(BACKEND_ID)
        return artifact

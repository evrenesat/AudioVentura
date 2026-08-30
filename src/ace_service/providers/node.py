"""Private HTTP adapter for a persistent ACE Node."""

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

BACKEND_ID = BackendId("node/ace-step-v15-xl-turbo")
MAX_BODY_BYTES = 65_536
_PLACEHOLDERS = frozenset({"change-me", "changeme", "replace-me", "replace_me", "example.invalid"})
_FEATURES = frozenset(RequestFeature)
_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})


class NodeProvider:
    """Translate the provider-neutral contract to the ACE Node API."""

    capabilities = ProviderCapabilities(
        name=ProviderName.NODE,
        modes=frozenset(InferenceMode),
        request_features=_FEATURES,
        accepts_worker_schema=frozenset({2}),
        supports_pending_cancel=True,
        supports_running_cancel=False,
        not_found_after_deadline_is_terminal=False,
        backend_id=BACKEND_ID,
        native_formats=frozenset({"mp3", "flac", "wav"}),
        adapter="node",
        result_delivery="worker_upload",
        enforces_requested_duration=True,
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
        normalized_token = token.strip().lower()
        if (
            not normalized_token
            or normalized_token in _PLACEHOLDERS
            or normalized_token.endswith(".example.invalid")
        ):
            raise ValueError("ACE Node bearer token is missing or still a placeholder")
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.path not in {"", "/"}
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ACE Node base URL must be a private HTTP(S) endpoint")
        hostname = parsed.host.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if hostname != "localhost" and not hostname.endswith(".ts.net"):
                raise ValueError("ACE Node base URL must resolve to a private endpoint") from None
        else:
            if address.is_global:
                raise ValueError("ACE Node base URL must resolve to a private endpoint")
        self.base_url = str(parsed).rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers={"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"},
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
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
    def _uuid(value: Any, operation: str) -> str:
        try:
            parsed = UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node job ID is invalid",
            ) from exc
        return str(parsed)

    @staticmethod
    def _mapping(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node response is invalid",
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
                ProviderErrorKind.TRANSIENT,
                operation,
                f"ACE Node {operation} request failed",
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
                kind,
                operation,
                f"ACE Node {operation} request failed",
                status_code=status,
            )
        if len(response.content) > MAX_BODY_BYTES:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node response is too large",
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node response is not JSON",
            ) from exc

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        payload = dict(request.worker_payload)
        if payload.get("schema_version") != 2:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "ACE Node accepts only worker schema version 2",
            )
        upload = payload.get("result_upload")
        if not isinstance(upload, Mapping):
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "ACE Node submission is missing result upload",
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
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "ACE Node submission metadata is invalid",
            ) from exc
        if len(encoded) > MAX_BODY_BYTES:
            raise ProviderError(
                ProviderErrorKind.REJECTED,
                "submit",
                "ACE Node submission is too large",
            )
        raw = await self._request("POST", "v1/jobs", "submit", body=body, expected=frozenset({202}))
        response = self._mapping(raw, "submit")
        external_id = self._uuid(response.get("job_id"), "submit")
        if (
            response.get("application_job_id") != request.application_job_id
            or response.get("variation_index") != request.variation_index
            or response.get("submission_nonce") != request.submission_nonce
            or response.get("status") not in _STATES
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "submit",
                "ACE Node submission response does not match the request",
            )
        return ProviderJobRef(ProviderName.NODE, external_id, BACKEND_ID)

    async def _job(self, ref: ProviderJobRef, operation: str = "status") -> dict[str, Any]:
        ref.require_provider(ProviderName.NODE)
        ref.require_backend(BACKEND_ID)
        external_id = self._uuid(ref.external_id, operation)
        response = self._mapping(
            await self._request("GET", f"v1/jobs/{external_id}", operation), operation
        )
        if self._uuid(response.get("job_id"), operation) != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node response job ID does not match",
            )
        if response.get("status") not in _STATES:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "ACE Node response state is invalid",
            )
        return response

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        body = await self._job(ref)
        raw_state = body.get("status")
        if not isinstance(raw_state, str):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "status",
                "ACE Node response state is invalid",
            )
        phase = {
            "queued": ProviderPhase.QUEUED,
            "running": ProviderPhase.RUNNING,
            "succeeded": ProviderPhase.SUCCEEDED,
            "failed": ProviderPhase.FAILED,
            "cancelled": ProviderPhase.CANCELLED,
        }[raw_state]
        return ProviderStatus(phase, provider_state=raw_state)

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        ref.require_provider(ProviderName.NODE)
        ref.require_backend(BACKEND_ID)
        external_id = self._uuid(ref.external_id, "result")
        body = self._mapping(
            await self._request("GET", f"v1/jobs/{external_id}/result", "result"), "result"
        )
        if self._uuid(body.get("job_id"), "result") != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "ACE Node result job ID does not match",
            )
        if body.get("status") != "succeeded":
            raise ProviderJobNotComplete()
        metadata = body.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "result",
                "ACE Node result metadata is invalid",
            )
        return InferenceResult(dict(metadata))

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        ref.require_provider(ProviderName.NODE)
        ref.require_backend(BACKEND_ID)
        external_id = self._uuid(ref.external_id, "cancel")
        body = self._mapping(
            await self._request(
                "POST",
                f"v1/jobs/{external_id}/cancel",
                "cancel",
                expected=frozenset({200}),
            ),
            "cancel",
        )
        if self._uuid(body.get("job_id"), "cancel") != external_id:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "cancel",
                "ACE Node cancel job ID does not match",
            )
        if body.get("outcome") == "cancelled" or body.get("status") == "cancelled":
            return CancelOutcome.CANCELLED
        if body.get("outcome") == "too_late" or body.get("status") == "running":
            return CancelOutcome.TOO_LATE
        raise ProviderError(
            ProviderErrorKind.INVALID_RESPONSE,
            "cancel",
            "ACE Node cancel outcome is invalid",
        )

    async def health(self) -> ProviderHealth:
        body = self._mapping(await self._request("GET", "healthz", "health"), "health")
        state = body.get("status")
        if state not in {"initializing", "ready", "failed"}:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "health",
                "ACE Node health state is invalid",
            )
        queued = body.get("queue_depth", 0)
        running = body.get("running", False)
        concurrency = body.get("max_concurrency", 1)
        if (
            isinstance(queued, bool)
            or not isinstance(queued, int)
            or queued < 0
            or not isinstance(running, bool)
            or concurrency != 1
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "health",
                "ACE Node health response is malformed",
            )
        return ProviderHealth(
            state == "ready",
            "ACE Node ready" if state == "ready" else f"ACE Node {state}",
            queued,
            1 if running else 0,
        )

    async def materialize_artifact(
        self, ref: ProviderJobRef, artifact: ProviderArtifact
    ) -> ProviderArtifact:
        ref.require_provider(ProviderName.NODE)
        ref.require_backend(BACKEND_ID)
        return artifact

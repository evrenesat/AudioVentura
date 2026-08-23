"""SaladCloud Job Queue adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx

from .base import (
    CancelOutcome,
    DetailScope,
    InferenceMode,
    InferenceRequest,
    InferenceResult,
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
from .salad_names import is_salad_resource_name

_MAX_BODY = 1_048_576
_FEATURES = frozenset(RequestFeature)


class SaladProvider:
    capabilities = ProviderCapabilities(
        ProviderName.SALAD, frozenset(InferenceMode), _FEATURES, frozenset({2}), True, False, False
    )

    def __init__(
        self,
        api_key: str,
        organization: str,
        project: str,
        queue: str,
        container_group: str,
        *,
        base_url: str = "https://api.salad.com/api/public",
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        for value, label in (
            (organization, "organization"),
            (project, "project"),
            (queue, "queue"),
            (container_group, "container group"),
        ):
            if not is_salad_resource_name(value):
                raise ValueError(f"Salad {label} name is invalid")
        if not api_key.strip():
            raise ValueError("Salad API key must not be empty")
        self.organization, self.project, self.queue, self.container_group = (
            organization,
            project,
            queue,
            container_group,
        )
        root = f"{base_url.rstrip('/')}/organizations/{organization}/projects/{project}"
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=root + "/",
            headers={"Salad-Api-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
            ),
        )
        if http_client is not None:
            http_client.headers.update({"Salad-Api-Key": api_key, "Accept": "application/json"})

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _job_path(self, external_id: str | None = None) -> str:
        base = f"queues/{self.queue}/jobs"
        return f"{base}/{external_id}" if external_id else base

    @staticmethod
    def _uuid(value: Any) -> str:
        try:
            return str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "response", "Salad job ID is invalid"
            ) from exc

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
                method, path, json=dict(body) if body is not None else None
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                ProviderErrorKind.TRANSIENT, operation, f"Salad {operation} request failed"
            ) from exc
        if response.status_code not in expected:
            status = response.status_code
            kind = (
                ProviderErrorKind.NOT_FOUND
                if status == 404
                else (
                    ProviderErrorKind.TRANSIENT
                    if status in {408, 429} or status >= 500
                    else ProviderErrorKind.REJECTED
                )
            )
            raise ProviderError(
                kind, operation, f"Salad {operation} request failed", status_code=status
            )
        if len(response.content) > _MAX_BODY:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Salad {operation} response is too large",
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Salad {operation} response is invalid",
            ) from exc

    @staticmethod
    def _mapping(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                f"Salad {operation} response is invalid",
            )
        return dict(value)

    async def submit(self, request: InferenceRequest) -> ProviderJobRef:
        metadata = {
            "application_job_id": request.application_job_id,
            "variation_index": request.variation_index,
            "submission_nonce": request.submission_nonce,
            "worker_schema_version": 2,
        }
        raw = await self._request(
            "POST",
            self._job_path(),
            "submit",
            body={"input": dict(request.worker_payload), "metadata": metadata},
            expected=frozenset({201, 202}),
        )
        body = self._mapping(raw, "submit")
        external_id = self._uuid(body.get("id"))
        if body.get("status") != "pending" or body.get("metadata") != metadata:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                "submit",
                "Salad submission response does not match the request",
            )
        return ProviderJobRef(ProviderName.SALAD, external_id)

    async def _get_job(self, ref: ProviderJobRef, operation: str = "status") -> dict[str, Any]:
        ref.require_provider(ProviderName.SALAD)
        requested = self._uuid(ref.external_id)
        body = self._mapping(
            await self._request("GET", self._job_path(requested), operation), operation
        )
        if self._uuid(body.get("id")) != requested:
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE,
                operation,
                "Salad response job ID does not match",
            )
        return body

    async def status(self, ref: ProviderJobRef) -> ProviderStatus:
        body = await self._get_job(ref)
        state = body.get("status")
        phase = {
            "pending": ProviderPhase.QUEUED,
            "running": ProviderPhase.RUNNING,
            "succeeded": ProviderPhase.SUCCEEDED,
            "failed": ProviderPhase.FAILED,
            "cancelled": ProviderPhase.CANCELLED,
        }.get(state if isinstance(state, str) else "", ProviderPhase.UNKNOWN)
        base = ProviderStatus(phase, provider_state=state if isinstance(state, str) else None)
        if state != "pending":
            return base
        try:
            raw = await self._request(
                "GET", f"containers/{self.container_group}/instances", "instances"
            )
            values = raw.get("items", raw.get("instances")) if isinstance(raw, Mapping) else raw
            if (
                not isinstance(values, list)
                or len(values) != 1
                or not isinstance(values[0], Mapping)
            ):
                return base
            instance = values[0]
            status = instance.get("state", instance.get("status"))
            ready = instance.get("ready")
            if status == "allocating":
                return ProviderStatus(
                    ProviderPhase.PROVISIONING,
                    "Waiting for GPU",
                    provider_state=state,
                    detail_scope=DetailScope.DEPLOYMENT,
                )
            if status == "downloading":
                raw_progress = instance.get("pulling_progress")
                progress = (
                    float(raw_progress) / 100
                    if isinstance(raw_progress, (int, float))
                    and not isinstance(raw_progress, bool)
                    and 0 <= raw_progress <= 100
                    else None
                )
                return ProviderStatus(
                    ProviderPhase.PROVISIONING,
                    "Downloading worker image",
                    progress,
                    state,
                    detail_scope=DetailScope.DEPLOYMENT,
                )
            if status == "creating":
                return ProviderStatus(
                    ProviderPhase.STARTING,
                    "Starting worker",
                    provider_state=state,
                    detail_scope=DetailScope.DEPLOYMENT,
                )
            if status == "running" and ready is False:
                return ProviderStatus(
                    ProviderPhase.STARTING,
                    "Initializing ACE-Step",
                    provider_state=state,
                    detail_scope=DetailScope.DEPLOYMENT,
                )
            if status == "running" and ready is True:
                return ProviderStatus(
                    ProviderPhase.QUEUED,
                    "Worker ready",
                    provider_state=state,
                    detail_scope=DetailScope.DEPLOYMENT,
                )
        except (ProviderError, ValueError, TypeError):
            return base
        return base

    async def result(self, ref: ProviderJobRef) -> InferenceResult:
        body = await self._get_job(ref, "result")
        if body.get("status") != "succeeded":
            raise ProviderJobNotComplete()
        output = body.get("output")
        if not isinstance(output, Mapping):
            raise ProviderError(
                ProviderErrorKind.INVALID_RESPONSE, "result", "Salad result output is invalid"
            )
        return InferenceResult(output)

    async def cancel(self, ref: ProviderJobRef) -> CancelOutcome:
        body = await self._get_job(ref, "cancel")
        state = body.get("status")
        if state == "cancelled":
            return CancelOutcome.CANCELLED
        if state != "pending":
            return CancelOutcome.TOO_LATE
        await self._request(
            "DELETE", self._job_path(ref.external_id), "cancel", expected=frozenset({202})
        )
        return CancelOutcome.CANCELLED

    async def health(self) -> ProviderHealth:
        queue = self._mapping(
            await self._request("GET", f"queues/{self.queue}", "health"), "health"
        )
        group = self._mapping(
            await self._request("GET", f"containers/{self.container_group}", "health"),
            "health",
        )
        queued = queue.get("current_queue_length")
        replicas = group.get("replicas")
        return ProviderHealth(
            True,
            "Salad available",
            queued if isinstance(queued, int) else None,
            replicas if isinstance(replicas, int) else None,
        )

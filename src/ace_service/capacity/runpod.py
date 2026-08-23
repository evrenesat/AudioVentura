"""RunPod REST Endpoint API capacity manager."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from ace_service.providers.base import BackendId, ProviderName
from ace_service.runpod_client import parse_worker_counts

from .base import (
    CapacityError,
    CapacityErrorKind,
    CapacityPhase,
    CapacitySnapshot,
    canonical_fingerprint,
    fingerprints_match,
)
from .fingerprints import build_runpod_fingerprint_payload

_MAX_BODY = 1_048_576
_BACKEND_ID = BackendId("runpod/ace-step-v15-xl-turbo")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class RunpodCapacityManager:
    key: str
    provider = ProviderName.RUNPOD
    backend_ids = frozenset({_BACKEND_ID})

    def __init__(
        self,
        api_key: str,
        endpoint_id: str,
        expected_fingerprint: str,
        *,
        rest_base_url: str = "https://rest.runpod.io/v1",
        health_base_url: str = "https://api.runpod.ai/v2",
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or not endpoint_id.strip() or not expected_fingerprint.strip():
            raise ValueError("RunPod capacity credentials and fingerprint are required")
        self.endpoint_id = endpoint_id.strip()
        self.expected_fingerprint = expected_fingerprint.lower()
        self.key = f"runpod/{self.endpoint_id}"
        self._health_base_url = health_base_url.rstrip("/")
        timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
        )
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=rest_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
        )
        if http_client is not None:
            http_client.headers.update(
                {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
            )
        self._endpoint: dict[str, Any] | None = None
        self._idle_timeout_seconds = 60

    @property
    def release_grace_seconds(self) -> int:
        return min(180, max(60, self._idle_timeout_seconds + 120))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(
        self, method: str, path: str, operation: str, *, body: Mapping[str, Any] | None = None
    ) -> Any:
        try:
            response = await self._client.request(method, path, json=dict(body) if body else None)
        except httpx.HTTPError as exc:
            raise CapacityError(
                CapacityErrorKind.TRANSIENT, operation, "RunPod capacity request failed"
            ) from exc
        if not 200 <= response.status_code < 300:
            kind = (
                CapacityErrorKind.NOT_FOUND
                if response.status_code == 404
                else CapacityErrorKind.TRANSIENT
                if response.status_code in {408, 429} or response.status_code >= 500
                else CapacityErrorKind.INVALID_RESPONSE
            )
            raise CapacityError(
                kind,
                operation,
                "RunPod capacity request was rejected",
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_BODY:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE,
                operation,
                "RunPod capacity response is too large",
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, operation, "RunPod capacity response is invalid"
            ) from exc

    @staticmethod
    def _mapping(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, operation, "RunPod response is not an object"
            )
        return dict(value)

    def _fingerprint(self, endpoint: Mapping[str, Any]) -> str:
        return canonical_fingerprint(
            build_runpod_fingerprint_payload(endpoint), schema="runpod-capacity-v1"
        )

    async def _health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._health_base_url}/{self.endpoint_id}/health",
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise CapacityError(
                CapacityErrorKind.TRANSIENT, "health", "RunPod health request failed"
            ) from exc
        if response.status_code != 200:
            kind = (
                CapacityErrorKind.NOT_FOUND
                if response.status_code == 404
                else CapacityErrorKind.TRANSIENT
            )
            raise CapacityError(
                kind,
                "health",
                "RunPod health request was rejected",
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_BODY:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "health", "RunPod health response is too large"
            )
        try:
            return self._mapping(response.json(), "health")
        except (json.JSONDecodeError, ValueError) as exc:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "health", "RunPod health response is invalid"
            ) from exc

    async def inspect(self) -> CapacitySnapshot:
        body = await self._request_json(
            "GET",
            "endpoints?includeTemplate=true&includeWorkers=true",
            "endpoint inspect",
        )
        endpoints = body.get("items") if isinstance(body, Mapping) else body
        if not isinstance(endpoints, list):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "RunPod endpoint list is invalid"
            )
        matches = [
            item
            for item in endpoints
            if isinstance(item, Mapping) and item.get("id") == self.endpoint_id
        ]
        if len(matches) != 1:
            raise CapacityError(
                CapacityErrorKind.DRIFT if matches else CapacityErrorKind.NOT_FOUND,
                "inspect",
                "RunPod endpoint identity is not unique",
            )
        endpoint = dict(matches[0])
        embedded_template = endpoint.get("template")
        if not isinstance(embedded_template, Mapping):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "RunPod template is invalid"
            )
        template_id = embedded_template.get("id")
        if not isinstance(template_id, str) or not _RESOURCE_ID.fullmatch(template_id):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "RunPod template ID is invalid"
            )
        template = self._mapping(
            await self._request_json("GET", f"templates/{template_id}", "template inspect"),
            "template inspect",
        )
        if template.get("id") != template_id or endpoint.get("templateId") not in {
            None,
            template_id,
        }:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "RunPod template identity drifted"
            )
        endpoint["template"] = template
        if endpoint.get("workersMax") != 1 or endpoint.get("gpuCount") != 1:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "RunPod capacity maximum is not one"
            )
        if not fingerprints_match(self._fingerprint(endpoint), self.expected_fingerprint):
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "RunPod endpoint fingerprint drifted"
            )
        floor = endpoint.get("workersMin")
        if floor not in {0, 1}:
            raise CapacityError(CapacityErrorKind.DRIFT, "inspect", "RunPod workersMin is unsafe")
        health = await self._health()
        try:
            counts = parse_worker_counts(type("Health", (), {"details": health})())
        except Exception as exc:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "health", "RunPod health counts are invalid"
            ) from exc
        observed = counts.active
        if observed > 1:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "health", "RunPod has more than one worker"
            )
        self._endpoint = endpoint
        idle_timeout = endpoint.get("idleTimeout")
        if (
            isinstance(idle_timeout, int)
            and not isinstance(idle_timeout, bool)
            and idle_timeout >= 0
        ):
            self._idle_timeout_seconds = idle_timeout
        if floor == 0 and observed == 0:
            phase = CapacityPhase.COLD
        elif floor == 0:
            phase = CapacityPhase.RELEASING
        elif counts.has_pending_work:
            phase = CapacityPhase.BUSY
        elif counts.idle:
            phase = CapacityPhase.READY
        else:
            phase = CapacityPhase.WARMING
        return CapacitySnapshot(
            self.key,
            ProviderName.RUNPOD,
            phase,
            int(floor),
            1,
            observed,
            1 if counts.idle else 0,
            counts.queued + counts.in_progress,
            None,
            datetime.now(UTC),
        )

    async def _patch_floor(self, floor: int, operation: str) -> CapacitySnapshot:
        if floor not in {0, 1}:
            raise ValueError("RunPod workersMin must be zero or one")
        await self._request_json(
            "PATCH", f"endpoints/{self.endpoint_id}", operation, body={"workersMin": floor}
        )
        return await self.inspect()

    async def retain_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        if before.key != self.key or before.configured_maximum != 1:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "retain", "RunPod retain snapshot is unsafe"
            )
        if before.configured_floor == 1:
            return before
        return await self._patch_floor(1, "retain")

    async def release_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        if before.key != self.key or before.provider_active_jobs or before.observed_instances > 1:
            raise CapacityError(
                CapacityErrorKind.UNSAFE_ACTIVE_WORK, "release", "RunPod still has active work"
            )
        if before.configured_floor == 0:
            return before
        return await self._patch_floor(0, "release")

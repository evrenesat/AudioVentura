"""SaladCloud capacity manager for one exact AudioVentura container group."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from ace_service.providers.base import BackendId, ProviderName
from ace_service.providers.salad_names import is_salad_resource_name

from .base import (
    CapacityError,
    CapacityErrorKind,
    CapacityPhase,
    CapacitySnapshot,
    canonical_fingerprint,
    fingerprints_match,
)
from .fingerprints import build_salad_fingerprint_payload

_MAX_BODY = 1_048_576
_BACKEND_ID = BackendId("salad/ace-step-v15-xl-turbo")


class SaladCapacityManager:
    key: str
    provider = ProviderName.SALAD
    backend_ids = frozenset({_BACKEND_ID})
    release_grace_seconds = 300

    def __init__(
        self,
        api_key: str,
        organization: str,
        project: str,
        queue: str,
        container_group: str,
        expected_fingerprint: str,
        *,
        base_url: str = "https://api.salad.com/api/public",
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or not expected_fingerprint.strip():
            raise ValueError("Salad capacity credentials and fingerprint are required")
        for value, label in (
            (organization, "organization"),
            (project, "project"),
            (queue, "queue"),
            (container_group, "container group"),
        ):
            if not is_salad_resource_name(value):
                raise ValueError(f"Salad {label} name is invalid")
        self.organization = organization
        self.project = project
        self.queue = queue
        self.container_group = container_group
        self.expected_fingerprint = expected_fingerprint.lower()
        self.key = f"salad/{container_group}"
        root = f"{base_url.rstrip('/')}/organizations/{organization}/projects/{project}"
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=root + "/",
            headers={"Salad-Api-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
            ),
            follow_redirects=False,
        )
        if http_client is not None:
            http_client.headers.update({"Salad-Api-Key": api_key, "Accept": "application/json"})
        self._autoscaler: dict[str, Any] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _mapping(value: Any, operation: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, operation, "Salad response is not an object"
            )
        return dict(value)

    async def _request_json(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        body: Mapping[str, Any] | None = None,
        expected: frozenset[int] = frozenset({200}),
    ) -> Any:
        try:
            response = await self._client.request(method, path, json=dict(body) if body else None)
        except httpx.HTTPError as exc:
            raise CapacityError(
                CapacityErrorKind.TRANSIENT, operation, "Salad capacity request failed"
            ) from exc
        if response.status_code not in expected:
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
                "Salad capacity request was rejected",
                status_code=response.status_code,
            )
        if len(response.content) > _MAX_BODY:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE,
                operation,
                "Salad capacity response is too large",
            )
        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, operation, "Salad capacity response is invalid"
            ) from exc

    async def _inspect_raw(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Any]]:
        queue = self._mapping(
            await self._request_json("GET", f"queues/{self.queue}", "queue inspect"),
            "queue inspect",
        )
        group = self._mapping(
            await self._request_json("GET", f"containers/{self.container_group}", "group inspect"),
            "group inspect",
        )
        jobs_body = await self._request_json(
            "GET", f"queues/{self.queue}/jobs?page=1&per_page=100", "queue jobs inspect"
        )
        instances_body = await self._request_json(
            "GET", f"containers/{self.container_group}/instances", "instances inspect"
        )
        jobs = jobs_body.get("items") if isinstance(jobs_body, Mapping) else jobs_body
        instances = (
            instances_body.get("instances")
            if isinstance(instances_body, Mapping)
            else instances_body
        )
        if not isinstance(jobs, list) or not isinstance(instances, list):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "Salad list response is invalid"
            )
        if len(instances) > 1:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "Salad has more than one worker"
            )
        if any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("state"), str)
            or not isinstance(item.get("ready"), bool)
            for item in instances
        ):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE,
                "inspect",
                "Salad instance state is invalid",
            )
        return queue, group, {"jobs": jobs}, instances

    def _fingerprint(self, queue: Mapping[str, Any], group: Mapping[str, Any]) -> str:
        payload = build_salad_fingerprint_payload(
            queue,
            group,
            organization=self.organization,
            project=self.project,
        )
        if payload["queue"]["name"] != self.queue or payload["group"]["name"] != (
            self.container_group
        ):
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "Salad resource identity drifted"
            )
        if payload["group"]["queue_connection"]["queue_name"] != self.queue:
            raise CapacityError(CapacityErrorKind.DRIFT, "inspect", "Salad queue ownership drifted")
        if payload["group"]["queue_autoscaler"]["max_replicas"] != 1:
            raise CapacityError(CapacityErrorKind.DRIFT, "inspect", "Salad capacity limits drifted")
        return canonical_fingerprint(payload, schema="salad-capacity-v1")

    async def inspect(self) -> CapacitySnapshot:
        queue, group, job_wrapper, instances = await self._inspect_raw()
        fingerprint = self._fingerprint(queue, group)
        if not fingerprints_match(fingerprint, self.expected_fingerprint):
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "Salad deployment fingerprint drifted"
            )
        autoscaler = group.get("queue_autoscaler")
        if not isinstance(autoscaler, Mapping):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "Salad autoscaler is invalid"
            )
        floor = autoscaler.get("min_replicas")
        replicas = group.get("replicas")
        if floor not in {0, 1} or replicas not in {0, 1}:
            raise CapacityError(
                CapacityErrorKind.DRIFT, "inspect", "Salad replica counts are unsafe"
            )
        queue_length = queue.get("current_queue_length")
        if (
            isinstance(queue_length, bool)
            or not isinstance(queue_length, int)
            or not 0 <= queue_length <= 10_000
        ):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, "inspect", "Salad queue length is invalid"
            )
        jobs = job_wrapper["jobs"]
        for item in jobs:
            if not isinstance(item, Mapping) or not isinstance(item.get("status"), str):
                raise CapacityError(
                    CapacityErrorKind.INVALID_RESPONSE, "inspect", "Salad queue job page is invalid"
                )
        active_jobs = queue_length or sum(
            1 for item in jobs if item.get("status") not in {"succeeded", "failed", "cancelled"}
        )
        observed = len(instances)
        ready = (
            1
            if observed and replicas == 1 and any(item.get("ready") is True for item in instances)
            else 0
        )
        if floor == 0 and observed == 0:
            phase = CapacityPhase.COLD
        elif floor == 0:
            phase = CapacityPhase.RELEASING
        elif active_jobs:
            phase = CapacityPhase.BUSY
        elif ready:
            phase = CapacityPhase.READY
        else:
            phase = CapacityPhase.WARMING
        self._autoscaler = dict(autoscaler)
        return CapacitySnapshot(
            self.key,
            ProviderName.SALAD,
            phase,
            int(floor),
            int(autoscaler.get("max_replicas", 0)),
            observed,
            ready,
            active_jobs,
            None,
            datetime.now(UTC),
        )

    async def _patch_floor(self, floor: int, replicas: int, operation: str) -> CapacitySnapshot:
        if floor not in {0, 1} or replicas not in {0, 1} or self._autoscaler is None:
            raise CapacityError(
                CapacityErrorKind.DRIFT, operation, "Salad capacity is not inspected"
            )
        autoscaler = dict(self._autoscaler)
        autoscaler["min_replicas"] = floor
        body = await self._request_json(
            "PATCH",
            f"containers/{self.container_group}",
            operation,
            body={"queue_autoscaler": autoscaler, "replicas": replicas},
        )
        if body is not None and not isinstance(body, Mapping):
            raise CapacityError(
                CapacityErrorKind.INVALID_RESPONSE, operation, "Salad patch response is invalid"
            )
        return await self.inspect()

    async def retain_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        if (
            before.key != self.key
            or before.configured_maximum != 1
            or before.provider_active_jobs < 0
        ):
            raise CapacityError(
                CapacityErrorKind.DRIFT, "retain", "Salad retain snapshot is unsafe"
            )
        if before.configured_floor == 1:
            return before
        return await self._patch_floor(1, 1, "retain")

    async def release_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        if before.key != self.key or before.provider_active_jobs or before.observed_instances > 1:
            raise CapacityError(
                CapacityErrorKind.UNSAFE_ACTIVE_WORK, "release", "Salad still has active work"
            )
        if before.configured_floor == 0:
            return before
        return await self._patch_floor(0, 0, "release")

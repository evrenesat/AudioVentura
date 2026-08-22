"""Strict asynchronous Runpod API access and durable submission recovery."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import httpx

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.repository import (
    get_job,
    persist_runpod_job_id,
    prepare_runpod_submission,
    recover_uncertain_submissions,
)

_MAX_RESPONSE_BYTES = 1_048_576
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RUNPOD_STATES = frozenset(
    {"IN_QUEUE", "IN_PROGRESS", "RUNNING", "COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}
)
_PROGRESS_KIND = "audioventura_progress_v1"
_WORKER_PROGRESS_PHASES = frozenset(
    {"source_download", "generation", "finalizing", "output_upload"}
)
_WORKER_PROGRESS_SEQUENCES = {
    "source_download": 10,
    "generation": 20,
    "finalizing": 30,
    "output_upload": 40,
}
LOGGER = logging.getLogger(__name__)


class RunpodError(RuntimeError):
    """Base class for safe, non-secret Runpod failures."""


class RunpodAPIError(RunpodError):
    """Raised when Runpod rejects or cannot complete an API request."""


class RunpodResponseError(RunpodError):
    """Raised when Runpod returns a body outside the expected contract."""


class RunpodState(StrEnum):
    """Internal state categories used by the controller."""

    CLOUD_QUEUED = "cloud_queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunpodHealth:
    """Validated health response without retaining an unbounded body."""

    details: dict[str, Any]


# A campaign endpoint above this many workers or queued jobs is outside the
# frozen contract; the gate fails closed instead of trusting an unknown shape.
_MAX_HEALTH_COUNT = 10_000


@dataclass(frozen=True, slots=True)
class EndpointWorkerCounts:
    """Bounded validated worker/job counts from one Runpod health response."""

    idle: int
    running: int
    queued: int
    in_progress: int
    initializing: int = 0
    unhealthy: int = 0

    @property
    def active(self) -> int:
        """Count every known worker state that prevents a zero-at-rest claim."""
        return self.idle + self.running + self.initializing + self.unhealthy

    @property
    def has_pending_work(self) -> bool:
        return self.queued > 0 or self.in_progress > 0


def _bounded_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunpodResponseError(f"Runpod health field {name} is malformed")
    if value > _MAX_HEALTH_COUNT:
        raise RunpodResponseError(f"Runpod health field {name} is out of bounds")
    return cast(int, value)


def parse_worker_counts(health: RunpodHealth) -> EndpointWorkerCounts:
    """Extract bounded zero-at-rest counts from the pinned Runpod /health contract.

    The pinned contract reports ``workers`` (``idle``/``running``) and ``jobs``
    (``inQueue``/``inProgress``) as non-negative integers.  Missing, boolean,
    negative, oversized, or unknown structures are rejected so teardown can
    never infer zero from a malformed or empty provider body.  The obsolete
    ``jobs.queued``/``jobs.running`` shape is likewise rejected rather than
    silently accepted as provider evidence.
    """

    details = health.details
    workers = details.get("workers")
    jobs = details.get("jobs")
    if not isinstance(workers, Mapping) or not isinstance(jobs, Mapping):
        raise RunpodResponseError("Runpod health response is missing the workers/jobs contract")
    if "queued" in jobs or "running" in jobs:
        raise RunpodResponseError(
            "Runpod health response uses the obsolete jobs.queued/jobs.running shape"
        )
    return EndpointWorkerCounts(
        idle=_bounded_count(workers.get("idle"), "workers.idle"),
        running=_bounded_count(workers.get("running"), "workers.running"),
        queued=_bounded_count(jobs.get("inQueue"), "jobs.inQueue"),
        in_progress=_bounded_count(jobs.get("inProgress"), "jobs.inProgress"),
        initializing=_bounded_optional_count(workers.get("initializing"), "workers.initializing"),
        unhealthy=_bounded_optional_count(workers.get("unhealthy"), "workers.unhealthy"),
    )


@dataclass(frozen=True, slots=True)
class RunpodStatusResult:
    """Normalized status and bounded metadata from a Runpod job."""

    job_id: str
    category: RunpodState
    raw_status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    delay_ms: int | None = None
    execution_ms: int | None = None
    progress: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """Compatibility-friendly string form of the normalized category."""

        return self.category.value


def _positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunpodResponseError(f"Runpod response field {name} is malformed")
    return cast(int, value)


def _bounded_optional_count(value: Any, name: str) -> int:
    return 0 if value is None else _bounded_count(value, name)


def _worker_progress(value: Any) -> dict[str, Any] | None:
    """Return one exact advisory progress object or ignore provider noise."""

    if not isinstance(value, Mapping) or set(value) != {"kind", "phase", "sequence"}:
        return None
    if value.get("kind") != _PROGRESS_KIND or value.get("phase") not in _WORKER_PROGRESS_PHASES:
        return None
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    if sequence != _WORKER_PROGRESS_SEQUENCES[value["phase"]]:
        return None
    return {"kind": _PROGRESS_KIND, "phase": value["phase"], "sequence": sequence}


def _job_id(value: Any, name: str = "job ID") -> str:
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value.strip()):
        raise RunpodResponseError(f"Runpod response {name} is malformed")
    return value.strip()


def _state(value: Any) -> tuple[str, RunpodState]:
    if not isinstance(value, str) or value not in _RUNPOD_STATES:
        raise RunpodResponseError("Runpod response status is malformed")
    if value == "IN_QUEUE":
        return value, RunpodState.CLOUD_QUEUED
    if value in {"IN_PROGRESS", "RUNNING"}:
        return value, RunpodState.GENERATING
    if value == "COMPLETED":
        return value, RunpodState.COMPLETED
    return value, RunpodState.FAILED


def _mapping_body(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunpodResponseError(f"Runpod {operation} response must be an object")
    return dict(value)


class RunpodClient:
    """One reusable async HTTP adapter for a queue-based Runpod endpoint."""

    def __init__(
        self,
        api_key: str,
        endpoint_id: str,
        *,
        base_url: str = "https://api.runpod.ai/v2",
        connect_timeout: float = 5,
        read_timeout: float = 30,
        write_timeout: float = 30,
        pool_timeout: float = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or not endpoint_id.strip():
            raise ValueError("Runpod API credentials must not be empty")
        if not base_url.startswith("https://"):
            raise ValueError("Runpod API base URL must use HTTPS")
        self._owns_client = http_client is None
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        self._client = http_client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/{endpoint_id.strip()}/",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
        if http_client is not None:
            http_client.headers.update(
                {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
            )

    @classmethod
    def from_settings(cls, settings: ServiceSettings) -> RunpodClient:
        return cls(
            settings.runpod_api_key,
            settings.runpod_endpoint_id,
            connect_timeout=settings.runpod_connect_timeout_seconds,
            read_timeout=settings.runpod_read_timeout_seconds,
            write_timeout=settings.runpod_write_timeout_seconds,
            pool_timeout=settings.runpod_pool_timeout_seconds,
        )

    async def __aenter__(self) -> RunpodClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> RunpodHealth:
        body = await self._request_json("GET", "health", operation="health")
        return RunpodHealth(details=_mapping_body(body, "health"))

    async def submit(
        self,
        payload: Mapping[str, Any],
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> str:
        if not isinstance(payload, Mapping):
            raise ValueError("Runpod payload must be an object")
        if execution_timeout_ms <= 0 or ttl_ms <= 0:
            raise ValueError("Runpod timeouts must be positive")
        body = await self._request_json(
            "POST",
            "run",
            operation="submission",
            json_body={
                "input": dict(payload),
                "policy": {"executionTimeout": execution_timeout_ms, "ttl": ttl_ms},
            },
        )
        response = _mapping_body(body, "submission")
        runpod_job_id = _job_id(response.get("id"))
        if "status" in response:
            _state(response["status"])
        return runpod_job_id

    async def status(self, runpod_job_id: str) -> RunpodStatusResult:
        request_id = _job_id(runpod_job_id, "job ID")
        body = await self._request_json("GET", f"status/{request_id}", operation="status")
        response = _mapping_body(body, "status")
        returned_id = _job_id(response.get("id"))
        if returned_id != request_id:
            raise RunpodResponseError("Runpod status response job ID does not match the request")
        raw_status, category = _state(response.get("status"))
        raw_output = response.get("output")
        result: dict[str, Any] | None = None
        progress: dict[str, Any] | None = None
        if category in {RunpodState.CLOUD_QUEUED, RunpodState.GENERATING}:
            progress = _worker_progress(raw_output)
            if raw_output is not None and progress is None:
                LOGGER.warning(
                    "stage=runpod operation=status error_code=runpod_progress_unavailable"
                )
        elif raw_output is not None:
            if not isinstance(raw_output, Mapping):
                raise RunpodResponseError("Runpod status output is malformed")
            result = dict(raw_output)
        raw_error = response.get("error")
        if raw_error is not None and (not isinstance(raw_error, str) or len(raw_error) > 4096):
            raise RunpodResponseError("Runpod status error is malformed")
        return RunpodStatusResult(
            job_id=request_id,
            category=category,
            raw_status=raw_status,
            result=result,
            error=raw_error,
            delay_ms=_positive_int(response.get("delayTime"), "delayTime"),
            execution_ms=_positive_int(response.get("executionTime"), "executionTime"),
            progress=progress,
        )

    async def cancel(self, runpod_job_id: str) -> RunpodStatusResult:
        request_id = _job_id(runpod_job_id, "job ID")
        body = await self._request_json("POST", f"cancel/{request_id}", operation="cancellation")
        response = _mapping_body(body, "cancellation")
        returned_id = _job_id(response.get("id"))
        if returned_id != request_id:
            raise RunpodResponseError("Runpod cancellation response job ID does not match")
        raw_status, category = _state(response.get("status"))
        if raw_status != "CANCELLED":
            raise RunpodResponseError("Runpod cancellation response was not CANCELLED")
        return RunpodStatusResult(job_id=request_id, category=category, raw_status=raw_status)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        started = time.monotonic()
        try:
            response = await self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            LOGGER.warning(
                "stage=runpod operation=%s error_code=runpod_request_failed "
                "exception_class=%s elapsed_ms=%d",
                operation,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise RunpodAPIError(f"Runpod {operation} request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            LOGGER.warning(
                "stage=runpod operation=%s error_code=runpod_http_error status=%d elapsed_ms=%d",
                operation,
                response.status_code,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise RunpodAPIError(
                f"Runpod {operation} request failed with HTTP {response.status_code}"
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise RunpodResponseError(f"Runpod {operation} response is too large")
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning(
                "stage=runpod operation=%s error_code=runpod_invalid_json "
                "exception_class=%s elapsed_ms=%d",
                operation,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "controller"},
            )
            raise RunpodResponseError(f"Runpod {operation} response is not valid JSON") from exc
        LOGGER.info(
            "stage=runpod operation=%s elapsed_ms=%d",
            operation,
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )
        return body


class RunpodSubmissionCoordinator:
    """Keep nonce persistence and cloud submission ordering explicit."""

    def __init__(self, client: RunpodClient, session_factory: SessionFactory) -> None:
        self.client = client
        self.session_factory = session_factory

    async def submit(
        self,
        job_id: str,
        payload: Mapping[str, Any],
        *,
        execution_timeout_ms: int,
        ttl_ms: int,
    ) -> str:
        with self.session_factory() as session:
            job, nonce = prepare_runpod_submission(session, job_id)
            session.commit()
            if "submission_nonce" in payload and payload["submission_nonce"] != nonce:
                raise ValueError("payload submission nonce does not match persisted nonce")
            request_payload = dict(payload)
            request_payload["submission_nonce"] = nonce
            del job

        # The committed nonce-only row is the durable no-duplicate boundary.
        runpod_job_id = await self.client.submit(
            request_payload, execution_timeout_ms=execution_timeout_ms, ttl_ms=ttl_ms
        )
        with self.session_factory() as session:
            persist_runpod_job_id(
                session,
                job_id,
                runpod_job_id,
                submission_nonce=nonce,
            )
            session.commit()
        return runpod_job_id

    async def poll_persisted_job(self, job_id: str) -> RunpodStatusResult:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise KeyError(f"unknown job: {job_id}")
            runpod_job_id = job.current_runpod_job_id
        if not runpod_job_id:
            raise RunpodError("job has no persisted Runpod ID; automatic resubmission is disabled")
        return await self.client.status(runpod_job_id)

    def recover_uncertain(self) -> list[str]:
        with self.session_factory() as session:
            jobs = recover_uncertain_submissions(session)
            identifiers = [job.id for job in jobs]
            session.commit()
        return identifiers

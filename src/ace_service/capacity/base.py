"""Finite provider-neutral capacity types and safe error contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ace_service.providers.base import BackendId, ProviderName


class CapacityPhase(StrEnum):
    COLD = "cold"
    WARMING = "warming"
    READY = "ready"
    BUSY = "busy"
    RELEASING = "releasing"
    UNKNOWN = "unknown"


class CapacityErrorKind(StrEnum):
    TRANSIENT = "transient"
    DRIFT = "drift"
    UNSAFE_ACTIVE_WORK = "unsafe_active_work"
    INVALID_RESPONSE = "invalid_response"
    NOT_FOUND = "not_found"


class CapacityError(RuntimeError):
    """Bounded provider failure; credentials and raw responses never enter it."""

    def __init__(
        self,
        kind: CapacityErrorKind | str,
        operation: str,
        safe_message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        if not operation or len(operation) > 64 or not safe_message or len(safe_message) > 256:
            raise ValueError("capacity error text is invalid")
        self.kind = CapacityErrorKind(kind)
        self.operation = operation
        self.safe_message = safe_message
        self.status_code = status_code
        super().__init__(safe_message)

    def __repr__(self) -> str:
        return (
            f"CapacityError(kind={self.kind.value!r}, operation={self.operation!r}, "
            f"status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    key: str
    provider: ProviderName
    phase: CapacityPhase
    configured_floor: int
    configured_maximum: int
    observed_instances: int
    ready_instances: int
    provider_active_jobs: int
    resource_revision: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > 256 or any(ord(char) < 33 for char in self.key):
            raise ValueError("capacity key is invalid")
        object.__setattr__(self, "provider", ProviderName(self.provider))
        object.__setattr__(self, "phase", CapacityPhase(self.phase))
        if self.configured_floor not in {0, 1} or self.configured_maximum not in {0, 1}:
            raise ValueError("capacity floor and maximum must be zero or one")
        if not 0 <= self.observed_instances <= 1 or not 0 <= self.ready_instances <= 1:
            raise ValueError("capacity instance counts must be zero or one")
        if self.ready_instances > self.observed_instances:
            raise ValueError("ready instances cannot exceed observed instances")
        if not 0 <= self.provider_active_jobs <= 10_000:
            raise ValueError("provider active jobs are out of bounds")
        if self.resource_revision is not None and (
            not self.resource_revision
            or len(self.resource_revision) > 128
            or any(ord(char) < 33 for char in self.resource_revision)
        ):
            raise ValueError("resource revision is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("capacity observation must be timezone-aware")


class CapacityManager(Protocol):
    key: str
    provider: ProviderName
    backend_ids: frozenset[BackendId]

    async def inspect(self) -> CapacitySnapshot: ...

    async def retain_one(self, before: CapacitySnapshot) -> CapacitySnapshot: ...

    async def release_one(self, before: CapacitySnapshot) -> CapacitySnapshot: ...


def canonical_fingerprint(payload: dict[str, object], *, schema: str) -> str:
    """Hash a sorted, compact, secret-free deployment identity."""

    if not schema or not schema.endswith("-v1"):
        raise ValueError("fingerprint schema must be versioned")
    normalized = {"schema": schema, **payload}
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("fingerprint payload is too large")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprints_match(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("ascii"), expected.encode("ascii"))

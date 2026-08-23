"""Exact backend-to-capacity manager registry."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ace_service.providers.base import BackendId

from .base import CapacityManager

if TYPE_CHECKING:
    from ace_service.config import ServiceSettings


class CapacityRegistry:
    """Map each configured inference backend to at most one capacity key."""

    def __init__(self, managers: Iterable[CapacityManager] = ()) -> None:
        by_key: dict[str, CapacityManager] = {}
        by_backend: dict[BackendId, CapacityManager] = {}
        for manager in managers:
            if not manager.key or manager.key in by_key:
                raise ValueError(f"duplicate capacity key: {manager.key}")
            by_key[manager.key] = manager
            for backend_id in manager.backend_ids:
                normalized = BackendId(str(backend_id))
                if normalized in by_backend:
                    raise ValueError(f"backend has multiple capacity managers: {normalized}")
                by_backend[normalized] = manager
        self._by_key = by_key
        self._by_backend = by_backend

    def for_backend(self, backend_id: BackendId | str) -> CapacityManager | None:
        return self._by_backend.get(BackendId(str(backend_id)))

    def for_key(self, key: str) -> CapacityManager | None:
        return self._by_key.get(key)

    @property
    def managers(self) -> tuple[CapacityManager, ...]:
        return tuple(self._by_key.values())


def build_capacity_registry(settings: ServiceSettings) -> CapacityRegistry:
    """Build only explicitly fingerprint-pinned managed capacity adapters."""

    from ace_service.capacity.runpod import RunpodCapacityManager
    from ace_service.capacity.salad import SaladCapacityManager

    managers: list[CapacityManager] = []
    runpod_fingerprint = getattr(settings, "runpod_capacity_expected_fingerprint", None)
    if runpod_fingerprint is not None:
        managers.append(
            RunpodCapacityManager(
                settings.runpod_api_key,
                settings.runpod_endpoint_id,
                runpod_fingerprint,
                connect_timeout=settings.runpod_connect_timeout_seconds,
                read_timeout=settings.runpod_read_timeout_seconds,
                write_timeout=settings.runpod_write_timeout_seconds,
                pool_timeout=settings.runpod_pool_timeout_seconds,
            )
        )
    salad_fingerprint = getattr(settings, "salad_capacity_expected_fingerprint", None)
    if salad_fingerprint is not None:
        if (
            not settings.salad_api_key
            or not settings.salad_organization
            or not settings.salad_project
        ):
            raise ValueError("Salad capacity fingerprint requires Salad configuration")
        managers.append(
            SaladCapacityManager(
                settings.salad_api_key,
                settings.salad_organization,
                settings.salad_project,
                settings.salad_queue_name,
                settings.salad_container_group_name,
                salad_fingerprint,
                connect_timeout=settings.salad_connect_timeout_seconds,
                read_timeout=settings.salad_read_timeout_seconds,
                write_timeout=settings.salad_write_timeout_seconds,
                pool_timeout=settings.salad_pool_timeout_seconds,
            )
        )
    return CapacityRegistry(managers)

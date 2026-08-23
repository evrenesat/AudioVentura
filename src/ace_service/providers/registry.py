"""Configured backend registry with immutable persisted lookup."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .base import BackendId, BackendOperation, InferenceProvider, ProviderName


class BackendRegistry:
    """Index providers by controller-owned backend ID.

    Coarse provider lookup remains available for old Runpod/Salad callers, but
    new work should resolve an exact backend ID before it is persisted.
    """

    def __init__(
        self,
        providers: Iterable[InferenceProvider],
        *,
        default: ProviderName | BackendId | str | None = None,
        defaults: Mapping[str, BackendId | str] | None = None,
    ) -> None:
        self._providers: dict[BackendId, InferenceProvider] = {}
        self._by_provider: dict[ProviderName, list[InferenceProvider]] = {}
        for provider in providers:
            backend_id = BackendId(str(provider.capabilities.backend_id))
            if backend_id in self._providers:
                raise ValueError(f"duplicate inference backend: {backend_id}")
            self._providers[backend_id] = provider
            self._by_provider.setdefault(provider.capabilities.name, []).append(provider)
        if not self._providers:
            raise ValueError("at least one inference backend is required")
        self._defaults: dict[BackendOperation, BackendId] = {}
        if defaults:
            for operation, raw_backend_id in defaults.items():
                normalized_backend_id = BackendId(str(raw_backend_id))
                self._defaults[BackendOperation(operation)] = normalized_backend_id
        self._legacy_default_provider: ProviderName | None = None
        if default is not None:
            if isinstance(default, ProviderName):
                provider_backends = self._by_provider.get(default, [])
                if not provider_backends:
                    raise ValueError(
                        f"default inference provider is not configured: {default.value}"
                    )
                selected = BackendId(str(provider_backends[0].capabilities.backend_id))
                self._legacy_default_provider = default
            else:
                selected = BackendId(str(default))
            self._defaults.setdefault(BackendOperation.TEXT_TO_MUSIC, selected)
            self._defaults.setdefault(BackendOperation.AUDIO_TRANSFORM, selected)
        for operation, backend_id in self._defaults.items():
            if backend_id not in self._providers:
                raise ValueError(f"default inference backend is not configured: {backend_id}")
            capability_operation = self._providers[backend_id].capabilities.operation
            if capability_operation is not None and capability_operation is not operation:
                raise ValueError(
                    f"default inference backend is not compatible with {operation.value}: "
                    f"{backend_id}"
                )

    @property
    def default(self) -> ProviderName:
        """Compatibility view used by the pre-backend web and test seams."""

        if self._legacy_default_provider is not None:
            return self._legacy_default_provider
        original = self._defaults.get(BackendOperation.TEXT_TO_MUSIC)
        if original is None:
            return next(iter(self._by_provider))
        return self.get_persisted(original).capabilities.name

    @property
    def default_backend_ids(self) -> Mapping[BackendOperation, BackendId]:
        return dict(self._defaults)

    def get_persisted(self, backend_id: BackendId | str) -> InferenceProvider:
        normalized = BackendId(str(backend_id))
        try:
            return self._providers[normalized]
        except KeyError as exc:
            # Focused compatibility for test doubles and pre-backend adapters
            # whose capabilities still use ``provider/default``.
            provider_name = normalized.split("/", 1)[0]
            try:
                provider = ProviderName(provider_name)
            except ValueError:
                provider = None
            candidates = self._by_provider.get(provider, []) if provider is not None else []
            if len(candidates) == 1 and str(candidates[0].capabilities.backend_id).endswith(
                "/default"
            ):
                return candidates[0]
            raise ValueError(f"inference backend is not configured: {normalized}") from exc

    def get(self, name: ProviderName | BackendId | str) -> InferenceProvider:
        """Resolve an exact backend, or the first legacy provider backend."""

        if isinstance(name, ProviderName):
            provider = name
        else:
            raw = str(name)
            try:
                return self.get_persisted(raw)
            except ValueError:
                try:
                    provider = ProviderName(raw)
                except ValueError as exc:
                    raise ValueError(
                        f"inference provider/backend is not configured: {name}"
                    ) from exc
        try:
            return self._by_provider[provider][0]
        except (KeyError, IndexError) as exc:
            raise ValueError(f"inference provider is not configured: {provider.value}") from exc

    def selectable(self, operation: BackendOperation | str) -> tuple[InferenceProvider, ...]:
        requested = BackendOperation(operation)
        return tuple(
            provider
            for provider in self._providers.values()
            if provider.capabilities.operation in {None, requested}
        )

    def default_for(self, operation: BackendOperation | str) -> InferenceProvider:
        requested = BackendOperation(operation)
        backend_id = self._defaults.get(requested)
        if backend_id is None:
            compatible = self.selectable(requested)
            if not compatible:
                raise ValueError(f"no backend is configured for operation: {requested.value}")
            return compatible[0]
        return self.get_persisted(backend_id)

    @property
    def providers(self) -> tuple[InferenceProvider, ...]:
        return tuple(self._providers.values())

    @property
    def backends(self) -> tuple[InferenceProvider, ...]:
        return self.providers

    @property
    def closeable_transports(self) -> tuple[Any, ...]:
        seen: set[int] = set()
        transports: list[Any] = []
        for provider in self._providers.values():
            transport = getattr(provider, "transport", getattr(provider, "_client", None))
            if transport is None:
                continue
            if id(transport) not in seen:
                seen.add(id(transport))
                transports.append(transport)
        return tuple(transports)


# Temporary import alias for existing tests and focused migrations.
ProviderRegistry = BackendRegistry

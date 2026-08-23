"""Configured provider registry with explicit persisted lookup."""

from __future__ import annotations

from collections.abc import Iterable

from .base import InferenceProvider, ProviderName


class ProviderRegistry:
    def __init__(self, providers: Iterable[InferenceProvider], *, default: ProviderName) -> None:
        self._providers: dict[ProviderName, InferenceProvider] = {}
        for provider in providers:
            name = provider.capabilities.name
            if name in self._providers:
                raise ValueError(f"duplicate inference provider: {name.value}")
            self._providers[name] = provider
        if default not in self._providers:
            raise ValueError(f"default inference provider is not configured: {default.value}")
        self.default = default

    def get(self, name: ProviderName | str) -> InferenceProvider:
        try:
            normalized = ProviderName(name)
            return self._providers[normalized]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"inference provider is not configured: {name}") from exc

    @property
    def providers(self) -> tuple[InferenceProvider, ...]:
        return tuple(self._providers.values())

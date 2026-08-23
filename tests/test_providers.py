from __future__ import annotations

import asyncio

import pytest

from ace_service.providers.base import (
    InferenceMode,
    InferenceRequest,
    ProviderJobRef,
    ProviderName,
    RequestFeature,
    unsupported_features,
)
from ace_service.providers.registry import ProviderRegistry
from ace_service.providers.runpod import RunpodProvider
from ace_service.runpod_client import RunpodHealth, RunpodState, RunpodStatusResult


def _request() -> InferenceRequest:
    return InferenceRequest(
        "job-1",
        1,
        "nonce",
        InferenceMode.PROMPT_TO_AUDIO,
        frozenset({RequestFeature.PROMPT}),
        {"schema_version": 2},
        1000,
        2000,
    )


class _Runpod:
    async def submit(self, payload, execution_timeout_ms, ttl_ms):
        assert payload == {"schema_version": 2}
        return "runpod-1"

    async def status(self, job_id):
        return RunpodStatusResult(job_id, RunpodState.COMPLETED, "COMPLETED", {"schema_version": 2})

    async def cancel(self, job_id):
        return RunpodStatusResult(job_id, RunpodState.FAILED, "CANCELLED")

    async def health(self):
        return RunpodHealth(
            {"workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0}}
        )


def test_registry_is_explicit_and_capabilities_are_shared() -> None:
    provider = RunpodProvider(_Runpod())  # type: ignore[arg-type]
    registry = ProviderRegistry([provider], default=ProviderName.RUNPOD)
    assert registry.get("runpod") is provider
    assert unsupported_features(provider.capabilities, _request()) == frozenset()
    with pytest.raises(ValueError, match="not configured"):
        registry.get(ProviderName.SALAD)
    with pytest.raises(ValueError, match="duplicate"):
        ProviderRegistry([provider, provider], default=ProviderName.RUNPOD)


def test_runpod_adapter_submit_result_and_cancel() -> None:
    async def scenario() -> None:
        provider = RunpodProvider(_Runpod())  # type: ignore[arg-type]
        ref = await provider.submit(_request())
        assert ref == ProviderJobRef(ProviderName.RUNPOD, "runpod-1")
        assert dict((await provider.result(ref)).metadata) == {"schema_version": 2}
        assert (await provider.cancel(ref)).value == "cancelled"

    asyncio.run(scenario())

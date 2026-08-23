from __future__ import annotations

import asyncio

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.providers.base import ProviderName


def test_salad_default_keeps_configured_runpod_available_for_historical_jobs(settings) -> None:
    values = settings.model_dump()
    values.update(
        inference_provider="salad",
        salad_api_key="salad-test-key",
        salad_organization="audio-org",
        salad_project="audio-project",
    )
    salad_settings = ServiceSettings(**values)
    engine = create_database_engine(salad_settings)
    initialize_database(engine)
    app = create_app(
        salad_settings,
        session_factory=create_session_factory(engine),
        worker=object(),
    )
    try:
        assert app.state.provider_registry.default is ProviderName.SALAD
        assert (
            app.state.provider_registry.get(ProviderName.SALAD).capabilities.name
            is ProviderName.SALAD
        )
        assert (
            app.state.provider_registry.get(ProviderName.RUNPOD).capabilities.name
            is ProviderName.RUNPOD
        )
    finally:

        async def close() -> None:
            for provider in app.state.provider_registry.providers:
                target = getattr(provider, "client", provider)
                method = getattr(target, "aclose", None)
                if method is not None:
                    await method()

        asyncio.run(close())
        engine.dispose()

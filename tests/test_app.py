from __future__ import annotations

import asyncio

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobType
from ace_service.providers.base import ProviderName
from ace_service.repository import create_job


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


def test_nonterminal_persisted_fal_backend_remains_recoverable_but_unselectable(
    settings,
) -> None:
    values = settings.model_dump()
    values.update(
        inference_enabled_backends="runpod/ace-step-v15-xl-turbo",
        default_original_backend="runpod/ace-step-v15-xl-turbo",
        default_cover_backend="runpod/ace-step-v15-xl-turbo",
        fal_key="test-fal-key",
    )
    fal_settings = ServiceSettings(**values)
    engine = create_database_engine(fal_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    backend_id = "fal/cassetteai/music-generator"
    with factory() as session:
        create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id="persisted-fal-job",
            inference_provider=ProviderName.FAL,
            inference_backend=backend_id,
            normalized_request_json={"schema_version": 2, "task_type": "original"},
            backend_snapshot_json={
                "backend_id": backend_id,
                "provider": ProviderName.FAL.value,
                "label": "fal.ai · CassetteAI Music Generator",
            },
        )
        session.commit()
    app = create_app(fal_settings, session_factory=factory, worker=object())
    try:
        assert (
            app.state.provider_registry.get_persisted(backend_id).capabilities.backend_id
            == backend_id
        )
        selectable = {
            str(provider.capabilities.backend_id)
            for provider in app.state.provider_registry.selectable("text_to_music")
        }
        assert backend_id not in selectable
    finally:

        async def close() -> None:
            for provider in app.state.provider_registry.providers:
                target = getattr(provider, "client", provider)
                method = getattr(target, "aclose", None)
                if method is not None:
                    await method()
            pricing = getattr(app.state, "fal_pricing", None)
            if pricing is not None:
                await pricing.aclose()

        asyncio.run(close())
        engine.dispose()

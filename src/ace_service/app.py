"""Application factory and lifecycle for the private Hetzner controller UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from ace_service.cleanup import cleanup_controller, cleanup_loop_interval
from ace_service.config import ServiceSettings
from ace_service.db import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
    ensure_schema_readiness,
    initialize_database,
)
from ace_service.home_ingest import HomeIngestClient
from ace_service.providers.base import ProviderName
from ace_service.providers.registry import ProviderRegistry
from ace_service.providers.runpod import RunpodProvider
from ace_service.providers.salad import SaladProvider
from ace_service.runpod_client import RunpodClient
from ace_service.web import register_web_routes
from ace_service.worker import ControllerWorker, RunpodWorkerClient

LOGGER = logging.getLogger(__name__)


async def _periodic_cleanup(app: FastAPI) -> None:
    settings: ServiceSettings = app.state.settings
    session_factory = app.state.session_factory
    while True:
        try:
            await asyncio.to_thread(cleanup_controller, settings, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "cleanup failed stage=cleanup exception_class=%s",
                type(exc).__name__,
                extra={"component": "controller"},
            )
        await asyncio.sleep(cleanup_loop_interval(settings))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: ServiceSettings = app.state.settings
    from ace_service.logging_config import configure_logging

    configure_logging(settings)
    try:
        await asyncio.to_thread(cleanup_controller, settings, app.state.session_factory)
    except Exception as exc:
        LOGGER.error(
            "startup cleanup failed stage=cleanup exception_class=%s",
            type(exc).__name__,
            extra={"component": "controller"},
        )
    cleanup_task = asyncio.create_task(_periodic_cleanup(app), name="ace-controller-cleanup")
    app.state.cleanup_task = cleanup_task
    worker = app.state.worker
    try:
        if hasattr(worker, "start"):
            await worker.start()
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        if hasattr(worker, "stop"):
            await worker.stop()
        clients = [app.state.home_ingest_client]
        clients.extend(
            getattr(provider, "client", provider)
            for provider in app.state.provider_registry.providers
        )
        for client in clients:
            close = getattr(client, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()
        engine = app.state.engine
        if engine is not None:
            engine.dispose()


def create_app(
    settings: ServiceSettings | None = None,
    *,
    session_factory: SessionFactory | None = None,
    runpod_client: RunpodWorkerClient | None = None,
    provider_registry: ProviderRegistry | None = None,
    home_ingest_client: Any | None = None,
    worker: Any | None = None,
) -> FastAPI:
    """Create the authenticated main app; the public transfer app is separate."""

    resolved_settings = settings or ServiceSettings()
    resolved_settings.ensure_data_layout()
    engine = None
    if session_factory is None:
        engine = create_database_engine(resolved_settings)
        database_path = Path(engine.url.database or "")
        if database_path.is_file():
            # An existing database is never touched by the foundation creator:
            # readiness must already be exact before create_all could add any
            # table, so startup refuses instead of silently migrating.
            ensure_schema_readiness(engine)
        initialize_database(engine)
        # Normal startup never migrates: refuse every schema state except the
        # exact expected version.  Tests that inject their own session factory
        # own their schema and skip this production-path guard.
        ensure_schema_readiness(engine)
        session_factory = create_session_factory(engine)

    resolved_runpod = runpod_client
    if provider_registry is None:
        providers: list[Any] = []
        if runpod_client is not None:
            providers.append(RunpodProvider(cast(Any, runpod_client)))
        elif resolved_settings.inference_provider == ProviderName.RUNPOD.value or all(
            value.strip().lower() not in {"", "change-me", "changeme", "replace-me", "replace_me"}
            for value in (
                resolved_settings.runpod_api_key,
                resolved_settings.runpod_endpoint_id,
            )
        ):
            resolved_runpod = RunpodClient.from_settings(resolved_settings)
            providers.append(RunpodProvider(cast(Any, resolved_runpod)))
        if resolved_settings.inference_provider == ProviderName.SALAD.value:
            assert resolved_settings.salad_api_key is not None
            assert resolved_settings.salad_organization is not None
            assert resolved_settings.salad_project is not None
            providers.append(
                SaladProvider(
                    resolved_settings.salad_api_key,
                    resolved_settings.salad_organization,
                    resolved_settings.salad_project,
                    resolved_settings.salad_queue_name,
                    resolved_settings.salad_container_group_name,
                    connect_timeout=resolved_settings.salad_connect_timeout_seconds,
                    read_timeout=resolved_settings.salad_read_timeout_seconds,
                    write_timeout=resolved_settings.salad_write_timeout_seconds,
                    pool_timeout=resolved_settings.salad_pool_timeout_seconds,
                )
            )
        provider_registry = ProviderRegistry(
            providers, default=ProviderName(resolved_settings.inference_provider)
        )
    resolved_home = home_ingest_client or HomeIngestClient(resolved_settings)
    resolved_worker = worker or ControllerWorker(
        resolved_settings,
        session_factory,
        provider_registry,
        home_ingest_client=resolved_home,
    )

    app = FastAPI(
        title="ACE Service",
        root_path=resolved_settings.service_root_path,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.runpod_client = resolved_runpod
    app.state.provider_registry = provider_registry
    app.state.home_ingest_client = resolved_home
    app.state.worker = resolved_worker
    app.state.cleanup_task = None
    register_web_routes(app)
    return app

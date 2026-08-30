"""Application factory and lifecycle for the private Hetzner controller UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from sqlalchemy import select

from ace_service.capacity.controller import CapacityController
from ace_service.capacity.registry import build_capacity_registry
from ace_service.cleanup import cleanup_controller, cleanup_loop_interval
from ace_service.config import ServiceSettings
from ace_service.costs import FalPricingClient
from ace_service.db import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
    ensure_schema_readiness,
    initialize_database,
)
from ace_service.home_ingest import HomeIngestClient
from ace_service.models import Job, JobStatus
from ace_service.notifications import NotificationDispatcher
from ace_service.providers.base import BackendOperation, ProviderName
from ace_service.providers.fal import FalProvider, FalQueueTransport
from ace_service.providers.fal_catalog import load_catalog
from ace_service.providers.mock import MockProvider
from ace_service.providers.node import NodeProvider
from ace_service.providers.registry import BackendRegistry
from ace_service.providers.runpod import RunpodProvider
from ace_service.providers.salad import SaladProvider
from ace_service.runpod_client import RunpodClient
from ace_service.source_assets import SourceIngestCoordinator
from ace_service.web import register_web_routes
from ace_service.worker import ControllerWorker, RunpodWorkerClient

LOGGER = logging.getLogger(__name__)


def _nonterminal_backend_ids(session_factory: SessionFactory) -> set[str]:
    """Return persisted backend identities that must remain recoverable."""

    with session_factory() as session:
        values = session.scalars(
            select(Job.inference_backend).where(
                Job.inference_backend.is_not(None),
                Job.status.not_in((JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)),
            )
        )
        return {str(value) for value in values if value}


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
    capacity_controller = app.state.capacity_controller
    await capacity_controller.start()
    notification_dispatcher = app.state.notification_dispatcher
    await notification_dispatcher.start()
    source_coordinator = app.state.source_coordinator
    await source_coordinator.start()
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
        await source_coordinator.stop()
        await capacity_controller.stop()
        await notification_dispatcher.stop()
        clients = [app.state.home_ingest_client]
        clients.extend(
            getattr(provider, "client", provider)
            for provider in app.state.provider_registry.providers
        )
        clients.extend(app.state.provider_registry.closeable_transports)
        clients.extend(
            getattr(manager, "_client", manager) for manager in app.state.capacity_registry.managers
        )
        if app.state.fal_pricing is not None:
            clients.append(app.state.fal_pricing)
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
    provider_registry: BackendRegistry | None = None,
    home_ingest_client: Any | None = None,
    worker: Any | None = None,
    source_coordinator: SourceIngestCoordinator | None = None,
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
        enabled = set(resolved_settings.enabled_backend_ids)
        persisted = _nonterminal_backend_ids(cast(SessionFactory, session_factory))
        configured = enabled | persisted
        selectable = set(enabled)
        if runpod_client is not None and "runpod/ace-step-v15-xl-turbo" in configured:
            providers.append(RunpodProvider(cast(Any, runpod_client)))
        elif "runpod/ace-step-v15-xl-turbo" in configured or all(
            value.strip().lower() not in {"", "change-me", "changeme", "replace-me", "replace_me"}
            for value in (
                resolved_settings.runpod_api_key,
                resolved_settings.runpod_endpoint_id,
            )
        ):
            resolved_runpod = RunpodClient.from_settings(resolved_settings)
            providers.append(RunpodProvider(cast(Any, resolved_runpod)))
        if "salad/ace-step-v15-xl-turbo" in configured:
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
        if any(item.startswith("fal/") for item in configured):
            if not resolved_settings.fal_key:
                raise ValueError("FAL_KEY is required while a persisted Fal job is nonterminal")
            catalog = load_catalog(resolved_settings.fal_catalog_path)
            transport = FalQueueTransport(
                resolved_settings.fal_key or "",
                output_retention_seconds=resolved_settings.fal_output_retention_seconds,
                connect_timeout=resolved_settings.fal_connect_timeout_seconds,
                read_timeout=resolved_settings.fal_read_timeout_seconds,
                write_timeout=resolved_settings.fal_write_timeout_seconds,
                pool_timeout=resolved_settings.fal_pool_timeout_seconds,
            )
            for backend_id in configured:
                if not backend_id.startswith("fal/"):
                    continue
                descriptor = catalog.by_backend_id(backend_id)
                if (
                    backend_id in persisted
                    or descriptor.media_kind.value
                    in resolved_settings.fal_allowed_media_kind_values
                ):
                    providers.append(FalProvider(descriptor, transport))
                elif backend_id in selectable:
                    selectable.remove(backend_id)
        if "mock/midi-sequential" in configured:
            # Persisted nonterminal mock jobs must remain recoverable even if
            # the backend was removed from the selectable list after restart.
            resolved_settings.validate_mock_runtime()
            providers.append(
                MockProvider(
                    resolved_settings.mock_base_url,
                    resolved_settings.mock_token,
                    connect_timeout=resolved_settings.mock_connect_timeout_seconds,
                    read_timeout=resolved_settings.mock_read_timeout_seconds,
                    write_timeout=resolved_settings.mock_write_timeout_seconds,
                    pool_timeout=resolved_settings.mock_pool_timeout_seconds,
                )
            )
        if "node/ace-step-v15-xl-turbo" in configured:
            resolved_settings.validate_node_runtime()
            providers.append(
                NodeProvider(
                    resolved_settings.ace_node_base_url,
                    resolved_settings.ace_node_token,
                    connect_timeout=resolved_settings.ace_node_connect_timeout_seconds,
                    read_timeout=resolved_settings.ace_node_read_timeout_seconds,
                    write_timeout=resolved_settings.ace_node_write_timeout_seconds,
                    pool_timeout=resolved_settings.ace_node_pool_timeout_seconds,
                )
            )
        configured_provider_names = {provider.capabilities.name for provider in providers}
        legacy_default: ProviderName | None = ProviderName(resolved_settings.inference_provider)
        if legacy_default not in configured_provider_names:
            legacy_default = None
        provider_registry = BackendRegistry(
            providers,
            default=legacy_default,
            defaults={
                BackendOperation.TEXT_TO_MUSIC.value: resolved_settings.default_original_backend,
                BackendOperation.AUDIO_TRANSFORM.value: resolved_settings.default_cover_backend,
            },
            selectable_backends=selectable,
        )
    capacity_registry = build_capacity_registry(resolved_settings)
    resolved_home = home_ingest_client or HomeIngestClient(resolved_settings)
    resolved_source_coordinator = source_coordinator or SourceIngestCoordinator(
        resolved_settings, cast(SessionFactory, session_factory), resolved_home
    )
    resolved_pricing = None
    if resolved_settings.fal_key and any(
        str(provider.capabilities.backend_id).startswith("fal/")
        for provider in provider_registry.providers
    ):
        resolved_pricing = FalPricingClient(resolved_settings.fal_key)
    capacity_controller = CapacityController(resolved_settings, session_factory, capacity_registry)
    resolved_worker = worker or ControllerWorker(
        resolved_settings,
        session_factory,
        provider_registry,
        home_ingest_client=resolved_home,
        capacity_registry=capacity_registry,
        home_ingest_semaphore=resolved_source_coordinator.home_ingest_semaphore,
    )
    if hasattr(resolved_worker, "capacity_controller"):
        resolved_worker.capacity_controller = capacity_controller
    if hasattr(resolved_worker, "capacity_registry"):
        resolved_worker.capacity_registry = capacity_registry

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
    app.state.capacity_registry = capacity_registry
    app.state.capacity_controller = capacity_controller
    app.state.notification_dispatcher = NotificationDispatcher(session_factory, resolved_settings)
    app.state.fal_pricing = resolved_pricing
    app.state.fal_health_cache = {}
    app.state.home_ingest_client = resolved_home
    app.state.worker = resolved_worker
    app.state.source_coordinator = resolved_source_coordinator
    app.state.cleanup_task = None
    register_web_routes(app)
    return app

"""Application factory and lifecycle for the private Hetzner controller UI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI

from ace_service.config import ServiceSettings
from ace_service.db import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from ace_service.home_ingest import HomeIngestClient
from ace_service.runpod_client import RunpodClient
from ace_service.web import register_web_routes
from ace_service.worker import ControllerWorker, RunpodWorkerClient


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = app.state.worker
    if hasattr(worker, "start"):
        await worker.start()
    try:
        yield
    finally:
        if hasattr(worker, "stop"):
            await worker.stop()
        for client in (app.state.runpod_client, app.state.home_ingest_client):
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
    home_ingest_client: Any | None = None,
    worker: Any | None = None,
) -> FastAPI:
    """Create the authenticated main app; the public transfer app is separate."""

    resolved_settings = settings or ServiceSettings()
    resolved_settings.ensure_data_layout()
    engine = None
    if session_factory is None:
        engine = create_database_engine(resolved_settings)
        initialize_database(engine)
        session_factory = create_session_factory(engine)

    resolved_runpod = runpod_client or RunpodClient.from_settings(resolved_settings)
    resolved_home = home_ingest_client or HomeIngestClient(resolved_settings)
    resolved_worker = worker or ControllerWorker(
        resolved_settings,
        session_factory,
        resolved_runpod,
        home_ingest_client=resolved_home,
    )

    app = FastAPI(
        title="ACE Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.runpod_client = resolved_runpod
    app.state.home_ingest_client = resolved_home
    app.state.worker = resolved_worker
    register_web_routes(app)
    return app

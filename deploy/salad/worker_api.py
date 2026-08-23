"""Local HTTP adapter used by the SaladCloud Job Queue Worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from runpod_worker.handler import configure_runtime, handler
from runpod_worker.runtime import WorkerRuntime, initialize_runtime

RuntimeInitializer = Callable[[], WorkerRuntime]
RuntimeConfigurer = Callable[[WorkerRuntime], None]
RequestHandler = Callable[[Mapping[str, Any]], dict[str, Any]]


def create_app(
    *,
    runtime_initializer: RuntimeInitializer = initialize_runtime,
    runtime_configurer: RuntimeConfigurer = configure_runtime,
    request_handler: RequestHandler = handler,
) -> FastAPI:
    """Create one fail-closed Salad worker application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime_ready = False
        runtime = await asyncio.to_thread(runtime_initializer)
        runtime_configurer(runtime)
        app.state.runtime_ready = True
        try:
            yield
        finally:
            app.state.runtime_ready = False

    application = FastAPI(title="AudioVentura Salad worker", lifespan=lifespan)
    application.state.runtime_ready = False

    @application.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/ready")
    async def ready() -> dict[str, str]:
        if not application.state.runtime_ready:
            raise HTTPException(status_code=503, detail="runtime is not ready")
        return {"status": "ready"}

    @application.post("/process")
    async def process(payload: dict[str, Any]) -> dict[str, Any]:
        if not application.state.runtime_ready:
            raise HTTPException(status_code=503, detail="runtime is not ready")
        return await asyncio.to_thread(request_handler, {"input": payload})

    return application


app = create_app()

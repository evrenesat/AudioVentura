"""Bearer-authenticated FastAPI surface for the private mock service."""

from __future__ import annotations

import json
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import MAX_REQUEST_BYTES, MockSettings
from .corpus import CorpusManifest
from .db import DatabaseError, MockDatabase, SubmissionConflict
from .transfer import TransferError
from .worker import MockWorker, SubmissionError, parse_submission


def _authorized(request: Request, settings: MockSettings) -> None:
    value = request.headers.get("authorization", "")
    expected = f"Bearer {settings.token}"
    if value != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _safe_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if str(parsed) != value.lower():
        raise HTTPException(status_code=404, detail="job not found")
    return str(parsed)


def _job_response(worker: MockWorker, external_uuid: str) -> JSONResponse:
    job = worker.database.get(external_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(job.response())


def create_app(
    settings: MockSettings,
    manifest: CorpusManifest,
    *,
    database: MockDatabase | None = None,
    worker: MockWorker | None = None,
) -> FastAPI:
    """Build an app; dependency injection keeps the API tests offline."""

    resolved_database = database or MockDatabase(settings.database_path, manifest)
    resolved_worker = worker or MockWorker(settings, manifest, resolved_database)
    app = FastAPI(title="AudioVentura MIDI mock", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.manifest = manifest
    app.state.database = resolved_database
    app.state.worker = resolved_worker

    @app.on_event("startup")
    async def startup() -> None:
        await resolved_worker.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await resolved_worker.stop()

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        _authorized(request, settings)
        return JSONResponse(
            {
                "status": "ok",
                "corpus": {
                    "archive_sha256": manifest.archive_sha256,
                    "member_count": manifest.member_count,
                    "manifest_sha256": manifest.manifest_sha256,
                },
                "cursor": resolved_database.cursor_snapshot(),
                "queue_depth": resolved_worker.queue_depth,
                "running": resolved_worker.running_id is not None,
            }
        )

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request) -> JSONResponse:
        _authorized(request, settings)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail="request is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request is too large")
        try:
            payload = json.loads(body)
            submission = parse_submission(payload, max_output_bytes=settings.max_output_bytes)
            job, created = resolved_worker.submit(submission)
        except SubmissionConflict as exc:
            raise HTTPException(status_code=409, detail="submission nonce conflicts") from exc
        except (SubmissionError, DatabaseError, TransferError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid submission") from exc
        response = job.response()
        response["created"] = created
        return JSONResponse(response, status_code=202)

    @app.get("/v1/jobs/{external_uuid}")
    async def job_status(request: Request, external_uuid: str) -> JSONResponse:
        _authorized(request, settings)
        return _job_response(resolved_worker, _safe_uuid(external_uuid))

    @app.get("/v1/jobs/{external_uuid}/result")
    async def result(request: Request, external_uuid: str) -> JSONResponse:
        _authorized(request, settings)
        normalized = _safe_uuid(external_uuid)
        job = resolved_worker.database.get(normalized)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.state != "succeeded":
            raise HTTPException(status_code=409, detail="job is not complete")
        return JSONResponse(
            {
                "job_id": job.external_uuid,
                "status": "succeeded",
                "metadata": resolved_worker.metadata(job),
            }
        )

    @app.post("/v1/jobs/{external_uuid}/cancel")
    async def cancel(request: Request, external_uuid: str) -> JSONResponse:
        _authorized(request, settings)
        job, outcome = resolved_worker.cancel(_safe_uuid(external_uuid))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JSONResponse({"job_id": job.external_uuid, "status": job.state, "outcome": outcome})

    return app

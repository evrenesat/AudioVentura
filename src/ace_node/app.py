"""Bearer-authenticated HTTP API for the persistent ACE Node."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from runpod_worker.schemas import WorkerRequest

from .config import NodeSettings
from .db import NodeDatabaseError, SubmissionConflict
from .worker import NodeWorker

MAX_BODY_BYTES = 65_536
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _authorize(request: Request, settings: NodeSettings) -> None:
    actual = request.headers.get("authorization", "")
    expected = "Bearer " + settings.require_token()
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value.lower()):
        raise ValueError(f"{field} must be a UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    return str(parsed)


def _parse_submission(body: Any, settings: NodeSettings) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        raise ValueError("submission must be an object")
    expected = {
        "schema_version",
        "application_job_id",
        "variation_index",
        "submission_nonce",
        "input",
        "source",
        "result_upload",
    }
    if set(body) != expected:
        raise ValueError("submission fields do not match")
    if body.get("schema_version") != 2:
        raise ValueError("schema_version must be 2")
    application_job_id = _canonical_uuid(body.get("application_job_id"), "application_job_id")
    submission_nonce = _canonical_uuid(body.get("submission_nonce"), "submission_nonce")
    variation_index = body.get("variation_index")
    if (
        isinstance(variation_index, bool)
        or not isinstance(variation_index, int)
        or not 1 <= variation_index <= 4
    ):
        raise ValueError("variation_index must be between 1 and 4")
    raw_input = body.get("input")
    if not isinstance(raw_input, Mapping):
        raise ValueError("input must be an object")
    try:
        parsed = WorkerRequest.from_mapping(
            raw_input,
            allowed_transfer_host=settings.transfer_allowed_host,
        )
    except Exception as exc:
        raise ValueError("input does not satisfy worker schema 2") from exc
    if parsed.schema_version != 2:
        raise ValueError("input schema_version must be 2")
    if parsed.job_id != application_job_id:
        raise ValueError("input job identity does not match application job")
    if parsed.variation_index != variation_index:
        raise ValueError("input variation does not match submission")
    if parsed.submission_nonce != submission_nonce:
        raise ValueError("input nonce does not match submission")
    source = body.get("source")
    input_source = raw_input.get("source")
    if source != input_source:
        raise ValueError("source envelope does not match input")
    upload = body.get("result_upload")
    input_upload = raw_input.get("result_upload")
    if upload != input_upload or not isinstance(upload, Mapping):
        raise ValueError("result upload envelope does not match input")
    max_bytes = upload.get("max_bytes")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes > settings.max_output_bytes
    ):
        raise ValueError("result upload exceeds node output limit")
    return {
        "schema_version": 2,
        "application_job_id": application_job_id,
        "variation_index": variation_index,
        "submission_nonce": submission_nonce,
        "input": dict(raw_input),
        "source": source,
        "result_upload": dict(upload),
    }


def _response(payload: Mapping[str, Any], *, status_code: int = 200) -> JSONResponse:
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BODY_BYTES:
        raise HTTPException(status_code=500, detail="response is too large")
    return JSONResponse(dict(payload), status_code=status_code)


async def _read_bounded_body(request: Request) -> bytes:
    """Read at most the protocol body limit, including chunked requests."""

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_job_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if str(parsed) != value.lower():
        raise HTTPException(status_code=404, detail="job not found")
    return str(parsed)


def create_app(
    settings: NodeSettings | None = None,
    *,
    database: Any | None = None,
    worker: NodeWorker | None = None,
    runtime_factory: Any | None = None,
    executor: Any | None = None,
) -> FastAPI:
    """Create a node app; runtime injection keeps tests GPU-free."""

    resolved_settings = settings or NodeSettings()
    # Model-bundle preparation may reuse settings without a node bearer
    # secret; an HTTP service may never start in that state.
    resolved_settings.require_token()
    resolved_database = database
    resolved_worker = worker or NodeWorker(
        resolved_settings,
        resolved_database,
        runtime_factory=runtime_factory,
        executor=executor,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(resolved_worker.start)
        try:
            yield
        finally:
            await asyncio.to_thread(resolved_worker.stop)

    app = FastAPI(
        title="AudioVentura ACE Node",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_worker.database
    app.state.worker = resolved_worker

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        _authorize(request, resolved_settings)
        return _response(resolved_worker.health())

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request) -> JSONResponse:
        _authorize(request, resolved_settings)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="request is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
        raw = await _read_bounded_body(request)
        try:
            parsed = _parse_submission(json.loads(raw), resolved_settings)
            job, created = resolved_worker.submit(parsed)
        except SubmissionConflict:
            raise HTTPException(status_code=409, detail="submission nonce conflicts") from None
        except RuntimeError as exc:
            if str(exc) == "node runtime is not ready":
                raise HTTPException(status_code=503, detail="node runtime is not ready") from None
            raise HTTPException(status_code=400, detail="invalid submission") from None
        except (json.JSONDecodeError, ValueError, NodeDatabaseError):
            raise HTTPException(status_code=400, detail="invalid submission") from None
        return _response(job.response(created=created), status_code=202)

    @app.get("/v1/jobs/{external_uuid}")
    async def job_status(request: Request, external_uuid: str) -> JSONResponse:
        _authorize(request, resolved_settings)
        job = resolved_worker.get(_safe_job_id(external_uuid))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _response(job.response())

    @app.get("/v1/jobs/{external_uuid}/result")
    async def result(request: Request, external_uuid: str) -> JSONResponse:
        _authorize(request, resolved_settings)
        job = resolved_worker.get(_safe_job_id(external_uuid))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.state != "succeeded":
            raise HTTPException(status_code=409, detail="job is not complete")
        metadata = resolved_worker.result(job)
        if metadata is None:
            raise HTTPException(status_code=409, detail="job result is unavailable")
        return _response({"job_id": job.job_id, "status": "succeeded", "metadata": metadata})

    @app.post("/v1/jobs/{external_uuid}/cancel")
    async def cancel(request: Request, external_uuid: str) -> JSONResponse:
        _authorize(request, resolved_settings)
        job, outcome = resolved_worker.cancel(_safe_job_id(external_uuid))
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _response({"job_id": job.job_id, "status": job.state, "outcome": outcome})

    return app

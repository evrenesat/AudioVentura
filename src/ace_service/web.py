"""Authenticated HTML, JSON status, readiness, and media routes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, text

from ace_service.auth import (
    attach_csrf_cookie,
    csrf_token,
    parse_form,
    require_basic_auth,
    require_csrf,
)
from ace_service.campaign import CampaignError, CampaignStore
from ace_service.config import ServiceSettings
from ace_service.costs import (
    apply_conservative_margin,
    build_cost_estimate_view,
    compute_submission_quote,
    select_highest_exact_rate_gpu,
    utc_now,
)
from ace_service.cover import (
    CoverSourceError,
    discard_staged_cover_source,
    remove_cover_source,
    stage_cover_continuation_source,
)
from ace_service.models import (
    PROJECT_TITLE_MAX_LENGTH,
    Job,
    JobStatus,
    JobType,
    Output,
    OutputFormat,
    Project,
    SubmissionQuote,
    VariationAttempt,
)
from ace_service.repository import (
    cancel_cover_staging,
    confirm_cover_job,
    create_cover_job,
    create_original_job,
    create_submission_quote,
    finalize_cover_job_duration,
    get_job,
    get_latest_gpu_rates,
    get_matching_runtime_calibration,
    get_output,
    get_project,
    list_project_jobs,
    list_projects,
    recent_completed_attempt_execution_ms,
    rename_project,
    resolve_continuation_source,
    resolve_cover_continuation_output,
    transition_job,
)
from ace_service.schemas import CoverRequest, OriginalSongRequest, resolve_relative_path

_TEMPLATES = Path(__file__).with_name("templates")
_STATIC = Path(__file__).with_name("static")
_MIME_TYPES = {"audio/mpeg", "audio/flac", "audio/wav"}
_READINESS_PROBE_TIMEOUT_SECONDS = 5.0
_FORMAT_MIME = {
    OutputFormat.MP3: "audio/mpeg",
    OutputFormat.FLAC: "audio/flac",
    OutputFormat.WAV: "audio/wav",
}
_STATUS_LABELS = {
    JobStatus.QUEUED: "Queued",
    JobStatus.INGESTING: "Preparing source at home",
    JobStatus.STAGING: "Staging source",
    JobStatus.CLOUD_QUEUED: "Waiting for cloud GPU",
    JobStatus.GENERATING: "Generating",
    JobStatus.COMPLETED: "Completed",
    JobStatus.FAILED: "Failed",
}
_PHASE_LABELS = {
    "cloud_wait": "Waiting for GPU/model cache",
    "worker_initializing": "Starting GPU worker and loading model",
    "worker_running": "Worker started",
    "source_download": "Transferring source audio",
    "generation": "Generating audio",
    "finalizing": "Finalizing audio",
    "output_upload": "Uploading result",
}


def capture_submission_quote(app: FastAPI, session: Any, job: Job) -> SubmissionQuote | None:
    """Compute and persist the server-owned quote in the acceptance transaction.

    Never accepted from the form; an unconfirmed cover staging record gets no
    quote.  With no calibration observations or fresh rates the quote is
    unavailable with a bounded reason and generation still proceeds.
    """

    normalized = job.normalized_request_json
    generation: Mapping[str, Any] = {}
    if isinstance(normalized, Mapping):
        raw_generation = normalized.get("generation")
        if isinstance(raw_generation, Mapping):
            generation = raw_generation
    profile_id = normalized.get("profile_id") if isinstance(normalized, Mapping) else None
    duration_mode = generation.get("duration_mode")
    duration_value = generation.get("duration_seconds")
    eligible_gpu_ids = list(app.state.settings.eligible_gpu_ids)
    captured_at = utc_now()
    latest_rates = get_latest_gpu_rates(session, eligible_gpu_ids)
    now = utc_now()
    fresh_rates = {
        gpu_id: row.rate_micro_usd_per_hour
        for gpu_id, row in latest_rates.items()
        if row.expires_at > now
    }
    fresh_rate_usd = {
        gpu_id: row.hourly_rate_usd for gpu_id, row in latest_rates.items() if row.expires_at > now
    }
    stale_gpu_ids = {gpu_id for gpu_id, row in latest_rates.items() if row.expires_at <= now}
    highest_gpu = (
        select_highest_exact_rate_gpu(
            eligible_gpu_ids=eligible_gpu_ids,
            fresh_rates=fresh_rates,
            fresh_rate_usd=fresh_rate_usd,
        )
        if fresh_rates and all(gpu_id in fresh_rates for gpu_id in eligible_gpu_ids)
        else None
    )
    model_identity = app.state.settings.acestep_model
    runtime_identity = app.state.settings.runpod_worker_runtime_identity
    calibration = None
    if (
        highest_gpu is not None
        and runtime_identity is not None
        and isinstance(profile_id, str)
        and isinstance(duration_mode, str)
    ):
        calibration = get_matching_runtime_calibration(
            session,
            task_mode=job.job_type.value,
            profile_id=profile_id,
            model_identity=model_identity,
            runtime_identity=runtime_identity,
            gpu_class=highest_gpu,
            duration_mode=duration_mode,
            duration_value_seconds=(
                float(duration_value) if isinstance(duration_value, (int, float)) else None
            ),
            output_count=job.variation_count,
        )
    predicted_range = (
        apply_conservative_margin(
            (calibration.execution_low_ms, calibration.execution_high_ms),
            calibration.conservative_margin,
        )
        if calibration is not None
        else None
    )
    estimate = compute_submission_quote(
        profile_id=profile_id if isinstance(profile_id, str) else None,
        duration_mode=duration_mode if isinstance(duration_mode, str) else None,
        duration_value_seconds=(
            float(duration_value) if isinstance(duration_value, (int, float)) else None
        ),
        variation_count=job.variation_count,
        eligible_gpu_ids=eligible_gpu_ids,
        fresh_rates=fresh_rates,
        fresh_rate_usd=fresh_rate_usd,
        stale_gpu_ids=stale_gpu_ids,
        calibration_version=calibration.version if calibration is not None else None,
        predicted_execution_range_ms=predicted_range,
        rate_source=(latest_rates[highest_gpu].source if highest_gpu is not None else None),
        rate_version=(
            str(latest_rates[highest_gpu].calibration_version) if highest_gpu is not None else None
        ),
        captured_at=captured_at,
        model_identity=model_identity,
    )
    return create_submission_quote(session, job.id, estimate, captured_at=captured_at)


def _cost_estimate_view(
    session: Any, job_type: JobType, *, variation_count: int
) -> dict[str, Any] | None:
    """Read-only approximate cost estimate for one request kind.

    Computed on read from completed attempt history at the fixed
    ``USD 0.50/GPU-hour`` rate.  Never persisted and never consulted by the
    submission path; any query or computation failure is bounded to ``None``
    so generation continues unchanged.  The returned view also carries the
    server-computed request label for every supported variation count, so the
    form can bind the visible total to the selected value with no client-side
    money arithmetic.
    """

    try:
        samples = recent_completed_attempt_execution_ms(session, job_type=job_type)
        kind_label = "original songs" if job_type is JobType.ORIGINAL else "covers"
        view = build_cost_estimate_view(
            execution_ms_samples=samples,
            variation_count=variation_count,
            kind_label=kind_label,
        )
        supported_counts = list(range(1, 5))
        view["variation_counts"] = supported_counts
        view["variation_request_labels"] = {
            count: build_cost_estimate_view(
                execution_ms_samples=samples, variation_count=count, kind_label=kind_label
            )["request_estimate_label"]
            for count in supported_counts
        }
        return view
    except Exception:
        return None


def _form_estimate(
    session: Any, job_type: JobType, form: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Read-only estimate for the variation count carried by one form.

    The count is read directly from the form (or the kind's default when
    absent), so the visible estimate always matches the value the form will
    submit — including continuation forms whose values are already typed.
    Any missing, invalid, or out-of-range count, or any estimate failure,
    omits only the estimate; authentication, validation, and the submission
    transaction never depend on it.
    """

    try:
        raw = form.get("variation_count", "1")
        if isinstance(raw, bool):
            return None
        variation_count = int(raw)
        supported = (1, 2, 3, 4)
        if variation_count not in supported:
            return None
    except (TypeError, ValueError):
        return None
    return _cost_estimate_view(session, job_type, variation_count=variation_count)


def register_web_routes(app: FastAPI) -> None:
    """Mount only private controller routes on the main app."""

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    app.mount("/static", _static_app(_STATIC), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        request_path = request.scope["path"]
        root_path = request.scope.get("root_path", "")
        if root_path and request_path.startswith(f"{root_path}/"):
            request_path = request_path[len(root_path) :]
        response = cast(Response, await call_next(request))
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; media-src 'self'; object-src 'none'; "
            "script-src 'self'; style-src 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request_path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def authenticated(request: Request) -> None:
        settings: ServiceSettings = app.state.settings
        require_basic_auth(request, settings.service_username, settings.service_password)

    def render(
        request: Request,
        name: str,
        context: Mapping[str, Any],
        *,
        response_status: int = status.HTTP_200_OK,
    ) -> Response:
        token = csrf_token(request)
        response = templates.TemplateResponse(
            request=request,
            name=name,
            context={
                **context,
                "csrf_token": token,
                "urls": {
                    "dashboard": _route_path(request, "dashboard"),
                    "create": _route_path(request, "create_form"),
                    "cover": _route_path(request, "cover_form"),
                    "projects": _route_path(request, "projects"),
                    "jobs": _route_path(request, "jobs"),
                    "app_css": _route_path(request, "static", path="app.css"),
                    "status_js": _route_path(request, "static", path="status.js"),
                    "estimate_selector_js": _route_path(
                        request, "static", path="estimate_selector.js"
                    ),
                },
            },
            status_code=response_status,
        )
        attach_csrf_cookie(request, response, token)
        return response

    @app.get("/", dependencies=[Depends(authenticated)], name="dashboard")
    async def dashboard(request: Request) -> Response:
        with app.state.session_factory() as session:
            jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(20)))
            job_views = [_job_view(request, job) for job in jobs]
        readiness = await _readiness(app)
        return render(request, "dashboard.html", {"jobs": job_views, "readiness": readiness})

    @app.get("/create", dependencies=[Depends(authenticated)], name="create_form")
    async def create_form(request: Request) -> Response:
        estimate = None
        with app.state.session_factory() as session:
            estimate = _form_estimate(session, JobType.ORIGINAL, {})
        return render(
            request,
            "original_form.html",
            {"form": {}, "errors": [], "continuing": False, "estimate": estimate},
        )

    @app.post("/create", dependencies=[Depends(authenticated)], name="create_original")
    async def create_original(request: Request) -> Response:
        # TODO(re-enable): the private campaign maintenance gate is quarantined
        # during the usability recovery; restore the call after ordinary
        # original and cover generation is stable (owner decision #10).
        # _assert_public_enqueue_allowed(app)
        fields = await parse_form(request)
        require_csrf(request, fields)
        form = dict(fields)
        with app.state.session_factory() as session:
            continuation_source = _continuation_source(
                session, fields, expected_job_type=JobType.ORIGINAL
            )
            project_id = continuation_source.project_id if continuation_source is not None else None
            try:
                job_request = OriginalSongRequest(**_original_form_values(fields))
            except ValidationError as exc:
                errors = _validation_errors(exc)
                return render(
                    request,
                    "original_form.html",
                    {
                        "form": form,
                        "errors": errors,
                        "continuing": project_id is not None,
                        "project_title": (
                            continuation_source.project.title
                            if continuation_source is not None
                            else None
                        ),
                        "estimate": _form_estimate(session, JobType.ORIGINAL, fields),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            job = create_original_job(session, job_request, project=project_id)
            # TODO(re-enable): legacy submission-quote capture is disconnected
            # during the usability recovery; restore after ordinary original
            # and cover generation is stable. Quotes are inert historical data
            # and never gate generation.
            # capture_submission_quote(app, session, job)
            session.commit()
            job_id = job.id
        _enqueue(app, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/cover", dependencies=[Depends(authenticated)], name="cover_form")
    async def cover_form(request: Request) -> Response:
        readiness = await _readiness(app, only={"home_ingest"})
        estimate = None
        with app.state.session_factory() as session:
            estimate = _form_estimate(session, JobType.COVER, {})
        return render(
            request,
            "cover_form.html",
            {
                "form": {},
                "errors": [],
                "readiness": readiness,
                "continuing": False,
                "estimate": estimate,
            },
        )

    @app.post("/cover", dependencies=[Depends(authenticated)], name="create_cover")
    async def create_cover(request: Request) -> Response:
        # TODO(re-enable): the private campaign maintenance gate is quarantined
        # during the usability recovery; restore the call after ordinary
        # original and cover generation is stable (owner decision #10).
        # _assert_public_enqueue_allowed(app)
        fields = await parse_form(request)
        require_csrf(request, fields)
        form = dict(fields)
        with app.state.session_factory() as session:
            continuation_source = _continuation_source(
                session, fields, expected_job_type=JobType.COVER
            )
            project_id = continuation_source.project_id if continuation_source is not None else None
            request_values = _cover_form_values(fields)
            if continuation_source is not None:
                request_values["youtube_url"] = continuation_source.source_url
            try:
                cover_request = CoverRequest(**request_values)
            except ValidationError as exc:
                errors = _validation_errors(exc)
                return render(
                    request,
                    "cover_form.html",
                    {
                        "form": form,
                        "errors": errors,
                        "readiness": await _readiness(app, only={"home_ingest"}),
                        "continuing": project_id is not None,
                        "project_title": (
                            continuation_source.project.title
                            if continuation_source is not None
                            else None
                        ),
                        "estimate": _form_estimate(session, JobType.COVER, fields),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            job = create_cover_job(session, cover_request, project=project_id)
            job_id = job.id
            if continuation_source is not None:
                try:
                    source_output, source_duration = resolve_cover_continuation_output(
                        continuation_source
                    )
                    prepared = stage_cover_continuation_source(
                        app.state.settings,
                        continuation_source,
                        source_output,
                        source_duration_seconds=source_duration,
                        target_job_id=job.id,
                    )
                    transition_job(session, job.id, JobStatus.INGESTING)
                    job.source_url = prepared.canonical_url
                    job.sanitized_source_title = prepared.title
                    job.source_sha256 = prepared.prepared_sha256
                    job.source_byte_size = prepared.prepared_bytes
                    finalize_cover_job_duration(session, job.id, prepared.duration_seconds)
                    transition_job(session, job.id, JobStatus.STAGING)
                    confirm_cover_job(session, job.id)
                    normalized = dict(job.normalized_request_json or {})
                    normalized["continuation_source"] = {
                        "job_id": continuation_source.id,
                        "output_id": source_output.id,
                    }
                    job.normalized_request_json = normalized
                    session.commit()
                except CoverSourceError as exc:
                    session.rollback()
                    discard_staged_cover_source(app.state.settings, job_id)
                    return render(
                        request,
                        "cover_form.html",
                        {
                            "form": form,
                            "errors": [exc.message],
                            "readiness": await _readiness(app, only={"home_ingest"}),
                            "continuing": True,
                            "project_title": continuation_source.project.title,
                            "estimate": _form_estimate(session, JobType.COVER, fields),
                        },
                        response_status=status.HTTP_409_CONFLICT,
                    )
                except Exception:
                    session.rollback()
                    discard_staged_cover_source(app.state.settings, job_id)
                    raise
            else:
                session.commit()
        _enqueue(app, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/cover/{job_id}/confirm",
        dependencies=[Depends(authenticated)],
        name="confirm_cover",
    )
    async def confirm_cover(request: Request, job_id: str) -> Response:
        # TODO(re-enable): the private campaign maintenance gate is quarantined
        # during the usability recovery; restore the call after ordinary
        # original and cover generation is stable (owner decision #10).
        # _assert_public_enqueue_allowed(app)
        fields = await parse_form(request)
        require_csrf(request, fields)
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
            try:
                confirm_cover_job(session, job.id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            # TODO(re-enable): legacy submission-quote capture is disconnected
            # during the usability recovery; restore after ordinary original
            # and cover generation is stable. Quotes are inert historical data
            # and never gate generation.
            # capture_submission_quote(app, session, job)
            session.commit()
        _enqueue(app, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/cover/{job_id}/cancel",
        dependencies=[Depends(authenticated)],
        name="cancel_cover",
    )
    async def cancel_cover(request: Request, job_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
            try:
                cancel_cover_staging(session, job.id)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            session.commit()
        remove_cover_source(app.state.settings, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs", dependencies=[Depends(authenticated)], name="jobs")
    async def jobs(request: Request) -> Response:
        with app.state.session_factory() as session:
            job_views = [
                _job_view(request, job)
                for job in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(100))
            ]
        return render(request, "jobs.html", {"jobs": job_views})

    @app.get("/projects", dependencies=[Depends(authenticated)], name="projects")
    async def projects(request: Request) -> Response:
        with app.state.session_factory() as session:
            project_views = [_project_view(request, project) for project in list_projects(session)]
        return render(request, "projects.html", {"projects": project_views})

    @app.get(
        "/projects/{project_id}",
        dependencies=[Depends(authenticated)],
        name="project_detail",
    )
    async def project_detail(request: Request, project_id: str) -> Response:
        with app.state.session_factory() as session:
            context = _project_detail_context(request, session, project_id)
        return render(request, "project_detail.html", context)

    @app.post(
        "/projects/{project_id}/rename",
        dependencies=[Depends(authenticated)],
        name="rename_project",
    )
    async def rename_project_route(request: Request, project_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        title = fields.get("title", "")
        with app.state.session_factory() as session:
            if get_project(session, project_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="project not found",
                )
            try:
                rename_project(session, project_id, title)
            except ValueError as exc:
                context = _project_detail_context(
                    request,
                    session,
                    project_id,
                    rename_title=title,
                    rename_errors=[str(exc)],
                )
                return render(
                    request,
                    "project_detail.html",
                    context,
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            session.commit()
        return RedirectResponse(
            _route_path(request, "project_detail", project_id=project_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get(
        "/jobs/{job_id}/continue",
        dependencies=[Depends(authenticated)],
        name="continue_job",
    )
    async def continue_job(request: Request, job_id: str) -> Response:
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="job not found",
                )
            try:
                source = resolve_continuation_source(
                    session, job.id, expected_job_type=job.job_type
                )
                form = _continuation_form(source)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="job is not compatible with continuation",
                ) from exc
            project_title = source.project.title
        estimate = None
        with app.state.session_factory() as session:
            estimate = _form_estimate(session, source.job_type, form)
        if source.job_type is JobType.ORIGINAL:
            return render(
                request,
                "original_form.html",
                {
                    "form": form,
                    "errors": [],
                    "continuing": True,
                    "project_title": project_title,
                    "estimate": estimate,
                },
            )
        return render(
            request,
            "cover_form.html",
            {
                "form": form,
                "errors": [],
                "readiness": await _readiness(app, only={"home_ingest"}),
                "continuing": True,
                "project_title": project_title,
                "estimate": estimate,
            },
        )

    @app.get("/jobs/{job_id}", dependencies=[Depends(authenticated)], name="job_detail")
    async def job_detail(request: Request, job_id: str) -> Response:
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
            view = _job_view(request, job)
        return render(request, "job_detail.html", {"job": view})

    @app.get(
        "/jobs/{job_id}/status",
        dependencies=[Depends(authenticated)],
        name="job_status",
    )
    async def job_status(request: Request, job_id: str) -> JSONResponse:
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
            return JSONResponse(_job_view(request, job), headers={"Cache-Control": "no-store"})

    @app.get("/media/{output_id}", dependencies=[Depends(authenticated)], name="media")
    async def media(output_id: int) -> FileResponse:
        output = _verified_output(app.state.settings, app.state.session_factory, output_id)
        return FileResponse(
            output[0],
            media_type=output[1].mime_type,
            filename=f"ace-output-{output_id}{output[0].suffix.lower()}",
            content_disposition_type="inline",
        )

    @app.get(
        "/files/{output_id}/download",
        dependencies=[Depends(authenticated)],
        name="download",
    )
    async def download(output_id: int) -> FileResponse:
        path, record = _verified_output(app.state.settings, app.state.session_factory, output_id)
        return FileResponse(
            path,
            media_type=record.mime_type,
            filename=f"ace-output-{output_id}{path.suffix.lower()}",
            content_disposition_type="attachment",
        )

    @app.get("/healthz", dependencies=[Depends(authenticated)])
    async def healthz() -> JSONResponse:
        checks: dict[str, bool] = {"process": True, "database": False, "data_directories": False}
        try:
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            pass
        checks["data_directories"] = _writable_data_layout(app.state.settings)
        healthy = all(checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "error", "checks": checks},
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/readyz", dependencies=[Depends(authenticated)])
    async def readyz() -> JSONResponse:
        readiness = await _readiness(app)
        required_ok = all(
            readiness["components"][key]["ok"]
            for key in ("controller_database", "runpod_api", "public_transfer")
        )
        return JSONResponse(
            readiness,
            status_code=status.HTTP_200_OK if required_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def _static_app(directory: Path) -> Any:
    from starlette.staticfiles import StaticFiles

    return StaticFiles(directory=str(directory), check_dir=True)


def _enqueue(app: FastAPI, job_id: str) -> None:
    worker = app.state.worker
    try:
        worker.enqueue(job_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="controller worker is unavailable",
        ) from exc


def _assert_public_enqueue_allowed(app: FastAPI) -> None:
    """Fail closed while the private campaign maintenance gate is active."""

    settings: ServiceSettings = app.state.settings
    database = settings.campaign_database_path
    if not database.is_file():
        return
    try:
        store = CampaignStore.open_existing(database)
        if store is not None:
            store.require_submission_allowed()
    except CampaignError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ordinary submissions are temporarily paused for operator maintenance",
        ) from exc


def _original_form_values(fields: Mapping[str, str]) -> dict[str, Any]:
    return {
        "description": fields.get("description", ""),
        "lyrics": fields.get("lyrics") or None,
        "instrumental": fields.get("instrumental") in {"1", "true", "on", "yes"},
        "vocal_language": fields.get("vocal_language", "en"),
        "profile_id": fields.get("profile_id", "fast-beta-v1"),
        "prompt_mode": fields.get("prompt_mode", "direct"),
        "duration_mode": fields.get("duration_mode", "auto"),
        "duration_seconds": _optional_number(fields.get("duration_seconds")),
        "bpm": _optional_int(fields.get("bpm")),
        "key_scale": fields.get("key_scale") or None,
        "time_signature": _optional_int(fields.get("time_signature")),
        "seed": _optional_int(fields.get("seed")),
        "variation_count": _required_int(fields.get("variation_count", "1")),
        "output_format": fields.get("output_format", OutputFormat.MP3.value),
    }


def _cover_form_values(fields: Mapping[str, str]) -> dict[str, Any]:
    return {
        "youtube_url": fields.get("youtube_url", ""),
        "target_style": fields.get("target_style", ""),
        "remix_guidance": fields.get("remix_guidance") or None,
        "lyrics": fields.get("lyrics") or None,
        "profile_id": fields.get("profile_id", "fast-beta-v1"),
        "audio_cover_strength": _optional_number(fields.get("audio_cover_strength"), default=0.65),
        "cover_noise_strength": _optional_number(fields.get("cover_noise_strength"), default=0.0),
        "duration_mode": fields.get("duration_mode", "source"),
        "duration_seconds": _optional_number(fields.get("duration_seconds")),
        "variation_count": _required_int(fields.get("variation_count", "1")),
        "seed": _optional_int(fields.get("seed")),
        "output_format": fields.get("output_format", OutputFormat.MP3.value),
        "rights_confirmation": fields.get("rights_confirmation") in {"1", "true", "on", "yes"},
    }


def _continuation_source(
    session: Any,
    fields: Mapping[str, str],
    *,
    expected_job_type: JobType,
) -> Job | None:
    if "continue_from_job_id" not in fields:
        return None
    source_id = fields.get("continue_from_job_id", "").strip()
    if not source_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="continuation source not found",
        )
    try:
        source = resolve_continuation_source(
            session, source_id, expected_job_type=expected_job_type
        )
        _continuation_form(source)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="continuation source not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="continuation source is incompatible",
        ) from exc
    return source


def _continuation_form(job: Job) -> dict[str, Any]:
    normalized = job.normalized_request_json
    if not isinstance(normalized, dict):
        raise ValueError("normalized request must be an object")
    if normalized.get("schema_version") != 2 or normalized.get("task_type") != job.job_type.value:
        raise ValueError("normalized request is not compatible with the job type")
    generation = normalized.get("generation")
    if not isinstance(generation, dict):
        raise ValueError("normalized generation must be an object")
    profile_id = normalized.get("profile_id")
    if not isinstance(profile_id, str):
        raise ValueError("normalized profile must be text")

    common = {
        "profile_id": profile_id,
        "variation_count": job.variation_count,
        "continue_from_job_id": job.id,
    }
    if job.job_type is JobType.ORIGINAL:
        required = {
            "prompt",
            "lyrics",
            "instrumental",
            "vocal_language",
            "prompt_mode",
            "duration_mode",
            "duration_seconds",
            "bpm",
            "key_scale",
            "time_signature",
            "seed",
            "output_format",
        }
        if not required.issubset(generation):
            raise ValueError("normalized original generation is incomplete")
        request_values = {
            "description": generation["prompt"],
            "lyrics": generation["lyrics"],
            "instrumental": generation["instrumental"],
            "vocal_language": generation["vocal_language"],
            "prompt_mode": generation["prompt_mode"],
            "duration_mode": generation["duration_mode"],
            "duration_seconds": generation["duration_seconds"],
            "bpm": generation["bpm"],
            "key_scale": generation["key_scale"],
            "time_signature": generation["time_signature"],
            "seed": generation["seed"],
            "output_format": generation["output_format"],
            "profile_id": profile_id,
            "variation_count": job.variation_count,
        }
        OriginalSongRequest(**request_values)
        return {
            **common,
            "description": generation["prompt"],
            "lyrics": generation["lyrics"],
            "instrumental": "true" if generation["instrumental"] is True else "",
            "vocal_language": generation["vocal_language"],
            "prompt_mode": generation["prompt_mode"],
            "duration_mode": generation["duration_mode"],
            "duration_seconds": generation["duration_seconds"],
            "bpm": generation["bpm"],
            "key_scale": generation["key_scale"],
            "time_signature": generation["time_signature"],
            "seed": generation["seed"],
            "output_format": generation["output_format"],
        }

    required = {
        "target_style",
        "remix_guidance",
        "lyrics",
        "audio_cover_strength",
        "cover_noise_strength",
        "duration_mode",
        "duration_seconds",
        "seed",
        "output_format",
    }
    if (
        not required.issubset(generation)
        or not isinstance(job.source_url, str)
        or not job.source_url.strip()
    ):
        raise ValueError("normalized cover generation is incomplete")
    resolve_cover_continuation_output(job)
    request_values = {
        "youtube_url": job.source_url,
        "target_style": generation["target_style"],
        "remix_guidance": generation["remix_guidance"],
        "lyrics": generation["lyrics"],
        "audio_cover_strength": generation["audio_cover_strength"],
        "cover_noise_strength": generation["cover_noise_strength"],
        "duration_mode": generation["duration_mode"],
        "duration_seconds": (
            generation["duration_seconds"] if generation["duration_mode"] == "custom" else None
        ),
        "seed": generation["seed"],
        "output_format": generation["output_format"],
        "profile_id": profile_id,
        "variation_count": job.variation_count,
        "rights_confirmation": True,
    }
    CoverRequest(**request_values)
    return {
        **common,
        "youtube_url": job.source_url,
        "target_style": generation["target_style"],
        "remix_guidance": generation["remix_guidance"],
        "lyrics": generation["lyrics"],
        "audio_cover_strength": generation["audio_cover_strength"],
        "cover_noise_strength": generation["cover_noise_strength"],
        "duration_mode": generation["duration_mode"],
        "duration_seconds": (
            generation["duration_seconds"] if generation["duration_mode"] == "custom" else None
        ),
        "seed": generation["seed"],
        "output_format": generation["output_format"],
        "rights_confirmation": "",
    }


def _optional_number(value: str | None, *, default: float | None = None) -> float | None:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return value  # type: ignore[return-value]


def _optional_int(value: str | None) -> int | str | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _required_int(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def _validation_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "form"
        message = str(item.get("msg", "invalid value"))
        errors.append(f"{location}: {message}")
    return errors or ["Please correct the highlighted fields."]


def _route_path(request: Request, name: str, **path_params: Any) -> str:
    """Build one same-origin browser path through Starlette's named routes."""

    return request.url_for(name, **path_params).path


def _project_view(request: Request, project: Project) -> dict[str, Any]:
    return {
        "project_id": project.id,
        "title": project.title,
        "job_type": project.job_type.value,
        "job_type_label": (
            "Cover project" if project.job_type is JobType.COVER else "Original project"
        ),
        "job_count": len(project.jobs),
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "detail_url": _route_path(request, "project_detail", project_id=project.id),
        "rename_url": _route_path(request, "rename_project", project_id=project.id),
    }


def _project_detail_context(
    request: Request,
    session: Any,
    project_id: str,
    *,
    rename_title: str | None = None,
    rename_errors: list[str] | None = None,
) -> dict[str, Any]:
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project not found",
        )
    jobs = list_project_jobs(session, project.id)
    return {
        "project": _project_view(request, project),
        "versions": [_job_view(request, job) for job in jobs],
        "rename_title": project.title if rename_title is None else rename_title,
        "rename_errors": rename_errors or [],
        "project_title_max_length": PROJECT_TITLE_MAX_LENGTH,
    }


def _job_view(request: Request, job: Job) -> dict[str, Any]:
    attempts = sorted(job.variation_attempts, key=lambda item: item.variation_index)
    outputs = sorted(job.outputs, key=lambda item: (item.variation_index, item.result_index))
    completed_variations = sum(item.status is JobStatus.COMPLETED for item in attempts)
    normalized_value = job.normalized_request_json
    normalized = (
        cast(dict[str, Any], normalized_value) if isinstance(normalized_value, dict) else {}
    )
    generation_value = normalized.get("generation")
    generation = (
        cast(dict[str, Any], generation_value) if isinstance(generation_value, dict) else {}
    )
    resolved_value = normalized.get("resolved_parameters")
    resolved = cast(dict[str, Any], resolved_value) if isinstance(resolved_value, dict) else {}
    staging_value = normalized.get("cover_staging")
    staging = cast(dict[str, Any], staging_value) if isinstance(staging_value, dict) else {}
    source_duration = normalized.get("source_duration_seconds", job.source_duration)
    target_duration = normalized.get(
        "resolved_target_duration_seconds", resolved.get("target_duration_seconds")
    )
    if target_duration is None:
        target_duration = generation.get("duration_seconds", resolved.get("duration"))
    audio_cover_strength = generation.get("audio_cover_strength", job.cover_strength)
    cover_noise_strength = generation.get("cover_noise_strength")
    phase: str | None = None
    phase_observed_at: str | None = None
    if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
        active_attempt = next(
            (item for item in attempts if item.variation_index == (job.current_variation or 1)),
            None,
        )
        progress = active_attempt.runpod_result_json if active_attempt is not None else None
        if isinstance(progress, dict) and progress.get("kind") == "audioventura_progress_v1":
            candidate_phase = progress.get("phase")
            candidate_observed_at = progress.get("observed_at")
            if isinstance(candidate_phase, str) and candidate_phase in _PHASE_LABELS:
                phase = candidate_phase
                if isinstance(candidate_observed_at, str) and len(candidate_observed_at) <= 64:
                    phase_observed_at = candidate_observed_at
    view = {
        "job_id": job.id,
        "job_type": job.job_type.value,
        "job_type_label": "Cover" if job.job_type is JobType.COVER else "Original song",
        "status": job.status.value,
        "status_label": _STATUS_LABELS[job.status],
        "source_title": job.sanitized_source_title,
        "source_url": job.source_url,
        "prompt": job.prompt,
        "lyrics": job.lyrics,
        "profile_id": normalized.get("profile_id"),
        "prompt_mode": generation.get("prompt_mode", resolved.get("prompt_mode")),
        "duration_mode": generation.get("duration_mode", resolved.get("duration_mode")),
        "duration_seconds": generation.get("duration_seconds"),
        "source_duration_seconds": source_duration,
        "target_duration_seconds": target_duration,
        "audio_cover_strength": audio_cover_strength,
        "cover_noise_strength": cover_noise_strength,
        "target_style": generation.get("target_style"),
        "remix_guidance": generation.get("remix_guidance"),
        "cover_confirmation_status": staging.get("status"),
        "output_format": job.output_format.value,
        "variation_count": job.variation_count,
        "current_variation": job.current_variation,
        "completed_variations": completed_variations,
        "error": job.user_facing_error if job.status is JobStatus.FAILED else None,
        "error_code": job.error_code if job.status is JobStatus.FAILED else None,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "elapsed_seconds": _elapsed(job),
        "phase": phase,
        "phase_label": _PHASE_LABELS.get(phase) if phase is not None else None,
        "phase_observed_at": phase_observed_at,
        "detail_url": _route_path(request, "job_detail", job_id=job.id),
        "status_url": _route_path(request, "job_status", job_id=job.id),
        "confirm_url": _route_path(request, "confirm_cover", job_id=job.id),
        "cancel_url": _route_path(request, "cancel_cover", job_id=job.id),
        "attempts": [_attempt_view(item) for item in attempts],
        "outputs": [_output_view(request, item) for item in outputs],
    }
    try:
        if job.project is not None and job.project.job_type is job.job_type:
            view["project_title"] = job.project.title
            view["project_url"] = _route_path(request, "project_detail", project_id=job.project.id)
            _continuation_form(job)
            view["continue_url"] = _route_path(request, "continue_job", job_id=job.id)
    except ValueError:
        pass
    return view


def _attempt_view(attempt: VariationAttempt) -> dict[str, Any]:
    result_value = attempt.runpod_result_json
    result = cast(dict[str, Any], result_value) if isinstance(result_value, dict) else {}
    output_value = result.get("output")
    output = cast(dict[str, Any], output_value) if isinstance(output_value, dict) else {}
    worker_value = result.get("worker")
    worker = cast(dict[str, Any], worker_value) if isinstance(worker_value, dict) else {}
    return {
        "variation_index": attempt.variation_index,
        "status": attempt.status.value,
        "status_label": _STATUS_LABELS[attempt.status],
        "queue_delay_ms": result.get("runpod_queue_delay_ms"),
        "execution_ms": result.get("runpod_execution_ms"),
        "profile_id": result.get("profile_id", worker.get("profile_id")),
        "gpu": worker.get("gpu"),
        "dit_model": worker.get("dit_model", worker.get("model")),
        "lm_model": worker.get("lm_model"),
        "requested_seed": output.get("requested_seed"),
        "effective_seed": output.get("effective_seed", output.get("seed")),
        "duration_seconds": output.get("duration_seconds"),
        "target_duration_seconds": output.get("target_duration_seconds"),
        "duration_tolerance_seconds": output.get("duration_tolerance_seconds"),
    }


def _output_view(request: Request, output: Output) -> dict[str, Any]:
    return {
        "id": output.id,
        "variation_index": output.variation_index,
        "result_index": output.result_index,
        "mime_type": output.mime_type,
        "byte_size": output.byte_size,
        "size_label": _size_label(output.byte_size),
        "media_url": _route_path(request, "media", output_id=output.id),
        "download_url": _route_path(request, "download", output_id=output.id),
        "created_at": _iso(output.created_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _elapsed(job: Job) -> float | None:
    start = job.started_at or job.created_at
    end = job.completed_at or utc_now()
    return max(0.0, round((end - start).total_seconds(), 2))


def _size_label(value: int) -> str:
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


async def _readiness(app: FastAPI, *, only: set[str] | None = None) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {
        "controller_database": {"ok": False, "message": "unavailable"},
        "runpod_api": {"ok": False, "message": "unavailable"},
        "home_ingest": {"ok": False, "message": "unavailable"},
        "public_transfer": {"ok": False, "message": "unavailable"},
    }
    if only is None or "controller_database" in only:
        try:
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
            components["controller_database"] = {"ok": True, "message": "ready"}
        except Exception:
            components["controller_database"] = {"ok": False, "message": "database unavailable"}

    settings: ServiceSettings = app.state.settings
    if only is None or "public_transfer" in only:
        parsed = settings.transfer_public_base_url
        components["public_transfer"] = {
            "ok": parsed.startswith("https://"),
            "message": "configured" if parsed.startswith("https://") else "HTTPS required",
        }

    async def probe(name: str, client: Any, method_name: str) -> None:
        if only is not None and name not in only:
            return
        method = getattr(client, method_name, None)
        if method is None:
            components[name] = {"ok": False, "message": "health probe unavailable"}
            return
        try:
            result = method()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=_READINESS_PROBE_TIMEOUT_SECONDS)
            components[name] = {"ok": True, "message": "ready"}
        except Exception:
            components[name] = {"ok": False, "message": "unreachable"}

    await asyncio.gather(
        probe("runpod_api", app.state.runpod_client, "health"),
        probe("home_ingest", app.state.home_ingest_client, "health"),
    )
    required_ok = all(
        components[key]["ok"] for key in ("controller_database", "runpod_api", "public_transfer")
    )
    return {
        "status": "ok" if required_ok and components["home_ingest"]["ok"] else "degraded",
        "components": components,
    }


def _writable_data_layout(settings: ServiceSettings) -> bool:
    try:
        settings.ensure_data_layout()
        for directory in settings.paths.all_directories:
            if not os.access(directory, os.W_OK):
                return False
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".healthz-", delete=True):
                pass
        return True
    except OSError:
        return False


def _verified_output(
    settings: ServiceSettings, session_factory: Any, output_id: int
) -> tuple[Path, Output]:
    with session_factory() as session:
        output = get_output(session, output_id)
        if output is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not found")
        record = Output(
            id=output.id,
            job_id=output.job_id,
            variation_index=output.variation_index,
            result_index=output.result_index,
            runpod_job_id=output.runpod_job_id,
            relative_path=output.relative_path,
            mime_type=output.mime_type,
            byte_size=output.byte_size,
            sha256=output.sha256,
            seed_metadata_json=output.seed_metadata_json,
            generation_metadata_json=output.generation_metadata_json,
            created_at=output.created_at,
        )
    if record.mime_type not in _MIME_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not available")
    expected_mime = (
        _FORMAT_MIME.get(OutputFormat(Path(record.relative_path).suffix.lstrip(".").lower()))
        if Path(record.relative_path).suffix.lstrip(".").lower()
        in {item.value for item in OutputFormat}
        else None
    )
    if expected_mime != record.mime_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not available")
    raw_path = settings.paths.outputs / record.relative_path
    try:
        path = resolve_relative_path(settings.paths.outputs, record.relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="output not available"
        ) from exc
    root = settings.paths.outputs.resolve()
    if not path.is_relative_to(root) or _has_symlink_component(settings.paths.outputs, raw_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="output not available")
    try:
        file_stat = raw_path.lstat()
        if not raw_path.is_file() or raw_path.is_symlink() or file_stat.st_size != record.byte_size:
            raise OSError("output file does not match its record")
        if _file_sha256(path) != record.sha256:
            raise OSError("output checksum does not match its record")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="output not available"
        ) from exc
    return path, record


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

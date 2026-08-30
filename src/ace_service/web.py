"""Authenticated HTML, JSON status, readiness, and media routes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import or_, select, text

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
from ace_service.media_library import (
    MediaLibraryError,
    MediaLibraryService,
    media_file_content_disposition,
    verify_media_file,
)
from ace_service.models import (
    KEEP_WARM_LABELS,
    KEEP_WARM_SECONDS,
    PROJECT_TITLE_MAX_LENGTH,
    AssetTransferCapability,
    AssetTransferPurpose,
    AssetTransferStatus,
    CapacityLease,
    Job,
    JobStatus,
    JobType,
    MediaDeletionState,
    MediaDerivativeTask,
    MediaFile,
    MediaFileState,
    MediaItem,
    NotificationDelivery,
    Output,
    OutputFormat,
    Playlist,
    PlaylistKind,
    Project,
    SourceAsset,
    SourceAssetOrigin,
    SourceAssetStatus,
    SubmissionQuote,
    VariationAttempt,
)
from ace_service.notifications import (
    MAX_ATTEMPTS,
    SubscriptionValidationError,
    create_or_replace_subscription,
    disable_subscription,
)
from ace_service.providers.base import BackendId, BackendOperation, ProviderHealth, ProviderName
from ace_service.providers.fal import FalProvider
from ace_service.repository import (
    PROVIDER_PROGRESS_MESSAGE_MAX_LENGTH,
    MediaLibraryQuery,
    PlaylistConflictError,
    add_playlist_entry,
    cancel_cover_staging,
    confirm_cover_job,
    create_cover_job,
    create_custom_playlist,
    create_original_job,
    create_project,
    create_project_deletion_audit,
    create_source_asset,
    create_source_remix_job,
    create_submission_quote,
    delete_playlist,
    finalize_cover_job_duration,
    get_job,
    get_keep_warm_seconds,
    get_latest_gpu_rates,
    get_matching_runtime_calibration,
    get_media_file,
    get_output,
    get_playlist,
    get_project,
    get_source_asset,
    issue_asset_transfer_capability,
    job_backend,
    list_media_library,
    list_media_queue,
    list_playlist_entries,
    list_playlists,
    list_project_jobs,
    list_projects,
    mark_source_uploaded,
    project_is_deletable,
    query_media_library,
    recent_completed_attempt_execution_ms,
    remove_playlist_entry,
    rename_media_item,
    rename_playlist,
    rename_project,
    reorder_playlist_entries,
    request_job_cancellation,
    resolve_continuation_source,
    resolve_cover_continuation_output,
    retry_derivative_task,
    retry_source_asset,
    set_keep_warm_seconds,
    transition_job,
    validate_source_range,
)
from ace_service.schemas import (
    CoverRequest,
    OriginalSongRequest,
    resolve_relative_path,
    validate_sha256,
    validate_youtube_url,
)

_TEMPLATES = Path(__file__).with_name("templates")
_STATIC = Path(__file__).with_name("static")
_MIME_TYPES = {"audio/mpeg", "audio/flac", "audio/wav"}
_READINESS_PROBE_TIMEOUT_SECONDS = 5.0
_FAL_HEALTH_CACHE_TTL_SECONDS = 30.0
_BACKEND_FIELD_ALIASES = {
    "prompt": ("description", "target_style"),
    "duration": ("duration_seconds",),
    "lyrics": ("lyrics",),
    "source_lyrics": ("source_lyrics",),
    "source_style": ("source_style",),
    "seed": ("seed",),
    "strength": ("strength", "audio_cover_strength"),
    "start_seconds": ("start_seconds",),
    "end_seconds": ("end_seconds",),
    "before_seconds": ("before_seconds",),
    "after_seconds": ("after_seconds",),
}
_FAL_UNIVERSAL_FIELDS = frozenset(
    {
        "backend",
        "csrf_token",
        "continue_from_job_id",
        "duration_mode",
        "output_format",
        "profile_id",
        "rights_confirmation",
        "variation_count",
        "youtube_url",
    }
)
_BUILTIN_GENERATION_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    BackendOperation.TEXT_TO_MUSIC.value: {
        "lyrics": {"type": "string", "required": False},
        "instrumental": {"type": "boolean", "required": False},
        "prompt_mode": {"type": "string", "required": False},
        "vocal_language": {"type": "string", "required": False},
        "duration": {"type": "number", "required": False, "minimum": 10, "maximum": 600},
        "bpm": {"type": "integer", "required": False, "minimum": 30, "maximum": 300},
        "key_scale": {"type": "string", "required": False},
        "time_signature": {
            "type": "integer",
            "required": False,
            "minimum": 2,
            "maximum": 6,
        },
        "seed": {"type": "integer", "required": False, "minimum": 0},
    },
    BackendOperation.AUDIO_TRANSFORM.value: {
        "lyrics": {"type": "string", "required": False},
        "audio_cover_strength": {
            "type": "number",
            "required": False,
            "minimum": 0,
            "maximum": 1,
        },
        "cover_noise_strength": {
            "type": "number",
            "required": False,
            "minimum": 0,
            "maximum": 1,
        },
        "duration": {"type": "number", "required": False, "minimum": 10, "maximum": 600},
        "seed": {"type": "integer", "required": False, "minimum": 0},
    },
}
_MOCK_GENERATION_FIELDS: dict[str, dict[str, Any]] = {
    # The mock advertises every field owned by the built-in forms. It accepts
    # these values for integration coverage and deliberately ignores them.
    "prompt": {"type": "string", "required": False},
    "lyrics": {"type": "string", "required": False},
    "instrumental": {"type": "boolean", "required": False},
    "prompt_mode": {"type": "string", "required": False},
    "vocal_language": {"type": "string", "required": False},
    "duration": {"type": "number", "required": False, "minimum": 10, "maximum": 600},
    "bpm": {"type": "integer", "required": False, "minimum": 30, "maximum": 300},
    "key_scale": {"type": "string", "required": False},
    "time_signature": {"type": "integer", "required": False, "minimum": 2, "maximum": 6},
    "seed": {"type": "integer", "required": False, "minimum": 0},
    "source_style": {"type": "string", "required": False},
    "source_lyrics": {"type": "string", "required": False},
    "audio_cover_strength": {
        "type": "number",
        "required": False,
        "minimum": 0,
        "maximum": 1,
    },
    "cover_noise_strength": {
        "type": "number",
        "required": False,
        "minimum": 0,
        "maximum": 1,
    },
    "strength": {"type": "number", "required": False, "minimum": 0, "maximum": 1},
    "start_seconds": {"type": "number", "required": False, "minimum": 0, "maximum": 600},
    "end_seconds": {"type": "number", "required": False, "minimum": 0, "maximum": 600},
    "before_seconds": {"type": "number", "required": False, "minimum": 0, "maximum": 600},
    "after_seconds": {"type": "number", "required": False, "minimum": 0, "maximum": 600},
}
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
    JobStatus.CANCELLED: "Cancelled",
}
_PHASE_LABELS = {
    "cloud_wait": "Waiting for the cloud provider",
    "worker_initializing": "Cloud worker initializing",
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

    if str(form.get("backend", "")).startswith(("fal/", "mock/")):
        return None
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


async def _backend_pricing_context(
    app: FastAPI,
    operation: BackendOperation | tuple[BackendOperation, ...],
    form: Mapping[str, Any],
) -> dict[str, Any]:
    client = getattr(app.state, "fal_pricing", None)
    if client is None:
        return {}
    choices = _backend_choices(app, operation)
    default_operation = operation[0] if isinstance(operation, tuple) else operation
    selected = str(
        form.get("backend")
        or app.state.provider_registry.default_for(default_operation).capabilities.backend_id
    )
    choice = next((item for item in choices if item["backend_id"] == selected), None)
    if choice is None or choice["provider"] != "fal.ai":
        return {}
    pricing = choice["snapshot"].get("pricing")
    declared_unit = pricing.get("unit") if isinstance(pricing, Mapping) else None
    try:
        variation_count = int(form.get("variation_count", "1"))
    except (TypeError, ValueError):
        variation_count = 1
    variation_count = variation_count if variation_count in {1, 2, 3, 4} else 1
    quantity: Decimal | None = None
    duration: Decimal | None = None
    if declared_unit in {"request", "variation"}:
        quantity = Decimal(variation_count)
    elif declared_unit == "second":
        duration_policy = choice["snapshot"].get("fields", {}).get("duration", {})
        raw_duration = (
            form.get("duration_seconds")
            if form.get("duration_mode", "auto") == "custom"
            else duration_policy.get("default")
        )
        try:
            duration = Decimal(str(raw_duration))
        except (ArithmeticError, TypeError, ValueError):
            duration = None
        if duration is not None and duration.is_finite() and duration > 0:
            minimum = duration_policy.get("minimum")
            maximum = duration_policy.get("maximum")
            if (minimum is None or duration >= Decimal(str(minimum))) and (
                maximum is None or duration <= Decimal(str(maximum))
            ):
                quantity = duration * variation_count
    endpoint_id = choice["snapshot"].get("endpoint_id", selected.removeprefix("fal/"))
    price = (
        await client.estimate(
            endpoint_id,
            unit_quantity=quantity,
            declared_unit=declared_unit,
        )
        if quantity is not None
        else await client.get(endpoint_id)
    )
    if price is None:
        return {"backend_pricing": {"available": False}}
    reference_total: str | None = None
    if quantity is not None and isinstance(pricing, Mapping) and price.unit != declared_unit:
        try:
            reference_micro = (
                Decimal(str(pricing.get("unit_price"))) * quantity * Decimal(1_000_000)
            ).to_integral_value(rounding=ROUND_HALF_UP)
            reference_total = f"{reference_micro / Decimal(1_000_000):.4f}"
        except (ArithmeticError, TypeError, ValueError):
            reference_total = None
    return {
        "backend_pricing": {
            "available": True,
            "unit_price": price.unit_price_usd,
            "unit": price.unit,
            "unit_label": price.unit.replace("_", " "),
            "fetched_at": price.fetched_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "stale": price.stale,
            "duration_seconds": format(duration, "f") if duration is not None else None,
            "variation_count": variation_count,
            "reference_total": reference_total,
            "total": (
                f"{Decimal(price.total_micro_usd) / Decimal(1_000_000):.4f}"
                if price.total_micro_usd is not None
                else None
            ),
        }
    }


def _backend_choices(
    app: FastAPI, operation: BackendOperation | tuple[BackendOperation, ...]
) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    operations = (operation,) if isinstance(operation, BackendOperation) else operation
    seen: set[str] = set()
    providers = [
        provider
        for item in operations
        for provider in app.state.provider_registry.selectable(item)
        if not isinstance(provider, FalProvider)
        or (
            (
                health_entry := getattr(app.state, "fal_health_cache", {}).get(
                    str(provider.capabilities.backend_id)
                )
            )
            is None
            or health_entry[1].ok
        )
    ]
    for provider in providers:
        capabilities = provider.capabilities
        if capabilities.operation is not None and capabilities.operation not in operations:
            continue
        if str(capabilities.backend_id) in seen:
            continue
        seen.add(str(capabilities.backend_id))
        descriptor = getattr(provider, "descriptor", None)
        if descriptor is not None:
            snapshot = descriptor.snapshot()
            if descriptor.pricing is not None:
                snapshot["pricing"] = descriptor.pricing
            fields = {
                name: {
                    "ui_name": policy.ui_name,
                    "type": policy.type,
                    "required": policy.required,
                    "default": policy.default,
                    "minimum": policy.minimum,
                    "maximum": policy.maximum,
                    "choices": list(policy.choices),
                    "advanced": policy.advanced,
                    "semantic_note": policy.semantic_note,
                }
                for name, policy in descriptor.fields.items()
            }
            snapshot["fields"] = fields
            snapshot["native_formats"] = list(descriptor.output.native_formats)
            label = descriptor.label
            provider_name = "fal.ai"
            actual_operation = descriptor.operation.value
            media_kind = descriptor.media_kind.value
        else:
            backend_id = str(capabilities.backend_id)
            provider_name = capabilities.name.value
            label = {
                "runpod/ace-step-v15-xl-turbo": "Runpod · ACE-Step 1.5 XL Turbo",
                "salad/ace-step-v15-xl-turbo": "Salad · ACE-Step 1.5 XL Turbo",
                "mock/midi-sequential": "Mock · Sequential MIDI → MP3",
            }.get(backend_id, backend_id)
            actual_operation = (
                capabilities.operation.value
                if capabilities.operation is not None
                else operations[0].value
            )
            media_kind = capabilities.media_kind.value
            snapshot = {
                "backend_id": backend_id,
                "provider": capabilities.name.value,
                "label": label,
                "operation": actual_operation,
                "media_kind": media_kind,
                "native_formats": sorted(capabilities.native_formats),
                "fields": (
                    dict(_MOCK_GENERATION_FIELDS)
                    if backend_id == "mock/midi-sequential"
                    else _BUILTIN_GENERATION_FIELDS[actual_operation]
                ),
                "result_delivery": capabilities.result_delivery.value,
                "catalog_revision": "builtin-v1",
                "source_duration_min_seconds": capabilities.source_duration_min_seconds,
                "source_duration_max_seconds": capabilities.source_duration_max_seconds,
                "output_duration_min_seconds": capabilities.output_duration_min_seconds,
                "output_duration_max_seconds": capabilities.output_duration_max_seconds,
            }
        choices.append(
            {
                "backend_id": str(capabilities.backend_id),
                "provider": provider_name,
                "label": label,
                "operation": actual_operation,
                "media_kind": media_kind,
                "native_formats": snapshot["native_formats"],
                "fields": snapshot["fields"],
                "snapshot": snapshot,
            }
        )
    return sorted(choices, key=lambda item: (item["provider"], item["label"], item["backend_id"]))


def _preferred_output_format(formats: Any) -> str:
    values = [str(value).lower() for value in formats]
    if "mp3" in values:
        return "mp3"
    return sorted(values)[0] if values else "mp3"


def _has_reviewed_source_bounds(choice: Mapping[str, Any]) -> bool:
    snapshot = choice.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return False
    minimum = snapshot.get("source_duration_min_seconds")
    maximum = snapshot.get("source_duration_max_seconds")
    return (
        isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        and math.isfinite(float(minimum))
        and float(minimum) > 0
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and math.isfinite(float(maximum))
        and float(maximum) >= float(minimum)
    )


def _source_backend_choices(app: FastAPI) -> list[dict[str, Any]]:
    """Return only audio-transform backends with a frozen source contract."""

    operations = (
        BackendOperation.AUDIO_TRANSFORM,
        BackendOperation.AUDIO_INPAINT,
        BackendOperation.AUDIO_OUTPAINT,
    )
    return [
        choice
        for choice in _backend_choices(app, operations)
        if _has_reviewed_source_bounds(choice)
    ]


_SOURCE_BACKEND_STALE_NOTE = (
    "The previous remix backend is no longer available; review the replacement before generating."
)


def _source_backend_id(choices: list[dict[str, Any]], value: Any) -> str | None:
    """Return an exact, syntactically valid source backend ID from choices."""

    if not isinstance(value, str):
        return None
    try:
        normalized = str(BackendId(value))
    except ValueError:
        return None
    if normalized != value:
        return None
    return next(
        (str(choice["backend_id"]) for choice in choices if choice["backend_id"] == normalized),
        None,
    )


def _valid_backend_syntax(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(BackendId(value)) == value
    except ValueError:
        return False


def _resolve_source_backend(
    app: FastAPI,
    choices: list[dict[str, Any]],
    *,
    explicit_backend: Any = None,
    preferred_backend: Any = None,
) -> tuple[str, str | None]:
    """Resolve source-form selection without looking up untrusted IDs."""

    stale = False
    if explicit_backend:
        selected = _source_backend_id(choices, explicit_backend)
        if selected is not None:
            return selected, None
        stale = _valid_backend_syntax(explicit_backend)

    if preferred_backend:
        selected = _source_backend_id(choices, preferred_backend)
        if selected is not None:
            return selected, _SOURCE_BACKEND_STALE_NOTE if stale else None
        stale = stale or _valid_backend_syntax(preferred_backend)

    configured_default = getattr(app.state.settings, "default_cover_backend", None)
    selected = _source_backend_id(choices, configured_default)
    if selected is not None:
        return selected, _SOURCE_BACKEND_STALE_NOTE if stale else None
    if choices:
        return str(choices[0]["backend_id"]), _SOURCE_BACKEND_STALE_NOTE if stale else None
    return "", _SOURCE_BACKEND_STALE_NOTE if stale else None


async def _refresh_fal_health(app: FastAPI, *, force: bool = False) -> None:
    """Refresh endpoint-scoped Fal health into the selection cache."""

    cache = getattr(app.state, "fal_health_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        app.state.fal_health_cache = cache
    now = time.monotonic()
    providers = {
        str(provider.capabilities.backend_id): provider
        for provider in app.state.provider_registry.providers
        if isinstance(provider, FalProvider)
    }

    async def refresh(backend_id: str, provider: FalProvider) -> None:
        cached = cache.get(backend_id)
        if (
            not force
            and isinstance(cached, tuple)
            and len(cached) == 2
            and now - float(cached[0]) < _FAL_HEALTH_CACHE_TTL_SECONDS
        ):
            return
        try:
            health = await asyncio.wait_for(
                provider.health(), timeout=_READINESS_PROBE_TIMEOUT_SECONDS
            )
            if not isinstance(health, ProviderHealth):
                health = ProviderHealth(True, "ready")
        except Exception:
            health = ProviderHealth(False, "unreachable")
        cache[backend_id] = (time.monotonic(), health)

    await asyncio.gather(
        *(refresh(backend_id, provider) for backend_id, provider in providers.items())
    )


def _backend_form_context(
    app: FastAPI,
    operation: BackendOperation | tuple[BackendOperation, ...],
    form: Mapping[str, Any],
) -> dict[str, Any]:
    choices = _backend_choices(app, operation)
    try:
        default_operation = operation[0] if isinstance(operation, tuple) else operation
        default_backend = str(
            app.state.provider_registry.default_for(default_operation).capabilities.backend_id
        )
    except ValueError:
        default_backend = choices[0]["backend_id"] if choices else ""
    selected = str(form.get("backend") or default_backend)
    selected_choice = next((choice for choice in choices if choice["backend_id"] == selected), None)
    native_formats = selected_choice["native_formats"] if selected_choice else ["mp3"]
    return {
        "backend_choices": choices,
        "selected_backend": selected,
        "selected_backend_is_fal": bool(
            selected_choice is not None and selected_choice["provider"] == "fal.ai"
        ),
        "selected_backend_is_mock": bool(
            selected_choice is not None and selected_choice["provider"] == ProviderName.MOCK.value
        ),
        "selected_native_formats": list(native_formats),
        "selected_output_format": _preferred_output_format(native_formats),
    }


def _select_backend(
    app: FastAPI,
    fields: Mapping[str, str],
    operation: BackendOperation | tuple[BackendOperation, ...],
    *,
    choices: list[dict[str, Any]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    requested = fields.get("backend", "").strip()
    available_choices = choices if choices is not None else _backend_choices(app, operation)
    if not requested:
        default_operation = operation[0] if isinstance(operation, tuple) else operation
        try:
            requested = str(
                app.state.provider_registry.default_for(default_operation).capabilities.backend_id
            )
        except ValueError:
            requested = available_choices[0]["backend_id"] if available_choices else ""
    choice = next((item for item in available_choices if item["backend_id"] == requested), None)
    if choice is None:
        raise ValueError("selected backend is not enabled or compatible with this form")
    try:
        backend_id = BackendId(requested)
        provider = app.state.provider_registry.get_persisted(backend_id)
    except (ValueError, KeyError) as exc:
        raise ValueError("selected backend is not configured") from exc
    allowed_advanced = set(choice["fields"])
    if isinstance(fields, dict) and not fields.get("output_format"):
        fields["output_format"] = _preferred_output_format(choice["native_formats"])
    output_format = fields.get("output_format")
    if output_format and output_format not in choice["native_formats"]:
        raise ValueError("output format is not supported by the selected backend")
    if provider.capabilities.name is ProviderName.FAL:
        if (
            fields.get("audio_cover_strength") not in (None, "", "0.65")
            and "strength" not in allowed_advanced
        ):
            raise ValueError("audio_cover_strength is not supported by the selected backend")
        if fields.get("cover_noise_strength") not in (None, "", "0", "0.0"):
            raise ValueError("cover_noise_strength is not supported by the selected backend")
    form_aliases = _BACKEND_FIELD_ALIASES
    allowed_fields = {
        alias
        for field_name in choice["fields"]
        for alias in form_aliases.get(field_name, (field_name,))
    }
    universal_fields = _FAL_UNIVERSAL_FIELDS | (
        {"description", "target_style", "remix_guidance"} if "prompt" in choice["fields"] else set()
    )
    if provider.capabilities.name is ProviderName.FAL:
        for field_name, raw_value in fields.items():
            if field_name in universal_fields or field_name in allowed_fields:
                continue
            if field_name == "instrumental" and raw_value in {"", "0", "false", "off"}:
                continue
            if field_name == "prompt_mode" and raw_value in {"", "direct"}:
                continue
            if field_name == "audio_cover_strength" and raw_value in {"", "0.65"}:
                continue
            if field_name == "cover_noise_strength" and raw_value in {"", "0", "0.0"}:
                continue
            if raw_value not in (None, ""):
                raise ValueError(f"{field_name} is not supported by the selected backend")
    for field_name, policy in choice["fields"].items():
        if not policy.get("required") or field_name == "source_audio":
            continue
        aliases = form_aliases.get(field_name, (field_name,))
        if not any(fields.get(alias) not in (None, "") for alias in aliases):
            raise ValueError(
                f"{policy.get('ui_name', field_name)} is required for the selected backend"
            )
    if choice["operation"] == BackendOperation.AUDIO_INPAINT.value:
        start = _backend_form_number(fields, "start_seconds")
        end = _backend_form_number(fields, "end_seconds")
        if start is None or end is None or not 0 <= start < end:
            raise ValueError("inpaint region must satisfy 0 <= start_seconds < end_seconds")
    elif choice["operation"] == BackendOperation.AUDIO_OUTPAINT.value:
        before = _backend_form_number(fields, "before_seconds") or 0.0
        after = _backend_form_number(fields, "after_seconds") or 0.0
        if before <= 0 and after <= 0:
            raise ValueError("outpaint must extend before or after the source")
    for field_name, policy in choice["fields"].items():
        aliases = form_aliases.get(field_name, (field_name,))
        if policy.get("type") not in {"number", "integer"}:
            continue
        raw_name = next((alias for alias in aliases if fields.get(alias) not in (None, "")), None)
        if raw_name is None:
            continue
        numeric_value = _backend_form_number(fields, raw_name)
        if numeric_value is None:
            continue
        if policy.get("type") == "integer" and not numeric_value.is_integer():
            raise ValueError(f"{policy.get('ui_name', field_name)} must be an integer")
        minimum = policy.get("minimum")
        maximum = policy.get("maximum")
        if minimum is not None and numeric_value < minimum:
            raise ValueError(f"{policy.get('ui_name', field_name)} is below its minimum")
        if maximum is not None and numeric_value > maximum:
            raise ValueError(f"{policy.get('ui_name', field_name)} is above its maximum")
    return provider, choice["snapshot"]


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
                    "sources_new": _route_path(request, "sources_new"),
                    "source_youtube": _route_path(request, "source_youtube"),
                    "source_uploads": _route_path(request, "source_uploads"),
                    "library": _route_path(request, "library"),
                    "playlists": _route_path(request, "playlists"),
                    "projects": _route_path(request, "projects"),
                    "jobs": _route_path(request, "jobs"),
                    "offline": _route_path(request, "offline"),
                    "offline_shell": _route_path(request, "offline_shell"),
                    "manifest": _route_path(request, "manifest"),
                    "app_css": _route_path(request, "static", path="app.css"),
                    "status_js": _route_path(request, "static", path="status.js"),
                    "estimate_selector_js": _route_path(
                        request, "static", path="estimate_selector.js"
                    ),
                    "backend_selector_js": _route_path(
                        request, "static", path="backend_selector.js"
                    ),
                    "backend_pricing": _route_path(request, "backend_pricing"),
                    "notifications_js": _route_path(request, "static", path="notifications.js"),
                    "notifications_config": _route_path(request, "notifications_config"),
                    "notifications_subscriptions": _route_path(
                        request, "notifications_subscriptions"
                    ),
                    "notifications_worker": _route_path(request, "notification_worker"),
                    "keep_warm": _route_path(request, "set_keep_warm"),
                    "player_js": _route_path(request, "static", path="player.js"),
                    "app_shell_js": _route_path(request, "static", path="app_shell.js"),
                    "offline_store_js": _route_path(request, "static", path="offline_store.js"),
                    "offline_cache_js": _route_path(request, "static", path="offline_cache.js"),
                    "media_library_js": _route_path(request, "static", path="media_library.js"),
                    "source_upload_js": _route_path(request, "static", path="source_upload.js"),
                    "source_range_js": _route_path(request, "static", path="source_range.js"),
                },
            },
            status_code=response_status,
        )
        attach_csrf_cookie(request, response, token)
        return response

    @app.get("/offline-shell", name="offline_shell")
    async def offline_shell(request: Request) -> Response:
        """Return the fixed, secret-free document used for offline navigation."""

        response = templates.TemplateResponse(
            request=request,
            name="offline_shell.html",
            context={
                "urls": {
                    "app_css": _route_path(request, "static", path="app.css"),
                    "manifest": _route_path(request, "manifest"),
                    "player_js": _route_path(request, "static", path="player.js"),
                    "app_shell_js": _route_path(request, "static", path="app_shell.js"),
                    "offline_store_js": _route_path(request, "static", path="offline_store.js"),
                    "offline_cache_js": _route_path(request, "static", path="offline_cache.js"),
                    "offline": _route_path(request, "offline"),
                    "offline_worker": _route_path(request, "notification_worker"),
                }
            },
        )
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/manifest.webmanifest", name="manifest")
    async def manifest(request: Request) -> JSONResponse:
        scope = _worker_scope(request)
        return JSONResponse(
            {
                "name": "AudioVentura",
                "short_name": "AudioVentura",
                "description": "A personal music player.",
                "start_url": scope,
                "scope": scope,
                "display": "standalone",
                "background_color": "#0c1110",
                "theme_color": "#0c1110",
                "prefer_related_applications": False,
                "icons": [
                    {
                        "src": _route_path(request, "static", path="icon-192.svg"),
                        "sizes": "192x192",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    },
                    {
                        "src": _route_path(request, "static", path="icon-512.svg"),
                        "sizes": "512x512",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    },
                ],
            },
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/offline", dependencies=[Depends(authenticated)], name="offline")
    async def offline(request: Request) -> Response:
        return render(request, "offline.html", {})

    @app.get(
        "/backend-pricing",
        dependencies=[Depends(authenticated)],
        name="backend_pricing",
    )
    async def backend_pricing(request: Request) -> JSONResponse:
        form = {
            "backend": request.query_params.get("backend", ""),
            "duration_mode": request.query_params.get("duration_mode", "auto"),
            "duration_seconds": request.query_params.get("duration_seconds", ""),
            "variation_count": request.query_params.get("variation_count", "1"),
        }
        context = await _backend_pricing_context(
            app,
            tuple(BackendOperation),
            form,
        )
        pricing = context.get("backend_pricing")
        return JSONResponse(
            pricing if isinstance(pricing, Mapping) else {"available": False, "applicable": False}
        )

    @app.get(
        "/notifications/config", dependencies=[Depends(authenticated)], name="notifications_config"
    )
    async def notifications_config() -> JSONResponse:
        settings: ServiceSettings = app.state.settings
        return JSONResponse(
            {
                "enabled": settings.web_push_enabled,
                "public_key": settings.web_push_vapid_public_key
                if settings.web_push_enabled
                else None,
            }
        )

    @app.post(
        "/notifications/subscriptions",
        dependencies=[Depends(authenticated)],
        name="notifications_subscriptions",
    )
    async def notifications_subscriptions(request: Request) -> JSONResponse:
        try:
            raw_body = await request.body()
            if len(raw_body) > 16 * 1024:
                raise HTTPException(status_code=413, detail="subscription body is too large")
            import json

            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="subscription body is invalid") from exc
        if not isinstance(body, dict) or len(body) > 8:
            raise HTTPException(status_code=400, detail="subscription body is invalid")
        require_csrf(
            request,
            {"csrf_token": str(body.get("csrf_token", request.headers.get("x-csrf-token", "")))},
        )
        if set(body) - {"endpoint", "keys", "csrf_token"} or not isinstance(body.get("keys"), dict):
            raise HTTPException(status_code=400, detail="subscription body is invalid")
        endpoint = body.get("endpoint")
        if not isinstance(endpoint, str):
            raise HTTPException(status_code=400, detail="subscription body is invalid")
        try:
            with app.state.session_factory() as session:
                subscription = create_or_replace_subscription(
                    session,
                    endpoint=endpoint,
                    p256dh=body["keys"].get("p256dh"),
                    auth=body["keys"].get("auth"),
                    allowed_origins=app.state.settings.web_push_allowed_origins,
                )
                session.commit()
                subscription_id = subscription.id
        except SubscriptionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"subscription_id": subscription_id, "enabled": True})

    @app.delete(
        "/notifications/subscriptions/{subscription_id}",
        dependencies=[Depends(authenticated)],
        name="disable_notification_subscription",
    )
    async def disable_notification_subscription(
        request: Request, subscription_id: str
    ) -> JSONResponse:
        require_csrf(request, {"csrf_token": request.headers.get("x-csrf-token", "")})
        with app.state.session_factory() as session:
            found = disable_subscription(session, subscription_id)
            session.commit()
        return JSONResponse(
            {"subscription_id": subscription_id, "enabled": False},
            status_code=200 if found else 404,
        )

    @app.get("/notification-worker.js", name="notification_worker")
    async def notification_worker(request: Request) -> Response:
        scope = _worker_scope(request)
        script = _notification_worker_script()
        return Response(
            script,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": scope,
            },
        )

    @app.post("/settings/keep-warm", dependencies=[Depends(authenticated)], name="set_keep_warm")
    async def set_keep_warm(request: Request) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        if "keep_warm_seconds" not in fields:
            return RedirectResponse(
                _route_path(request, "dashboard"), status_code=status.HTTP_303_SEE_OTHER
            )
        raw_seconds = fields["keep_warm_seconds"]
        try:
            seconds = int(raw_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="keep-warm value is invalid") from exc
        if seconds not in KEEP_WARM_SECONDS:
            raise HTTPException(status_code=422, detail="keep-warm value is invalid")
        with app.state.session_factory() as session:
            set_keep_warm_seconds(session, seconds)
            session.commit()
        return RedirectResponse(
            _route_path(request, "dashboard"), status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/", dependencies=[Depends(authenticated)], name="dashboard")
    async def dashboard(request: Request) -> Response:
        with app.state.session_factory() as session:
            jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(20)))
            job_views = [_job_view(request, job) for job in jobs]
        readiness = await _readiness(app)
        with app.state.session_factory() as session:
            keep_warm_seconds = get_keep_warm_seconds(session)
            capacity = _capacity_views(app, session)
        return render(
            request,
            "dashboard.html",
            {
                "jobs": job_views,
                "readiness": readiness,
                "keep_warm_seconds": keep_warm_seconds,
                "keep_warm_options": KEEP_WARM_SECONDS,
                "keep_warm_labels": KEEP_WARM_LABELS,
                "capacity": capacity,
            },
        )

    @app.get("/sources/new", dependencies=[Depends(authenticated)], name="sources_new")
    async def sources_new(request: Request) -> Response:
        await _refresh_fal_health(app)
        choices = _source_backend_choices(app)
        if not choices:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no reviewed remix backend is available",
            )
        form = {"project_title": "", "youtube_url": ""}
        return render(
            request,
            "source_new.html",
            {
                "form": form,
                "errors": [],
                **_source_backend_form_context(app, choices, form),
            },
        )

    @app.post("/sources/youtube", dependencies=[Depends(authenticated)], name="source_youtube")
    async def source_youtube(request: Request) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        await _refresh_fal_health(app)
        choices = _source_backend_choices(app)
        if not choices:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no reviewed remix backend is available",
            )
        form = dict(fields)
        errors: list[str] = []
        selected_backend = _source_backend_id(choices, fields.get("backend"))
        if selected_backend is None:
            errors.append("Select a reviewed remix backend for this source.")
        project_title = fields.get("project_title", "").strip()
        youtube_url = fields.get("youtube_url", "").strip()
        if not _truthy_form_value(fields.get("rights_confirmation")):
            errors.append("Confirm that you have the rights to use this source.")
        try:
            validated_url = validate_youtube_url(youtube_url)
            video_id = _youtube_video_id(validated_url)
        except ValueError as exc:
            errors.append(str(exc))
            validated_url = ""
            video_id = ""
        if not project_title or len(project_title) > PROJECT_TITLE_MAX_LENGTH:
            errors.append(f"Project title must contain 1-{PROJECT_TITLE_MAX_LENGTH} characters.")
        if errors:
            return render(
                request,
                "source_new.html",
                {
                    "form": form,
                    "errors": errors,
                    **_source_backend_form_context(app, choices, form),
                },
                response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            with app.state.session_factory() as session:
                project = create_project(session, job_type=JobType.COVER, title=project_title)
                asset = create_source_asset(
                    session,
                    project=project,
                    origin=SourceAssetOrigin.YOUTUBE,
                    display_title=project_title,
                    youtube_url=validated_url,
                    youtube_video_id=video_id,
                    rights_confirmation_at=utc_now(),
                    preferred_remix_backend=selected_backend,
                )
                session.commit()
                source_asset_id = asset.id
        except (KeyError, ValueError) as exc:
            return render(
                request,
                "source_new.html",
                {
                    "form": form,
                    "errors": [str(exc)],
                    **_source_backend_form_context(app, choices, form),
                },
                response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        app.state.source_coordinator.enqueue_source(source_asset_id)
        return RedirectResponse(
            _route_path(request, "source_status", source_asset_id=source_asset_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/sources/uploads", dependencies=[Depends(authenticated)], name="source_uploads")
    async def source_uploads(request: Request) -> JSONResponse:
        fields = await _parse_source_init(request)
        require_csrf(request, fields)
        await _refresh_fal_health(app)
        choices = _source_backend_choices(app)
        if not choices:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no reviewed remix backend is available",
            )
        project_title = fields.get("project_title", "").strip()
        filename = fields.get("filename", "").strip()
        source_title = fields.get("source_title", "").strip() or _source_filename_title(filename)
        raw_size_value = _required_int(fields.get("byte_size", ""))
        raw_size = raw_size_value if isinstance(raw_size_value, int) else None
        errors: list[str] = []
        selected_backend = _source_backend_id(choices, fields.get("backend"))
        if selected_backend is None:
            errors.append("Select a reviewed remix backend for this source.")
        if not project_title or len(project_title) > PROJECT_TITLE_MAX_LENGTH:
            errors.append(f"Project title must contain 1-{PROJECT_TITLE_MAX_LENGTH} characters.")
        if (
            not filename
            or len(filename) > 300
            or any(ord(character) < 32 for character in filename)
        ):
            errors.append("The upload filename is invalid.")
        if not source_title or len(source_title) > 300:
            errors.append("The source title is invalid.")
        if not isinstance(raw_size, int) or not (
            raw_size is not None
            and 1 <= raw_size <= request.app.state.settings.direct_upload_max_bytes
        ):
            errors.append("Upload size must be between 1 byte and 512 MiB.")
        if not _truthy_form_value(fields.get("rights_confirmation")):
            errors.append("Confirm that you have the rights to use this source.")
        if errors:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)
        settings: ServiceSettings = app.state.settings
        try:
            with app.state.session_factory() as session:
                project = create_project(session, job_type=JobType.COVER, title=project_title)
                asset = create_source_asset(
                    session,
                    project=project,
                    origin=SourceAssetOrigin.UPLOAD,
                    display_title=source_title,
                    original_filename=filename,
                    declared_byte_size=raw_size,
                    rights_confirmation_at=utc_now(),
                    preferred_remix_backend=selected_backend,
                )
                capability = issue_asset_transfer_capability(
                    session,
                    purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
                    source_asset_id=asset.id,
                    expected_relative_path=f"{asset.id}/source.bin",
                    expected_extension=".bin",
                    expected_mime_type="application/octet-stream",
                    expected_byte_size=raw_size,
                    max_bytes=settings.direct_upload_max_bytes,
                    expires_at=utc_now() + timedelta(seconds=settings.transfer_token_ttl_seconds),
                )
                session.commit()
                source_asset_id = asset.id
                project_id = project.id
                token = capability.token
                expires_at = capability.capability.expires_at
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            {
                "source_asset_id": source_asset_id,
                "project_id": project_id,
                "status": SourceAssetStatus.AWAITING_UPLOAD.value,
                "status_url": _route_path(
                    request, "source_status", source_asset_id=source_asset_id
                ),
                "project_url": _route_path(request, "project_detail", project_id=project_id),
                "upload_complete_url": _route_path(
                    request, "source_upload_complete", source_asset_id=source_asset_id
                ),
                "cancel_url": _route_path(
                    request, "source_cancel", source_asset_id=source_asset_id
                ),
                "upload_url": (
                    f"{settings.transfer_public_base_url.rstrip('/')}/"
                    f"asset-transfer/v2/upload/{token}"
                ),
                "expires_at": _iso(expires_at),
                "max_bytes": settings.direct_upload_max_bytes,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/sources/{source_asset_id}/upload-complete",
        dependencies=[Depends(authenticated)],
        name="source_upload_complete",
    )
    async def source_upload_complete(request: Request, source_asset_id: str) -> JSONResponse:
        fields = await _parse_source_init(request)
        require_csrf(request, fields)
        should_enqueue = False
        try:
            with app.state.session_factory() as session:
                asset = get_source_asset(session, source_asset_id)
                if asset is None:
                    raise HTTPException(status_code=404, detail="source asset not found")
                if (
                    asset.origin is SourceAssetOrigin.UPLOAD
                    and asset.status is SourceAssetStatus.AWAITING_UPLOAD
                ):
                    capability = session.scalar(
                        select(AssetTransferCapability)
                        .where(
                            AssetTransferCapability.source_asset_id == asset.id,
                            AssetTransferCapability.purpose
                            == AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
                            AssetTransferCapability.status == AssetTransferStatus.CONSUMED,
                        )
                        .order_by(AssetTransferCapability.created_at.desc())
                    )
                    if (
                        capability is not None
                        and capability.received_byte_size is not None
                        and capability.received_sha256
                    ):
                        mark_source_uploaded(
                            session,
                            asset.id,
                            raw_relative_path=capability.expected_relative_path,
                            raw_byte_size=capability.received_byte_size,
                            raw_sha256=capability.received_sha256,
                        )
                if asset.status is SourceAssetStatus.AWAITING_UPLOAD:
                    raise HTTPException(status_code=409, detail="upload is not complete")
                should_enqueue = asset.status in {
                    SourceAssetStatus.UPLOADED,
                    SourceAssetStatus.QUEUED,
                }
                view = _source_asset_view(request, asset)
                session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if should_enqueue:
            app.state.source_coordinator.enqueue_source(source_asset_id)
        return JSONResponse(view, headers={"Cache-Control": "no-store"})

    @app.get(
        "/sources/{source_asset_id}/status",
        dependencies=[Depends(authenticated)],
        name="source_status",
    )
    async def source_status(request: Request, source_asset_id: str) -> Response:
        try:
            with app.state.session_factory() as session:
                asset = get_source_asset(session, source_asset_id)
                if asset is None:
                    raise HTTPException(status_code=404, detail="source asset not found")
                view = _source_asset_view(request, asset)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="source asset not found") from exc
        if "text/html" in request.headers.get("accept", "").lower():
            return render(request, "source_status.html", {"source": view})
        return JSONResponse(view, headers={"Cache-Control": "no-store"})

    @app.post(
        "/sources/{source_asset_id}/cancel-upload",
        dependencies=[Depends(authenticated)],
        name="source_cancel",
    )
    async def source_cancel(request: Request, source_asset_id: str) -> Response:
        fields = await _parse_source_init(request)
        require_csrf(request, fields)
        from ace_service.source_assets import purge_source_raw

        try:
            with app.state.session_factory() as session:
                asset = get_source_asset(session, source_asset_id)
                if asset is None:
                    raise HTTPException(status_code=404, detail="source asset not found")
                if asset.status is SourceAssetStatus.READY:
                    raise HTTPException(status_code=409, detail="ready sources cannot be cancelled")
                if asset.status in {SourceAssetStatus.FAILED, SourceAssetStatus.CANCELLED}:
                    view = _source_asset_view(request, asset)
                    session.rollback()
                else:
                    asset.status = SourceAssetStatus.CANCELLED
                    asset.error_code = "upload_cancelled"
                    asset.user_facing_error = "This source upload was cancelled."
                    asset.next_attempt_at = None
                    asset.updated_at = utc_now()
                    if asset.origin is SourceAssetOrigin.UPLOAD:
                        asset.raw_relative_path = None
                        asset.raw_byte_size = None
                        asset.raw_sha256 = None
                    from ace_service.repository import revoke_asset_transfers

                    revoke_asset_transfers(session, source_asset_id=asset.id)
                    view = _source_asset_view(request, asset)
                    session.commit()
            purge_source_raw(app.state.settings, source_asset_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="source asset not found") from exc
        if request.headers.get("accept", "").lower().startswith("application/json"):
            return JSONResponse(view, headers={"Cache-Control": "no-store"})
        return RedirectResponse(view["status_url"], status_code=status.HTTP_303_SEE_OTHER)

    @app.post(
        "/sources/{source_asset_id}/retry",
        dependencies=[Depends(authenticated)],
        name="source_retry",
    )
    async def source_retry(request: Request, source_asset_id: str) -> Response:
        fields = await _parse_source_init(request)
        require_csrf(request, fields)
        settings: ServiceSettings = app.state.settings
        upload_url: str | None = None
        issued_capability = None
        try:
            with app.state.session_factory() as session:
                asset = retry_source_asset(session, source_asset_id)
                from ace_service.repository import revoke_asset_transfers

                revoke_asset_transfers(session, source_asset_id=asset.id)
                if asset.status is SourceAssetStatus.AWAITING_UPLOAD:
                    issued_capability = issue_asset_transfer_capability(
                        session,
                        purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
                        source_asset_id=asset.id,
                        expected_relative_path=f"{asset.id}/source.bin",
                        expected_extension=".bin",
                        expected_mime_type="application/octet-stream",
                        expected_byte_size=asset.declared_byte_size,
                        max_bytes=settings.direct_upload_max_bytes,
                        expires_at=utc_now()
                        + timedelta(seconds=settings.transfer_token_ttl_seconds),
                    )
                    upload_url = (
                        f"{settings.transfer_public_base_url.rstrip('/')}/"
                        f"asset-transfer/v2/upload/{issued_capability.token}"
                    )
                view = _source_asset_view(request, asset)
                session.commit()
                expires_at = (
                    issued_capability.capability.expires_at
                    if upload_url and issued_capability is not None
                    else None
                )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if asset.status is SourceAssetStatus.QUEUED:
            app.state.source_coordinator.enqueue_source(asset.id)
        if request.headers.get("accept", "").lower().startswith("application/json"):
            payload = dict(view)
            payload.update(
                {
                    "upload_url": upload_url,
                    "expires_at": _iso(expires_at) if expires_at is not None else None,
                    "max_bytes": settings.direct_upload_max_bytes if upload_url else None,
                }
            )
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        return RedirectResponse(view["status_url"], status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/create", dependencies=[Depends(authenticated)], name="create_form")
    async def create_form(request: Request) -> Response:
        await _refresh_fal_health(app)
        estimate = None
        form = {"backend": request.query_params.get("backend", "")}
        with app.state.session_factory() as session:
            estimate = _form_estimate(session, JobType.ORIGINAL, form)
        return render(
            request,
            "original_form.html",
            {
                "form": form,
                "errors": [],
                "continuing": False,
                "estimate": estimate,
                **_backend_form_context(app, BackendOperation.TEXT_TO_MUSIC, form),
                **await _backend_pricing_context(app, BackendOperation.TEXT_TO_MUSIC, form),
            },
        )

    @app.post("/create", dependencies=[Depends(authenticated)], name="create_original")
    async def create_original(request: Request) -> Response:
        # TODO(re-enable): the private campaign maintenance gate is quarantined
        # during the usability recovery; restore the call after ordinary
        # original and cover generation is stable (owner decision #10).
        # _assert_public_enqueue_allowed(app)
        fields = await parse_form(request)
        require_csrf(request, fields)
        await _refresh_fal_health(app)
        form = dict(fields)
        with app.state.session_factory() as session:
            continuation_source = _continuation_source(
                session, fields, expected_job_type=JobType.ORIGINAL
            )
            project_id = continuation_source.project_id if continuation_source is not None else None
            try:
                selected_provider, backend_snapshot = _select_backend(
                    app, fields, BackendOperation.TEXT_TO_MUSIC
                )
            except ValueError as exc:
                return render(
                    request,
                    "original_form.html",
                    {
                        "form": form,
                        "errors": [str(exc)],
                        "continuing": project_id is not None,
                        "project_title": (
                            continuation_source.project.title
                            if continuation_source is not None
                            else None
                        ),
                        "estimate": _form_estimate(session, JobType.ORIGINAL, fields),
                        **_backend_form_context(app, BackendOperation.TEXT_TO_MUSIC, form),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
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
                        **_backend_form_context(app, BackendOperation.TEXT_TO_MUSIC, form),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            job = create_original_job(
                session,
                job_request,
                project=project_id,
                inference_provider=selected_provider.capabilities.name,
                inference_backend=selected_provider.capabilities.backend_id,
                backend_snapshot_json=backend_snapshot,
            )
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
        await _refresh_fal_health(app)
        readiness = await _readiness(app, only={"home_ingest"})
        estimate = None
        form = {"backend": request.query_params.get("backend", "")}
        with app.state.session_factory() as session:
            estimate = _form_estimate(session, JobType.COVER, form)
        return render(
            request,
            "cover_form.html",
            {
                "form": form,
                "errors": [],
                "readiness": readiness,
                "continuing": False,
                "estimate": estimate,
                **await _backend_pricing_context(
                    app,
                    (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    ),
                    form,
                ),
                **_backend_form_context(
                    app,
                    (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    ),
                    form,
                ),
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
        await _refresh_fal_health(app)
        form = dict(fields)
        with app.state.session_factory() as session:
            continuation_source = _continuation_source(
                session, fields, expected_job_type=JobType.COVER
            )
            project_id = continuation_source.project_id if continuation_source is not None else None
            cover_operations = (
                BackendOperation.AUDIO_TRANSFORM,
                BackendOperation.AUDIO_INPAINT,
                BackendOperation.AUDIO_OUTPAINT,
            )
            try:
                selected_provider, backend_snapshot = _select_backend(app, fields, cover_operations)
            except ValueError as exc:
                return render(
                    request,
                    "cover_form.html",
                    {
                        "form": form,
                        "errors": [str(exc)],
                        "readiness": await _readiness(app, only={"home_ingest"}),
                        "continuing": project_id is not None,
                        "project_title": (
                            continuation_source.project.title
                            if continuation_source is not None
                            else None
                        ),
                        "estimate": _form_estimate(session, JobType.COVER, fields),
                        **_backend_form_context(app, cover_operations, form),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
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
                        **_backend_form_context(app, cover_operations, form),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            job = create_cover_job(
                session,
                cover_request,
                project=project_id,
                inference_provider=selected_provider.capabilities.name,
                inference_backend=selected_provider.capabilities.backend_id,
                backend_snapshot_json=backend_snapshot,
            )
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
                            **_backend_form_context(app, cover_operations, form),
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

    @app.get("/library", dependencies=[Depends(authenticated)], name="library")
    async def library(request: Request) -> Response:
        raw_page = request.query_params.get("page", "1")
        try:
            page_number = int(raw_page)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="library page is invalid") from exc
        query = request.query_params.get("q", "")
        project_id = request.query_params.get("project") or None
        sort = request.query_params.get("sort", "recent")
        try:
            with app.state.session_factory() as session:
                if project_id is not None and get_project(session, project_id) is None:
                    raise HTTPException(status_code=404, detail="project not found")
                result = query_media_library(
                    session,
                    MediaLibraryQuery(q=query, project_id=project_id, sort=sort, page=page_number),
                )
                project_views = [
                    _project_view(request, project) for project in list_projects(session)
                ]
                media_views = [_media_item_view(request, item) for item in result.items]
                playlist_views = [
                    _playlist_view(request, playlist, include_entries=False)
                    for playlist in list_playlists(session)
                ]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return render(
            request,
            "library.html",
            {
                "media_items": media_views,
                "library_query": result.query,
                "library_has_next": result.has_next,
                "library_projects": project_views,
                "library_playlists": playlist_views,
                "library_page": result.page,
            },
        )

    @app.get("/playlists", dependencies=[Depends(authenticated)], name="playlists")
    async def playlists(request: Request) -> Response:
        with app.state.session_factory() as session:
            views = [
                _playlist_view(request, playlist, include_entries=False)
                for playlist in list_playlists(session)
            ]
        return render(request, "playlists.html", {"playlists": views})

    @app.post("/playlists", dependencies=[Depends(authenticated)], name="create_playlist")
    async def create_playlist_route(request: Request) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                playlist = create_custom_playlist(session, fields.get("title", ""))
                session.commit()
                playlist_id = playlist.id
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(
            _route_path(request, "playlist_detail", playlist_id=playlist_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get(
        "/playlists/{playlist_id}", dependencies=[Depends(authenticated)], name="playlist_detail"
    )
    async def playlist_detail(request: Request, playlist_id: str) -> Response:
        with app.state.session_factory() as session:
            playlist = get_playlist(session, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404, detail="playlist not found")
            view = _playlist_view(request, playlist, include_entries=True)
            available = [
                _media_item_view(request, item) for item in list_media_library(session, page=1)
            ]
        return render(
            request,
            "playlist_detail.html",
            {"playlist": view, "available_media_items": available[:50]},
        )

    @app.post(
        "/playlists/{playlist_id}/rename",
        dependencies=[Depends(authenticated)],
        name="rename_playlist",
    )
    async def rename_playlist_route(request: Request, playlist_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                rename_playlist(session, playlist_id, fields.get("title", ""))
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(
            _route_path(request, "playlist_detail", playlist_id=playlist_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/playlists/{playlist_id}/delete",
        dependencies=[Depends(authenticated)],
        name="delete_playlist",
    )
    async def delete_playlist_route(request: Request, playlist_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                delete_playlist(session, playlist_id)
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist not found") from exc
        return _offline_invalidation_redirect(
            request, "playlists", kind="playlist", identifier=playlist_id
        )

    @app.post(
        "/playlists/{playlist_id}/entries",
        dependencies=[Depends(authenticated)],
        name="add_playlist_entry",
    )
    async def add_playlist_entry_route(request: Request, playlist_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        media_item_id = fields.get("media_item_id", "").strip()
        if not media_item_id:
            raise HTTPException(status_code=422, detail="media item is required")
        try:
            with app.state.session_factory() as session:
                add_playlist_entry(session, playlist_id, media_item_id)
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist or media item not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            _route_path(request, "playlist_detail", playlist_id=playlist_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/playlists/{playlist_id}/entries/{entry_id}/remove",
        dependencies=[Depends(authenticated)],
        name="remove_playlist_entry",
    )
    async def remove_playlist_entry_route(
        request: Request, playlist_id: str, entry_id: int
    ) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                remove_playlist_entry(session, playlist_id, entry_id)
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist entry not found") from exc
        return RedirectResponse(
            _route_path(request, "playlist_detail", playlist_id=playlist_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/playlists/{playlist_id}/entries/reorder",
        dependencies=[Depends(authenticated)],
        name="reorder_playlist_entries",
    )
    async def reorder_playlist_entries_route(request: Request, playlist_id: str) -> JSONResponse:
        raw_body = await request.body()
        if len(raw_body) > 256 * 1024:
            raise HTTPException(status_code=413, detail="playlist reorder body is too large")
        try:
            body = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="playlist reorder body is invalid") from exc
        if not isinstance(body, dict) or set(body) != {"csrf_token", "entry_ids"}:
            raise HTTPException(status_code=422, detail="playlist reorder body is invalid")
        require_csrf(request, {"csrf_token": str(body.get("csrf_token", ""))})
        entry_ids = body.get("entry_ids")
        if not isinstance(entry_ids, list) or any(
            isinstance(value, bool) or not isinstance(value, int) for value in entry_ids
        ):
            raise HTTPException(status_code=422, detail="playlist entry IDs are invalid")
        try:
            with app.state.session_factory() as session:
                entries = reorder_playlist_entries(session, playlist_id, entry_ids)
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist not found") from exc
        except (PlaylistConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {"playlist_id": playlist_id, "entry_ids": [entry.id for entry in entries]},
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/player/queue/playlist/{playlist_id}",
        dependencies=[Depends(authenticated)],
        name="player_playlist_queue",
    )
    async def player_playlist_queue(request: Request, playlist_id: str) -> JSONResponse:
        with app.state.session_factory() as session:
            playlist = get_playlist(session, playlist_id)
            if playlist is None:
                raise HTTPException(status_code=404, detail="playlist not found")
            try:
                items = [
                    _safe_queue_media_view(request, entry.media_item, entry=entry)
                    for entry in list_playlist_entries(session, playlist_id)
                    if entry.media_item.deletion_state is MediaDeletionState.ACTIVE
                ]
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            revision = _playlist_revision(playlist)
            context = {
                "type": "playlist",
                "playlist_id": playlist.id,
                "playlist_title": playlist.title,
                "playlist_kind": playlist.kind.value,
                "revision": revision,
            }
        return JSONResponse(
            {
                "schema_version": 2,
                "context": context,
                "items": items,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/player/queue/library",
        dependencies=[Depends(authenticated)],
        name="player_library_queue",
    )
    async def player_library_queue(request: Request) -> JSONResponse:
        with app.state.session_factory() as session:
            try:
                items = list_media_queue(
                    session,
                    q=request.query_params.get("q", ""),
                    project_id=request.query_params.get("project") or None,
                    sort=request.query_params.get("sort", "recent"),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                payload = [_safe_queue_media_view(request, item) for item in items]
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            query = {
                "q": request.query_params.get("q", "").strip(),
                "project": request.query_params.get("project") or None,
                "sort": request.query_params.get("sort", "recent"),
            }
        return JSONResponse(
            {
                "schema_version": 2,
                "context": {
                    "type": "library",
                    "playlist_id": None,
                    "playlist_title": "Library",
                    "playlist_kind": None,
                    "revision": _library_revision(payload, query),
                },
                "items": payload,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/media/{media_item_id}/rename",
        dependencies=[Depends(authenticated)],
        name="rename_media",
    )
    async def rename_media_route(request: Request, media_item_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                rename_media_item(session, media_item_id, fields.get("title", ""))
                session.commit()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="media item not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            _route_path(request, "library"), status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post(
        "/media/{media_item_id}/delete",
        dependencies=[Depends(authenticated)],
        name="delete_media",
    )
    async def delete_media_route(request: Request, media_item_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        service = MediaLibraryService(app.state.settings, app.state.session_factory)
        try:
            service.request_item_deletion(media_item_id)
            service.reconcile_item_deletion(media_item_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="media item not found") from exc
        except (MediaLibraryError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="audio deletion is pending and will be retried safely",
            ) from exc
        return _offline_invalidation_redirect(
            request, "library", kind="media", identifier=media_item_id
        )

    @app.post(
        "/jobs/{job_id}/cancel",
        dependencies=[Depends(authenticated)],
        name="cancel_job",
    )
    async def cancel_job_route(request: Request, job_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        with app.state.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            if not _job_cancellation_available(app, job):
                raise HTTPException(status_code=409, detail="cancellation is not available")
            try:
                request_job_cancellation(session, job.id)
                session.commit()
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        _enqueue(app, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/projects/{project_id}/delete",
        dependencies=[Depends(authenticated)],
        name="delete_project",
    )
    async def delete_project_route(request: Request, project_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        with app.state.session_factory() as session:
            project = get_project(session, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            if not project_is_deletable(session, project.id):
                raise HTTPException(status_code=409, detail="project has active jobs")
            if fields.get("confirm_title", "") != project.title:
                raise HTTPException(status_code=422, detail="type the current project title")
            project_playlist_id = next(
                (
                    playlist.id
                    for playlist in project.playlists
                    if playlist.kind is PlaylistKind.PROJECT
                ),
                None,
            )
            create_project_deletion_audit(session, project.id)
            session.commit()
        service = MediaLibraryService(app.state.settings, app.state.session_factory)
        try:
            service.reconcile_project_deletion(project_id)
        except (MediaLibraryError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="project audio deletion is pending; retry after cleanup converges",
            ) from exc
        _remove_empty_project_dirs(app.state.settings, project_id)
        return _offline_invalidation_redirect(
            request,
            "projects",
            kind="project",
            identifier=project_id,
            extra_markers=(("playlist", project_playlist_id),) if project_playlist_id else (),
        )

    @app.get(
        "/projects/{project_id}",
        dependencies=[Depends(authenticated)],
        name="project_detail",
    )
    async def project_detail(request: Request, project_id: str) -> Response:
        with app.state.session_factory() as session:
            context = _project_detail_context(request, session, project_id)
        return render(request, "project_detail.html", context)

    @app.get(
        "/projects/{project_id}/remixes/new",
        dependencies=[Depends(authenticated)],
        name="new_source_remix",
    )
    async def new_source_remix(request: Request, project_id: str) -> Response:
        await _refresh_fal_health(app)
        choices = _source_backend_choices(app)
        with app.state.session_factory() as session:
            project = get_project(session, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            source = project.source_asset
            if (
                source is None
                or source.status is not SourceAssetStatus.READY
                or source.media_item is None
                or source.media_item.deletion_state is not MediaDeletionState.ACTIVE
                or not any(
                    media_file.format is OutputFormat.MP3
                    and media_file.is_playback == 1
                    and media_file.state is MediaFileState.ACTIVE
                    for media_file in source.media_item.files
                )
            ):
                raise HTTPException(status_code=409, detail="source is not ready to remix")
            source_view = _source_asset_view(request, source)
            form = {
                "backend": request.query_params.get("backend", ""),
                "source_media_item_id": source.media_item.id,
                "clip_start_seconds": "0",
                "clip_end_seconds": str(source.duration_seconds or ""),
                "target_style": "",
                "duration_mode": "source",
                "variation_count": "1",
                "output_format": "mp3",
            }
        if not choices:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no reviewed remix backend is available",
            )
        return render(
            request,
            "remix_form.html",
            {
                "source": source_view,
                "form": form,
                "errors": [],
                "source_duration_seconds": source.duration_seconds,
                "remix_url": _route_path(request, "create_source_remix", project_id=project.id),
                **_source_backend_form_context(
                    app,
                    choices,
                    form,
                    preferred_backend=source.preferred_remix_backend,
                ),
            },
        )

    @app.post(
        "/projects/{project_id}/remixes",
        dependencies=[Depends(authenticated)],
        name="create_source_remix",
    )
    async def create_source_remix(request: Request, project_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        await _refresh_fal_health(app)
        form = dict(fields)
        choices = _source_backend_choices(app)
        with app.state.session_factory() as session:
            project = get_project(session, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            source = project.source_asset
            if (
                source is None
                or source.media_item is None
                or source.status is not SourceAssetStatus.READY
            ):
                raise HTTPException(status_code=409, detail="source is not ready to remix")
            source_view = _source_asset_view(request, source)
            errors: list[str] = []
            try:
                selected_provider, backend_snapshot = _select_backend(
                    app,
                    fields,
                    (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    ),
                    choices=choices,
                )
                source_item_id = fields.get("source_media_item_id", "").strip()
                if source_item_id != source.media_item.id:
                    raise ValueError("selected source does not belong to this project")
                start = _optional_number(fields.get("clip_start_seconds"))
                end = _optional_number(fields.get("clip_end_seconds"))
                if (
                    not isinstance(start, (int, float))
                    or isinstance(start, bool)
                    or not isinstance(end, (int, float))
                    or isinstance(end, bool)
                ):
                    raise ValueError("start and end seconds are required")
                validate_source_range(source.media_item, float(start), float(end), backend_snapshot)
                maximum = float(backend_snapshot["source_duration_max_seconds"])
                if float(source.duration_seconds or 0) > maximum + 0.001 and not _truthy_form_value(
                    fields.get("range_confirmation")
                ):
                    raise ValueError("confirm the selected range before generating")
                request_values = _cover_form_values(fields)
                request_values["youtube_url"] = None
                request_values["duration_mode"] = fields.get("duration_mode", "source")
                if request_values["duration_mode"] == "source":
                    request_values["duration_seconds"] = None
                request_values["rights_confirmation"] = True
                cover_request = CoverRequest(**request_values)
                job = create_source_remix_job(
                    session,
                    cover_request,
                    source_media_item_id=source.media_item.id,
                    clip_start_seconds=float(start),
                    clip_end_seconds=float(end),
                    backend_snapshot_json=backend_snapshot,
                    rights_confirmation_at=utc_now(),
                    project=project.id,
                    inference_provider=selected_provider.capabilities.name,
                    inference_backend=selected_provider.capabilities.backend_id,
                )
                source.preferred_remix_backend = str(selected_provider.capabilities.backend_id)
                job_id = job.id
                session.commit()
            except (ValidationError, ValueError, KeyError) as exc:
                session.rollback()
                errors = _validation_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
            if errors:
                return render(
                    request,
                    "remix_form.html",
                    {
                        "source": source_view,
                        "form": form,
                        "errors": errors,
                        "source_duration_seconds": source.duration_seconds,
                        "remix_url": _route_path(
                            request, "create_source_remix", project_id=project.id
                        ),
                        **_source_backend_form_context(
                            app,
                            choices,
                            form,
                            preferred_backend=source.preferred_remix_backend,
                        ),
                    },
                    response_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        _enqueue(app, job_id)
        return RedirectResponse(
            _route_path(request, "job_detail", job_id=job_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
        await _refresh_fal_health(app)
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
                continuation_operations = (
                    (BackendOperation.TEXT_TO_MUSIC,)
                    if source.job_type is JobType.ORIGINAL
                    else (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    )
                )
                continuation_choices = _backend_choices(app, continuation_operations)
                if source.inference_backend in {
                    item["backend_id"] for item in continuation_choices
                }:
                    form["backend"] = source.inference_backend
                elif continuation_choices:
                    form["backend"] = continuation_choices[0]["backend_id"]
                    form["backend_note"] = (
                        "The original backend is no longer enabled; this version uses "
                        "the current default."
                    )
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
                    **_backend_form_context(app, BackendOperation.TEXT_TO_MUSIC, form),
                    **await _backend_pricing_context(app, BackendOperation.TEXT_TO_MUSIC, form),
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
                **await _backend_pricing_context(
                    app,
                    (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    ),
                    form,
                ),
                **_backend_form_context(
                    app,
                    (
                        BackendOperation.AUDIO_TRANSFORM,
                        BackendOperation.AUDIO_INPAINT,
                        BackendOperation.AUDIO_OUTPAINT,
                    ),
                    form,
                ),
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

    @app.get(
        "/media/library/{media_file_id}",
        dependencies=[Depends(authenticated)],
        name="library_media",
    )
    async def library_media(media_file_id: int) -> FileResponse:
        with app.state.session_factory() as session:
            media_file = get_media_file(session, media_file_id)
            if media_file is None:
                raise HTTPException(status_code=404, detail="media file not found")
            if (
                media_file.format is not OutputFormat.MP3
                or media_file.is_playback != 1
                or media_file.mime_type != "audio/mpeg"
            ):
                raise HTTPException(status_code=404, detail="media file is not a playback MP3")
            try:
                path = verify_media_file(app.state.settings, media_file)
            except MediaLibraryError as exc:
                raise HTTPException(status_code=404, detail="media file not available") from exc
            disposition = media_file_content_disposition(media_file, attachment=False)
            media_type = media_file.mime_type
        return FileResponse(
            path,
            media_type=media_type,
            headers=_media_response_headers(media_file, disposition),
        )

    @app.get(
        "/files/library/{media_file_id}/download",
        dependencies=[Depends(authenticated)],
        name="library_download",
    )
    async def library_download(media_file_id: int) -> FileResponse:
        with app.state.session_factory() as session:
            media_file = get_media_file(session, media_file_id)
            if media_file is None:
                raise HTTPException(status_code=404, detail="media file not found")
            try:
                path = verify_media_file(app.state.settings, media_file)
            except MediaLibraryError as exc:
                raise HTTPException(status_code=404, detail="media file not available") from exc
            disposition = media_file_content_disposition(media_file, attachment=True)
            media_type = media_file.mime_type
        return FileResponse(
            path,
            media_type=media_type,
            headers=_media_response_headers(media_file, disposition),
        )

    @app.post(
        "/media/{media_item_id}/derivative/retry",
        dependencies=[Depends(authenticated)],
        name="retry_derivative",
    )
    async def retry_derivative(request: Request, media_item_id: str) -> Response:
        fields = await parse_form(request)
        require_csrf(request, fields)
        try:
            with app.state.session_factory() as session:
                task = session.scalar(
                    select(MediaDerivativeTask).where(
                        MediaDerivativeTask.media_item_id == media_item_id
                    )
                )
                if task is None or task.media_item.project is None:
                    raise HTTPException(status_code=404, detail="derivative task not found")
                project_id = task.media_item.project_id
                retry_derivative_task(session, task.id)
                session.commit()
                task_id = task.id
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        app.state.source_coordinator.enqueue_derivative(task_id)
        return RedirectResponse(
            _route_path(request, "project_detail", project_id=project_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

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
            for key in (
                "controller_database",
                "inference_provider",
                "public_transfer",
                "capacity_management",
                "web_push",
            )
        )
        return JSONResponse(
            readiness,
            status_code=status.HTTP_200_OK if required_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def _static_app(directory: Path) -> Any:
    from starlette.staticfiles import StaticFiles

    return StaticFiles(directory=str(directory), check_dir=True)


def _capacity_views(app: FastAPI, session: Any) -> list[dict[str, Any]]:
    """Render only safe lease facts; provider identity and instance data stay server-side."""

    now = datetime.now(UTC)
    views: list[dict[str, Any]] = []
    for manager in app.state.capacity_registry.managers:
        lease = session.get(CapacityLease, manager.key)
        state = lease.state if lease is not None else "cold"
        display_state = {
            "retained": "busy",
            "idle": "ready",
            "release_overdue": "release needs attention",
        }.get(state, state)
        label = "Salad" if manager.provider is ProviderName.SALAD else "RunPod"
        retained_minutes = None
        release_minutes = None
        release_at = None
        if lease is not None and lease.warmed_at is not None:
            retained_minutes = max(0, int((now - lease.warmed_at).total_seconds()) // 60)
        if lease is not None and lease.release_due_at is not None:
            release_at = _iso(lease.release_due_at)
            release_minutes = max(0, int((lease.release_due_at - now).total_seconds()) // 60)
        views.append(
            {
                "provider": label,
                "state": display_state,
                "retained_minutes": retained_minutes,
                "release_at": release_at,
                "release_minutes": release_minutes,
                "warning": state == "release_overdue"
                or (lease is not None and lease.last_error_code in {"drift", "unsafe_active_work"}),
            }
        )
    return views


def _notification_worker_script() -> str:
    """Return the checked-in unified service-worker source."""

    return (_STATIC / "service_worker.js").read_text(encoding="utf-8")


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
        "source_style": fields.get("source_style") or None,
        "remix_guidance": fields.get("remix_guidance") or None,
        "source_lyrics": fields.get("source_lyrics") or None,
        "lyrics": fields.get("lyrics") or None,
        "profile_id": fields.get("profile_id", "fast-beta-v1"),
        "audio_cover_strength": _optional_number(fields.get("audio_cover_strength"), default=0.65),
        "cover_noise_strength": _optional_number(fields.get("cover_noise_strength"), default=0.0),
        "strength": _optional_number(fields.get("strength")),
        "start_seconds": _optional_number(fields.get("start_seconds")),
        "end_seconds": _optional_number(fields.get("end_seconds")),
        "before_seconds": _optional_number(fields.get("before_seconds")),
        "after_seconds": _optional_number(fields.get("after_seconds")),
        "duration_mode": fields.get("duration_mode", "source"),
        "duration_seconds": _optional_number(fields.get("duration_seconds")),
        "variation_count": _required_int(fields.get("variation_count", "1")),
        "seed": _optional_int(fields.get("seed")),
        "output_format": fields.get("output_format", OutputFormat.MP3.value),
        "rights_confirmation": True,
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
        return _renderable_form_values(
            {
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
        )

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
        "source_style": generation.get("source_style"),
        "remix_guidance": generation["remix_guidance"],
        "source_lyrics": generation.get("source_lyrics"),
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
    return _renderable_form_values(
        {
            **common,
            "youtube_url": job.source_url,
            "target_style": generation["target_style"],
            "source_style": generation.get("source_style"),
            "remix_guidance": generation["remix_guidance"],
            "source_lyrics": generation.get("source_lyrics"),
            "lyrics": generation["lyrics"],
            "audio_cover_strength": generation["audio_cover_strength"],
            "cover_noise_strength": generation["cover_noise_strength"],
            "duration_mode": generation["duration_mode"],
            "duration_seconds": (
                generation["duration_seconds"] if generation["duration_mode"] == "custom" else None
            ),
            "seed": generation["seed"],
            "output_format": generation["output_format"],
        }
    )


def _renderable_form_values(form: Mapping[str, Any]) -> dict[str, Any]:
    """Keep absent optional values blank when a form is rendered.

    Jinja renders Python ``None`` as the literal text ``None``. If that text
    reaches an HTML input, the browser submits it as user input and strict
    request validation rejects an otherwise valid continuation with an
    optional field left at its default.
    """

    return {name: "" if value is None else value for name, value in form.items()}


def _optional_number(value: str | None, *, default: float | None = None) -> float | None:
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return value  # type: ignore[return-value]


def _backend_form_number(fields: Mapping[str, str], name: str) -> float | None:
    raw = fields.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


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


def _offline_invalidation_path(
    request: Request,
    route_name: str,
    *,
    kind: str,
    identifier: str,
    extra_markers: tuple[tuple[str, str], ...] = (),
) -> str:
    markers = [("offline_invalidate", f"{kind}:{identifier}")]
    markers.extend(
        ("offline_invalidate", f"{extra_kind}:{extra_identifier}")
        for extra_kind, extra_identifier in extra_markers
        if extra_identifier
    )
    return f"{_route_path(request, route_name)}?{urlencode(markers)}"


def _offline_invalidation_redirect(
    request: Request,
    route_name: str,
    *,
    kind: str,
    identifier: str,
    extra_markers: tuple[tuple[str, str], ...] = (),
) -> RedirectResponse:
    response = RedirectResponse(
        _offline_invalidation_path(
            request,
            route_name,
            kind=kind,
            identifier=identifier,
            extra_markers=extra_markers,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    marker_values = [f"{kind}:{identifier}"] + [
        f"{extra_kind}:{extra_identifier}"
        for extra_kind, extra_identifier in extra_markers
        if extra_identifier
    ]
    root_path = str(request.scope.get("root_path", "") or "")
    scope_key = re.sub(r"[^A-Za-z0-9_-]+", "_", root_path.strip("/") or "root")
    cookie_key = f"audioventura-offline-invalidate-{scope_key}"
    previous = request.cookies.get(cookie_key, "")
    pending = list(
        dict.fromkeys([marker for marker in previous.split(",") if marker] + marker_values)
    )
    cookie_path = str(request.scope.get("root_path", "") or "/").rstrip("/") or "/"
    response.set_cookie(
        cookie_key,
        ",".join(pending),
        max_age=300,
        path=cookie_path,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _worker_scope(request: Request) -> str:
    """Return the normalized public root controlled by the current app."""

    root_path = str(request.scope.get("root_path", "") or "")
    return f"{root_path}/" if root_path else "/"


_SOURCE_STATUS_LABELS = {
    SourceAssetStatus.AWAITING_UPLOAD: "Waiting for upload",
    SourceAssetStatus.UPLOADED: "Upload received",
    SourceAssetStatus.QUEUED: "Queued for source preparation",
    SourceAssetStatus.PREPARING: "Extracting and preparing audio",
    SourceAssetStatus.READY: "Ready to remix",
    SourceAssetStatus.FAILED: "Source preparation failed",
    SourceAssetStatus.CANCELLED: "Upload cancelled",
}


def _source_asset_view(request: Request, asset: SourceAsset) -> dict[str, Any]:
    declared_size = asset.declared_byte_size
    received_size = asset.raw_byte_size
    upload_progress = None
    if declared_size and received_size is not None:
        upload_progress = min(100, round(received_size / declared_size * 100))
    media = asset.media_item
    playable = media is not None and media.deletion_state is MediaDeletionState.ACTIVE
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "title": asset.display_title,
        "origin": asset.origin.value,
        "status": asset.status.value,
        "status_label": _SOURCE_STATUS_LABELS[asset.status],
        "filename": asset.original_filename,
        "declared_byte_size": declared_size,
        "received_byte_size": received_size,
        "declared_size_label": _size_label(declared_size) if declared_size else None,
        "received_size_label": _size_label(received_size) if received_size else None,
        "upload_progress": upload_progress,
        "duration_seconds": asset.duration_seconds,
        "error": asset.user_facing_error,
        "error_code": asset.error_code,
        "project_url": _route_path(request, "project_detail", project_id=asset.project_id),
        "status_url": _route_path(request, "source_status", source_asset_id=asset.id),
        "upload_complete_url": _route_path(
            request, "source_upload_complete", source_asset_id=asset.id
        ),
        "cancel_url": _route_path(request, "source_cancel", source_asset_id=asset.id),
        "retry_url": _route_path(request, "source_retry", source_asset_id=asset.id),
        "remix_url": (
            _route_path(request, "new_source_remix", project_id=asset.project_id)
            if playable
            else None
        ),
        "media": _media_item_view(request, media) if media is not None else None,
        "playable": playable,
        "can_cancel": asset.status
        in {
            SourceAssetStatus.AWAITING_UPLOAD,
            SourceAssetStatus.UPLOADED,
            SourceAssetStatus.QUEUED,
            SourceAssetStatus.PREPARING,
        },
        "can_retry": asset.status in {SourceAssetStatus.FAILED, SourceAssetStatus.CANCELLED},
    }


def _source_backend_form_context(
    app: FastAPI,
    choices: list[dict[str, Any]],
    form: Mapping[str, Any],
    *,
    preferred_backend: Any = None,
) -> dict[str, Any]:
    selected, backend_note = _resolve_source_backend(
        app,
        choices,
        explicit_backend=form.get("backend"),
        preferred_backend=preferred_backend,
    )
    choice = next((item for item in choices if item["backend_id"] == selected), None)
    native_formats = choice["native_formats"] if choice is not None else ["mp3"]
    return {
        "backend_choices": choices,
        "selected_backend": selected,
        "backend_note": backend_note,
        "selected_backend_is_fal": choice is not None and choice["provider"] == "fal.ai",
        "selected_backend_is_mock": choice is not None
        and choice["provider"] == ProviderName.MOCK.value,
        "selected_native_formats": list(native_formats),
        "selected_output_format": _preferred_output_format(native_formats),
    }


def _source_filename_title(filename: str) -> str:
    """Derive a bounded display title without treating the filename as a path."""

    basename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = Path(basename).stem.strip()
    stem = re.sub(r"\s+", " ", stem)
    return (stem or "Uploaded source")[:300]


def _truthy_form_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _youtube_video_id(value: str) -> str:
    validated = validate_youtube_url(value)
    parsed = urlsplit(validated)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [""])[0]
    return parsed.path.strip("/").split("/", 1)[-1]


async def _parse_source_init(request: Request) -> dict[str, str]:
    """Parse the small source initialization body, never the media payload."""

    body = await request.body()
    if len(body) > 128 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="source initialization is too large",
        )
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="source initialization is invalid") from exc
        if not isinstance(payload, dict) or len(payload) > 12:
            raise HTTPException(status_code=422, detail="source initialization is invalid")
        fields = {
            str(key): str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in payload.items()
        }
        if "csrf_token" not in fields and request.headers.get("x-csrf-token"):
            fields["csrf_token"] = request.headers["x-csrf-token"]
        return fields
    return await parse_form(request)


def _project_view(request: Request, project: Project) -> dict[str, Any]:
    deletable = all(
        job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        for job in project.jobs
    )
    return {
        "project_id": project.id,
        "title": project.title,
        "job_type": project.job_type.value,
        "job_type_label": (
            "Cover project" if project.job_type is JobType.COVER else "Original project"
        ),
        "job_count": len(project.jobs),
        "media_count": len(project.media_items),
        "deletable": deletable,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "source_status": project.source_asset.status.value if project.source_asset else None,
        "source_url": (
            _route_path(request, "source_status", source_asset_id=project.source_asset.id)
            if project.source_asset
            else None
        ),
        "detail_url": _route_path(request, "project_detail", project_id=project.id),
        "rename_url": _route_path(request, "rename_project", project_id=project.id),
        "delete_url": _route_path(request, "delete_project", project_id=project.id),
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
    source = project.source_asset
    return {
        "project": _project_view(request, project),
        "source": _source_asset_view(request, source) if source is not None else None,
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
    provider_message: str | None = None
    provider_progress: float | None = None
    detail_scope: str | None = None
    if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        active_attempt = next(
            (item for item in attempts if item.variation_index == (job.current_variation or 1)),
            None,
        )
        progress = (
            active_attempt.provider_result_json or active_attempt.runpod_result_json
            if active_attempt is not None
            else None
        )
        if isinstance(progress, dict) and progress.get("kind") == "audioventura_progress_v1":
            candidate_phase = progress.get("phase")
            candidate_observed_at = progress.get("observed_at")
            if isinstance(candidate_phase, str) and candidate_phase in _PHASE_LABELS:
                phase = candidate_phase
                if isinstance(candidate_observed_at, str) and len(candidate_observed_at) <= 64:
                    phase_observed_at = candidate_observed_at
                candidate_message = progress.get("provider_message")
                if (
                    isinstance(candidate_message, str)
                    and 0 < len(candidate_message) <= PROVIDER_PROGRESS_MESSAGE_MAX_LENGTH
                    and all(character.isprintable() for character in candidate_message)
                ):
                    provider_message = candidate_message
                candidate_progress = progress.get("provider_progress")
                if (
                    isinstance(candidate_progress, (int, float))
                    and not isinstance(candidate_progress, bool)
                    and math.isfinite(float(candidate_progress))
                    and 0 <= float(candidate_progress) <= 1
                ):
                    provider_progress = float(candidate_progress)
                candidate_scope = progress.get("detail_scope")
                if isinstance(candidate_scope, str) and candidate_scope in {"job", "deployment"}:
                    detail_scope = candidate_scope
    phase_label = _PHASE_LABELS.get(phase) if phase is not None else None
    phase_detail_label = provider_message or phase_label
    if phase_detail_label is not None and provider_progress is not None:
        phase_detail_label += f" — {round(provider_progress * 100)}%"
    if phase_detail_label is not None and detail_scope == "deployment":
        phase_detail_label += " · Deployment status (inferred)"
    view = {
        "job_id": job.id,
        "inference_provider": job.inference_provider or "runpod",
        "inference_backend": str(job_backend(job)),
        "backend_label": (
            job.backend_snapshot_json.get("label")
            if isinstance(job.backend_snapshot_json, dict)
            and isinstance(job.backend_snapshot_json.get("label"), str)
            else str(job_backend(job))
        ),
        "backend_endpoint_id": (
            job.backend_snapshot_json.get("endpoint_id")
            if isinstance(job.backend_snapshot_json, dict)
            else None
        ),
        "backend_operation": (
            job.backend_snapshot_json.get("operation")
            if isinstance(job.backend_snapshot_json, dict)
            else None
        ),
        "backend_native_formats": (
            job.backend_snapshot_json.get("native_formats", [])
            if isinstance(job.backend_snapshot_json, dict)
            else []
        ),
        "job_type": job.job_type.value,
        "job_type_label": "Cover" if job.job_type is JobType.COVER else "Original song",
        "status": job.status.value,
        "status_label": _STATUS_LABELS[job.status],
        "source_title": job.sanitized_source_title,
        "source_url": job.source_url,
        "source_media_item_id": job.source_media_item_id,
        "source_clip_start_seconds": job.source_clip_start_seconds,
        "source_clip_end_seconds": job.source_clip_end_seconds,
        "source_clip_duration_seconds": job.source_clip_duration_seconds,
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
        "error": (
            job.user_facing_error if job.status in {JobStatus.FAILED, JobStatus.CANCELLED} else None
        ),
        "error_code": (
            job.error_code if job.status in {JobStatus.FAILED, JobStatus.CANCELLED} else None
        ),
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "elapsed_seconds": _elapsed(job),
        "phase": phase,
        "phase_label": phase_label,
        "phase_detail_label": phase_detail_label,
        "phase_observed_at": phase_observed_at,
        "provider_message": provider_message,
        "provider_progress": provider_progress,
        "detail_scope": detail_scope,
        "cancel_requested_at": _iso(job.cancel_requested_at),
        "cancel_completed_at": _iso(job.cancel_completed_at),
        "cancel_outcome": job.cancel_outcome,
        "cancel_available": _job_cancellation_available(request.app, job),
        "cancel_job_url": _route_path(request, "cancel_job", job_id=job.id),
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
    result_value = attempt.provider_result_json or attempt.runpod_result_json
    result = cast(dict[str, Any], result_value) if isinstance(result_value, dict) else {}
    output_value = result.get("output")
    output = cast(dict[str, Any], output_value) if isinstance(output_value, dict) else {}
    worker_value = result.get("worker")
    worker = cast(dict[str, Any], worker_value) if isinstance(worker_value, dict) else {}
    return {
        "variation_index": attempt.variation_index,
        "inference_provider": attempt.inference_provider or "runpod",
        "inference_backend": attempt.inference_backend,
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
        "output_bytes": output.get("bytes"),
        "output_sha256": output.get("sha256"),
        "duration_seconds": output.get("duration_seconds"),
        "corpus_index": output.get("corpus_index"),
        "target_duration_seconds": output.get("target_duration_seconds"),
        "duration_tolerance_seconds": output.get("duration_tolerance_seconds"),
    }


def _output_view(request: Request, output: Output) -> dict[str, Any]:
    media_item = output.media_item
    if media_item is not None:
        playback_file = next(
            (
                media_file
                for media_file in media_item.files
                if media_file.format is OutputFormat.MP3
                and media_file.is_playback == 1
                and media_file.state is MediaFileState.ACTIVE
            ),
            None,
        )
        primary_file = next(
            (
                media_file
                for media_file in media_item.files
                if media_file.is_primary_download == 1 and media_file.state is MediaFileState.ACTIVE
            ),
            None,
        )
        active = media_item.deletion_state is MediaDeletionState.ACTIVE
        playable = active and playback_file is not None
        download_file = primary_file if primary_file is not None else playback_file
        derivative = media_item.derivative_task
        playback_sha256 = None
        if playback_file is not None:
            try:
                playback_sha256 = validate_sha256(playback_file.sha256)
            except (TypeError, ValueError):
                playback_sha256 = None
        return {
            "id": output.id,
            "variation_index": output.variation_index,
            "result_index": output.result_index,
            "mime_type": output.mime_type,
            "byte_size": output.byte_size,
            "size_label": _size_label(
                primary_file.byte_size if primary_file is not None else output.byte_size
            ),
            "media_url": (
                _route_path(request, "library_media", media_file_id=playback_file.id)
                if active and playback_file is not None
                else None
            ),
            "download_url": (
                _route_path(
                    request,
                    "library_download",
                    media_file_id=download_file.id,
                )
                if active and download_file is not None
                else None
            ),
            "created_at": _iso(output.created_at),
            "title": media_item.title,
            "media_item_id": media_item.id,
            "project_id": media_item.project_id,
            "project_title": media_item.project.title if media_item.project is not None else "",
            "playback_media_file_id": playback_file.id
            if active and playback_file is not None
            else None,
            "playback_byte_size": playback_file.byte_size
            if active and playback_file is not None
            else None,
            "playback_sha256": playback_sha256 if playable else None,
            "playback_mime_type": playback_file.mime_type
            if active and playback_file is not None
            else None,
            "playback_updated_at": _iso(media_item.updated_at) if playable else None,
            "deleted": not active,
            "playable": playable,
            "preparing_mp3": active and not playable and derivative is not None,
            "derivative_status": derivative.status if derivative is not None else None,
            "derivative_error": derivative.user_facing_error if derivative is not None else None,
            "derivative_retry_url": (
                _route_path(request, "retry_derivative", media_item_id=media_item.id)
                if derivative is not None and derivative.status == "failed"
                else None
            ),
            "legacy": False,
        }
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
        "title": None,
        "media_item_id": None,
        "project_id": None,
        "project_title": "",
        "playback_media_file_id": None,
        "playback_byte_size": None,
        "playback_sha256": None,
        "playback_mime_type": None,
        "playback_updated_at": None,
        "deleted": False,
        "legacy": True,
    }


def _media_item_view(request: Request, item: MediaItem) -> dict[str, Any]:
    playback_file = next(
        (
            media_file
            for media_file in item.files
            if media_file.format is OutputFormat.MP3
            and media_file.is_playback == 1
            and media_file.state is MediaFileState.ACTIVE
        ),
        None,
    )
    primary_file = next(
        (
            media_file
            for media_file in item.files
            if media_file.is_primary_download == 1 and media_file.state is MediaFileState.ACTIVE
        ),
        None,
    )
    active = item.deletion_state is MediaDeletionState.ACTIVE
    playable = active and playback_file is not None
    download_file = primary_file if primary_file is not None else playback_file
    derivative = item.derivative_task
    project = item.project
    playback_sha256 = None
    if playback_file is not None:
        try:
            playback_sha256 = validate_sha256(playback_file.sha256)
        except (TypeError, ValueError):
            playback_sha256 = None
    return {
        "id": item.id,
        "title": item.title if active else "Deleted audio",
        "original_title": item.title,
        "project_id": item.project_id,
        "project_title": project.title if project is not None else "Deleted project",
        "project_url": (
            _route_path(request, "project_detail", project_id=item.project_id)
            if project is not None
            else None
        ),
        "duration_seconds": item.duration_seconds,
        "size_label": _size_label(
            primary_file.byte_size
            if primary_file is not None
            else playback_file.byte_size
            if playback_file is not None
            else 0
        )
        if primary_file is not None or playback_file is not None
        else None,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "deletion_state": item.deletion_state.value,
        "deleted": not active,
        "file_available": active and (primary_file is not None or playback_file is not None),
        "playable": playable,
        "media_file_id": playback_file.id if active and playback_file is not None else None,
        "playback_media_file_id": playback_file.id
        if active and playback_file is not None
        else None,
        "playback_byte_size": playback_file.byte_size
        if active and playback_file is not None
        else None,
        "playback_sha256": playback_sha256 if playable else None,
        "playback_mime_type": playback_file.mime_type
        if active and playback_file is not None
        else None,
        "playback_updated_at": _iso(item.updated_at) if playable else None,
        "primary_media_file_id": primary_file.id if active and primary_file is not None else None,
        "media_url": (
            _route_path(request, "library_media", media_file_id=playback_file.id)
            if active and playback_file is not None
            else None
        ),
        "download_url": (
            _route_path(
                request,
                "library_download",
                media_file_id=download_file.id,
            )
            if active and download_file is not None
            else None
        ),
        "rename_url": _route_path(request, "rename_media", media_item_id=item.id)
        if active
        else None,
        "delete_url": _route_path(request, "delete_media", media_item_id=item.id)
        if active
        else None,
        "preparing_mp3": active and not playable and derivative is not None,
        "derivative_status": derivative.status if derivative is not None else None,
        "derivative_error": derivative.user_facing_error if derivative is not None else None,
        "derivative_retry_url": (
            _route_path(request, "retry_derivative", media_item_id=item.id)
            if derivative is not None and derivative.status == "failed"
            else None
        ),
        "offline_context_type": "direct",
    }


def _playlist_view(
    request: Request, playlist: Playlist, *, include_entries: bool
) -> dict[str, Any]:
    entries = sorted(playlist.entries, key=lambda entry: (entry.position, entry.id))
    entry_views = []
    if include_entries:
        entry_views = [
            {
                "entry_id": entry.id,
                "position": entry.position,
                "media": _media_item_view(request, entry.media_item),
                "remove_url": _route_path(
                    request,
                    "remove_playlist_entry",
                    playlist_id=playlist.id,
                    entry_id=entry.id,
                ),
            }
            for entry in entries
        ]
    return {
        "id": playlist.id,
        "title": playlist.title,
        "kind": playlist.kind.value,
        "is_project": playlist.kind is PlaylistKind.PROJECT,
        # Automatic project playlists expose the same playlist operations as
        # custom playlists. Automatic appends remain owned by publication.
        "editable": True,
        "project_id": playlist.project_id,
        "project_url": (
            _route_path(request, "project_detail", project_id=playlist.project_id)
            if playlist.project_id is not None
            else None
        ),
        "entry_count": len(entries),
        "detail_url": _route_path(request, "playlist_detail", playlist_id=playlist.id),
        "rename_url": _route_path(request, "rename_playlist", playlist_id=playlist.id),
        "delete_url": _route_path(request, "delete_playlist", playlist_id=playlist.id),
        "add_url": _route_path(request, "add_playlist_entry", playlist_id=playlist.id),
        "reorder_url": _route_path(request, "reorder_playlist_entries", playlist_id=playlist.id),
        "queue_url": _route_path(request, "player_playlist_queue", playlist_id=playlist.id),
        "revision": _playlist_revision(playlist),
        "playlist_kind": playlist.kind.value,
        "entries": entry_views,
    }


def _queue_playback_file(item: MediaItem) -> MediaFile:
    """Return the active, server-verified MP3 representation for a queue item."""

    for media_file in item.files:
        format_value = (
            media_file.format.value
            if isinstance(media_file.format, OutputFormat)
            else str(media_file.format)
        )
        if (
            format_value == OutputFormat.MP3.value
            and media_file.is_playback == 1
            and media_file.state is MediaFileState.ACTIVE
            and media_file.mime_type == "audio/mpeg"
        ):
            if (
                isinstance(media_file.byte_size, bool)
                or not isinstance(media_file.byte_size, int)
                or media_file.byte_size <= 0
            ):
                break
            try:
                validate_sha256(media_file.sha256)
            except (TypeError, ValueError):
                break
            return media_file
    raise ValueError("queue item has no active verified MP3 playback file")


def _safe_queue_media_view(
    request: Request, item: MediaItem, *, entry: Any | None = None
) -> dict[str, Any]:
    media_file = _queue_playback_file(item)
    return {
        "id": item.id,
        "queue_entry_id": entry.id if entry is not None else None,
        "position": entry.position if entry is not None else None,
        "media_item_id": item.id,
        "media_file_id": media_file.id,
        "title": item.title,
        "project_id": item.project_id,
        "project_title": item.project.title if item.project is not None else "Deleted project",
        "duration_seconds": item.duration_seconds,
        "mime_type": "audio/mpeg",
        "byte_size": media_file.byte_size,
        "sha256": validate_sha256(media_file.sha256),
        "updated_at": _iso(item.updated_at),
        "media_updated_at": _iso(item.updated_at),
        "media_url": _route_path(request, "library_media", media_file_id=media_file.id),
        "download_url": _route_path(request, "library_download", media_file_id=media_file.id),
    }


def _playlist_revision(playlist: Playlist) -> str:
    """Hash all server-owned playlist metadata that affects an offline snapshot."""

    entries: list[dict[str, Any]] = []
    for entry in sorted(playlist.entries, key=lambda value: (value.position, value.id)):
        item = entry.media_item
        try:
            media_file = _queue_playback_file(item)
            file_view: dict[str, Any] | None = {
                "id": media_file.id,
                "sha256": validate_sha256(media_file.sha256),
                "byte_size": media_file.byte_size,
                "mime_type": media_file.mime_type,
            }
        except ValueError:
            file_view = None
        entries.append(
            {
                "entry_id": entry.id,
                "position": entry.position,
                "media_item_id": item.id,
                "title": item.title,
                "updated_at": _iso(item.updated_at),
                "file": file_view,
            }
        )
    canonical = {
        "id": playlist.id,
        "kind": playlist.kind.value,
        "project_id": playlist.project_id,
        "title": playlist.title,
        "updated_at": _iso(playlist.updated_at),
        "entries": entries,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _library_revision(items: list[dict[str, Any]], query: Mapping[str, Any]) -> str:
    canonical = {"query": dict(query), "items": items}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _media_response_headers(media_file: MediaFile, disposition: str) -> dict[str, str]:
    """Build identity and range headers for an authenticated verified file."""

    digest = validate_sha256(media_file.sha256)
    return {
        "Content-Disposition": disposition,
        "Content-Length": str(media_file.byte_size),
        "Accept-Ranges": "bytes",
        "ETag": f'"sha256-{digest}"',
        "Cache-Control": "private, no-store",
    }


def _job_cancellation_available(app: FastAPI, job: Job) -> bool:
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        return False
    if job.cancel_outcome in {"too_late", "unsupported"}:
        return False
    if job.status in {JobStatus.QUEUED, JobStatus.INGESTING, JobStatus.STAGING}:
        return True
    attempt = next(
        (
            item
            for item in job.variation_attempts
            if item.variation_index == (job.current_variation or 1)
        ),
        None,
    )
    backend_id = (
        attempt.inference_backend if attempt is not None else None
    ) or job.inference_backend
    if not backend_id:
        return True
    try:
        provider = app.state.provider_registry.get_persisted(BackendId(str(backend_id)))
    except (AttributeError, KeyError, ValueError):
        return False
    if job.status is JobStatus.CLOUD_QUEUED:
        return bool(provider.capabilities.supports_pending_cancel)
    if job.status is JobStatus.GENERATING:
        return bool(provider.capabilities.supports_running_cancel)
    return True


def _remove_empty_project_dirs(settings: ServiceSettings, project_id: str) -> None:
    """Remove only known empty per-project directories after a delete."""

    for root in (settings.paths.incoming, settings.paths.library, settings.paths.temporary):
        candidate = root / project_id
        try:
            candidate.relative_to(root)
            if candidate.is_dir() and not candidate.is_symlink() and not any(candidate.iterdir()):
                candidate.rmdir()
        except (OSError, ValueError):
            continue


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
        "inference_provider": {"ok": False, "message": "unavailable"},
        "mock_backend": {"ok": True, "message": "disabled"},
        "home_ingest": {"ok": False, "message": "unavailable"},
        "public_transfer": {"ok": False, "message": "unavailable"},
        "capacity_management": {"ok": True, "message": "disabled"},
        "web_push": {"ok": True, "message": "disabled"},
    }
    if only is None or "controller_database" in only:
        try:
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
            components["controller_database"] = {"ok": True, "message": "ready"}
        except Exception:
            components["controller_database"] = {"ok": False, "message": "database unavailable"}

    settings: ServiceSettings = app.state.settings
    if only is None or "capacity_management" in only:
        with app.state.session_factory() as session:
            leases = list(session.scalars(select(CapacityLease)))
        unhealthy = any(
            lease.state == "release_overdue" or lease.last_error_code == "drift" for lease in leases
        )
        components["capacity_management"] = {
            "ok": not unhealthy,
            "message": "release needs attention" if unhealthy else "ready",
        }
    if only is None or "web_push" in only:
        exhausted = False
        if settings.web_push_enabled:
            with app.state.session_factory() as session:
                exhausted = (
                    session.scalar(
                        select(NotificationDelivery.id)
                        .where(
                            NotificationDelivery.status == "abandoned",
                            NotificationDelivery.attempt_count >= MAX_ATTEMPTS,
                            or_(
                                NotificationDelivery.last_status_code.is_(None),
                                NotificationDelivery.last_status_code.not_in((404, 410)),
                            ),
                        )
                        .limit(1)
                    )
                    is not None
                )
        components["web_push"] = {
            "ok": (settings.web_push_enabled and not exhausted)
            or not settings.web_push_allowed_origins,
            "message": (
                "delivery retry exhausted"
                if exhausted
                else "ready"
                if settings.web_push_enabled
                else "disabled"
            ),
        }
    if only is None or "public_transfer" in only:
        parsed = settings.transfer_public_base_url
        components["public_transfer"] = {
            "ok": parsed.startswith("https://"),
            "message": "configured" if parsed.startswith("https://") else "HTTPS required",
        }

    async def probe(name: str, client: Any, method_name: str) -> None:
        if only is not None and name not in only:
            return
        if isinstance(client, FalProvider):
            cached = getattr(app.state, "fal_health_cache", {}).get(
                str(client.capabilities.backend_id)
            )
            if isinstance(cached, tuple) and len(cached) == 2:
                health = cached[1]
                if isinstance(health, ProviderHealth):
                    components[name] = {"ok": health.ok, "message": health.message}
                    return
        method = getattr(client, method_name, None)
        if method is None:
            components[name] = {"ok": False, "message": "health probe unavailable"}
            return
        try:
            result = method()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=_READINESS_PROBE_TIMEOUT_SECONDS)
            if isinstance(result, ProviderHealth):
                components[name] = {"ok": result.ok, "message": result.message}
            else:
                components[name] = {"ok": True, "message": "ready"}
        except Exception:
            components[name] = {"ok": False, "message": "unreachable"}

    await _refresh_fal_health(app)
    active_provider = app.state.provider_registry.get(app.state.provider_registry.default)
    mock_provider = next(
        (
            provider
            for provider in app.state.provider_registry.providers
            if provider.capabilities.name is ProviderName.MOCK
        ),
        None,
    )
    await asyncio.gather(
        probe("inference_provider", active_provider, "health"),
        probe("runpod_api", app.state.runpod_client, "health")
        if app.state.runpod_client is not None
        else asyncio.sleep(0),
        probe("home_ingest", app.state.home_ingest_client, "health"),
        probe("mock_backend", mock_provider, "health")
        if mock_provider is not None
        else asyncio.sleep(0),
    )
    if app.state.provider_registry.default.value == "runpod":
        components["runpod_api"] = dict(components["inference_provider"])
    required_keys = [
        "controller_database",
        "inference_provider",
        "public_transfer",
        "capacity_management",
        "web_push",
    ]
    if app.state.provider_registry.default is ProviderName.MOCK:
        required_keys.append("mock_backend")
    required_ok = all(components[key]["ok"] for key in required_keys)
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

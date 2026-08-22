"""Small synchronous repository operations for durable domain records."""

from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast, overload
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ace_service.costs import (
    ESTIMATE_HISTORY_SAMPLE_LIMIT,
    CostSummary,
    NetworkVolumeSummary,
    QuoteEstimate,
    ReconciliationResult,
    is_cost_fingerprint,
    observation_checksum,
    observation_event_checksum,
    parse_micro_usd_decimal,
    round_half_up_compute_cost_usd,
)
from ace_service.models import (
    ATTEMPT_UNAVAILABLE_REASONS,
    EVIDENCE_STATUSES,
    PROJECT_TITLE_MAX_LENGTH,
    QUOTE_UNAVAILABLE_REASONS,
    BillingObservation,
    BillingProjection,
    GpuRateCatalog,
    Job,
    JobStatus,
    JobType,
    Output,
    OutputFormat,
    Project,
    RuntimeCalibration,
    SubmissionQuote,
    TransferCapability,
    TransferDirection,
    TransferStatus,
    VariationAttempt,
    utc_now,
)
from ace_service.schemas import (
    CoverRequest,
    OriginalSongRequest,
    finalize_cover_normalized_request,
    normalize_extension,
    normalize_relative_path,
    validate_sha256,
)
from ace_service.state import validate_job_transition, validate_variation_transition

COVER_STAGING_CANCELLED_CODE = "cover_staging_cancelled"
COVER_STAGING_CANCELLED_MESSAGE = "Cover preparation was cancelled before confirmation."
RUNTIME_CALIBRATION_EVIDENCE_SOURCES = frozenset({"accepted-local-measurement-v1"})
_TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})
_NONTERMINAL_JOB_STATUSES = frozenset(JobStatus) - _TERMINAL_JOB_STATUSES
_COVER_STAGING_STATUSES = frozenset({"awaiting_confirmation", "confirmed"})
_COVER_STAGING_KEYS = frozenset({"status", "staged_at", "confirmed_at"})
_MISSING = object()
_PROGRESS_KIND = "audioventura_progress_v1"
_PROGRESS_SEQUENCES = {
    "cloud_wait": 0,
    "worker_initializing": 1,
    "worker_running": 5,
    "source_download": 10,
    "generation": 20,
    "finalizing": 30,
    "output_upload": 40,
}


@dataclass(frozen=True, slots=True)
class RollbackJobDiagnostic:
    """Bounded, non-secret classification of one job for schema-v1 rollback."""

    job_id: str
    status: str
    schema: str
    classification: str
    blocks_rollback: bool


@dataclass(frozen=True, slots=True)
class RollbackReadiness:
    """Read-only schema-v1 rollback decision and its bounded diagnostics."""

    diagnostics: tuple[RollbackJobDiagnostic, ...]

    @property
    def blockers(self) -> tuple[RollbackJobDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.blocks_rollback)

    @property
    def safe(self) -> bool:
        return not self.blockers


def _id_string(value: str | UUID | None) -> str:
    return str(value or uuid4())


def _project_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError("project title must not be empty")
    if len(title) > PROJECT_TITLE_MAX_LENGTH:
        raise ValueError(f"project title must be at most {PROJECT_TITLE_MAX_LENGTH} characters")
    return title


def _default_project_title(
    job_type: JobType, *, sanitized_source_title: str | None, prompt: str | None
) -> str:
    for candidate in (sanitized_source_title, prompt):
        if candidate is not None and candidate.strip():
            return candidate.strip()[:PROJECT_TITLE_MAX_LENGTH]
    return "Original song" if job_type is JobType.ORIGINAL else "Cover"


def create_project(
    session: Session,
    *,
    job_type: JobType,
    title: str,
    project_id: str | UUID | None = None,
) -> Project:
    project = Project(
        id=_id_string(project_id),
        job_type=job_type,
        title=_project_title(title),
    )
    session.add(project)
    session.flush()
    return project


def get_project(session: Session, project_id: str | UUID) -> Project | None:
    return session.get(Project, str(project_id))


def list_projects(session: Session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.updated_at.desc(), Project.id)))


def rename_project(session: Session, project_id: str | UUID, title: str) -> Project:
    project = get_project(session, project_id)
    if project is None:
        raise KeyError(f"unknown project: {project_id}")
    project.title = _project_title(title)
    project.updated_at = utc_now()
    session.flush()
    return project


def list_project_jobs(session: Session, project_id: str | UUID) -> list[Job]:
    if get_project(session, project_id) is None:
        raise KeyError(f"unknown project: {project_id}")
    return list(
        session.scalars(
            select(Job)
            .where(Job.project_id == str(project_id))
            .order_by(Job.created_at.desc(), Job.id.desc())
        )
    )


def resolve_continuation_source(
    session: Session, job_id: str | UUID, *, expected_job_type: JobType
) -> Job:
    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown continuation source: {job_id}")
    project = get_project(session, job.project_id)
    if project is None:
        raise ValueError("continuation source is missing its project")
    if job.job_type is not expected_job_type or project.job_type is not job.job_type:
        raise ValueError("continuation source type does not match its project or request")
    normalized = job.normalized_request_json
    if (
        not isinstance(normalized, dict)
        or normalized.get("schema_version") != 2
        or normalized.get("task_type") != job.job_type.value
    ):
        raise ValueError("continuation source is not schema-v2 compatible")
    if job.job_type is JobType.COVER:
        resolve_cover_continuation_output(job)
    return job


def resolve_cover_continuation_output(job: Job) -> tuple[Output, float]:
    """Return the first reusable completed MP3 output and its measured duration."""

    if job.job_type is not JobType.COVER or job.status is not JobStatus.COMPLETED:
        raise ValueError("cover continuation requires a completed source job")
    outputs = sorted(
        (
            output
            for output in job.outputs
            if output.result_index == 0
            and output.mime_type == "audio/mpeg"
            and output.relative_path.endswith(".mp3")
        ),
        key=lambda output: (output.variation_index, output.id),
    )
    if not outputs:
        raise ValueError("cover continuation requires a completed MP3 output")
    output = outputs[0]
    attempt = next(
        (
            item
            for item in job.variation_attempts
            if item.variation_index == output.variation_index and item.status is JobStatus.COMPLETED
        ),
        None,
    )
    result = attempt.runpod_result_json if attempt is not None else None
    result_output = result.get("output") if isinstance(result, dict) else None
    duration = result_output.get("duration_seconds") if isinstance(result_output, dict) else None
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) <= 0
    ):
        raise ValueError("cover continuation output has no measured duration")
    return output, float(duration)


def _resolve_job_project(
    session: Session,
    *,
    job_type: JobType,
    project: Project | str | UUID | None,
    sanitized_source_title: str | None,
    prompt: str | None,
) -> Project:
    if project is None:
        return create_project(
            session,
            job_type=job_type,
            title=_default_project_title(
                job_type,
                sanitized_source_title=sanitized_source_title,
                prompt=prompt,
            ),
        )
    project_id = project.id if isinstance(project, Project) else str(project)
    persisted = get_project(session, project_id)
    if persisted is None:
        raise KeyError(f"unknown project: {project_id}")
    if persisted.job_type is not job_type:
        raise ValueError("project job type does not match the new job")
    return persisted


def _token_sha256(token: str) -> str:
    if not token:
        raise ValueError("transfer token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_transfer_token(token: str) -> str:
    """Hash a capability token without ever returning or storing its plaintext."""

    return _token_sha256(token)


@dataclass(frozen=True, slots=True)
class IssuedTransfer:
    """The one-time plaintext token returned at capability creation time."""

    capability: TransferCapability
    token: str


def create_job(
    session: Session,
    *,
    job_type: JobType,
    source_url: str | None = None,
    sanitized_source_title: str | None = None,
    source_duration: float | None = None,
    prompt: str | None = None,
    lyrics: str | None = None,
    rights_confirmation_at: datetime | None = None,
    cover_strength: float | None = None,
    output_format: OutputFormat = OutputFormat.MP3,
    variation_count: int = 1,
    normalized_request_json: dict[str, Any] | None = None,
    job_id: str | UUID | None = None,
    project: Project | str | UUID | None = None,
) -> Job:
    if variation_count < 1 or variation_count > 4:
        raise ValueError("variation_count must be between 1 and 4")
    if cover_strength is not None and not 0 <= cover_strength <= 1:
        raise ValueError("cover_strength must be between 0 and 1")
    persisted_project = _resolve_job_project(
        session,
        job_type=job_type,
        project=project,
        sanitized_source_title=sanitized_source_title,
        prompt=prompt,
    )
    job = Job(
        id=_id_string(job_id),
        project_id=persisted_project.id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        source_url=source_url,
        sanitized_source_title=sanitized_source_title,
        source_duration=source_duration,
        prompt=prompt,
        lyrics=lyrics,
        rights_confirmation_at=rights_confirmation_at,
        cover_strength=cover_strength,
        output_format=output_format,
        variation_count=variation_count,
        current_variation=1,
        normalized_request_json=normalized_request_json,
    )
    session.add(job)
    persisted_project.updated_at = utc_now()
    session.flush()
    return job


def create_original_job(
    session: Session,
    request: OriginalSongRequest,
    *,
    job_id: str | UUID | None = None,
    project: Project | str | UUID | None = None,
) -> Job:
    """Persist one validated original-song request before it is enqueued."""

    return create_job(
        session,
        job_type=JobType.ORIGINAL,
        prompt=request.description,
        lyrics=request.lyrics,
        output_format=request.output_format,
        variation_count=request.variation_count,
        normalized_request_json=request.to_normalized_request_json(),
        job_id=job_id,
        project=project,
    )


def create_cover_job(
    session: Session,
    request: CoverRequest,
    *,
    rights_confirmation_at: datetime | None = None,
    job_id: str | UUID | None = None,
    project: Project | str | UUID | None = None,
) -> Job:
    """Persist one validated cover request before home ingestion is queued."""

    confirmation_at = (
        _utc_timestamp(rights_confirmation_at) if rights_confirmation_at is not None else utc_now()
    )
    return create_job(
        session,
        job_type=JobType.COVER,
        source_url=request.youtube_url,
        prompt=request.effective_prompt,
        lyrics=request.lyrics,
        rights_confirmation_at=confirmation_at,
        cover_strength=request.effective_audio_cover_strength,
        output_format=request.output_format,
        variation_count=request.variation_count,
        normalized_request_json=request.to_normalized_request_json(),
        job_id=job_id,
        project=project,
    )


def finalize_cover_job_duration(
    session: Session, job_id: str | UUID, source_duration_seconds: float
) -> Job:
    """Persist the home-probed cover duration before the first cloud attempt."""

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.job_type is not JobType.COVER:
        raise ValueError("only cover jobs have a source duration")
    if job.normalized_request_json is None:
        raise ValueError("cover job is missing its normalized request")
    finalized = finalize_cover_normalized_request(
        job.normalized_request_json, source_duration_seconds
    )
    job.normalized_request_json = finalized
    duration = float(source_duration_seconds)
    target_duration = float(finalized["resolved_target_duration_seconds"])
    job.source_duration = duration
    result = dict(job.runpod_result_json or {})
    result.update(
        {
            "schema_version": 2,
            "source_duration_seconds": duration,
            "resolved_target_duration_seconds": target_duration,
            "ace_duration_seconds": target_duration,
        }
    )
    job.runpod_result_json = result
    normalized = dict(job.normalized_request_json)
    normalized["cover_staging"] = {
        "status": "awaiting_confirmation",
        "staged_at": utc_now().isoformat(),
    }
    job.normalized_request_json = normalized
    job.updated_at = utc_now()
    session.flush()
    return job


def confirm_cover_job(session: Session, job_id: str | UUID) -> Job:
    """Atomically freeze one prepared cover for the serialized cloud queue."""

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.job_type is not JobType.COVER or job.status is not JobStatus.STAGING:
        raise ValueError("cover is not awaiting confirmation")
    normalized = dict(job.normalized_request_json or {})
    staging = dict(normalized.get("cover_staging") or {})
    state = staging.get("status")
    if state == "confirmed":
        raise ValueError("cover confirmation has already been consumed")
    if state != "awaiting_confirmation":
        raise ValueError("cover is not awaiting confirmation")
    if not isinstance(normalized.get("resolved_target_duration_seconds"), (int, float)):
        raise ValueError("cover source duration is not finalized")
    staging.update({"status": "confirmed", "confirmed_at": utc_now().isoformat()})
    normalized["cover_staging"] = staging
    job.normalized_request_json = normalized
    job.updated_at = utc_now()
    session.flush()
    return job


def cancel_cover_staging(session: Session, job_id: str | UUID) -> Job:
    """Cancel one unconfirmed schema-v2 cover while it is still staged."""

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.job_type is not JobType.COVER or job.status is not JobStatus.STAGING:
        raise ValueError("cover is not awaiting confirmation")
    normalized = job.normalized_request_json
    if not isinstance(normalized, dict) or normalized.get("schema_version") != 2:
        raise ValueError("cover cancellation is only supported for schema-v2 staging")
    staging = normalized.get("cover_staging")
    if not isinstance(staging, dict) or staging.get("status") != "awaiting_confirmation":
        raise ValueError("cover is not awaiting confirmation")
    transition_job(
        session,
        job.id,
        JobStatus.FAILED,
        error_code=COVER_STAGING_CANCELLED_CODE,
        user_facing_error=COVER_STAGING_CANCELLED_MESSAGE,
    )
    return job


def get_job(session: Session, job_id: str | UUID) -> Job | None:
    return session.get(Job, str(job_id))


def check_schema_v1_rollback_readiness(session: Session) -> RollbackReadiness:
    """Classify durable jobs without changing the database or external state."""

    diagnostics: list[RollbackJobDiagnostic] = []
    jobs = session.scalars(select(Job).order_by(Job.created_at, Job.id))
    for job in jobs:
        schema_version = _normalized_schema_version(job.normalized_request_json)
        status = job.status.value
        job_id = _bounded_job_id(job.id)
        if schema_version != 2:
            diagnostics.append(
                RollbackJobDiagnostic(
                    job_id=job_id,
                    status=status,
                    schema="v1" if schema_version == 1 else "unknown",
                    classification="legacy_or_unknown",
                    blocks_rollback=False,
                )
            )
            continue

        lifecycle_error = _v2_rollback_lifecycle_error(job)
        if lifecycle_error is not None:
            diagnostics.append(
                RollbackJobDiagnostic(
                    job_id=job_id,
                    status=status,
                    schema="v2",
                    classification="malformed_v2_lifecycle",
                    blocks_rollback=True,
                )
            )
            continue

        if job.status in _TERMINAL_JOB_STATUSES:
            classification = "terminal"
            blocks_rollback = False
        elif (
            job.job_type is JobType.COVER
            and job.status is JobStatus.STAGING
            and _cover_staging_status(job.normalized_request_json) == "awaiting_confirmation"
        ):
            classification = "unconfirmed_v2_cover_staging"
            blocks_rollback = True
        elif job.status in _NONTERMINAL_JOB_STATUSES:
            classification = "nonterminal_v2"
            blocks_rollback = True
        else:
            # A future/unknown enum value must not be treated as safe.
            classification = "malformed_v2_lifecycle"
            blocks_rollback = True
        diagnostics.append(
            RollbackJobDiagnostic(
                job_id=job_id,
                status=status,
                schema="v2",
                classification=classification,
                blocks_rollback=blocks_rollback,
            )
        )
    return RollbackReadiness(tuple(diagnostics))


def _normalized_schema_version(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        return None
    return schema_version


def _bounded_job_id(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 128 else text[:125] + "..."


def _cover_staging_status(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    staging = value.get("cover_staging")
    if not isinstance(staging, dict):
        return None
    status = staging.get("status")
    return status if isinstance(status, str) else None


def _v2_rollback_lifecycle_error(job: Job) -> str | None:
    """Return a bounded reason when a v2 lifecycle cannot be classified safely."""

    normalized = job.normalized_request_json
    if not isinstance(normalized, dict) or _normalized_schema_version(normalized) != 2:
        return "schema-v2 request is not a JSON object"
    if normalized.get("task_type") != job.job_type.value:
        return "schema-v2 task type does not match the durable job"
    if job.job_type is JobType.ORIGINAL:
        if "cover_staging" in normalized:
            return "original schema-v2 request contains cover staging metadata"
        return None

    staging = normalized.get("cover_staging", _MISSING)
    if staging is _MISSING:
        if job.status in {
            JobStatus.QUEUED,
            JobStatus.INGESTING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
        }:
            return None
        return "cover schema-v2 request is missing staging metadata"
    if not isinstance(staging, dict) or not set(staging).issubset(_COVER_STAGING_KEYS):
        return "cover staging metadata is malformed"
    staging_status = staging.get("status")
    if staging_status not in _COVER_STAGING_STATUSES:
        return "cover staging status is malformed"
    for field_name in ("staged_at", "confirmed_at"):
        value = staging.get(field_name)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 64):
            return "cover staging timestamp is malformed"
    if staging_status == "awaiting_confirmation":
        if job.status is JobStatus.STAGING or job.status in _TERMINAL_JOB_STATUSES:
            return None
        return "unconfirmed cover staging has an inconsistent job status"
    if job.status in {
        JobStatus.STAGING,
        JobStatus.CLOUD_QUEUED,
        JobStatus.GENERATING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    }:
        return None
    return "confirmed cover staging has an inconsistent job status"


def transition_job(
    session: Session,
    job_id: str | UUID,
    status: JobStatus,
    *,
    now: datetime | None = None,
    error_code: str | None = None,
    user_facing_error: str | None = None,
) -> Job:
    """Apply one validated parent-job lifecycle transition."""

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    validate_job_transition(job.status, status)
    timestamp = _utc_timestamp(now)
    if job.status is not status:
        job.status = status
    if status in {JobStatus.CLOUD_QUEUED, JobStatus.GENERATING} and job.started_at is None:
        job.started_at = timestamp
    if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        job.completed_at = timestamp
    if error_code is not None:
        job.error_code = error_code
    if user_facing_error is not None:
        job.user_facing_error = user_facing_error
    job.updated_at = timestamp
    session.flush()
    return job


def get_variation_attempt(
    session: Session, job_id: str | UUID, variation_index: int
) -> VariationAttempt | None:
    return session.scalar(
        select(VariationAttempt).where(
            VariationAttempt.job_id == str(job_id),
            VariationAttempt.variation_index == variation_index,
        )
    )


def list_variation_attempts(session: Session, job_id: str | UUID) -> list[VariationAttempt]:
    return list(
        session.scalars(
            select(VariationAttempt)
            .where(VariationAttempt.job_id == str(job_id))
            .order_by(VariationAttempt.variation_index)
        )
    )


def recent_completed_attempt_execution_ms(
    session: Session,
    *,
    job_type: JobType,
    limit: int = ESTIMATE_HISTORY_SAMPLE_LIMIT,
) -> list[int]:
    """Return measured durations of the latest completed attempts of one kind.

    Read-only input for the informational cost estimate; one variation attempt
    is one sample.  Only attempts with ``status=completed``, a non-null
    ``completed_at``, and a non-null non-negative ``execution_ms`` qualify,
    ordered newest-first with deterministic id tie-breaking and limited to the
    latest samples.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return list(
        session.scalars(
            select(VariationAttempt.execution_ms)
            .join(Job, Job.id == VariationAttempt.job_id)
            .where(
                Job.job_type == job_type,
                VariationAttempt.status == JobStatus.COMPLETED,
                VariationAttempt.completed_at.is_not(None),
                VariationAttempt.execution_ms.is_not(None),
                VariationAttempt.execution_ms >= 0,
            )
            .order_by(VariationAttempt.completed_at.desc(), VariationAttempt.id.desc())
            .limit(limit)
        )
    )


def create_variation_attempt(
    session: Session,
    *,
    job_id: str | UUID,
    variation_index: int,
) -> VariationAttempt:
    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if variation_index < 1 or variation_index > job.variation_count:
        raise ValueError("variation index is outside the job variation count")
    existing = get_variation_attempt(session, job.id, variation_index)
    if existing is not None:
        return existing
    attempt = VariationAttempt(job_id=job.id, variation_index=variation_index)
    session.add(attempt)
    session.flush()
    return attempt


def transition_variation_attempt(
    session: Session,
    attempt_id: int,
    status: JobStatus,
    *,
    now: datetime | None = None,
    error_code: str | None = None,
    user_facing_error: str | None = None,
) -> VariationAttempt:
    """Apply one validated individual variation transition."""

    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    validate_variation_transition(attempt.status, status)
    timestamp = _utc_timestamp(now)
    attempt.status = status
    if status in {JobStatus.CLOUD_QUEUED, JobStatus.GENERATING} and attempt.started_at is None:
        attempt.started_at = timestamp
    if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        attempt.completed_at = timestamp
    if error_code is not None:
        attempt.error_code = error_code
    if user_facing_error is not None:
        attempt.user_facing_error = user_facing_error
    attempt.updated_at = timestamp
    session.flush()
    return attempt


def prepare_variation_submission(
    session: Session,
    job_id: str | UUID,
    variation_index: int,
    *,
    submission_nonce: str | UUID | None = None,
    now: datetime | None = None,
) -> tuple[Job, VariationAttempt, str]:
    """Commit the variation's nonce before the external Runpod request."""

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    attempt = create_variation_attempt(session, job_id=job.id, variation_index=variation_index)
    if attempt.runpod_job_id:
        raise ValueError("variation already has a Runpod job ID")
    if attempt.submission_nonce:
        raise ValueError("variation submission outcome is uncertain")
    if attempt.status is not JobStatus.QUEUED:
        raise ValueError(f"variation is not queued: {attempt.status.value}")
    nonce = str(submission_nonce or uuid4())
    if not nonce:
        raise ValueError("submission nonce must not be empty")
    timestamp = _utc_timestamp(now)
    transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED, now=timestamp)
    if job.status in {JobStatus.QUEUED, JobStatus.STAGING}:
        transition_job(session, job.id, JobStatus.CLOUD_QUEUED, now=timestamp)
    elif job.status is not JobStatus.GENERATING and job.status is not JobStatus.CLOUD_QUEUED:
        raise ValueError(f"job is not ready for cloud submission: {job.status.value}")
    job.current_variation = variation_index
    job.current_submission_nonce = nonce
    job.current_runpod_job_id = None
    job.updated_at = timestamp
    attempt.submission_nonce = nonce
    attempt.updated_at = timestamp
    session.flush()
    return job, attempt, nonce


def persist_variation_runpod_job_id(
    session: Session,
    attempt_id: int,
    runpod_job_id: str,
    *,
    submission_nonce: str,
    now: datetime | None = None,
) -> VariationAttempt:
    """Persist a Runpod ID immediately after an accepted variation submission."""

    if not runpod_job_id or len(runpod_job_id) > 128:
        raise ValueError("Runpod job ID is invalid")
    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    if attempt.submission_nonce != submission_nonce:
        raise ValueError("submission nonce does not match the pending variation")
    if attempt.runpod_job_id and attempt.runpod_job_id != runpod_job_id:
        raise ValueError("variation already has a different Runpod job ID")
    attempt.runpod_job_id = runpod_job_id
    job = get_job(session, attempt.job_id)
    if job is not None:
        job.current_runpod_job_id = runpod_job_id
        job.updated_at = _utc_timestamp(now)
    attempt.updated_at = _utc_timestamp(now)
    session.flush()
    return attempt


def recover_uncertain_variation_submissions(
    session: Session, *, now: datetime | None = None
) -> list[VariationAttempt]:
    """Fail nonce-only variation attempts without resubmitting them."""

    timestamp = _utc_timestamp(now)
    attempts = list(
        session.scalars(
            select(VariationAttempt).where(
                VariationAttempt.submission_nonce.is_not(None),
                VariationAttempt.runpod_job_id.is_(None),
                VariationAttempt.status == JobStatus.CLOUD_QUEUED,
            )
        )
    )
    for attempt in attempts:
        record_attempt_evidence(
            session,
            attempt.id,
            evidence_status="unavailable",
            unavailable_reason="worker_no_evidence",
            now=timestamp,
        )
        transition_variation_attempt(
            session,
            attempt.id,
            JobStatus.FAILED,
            now=timestamp,
            error_code="uncertain_cloud_submission",
            user_facing_error=(
                "Cloud submission outcome is uncertain; automatic resubmission was prevented."
            ),
        )
        job = get_job(session, attempt.job_id)
        if job is not None and job.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
            transition_job(
                session,
                job.id,
                JobStatus.FAILED,
                now=timestamp,
                error_code="uncertain_cloud_submission",
                user_facing_error=attempt.user_facing_error,
            )
    session.flush()
    return attempts


def set_variation_runpod_result(
    session: Session,
    attempt_id: int,
    result: dict[str, Any] | None,
    *,
    project_to_output: bool = True,
    now: datetime | None = None,
) -> VariationAttempt:
    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    attempt.runpod_result_json = result
    job = get_job(session, attempt.job_id)
    if job is not None and result is not None:
        merged_job_result = dict(job.runpod_result_json or {})
        merged_job_result.update(result)
        job.runpod_result_json = merged_job_result
        if project_to_output:
            output_records = list(
                session.scalars(
                    select(Output).where(
                        Output.job_id == attempt.job_id,
                        Output.variation_index == attempt.variation_index,
                    )
                )
            )
            for output in output_records:
                project_runpod_result_to_output(output, result)
    attempt.updated_at = _utc_timestamp(now)
    session.flush()
    return attempt


def set_variation_progress(
    session: Session,
    attempt_id: int,
    phase: str,
    *,
    sequence: int | None = None,
    now: datetime | None = None,
) -> VariationAttempt:
    """Persist one bounded monotonic nonterminal progress envelope."""

    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    if attempt.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        raise ValueError("terminal variation progress cannot be changed")
    expected_sequence = _PROGRESS_SEQUENCES.get(phase)
    if expected_sequence is None:
        raise ValueError("variation progress phase is unsupported")
    resolved_sequence = expected_sequence if sequence is None else sequence
    if resolved_sequence != expected_sequence:
        raise ValueError("variation progress sequence does not match its phase")
    current = attempt.runpod_result_json
    if isinstance(current, dict) and current.get("kind") == _PROGRESS_KIND:
        current_sequence = current.get("sequence")
        if isinstance(current_sequence, int) and not isinstance(current_sequence, bool):
            if current_sequence > resolved_sequence:
                return attempt
    timestamp = _utc_timestamp(now)
    attempt.runpod_result_json = {
        "kind": _PROGRESS_KIND,
        "phase": phase,
        "sequence": resolved_sequence,
        "observed_at": timestamp.isoformat(),
    }
    attempt.updated_at = timestamp
    session.flush()
    return attempt


def project_runpod_result_to_output(output: Output, result: dict[str, Any]) -> None:
    """Project only bounded generation truth from a worker result onto an output."""

    result_output = result.get("output")
    if isinstance(result_output, dict):
        seed_metadata = {
            key: result_output[key]
            for key in ("requested_seed", "effective_seed", "seed")
            if key in result_output
        }
        if seed_metadata:
            output.seed_metadata_json = seed_metadata

    generation_metadata = {
        key: result[key]
        for key in (
            "schema_version",
            "profile_id",
            "input",
            "effective",
            "generated_metadata",
            "resolved_parameters",
            "output",
            "worker",
        )
        if key in result
    }
    if generation_metadata:
        output.generation_metadata_json = generation_metadata


def complete_variation_attempt(
    session: Session,
    attempt_id: int,
    *,
    now: datetime | None = None,
    note: str | None = None,
) -> tuple[Job, VariationAttempt, bool]:
    """Complete one variation and report whether it completed the parent job."""

    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    job = get_job(session, attempt.job_id)
    if job is None:
        raise KeyError(f"unknown job: {attempt.job_id}")
    timestamp = _utc_timestamp(now)
    if note:
        result = dict(attempt.runpod_result_json or {})
        result["recovery_note"] = note
        attempt.runpod_result_json = result
    if attempt.status is not JobStatus.COMPLETED:
        transition_variation_attempt(session, attempt.id, JobStatus.COMPLETED, now=timestamp)
    attempt = session.get(VariationAttempt, attempt.id)
    assert attempt is not None
    if attempt.variation_index >= job.variation_count:
        job.current_runpod_job_id = None
        job.current_submission_nonce = None
        transition_job(session, job.id, JobStatus.COMPLETED, now=timestamp)
        return job, attempt, True
    next_index = attempt.variation_index + 1
    create_variation_attempt(session, job_id=job.id, variation_index=next_index)
    if job.status is JobStatus.CLOUD_QUEUED:
        transition_job(session, job.id, JobStatus.GENERATING, now=timestamp)
    job.current_variation = next_index
    job.current_runpod_job_id = None
    job.current_submission_nonce = None
    job.updated_at = timestamp
    session.flush()
    return job, attempt, False


def fail_variation_attempt(
    session: Session,
    attempt_id: int,
    *,
    error_code: str,
    user_facing_error: str,
    now: datetime | None = None,
) -> Job:
    """Persist a variation failure and make its parent terminal."""

    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    timestamp = _utc_timestamp(now)
    if attempt.status is not JobStatus.FAILED:
        transition_variation_attempt(
            session,
            attempt.id,
            JobStatus.FAILED,
            now=timestamp,
            error_code=error_code,
            user_facing_error=user_facing_error,
        )
    job = get_job(session, attempt.job_id)
    if job is None:
        raise KeyError(f"unknown job: {attempt.job_id}")
    if job.status is not JobStatus.FAILED:
        transition_job(
            session,
            job.id,
            JobStatus.FAILED,
            now=timestamp,
            error_code=error_code,
            user_facing_error=user_facing_error,
        )
    session.flush()
    return job


def update_job(session: Session, job_id: str | UUID, **changes: Any) -> Job:
    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    for field_name, value in changes.items():
        if not hasattr(Job, field_name):
            raise ValueError(f"unsupported job field: {field_name}")
        setattr(job, field_name, value)
    job.updated_at = utc_now()
    session.flush()
    return job


def prepare_runpod_submission(
    session: Session,
    job_id: str | UUID,
    *,
    submission_nonce: str | UUID | None = None,
    now: datetime | None = None,
) -> tuple[Job, str]:
    """Durably mark a job as ready to submit before calling Runpod.

    A nonce without a persisted Runpod ID is intentionally recoverable as an
    uncertain cloud submission.  The caller must commit this change before
    making the external request.
    """

    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.current_runpod_job_id:
        raise ValueError("job already has a Runpod job ID")
    if job.current_submission_nonce:
        raise ValueError("submission outcome is uncertain; automatic resubmission is disabled")
    nonce = str(submission_nonce or uuid4())
    if not nonce:
        raise ValueError("submission nonce must not be empty")
    timestamp = _utc_timestamp(now)
    job.current_submission_nonce = nonce
    job.current_runpod_job_id = None
    job.status = JobStatus.CLOUD_QUEUED
    job.updated_at = timestamp
    session.flush()
    return job, nonce


def persist_runpod_job_id(
    session: Session,
    job_id: str | UUID,
    runpod_job_id: str,
    *,
    submission_nonce: str,
    now: datetime | None = None,
) -> Job:
    """Persist the accepted cloud ID immediately after `/run` returns."""

    if not runpod_job_id or len(runpod_job_id) > 128:
        raise ValueError("Runpod job ID is invalid")
    job = get_job(session, job_id)
    if job is None:
        raise KeyError(f"unknown job: {job_id}")
    if job.current_submission_nonce != submission_nonce:
        raise ValueError("submission nonce does not match the pending attempt")
    if job.current_runpod_job_id and job.current_runpod_job_id != runpod_job_id:
        raise ValueError("job already has a different Runpod job ID")
    job.current_runpod_job_id = runpod_job_id
    job.updated_at = _utc_timestamp(now)
    session.flush()
    return job


def recover_uncertain_submissions(session: Session, *, now: datetime | None = None) -> list[Job]:
    """Fail pending nonce-only attempts without submitting them again."""

    timestamp = _utc_timestamp(now)
    jobs = list(
        session.scalars(
            select(Job).where(
                and_(
                    Job.current_submission_nonce.is_not(None),
                    Job.current_runpod_job_id.is_(None),
                    Job.status == JobStatus.CLOUD_QUEUED,
                )
            )
        )
    )
    for job in jobs:
        job.status = JobStatus.FAILED
        job.error_code = "uncertain_cloud_submission"
        job.user_facing_error = (
            "Cloud submission outcome is uncertain; automatic resubmission was prevented."
        )
        job.updated_at = timestamp
    session.flush()
    return jobs


def create_output(
    session: Session,
    *,
    job_id: str | UUID,
    variation_index: int,
    result_index: int,
    relative_path: str,
    mime_type: str,
    byte_size: int,
    sha256: str,
    runpod_job_id: str | None = None,
    seed_metadata_json: dict[str, Any] | None = None,
    generation_metadata_json: dict[str, Any] | None = None,
) -> Output:
    if variation_index < 1 or result_index < 0 or byte_size < 0:
        raise ValueError("output indexes and byte size are invalid")
    output = Output(
        job_id=str(job_id),
        variation_index=variation_index,
        result_index=result_index,
        runpod_job_id=runpod_job_id,
        relative_path=normalize_relative_path(relative_path),
        mime_type=mime_type,
        byte_size=byte_size,
        sha256=validate_sha256(sha256),
        seed_metadata_json=seed_metadata_json,
        generation_metadata_json=generation_metadata_json,
    )
    session.add(output)
    session.flush()
    return output


def get_output(session: Session, output_id: int) -> Output | None:
    return session.get(Output, output_id)


def get_output_by_path(
    session: Session, *, job_id: str | UUID, relative_path: str
) -> Output | None:
    """Find the durable output associated with one deterministic path."""

    return session.scalar(
        select(Output).where(
            Output.job_id == str(job_id),
            Output.relative_path == normalize_relative_path(relative_path),
        )
    )


def issue_transfer_capability(
    session: Session,
    *,
    job_id: str | UUID,
    direction: TransferDirection,
    expected_relative_path: str,
    expected_extension: str,
    max_bytes: int,
    expires_at: datetime,
    token: str | None = None,
    capability_id: str | UUID | None = None,
) -> IssuedTransfer:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    plaintext_token = token or secrets.token_urlsafe(32)
    capability = TransferCapability(
        id=_id_string(capability_id),
        job_id=str(job_id),
        direction=direction,
        status=TransferStatus.ISSUED,
        token_sha256=_token_sha256(plaintext_token),
        expected_relative_path=normalize_relative_path(expected_relative_path),
        expected_extension=normalize_extension(expected_extension),
        max_bytes=max_bytes,
        expires_at=expires_at,
    )
    session.add(capability)
    session.flush()
    return IssuedTransfer(capability=capability, token=plaintext_token)


def get_transfer_by_token(session: Session, token: str) -> TransferCapability | None:
    token_hash = _token_sha256(token)
    return session.scalar(
        select(TransferCapability).where(TransferCapability.token_sha256 == token_hash)
    )


def get_active_transfer(
    session: Session, token: str, now: datetime | None = None
) -> TransferCapability | None:
    capability = get_transfer_by_token(session, token)
    if capability is None:
        return None
    current_time = now or utc_now()
    if capability.status is TransferStatus.ISSUED and capability.expires_at <= current_time:
        capability.status = TransferStatus.EXPIRED
        session.flush()
        return None
    if capability.status is not TransferStatus.ISSUED:
        return None
    return capability


def consume_transfer(
    session: Session, capability_id: str | UUID, now: datetime | None = None
) -> TransferCapability:
    capability = session.get(TransferCapability, str(capability_id))
    if capability is None:
        raise KeyError(f"unknown transfer capability: {capability_id}")
    current_time = now or utc_now()
    if capability.status is not TransferStatus.ISSUED:
        raise ValueError(f"transfer capability is {capability.status.value}")
    if capability.expires_at <= current_time:
        capability.status = TransferStatus.EXPIRED
        session.flush()
        raise ValueError("transfer capability has expired")
    capability.status = TransferStatus.CONSUMED
    capability.consumed_at = current_time
    session.flush()
    return capability


def revoke_transfer(
    session: Session, capability_id: str | UUID, now: datetime | None = None
) -> TransferCapability:
    capability = session.get(TransferCapability, str(capability_id))
    if capability is None:
        raise KeyError(f"unknown transfer capability: {capability_id}")
    capability.status = TransferStatus.REVOKED
    capability.revoked_at = now or utc_now()
    session.flush()
    return capability


def revoke_active_transfers(
    session: Session, job_id: str | UUID, *, now: datetime | None = None
) -> list[TransferCapability]:
    """Revoke every still-usable capability for one terminal job."""

    timestamp = _utc_timestamp(now)
    capabilities = list(
        session.scalars(
            select(TransferCapability).where(
                TransferCapability.job_id == str(job_id),
                TransferCapability.status == TransferStatus.ISSUED,
            )
        )
    )
    for capability in capabilities:
        capability.status = TransferStatus.REVOKED
        capability.revoked_at = timestamp
    session.flush()
    return capabilities


def _utc_timestamp(value: datetime | None) -> datetime:
    timestamp = value or utc_now()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return timestamp.astimezone(UTC)


class EvidenceConflictError(ValueError):
    """Raised when immutable attempt evidence conflicts with a stored record.

    The rejection happens before any field or timestamp mutation, so the
    stored evidence and its ``updated_at`` remain exactly as they were.
    """


@overload
def _bounded_string(
    value: Any, field_name: str, *, max_length: int, allow_none: Literal[False]
) -> str: ...


@overload
def _bounded_string(
    value: Any, field_name: str, *, max_length: int, allow_none: Literal[True]
) -> str | None: ...


def _bounded_string(
    value: Any, field_name: str, *, max_length: int, allow_none: bool
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field_name} must be bounded non-empty text")
    return value.strip()


@overload
def _non_negative_int(value: Any, field_name: str, *, allow_none: Literal[False]) -> int: ...


@overload
def _non_negative_int(value: Any, field_name: str, *, allow_none: Literal[True]) -> int | None: ...


def _non_negative_int(value: Any, field_name: str, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return int(value)


def get_submission_quote(session: Session, job_id: str | UUID) -> SubmissionQuote | None:
    return cast(
        SubmissionQuote | None,
        session.scalar(select(SubmissionQuote).where(SubmissionQuote.job_id == str(job_id))),
    )


def _quote_persisted_values(quote: SubmissionQuote) -> tuple[Any, ...]:
    return (
        quote.cost_fingerprint,
        quote.model_identity,
        quote.profile_id,
        quote.duration_mode,
        quote.duration_value_seconds,
        quote.variation_count,
        quote.eligible_gpu_ids,
        quote.highest_trusted_hourly_rate_micro_usd,
        quote.highest_trusted_hourly_rate_usd,
        quote.calibration_version,
        quote.predicted_execution_range_ms,
        quote.quoted_amount_micro_usd,
        quote.quoted_range_low_micro_usd,
        quote.quoted_range_high_micro_usd,
        quote.currency,
        quote.rate_source,
        quote.rate_version,
        quote.unavailable_reason_code,
    )


def create_submission_quote(
    session: Session,
    job_id: str | UUID,
    estimate: QuoteEstimate,
    *,
    captured_at: datetime | None = None,
) -> SubmissionQuote:
    """Persist exactly one immutable submission quote for one job.

    An identical repeat returns the stored record unchanged (idempotent); a
    conflicting repeat raises before any mutation.  ``captured_at`` is the
    server capture time and is not part of the idempotence comparison.
    """

    if not isinstance(estimate, QuoteEstimate):
        raise ValueError("estimate must be a QuoteEstimate")
    if not is_cost_fingerprint(estimate.cost_fingerprint):
        raise ValueError("cost fingerprint must be a canonical sha256 hex digest")
    model_identity = _bounded_string(
        estimate.model_identity, "model_identity", max_length=256, allow_none=False
    )
    if not isinstance(estimate.eligible_gpu_ids, list):
        raise ValueError("eligible_gpu_ids must be a list")
    if not all(isinstance(gpu_id, str) and gpu_id.strip() for gpu_id in estimate.eligible_gpu_ids):
        raise ValueError("eligible_gpu_ids must contain non-empty strings")
    if (
        isinstance(estimate.variation_count, bool)
        or not isinstance(estimate.variation_count, int)
        or not 1 <= estimate.variation_count <= 4
    ):
        raise ValueError("variation_count must be between 1 and 4")
    if estimate.currency != "USD":
        raise ValueError("quote currency must be USD")
    if estimate.duration_mode is not None and (
        not isinstance(estimate.duration_mode, str) or not estimate.duration_mode.strip()
    ):
        raise ValueError("duration_mode must be bounded text")
    if estimate.duration_value_seconds is not None and (
        isinstance(estimate.duration_value_seconds, bool)
        or not isinstance(estimate.duration_value_seconds, (int, float))
        or estimate.duration_value_seconds < 0
    ):
        raise ValueError("duration_value_seconds must be a non-negative number")
    if estimate.calibration_version is not None and (
        isinstance(estimate.calibration_version, bool)
        or not isinstance(estimate.calibration_version, int)
        or estimate.calibration_version < 1
    ):
        raise ValueError("calibration_version must be a positive integer")
    if estimate.profile_id is not None:
        _bounded_string(estimate.profile_id, "profile_id", max_length=128, allow_none=False)
    if estimate.rate_source is not None:
        _bounded_string(estimate.rate_source, "rate_source", max_length=256, allow_none=False)
    if estimate.rate_version is not None:
        _bounded_string(estimate.rate_version, "rate_version", max_length=64, allow_none=False)
    if estimate.unavailable_reason_code is not None:
        if estimate.unavailable_reason_code not in QUOTE_UNAVAILABLE_REASONS:
            raise ValueError(
                f"unavailable_reason_code must be one of {sorted(QUOTE_UNAVAILABLE_REASONS)}"
            )
    available = estimate.unavailable_reason_code is None
    if available != (estimate.quoted_amount_micro_usd is not None):
        raise ValueError("quote availability must pair with a quoted amount")
    if available != (estimate.highest_trusted_hourly_rate_micro_usd is not None):
        raise ValueError("quote availability must pair with a trusted rate")
    if available != (estimate.highest_trusted_hourly_rate_usd is not None):
        raise ValueError("quote availability must pair with an exact trusted rate")
    if available and estimate.rate_source is None:
        raise ValueError("an available quote requires a rate source")
    if available and estimate.predicted_execution_range_ms is None:
        raise ValueError("an available quote requires a predicted execution range")
    amount = _non_negative_int(
        estimate.quoted_amount_micro_usd, "quoted_amount_micro_usd", allow_none=True
    )
    rate = _non_negative_int(
        estimate.highest_trusted_hourly_rate_micro_usd,
        "highest_trusted_hourly_rate_micro_usd",
        allow_none=True,
    )
    rate_usd: str | None = None
    if estimate.highest_trusted_hourly_rate_usd is not None:
        rate_usd, derived_rate = parse_micro_usd_decimal(
            estimate.highest_trusted_hourly_rate_usd,
            field_name="highest_trusted_hourly_rate_usd",
        )
        if derived_rate != rate:
            raise ValueError("exact trusted rate does not match derived micro-USD rate")
    low = _non_negative_int(
        estimate.quoted_range_low_micro_usd, "quoted_range_low", allow_none=True
    )
    high = _non_negative_int(
        estimate.quoted_range_high_micro_usd, "quoted_range_high", allow_none=True
    )
    if available:
        if low is None or high is None or low > high:
            raise ValueError("an available quote requires an ordered quoted range")
        predicted = estimate.predicted_execution_range_ms
        if (
            not isinstance(predicted, (list, tuple))
            or len(predicted) != 2
            or isinstance(predicted[0], bool)
            or not isinstance(predicted[0], int)
            or isinstance(predicted[1], bool)
            or not isinstance(predicted[1], int)
            or predicted[0] < 0
            or predicted[0] > predicted[1]
        ):
            raise ValueError("predicted_execution_range_ms must be an ordered non-negative pair")
    elif estimate.predicted_execution_range_ms is not None:
        raise ValueError("an unavailable quote must not carry a predicted range")
    captured = _utc_timestamp(captured_at)

    existing = get_submission_quote(session, str(job_id))
    if existing is not None:
        if _quote_persisted_values(existing) == (
            estimate.cost_fingerprint,
            model_identity,
            estimate.profile_id,
            estimate.duration_mode,
            estimate.duration_value_seconds,
            estimate.variation_count,
            list(estimate.eligible_gpu_ids),
            rate,
            rate_usd,
            estimate.calibration_version,
            (
                list(estimate.predicted_execution_range_ms)
                if estimate.predicted_execution_range_ms is not None
                else None
            ),
            amount,
            low,
            high,
            estimate.currency,
            estimate.rate_source,
            estimate.rate_version,
            estimate.unavailable_reason_code,
        ):
            return existing
        raise EvidenceConflictError("conflicting submission quote for the same job")

    quote = SubmissionQuote(
        job_id=str(job_id),
        cost_fingerprint=estimate.cost_fingerprint,
        model_identity=model_identity,
        profile_id=estimate.profile_id,
        duration_mode=estimate.duration_mode,
        duration_value_seconds=estimate.duration_value_seconds,
        variation_count=estimate.variation_count,
        eligible_gpu_ids=list(estimate.eligible_gpu_ids),
        highest_trusted_hourly_rate_micro_usd=rate,
        highest_trusted_hourly_rate_usd=rate_usd,
        calibration_version=estimate.calibration_version,
        predicted_execution_range_ms=(
            list(estimate.predicted_execution_range_ms)
            if estimate.predicted_execution_range_ms is not None
            else None
        ),
        quoted_amount_micro_usd=amount,
        quoted_range_low_micro_usd=low,
        quoted_range_high_micro_usd=high,
        currency=estimate.currency,
        rate_source=estimate.rate_source,
        rate_version=estimate.rate_version,
        unavailable_reason_code=estimate.unavailable_reason_code,
        captured_at=captured,
    )
    session.add(quote)
    session.flush()
    return quote


def record_attempt_evidence(
    session: Session,
    attempt_id: int,
    *,
    evidence_status: str,
    actual_gpu: str | None = None,
    model_identity: str | None = None,
    runtime_image_identity: str | None = None,
    execution_ms: int | None = None,
    hourly_rate_usd: str | None = None,
    hourly_rate_micro_usd: int | None = None,
    rate_currency: str | None = None,
    rate_source: str | None = None,
    rate_captured_at: datetime | None = None,
    estimated_compute_micro_usd: int | None = None,
    unavailable_reason: str | None = None,
    now: datetime | None = None,
) -> VariationAttempt:
    """Record immutable execution-cost evidence for one variation attempt.

    Transitions: ``pending`` → ``unavailable`` or ``complete``;
    ``unavailable`` → ``complete`` only when newly received authoritative
    evidence fills the missing inputs while every stored field stays
    identical.  ``complete`` never changes.  Conflicting terminal/timing/GPU/
    rate evidence raises :class:`EvidenceConflictError` before any field or
    timestamp mutation; exact repeats are idempotent no-ops.
    """

    if evidence_status not in EVIDENCE_STATUSES:
        raise ValueError(f"evidence_status must be one of {sorted(EVIDENCE_STATUSES)}")
    attempt = session.get(VariationAttempt, attempt_id)
    if attempt is None:
        raise KeyError(f"unknown variation attempt: {attempt_id}")
    gpu = _bounded_string(actual_gpu, "actual_gpu", max_length=128, allow_none=True)
    model = _bounded_string(model_identity, "model_identity", max_length=256, allow_none=True)
    runtime_image = _bounded_string(
        runtime_image_identity, "runtime_image_identity", max_length=256, allow_none=True
    )
    execution = _non_negative_int(execution_ms, "execution_ms", allow_none=True)
    rate = _non_negative_int(hourly_rate_micro_usd, "hourly_rate_micro_usd", allow_none=True)
    rate_usd: str | None = None
    if hourly_rate_usd is not None:
        rate_usd, derived_rate = parse_micro_usd_decimal(
            hourly_rate_usd, field_name="hourly_rate_usd"
        )
        if rate is not None and derived_rate != rate:
            raise ValueError("hourly_rate_usd does not match hourly_rate_micro_usd")
    elif rate is not None:
        # Compatibility for internal callers that only held the already exact
        # integer token before CP4 v02. New provider/catalog paths always pass
        # the original validated decimal text.
        rate_usd = format(Decimal(rate) / Decimal(1_000_000), "f")
    source = _bounded_string(rate_source, "rate_source", max_length=256, allow_none=True)
    estimate = _non_negative_int(
        estimated_compute_micro_usd, "estimated_compute_micro_usd", allow_none=True
    )
    if rate_currency is not None and rate_currency != "USD":
        raise ValueError("rate_currency must be USD")
    if rate_captured_at is not None:
        rate_captured_at = _utc_timestamp(rate_captured_at)
    reason: str | None = None
    if unavailable_reason is not None:
        if unavailable_reason not in ATTEMPT_UNAVAILABLE_REASONS:
            raise ValueError(
                f"unavailable_reason must be one of {sorted(ATTEMPT_UNAVAILABLE_REASONS)}"
            )
        reason = unavailable_reason

    supplied = (
        gpu,
        model,
        runtime_image,
        execution,
        rate_usd,
        rate,
        rate_currency,
        source,
        rate_captured_at,
        estimate,
        reason,
    )
    if evidence_status == "pending":
        if any(value is not None for value in supplied):
            raise ValueError("pending evidence must not carry cost fields")
        if attempt.evidence_status != "pending":
            raise EvidenceConflictError("cannot regress terminal attempt evidence back to pending")
        return attempt

    if evidence_status == "complete":
        missing = [
            name
            for name, value in (
                ("actual_gpu", gpu),
                ("execution_ms", execution),
                ("hourly_rate_usd", rate_usd),
                ("hourly_rate_micro_usd", rate),
                ("rate_source", source),
                ("rate_captured_at", rate_captured_at),
                ("estimated_compute_micro_usd", estimate),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"complete evidence requires: {', '.join(missing)}")
        if rate_currency != "USD":
            raise ValueError("complete evidence requires rate_currency='USD'")
        assert execution is not None and rate is not None
        assert rate_usd is not None
        if estimate != round_half_up_compute_cost_usd(execution, rate_usd):
            raise ValueError(
                "estimated_compute_micro_usd must equal the centralized half-up formula"
            )
    elif evidence_status == "unavailable":
        if reason is None:
            raise ValueError("unavailable evidence requires an unavailable reason")
        if estimate is not None:
            raise ValueError("unavailable evidence must not carry an estimate")
    else:
        if reason is not None:
            raise ValueError("complete evidence must not carry an unavailable reason")
    timestamp = _utc_timestamp(now)

    existing_status = attempt.evidence_status
    if existing_status == evidence_status:
        # Exact-repeat idempotence: every field must match, including None.
        stored = (
            attempt.actual_gpu,
            attempt.model_identity,
            attempt.runtime_image_identity,
            attempt.execution_ms,
            attempt.hourly_rate_usd,
            attempt.hourly_rate_micro_usd,
            attempt.rate_currency,
            attempt.rate_source,
            attempt.rate_captured_at,
            attempt.estimated_compute_micro_usd,
            attempt.unavailable_reason,
        )
        if stored != supplied:
            raise EvidenceConflictError(
                "conflicting attempt evidence; stored immutable evidence is unchanged"
            )
        return attempt
    if existing_status == "complete":
        raise EvidenceConflictError(
            "complete attempt evidence is immutable; conflicting evidence is rejected"
        )
    if existing_status == "unavailable" and evidence_status != "complete":
        raise EvidenceConflictError("unavailable attempt evidence may only advance to complete")
    # unavailable -> complete: stored fields must stay identical while the
    # previously missing inputs are filled by the new authoritative evidence.
    # The stored unavailable reason is historical context for the missing
    # input and is cleared by completion, so it is not compared here.
    if existing_status == "unavailable":
        stored_partial = (
            attempt.actual_gpu,
            attempt.model_identity,
            attempt.runtime_image_identity,
            attempt.execution_ms,
            attempt.hourly_rate_usd,
            attempt.hourly_rate_micro_usd,
            attempt.rate_currency,
            attempt.rate_source,
            attempt.rate_captured_at,
        )
        new_partial = (
            gpu,
            model,
            runtime_image,
            execution,
            rate_usd,
            rate,
            rate_currency,
            source,
            rate_captured_at,
        )
        for stored_value, new_value in zip(stored_partial, new_partial, strict=True):
            if stored_value is not None and stored_value != new_value:
                raise EvidenceConflictError(
                    "conflicting attempt evidence; stored immutable evidence is unchanged"
                )

    attempt.actual_gpu = gpu
    attempt.model_identity = model
    attempt.runtime_image_identity = runtime_image
    attempt.execution_ms = execution
    attempt.hourly_rate_usd = rate_usd
    attempt.hourly_rate_micro_usd = rate
    attempt.rate_currency = rate_currency
    attempt.rate_source = source
    attempt.rate_captured_at = rate_captured_at
    attempt.estimated_compute_micro_usd = estimate
    attempt.evidence_status = evidence_status
    attempt.unavailable_reason = reason
    attempt.updated_at = timestamp
    session.flush()
    return attempt


def record_billing_observation(
    session: Session,
    *,
    provider: str,
    resource_type: str,
    grouping_key: str,
    bucket_start: datetime,
    bucket_size_hours: int,
    raw_amount: str,
    raw_time_billed: str | None,
    currency: str,
    fetched_at: datetime,
    response_size_bytes: int | None,
    is_network_volume: bool,
    source_contract: str,
    documented_fields: dict[str, str] | None = None,
) -> BillingObservation:
    """Append one immutable provider bucket observation and upsert the projection.

    An exact repeat is relative to the current projection: it never appends
    history, a newer repeat advances only projection freshness, and an older or
    equal repeat is a no-op. Changed evidence always appends once per fetch
    event, including A -> B -> A, but only a fetch at least as fresh as the
    current projection may replace its projected values.
    """

    provider_value = _bounded_string(provider, "provider", max_length=32, allow_none=False)
    resource_type_value = _bounded_string(
        resource_type, "resource_type", max_length=32, allow_none=False
    )
    grouping_key_value = _bounded_string(
        grouping_key, "grouping_key", max_length=256, allow_none=False
    )
    if bucket_size_hours not in {1, 24}:
        raise ValueError("bucket_size_hours must be 1 or 24")
    if currency != "USD":
        raise ValueError("billing currency must be USD")
    bucket_start = _utc_timestamp(bucket_start)
    fetched_at = _utc_timestamp(fetched_at)
    if not isinstance(raw_amount, str) or not raw_amount:
        raise ValueError("raw_amount must be non-empty decimal text")
    if raw_time_billed is not None and (
        not isinstance(raw_time_billed, str) or not raw_time_billed
    ):
        raise ValueError("raw_time_billed must be non-empty decimal text")
    if response_size_bytes is not None and (
        isinstance(response_size_bytes, bool)
        or not isinstance(response_size_bytes, int)
        or response_size_bytes < 0
    ):
        raise ValueError("response_size_bytes must be a non-negative integer")
    source_contract_value = _bounded_string(
        source_contract, "source_contract", max_length=128, allow_none=False
    )
    evidence_fields: dict[str, str] = {}
    if documented_fields is not None:
        if not isinstance(documented_fields, dict) or len(documented_fields) > 8:
            raise ValueError("documented_fields must be a bounded object")
        for key, value in documented_fields.items():
            bounded_key = _bounded_string(
                key, "documented field name", max_length=64, allow_none=False
            )
            bounded_value = _bounded_string(
                value, "documented field value", max_length=256, allow_none=False
            )
            evidence_fields[bounded_key] = bounded_value
    evidence_checksum = observation_checksum(
        provider=provider_value,
        resource_type=resource_type_value,
        grouping_key=grouping_key_value,
        bucket_start=bucket_start,
        bucket_size_hours=bucket_size_hours,
        raw_amount=raw_amount,
        raw_time_billed=raw_time_billed,
        currency=currency,
        is_network_volume=is_network_volume,
        source_contract=source_contract_value,
        documented_fields=evidence_fields,
    )
    projection = session.scalar(
        select(BillingProjection).where(
            BillingProjection.provider == provider_value,
            BillingProjection.resource_type == resource_type_value,
            BillingProjection.grouping_key == grouping_key_value,
            BillingProjection.bucket_start == bucket_start,
            BillingProjection.bucket_size_hours == bucket_size_hours,
            BillingProjection.currency == currency,
        )
    )

    def matching_observation() -> BillingObservation | None:
        return session.scalar(
            select(BillingObservation)
            .where(
                BillingObservation.provider == provider_value,
                BillingObservation.resource_type == resource_type_value,
                BillingObservation.grouping_key == grouping_key_value,
                BillingObservation.bucket_start == bucket_start,
                BillingObservation.bucket_size_hours == bucket_size_hours,
                BillingObservation.currency == currency,
                or_(
                    BillingObservation.evidence_checksum == evidence_checksum,
                    and_(
                        BillingObservation.evidence_checksum.is_(None),
                        BillingObservation.checksum == evidence_checksum,
                    ),
                ),
            )
            .order_by(BillingObservation.fetched_at.desc(), BillingObservation.id.desc())
            .limit(1)
        )

    def matching_legacy_event() -> BillingObservation | None:
        legacy_rows = session.scalars(
            select(BillingObservation).where(
                BillingObservation.provider == provider_value,
                BillingObservation.resource_type == resource_type_value,
                BillingObservation.grouping_key == grouping_key_value,
                BillingObservation.bucket_start == bucket_start,
                BillingObservation.bucket_size_hours == bucket_size_hours,
                BillingObservation.currency == currency,
                BillingObservation.raw_amount == raw_amount,
                BillingObservation.raw_time_billed == raw_time_billed,
                BillingObservation.fetched_at == fetched_at,
                BillingObservation.is_network_volume == (1 if is_network_volume else 0),
                BillingObservation.source_contract == source_contract_value,
                BillingObservation.evidence_checksum.is_(None),
                BillingObservation.checksum == evidence_checksum,
            )
        )
        for legacy_row in legacy_rows:
            if (legacy_row.documented_fields_json or {}) == evidence_fields:
                return legacy_row
        return None

    if projection is not None and projection.latest_evidence_checksum is None:
        projected_rows = session.scalars(
            select(BillingObservation)
            .where(
                BillingObservation.provider == provider_value,
                BillingObservation.resource_type == resource_type_value,
                BillingObservation.grouping_key == grouping_key_value,
                BillingObservation.bucket_start == bucket_start,
                BillingObservation.bucket_size_hours == bucket_size_hours,
                BillingObservation.currency == currency,
                BillingObservation.raw_amount == projection.latest_amount,
                BillingObservation.raw_time_billed == projection.latest_time_billed,
            )
            .order_by(BillingObservation.fetched_at.desc(), BillingObservation.id.desc())
        )
        for projected_row in projected_rows:
            if (projected_row.documented_fields_json or {}) == (
                projection.latest_documented_fields_json or {}
            ):
                projection.latest_evidence_checksum = (
                    projected_row.evidence_checksum or projected_row.checksum
                )
                break
        if projection.latest_evidence_checksum is None:
            raise EvidenceConflictError("billing projection has no matching immutable evidence")

    checksum = observation_event_checksum(
        evidence_checksum=evidence_checksum,
        fetched_at=fetched_at,
    )
    existing = session.scalar(
        select(BillingObservation).where(BillingObservation.checksum == checksum)
    )
    if existing is not None:
        return existing
    legacy_event = matching_legacy_event()
    if legacy_event is not None:
        return legacy_event

    if projection is not None and projection.latest_evidence_checksum == evidence_checksum:
        existing = matching_observation()
        if existing is None:
            raise EvidenceConflictError("billing projection has no matching immutable evidence")
        if fetched_at > projection.last_updated_at:
            projection.last_updated_at = fetched_at
            session.flush()
        return existing

    observation = BillingObservation(
        provider=provider_value,
        resource_type=resource_type_value,
        grouping_key=grouping_key_value,
        bucket_start=bucket_start,
        bucket_size_hours=bucket_size_hours,
        raw_amount=raw_amount,
        raw_time_billed=raw_time_billed,
        currency=currency,
        fetched_at=fetched_at,
        response_size_bytes=response_size_bytes,
        is_network_volume=1 if is_network_volume else 0,
        source_contract=source_contract_value,
        documented_fields_json=evidence_fields or None,
        evidence_checksum=evidence_checksum,
        checksum=checksum,
    )
    session.add(observation)
    if projection is None:
        projection = BillingProjection(
            provider=provider_value,
            resource_type=resource_type_value,
            grouping_key=grouping_key_value,
            bucket_start=bucket_start,
            bucket_size_hours=bucket_size_hours,
            latest_amount=raw_amount,
            latest_time_billed=raw_time_billed,
            currency=currency,
            last_updated_at=fetched_at,
            latest_documented_fields_json=evidence_fields or None,
            latest_evidence_checksum=evidence_checksum,
        )
        session.add(projection)
    elif fetched_at >= projection.last_updated_at:
        projection.latest_amount = raw_amount
        projection.latest_time_billed = raw_time_billed
        projection.last_updated_at = fetched_at
        projection.latest_documented_fields_json = evidence_fields or None
        projection.latest_evidence_checksum = evidence_checksum
    session.flush()
    return observation


def upsert_gpu_rate(
    session: Session,
    *,
    gpu_id: str,
    rate_micro_usd_per_hour: int,
    hourly_rate_usd: str,
    source: str,
    calibration_version: int,
    captured_at: datetime | None = None,
    provider: str = "runpod",
    currency: str = "USD",
    price_max_age_hours: int = 24,
) -> GpuRateCatalog:
    """Upsert one versioned, server-owned GPU rate catalog entry."""

    gpu_id_value = _bounded_string(gpu_id, "gpu_id", max_length=64, allow_none=False)
    provider_value = _bounded_string(provider, "provider", max_length=32, allow_none=False)
    source_value = _bounded_string(source, "source", max_length=128, allow_none=False)
    rate_value = _non_negative_int(
        rate_micro_usd_per_hour, "rate_micro_usd_per_hour", allow_none=False
    )
    rate_text, derived_rate = parse_micro_usd_decimal(hourly_rate_usd, field_name="hourly_rate_usd")
    if derived_rate != rate_value:
        raise ValueError("hourly_rate_usd does not match rate_micro_usd_per_hour")
    if currency != "USD":
        raise ValueError("rate currency must be USD")
    if isinstance(calibration_version, bool) or not isinstance(calibration_version, int):
        raise ValueError("calibration_version must be an integer")
    if calibration_version < 1:
        raise ValueError("calibration_version must be at least 1")
    if isinstance(price_max_age_hours, bool) or not isinstance(price_max_age_hours, int):
        raise ValueError("price_max_age_hours must be an integer")
    if price_max_age_hours < 1:
        raise ValueError("price_max_age_hours must be at least 1")
    captured = _utc_timestamp(captured_at)
    expires = captured + timedelta(hours=price_max_age_hours)
    existing = session.scalar(
        select(GpuRateCatalog).where(
            GpuRateCatalog.gpu_id == gpu_id_value,
            GpuRateCatalog.provider == provider_value,
            GpuRateCatalog.calibration_version == calibration_version,
        )
    )
    if existing is not None:
        expected = (rate_value, rate_text, currency, source_value, captured, expires)
        stored = (
            existing.rate_micro_usd_per_hour,
            existing.hourly_rate_usd,
            existing.currency,
            existing.source,
            existing.captured_at,
            existing.expires_at,
        )
        if stored == expected:
            return existing
        raise EvidenceConflictError("conflicting reuse of immutable GPU rate version")
    row = GpuRateCatalog(
        gpu_id=gpu_id_value,
        provider=provider_value,
        rate_micro_usd_per_hour=rate_value,
        hourly_rate_usd=rate_text,
        currency=currency,
        source=source_value,
        calibration_version=calibration_version,
        captured_at=captured,
        expires_at=expires,
    )
    session.add(row)
    session.flush()
    return row


def upsert_runtime_calibration(
    session: Session,
    *,
    version: int,
    task_mode: str,
    profile_id: str,
    model_identity: str,
    runtime_identity: str,
    gpu_class: str,
    duration_mode: str,
    duration_band_min_seconds: float,
    duration_band_max_seconds: float,
    output_count: int,
    execution_low_ms: int,
    execution_high_ms: int,
    evidence_source: str,
    conservative_margin: str,
    captured_at: datetime,
) -> RuntimeCalibration:
    """Persist one immutable measured calibration; exact repeats are no-ops."""

    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("calibration version must be a positive integer")
    strings = {
        "task_mode": (task_mode, 32),
        "profile_id": (profile_id, 128),
        "model_identity": (model_identity, 256),
        "runtime_identity": (runtime_identity, 256),
        "gpu_class": (gpu_class, 64),
        "duration_mode": (duration_mode, 32),
        "evidence_source": (evidence_source, 256),
    }
    bounded = {
        key: _bounded_string(value, key, max_length=limit, allow_none=False)
        for key, (value, limit) in strings.items()
    }
    if bounded["evidence_source"] not in RUNTIME_CALIBRATION_EVIDENCE_SOURCES:
        raise ValueError("runtime calibration evidence_source is not accepted")
    for name, value in (
        ("duration_band_min_seconds", duration_band_min_seconds),
        ("duration_band_max_seconds", duration_band_max_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{name} must be a non-negative number")
    if duration_band_max_seconds < duration_band_min_seconds:
        raise ValueError("duration calibration band must be ordered")
    if (
        isinstance(output_count, bool)
        or not isinstance(output_count, int)
        or not 1 <= output_count <= 4
    ):
        raise ValueError("output_count must be between 1 and 4")
    low = _non_negative_int(execution_low_ms, "execution_low_ms", allow_none=False)
    high = _non_negative_int(execution_high_ms, "execution_high_ms", allow_none=False)
    if low > high:
        raise ValueError("execution range must be ordered")
    margin, _ = parse_micro_usd_decimal(conservative_margin, field_name="conservative_margin")
    if Decimal(margin) > Decimal("1"):
        raise ValueError("conservative_margin must not exceed 1.0")
    captured = _utc_timestamp(captured_at)
    values = (
        version,
        bounded["task_mode"],
        bounded["profile_id"],
        bounded["model_identity"],
        bounded["runtime_identity"],
        bounded["gpu_class"],
        bounded["duration_mode"],
        float(duration_band_min_seconds),
        float(duration_band_max_seconds),
        output_count,
        low,
        high,
        bounded["evidence_source"],
        margin,
        captured,
    )
    existing = session.scalar(
        select(RuntimeCalibration).where(RuntimeCalibration.version == version)
    )
    if existing is not None:
        stored = (
            existing.version,
            existing.task_mode,
            existing.profile_id,
            existing.model_identity,
            existing.runtime_identity,
            existing.gpu_class,
            existing.duration_mode,
            existing.duration_band_min_seconds,
            existing.duration_band_max_seconds,
            existing.output_count,
            existing.execution_low_ms,
            existing.execution_high_ms,
            existing.evidence_source,
            existing.conservative_margin,
            existing.captured_at,
        )
        if stored == values:
            return existing
        raise EvidenceConflictError("conflicting reuse of immutable runtime calibration version")
    row = RuntimeCalibration(
        version=version,
        task_mode=bounded["task_mode"],
        profile_id=bounded["profile_id"],
        model_identity=bounded["model_identity"],
        runtime_identity=bounded["runtime_identity"],
        gpu_class=bounded["gpu_class"],
        duration_mode=bounded["duration_mode"],
        duration_band_min_seconds=float(duration_band_min_seconds),
        duration_band_max_seconds=float(duration_band_max_seconds),
        output_count=output_count,
        execution_low_ms=low,
        execution_high_ms=high,
        evidence_source=bounded["evidence_source"],
        conservative_margin=margin,
        captured_at=captured,
    )
    session.add(row)
    session.flush()
    return row


def get_matching_runtime_calibration(
    session: Session,
    *,
    task_mode: str,
    profile_id: str,
    model_identity: str,
    runtime_identity: str,
    gpu_class: str,
    duration_mode: str,
    duration_value_seconds: float | None,
    output_count: int,
) -> RuntimeCalibration | None:
    """Return the newest exact-dimension calibration without extrapolation."""

    if duration_value_seconds is None:
        return None
    return cast(
        RuntimeCalibration | None,
        session.scalar(
            select(RuntimeCalibration)
            .where(
                RuntimeCalibration.task_mode == task_mode,
                RuntimeCalibration.profile_id == profile_id,
                RuntimeCalibration.model_identity == model_identity,
                RuntimeCalibration.runtime_identity == runtime_identity,
                RuntimeCalibration.gpu_class == gpu_class,
                RuntimeCalibration.duration_mode == duration_mode,
                RuntimeCalibration.duration_band_min_seconds <= duration_value_seconds,
                RuntimeCalibration.duration_band_max_seconds >= duration_value_seconds,
                RuntimeCalibration.output_count == output_count,
            )
            .order_by(RuntimeCalibration.version.desc())
            .limit(1)
        ),
    )


def get_gpu_rate(
    session: Session,
    gpu_id: str,
    *,
    provider: str = "runpod",
    now: datetime | None = None,
) -> GpuRateCatalog | None:
    """Return the freshest trusted rate for one GPU, or None when stale/missing."""

    current = _utc_timestamp(now)
    return session.scalar(
        select(GpuRateCatalog)
        .where(
            GpuRateCatalog.gpu_id == gpu_id,
            GpuRateCatalog.provider == provider,
            GpuRateCatalog.expires_at > current,
        )
        .order_by(GpuRateCatalog.calibration_version.desc())
        .limit(1)
    )


def get_current_gpu_rates(
    session: Session,
    gpu_ids: list[str],
    *,
    provider: str = "runpod",
    now: datetime | None = None,
) -> dict[str, GpuRateCatalog]:
    """Return only the fresh trusted rates for the requested GPU IDs.

    Unknown or stale eligible GPUs are simply absent; quote/estimate callers
    must treat any missing eligible GPU as a reason to make the amount
    unavailable rather than quoting from the cheaper known subset.
    """

    current = _utc_timestamp(now)
    if not gpu_ids:
        return {}
    rows = session.scalars(
        select(GpuRateCatalog).where(
            GpuRateCatalog.provider == provider,
            GpuRateCatalog.gpu_id.in_(gpu_ids),
            GpuRateCatalog.expires_at > current,
        )
    )
    rates: dict[str, GpuRateCatalog] = {}
    for row in rows:
        prior = rates.get(row.gpu_id)
        if prior is None or row.calibration_version > prior.calibration_version:
            rates[row.gpu_id] = row
    return rates


def get_latest_gpu_rates(
    session: Session,
    gpu_ids: list[str],
    *,
    provider: str = "runpod",
) -> dict[str, GpuRateCatalog]:
    """Return the latest catalog row per GPU regardless of freshness.

    Callers split the result into fresh and stale sets so an unavailable
    quote can distinguish ``rate_unknown`` from ``rate_stale``.
    """

    if not gpu_ids:
        return {}
    rows = session.scalars(
        select(GpuRateCatalog).where(
            GpuRateCatalog.provider == provider,
            GpuRateCatalog.gpu_id.in_(gpu_ids),
        )
    )
    latest: dict[str, GpuRateCatalog] = {}
    for row in rows:
        prior = latest.get(row.gpu_id)
        if prior is None or row.calibration_version > prior.calibration_version:
            latest[row.gpu_id] = row
    return latest


def sum_terminal_attempt_estimates(
    session: Session,
    *,
    interval_start: datetime,
    interval_end: datetime,
) -> CostSummary:
    """Sum terminal attempt estimates by terminal ``completed_at``.

    The interval is half-open UTC ``[start, end)``.  A terminal attempt with
    complete evidence contributes its non-null estimate (including a proven
    zero); a terminal attempt with pending or unavailable evidence (including
    every legacy row) reports partial coverage instead of an invented number.
    """

    start = _utc_timestamp(interval_start)
    end = _utc_timestamp(interval_end)
    if end < start:
        raise ValueError("interval_end must not precede interval_start")
    attempts = session.scalars(
        select(VariationAttempt).where(
            VariationAttempt.status.in_([JobStatus.COMPLETED, JobStatus.FAILED]),
            VariationAttempt.completed_at >= start,
            VariationAttempt.completed_at < end,
        )
    )
    summed = 0
    terminal_attempts = 0
    with_estimate = 0
    without_cost = 0
    for attempt in attempts:
        terminal_attempts += 1
        if (
            attempt.evidence_status == "complete"
            and attempt.estimated_compute_micro_usd is not None
        ):
            summed += attempt.estimated_compute_micro_usd
            with_estimate += 1
        else:
            without_cost += 1
    return CostSummary(
        interval_start=start,
        interval_end=end,
        summed_estimate_micro_usd=summed,
        terminal_attempts=terminal_attempts,
        attempts_with_estimate=with_estimate,
        attempts_without_cost=without_cost,
        partial_coverage=without_cost > 0,
    )


def reconcile_delta(
    session: Session,
    *,
    interval_start: datetime,
    interval_end: datetime,
    actual_endpoint_micro_usd: int | None,
    cutoff_at: datetime | None = None,
    source_contract: str | None = None,
) -> ReconciliationResult:
    """Compute the signed endpoint reconciliation delta for one half-open interval.

    ``delta = actual endpoint amount - summed terminal estimates``; negative
    values are preserved and never clamped or relabeled as startup/idle spend.
    Any terminal attempt without cost evidence makes coverage partial, and a
    missing actual amount makes reconciliation unavailable.
    """

    summary = sum_terminal_attempt_estimates(
        session, interval_start=interval_start, interval_end=interval_end
    )
    if actual_endpoint_micro_usd is None:
        return ReconciliationResult(
            interval_start=summary.interval_start,
            interval_end=summary.interval_end,
            actual_endpoint_micro_usd=None,
            summed_estimates_micro_usd=None,
            delta_micro_usd=None,
            coverage="unavailable",
            cutoff_at=cutoff_at,
            source_contract=source_contract,
        )
    if isinstance(actual_endpoint_micro_usd, bool) or not isinstance(
        actual_endpoint_micro_usd, int
    ):
        raise ValueError("actual_endpoint_micro_usd must be an integer")
    summed = summary.summed_estimate_micro_usd
    coverage = "partial" if summary.partial_coverage else "complete"
    return ReconciliationResult(
        interval_start=summary.interval_start,
        interval_end=summary.interval_end,
        actual_endpoint_micro_usd=actual_endpoint_micro_usd,
        summed_estimates_micro_usd=summed,
        delta_micro_usd=actual_endpoint_micro_usd - summed,
        coverage=coverage,
        cutoff_at=cutoff_at,
        source_contract=source_contract,
    )


def sum_billing_projections(
    session: Session,
    *,
    resource_type: str,
    interval_start: datetime,
    interval_end: datetime,
    provider: str = "runpod",
) -> int:
    """Sum current provider buckets whose native UTC start lies in the interval.

    Native provider buckets are never shifted or prorated; callers must align
    the half-open interval with native bucket boundaries before using the
    total (the boundary probe and reconciliation gates enforce this).
    """

    start = _utc_timestamp(interval_start)
    end = _utc_timestamp(interval_end)
    if end < start:
        raise ValueError("interval_end must not precede interval_start")
    rows = session.scalars(
        select(BillingProjection).where(
            BillingProjection.provider == provider,
            BillingProjection.resource_type == resource_type,
            BillingProjection.bucket_start >= start,
            BillingProjection.bucket_start < end,
        )
    )

    total = 0
    for row in rows:
        _, micro_usd = parse_micro_usd_decimal(row.latest_amount, field_name="latest_amount")
        total += micro_usd
    return total


def sum_network_volume_observations(
    session: Session,
    *,
    interval_start: datetime,
    interval_end: datetime,
) -> NetworkVolumeSummary:
    """Account-wide network-volume evidence for one half-open UTC interval.

    The result is deliberately separate: it is never allocated to jobs and
    never summed into service totals without a future provider-supported
    volume dimension.
    """

    start = _utc_timestamp(interval_start)
    end = _utc_timestamp(interval_end)
    if end < start:
        raise ValueError("interval_end must not precede interval_start")
    rows = session.scalars(
        select(BillingProjection).where(
            BillingProjection.provider == "runpod",
            BillingProjection.resource_type == "network_volume",
            BillingProjection.grouping_key == "account",
            BillingProjection.bucket_start >= start,
            BillingProjection.bucket_start < end,
        )
    )

    total = 0
    count = 0
    for row in rows:
        _, micro_usd = parse_micro_usd_decimal(row.latest_amount, field_name="latest_amount")
        total += micro_usd
        count += 1
    return NetworkVolumeSummary(
        interval_start=start,
        interval_end=end,
        summed_amount_micro_usd=total,
        observation_count=count,
        currency="USD",
    )

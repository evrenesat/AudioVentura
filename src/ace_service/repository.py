"""Small synchronous repository operations for durable domain records."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ace_service.models import (
    Job,
    JobStatus,
    JobType,
    Output,
    OutputFormat,
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
_TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})
_NONTERMINAL_JOB_STATUSES = frozenset(JobStatus) - _TERMINAL_JOB_STATUSES
_COVER_STAGING_STATUSES = frozenset({"awaiting_confirmation", "confirmed"})
_COVER_STAGING_KEYS = frozenset({"status", "staged_at", "confirmed_at"})
_MISSING = object()


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
) -> Job:
    if variation_count < 1 or variation_count > 4:
        raise ValueError("variation_count must be between 1 and 4")
    if cover_strength is not None and not 0 <= cover_strength <= 1:
        raise ValueError("cover_strength must be between 0 and 1")
    job = Job(
        id=_id_string(job_id),
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
    session.flush()
    return job


def create_original_job(
    session: Session,
    request: OriginalSongRequest,
    *,
    job_id: str | UUID | None = None,
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
    )


def create_cover_job(
    session: Session,
    request: CoverRequest,
    *,
    rights_confirmation_at: datetime | None = None,
    job_id: str | UUID | None = None,
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
    job.normalized_request_json = finalize_cover_normalized_request(
        job.normalized_request_json, source_duration_seconds
    )
    duration = float(source_duration_seconds)
    job.source_duration = duration
    result = dict(job.runpod_result_json or {})
    result.update(
        {
            "schema_version": 2,
            "source_duration_seconds": duration,
            "resolved_target_duration_seconds": duration,
            "ace_duration_seconds": duration,
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

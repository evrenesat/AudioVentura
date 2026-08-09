"""SQLAlchemy persistence models for durable controller state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Return an explicitly UTC-aware timestamp."""

    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC and reject ambiguous naive values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC timestamps in SQLite and restore their UTC tzinfo on reads."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return as_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class _ValueEnum(StrEnum):
    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class JobType(_ValueEnum):
    ORIGINAL = "original"
    COVER = "cover"

    original = ORIGINAL
    cover = COVER


class JobStatus(_ValueEnum):
    QUEUED = "queued"
    INGESTING = "ingesting"
    STAGING = "staging"
    CLOUD_QUEUED = "cloud_queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

    queued = QUEUED
    ingesting = INGESTING
    staging = STAGING
    cloud_queued = CLOUD_QUEUED
    generating = GENERATING
    completed = COMPLETED
    failed = FAILED


class OutputFormat(_ValueEnum):
    MP3 = "mp3"
    FLAC = "flac"
    WAV = "wav"

    mp3 = MP3
    flac = FLAC
    wav = WAV


class TransferDirection(_ValueEnum):
    SOURCE_DOWNLOAD = "source_download"
    OUTPUT_UPLOAD = "output_upload"

    source_download = SOURCE_DOWNLOAD
    output_upload = OUTPUT_UPLOAD


class TransferStatus(_ValueEnum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"

    issued = ISSUED
    consumed = CONSUMED
    expired = EXPIRED
    revoked = REVOKED


def _enum_type(enum_type: type[_ValueEnum]) -> SqlEnum:
    return SqlEnum(
        enum_type,
        values_callable=lambda enum_cls: enum_cls.values(),
        native_enum=False,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[JobType] = mapped_column(_enum_type(JobType), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum_type(JobStatus), nullable=False, default=JobStatus.QUEUED
    )
    source_url: Mapped[str | None] = mapped_column(String(2048))
    sanitized_source_title: Mapped[str | None] = mapped_column(String(512))
    source_duration: Mapped[float | None]
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_byte_size: Mapped[int | None]
    prompt: Mapped[str | None] = mapped_column(Text)
    lyrics: Mapped[str | None] = mapped_column(Text)
    rights_confirmation_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cover_strength: Mapped[float | None]
    output_format: Mapped[OutputFormat] = mapped_column(
        _enum_type(OutputFormat), nullable=False, default=OutputFormat.MP3
    )
    variation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_variation: Mapped[int | None] = mapped_column(Integer)
    normalized_request_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    current_runpod_job_id: Mapped[str | None] = mapped_column(String(128))
    current_submission_nonce: Mapped[str | None] = mapped_column(String(128))
    runpod_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    user_facing_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    outputs: Mapped[list[Output]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    variation_attempts: Mapped[list[VariationAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    transfers: Mapped[list[TransferCapability]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class Output(Base):
    __tablename__ = "outputs"
    __table_args__ = (UniqueConstraint("job_id", "variation_index", "result_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    result_index: Mapped[int] = mapped_column(Integer, nullable=False)
    runpod_job_id: Mapped[str | None] = mapped_column(String(128))
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    generation_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    job: Mapped[Job] = relationship(back_populates="outputs")


class VariationAttempt(Base):
    """Durable state for one serialized Runpod variation attempt.

    The immutable execution-cost evidence columns are added by the ordered
    CP4 migration (``evidence_status`` defaults to ``pending`` for legacy
    rows, which render/query as cost-unavailable).  Evidence transitions are
    enforced by ``ace_service.repository.record_attempt_evidence``; the
    columns carry no CHECK constraints because SQLite cannot add CHECKs to an
    existing table without a rebuild, and production migrations stay additive.
    """

    __tablename__ = "variation_attempts"
    __table_args__ = (UniqueConstraint("job_id", "variation_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum_type(JobStatus), nullable=False, default=JobStatus.QUEUED
    )
    runpod_job_id: Mapped[str | None] = mapped_column(String(128))
    submission_nonce: Mapped[str | None] = mapped_column(String(128))
    runpod_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    user_facing_error: Mapped[str | None] = mapped_column(Text)
    actual_gpu: Mapped[str | None] = mapped_column(String(128))
    model_identity: Mapped[str | None] = mapped_column(String(256))
    runtime_image_identity: Mapped[str | None] = mapped_column(String(256))
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    hourly_rate_usd: Mapped[str | None] = mapped_column(String(64))
    hourly_rate_micro_usd: Mapped[int | None] = mapped_column(Integer)
    rate_currency: Mapped[str | None] = mapped_column(String(16), default="USD")
    rate_source: Mapped[str | None] = mapped_column(String(256))
    rate_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    estimated_compute_micro_usd: Mapped[int | None] = mapped_column(Integer)
    evidence_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    unavailable_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="variation_attempts")


_QUOTE_REASON_CODES = (
    "'rate_stale', 'rate_unknown', 'gpu_unknown', 'provider_unreachable', 'calibration_missing'"
)


class SubmissionQuote(Base):
    """Immutable server-owned pre-submission cost quote, one per job.

    Stores only non-sensitive cost drivers; prompts, lyrics, source URLs,
    transfer capabilities, and raw normalized requests never appear here.  An
    unavailable quote stores a null amount plus an allow-listed bounded reason
    code; the DB CHECK keeps an available quote paired with its amount/rate
    and an unavailable quote paired with its reason.
    """

    __tablename__ = "submission_quotes"
    __table_args__ = (
        CheckConstraint(
            "unavailable_reason_code IS NULL OR unavailable_reason_code IN ("
            + _QUOTE_REASON_CODES
            + ")",
            name="ck_submission_quotes_reason_allowlist",
        ),
        CheckConstraint(
            "(unavailable_reason_code IS NULL) = (quoted_amount_micro_usd IS NOT NULL)",
            name="ck_submission_quotes_amount_pairing",
        ),
        CheckConstraint(
            "(unavailable_reason_code IS NULL) = "
            "(highest_trusted_hourly_rate_micro_usd IS NOT NULL)",
            name="ck_submission_quotes_rate_pairing",
        ),
        CheckConstraint(
            "(unavailable_reason_code IS NULL) = (highest_trusted_hourly_rate_usd IS NOT NULL)",
            name="ck_submission_quotes_exact_rate_pairing",
        ),
        CheckConstraint("variation_count BETWEEN 1 AND 4", name="ck_submission_quotes_variations"),
        CheckConstraint("currency = 'USD'", name="ck_submission_quotes_currency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    cost_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(128))
    duration_mode: Mapped[str | None] = mapped_column(String(32))
    duration_value_seconds: Mapped[float | None]
    variation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_gpu_ids: Mapped[list[str] | None] = mapped_column(JSON)
    highest_trusted_hourly_rate_micro_usd: Mapped[int | None] = mapped_column(Integer)
    highest_trusted_hourly_rate_usd: Mapped[str | None] = mapped_column(String(64))
    calibration_version: Mapped[int | None] = mapped_column(Integer)
    predicted_execution_range_ms: Mapped[list[int] | None] = mapped_column(JSON)
    quoted_amount_micro_usd: Mapped[int | None] = mapped_column(Integer)
    quoted_range_low_micro_usd: Mapped[int | None] = mapped_column(Integer)
    quoted_range_high_micro_usd: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    rate_source: Mapped[str | None] = mapped_column(String(256))
    rate_version: Mapped[str | None] = mapped_column(String(64))
    unavailable_reason_code: Mapped[str | None] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    job: Mapped[Job] = relationship()


class BillingObservation(Base):
    """Append-only provider billing bucket evidence.

    Every changed bucket appends an observation with fetch time and raw
    amount/time-billed evidence; the current value lives in the separate
    ``billing_projections`` upsert. ``evidence_checksum`` identifies equal
    values while unique ``checksum`` identifies one changed fetch event, and
    ``is_network_volume`` keeps account-wide volume evidence separate from
    endpoint costs. Nullable evidence checksums preserve additive migration of
    pre-repair rows without rewriting them.
    """

    __tablename__ = "billing_observations"
    __table_args__ = (
        CheckConstraint("bucket_size_hours IN (1, 24)", name="ck_billing_observations_bucket"),
        CheckConstraint("currency = 'USD'", name="ck_billing_observations_currency"),
        CheckConstraint("is_network_volume IN (0, 1)", name="ck_billing_observations_volume"),
        CheckConstraint("length(checksum) = 64", name="ck_billing_observations_checksum"),
        CheckConstraint(
            "evidence_checksum IS NULL OR length(evidence_checksum) = 64",
            name="ck_billing_observations_evidence_checksum",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="runpod")
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    grouping_key: Mapped[str] = mapped_column(String(256), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    bucket_size_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    raw_amount: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_time_billed: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    response_size_bytes: Mapped[int | None] = mapped_column(Integer)
    is_network_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_contract: Mapped[str] = mapped_column(String(128), nullable=False)
    documented_fields_json: Mapped[dict[str, str] | None] = mapped_column(JSON)
    evidence_checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)


class BillingProjection(Base):
    """Idempotent current snapshot of one provider billing bucket."""

    __tablename__ = "billing_projections"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "resource_type",
            "grouping_key",
            "bucket_start",
            "bucket_size_hours",
            "currency",
            name="uq_billing_projections_bucket",
        ),
        CheckConstraint("bucket_size_hours IN (1, 24)", name="ck_billing_projections_bucket"),
        CheckConstraint("currency = 'USD'", name="ck_billing_projections_currency"),
        CheckConstraint(
            "latest_evidence_checksum IS NULL OR length(latest_evidence_checksum) = 64",
            name="ck_billing_projections_evidence_checksum",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    grouping_key: Mapped[str] = mapped_column(String(256), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    bucket_size_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    latest_amount: Mapped[str] = mapped_column(String(64), nullable=False)
    latest_time_billed: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    last_updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    latest_documented_fields_json: Mapped[dict[str, str] | None] = mapped_column(JSON)
    latest_evidence_checksum: Mapped[str | None] = mapped_column(String(64))


class GpuRateCatalog(Base):
    """Versioned server-owned GPU rate catalog with fixed-decimal validation."""

    __tablename__ = "gpu_rate_catalog"
    __table_args__ = (
        UniqueConstraint(
            "gpu_id", "provider", "calibration_version", name="uq_gpu_rate_catalog_version"
        ),
        CheckConstraint("rate_micro_usd_per_hour >= 0", name="ck_gpu_rate_catalog_nonnegative"),
        CheckConstraint("currency = 'USD'", name="ck_gpu_rate_catalog_currency"),
        CheckConstraint("calibration_version >= 1", name="ck_gpu_rate_catalog_version_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gpu_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="runpod")
    rate_micro_usd_per_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    hourly_rate_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    calibration_version: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RuntimeCalibration(Base):
    """Immutable measured runtime range for one exact quote-input shape."""

    __tablename__ = "runtime_calibrations"
    __table_args__ = (
        UniqueConstraint("version", name="uq_runtime_calibrations_version"),
        CheckConstraint("version >= 1", name="ck_runtime_calibrations_version"),
        CheckConstraint("output_count BETWEEN 1 AND 4", name="ck_runtime_calibrations_outputs"),
        CheckConstraint(
            "duration_band_min_seconds >= 0 AND "
            "duration_band_max_seconds >= duration_band_min_seconds",
            name="ck_runtime_calibrations_duration_band",
        ),
        CheckConstraint(
            "execution_low_ms >= 0 AND execution_high_ms >= execution_low_ms",
            name="ck_runtime_calibrations_execution_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    runtime_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    gpu_class: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_band_min_seconds: Mapped[float] = mapped_column(nullable=False)
    duration_band_max_seconds: Mapped[float] = mapped_column(nullable=False)
    output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_low_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_high_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_source: Mapped[str] = mapped_column(String(256), nullable=False)
    conservative_margin: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BillingLease(Base):
    """Database-backed singleton lease for one operator billing sync run."""

    __tablename__ = "billing_lease"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_billing_lease_singleton"),
        CheckConstraint("status IN ('free', 'locked')", name="ck_billing_lease_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TransferCapability(Base):
    __tablename__ = "transfer_capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[TransferDirection] = mapped_column(
        _enum_type(TransferDirection), nullable=False
    )
    status: Mapped[TransferStatus] = mapped_column(
        _enum_type(TransferStatus), nullable=False, default=TransferStatus.ISSUED
    )
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expected_relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    expected_extension: Mapped[str] = mapped_column(String(16), nullable=False)
    max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    job: Mapped[Job] = relationship(back_populates="transfers")


JobOutput = Output


# Allow-listed bounded unavailable reasons for submission quotes and attempt
# evidence.  Repository layer enforces these before persistence.
QUOTE_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {"rate_stale", "rate_unknown", "gpu_unknown", "provider_unreachable", "calibration_missing"}
)
ATTEMPT_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {"rate_stale", "rate_unknown", "worker_no_evidence", "timing_unavailable"}
)
EVIDENCE_STATUSES: frozenset[str] = frozenset({"pending", "unavailable", "complete"})

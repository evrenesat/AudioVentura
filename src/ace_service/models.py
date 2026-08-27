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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates
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
    CANCELLED = "cancelled"

    queued = QUEUED
    ingesting = INGESTING
    staging = STAGING
    cloud_queued = CLOUD_QUEUED
    generating = GENERATING
    completed = COMPLETED
    failed = FAILED
    cancelled = CANCELLED


class OutputFormat(_ValueEnum):
    MP3 = "mp3"
    FLAC = "flac"
    WAV = "wav"

    mp3 = MP3
    flac = FLAC
    wav = WAV


class MediaItemKind(_ValueEnum):
    GENERATED = "generated"
    SOURCE = "source"


class MediaDeletionState(_ValueEnum):
    ACTIVE = "active"
    PENDING = "pending"
    DELETED = "deleted"


class MediaFileState(_ValueEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    PURGED = "purged"


class PlaylistKind(_ValueEnum):
    PROJECT = "project"
    CUSTOM = "custom"


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


PROJECT_TITLE_MAX_LENGTH = 160
MEDIA_TITLE_MAX_LENGTH = 300


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[JobType] = mapped_column(_enum_type(JobType), nullable=False)
    title: Mapped[str] = mapped_column(String(PROJECT_TITLE_MAX_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    jobs: Mapped[list[Job]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    media_items: Mapped[list[MediaItem]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    playlists: Mapped[list[Playlist]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("job_type")
    def _keep_job_type_immutable(self, key: str, value: JobType) -> JobType:
        del key
        current = self.__dict__.get("job_type")
        if current is not None and current != value:
            raise ValueError("project job type is immutable")
        return value


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "cancel_outcome IS NULL OR cancel_outcome IN ('cancelled', 'too_late', 'unsupported')",
            name="ck_jobs_cancel_outcome",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    inference_provider: Mapped[str | None] = mapped_column(String(32))
    inference_backend: Mapped[str | None] = mapped_column(String(256))
    backend_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    current_provider_job_id: Mapped[str | None] = mapped_column(String(128))
    provider_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    current_runpod_job_id: Mapped[str | None] = mapped_column(String(128))
    current_submission_nonce: Mapped[str | None] = mapped_column(String(128))
    runpod_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    user_facing_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_outcome: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
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
    inference_provider: Mapped[str | None] = mapped_column(String(32))
    inference_backend: Mapped[str | None] = mapped_column(String(256))
    provider_job_id: Mapped[str | None] = mapped_column(String(128))
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    generation_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    job: Mapped[Job] = relationship(back_populates="outputs")
    media_item: Mapped[MediaItem | None] = relationship(back_populates="generated_output")


class MediaItem(Base):
    """Logical playable media, published only after output verification."""

    __tablename__ = "media_items"
    __table_args__ = (
        CheckConstraint("kind IN ('generated', 'source')", name="ck_media_items_kind"),
        CheckConstraint(
            "kind = 'source' OR generated_output_id IS NOT NULL",
            name="ck_media_items_generated_output",
        ),
        CheckConstraint(
            "deletion_state IN ('active', 'pending', 'deleted')",
            name="ck_media_items_deletion_state",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR (duration_seconds > 0 AND "
            "duration_seconds = duration_seconds)",
            name="ck_media_items_duration",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generated_output_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("outputs.id", ondelete="SET NULL"), unique=True, index=True
    )
    kind: Mapped[MediaItemKind] = mapped_column(
        _enum_type(MediaItemKind), nullable=False, default=MediaItemKind.GENERATED
    )
    title: Mapped[str] = mapped_column(String(MEDIA_TITLE_MAX_LENGTH), nullable=False)
    duration_seconds: Mapped[float | None]
    deletion_state: Mapped[MediaDeletionState] = mapped_column(
        _enum_type(MediaDeletionState), nullable=False, default=MediaDeletionState.ACTIVE
    )
    deletion_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="media_items")
    generated_output: Mapped[Output | None] = relationship(back_populates="media_item")
    files: Mapped[list[MediaFile]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan", passive_deletes=True
    )
    playlist_entries: Mapped[list[PlaylistEntry]] = relationship(
        back_populates="media_item", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("title")
    def _validate_title(self, key: str, value: str) -> str:
        del key
        title = value.strip() if isinstance(value, str) else ""
        if not title or len(title) > MEDIA_TITLE_MAX_LENGTH:
            raise ValueError(f"media title must be 1..{MEDIA_TITLE_MAX_LENGTH} characters")
        return title


class MediaFile(Base):
    """One physical file variant belonging to a logical media item."""

    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint("media_item_id", "format"),
        CheckConstraint(
            "storage_namespace IN ('outputs', 'library')", name="ck_media_files_namespace"
        ),
        CheckConstraint("format IN ('mp3', 'flac', 'wav')", name="ck_media_files_format"),
        CheckConstraint(
            "state IN ('active', 'quarantined', 'purged')", name="ck_media_files_state"
        ),
        CheckConstraint("byte_size > 0", name="ck_media_files_byte_size"),
        CheckConstraint("is_playback IN (0, 1)", name="ck_media_files_playback"),
        CheckConstraint("is_primary_download IN (0, 1)", name="ck_media_files_download"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_namespace: Mapped[str] = mapped_column(String(16), nullable=False, default="outputs")
    format: Mapped[OutputFormat] = mapped_column(_enum_type(OutputFormat), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_playback: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_primary_download: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[MediaFileState] = mapped_column(
        _enum_type(MediaFileState), nullable=False, default=MediaFileState.ACTIVE
    )
    quarantine_relative_path: Mapped[str | None] = mapped_column(String(1024))
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    media_item: Mapped[MediaItem] = relationship(back_populates="files")


class Playlist(Base):
    """An editable project playlist or user-created custom playlist."""

    __tablename__ = "playlists"
    __table_args__ = (
        UniqueConstraint("project_id"),
        CheckConstraint("kind IN ('project', 'custom')", name="ck_playlists_kind"),
        CheckConstraint(
            "(kind = 'project' AND project_id IS NOT NULL) OR "
            "(kind = 'custom' AND project_id IS NULL)",
            name="ck_playlists_project_pairing",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[PlaylistKind] = mapped_column(_enum_type(PlaylistKind), nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(PROJECT_TITLE_MAX_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )

    project: Mapped[Project | None] = relationship(back_populates="playlists")
    entries: Mapped[list[PlaylistEntry]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PlaylistEntry.position",
    )


class PlaylistEntry(Base):
    """A position in a playlist; no item uniqueness permits duplicates."""

    __tablename__ = "playlist_entries"
    __table_args__ = (UniqueConstraint("playlist_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    playlist: Mapped[Playlist] = relationship(back_populates="entries")
    media_item: Mapped[MediaItem] = relationship(back_populates="playlist_entries")


class ProjectDeletionAudit(Base):
    """Redacted, bounded evidence that a project was intentionally deleted."""

    __tablename__ = "project_deletion_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    job_count: Mapped[int] = mapped_column(Integer, nullable=False)
    media_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cost_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    project_created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


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
    inference_provider: Mapped[str | None] = mapped_column(String(32))
    inference_backend: Mapped[str | None] = mapped_column(String(256))
    provider_job_id: Mapped[str | None] = mapped_column(String(128))
    submission_nonce: Mapped[str | None] = mapped_column(String(128))
    provider_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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


KEEP_WARM_SECONDS = (0, 60, 120, 180, 300, 600, 900, 1800, 2700, 3600, 7200, 10800, 14400)
KEEP_WARM_LABELS = {
    0: "0 minutes",
    60: "1 minute",
    120: "2 minutes",
    180: "3 minutes",
    300: "5 minutes",
    600: "10 minutes",
    900: "15 minutes",
    1800: "30 minutes",
    2700: "45 minutes",
    3600: "1 hour",
    7200: "2 hours",
    10800: "3 hours",
    14400: "4 hours",
}
CAPACITY_LEASE_STATES = frozenset(
    {"cold", "warming", "retained", "idle", "releasing", "release_overdue"}
)
NOTIFICATION_EVENT_KINDS = frozenset(
    {
        "generation_completed",
        "managed_generation_started",
        "capacity_retained_reminder",
        "capacity_release_warning",
        "capacity_released",
        "capacity_release_overdue",
    }
)


class ControllerSetting(Base):
    """The singleton settings row owned by the database, not the environment."""

    __tablename__ = "controller_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_controller_settings_singleton"),
        CheckConstraint(
            "keep_warm_seconds IN (0, 60, 120, 180, 300, 600, 900, 1800, "
            "2700, 3600, 7200, 10800, 14400)",
            name="ck_controller_settings_keep_warm",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keep_warm_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class CapacityLease(Base):
    """Durable controller state for one provider-owned billable capacity key."""

    __tablename__ = "capacity_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('cold', 'warming', 'retained', 'idle', 'releasing', 'release_overdue')",
            name="ck_capacity_leases_state",
        ),
    )

    capacity_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="cold")
    session_id: Mapped[str | None] = mapped_column(String(36))
    idle_epoch_id: Mapped[str | None] = mapped_column(String(36))
    last_activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    release_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    warmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    release_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    action_owner: Mapped[str | None] = mapped_column(String(128))
    action_lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class NotificationEvent(Base):
    """Safe, durable notification payload and its idempotency key."""

    __tablename__ = "notification_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('generation_completed', 'managed_generation_started', "
            "'capacity_retained_reminder', 'capacity_release_warning', "
            "'capacity_released', 'capacity_release_overdue')",
            name="ck_notification_events_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE")
    )
    provider: Mapped[str | None] = mapped_column(String(32))
    capacity_key: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(String(512), nullable=False)
    target_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        "NotificationDelivery",
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PushSubscription(Base):
    """One browser push subscription; endpoint and keys are never exposed."""

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    endpoint_origin: Mapped[str] = mapped_column(String(256), nullable=False)
    p256dh: Mapped[str] = mapped_column(String(256), nullable=False)
    auth: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        "NotificationDelivery",
        back_populates="subscription",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class NotificationDelivery(Base):
    """Per-event delivery state with bounded retry ownership."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "subscription_id", name="uq_notification_delivery_target"),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'abandoned')",
            name="ck_notification_deliveries_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("notification_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("push_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[NotificationEvent] = relationship(
        "NotificationEvent", back_populates="deliveries"
    )
    subscription: Mapped[PushSubscription] = relationship(
        "PushSubscription", back_populates="deliveries"
    )


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
    {
        "rate_stale",
        "rate_unknown",
        "worker_no_evidence",
        "timing_unavailable",
        "provider_managed_pricing",
    }
)
EVIDENCE_STATUSES: frozenset[str] = frozenset({"pending", "unavailable", "complete"})

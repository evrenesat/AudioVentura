"""SQLAlchemy persistence models for durable controller state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
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

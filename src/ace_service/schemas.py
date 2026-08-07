"""Validated input shapes and path-safety helpers for the controller."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ace_service.models import JobType, OutputFormat, TransferDirection

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,12}$")
WORKER_SCHEMA_VERSION = 1


def normalize_relative_path(value: str) -> str:
    """Return a canonical relative POSIX path or reject traversal attempts."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("relative path must be a non-empty string without NUL bytes")
    if "\\" in value:
        raise ValueError("relative paths must use POSIX separators")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("relative path must not be absolute")
    if ".." in posix_path.parts:
        raise ValueError("relative path traversal is not allowed")
    parts = [part for part in posix_path.parts if part not in {"", "."}]
    if not parts:
        raise ValueError("relative path must name a file")
    return "/".join(parts)


def resolve_relative_path(root: Path, relative_path: str) -> Path:
    """Resolve a stored relative path while defending against symlink escapes."""

    normalized = normalize_relative_path(relative_path)
    resolved_root = root.expanduser().resolve()
    resolved_path = (resolved_root / normalized).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("resolved path escapes its data root")
    return resolved_path


def normalize_extension(value: str) -> str:
    extension = value.strip().lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    if not _EXTENSION_RE.fullmatch(extension):
        raise ValueError("extension must contain only 1-12 lowercase alphanumeric characters")
    return extension


def validate_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("SHA-256 values must be exactly 64 hexadecimal characters")
    return value.lower()


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    source_url: str | None = Field(default=None, max_length=2048)
    prompt: str | None = None
    lyrics: str | None = None
    rights_confirmation_at: datetime | None = None
    cover_strength: float | None = Field(default=None, ge=0, le=1)
    output_format: OutputFormat = OutputFormat.MP3
    variation_count: int = Field(default=1, ge=1, le=4)
    normalized_request_json: dict[str, Any] | None = None

    @field_validator("rights_confirmation_at")
    @classmethod
    def rights_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)


class OriginalSongRequest(BaseModel):
    """Validated creative request persisted before an original job is queued."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=3, max_length=4000)
    lyrics: str | None = Field(default=None, max_length=20_000)
    instrumental: StrictBool = False
    vocal_language: str = Field(default="en", min_length=1, max_length=32)
    duration: float | None = Field(default=None, ge=10, le=600)
    bpm: StrictInt | None = Field(default=None, ge=30, le=300)
    key_scale: str | None = Field(default=None, max_length=64)
    time_signature: StrictInt | None = Field(default=None)
    seed: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    variation_count: StrictInt = Field(default=1, ge=1, le=4)
    output_format: OutputFormat = OutputFormat.MP3

    @field_validator("description", mode="before")
    @classmethod
    def description_is_trimmed_and_bounded(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("description must be text")
        normalized = value.strip()
        if not 3 <= len(normalized) <= 4000:
            raise ValueError("description must contain 3-4000 characters")
        return normalized

    @field_validator("vocal_language")
    @classmethod
    def vocal_language_is_trimmed(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("vocal_language must not be empty")
        return normalized

    @field_validator("key_scale")
    @classmethod
    def key_scale_is_non_empty_when_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("key_scale must not be empty when supplied")
        return normalized

    @field_validator("duration", mode="before")
    @classmethod
    def duration_must_not_be_boolean(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("duration must be a number")
        return value

    @field_validator("time_signature")
    @classmethod
    def time_signature_is_supported(cls, value: int | None) -> int | None:
        if value is not None and value not in {2, 3, 4, 6}:
            raise ValueError("time_signature must be 2, 3, 4, or 6")
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> OriginalSongRequest:
        if self.instrumental and self.lyrics is not None and self.lyrics.strip():
            raise ValueError("instrumental jobs must not include lyrics")
        if self.seed is not None and self.seed + self.variation_count - 1 > 2_147_483_647:
            raise ValueError("seed progression exceeds the supported integer range")
        return self

    def to_normalized_request_json(self) -> dict[str, Any]:
        """Return the exact metadata-only shape accepted by the Runpod worker."""

        generation: dict[str, Any] = {
            "prompt": self.description,
            "instrumental": self.instrumental,
            "vocal_language": self.vocal_language,
            "output_format": self.output_format.value,
        }
        if self.lyrics is not None:
            generation["lyrics"] = self.lyrics
        if self.duration is not None:
            generation["duration"] = self.duration
        if self.bpm is not None:
            generation["bpm"] = self.bpm
        if self.key_scale is not None:
            generation["key_scale"] = self.key_scale
        if self.time_signature is not None:
            generation["time_signature"] = self.time_signature
        if self.seed is not None:
            generation["seed"] = self.seed
        return {
            "schema_version": WORKER_SCHEMA_VERSION,
            "generation": generation,
            "source": None,
        }


class OutputCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variation_index: int = Field(ge=1)
    result_index: int = Field(ge=0)
    runpod_job_id: str | None = None
    relative_path: str
    mime_type: str = Field(min_length=1, max_length=128)
    byte_size: int = Field(ge=0)
    sha256: str
    seed_metadata_json: dict[str, Any] | None = None
    generation_metadata_json: dict[str, Any] | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def sha256_is_valid(cls, value: str) -> str:
        return validate_sha256(value)


class TransferCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: TransferDirection
    expected_relative_path: str
    expected_extension: str
    max_bytes: int = Field(gt=0)
    expires_at: datetime

    @field_validator("expected_relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("expected_extension")
    @classmethod
    def extension_is_safe(cls, value: str) -> str:
        return normalize_extension(value)

    @field_validator("expires_at")
    @classmethod
    def expiry_is_utc(cls, value: datetime) -> datetime:
        return require_utc(value)

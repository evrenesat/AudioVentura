"""Validated input shapes and path-safety helpers for the controller."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ace_service.models import JobType, OutputFormat, TransferDirection

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,12}$")


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

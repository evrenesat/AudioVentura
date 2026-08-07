"""Strict, metadata-only input validation for the Runpod worker."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

SCHEMA_VERSION = 1
TASK_TYPES = frozenset({"original", "cover"})
OUTPUT_FORMATS = frozenset({"mp3", "flac", "wav"})
SOURCE_FORMATS = frozenset({"mp3"})
TRANSFER_PATH_PREFIX = "/transfer/v1/"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
DEFAULT_MAX_SOURCE_BYTES = 268_435_456
DEFAULT_MAX_OUTPUT_BYTES = 268_435_456


class SchemaError(ValueError):
    """Raised when a Runpod request is not valid for this worker."""


@dataclass(frozen=True, slots=True)
class SourceInput:
    """A prepared source object that may be fetched once by a cover job."""

    url: str
    sha256: str
    bytes: int
    format: str


@dataclass(frozen=True, slots=True)
class ResultUpload:
    """A one-job output capability."""

    url: str
    max_bytes: int


@dataclass(frozen=True, slots=True)
class GenerationInput:
    """The controller's bounded ACE-Step generation metadata."""

    prompt: str
    lyrics: str
    instrumental: bool
    vocal_language: str
    duration: float | None
    bpm: int | None
    key_scale: str | None
    time_signature: int | None
    seed: int | None
    output_format: str
    cover_strength: float


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    """Validated worker input, with no user-controlled filesystem paths."""

    schema_version: int
    job_id: str
    submission_nonce: str
    variation_index: int
    task_type: str
    generation: GenerationInput
    source: SourceInput | None
    result_upload: ResultUpload

    @classmethod
    def from_event(
        cls,
        event: Mapping[str, Any],
        *,
        allowed_transfer_host: str | None = None,
    ) -> WorkerRequest:
        """Validate a Runpod event and return only typed, bounded values."""

        if not isinstance(event, Mapping):
            raise SchemaError("event must be an object")
        raw_input = event.get("input")
        if not isinstance(raw_input, Mapping):
            raise SchemaError("event.input must be an object")
        return cls.from_mapping(raw_input, allowed_transfer_host=allowed_transfer_host)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        allowed_transfer_host: str | None = None,
    ) -> WorkerRequest:
        expected_keys = {
            "schema_version",
            "job_id",
            "submission_nonce",
            "variation_index",
            "task_type",
            "generation",
            "source",
            "result_upload",
        }
        _reject_unknown_keys(payload, expected_keys, "request")

        schema_version = payload.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise SchemaError(f"schema_version must be {SCHEMA_VERSION}")

        job_id = _uuid_string(payload.get("job_id"), "job_id")
        submission_nonce = _uuid_string(payload.get("submission_nonce"), "submission_nonce")
        variation_index = _bounded_int(payload.get("variation_index"), "variation_index", 1, 4)
        task_type = payload.get("task_type")
        if task_type not in TASK_TYPES:
            raise SchemaError("task_type must be original or cover")

        raw_generation = payload.get("generation")
        if not isinstance(raw_generation, Mapping):
            raise SchemaError("generation must be an object")
        generation = _parse_generation(raw_generation, task_type)

        raw_source = payload.get("source")
        source = None
        if raw_source is not None:
            if not isinstance(raw_source, Mapping):
                raise SchemaError("source must be an object or null")
            source = _parse_source(raw_source, allowed_transfer_host)
        if task_type == "cover" and source is None:
            raise SchemaError("cover jobs require source")
        if task_type == "original" and source is not None:
            raise SchemaError("original jobs must not include source")

        raw_upload = payload.get("result_upload")
        if not isinstance(raw_upload, Mapping):
            raise SchemaError("result_upload must be an object")
        result_upload = _parse_result_upload(raw_upload, allowed_transfer_host)

        return cls(
            schema_version=schema_version,
            job_id=job_id,
            submission_nonce=submission_nonce,
            variation_index=variation_index,
            task_type=task_type,
            generation=generation,
            source=source,
            result_upload=result_upload,
        )


def _reject_unknown_keys(payload: Mapping[str, Any], expected: set[str], name: str) -> None:
    unknown = sorted(set(payload) - expected)
    if unknown:
        raise SchemaError(f"{name} contains unsupported fields: {', '.join(unknown)}")


def _uuid_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{name} must be a UUID string")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise SchemaError(f"{name} must be a UUID string") from exc


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SchemaError(f"{name} must be an integer between {minimum} and {maximum}")
    return cast(int, value)


def _optional_text(
    payload: Mapping[str, Any], name: str, *, maximum: int, default: str | None = None
) -> str | None:
    value = payload.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise SchemaError(f"{name} must be text of at most {maximum} characters")
    return value


def _required_text(payload: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SchemaError(f"{name} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _parse_generation(payload: Mapping[str, Any], task_type: str) -> GenerationInput:
    expected_keys = {
        "prompt",
        "lyrics",
        "instrumental",
        "vocal_language",
        "duration",
        "bpm",
        "key_scale",
        "time_signature",
        "seed",
        "output_format",
        "cover_strength",
    }
    _reject_unknown_keys(payload, expected_keys, "generation")

    prompt = _required_text(payload, "prompt", maximum=4000)
    lyrics = _optional_text(payload, "lyrics", maximum=20_000, default="") or ""
    instrumental = payload.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise SchemaError("instrumental must be a boolean")
    if instrumental and lyrics.strip():
        raise SchemaError("instrumental jobs must not include lyrics")

    vocal_language = payload.get("vocal_language", "en")
    if (
        not isinstance(vocal_language, str)
        or not vocal_language.strip()
        or len(vocal_language) > 32
    ):
        raise SchemaError("vocal_language must be non-empty text of at most 32 characters")

    duration = payload.get("duration")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 10 <= duration <= 600
    ):
        raise SchemaError("duration must be between 10 and 600 seconds")
    duration_value = float(duration) if duration is not None else None

    bpm = payload.get("bpm")
    if bpm is not None:
        bpm = _bounded_int(bpm, "bpm", 30, 300)

    key_scale = _optional_text(payload, "key_scale", maximum=64)
    time_signature = payload.get("time_signature")
    if time_signature is not None:
        time_signature = _bounded_int(time_signature, "time_signature", 2, 6)
        if time_signature not in {2, 3, 4, 6}:
            raise SchemaError("time_signature must be 2, 3, 4, or 6")

    seed = payload.get("seed")
    if seed is not None:
        seed = _bounded_int(seed, "seed", 0, 2_147_483_647)

    output_format = payload.get("output_format", "mp3")
    if output_format not in OUTPUT_FORMATS:
        raise SchemaError("output_format must be mp3, flac, or wav")

    cover_strength = payload.get("cover_strength", 1.0)
    if (
        isinstance(cover_strength, bool)
        or not isinstance(cover_strength, (int, float))
        or not 0 <= cover_strength <= 1
    ):
        raise SchemaError("cover_strength must be between 0 and 1")
    if task_type == "original" and cover_strength != 1.0:
        raise SchemaError("cover_strength is only supported for cover jobs")

    return GenerationInput(
        prompt=prompt,
        lyrics=lyrics,
        instrumental=instrumental,
        vocal_language=vocal_language.strip(),
        duration=duration_value,
        bpm=bpm,
        key_scale=key_scale.strip() if key_scale is not None else None,
        time_signature=time_signature,
        seed=seed,
        output_format=output_format,
        cover_strength=float(cover_strength),
    )


def _parse_source(payload: Mapping[str, Any], allowed_transfer_host: str | None) -> SourceInput:
    _reject_unknown_keys(payload, {"url", "sha256", "bytes", "format"}, "source")
    url = _capability_url(payload.get("url"), "source.url", allowed_transfer_host)
    sha256 = payload.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise SchemaError("source.sha256 must be 64 hexadecimal characters")
    byte_size = payload.get("bytes")
    max_source_bytes = _configured_limit("ACE_WORKER_MAX_SOURCE_BYTES", DEFAULT_MAX_SOURCE_BYTES)
    if (
        isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or not 1 <= byte_size <= max_source_bytes
    ):
        raise SchemaError(f"source.bytes must be between 1 and {max_source_bytes}")
    source_format = payload.get("format")
    if source_format not in SOURCE_FORMATS:
        raise SchemaError("source.format must be mp3")
    return SourceInput(url=url, sha256=sha256.lower(), bytes=byte_size, format=source_format)


def _parse_result_upload(
    payload: Mapping[str, Any], allowed_transfer_host: str | None
) -> ResultUpload:
    _reject_unknown_keys(payload, {"url", "max_bytes"}, "result_upload")
    url = _capability_url(payload.get("url"), "result_upload.url", allowed_transfer_host)
    max_bytes = payload.get("max_bytes")
    configured_limit = _configured_limit("ACE_WORKER_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= configured_limit
    ):
        raise SchemaError(f"result_upload.max_bytes must be between 1 and {configured_limit}")
    return ResultUpload(url=url, max_bytes=max_bytes)


def _capability_url(value: Any, name: str, allowed_transfer_host: str | None) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise SchemaError(f"{name} must be a valid HTTPS capability URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SchemaError(f"{name} must be a valid HTTPS capability URL") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith(TRANSFER_PATH_PREFIX)
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise SchemaError(f"{name} must be an HTTPS transfer capability URL")
    if allowed_transfer_host and hostname.lower() != allowed_transfer_host.strip().lower():
        raise SchemaError(f"{name} host is not the configured transfer host")
    return value


def _configured_limit(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SchemaError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise SchemaError(f"{name} must be a positive integer")
    return value


def split_capability_url(url: str) -> SplitResult:
    """Return a parsed URL for the transfer client without logging its token."""

    return urlsplit(url)

"""Strict, versioned metadata-only input validation for the Runpod worker."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
TASK_TYPES = frozenset({"original", "cover"})
OUTPUT_FORMATS = frozenset({"mp3", "flac", "wav"})
SOURCE_FORMATS = frozenset({"mp3"})
PROFILE_IDS = frozenset({"fast-beta-v1", "quality-v1"})
PROMPT_MODES = frozenset({"direct", "enhance", "auto-compose"})
TRANSFER_PATH_PREFIX = "/transfer/v1/"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
DEFAULT_MAX_SOURCE_BYTES = 268_435_456
DEFAULT_MAX_OUTPUT_BYTES = 268_435_456
MAX_CAPTION_LENGTH = 511
MAX_LYRICS_LENGTH = 4_095
MAX_SEED = 2_147_483_647
MAX_RESULT_METADATA_BYTES = 65_536


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
    """Validated user and resolved generation values."""

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
    audio_cover_strength: float
    cover_noise_strength: float
    prompt_mode: str | None = None
    duration_mode: str | None = None
    duration_seconds: float | None = None
    target_style: str | None = None
    remix_guidance: str | None = None

    @property
    def cover_strength(self) -> float:
        """Legacy read-only alias for v1 tests and diagnostics."""

        return self.audio_cover_strength


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
    profile_id: str | None = None
    resolved_parameters: dict[str, Any] | None = None

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
        if not isinstance(payload, Mapping):
            raise SchemaError("request must be an object")
        schema_version = payload.get("schema_version")
        if isinstance(schema_version, bool) or schema_version not in {
            LEGACY_SCHEMA_VERSION,
            SCHEMA_VERSION,
        }:
            raise SchemaError("schema_version must be 1 or 2")
        if schema_version == LEGACY_SCHEMA_VERSION:
            return _parse_v1(payload, allowed_transfer_host)
        return _parse_v2(payload, allowed_transfer_host)


def _parse_envelope(
    payload: Mapping[str, Any], expected_keys: set[str], *, version: int
) -> tuple[str, str, int, str, Mapping[str, Any], Any, Any]:
    _reject_unknown_keys(payload, expected_keys, "request")
    job_id = _uuid_string(payload.get("job_id"), "job_id")
    submission_nonce = _uuid_string(payload.get("submission_nonce"), "submission_nonce")
    variation_index = _bounded_int(payload.get("variation_index"), "variation_index", 1, 4)
    task_type = payload.get("task_type")
    if task_type not in TASK_TYPES:
        raise SchemaError("task_type must be original or cover")
    raw_generation = payload.get("generation")
    if not isinstance(raw_generation, Mapping):
        raise SchemaError("generation must be an object")
    raw_source = payload.get("source")
    raw_upload = payload.get("result_upload")
    if not isinstance(raw_upload, Mapping):
        raise SchemaError("result_upload must be an object")
    return (
        job_id,
        submission_nonce,
        variation_index,
        cast(str, task_type),
        raw_generation,
        raw_source,
        raw_upload,
    )


def _parse_v1(payload: Mapping[str, Any], allowed_transfer_host: str | None) -> WorkerRequest:
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
    job_id, nonce, variation, task_type, raw_generation, raw_source, raw_upload = _parse_envelope(
        payload, expected_keys, version=LEGACY_SCHEMA_VERSION
    )
    generation = _parse_generation_v1(raw_generation, task_type)
    source = _parse_optional_source(raw_source, task_type, allowed_transfer_host)
    result_upload = _parse_result_upload(raw_upload, allowed_transfer_host)
    return WorkerRequest(
        schema_version=LEGACY_SCHEMA_VERSION,
        job_id=job_id,
        submission_nonce=nonce,
        variation_index=variation,
        task_type=task_type,
        generation=generation,
        source=source,
        result_upload=result_upload,
    )


def _parse_v2(payload: Mapping[str, Any], allowed_transfer_host: str | None) -> WorkerRequest:
    expected_keys = {
        "schema_version",
        "job_id",
        "submission_nonce",
        "variation_index",
        "task_type",
        "profile_id",
        "resolved_parameters",
        "source_duration_seconds",
        "resolved_target_duration_seconds",
        "ace_duration_seconds",
        "cover_staging",
        "generation",
        "source",
        "result_upload",
    }
    job_id, nonce, variation, task_type, raw_generation, raw_source, raw_upload = _parse_envelope(
        payload, expected_keys, version=SCHEMA_VERSION
    )
    profile_id = payload.get("profile_id")
    if profile_id not in PROFILE_IDS:
        raise SchemaError("profile_id must be fast-beta-v1 or quality-v1")
    top_level_durations = _parse_v2_duration_metadata(payload, task_type)
    if task_type == "original" and "cover_staging" in payload:
        raise SchemaError("original requests must not include cover staging metadata")
    _parse_cover_staging(payload.get("cover_staging"), task_type)
    raw_resolved = payload.get("resolved_parameters")
    if not isinstance(raw_resolved, Mapping):
        raise SchemaError("resolved_parameters must be an object")
    resolved = _parse_resolved_parameters(raw_resolved, task_type, str(profile_id))
    if task_type == "cover":
        resolved_durations = (
            resolved.get("source_duration_seconds"),
            resolved.get("target_duration_seconds"),
            resolved.get("duration"),
        )
        if top_level_durations != resolved_durations:
            raise SchemaError("cover duration metadata does not match resolved parameters")
    generation = _parse_generation_v2(raw_generation, task_type, resolved)
    source = _parse_optional_source(raw_source, task_type, allowed_transfer_host)
    result_upload = _parse_result_upload(raw_upload, allowed_transfer_host)
    return WorkerRequest(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        submission_nonce=nonce,
        variation_index=variation,
        task_type=task_type,
        generation=generation,
        source=source,
        result_upload=result_upload,
        profile_id=str(profile_id),
        resolved_parameters=resolved,
    )


def _parse_v2_duration_metadata(
    payload: Mapping[str, Any], task_type: str
) -> tuple[float, float, float] | None:
    names = (
        "source_duration_seconds",
        "resolved_target_duration_seconds",
        "ace_duration_seconds",
    )
    present = [name for name in names if name in payload]
    if task_type == "original":
        if present:
            raise SchemaError("original requests must not include cover duration metadata")
        return None
    if present != list(names):
        raise SchemaError("cover requests require complete source duration metadata")
    values = (
        _bounded_float(payload[names[0]], names[0], 0.000001, 600.0),
        _bounded_float(payload[names[1]], names[1], 0.000001, 600.0),
        _bounded_float(payload[names[2]], names[2], 0.000001, 600.0),
    )
    if not values[0] == values[1] == values[2]:
        raise SchemaError("cover duration metadata must agree")
    return values


def _parse_cover_staging(value: Any, task_type: str) -> None:
    if task_type == "original":
        if value is not None:
            raise SchemaError("original requests must not include cover staging metadata")
        return
    if not isinstance(value, Mapping):
        raise SchemaError("cover requests require cover staging metadata")
    _reject_unknown_keys(value, {"status", "staged_at", "confirmed_at"}, "cover_staging")
    if value.get("status") != "confirmed":
        raise SchemaError("cover request is not confirmed for worker submission")
    for name in ("staged_at", "confirmed_at"):
        timestamp = value.get(name)
        if timestamp is not None and (
            not isinstance(timestamp, str) or not timestamp or len(timestamp) > 64
        ):
            raise SchemaError(f"cover_staging.{name} is malformed")


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


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{name} must be a finite number between {minimum:g} and {maximum:g}")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise SchemaError(f"{name} must be a finite number between {minimum:g} and {maximum:g}")
    return result


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


def _parse_generation_v1(payload: Mapping[str, Any], task_type: str) -> GenerationInput:
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
    prompt = _required_text(payload, "prompt", maximum=4_000)
    lyrics = _optional_text(payload, "lyrics", maximum=20_000, default="") or ""
    instrumental = payload.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise SchemaError("instrumental must be a boolean")
    if instrumental and lyrics.strip():
        raise SchemaError("instrumental jobs must not include lyrics")
    vocal_language = _parse_vocal_language(payload.get("vocal_language", "en"))
    duration = _parse_legacy_duration(payload.get("duration"))
    bpm = _optional_bounded_int(payload.get("bpm"), "bpm", 30, 300)
    key_scale = _optional_text(payload, "key_scale", maximum=64)
    time_signature = _parse_time_signature(payload.get("time_signature"))
    seed = _optional_bounded_int(payload.get("seed"), "seed", 0, MAX_SEED)
    output_format = _parse_output_format(payload.get("output_format", "mp3"))
    cover_strength = _bounded_float(payload.get("cover_strength", 1.0), "cover_strength", 0, 1)
    if task_type == "original" and cover_strength != 1.0:
        raise SchemaError("cover_strength is only supported for cover jobs")
    return GenerationInput(
        prompt=prompt,
        lyrics=lyrics,
        instrumental=instrumental,
        vocal_language=vocal_language,
        duration=duration,
        bpm=bpm,
        key_scale=key_scale.strip() if key_scale is not None else None,
        time_signature=time_signature,
        seed=seed,
        output_format=output_format,
        audio_cover_strength=cover_strength,
        cover_noise_strength=0.0,
    )


def _parse_generation_v2(
    payload: Mapping[str, Any], task_type: str, resolved: Mapping[str, Any]
) -> GenerationInput:
    expected_keys = {
        "prompt",
        "lyrics",
        "instrumental",
        "vocal_language",
        "prompt_mode",
        "duration_mode",
        "duration_seconds",
        "duration",
        "bpm",
        "key_scale",
        "time_signature",
        "seed",
        "output_format",
        "audio_cover_strength",
        "cover_noise_strength",
        "target_style",
        "remix_guidance",
    }
    _reject_unknown_keys(payload, expected_keys, "generation")
    prompt = _required_text(payload, "prompt", maximum=MAX_CAPTION_LENGTH)
    lyrics = _optional_text(payload, "lyrics", maximum=MAX_LYRICS_LENGTH, default="") or ""
    if prompt != resolved["caption"]:
        raise SchemaError("generation prompt does not match resolved caption")
    if lyrics != resolved["lyrics"]:
        raise SchemaError("generation lyrics do not match resolved lyrics")
    instrumental = payload.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise SchemaError("instrumental must be a boolean")
    if instrumental and lyrics.strip():
        raise SchemaError("instrumental jobs must not include lyrics")
    vocal_language = _parse_vocal_language(payload.get("vocal_language", "en"))
    prompt_mode = payload.get("prompt_mode")
    if prompt_mode not in PROMPT_MODES:
        raise SchemaError("prompt_mode is not supported")
    if prompt_mode != resolved["prompt_mode"]:
        raise SchemaError("generation prompt_mode does not match resolved parameters")
    duration_mode = payload.get("duration_mode")
    if task_type == "cover":
        if duration_mode != "source":
            raise SchemaError("cover duration_mode must be source")
    elif duration_mode not in {"auto", "custom"}:
        raise SchemaError("original duration_mode must be auto or custom")
    if duration_mode != resolved["duration_mode"]:
        raise SchemaError("generation duration_mode does not match resolved parameters")
    duration = payload.get("duration")
    duration_value: float | None
    if duration is None:
        duration_value = None
    else:
        duration_value = _bounded_float(duration, "duration", -1.0, 600.0)
    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is not None:
        duration_seconds = _bounded_float(duration_seconds, "duration_seconds", 0.000001, 600.0)
    if task_type == "original":
        if duration_mode == "auto":
            if duration_value != -1.0 or duration_seconds is not None:
                raise SchemaError("auto duration must resolve to -1.0 without custom seconds")
        else:
            if duration_value is None or not 10 <= duration_value <= 600:
                raise SchemaError("custom duration must resolve to 10-600 seconds")
            if duration_seconds is None or duration_seconds != duration_value:
                raise SchemaError("custom duration seconds do not match the resolved duration")
    else:
        if duration_value is None or duration_value <= 0:
            raise SchemaError("cover duration must be finalized from the source")
        if duration_seconds is None or duration_seconds != duration_value:
            raise SchemaError("cover duration seconds do not match the resolved duration")
    if duration_value != resolved["duration"]:
        raise SchemaError("generation duration does not match resolved parameters")
    bpm = _optional_bounded_int(payload.get("bpm"), "bpm", 30, 300)
    key_scale = _optional_text(payload, "key_scale", maximum=64)
    time_signature = _parse_time_signature(payload.get("time_signature"))
    seed = _optional_bounded_int(payload.get("seed"), "seed", 0, MAX_SEED)
    if seed != resolved["seed"]:
        raise SchemaError("generation seed does not match resolved parameters")
    output_format = _parse_output_format(payload.get("output_format", "mp3"))
    audio_strength = _bounded_float(
        payload.get("audio_cover_strength", resolved["audio_cover_strength"]),
        "audio_cover_strength",
        0,
        1,
    )
    noise_strength = _bounded_float(
        payload.get("cover_noise_strength", resolved["cover_noise_strength"]),
        "cover_noise_strength",
        0,
        1,
    )
    if task_type == "original" and (audio_strength != 1.0 or noise_strength != 0.0):
        raise SchemaError("original jobs must use neutral cover controls")
    if audio_strength != resolved["audio_cover_strength"]:
        raise SchemaError("audio_cover_strength does not match resolved parameters")
    if noise_strength != resolved["cover_noise_strength"]:
        raise SchemaError("cover_noise_strength does not match resolved parameters")
    if task_type == "cover" and (
        "audio_cover_strength" not in payload or "cover_noise_strength" not in payload
    ):
        raise SchemaError("cover requests must include both cover controls")
    target_style = _optional_text(payload, "target_style", maximum=MAX_CAPTION_LENGTH)
    remix_guidance = _optional_text(payload, "remix_guidance", maximum=MAX_CAPTION_LENGTH)
    if task_type == "cover" and target_style is None:
        raise SchemaError("cover target_style is required")
    if task_type == "original" and ("target_style" in payload or "remix_guidance" in payload):
        raise SchemaError("original requests must not include cover prompt fields")
    return GenerationInput(
        prompt=prompt,
        lyrics=lyrics,
        instrumental=instrumental,
        vocal_language=vocal_language,
        duration=duration_value,
        bpm=bpm,
        key_scale=key_scale.strip() if key_scale is not None else None,
        time_signature=time_signature,
        seed=seed,
        output_format=output_format,
        audio_cover_strength=audio_strength,
        cover_noise_strength=noise_strength,
        prompt_mode=cast(str, prompt_mode),
        duration_mode=cast(str, duration_mode),
        duration_seconds=duration_seconds,
        target_style=target_style,
        remix_guidance=remix_guidance,
    )


def _parse_resolved_parameters(
    payload: Mapping[str, Any], task_type: str, profile_id: str
) -> dict[str, Any]:
    expected_keys = {
        "profile_id",
        "task_type",
        "prompt_mode",
        "duration_mode",
        "duration",
        "caption",
        "lyrics",
        "seed",
        "inference_steps",
        "shift",
        "lm_temperature",
        "lm_cfg_scale",
        "lm_top_k",
        "lm_top_p",
        "lm_negative_prompt",
        "thinking",
        "use_cot_metas",
        "use_cot_caption",
        "use_cot_language",
        "audio_cover_strength",
        "cover_noise_strength",
        "source_duration_seconds",
        "target_duration_seconds",
    }
    _reject_unknown_keys(payload, expected_keys, "resolved_parameters")
    required = {
        "profile_id",
        "task_type",
        "prompt_mode",
        "duration_mode",
        "duration",
        "caption",
        "lyrics",
        "seed",
        "inference_steps",
        "shift",
        "lm_temperature",
        "lm_cfg_scale",
        "lm_top_k",
        "lm_top_p",
        "lm_negative_prompt",
        "thinking",
        "use_cot_metas",
        "use_cot_caption",
        "use_cot_language",
        "audio_cover_strength",
        "cover_noise_strength",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise SchemaError("resolved_parameters is missing: " + ", ".join(missing))
    if payload.get("profile_id") != profile_id:
        raise SchemaError("resolved profile_id does not match the request")
    if payload.get("task_type") != task_type:
        raise SchemaError("resolved task_type does not match the request")
    prompt_mode = payload.get("prompt_mode")
    if prompt_mode not in PROMPT_MODES:
        raise SchemaError("resolved prompt_mode is not supported")
    duration_mode = payload.get("duration_mode")
    if task_type == "cover" and duration_mode != "source":
        raise SchemaError("resolved cover duration_mode must be source")
    if task_type == "original" and duration_mode not in {"auto", "custom"}:
        raise SchemaError("resolved original duration_mode is not supported")
    caption = payload.get("caption")
    lyrics = payload.get("lyrics")
    if not isinstance(caption, str) or not caption.strip() or len(caption) > MAX_CAPTION_LENGTH:
        raise SchemaError(f"resolved caption must be at most {MAX_CAPTION_LENGTH} characters")
    if not isinstance(lyrics, str) or len(lyrics) > MAX_LYRICS_LENGTH:
        raise SchemaError(f"resolved lyrics must be at most {MAX_LYRICS_LENGTH} characters")
    duration = payload.get("duration")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(float(duration))
    ):
        raise SchemaError("resolved duration must be finite")
    seed = payload.get("seed")
    if seed is not None:
        _bounded_int(seed, "resolved seed", 0, MAX_SEED)
    _bounded_int(payload.get("inference_steps"), "inference_steps", 1, 512)
    _bounded_float(payload.get("shift"), "shift", 0.000001, 100)
    _bounded_float(payload.get("lm_temperature"), "lm_temperature", 0, 2)
    _bounded_float(payload.get("lm_cfg_scale"), "lm_cfg_scale", 0, 100)
    _bounded_int(payload.get("lm_top_k"), "lm_top_k", 0, 100_000)
    _bounded_float(payload.get("lm_top_p"), "lm_top_p", 0, 1)
    if (
        not isinstance(payload.get("lm_negative_prompt"), str)
        or len(payload["lm_negative_prompt"]) > 511
    ):
        raise SchemaError("lm_negative_prompt is malformed")
    for name in ("thinking", "use_cot_metas", "use_cot_caption", "use_cot_language"):
        if not isinstance(payload.get(name), bool):
            raise SchemaError(f"resolved {name} must be a boolean")
    _bounded_float(payload.get("audio_cover_strength"), "resolved audio_cover_strength", 0, 1)
    _bounded_float(payload.get("cover_noise_strength"), "resolved cover_noise_strength", 0, 1)
    if task_type == "original" and (
        payload["audio_cover_strength"] != 1.0 or payload["cover_noise_strength"] != 0.0
    ):
        raise SchemaError("resolved original jobs must use neutral cover controls")
    if task_type == "cover" and not {
        "source_duration_seconds",
        "target_duration_seconds",
    }.issubset(payload):
        raise SchemaError("resolved cover duration metadata is incomplete")
    if task_type == "original" and {
        "source_duration_seconds",
        "target_duration_seconds",
    }.intersection(payload):
        raise SchemaError("resolved original parameters must not include source duration metadata")
    for name in ("source_duration_seconds", "target_duration_seconds"):
        if name in payload:
            _bounded_float(payload[name], name, 0.000001, 600)
    return dict(payload)


def _parse_optional_source(
    raw_source: Any, task_type: str, allowed_transfer_host: str | None
) -> SourceInput | None:
    source = None
    if raw_source is not None:
        if not isinstance(raw_source, Mapping):
            raise SchemaError("source must be an object or null")
        source = _parse_source(raw_source, allowed_transfer_host)
    if task_type == "cover" and source is None:
        raise SchemaError("cover jobs require source")
    if task_type == "original" and source is not None:
        raise SchemaError("original jobs must not include source")
    return source


def _parse_vocal_language(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise SchemaError("vocal_language must be non-empty text of at most 32 characters")
    return value.strip()


def _parse_legacy_duration(value: Any) -> float:
    if value is None:
        return -1.0
    return _bounded_float(value, "duration", 10, 600)


def _optional_bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, name, minimum, maximum)


def _parse_time_signature(value: Any) -> int | None:
    if value is None:
        return None
    result = _bounded_int(value, "time_signature", 2, 6)
    if result not in {2, 3, 4, 6}:
        raise SchemaError("time_signature must be 2, 3, 4, or 6")
    return result


def _parse_output_format(value: Any) -> str:
    if value not in OUTPUT_FORMATS:
        raise SchemaError("output_format must be mp3, flac, or wav")
    return cast(str, value)


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

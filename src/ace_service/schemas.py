"""Validated input shapes and path-safety helpers for the controller."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

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
from ace_service.quality_profiles import (
    FAST_PROFILE_ID,
    MAX_CAPTION_LENGTH,
    MAX_LYRICS_LENGTH,
    MAX_SEED,
    compose_cover_prompt,
    contains_duration_language,
    explicit_duration_seconds,
    has_vague_duration_language,
    resolve_parameters,
    resolve_profile,
    resolve_prompt_mode,
    validate_caption,
    validate_duration,
    validate_lyrics,
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,12}$")
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}
)
_YOUTUBE_PLAYLIST_KEYS = frozenset({"list", "index", "start_radio", "playlist", "pp"})
_YOUTUBE_WATCH_KEYS = frozenset({"v", "t", "start"})
LEGACY_WORKER_SCHEMA_VERSION = 1
WORKER_SCHEMA_VERSION = 2


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


def validate_youtube_url(value: str) -> str:
    """Accept one public YouTube video and reject playlists or redirectors."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("youtube_url must be one approved single-video URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname.lower() if parsed.hostname else None
        port = parsed.port
    except ValueError as exc:
        raise ValueError("youtube_url must be one approved single-video URL") from exc
    if (
        parsed.scheme != "https"
        or hostname not in _YOUTUBE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError("youtube_url must be one approved single-video URL")

    query = parse_qs(parsed.query, keep_blank_values=True)
    if _YOUTUBE_PLAYLIST_KEYS.intersection(query):
        raise ValueError("youtube_url must be one approved single-video URL")

    video_id: str | None = None
    if hostname == "youtu.be":
        parts = parsed.path.split("/")
        if len(parts) == 2 and parts[1] and not set(query) - {"t", "start"}:
            video_id = parts[1]
    elif parsed.path == "/watch":
        if not set(query) - _YOUTUBE_WATCH_KEYS and len(query.get("v", [])) == 1:
            video_id = query["v"][0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] in {"shorts", "embed"} and not query:
            video_id = parts[1]
    if video_id is None or not _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("youtube_url must be one approved single-video URL")
    return value


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    source_url: str | None = Field(default=None, max_length=2048)
    prompt: str | None = None
    lyrics: str | None = None
    rights_confirmation_at: datetime | None = None
    audio_cover_strength: float | None = Field(default=None, ge=0, le=1)
    cover_noise_strength: float | None = Field(default=None, ge=0, le=1)
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
    prompt_mode: Literal["direct", "enhance", "auto-compose"] = "direct"
    duration_mode: Literal["auto", "custom"] = "auto"
    duration_seconds: float | None = None
    bpm: StrictInt | None = Field(default=None, ge=30, le=300)
    key_scale: str | None = Field(default=None, max_length=64)
    time_signature: StrictInt | None = Field(default=None)
    seed: StrictInt | None = Field(default=None, ge=0, le=MAX_SEED)
    variation_count: StrictInt = Field(default=1, ge=1, le=4)
    profile_id: str = FAST_PROFILE_ID
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

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def duration_must_be_finite_number(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("duration_seconds must be a finite number")
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("duration_seconds must be a finite number")
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
        try:
            resolve_profile(self.profile_id)
            resolve_prompt_mode(self.prompt_mode)
            validate_caption(self.description)
            validate_lyrics(self.lyrics)
            validate_duration(self.duration_mode, self.duration_seconds)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if contains_duration_language(self.description):
            explicit_durations = explicit_duration_seconds(self.description)
            if (
                self.duration_mode != "custom"
                or self.duration_seconds is None
                or not explicit_durations
                or has_vague_duration_language(self.description)
                or any(
                    not math.isclose(
                        duration,
                        self.duration_seconds,
                        rel_tol=0.0,
                        abs_tol=1e-6,
                    )
                    for duration in explicit_durations
                )
            ):
                raise ValueError(
                    "duration-like language must match the selected Custom duration in seconds"
                )
        return self

    def to_normalized_request_json(self) -> dict[str, Any]:
        """Return the exact metadata-only shape accepted by the Runpod worker."""

        ace_duration = validate_duration(self.duration_mode, self.duration_seconds)
        resolved_parameters = resolve_parameters(
            self.profile_id,
            task_type="original",
            prompt_mode=self.prompt_mode,
            duration_mode=self.duration_mode,
            duration=ace_duration,
            caption=self.description,
            lyrics=self.lyrics,
            seed=self.seed,
        )
        generation: dict[str, Any] = {
            "prompt": self.description,
            "lyrics": self.lyrics or "",
            "instrumental": self.instrumental,
            "vocal_language": self.vocal_language,
            "prompt_mode": self.prompt_mode,
            "duration_mode": self.duration_mode,
            "duration_seconds": self.duration_seconds,
            "duration": ace_duration,
            "bpm": self.bpm,
            "key_scale": self.key_scale,
            "time_signature": self.time_signature,
            "seed": self.seed,
            "output_format": self.output_format.value,
        }
        return {
            "schema_version": WORKER_SCHEMA_VERSION,
            "task_type": "original",
            "profile_id": self.profile_id,
            "resolved_parameters": resolved_parameters,
            "generation": generation,
            "source": None,
        }


class CoverRequest(BaseModel):
    """Validated metadata for one home-ingested ACE-Step cover job."""

    model_config = ConfigDict(extra="forbid")

    youtube_url: str | None = Field(default=None, min_length=1, max_length=2048)
    target_style: str = Field(min_length=3, max_length=4000)
    source_style: str | None = Field(default=None, min_length=1, max_length=4000)
    remix_guidance: str | None = Field(default=None, max_length=4000)
    source_lyrics: str | None = Field(default=None, max_length=20_000)
    lyrics: str | None = Field(default=None, max_length=20_000)
    audio_cover_strength: float | None = None
    cover_noise_strength: float | None = None
    strength: float | None = Field(default=None, ge=0, le=1)
    start_seconds: float | None = Field(default=None, ge=0, le=600)
    end_seconds: float | None = Field(default=None, ge=0, le=600)
    before_seconds: float | None = Field(default=None, ge=0, le=600)
    after_seconds: float | None = Field(default=None, ge=0, le=600)
    duration_mode: Literal["source", "custom"] = "source"
    duration_seconds: float | None = None
    variation_count: StrictInt = Field(default=1, ge=1, le=4)
    seed: StrictInt | None = Field(default=None, ge=0, le=MAX_SEED)
    profile_id: str = FAST_PROFILE_ID
    output_format: OutputFormat = OutputFormat.MP3
    # Retained as an internal compatibility invariant for persisted schema-v2
    # jobs. Browser submissions no longer ask for a redundant confirmation.
    rights_confirmation: StrictBool = True

    @field_validator("youtube_url", mode="before")
    @classmethod
    def youtube_url_is_approved(cls, value: str | None) -> str | None:
        return validate_youtube_url(value) if value is not None else None

    @field_validator("target_style", mode="before")
    @classmethod
    def target_style_is_trimmed(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("target_style must be text")
        normalized = value.strip()
        if not 3 <= len(normalized) <= 4000:
            raise ValueError("target_style must contain 3-4000 characters")
        return normalized

    @field_validator("remix_guidance", mode="before")
    @classmethod
    def remix_guidance_is_trimmed(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("remix_guidance must be text")
        normalized = value.strip()
        return normalized or None

    @field_validator("lyrics", mode="before")
    @classmethod
    def empty_lyrics_are_omitted(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("lyrics must be text")
        return None if not value.strip() else value

    @field_validator("rights_confirmation")
    @classmethod
    def rights_must_be_confirmed(cls, value: bool) -> bool:
        if not value:
            raise ValueError("rights_confirmation must be true")
        return value

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def duration_must_be_finite_number(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("duration_seconds must be a finite number")
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("duration_seconds must be a finite number")
        return value

    @model_validator(mode="after")
    def effective_prompt_is_bounded(self) -> CoverRequest:
        try:
            resolve_profile(self.profile_id)
            effective_prompt = self.effective_prompt
            validate_lyrics(self.lyrics)
            if self.duration_mode == "source":
                if self.duration_seconds is not None:
                    raise ValueError("Source duration must not include custom seconds")
            else:
                validate_duration(self.duration_mode, self.duration_seconds)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if contains_duration_language(effective_prompt):
            explicit_durations = explicit_duration_seconds(effective_prompt)
            if (
                self.duration_mode != "custom"
                or self.duration_seconds is None
                or not explicit_durations
                or has_vague_duration_language(effective_prompt)
                or any(
                    not math.isclose(
                        duration,
                        self.duration_seconds,
                        rel_tol=0.0,
                        abs_tol=1e-6,
                    )
                    for duration in explicit_durations
                )
            ):
                raise ValueError(
                    "duration-like language must match the selected Custom duration in seconds"
                )
        if self.seed is not None and self.seed + self.variation_count - 1 > MAX_SEED:
            raise ValueError("seed progression exceeds the supported integer range")
        return self

    @field_validator("audio_cover_strength", "cover_noise_strength", mode="before")
    @classmethod
    def cover_strengths_must_be_finite_numbers(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("cover controls must be numbers between 0 and 1")
        if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
            raise ValueError("cover controls must be numbers between 0 and 1")
        return value

    @property
    def effective_prompt(self) -> str:
        """Compose style and optional guidance without rewriting either value."""

        return compose_cover_prompt(self.target_style, self.remix_guidance)

    @property
    def effective_audio_cover_strength(self) -> float:
        if self.audio_cover_strength is not None:
            return self.audio_cover_strength
        return float(resolve_profile(self.profile_id)["audio_cover_strength"])

    @property
    def effective_cover_noise_strength(self) -> float:
        if self.cover_noise_strength is not None:
            return self.cover_noise_strength
        return float(resolve_profile(self.profile_id)["cover_noise_strength"])

    def to_normalized_request_json(
        self, *, source_duration_seconds: float | None = None
    ) -> dict[str, Any]:
        """Return the metadata-only cover shape accepted by the Runpod worker."""

        if self.duration_mode == "custom":
            ace_duration = validate_duration("custom", self.duration_seconds)
        elif source_duration_seconds is not None:
            ace_duration = validate_duration(
                "source", float(source_duration_seconds), allow_source=True
            )
        else:
            ace_duration = -1.0
        resolved_parameters = resolve_parameters(
            self.profile_id,
            task_type="cover",
            prompt_mode="direct",
            duration_mode=self.duration_mode,
            duration=ace_duration,
            caption=self.effective_prompt,
            lyrics=self.lyrics,
            seed=self.seed,
            audio_cover_strength=self.effective_audio_cover_strength,
            cover_noise_strength=self.effective_cover_noise_strength,
            source_duration_seconds=source_duration_seconds,
        )
        if source_duration_seconds is None:
            resolved_parameters.pop("source_duration_seconds", None)
            if self.duration_mode == "source":
                resolved_parameters.pop("target_duration_seconds", None)
                resolved_parameters["duration"] = None
        generation: dict[str, Any] = {
            "prompt": self.effective_prompt,
            "target_style": self.target_style,
            "remix_guidance": self.remix_guidance,
            "lyrics": self.lyrics or "",
            "instrumental": False,
            "vocal_language": "en",
            "prompt_mode": "direct",
            "duration_mode": self.duration_mode,
            "duration_seconds": (
                self.duration_seconds if self.duration_mode == "custom" else source_duration_seconds
            ),
            "duration": (
                self.duration_seconds if self.duration_mode == "custom" else source_duration_seconds
            ),
            "audio_cover_strength": self.effective_audio_cover_strength,
            "cover_noise_strength": self.effective_cover_noise_strength,
            "seed": self.seed,
            "output_format": self.output_format.value,
        }
        generation.update(
            {
                name: value
                for name, value in {
                    "strength": self.strength,
                    "start_seconds": self.start_seconds,
                    "end_seconds": self.end_seconds,
                    "before_seconds": self.before_seconds,
                    "after_seconds": self.after_seconds,
                }.items()
                if value is not None
            }
        )
        if self.source_style is not None:
            generation["source_style"] = self.source_style
        if self.source_lyrics is not None:
            generation["source_lyrics"] = self.source_lyrics
        return {
            "schema_version": WORKER_SCHEMA_VERSION,
            "task_type": "cover",
            "profile_id": self.profile_id,
            "resolved_parameters": resolved_parameters,
            "generation": generation,
            "source": None,
        }


def finalize_cover_normalized_request(
    normalized_request: dict[str, Any], source_duration_seconds: float
) -> dict[str, Any]:
    """Add the probed source duration to a staged v2 cover request."""

    if (
        not isinstance(source_duration_seconds, (int, float))
        or isinstance(source_duration_seconds, bool)
        or not math.isfinite(float(source_duration_seconds))
        or source_duration_seconds <= 0
    ):
        raise ValueError("source duration must be a finite positive number")
    source_duration = validate_duration("source", float(source_duration_seconds), allow_source=True)
    value = deepcopy(normalized_request)
    if value.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise ValueError("only schema-v2 cover requests can be finalized")
    generation = value.get("generation")
    resolved = value.get("resolved_parameters")
    if not isinstance(generation, dict) or not isinstance(resolved, dict):
        raise ValueError("cover request is missing its resolved parameters")
    duration_mode = generation.get("duration_mode")
    if duration_mode == "source":
        target_duration = source_duration
        generation["duration_seconds"] = target_duration
        generation["duration"] = target_duration
    elif duration_mode == "custom":
        target_duration = validate_duration("custom", generation.get("duration_seconds"))
        generation["duration"] = target_duration
    else:
        raise ValueError("cover request has an invalid duration mode")
    resolved["duration_mode"] = duration_mode
    resolved["duration"] = target_duration
    resolved["source_duration_seconds"] = source_duration
    resolved["target_duration_seconds"] = target_duration
    value["source_duration_seconds"] = source_duration
    value["resolved_target_duration_seconds"] = target_duration
    value["ace_duration_seconds"] = target_duration
    return value


MAX_RESULT_METADATA_BYTES = 65_536
MAX_RESULT_METADATA_DEPTH = 6
MAX_RESULT_METADATA_KEYS = 128
MAX_RESULT_METADATA_STRING = 16_384


def validate_worker_result_metadata(
    value: Any, *, expected_schema_version: int | None = None
) -> dict[str, Any]:
    """Validate and copy bounded private metadata before database persistence."""

    if not isinstance(value, dict):
        raise ValueError("worker result metadata must be an object")
    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {
        LEGACY_WORKER_SCHEMA_VERSION,
        WORKER_SCHEMA_VERSION,
    }:
        raise ValueError("worker result metadata has an unsupported schema version")
    if expected_schema_version is not None and schema_version != expected_schema_version:
        raise ValueError("worker result schema version does not match the stored request")
    _validate_metadata_node(value, depth=0)
    try:
        import json

        encoded_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError) as exc:
        raise ValueError("worker result metadata is not JSON serializable") from exc
    if encoded_size > MAX_RESULT_METADATA_BYTES:
        raise ValueError("worker result metadata is too large")
    for field_name in ("input", "effective"):
        record = value.get(field_name)
        if isinstance(record, dict):
            caption = record.get("caption", record.get("prompt"))
            lyrics = record.get("lyrics")
            caption_limit = MAX_CAPTION_LENGTH if schema_version == WORKER_SCHEMA_VERSION else 4_000
            lyrics_limit = MAX_LYRICS_LENGTH if schema_version == WORKER_SCHEMA_VERSION else 20_000
            if caption is not None:
                if (
                    not isinstance(caption, str)
                    or not caption.strip()
                    or len(caption) > caption_limit
                ):
                    raise ValueError(
                        f"worker result caption must be at most {caption_limit} characters"
                    )
            if lyrics is not None:
                if not isinstance(lyrics, str) or len(lyrics) > lyrics_limit:
                    raise ValueError(
                        f"worker result lyrics must be at most {lyrics_limit} characters"
                    )
    return deepcopy(value)


def _validate_metadata_node(value: Any, *, depth: int) -> None:
    if depth > MAX_RESULT_METADATA_DEPTH:
        raise ValueError("worker result metadata is too deeply nested")
    if isinstance(value, dict):
        if len(value) > MAX_RESULT_METADATA_KEYS:
            raise ValueError("worker result metadata contains too many fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("worker result metadata contains an invalid field name")
            _validate_metadata_node(child, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_RESULT_METADATA_KEYS:
            raise ValueError("worker result metadata contains too many list items")
        for child in value:
            _validate_metadata_node(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MAX_RESULT_METADATA_STRING:
            raise ValueError("worker result metadata contains an oversized text field")
        if "/transfer/v1/" in value:
            raise ValueError("worker result metadata must not contain transfer URLs")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("worker result metadata contains a non-finite number")
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("worker result metadata contains an unsupported value")


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

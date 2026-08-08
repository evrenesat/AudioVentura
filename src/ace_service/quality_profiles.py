"""Immutable quality controls and deterministic prompt helpers.

The controller resolves this small catalog once, at request creation time.  A
worker receives the resulting values and never looks up a profile by name, so
later catalog edits cannot reinterpret a persisted job.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

MAX_CAPTION_LENGTH = 511
MAX_LYRICS_LENGTH = 4_095
MIN_DURATION_SECONDS = 10.0
MAX_DURATION_SECONDS = 600.0
MAX_SEED = 2_147_483_647

FAST_PROFILE_ID = "fast-beta-v1"
QUALITY_PROFILE_ID = "quality-v1"
PROFILE_IDS = frozenset({FAST_PROFILE_ID, QUALITY_PROFILE_ID})

_PROFILE_CATALOG: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        FAST_PROFILE_ID: MappingProxyType(
            {
                "inference_steps": 8,
                "shift": 1.0,
                "lm_temperature": 0.85,
                "lm_cfg_scale": 2.0,
                "lm_top_k": 0,
                "lm_top_p": 0.9,
                "lm_negative_prompt": "NO USER INPUT",
                "audio_cover_strength": 0.65,
                "cover_noise_strength": 0.0,
            }
        ),
        QUALITY_PROFILE_ID: MappingProxyType(
            {
                "inference_steps": 8,
                "shift": 1.0,
                "lm_temperature": 0.85,
                "lm_cfg_scale": 2.0,
                "lm_top_k": 0,
                "lm_top_p": 0.9,
                "lm_negative_prompt": "NO USER INPUT",
                "audio_cover_strength": 0.65,
                "cover_noise_strength": 0.20,
            }
        ),
    }
)

_PROMPT_MODES: Mapping[str, Mapping[str, bool]] = MappingProxyType(
    {
        "direct": MappingProxyType(
            {
                "thinking": False,
                "use_cot_metas": False,
                "use_cot_caption": False,
                "use_cot_language": False,
            }
        ),
        "enhance": MappingProxyType(
            {
                "thinking": False,
                "use_cot_metas": True,
                "use_cot_caption": True,
                "use_cot_language": True,
            }
        ),
        "auto-compose": MappingProxyType(
            {
                "thinking": True,
                "use_cot_metas": True,
                "use_cot_caption": True,
                "use_cot_language": True,
            }
        ),
    }
)
PROMPT_MODE_IDS = frozenset(_PROMPT_MODES)

_DURATION_HINT_RE = re.compile(
    r"(?i)(?:\b\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b"
    r"|\b(?:duration|length|longer|shorter|extended|full[- ]length|same[- ]length|"
    r"minutes?|seconds?)\b)"
)
_EXPLICIT_DURATION_RE = re.compile(
    r"(?i)(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s*(?:-\s*)?"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b"
)
_VAGUE_DURATION_RE = re.compile(
    r"(?i)\b(?:longer|shorter|extended|full[- ]length|same[- ]length|"
    r"lengthen(?:ed)?|shorten(?:ed)?|duration[- ]changing)\b"
)


class QualityProfileError(ValueError):
    """Raised when a profile or resolved quality value is invalid."""


def resolve_profile(profile_id: str) -> Mapping[str, Any]:
    """Return a fresh immutable view of one catalog entry."""

    try:
        values = _PROFILE_CATALOG[profile_id]
    except KeyError as exc:
        raise QualityProfileError(f"profile_id must be one of {sorted(PROFILE_IDS)}") from exc
    return MappingProxyType(dict(values))


def resolve_prompt_mode(prompt_mode: str) -> Mapping[str, bool]:
    """Return a fresh immutable view of the pinned prompt-mode flags."""

    try:
        values = _PROMPT_MODES[prompt_mode]
    except KeyError as exc:
        raise QualityProfileError("prompt_mode must be direct, enhance, or auto-compose") from exc
    return MappingProxyType(dict(values))


def compose_cover_prompt(target_style: str, remix_guidance: str | None = None) -> str:
    """Join cover style inputs without rewriting either user-authored value."""

    effective = target_style if remix_guidance is None else f"{target_style}\n\n{remix_guidance}"
    validate_caption(effective)
    return effective


def validate_caption(value: str) -> str:
    """Validate an effective ACE caption without silently truncating it."""

    if not isinstance(value, str) or not value.strip():
        raise QualityProfileError("caption must be non-empty text")
    if len(value) > MAX_CAPTION_LENGTH:
        raise QualityProfileError(
            f"the final caption must be at most {MAX_CAPTION_LENGTH} characters"
        )
    return value


def validate_lyrics(value: str | None) -> str | None:
    """Validate supplied lyrics while preserving their exact characters."""

    if value is None or not value.strip():
        return None
    if len(value) > MAX_LYRICS_LENGTH:
        raise QualityProfileError(
            f"the final lyrics must be at most {MAX_LYRICS_LENGTH} characters"
        )
    return value


def contains_duration_language(value: str) -> bool:
    """Identify duration-like prose that must not be interpreted as a value."""

    return bool(_DURATION_HINT_RE.search(value))


def explicit_duration_seconds(value: str) -> list[float]:
    """Return only numeric second/minute phrases, without changing the text."""

    durations: list[float] = []
    for match in _EXPLICIT_DURATION_RE.finditer(value):
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        durations.append(amount * (60.0 if unit.startswith("m") else 1.0))
    return durations


def has_vague_duration_language(value: str) -> bool:
    """Identify duration-changing prose that has no bounded numeric meaning."""

    return bool(_VAGUE_DURATION_RE.search(value))


def validate_duration(
    duration_mode: str,
    duration_seconds: float | None,
    *,
    allow_source: bool = False,
) -> float:
    """Return the exact ACE duration for an original or source-duration cover."""

    if allow_source and duration_mode == "source":
        if (
            duration_seconds is None
            or not math.isfinite(duration_seconds)
            or not 0 < duration_seconds <= MAX_DURATION_SECONDS
        ):
            raise QualityProfileError("source duration must be finite and at most 600 seconds")
        return float(duration_seconds)
    if duration_mode == "auto":
        if duration_seconds is not None:
            raise QualityProfileError("Auto duration must not include custom seconds")
        return -1.0
    if duration_mode != "custom":
        raise QualityProfileError("duration_mode must be auto or custom")
    if duration_seconds is None or not math.isfinite(duration_seconds):
        raise QualityProfileError("Custom duration requires finite seconds")
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise QualityProfileError("Custom duration must be between 10 and 600 seconds")
    return float(duration_seconds)


def resolve_parameters(
    profile_id: str,
    *,
    task_type: str,
    prompt_mode: str,
    duration_mode: str,
    duration: float,
    caption: str,
    lyrics: str | None,
    seed: int | None,
    audio_cover_strength: float | None = None,
    cover_noise_strength: float | None = None,
    source_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Resolve all profile-owned ACE values into a JSON-ready record."""

    if task_type not in {"original", "cover"}:
        raise QualityProfileError("task_type must be original or cover")
    validate_caption(caption)
    validate_lyrics(lyrics)
    if seed is not None and not 0 <= seed <= MAX_SEED:
        raise QualityProfileError("seed is outside the supported integer range")
    if not math.isfinite(duration):
        raise QualityProfileError("duration must be finite")

    profile = dict(resolve_profile(profile_id))
    prompt_flags = dict(resolve_prompt_mode(prompt_mode))
    if task_type == "original":
        resolved_audio_strength = 1.0
        resolved_noise_strength = 0.0
    else:
        resolved_audio_strength = (
            profile["audio_cover_strength"]
            if audio_cover_strength is None
            else audio_cover_strength
        )
        resolved_noise_strength = (
            profile["cover_noise_strength"]
            if cover_noise_strength is None
            else cover_noise_strength
        )
    for name, value in (
        ("audio_cover_strength", resolved_audio_strength),
        ("cover_noise_strength", resolved_noise_strength),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise QualityProfileError(f"{name} must be between 0 and 1")

    result: dict[str, Any] = {
        **profile,
        **prompt_flags,
        "profile_id": profile_id,
        "task_type": task_type,
        "prompt_mode": prompt_mode,
        "duration_mode": duration_mode,
        "duration": float(duration),
        "caption": caption,
        "lyrics": lyrics or "",
        "seed": seed,
        "audio_cover_strength": float(resolved_audio_strength),
        "cover_noise_strength": float(resolved_noise_strength),
    }
    if source_duration_seconds is not None:
        if (
            not math.isfinite(source_duration_seconds)
            or not 0 < source_duration_seconds <= MAX_DURATION_SECONDS
        ):
            raise QualityProfileError("source duration must be finite and at most 600 seconds")
        result["source_duration_seconds"] = float(source_duration_seconds)
        result["target_duration_seconds"] = float(duration)
    return result


def profile_catalog() -> Mapping[str, Mapping[str, Any]]:
    """Expose an immutable catalog for diagnostics and focused tests."""

    return cast(Mapping[str, Mapping[str, Any]], _PROFILE_CATALOG)

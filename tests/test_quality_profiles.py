from __future__ import annotations

import pytest

from ace_service.quality_profiles import (
    FAST_PROFILE_ID,
    QUALITY_PROFILE_ID,
    QualityProfileError,
    compose_cover_prompt,
    profile_catalog,
    resolve_profile,
    resolve_prompt_mode,
)
from ace_service.schemas import CoverRequest


def test_profile_catalog_and_resolved_values_are_immutable() -> None:
    catalog = profile_catalog()
    fast = resolve_profile(FAST_PROFILE_ID)

    assert catalog[QUALITY_PROFILE_ID]["cover_noise_strength"] == 0.20
    assert fast["inference_steps"] == 8
    with pytest.raises(TypeError):
        catalog[FAST_PROFILE_ID]["inference_steps"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        fast["inference_steps"] = 99  # type: ignore[index]
    assert resolve_profile(FAST_PROFILE_ID)["inference_steps"] == 8


@pytest.mark.parametrize(
    ("mode", "thinking", "use_cot"),
    [("direct", False, False), ("enhance", False, True), ("auto-compose", True, True)],
)
def test_prompt_modes_pin_each_ace_flag(mode: str, thinking: bool, use_cot: bool) -> None:
    flags = resolve_prompt_mode(mode)

    assert flags["thinking"] is thinking
    assert flags["use_cot_metas"] is use_cot
    assert flags["use_cot_caption"] is use_cot
    assert flags["use_cot_language"] is use_cot
    assert "use_cot_lyrics" not in flags


def test_cover_prompt_helper_preserves_inputs_and_rejects_final_overflow() -> None:
    assert compose_cover_prompt("dreamy synthwave", "wider drums") == (
        "dreamy synthwave\n\nwider drums"
    )
    with pytest.raises(QualityProfileError, match="511"):
        compose_cover_prompt("x" * 511, "y")


def test_quality_profile_defaults_are_used_when_cover_controls_are_omitted() -> None:
    request = CoverRequest(
        youtube_url="https://www.youtube.com/watch?v=abc123",
        target_style="dreamy synthwave",
        profile_id=QUALITY_PROFILE_ID,
        rights_confirmation=True,
    )

    normalized = request.to_normalized_request_json()
    assert normalized["resolved_parameters"]["audio_cover_strength"] == 0.65
    assert normalized["resolved_parameters"]["cover_noise_strength"] == 0.20

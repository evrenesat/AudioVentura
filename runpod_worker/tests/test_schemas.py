from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from runpod_worker.schemas import SchemaError, WorkerRequest

JOB_ID = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"
SOURCE_BYTES = b"prepared source"


def _payload(task_type: str = "original") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "submission_nonce": NONCE,
        "variation_index": 1,
        "task_type": task_type,
        "generation": {
            "prompt": "warm analog synth",
            "lyrics": "",
            "instrumental": False,
            "vocal_language": "en",
            "duration": 20,
            "bpm": 120,
            "key_scale": "C major",
            "time_signature": 4,
            "seed": 17,
            "output_format": "mp3",
            **({"cover_strength": 0.75} if task_type == "cover" else {}),
        },
        "source": None,
        "result_upload": {
            "url": "https://transfer.example.test/transfer/v1/output/capability",
            "max_bytes": 1024,
        },
    }
    if task_type == "cover":
        payload["source"] = {
            "url": "https://transfer.example.test/transfer/v1/source/capability",
            "sha256": sha256(SOURCE_BYTES).hexdigest(),
            "bytes": len(SOURCE_BYTES),
            "format": "mp3",
        }
    return payload


def _v2_payload(task_type: str = "original") -> dict[str, object]:
    is_cover = task_type == "cover"
    resolved: dict[str, object] = {
        "profile_id": "fast-beta-v1",
        "task_type": task_type,
        "prompt_mode": "direct",
        "duration_mode": "source" if is_cover else "custom",
        "duration": 42.0 if is_cover else 30.0,
        "caption": "warm analog synth",
        "lyrics": "",
        "seed": 17,
        "inference_steps": 8,
        "shift": 1.0,
        "lm_temperature": 0.85,
        "lm_cfg_scale": 2.0,
        "lm_top_k": 0,
        "lm_top_p": 0.9,
        "lm_negative_prompt": "NO USER INPUT",
        "thinking": False,
        "use_cot_metas": False,
        "use_cot_caption": False,
        "use_cot_language": False,
        "audio_cover_strength": 0.65 if is_cover else 1.0,
        "cover_noise_strength": 0.0,
    }
    generation: dict[str, object] = {
        "prompt": "warm analog synth",
        "lyrics": "",
        "instrumental": False,
        "vocal_language": "en",
        "prompt_mode": "direct",
        "duration_mode": "source" if is_cover else "custom",
        "duration_seconds": 42.0 if is_cover else 30.0,
        "duration": 42.0 if is_cover else 30.0,
        "bpm": 120,
        "key_scale": "C major",
        "time_signature": 4,
        "seed": 17,
        "output_format": "mp3",
        "audio_cover_strength": 0.65 if is_cover else 1.0,
        "cover_noise_strength": 0.0,
    }
    payload: dict[str, object] = {
        "schema_version": 2,
        "job_id": JOB_ID,
        "submission_nonce": NONCE,
        "variation_index": 1,
        "task_type": task_type,
        "profile_id": "fast-beta-v1",
        "resolved_parameters": resolved,
        "generation": generation,
        "source": None,
        "result_upload": {
            "url": "https://transfer.example.test/transfer/v1/output/capability",
            "max_bytes": 1024,
        },
    }
    if is_cover:
        resolved["source_duration_seconds"] = 42.0
        resolved["target_duration_seconds"] = 42.0
        generation["target_style"] = "warm analog synth"
        generation["remix_guidance"] = None
        payload.update(
            {
                "source_duration_seconds": 42.0,
                "resolved_target_duration_seconds": 42.0,
                "ace_duration_seconds": 42.0,
                "cover_staging": {"status": "confirmed"},
                "source": {
                    "url": "https://transfer.example.test/transfer/v1/source/capability",
                    "sha256": sha256(SOURCE_BYTES).hexdigest(),
                    "bytes": len(SOURCE_BYTES),
                    "format": "mp3",
                },
            }
        )
    return payload


def test_original_request_is_typed_and_bounded() -> None:
    request = WorkerRequest.from_mapping(_payload(), allowed_transfer_host="transfer.example.test")

    assert request.job_id == JOB_ID
    assert request.task_type == "original"
    assert request.generation.seed == 17
    assert request.source is None
    assert request.result_upload.max_bytes == 1024


def test_v1_cover_maps_legacy_strength_and_omitted_duration() -> None:
    original = _payload()
    assert isinstance(original["generation"], dict)
    original["generation"].pop("duration")
    request = WorkerRequest.from_mapping(original, allowed_transfer_host="transfer.example.test")
    assert request.generation.duration == -1.0

    cover = WorkerRequest.from_mapping(
        _payload("cover"), allowed_transfer_host="transfer.example.test"
    )
    assert cover.generation.audio_cover_strength == 0.75
    assert cover.generation.cover_noise_strength == 0.0


def test_cover_request_requires_prepared_mp3_and_https_capabilities() -> None:
    request = WorkerRequest.from_mapping(
        _payload("cover"), allowed_transfer_host="transfer.example.test"
    )

    assert request.source is not None
    assert request.source.format == "mp3"

    invalid = deepcopy(_payload("cover"))
    assert isinstance(invalid["source"], dict)
    invalid["source"]["url"] = "http://transfer.example.test/transfer/v1/source/capability"
    with pytest.raises(SchemaError, match="HTTPS"):
        WorkerRequest.from_mapping(invalid)


def test_v2_request_is_strict_and_preserves_independent_controls() -> None:
    request = WorkerRequest.from_mapping(
        _v2_payload("cover"), allowed_transfer_host="transfer.example.test"
    )

    assert request.schema_version == 2
    assert request.profile_id == "fast-beta-v1"
    assert request.generation.audio_cover_strength == 0.65
    assert request.generation.cover_noise_strength == 0.0
    assert request.generation.duration == 42.0
    assert request.resolved_parameters is not None
    assert request.resolved_parameters["target_duration_seconds"] == 42.0


@pytest.mark.parametrize(
    ("location", "field"),
    [("top", "unexpected"), ("generation", "cover_strength"), ("resolved", "use_cot_lyrics")],
)
def test_v2_unknown_fields_and_legacy_alias_are_rejected(location: str, field: str) -> None:
    payload = _v2_payload("cover" if location == "top" else "original")
    if location == "top":
        payload[field] = True
    elif location == "generation":
        assert isinstance(payload["generation"], dict)
        payload["generation"][field] = 0.5
    else:
        assert isinstance(payload["resolved_parameters"], dict)
        payload["resolved_parameters"][field] = True

    with pytest.raises(SchemaError):
        WorkerRequest.from_mapping(payload, allowed_transfer_host="transfer.example.test")


def test_v2_cover_requires_confirmed_source_duration_metadata() -> None:
    payload = _v2_payload("cover")
    payload.pop("resolved_target_duration_seconds")

    with pytest.raises(SchemaError, match="complete source duration"):
        WorkerRequest.from_mapping(payload, allowed_transfer_host="transfer.example.test")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "unsupported"),
        ("variation_index", 0),
        ("schema_version", 2),
        ("schema_version", 3),
        ("job_id", "not-a-uuid"),
    ],
)
def test_invalid_request_metadata_is_rejected(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(SchemaError):
        WorkerRequest.from_mapping(payload)


def test_instrumental_request_rejects_lyrics() -> None:
    payload = _payload()
    assert isinstance(payload["generation"], dict)
    payload["generation"]["instrumental"] = True
    payload["generation"]["lyrics"] = "must be empty"

    with pytest.raises(SchemaError, match="lyrics"):
        WorkerRequest.from_mapping(payload)


def test_transfer_host_allowlist_is_exact() -> None:
    payload = _payload()

    with pytest.raises(SchemaError, match="configured transfer host"):
        WorkerRequest.from_mapping(payload, allowed_transfer_host="other.example.test")

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


def test_original_request_is_typed_and_bounded() -> None:
    request = WorkerRequest.from_mapping(_payload(), allowed_transfer_host="transfer.example.test")

    assert request.job_id == JOB_ID
    assert request.task_type == "original"
    assert request.generation.seed == 17
    assert request.source is None
    assert request.result_upload.max_bytes == 1024


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_type", "unsupported"),
        ("variation_index", 0),
        ("schema_version", 2),
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

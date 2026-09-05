"""Tests for the ailocals.v1 transport protocol boundary."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest

from ace_service.ailocals.protocol import (
    ACE_DECODED_PAYLOAD_MAX_BYTES,
    CAPABILITY_ACE,
    CAPABILITY_APPLE_SPEECH,
    CAPABILITY_CHATTERBOX,
    CAPABILITY_RELAY,
    PAYLOAD_MAX_BYTES,
    PROTOCOL_VERSION,
    AilocalsError,
    ErrorCode,
    build_lease_response,
    canonical_request_identity,
    decode_complete_metadata,
    decode_enroll_request,
    decode_fail_request,
    decode_heartbeat_request,
    decode_lease_payload_fields,
    decode_lease_request,
    decode_presence_request,
    encode_lease_payload,
    error_envelope,
    format_timestamp,
    new_worker_token,
    parse_json,
    parse_timestamp,
    token_hash,
)

ACE_PARAMETERS = {
    "worker_schema": 2,
    "model_bundle_revision": "ace-step-v15-fixture-1",
    "manifest_sha256": "a" * 64,
    "accelerator": "mps",
    "formats": ["mp3", "flac", "wav"],
}
APPLE_PARAMETERS = {
    "engine": "avspeech",
    "languages": ["nl-NL"],
    "unit_kinds": ["word", "phrase", "sentence"],
    "max_bytes": 2097152,
    "max_duration_ms": 120000,
}
CHATTERBOX_PARAMETERS = {
    "engine": "chatterbox",
    "languages": ["nl"],
    "unit_kinds": ["sentence"],
    "max_bytes": 2097152,
    "max_duration_ms": 60000,
}
RELAY_PARAMETERS = {
    "max_completion_bytes": 2097152,
    "operations": ["chat_completion", "list_models"],
}


def _enroll_body() -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "worker_name": "Fixture Mac",
        "software_version": "0.1.0 (1)",
        "capabilities": [
            {"id": CAPABILITY_APPLE_SPEECH, "category": "tts", "parameters": APPLE_PARAMETERS},
            {
                "id": CAPABILITY_CHATTERBOX,
                "category": "tts",
                "parameters": CHATTERBOX_PARAMETERS,
            },
            {"id": CAPABILITY_RELAY, "category": "llm", "parameters": RELAY_PARAMETERS},
        ],
    }


def test_timestamp_roundtrip_and_strict_forms() -> None:
    value = datetime(2026, 9, 5, 12, 0, 0, 123000, tzinfo=UTC)
    rendered = format_timestamp(value)
    assert rendered == "2026-09-05T12:00:00.123Z"
    assert parse_timestamp(rendered) == value
    with pytest.raises(AilocalsError):
        parse_timestamp("2026-09-05T12:00:00Z")
    with pytest.raises(AilocalsError):
        parse_timestamp("2026-09-05T12:00:00.123+00:00")
    with pytest.raises(AilocalsError):
        parse_timestamp("not-a-timestamp")


def test_parse_json_rejects_duplicate_keys_nan_and_trailing() -> None:
    assert parse_json(b'{"a": 1}') == {"a": 1}
    with pytest.raises(AilocalsError):
        parse_json(b'{"a": 1, "a": 2}')
    with pytest.raises(AilocalsError):
        parse_json(b'{"a": NaN}')
    with pytest.raises(AilocalsError):
        parse_json(b'{"a": 1} {"b": 2}')
    with pytest.raises(AilocalsError):
        parse_json(b'{"a": "\xff"}')
    with pytest.raises(AilocalsError):
        parse_json(b"")
    assert parse_json(b"[1, 2]") == [1, 2]


def test_decode_enroll_request_accepts_owner_selected_subset() -> None:
    worker_name, software_version, entries = decode_enroll_request(_enroll_body())
    assert worker_name == "Fixture Mac"
    assert software_version == "0.1.0 (1)"
    assert [entry.id for entry in entries] == [
        CAPABILITY_APPLE_SPEECH,
        CAPABILITY_CHATTERBOX,
        CAPABILITY_RELAY,
    ]


def test_decode_enroll_request_accepts_ace_entry() -> None:
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "worker_name": "Music Mac",
        "software_version": "0.1.0",
        "capabilities": [{"id": CAPABILITY_ACE, "category": "music", "parameters": ACE_PARAMETERS}],
    }
    _, _, entries = decode_enroll_request(body)
    assert entries[0].parameters.formats == ("mp3", "flac", "wav")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(protocol_version="speech-worker.v1"),
        lambda body: body.update(extra_field=True),
        lambda body: body.update(worker_name="x" * 121),
        lambda body: body.update(worker_name=""),
        lambda body: body.update(software_version="bad\nversion"),
        lambda body: body.update(capabilities=[]),
        lambda body: body.update(
            capabilities=[
                {"id": CAPABILITY_APPLE_SPEECH, "category": "tts", "parameters": APPLE_PARAMETERS}
            ]
            * 2
        ),
        lambda body: body[  # noqa: B023
            "capabilities"
        ].__setitem__(0, {"id": "music.other.v1", "category": "music", "parameters": {}}),
    ],
)
def test_decode_enroll_request_rejects_invalid_bodies(mutate) -> None:
    body = _enroll_body()
    mutate(body)
    with pytest.raises(AilocalsError):
        decode_enroll_request(body)


def test_decode_enroll_request_rejects_mismatched_parameters() -> None:
    body = _enroll_body()
    body["capabilities"][0]["parameters"]["engine"] = "chatterbox"
    with pytest.raises(AilocalsError):
        decode_enroll_request(body)
    body = _enroll_body()
    body["capabilities"][2]["parameters"]["max_completion_bytes"] = 12
    with pytest.raises(AilocalsError):
        decode_enroll_request(body)
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "worker_name": "Music Mac",
        "software_version": "0.1.0",
        "capabilities": [
            {
                "id": CAPABILITY_ACE,
                "category": "music",
                "parameters": {**ACE_PARAMETERS, "accelerator": "cuda"},
            }
        ],
    }
    with pytest.raises(AilocalsError):
        decode_enroll_request(body)


def _presence_body(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"protocol_version": PROTOCOL_VERSION, "capabilities": entries}


def test_decode_presence_request_accepts_truthful_snapshot() -> None:
    entries = decode_presence_request(
        _presence_body(
            [
                {
                    "id": CAPABILITY_APPLE_SPEECH,
                    "state": "ready",
                    "accepting": True,
                    "active_jobs": 0,
                    "reason": None,
                },
                {
                    "id": CAPABILITY_CHATTERBOX,
                    "state": "busy",
                    "accepting": False,
                    "active_jobs": 1,
                    "reason": None,
                },
                {
                    "id": CAPABILITY_RELAY,
                    "state": "paused",
                    "accepting": False,
                    "active_jobs": 0,
                    "reason": "user_paused",
                },
                {
                    "id": CAPABILITY_ACE,
                    "state": "busy",
                    "accepting": False,
                    "active_jobs": 0,
                    "reason": "insufficient_memory",
                },
            ]
        )
    )
    assert len(entries) == 4


@pytest.mark.parametrize(
    "entry",
    [
        # Paused must not accept work.
        {
            "id": CAPABILITY_RELAY,
            "state": "paused",
            "accepting": True,
            "active_jobs": 0,
            "reason": "user_paused",
        },
        # A resource wait needs a resource reason and accepting=false.
        {
            "id": CAPABILITY_CHATTERBOX,
            "state": "busy",
            "accepting": True,
            "active_jobs": 0,
            "reason": "memory_pressure",
        },
        {
            "id": CAPABILITY_CHATTERBOX,
            "state": "busy",
            "accepting": False,
            "active_jobs": 0,
            "reason": "user_paused",
        },
        # A running job reports no reason.
        {
            "id": CAPABILITY_CHATTERBOX,
            "state": "busy",
            "accepting": False,
            "active_jobs": 1,
            "reason": "slot_busy",
        },
        # Ready is idle and accepting.
        {
            "id": CAPABILITY_APPLE_SPEECH,
            "state": "ready",
            "accepting": False,
            "active_jobs": 0,
            "reason": None,
        },
        {
            "id": CAPABILITY_APPLE_SPEECH,
            "state": "ready",
            "accepting": True,
            "active_jobs": 1,
            "reason": None,
        },
        # Unknown reason.
        {
            "id": CAPABILITY_APPLE_SPEECH,
            "state": "error",
            "accepting": False,
            "active_jobs": 0,
            "reason": "exploded",
        },
    ],
)
def test_decode_presence_request_rejects_inconsistent_entries(entry: dict[str, object]) -> None:
    with pytest.raises(AilocalsError):
        decode_presence_request(_presence_body([entry]))


def test_decode_lease_request_bounds_wait_seconds() -> None:
    decoded = decode_lease_request(
        {"protocol_version": PROTOCOL_VERSION, "capability_id": CAPABILITY_ACE, "wait_seconds": 25}
    )
    assert decoded.wait_seconds == 25
    assert (
        decode_lease_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "capability_id": CAPABILITY_ACE,
                "wait_seconds": 0,
            }
        ).wait_seconds
        == 0
    )
    for wait in (-1, 26, True, 1.5, "0"):
        with pytest.raises(AilocalsError):
            decode_lease_request(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "capability_id": CAPABILITY_ACE,
                    "wait_seconds": wait,
                }
            )
    with pytest.raises(AilocalsError) as excinfo:
        decode_lease_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "capability_id": "tts.unknown.v9",
                "wait_seconds": 0,
            }
        )
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_decode_heartbeat_fail_and_metadata() -> None:
    heartbeat = decode_heartbeat_request(
        {"protocol_version": PROTOCOL_VERSION, "attempt": 1, "progress_percent": 40}
    )
    assert heartbeat.progress_percent == 40
    with pytest.raises(AilocalsError):
        decode_heartbeat_request(
            {"protocol_version": PROTOCOL_VERSION, "attempt": 1, "progress_percent": 101}
        )
    for code in (
        "canceled",
        "interrupted",
        "resource_exhausted",
        "invalid_payload",
        "setup_required",
        "execution_failed",
        "relay_unreachable",
        "relay_auth",
        "relay_invalid_response",
        "relay_model_unknown",
    ):
        decoded = decode_fail_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "attempt": 1,
                "code": code,
                "retryable": code in {"resource_exhausted", "relay_unreachable"},
            }
        )
        assert decoded.code == code
    with pytest.raises(AilocalsError):
        decode_fail_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "attempt": 1,
                "code": "v1.relay_unreachable",
                "retryable": True,
            }
        )
    metadata = decode_complete_metadata(
        {
            "protocol_version": PROTOCOL_VERSION,
            "attempt": 1,
            "result_sha256": hashlib.sha256(b"result").hexdigest(),
        }
    )
    assert metadata.attempt == 1
    with pytest.raises(AilocalsError):
        decode_complete_metadata(
            {
                "protocol_version": PROTOCOL_VERSION,
                "attempt": 1,
                "result_sha256": hashlib.sha256(b"result").hexdigest().upper(),
            }
        )


def test_lease_payload_encode_decode_roundtrip_and_hash_mismatch() -> None:
    payload = {"render_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "spoken_text": "Demosatz ✓ / ok"}
    payload_base64, payload_sha256 = encode_lease_payload(payload)
    decoded_bytes = decode_lease_payload_fields(
        {
            "payload_encoding": "base64",
            "payload_base64": payload_base64,
            "payload_sha256": payload_sha256,
        }
    )
    assert json.loads(decoded_bytes) == payload
    altered = base64.b64encode(b'{"tampered": true}').decode("ascii")
    with pytest.raises(AilocalsError):
        decode_lease_payload_fields(
            {
                "payload_encoding": "base64",
                "payload_base64": altered,
                "payload_sha256": payload_sha256,
            }
        )
    with pytest.raises(AilocalsError):
        decode_lease_payload_fields(
            {
                "payload_encoding": "base64",
                "payload_base64": base64.b64encode(b"x" * (PAYLOAD_MAX_BYTES + 1)).decode("ascii")[
                    :4194300
                ],
                "payload_sha256": payload_sha256,
            }
        )


def test_build_lease_response_has_exact_fields() -> None:
    payload_base64, payload_sha256 = encode_lease_payload({"ok": True})
    expires = datetime(2026, 9, 5, 12, 1, 30, tzinfo=UTC)
    body = build_lease_response(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt=1,
        lease_token=new_worker_token(),
        lease_expires_at=expires,
        deadline_at=None,
        capability_id=CAPABILITY_ACE,
        payload_base64=payload_base64,
        payload_sha256=payload_sha256,
    )
    assert set(body) == {
        "protocol_version",
        "job_id",
        "attempt",
        "lease_token",
        "lease_expires_at",
        "deadline_at",
        "capability_id",
        "payload_encoding",
        "payload_base64",
        "payload_sha256",
    }
    assert body["deadline_at"] is None
    assert len(json.dumps(body).encode("utf-8")) < 3 * 1024 * 1024


def test_canonical_request_identity_ignores_transfer_urls() -> None:
    base = {
        "schema_version": 2,
        "job_id": "11111111-1111-4111-8111-111111111111",
        "generation": {"prompt": "fixture prompt", "lyrics": ""},
        "source": {"url": "https://transfer.invalid/a", "sha256": "b" * 64, "bytes": 10},
        "result_upload": {"url": "https://transfer.invalid/b", "max_bytes": 1},
    }
    other_urls = copy.deepcopy(base)
    other_urls["source"]["url"] = "https://transfer.invalid/other"
    other_urls["result_upload"]["url"] = "https://transfer.invalid/other"
    assert canonical_request_identity(base) == canonical_request_identity(other_urls)
    different = copy.deepcopy(base)
    different["generation"]["prompt"] = "another prompt"
    assert canonical_request_identity(base) != canonical_request_identity(different)


def test_error_envelope_and_status_mapping() -> None:
    envelope = error_envelope(ErrorCode.LEASE_LOST, "lease is no longer valid")
    assert envelope == {
        "protocol_version": PROTOCOL_VERSION,
        "error": {"code": "lease_lost", "message": "lease is no longer valid"},
    }
    assert ErrorCode("worker_busy") in ErrorCode
    from ace_service.ailocals.protocol import ERROR_STATUS

    assert ERROR_STATUS[ErrorCode.UNAUTHORIZED] == 401
    assert ERROR_STATUS[ErrorCode.PAYLOAD_TOO_LARGE] == 413
    assert ERROR_STATUS[ErrorCode.INTERNAL_ERROR] == 503


def test_worker_tokens_are_url_safe_and_hashed_stably() -> None:
    token = new_worker_token()
    assert len(token) == 43
    assert token_hash(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token_hash(token) != token
    assert new_worker_token() != token


def test_ace_payload_bound_is_transport_documented() -> None:
    assert ACE_DECODED_PAYLOAD_MAX_BYTES == 65536

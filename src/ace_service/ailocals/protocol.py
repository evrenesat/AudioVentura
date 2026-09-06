"""ailocals.v1 transport protocol: strict DTOs, validation, and wire constants.

This module is the boundary validator for the shared universal-worker
protocol. It owns no database, product, or network behavior. Field semantics
beyond the transport envelope remain owned by each product's existing
validators, per ``contracts/ailocals-v1/PROTOCOL.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = "ailocals.v1"
ROUTE_NAMESPACE = "api/ailocals/v1"

WORKER_TOKEN_HEADER = "X-Ailocals-Worker-Token"
ENROLLMENT_TOKEN_HEADER = "X-Ailocals-Enrollment-Token"
LEASE_TOKEN_HEADER = "X-Ailocals-Lease-Token"

CAPABILITY_ACE = "music.ace-step.v1"
CAPABILITY_APPLE_SPEECH = "tts.apple-speech.v1"
CAPABILITY_CHATTERBOX = "tts.chatterbox.v3"
CAPABILITY_RELAY = "llm.openai-relay.v1"
SUPPORTED_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_ACE,
    CAPABILITY_APPLE_SPEECH,
    CAPABILITY_CHATTERBOX,
    CAPABILITY_RELAY,
)
CAPABILITY_CATEGORIES: dict[str, str] = {
    CAPABILITY_ACE: "music",
    CAPABILITY_APPLE_SPEECH: "tts",
    CAPABILITY_CHATTERBOX: "tts",
    CAPABILITY_RELAY: "llm",
}

POLL_MAX_SECONDS = 25
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30
PRESENCE_SECONDS = 20
CONTROL_MAX_BYTES = 262144
PAYLOAD_MAX_BYTES = 2097152
RESULT_MAX_BYTES = 2097152
LEASE_RESPONSE_MAX_BYTES = 3 * 1024 * 1024
ACE_DECODED_PAYLOAD_MAX_BYTES = 65536
METADATA_PART_MAX_BYTES = 8192
RESULT_METADATA_MAX_BYTES = 65536
ENROLLMENT_LIFETIME = timedelta(minutes=30)
PRESENCE_OFFLINE_SECONDS = 120
WORKER_NAME_MAX_SCALARS = 120
MAX_ENROLL_CAPABILITIES = 32

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
SERVICE_KIND_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
SOFTWARE_VERSION_RE = re.compile(r"^[ -~]{1,64}$")
BASE64_RE = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    ENROLLMENT_INVALID = "enrollment_invalid"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    WORKER_BUSY = "worker_busy"
    CLIENT_ALREADY_ENROLLED = "client_already_enrolled"
    LEASE_LOST = "lease_lost"
    RESULT_CONFLICT = "result_conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.ENROLLMENT_INVALID: 401,
    ErrorCode.PROTOCOL_UNSUPPORTED: 400,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNSUPPORTED_CAPABILITY: 400,
    ErrorCode.WORKER_BUSY: 409,
    ErrorCode.CLIENT_ALREADY_ENROLLED: 409,
    ErrorCode.LEASE_LOST: 409,
    ErrorCode.RESULT_CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 503,
}


class FailureCode(StrEnum):
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INVALID_PAYLOAD = "invalid_payload"
    SETUP_REQUIRED = "setup_required"
    EXECUTION_FAILED = "execution_failed"
    RELAY_UNREACHABLE = "relay_unreachable"
    RELAY_AUTH = "relay_auth"
    RELAY_INVALID_RESPONSE = "relay_invalid_response"
    RELAY_MODEL_UNKNOWN = "relay_model_unknown"


FAILURE_CODES: frozenset[str] = frozenset(code.value for code in FailureCode)


class PresenceState(StrEnum):
    READY = "ready"
    BUSY = "busy"
    PAUSED = "paused"
    SETUP_REQUIRED = "setup_required"
    ERROR = "error"


RESOURCE_REASONS: frozenset[str] = frozenset(
    {
        "slot_busy",
        "memory_pressure",
        "insufficient_memory",
        "storage_unavailable",
        "local_service_unreachable",
    }
)
PRESENCE_REASONS: frozenset[str | None] = RESOURCE_REASONS | {"setup_missing", "user_paused", None}


class AilocalsError(Exception):
    """Protocol failure carrying its allowlisted code and HTTP status."""

    def __init__(self, code: ErrorCode | str, message: str) -> None:
        self.code = ErrorCode(code)
        self.message = message[:256]
        self.http_status = ERROR_STATUS[self.code]
        super().__init__(self.message)


def error_envelope(code: ErrorCode | str, message: str) -> dict[str, Any]:
    """Build the bounded safe error body."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "error": {"code": ErrorCode(code).value, "message": message[:256]},
    }


def new_worker_token() -> str:
    """Random 32-byte base64url credential (43 characters, no padding)."""

    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Hash a credential for durable storage; raw tokens are never stored."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as RFC 3339 UTC with millisecond precision."""

    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{normalized.microsecond // 1000:03d}Z"


def parse_timestamp(value: Any) -> datetime:
    """Parse the strict millisecond UTC timestamp form."""

    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise AilocalsError(
            ErrorCode.INVALID_REQUEST, "timestamp must be RFC 3339 UTC with milliseconds"
        )
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return parsed


def utc_now() -> datetime:
    return datetime.now(UTC)


def _reject_constant(value: str) -> float:
    raise AilocalsError(ErrorCode.INVALID_REQUEST, f"unsupported JSON constant {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "duplicate object key")
        result[key] = value
    return result


def parse_json(data: bytes | str) -> Any:
    """Strict JSON parse: UTF-8, no duplicate keys, no NaN/Infinity, no trailing."""

    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError as exc:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "body must be UTF-8 JSON") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "body must be valid JSON") from exc


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "body must be a JSON object")
    return value


def require_str(
    container: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 1,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, str):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} must be a string")
    length = len(value)
    if not minimum <= length <= maximum:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} length is out of bounds")
    if pattern is not None and not pattern.fullmatch(value):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} format is invalid")
    return value


def require_int(container: Mapping[str, Any], key: str, *, minimum: int, maximum: int) -> int:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} is out of range")
    return value


def require_bool(container: Mapping[str, Any], key: str) -> bool:
    value = container.get(key)
    if not isinstance(value, bool):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{key} must be a boolean")
    return value


def require_protocol_version(container: Mapping[str, Any]) -> None:
    value = container.get("protocol_version")
    if value != PROTOCOL_VERSION:
        raise AilocalsError(ErrorCode.PROTOCOL_UNSUPPORTED, "unsupported protocol version")


@dataclass(frozen=True, slots=True)
class AceCapabilityParameters:
    worker_schema: int
    model_bundle_revision: str
    manifest_sha256: str
    accelerator: str
    formats: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TtsCapabilityParameters:
    engine: str
    languages: tuple[str, ...]
    unit_kinds: tuple[str, ...]
    max_bytes: int
    max_duration_ms: int


@dataclass(frozen=True, slots=True)
class RelayCapabilityParameters:
    max_completion_bytes: int
    operations: tuple[str, ...]


CapabilityParameters = AceCapabilityParameters | TtsCapabilityParameters | RelayCapabilityParameters


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    id: str
    category: str
    parameters: CapabilityParameters


_TTS_UNIT_KINDS = frozenset({"word", "phrase", "sentence", "*"})
_RELAY_OPERATIONS = frozenset({"chat_completion", "list_models"})


def decode_capability_parameters(raw: Any) -> CapabilityParameters:
    if not isinstance(raw, dict):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters must be an object")
    engine = raw.get("engine")
    if engine in {"avspeech", "chatterbox"}:
        languages = raw.get("languages")
        unit_kinds = raw.get("unit_kinds")
        if not isinstance(languages, list) or not 1 <= len(languages) <= 32:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "languages must hold 1..32 entries")
        if any(
            isinstance(item, bool) or not isinstance(item, str) or not item for item in languages
        ):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "languages entries must be strings")
        if not isinstance(unit_kinds, list) or not 1 <= len(unit_kinds) <= 8:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "unit_kinds must hold 1..8 entries")
        if any(item not in _TTS_UNIT_KINDS for item in unit_kinds):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "unit_kinds entries are invalid")
        return TtsCapabilityParameters(
            engine=str(engine),
            languages=tuple(languages),
            unit_kinds=tuple(unit_kinds),
            max_bytes=require_int(raw, "max_bytes", minimum=0, maximum=2**31 - 1),
            max_duration_ms=require_int(raw, "max_duration_ms", minimum=0, maximum=2**31 - 1),
        )
    if set(raw) == {"max_completion_bytes", "operations"}:
        operations = raw.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 2:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "operations must hold 1..2 entries")
        if any(item not in _RELAY_OPERATIONS for item in operations):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "operations entries are invalid")
        return RelayCapabilityParameters(
            max_completion_bytes=require_int(
                raw, "max_completion_bytes", minimum=1, maximum=PAYLOAD_MAX_BYTES
            ),
            operations=tuple(operations),
        )
    if {"worker_schema", "model_bundle_revision", "manifest_sha256", "accelerator", "formats"} <= (
        set(raw)
    ):
        formats = raw.get("formats")
        if not isinstance(formats, list) or not 1 <= len(formats) <= 3:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "formats must hold 1..3 entries")
        if any(item not in {"mp3", "flac", "wav"} for item in formats):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "formats entries are invalid")
        if raw.get("accelerator") != "mps":
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "accelerator must be mps")
        if raw.get("worker_schema") != 2:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "worker_schema must be 2")
        manifest = require_str(raw, "manifest_sha256", maximum=64, pattern=SHA256_RE)
        return AceCapabilityParameters(
            worker_schema=2,
            model_bundle_revision=require_str(raw, "model_bundle_revision", maximum=128),
            manifest_sha256=manifest,
            accelerator="mps",
            formats=tuple(formats),
        )
    raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters are unrecognized")


def decode_capability_entry(raw: Any) -> CapabilityEntry:
    if not isinstance(raw, dict):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability entry must be an object")
    capability_id = require_str(raw, "id", maximum=128)
    if capability_id not in SUPPORTED_CAPABILITIES:
        raise AilocalsError(ErrorCode.UNSUPPORTED_CAPABILITY, "capability is not supported")
    if set(raw) != {"id", "category", "parameters"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability entry fields are invalid")
    category = require_str(raw, "category", maximum=16)
    if category != CAPABILITY_CATEGORIES[capability_id]:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability category does not match")
    parameters = decode_capability_parameters(raw.get("parameters"))
    _validate_parameters_match_id(capability_id, parameters)
    return CapabilityEntry(id=capability_id, category=category, parameters=parameters)


def _validate_parameters_match_id(capability_id: str, parameters: CapabilityParameters) -> None:
    if capability_id == CAPABILITY_ACE:
        if not isinstance(parameters, AceCapabilityParameters):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters do not match")
        return
    if capability_id in {CAPABILITY_APPLE_SPEECH, CAPABILITY_CHATTERBOX}:
        if not isinstance(parameters, TtsCapabilityParameters):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters do not match")
        expected_engine = "avspeech" if capability_id == CAPABILITY_APPLE_SPEECH else "chatterbox"
        if parameters.engine != expected_engine:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters do not match")
        return
    if capability_id == CAPABILITY_RELAY:
        if not isinstance(parameters, RelayCapabilityParameters):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability parameters do not match")
        return
    raise AilocalsError(ErrorCode.UNSUPPORTED_CAPABILITY, "capability is not supported")


def decode_enroll_request(payload: Any) -> tuple[str, str, tuple[CapabilityEntry, ...]]:
    """Decode the enroll body into (worker_name, software_version, capabilities)."""

    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "worker_name", "software_version", "capabilities"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "enroll fields are invalid")
    worker_name = require_str(body, "worker_name", maximum=WORKER_NAME_MAX_SCALARS)
    software_version = require_str(
        body, "software_version", maximum=64, pattern=SOFTWARE_VERSION_RE
    )
    raw_capabilities = body.get("capabilities")
    if not isinstance(raw_capabilities, list) or not 1 <= len(raw_capabilities) <= (
        MAX_ENROLL_CAPABILITIES
    ):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capabilities must hold 1..32 entries")
    entries = tuple(decode_capability_entry(item) for item in raw_capabilities)
    identifiers = {entry.id for entry in entries}
    if len(identifiers) != len(entries):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capability entries must be unique")
    return worker_name, software_version, entries


@dataclass(frozen=True, slots=True)
class PresenceEntry:
    id: str
    state: PresenceState
    accepting: bool
    active_jobs: int
    reason: str | None


def decode_presence_request(payload: Any) -> tuple[PresenceEntry, ...]:
    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "capabilities"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "presence fields are invalid")
    raw_entries = body.get("capabilities")
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ENROLL_CAPABILITIES:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "capabilities must hold at most 32 entries")
    entries = tuple(decode_presence_entry(item) for item in raw_entries)
    identifiers = {entry.id for entry in entries}
    if len(identifiers) != len(entries):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "presence entries must be unique")
    return entries


def decode_presence_entry(raw: Any) -> PresenceEntry:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "state",
        "accepting",
        "active_jobs",
        "reason",
    }:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "presence entry fields are invalid")
    capability_id = require_str(raw, "id", maximum=128)
    state_raw = require_str(raw, "state", maximum=32)
    try:
        state = PresenceState(state_raw)
    except ValueError as exc:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "presence state is invalid") from exc
    accepting = require_bool(raw, "accepting")
    active_jobs = require_int(raw, "active_jobs", minimum=0, maximum=1)
    reason_raw = raw.get("reason")
    if reason_raw is not None:
        require_str(raw, "reason", maximum=64)
    if reason_raw is not None and reason_raw not in PRESENCE_REASONS:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "presence reason is invalid")
    reason = str(reason_raw) if reason_raw is not None else None
    validate_presence_matrix(state, accepting, active_jobs, reason)
    return PresenceEntry(
        id=capability_id, state=state, accepting=accepting, active_jobs=active_jobs, reason=reason
    )


def validate_presence_matrix(
    state: PresenceState, accepting: bool, active_jobs: int, reason: str | None
) -> None:
    """Enforce the documented state/accepting/reason combinations."""

    if state in {PresenceState.PAUSED, PresenceState.SETUP_REQUIRED, PresenceState.ERROR}:
        if accepting:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "state must not accept work")
    if state is PresenceState.READY:
        if not accepting or active_jobs != 0 or reason is not None:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "ready state is inconsistent")
    if state is PresenceState.BUSY:
        if active_jobs == 1:
            if reason is not None:
                raise AilocalsError(ErrorCode.INVALID_REQUEST, "busy running reason must be null")
        else:
            if accepting:
                raise AilocalsError(ErrorCode.INVALID_REQUEST, "busy waiting must not accept work")
            if reason not in RESOURCE_REASONS:
                raise AilocalsError(
                    ErrorCode.INVALID_REQUEST, "busy waiting requires a resource reason"
                )


@dataclass(frozen=True, slots=True)
class LeaseRequestData:
    capability_id: str
    wait_seconds: int


def decode_lease_request(payload: Any) -> LeaseRequestData:
    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "capability_id", "wait_seconds"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "lease fields are invalid")
    capability_id = require_str(body, "capability_id", maximum=128)
    if capability_id not in SUPPORTED_CAPABILITIES:
        raise AilocalsError(ErrorCode.UNSUPPORTED_CAPABILITY, "capability is not supported")
    wait_seconds = require_int(body, "wait_seconds", minimum=0, maximum=POLL_MAX_SECONDS)
    return LeaseRequestData(capability_id=capability_id, wait_seconds=wait_seconds)


@dataclass(frozen=True, slots=True)
class HeartbeatRequestData:
    attempt: int
    progress_percent: int


def decode_heartbeat_request(payload: Any) -> HeartbeatRequestData:
    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "attempt", "progress_percent"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "heartbeat fields are invalid")
    return HeartbeatRequestData(
        attempt=require_int(body, "attempt", minimum=1, maximum=2**31 - 1),
        progress_percent=require_int(body, "progress_percent", minimum=0, maximum=100),
    )


@dataclass(frozen=True, slots=True)
class FailRequestData:
    attempt: int
    code: str
    retryable: bool


def decode_fail_request(payload: Any) -> FailRequestData:
    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "attempt", "code", "retryable"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "fail fields are invalid")
    code = require_str(body, "code", maximum=64)
    if code not in FAILURE_CODES:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "failure code is not allowlisted")
    return FailRequestData(
        attempt=require_int(body, "attempt", minimum=1, maximum=2**31 - 1),
        code=code,
        retryable=require_bool(body, "retryable"),
    )


@dataclass(frozen=True, slots=True)
class CompleteMetadataData:
    attempt: int
    result_sha256: str


def decode_complete_metadata(payload: Any) -> CompleteMetadataData:
    body = require_object(payload)
    require_protocol_version(body)
    if set(body) != {"protocol_version", "attempt", "result_sha256"}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "completion metadata fields are invalid")
    return CompleteMetadataData(
        attempt=require_int(body, "attempt", minimum=1, maximum=2**31 - 1),
        result_sha256=require_str(body, "result_sha256", maximum=64, pattern=SHA256_RE),
    )


def encode_lease_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Serialize domain payload bytes and return (base64, sha256)."""

    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload is not serializable") from exc
    if len(encoded) > PAYLOAD_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "payload exceeds the byte bound")
    return (
        base64.b64encode(encoded).decode("ascii"),
        hashlib.sha256(encoded).hexdigest(),
    )


def decode_lease_payload_fields(body: Mapping[str, Any]) -> bytes:
    """Validate and decode the lease envelope payload fields to raw bytes."""

    encoding = body.get("payload_encoding")
    if encoding != "base64":
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload_encoding must be base64")
    raw_payload = body.get("payload_base64")
    if not isinstance(raw_payload, str) or not BASE64_RE.fullmatch(raw_payload):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload_base64 is not canonical base64")
    if len(raw_payload) > LEASE_RESPONSE_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "encoded payload exceeds 3 MiB")
    try:
        decoded = base64.b64decode(raw_payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload_base64 is malformed") from exc
    if not decoded:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload must not be empty")
    if len(decoded) > PAYLOAD_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "payload exceeds the byte bound")
    digest = require_str(body, "payload_sha256", maximum=64, pattern=SHA256_RE)
    actual = hashlib.sha256(decoded).hexdigest()
    if actual != digest:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "payload hash does not match")
    return decoded


def build_lease_response(
    *,
    job_id: str,
    attempt: int,
    lease_token: str,
    lease_expires_at: datetime,
    deadline_at: datetime | None,
    capability_id: str,
    payload_base64: str,
    payload_sha256: str,
) -> dict[str, Any]:
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "attempt": attempt,
        "lease_token": lease_token,
        "lease_expires_at": format_timestamp(lease_expires_at),
        "deadline_at": format_timestamp(deadline_at) if deadline_at is not None else None,
        "capability_id": capability_id,
        "payload_encoding": "base64",
        "payload_base64": payload_base64,
        "payload_sha256": payload_sha256,
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > LEASE_RESPONSE_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "lease response exceeds 3 MiB")
    return body


def parse_identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "identifier is invalid")
    return value


def canonical_request_identity(payload: Mapping[str, Any]) -> str:
    """Semantic identity hash over the worker payload without transfer URLs."""

    trimmed = _strip_transfer_urls(payload)
    encoded = json.dumps(
        trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strip_transfer_urls(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "url":
                continue
            result[key] = _strip_transfer_urls(item)
        return result
    if isinstance(value, list):
        return [_strip_transfer_urls(item) for item in value]
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

"""HTTP transport for the ailocals.v1 universal worker protocol.

Routes enforce body bounds while streaming and never log payload bytes,
prompts, or credentials. Error bodies use the shared bounded envelope.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ace_service.ailocals import protocol
from ace_service.ailocals.protocol import (
    AilocalsError,
    CompleteMetadataData,
    ErrorCode,
)
from ace_service.ailocals.service import AilocalsWorkerService
from ace_service.config import ServiceSettings

CONTENT_TYPE_RE = re.compile(r"^\s*multipart/form-data\s*;\s*boundary=(?P<boundary>[^;]+)\s*$")
DISPOSITION_NAME_RE = re.compile(rb'name="(?P<name>[^"]*)"')
DISPOSITION_FILENAME_RE = re.compile(rb'filename="(?P<filename>[^"]*)"')
MULTIPART_OVERHEAD_BYTES = 65536

router = APIRouter(prefix="/" + protocol.ROUTE_NAMESPACE)


def build_info_response(settings: ServiceSettings) -> dict[str, Any]:
    """Public capability advertisement; no machine-specific values."""

    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "service_kind": "audioventura",
        "environment": settings.ailocals_environment,
        "supported_capabilities": [protocol.CAPABILITY_ACE],
        "limits": {
            "poll_max_seconds": protocol.POLL_MAX_SECONDS,
            "lease_seconds": protocol.LEASE_SECONDS,
            "heartbeat_seconds": protocol.HEARTBEAT_SECONDS,
            "presence_seconds": protocol.PRESENCE_SECONDS,
            "control_max_bytes": protocol.CONTROL_MAX_BYTES,
            "payload_max_bytes": protocol.PAYLOAD_MAX_BYTES,
            "result_max_bytes": protocol.RESULT_MAX_BYTES,
        },
    }


async def _read_bounded(request: Request, limit: int, *, what: str) -> bytes:
    length_header = request.headers.get("content-length")
    if length_header is not None:
        try:
            declared = int(length_header)
        except ValueError as exc:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{what} length is invalid") from exc
        if declared < 0:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{what} length is invalid")
        if declared > limit:
            raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, f"{what} exceeds the byte bound")
    chunks: list[bytes] = []
    total = 0
    stream: AsyncIterator[bytes] = request.stream()
    async for chunk in stream:
        total += len(chunk)
        if total > limit:
            raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, f"{what} exceeds the byte bound")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_control_json(request: Request) -> Any:
    body = await _read_bounded(request, protocol.CONTROL_MAX_BYTES, what="body")
    return protocol.parse_json(body)


def _header(request: Request, name: str) -> str:
    value = request.headers.get(name)
    if not value:
        raise AilocalsError(ErrorCode.UNAUTHORIZED, "credential header is required")
    return value


def _json_response(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


@router.get("/info")
async def info(request: Request) -> dict[str, Any]:
    settings: ServiceSettings = request.app.state.settings
    return build_info_response(settings)


@router.post("/enroll")
async def enroll(request: Request) -> JSONResponse:
    token = _header(request, protocol.ENROLLMENT_TOKEN_HEADER)
    worker_name, software_version, capabilities = protocol.decode_enroll_request(
        await _read_control_json(request)
    )
    service: AilocalsWorkerService = request.app.state.ailocals_service
    outcome = service.enroll(token, worker_name, software_version, capabilities)
    return _json_response(
        {
            "protocol_version": protocol.PROTOCOL_VERSION,
            "worker_id": outcome.worker_id,
            "worker_token": outcome.worker_token,
            "environment": outcome.environment,
        },
        status_code=201,
    )


@router.post("/presence")
async def presence(request: Request) -> dict[str, Any]:
    service: AilocalsWorkerService = request.app.state.ailocals_service
    worker = service.authenticate(_header(request, protocol.WORKER_TOKEN_HEADER))
    entries = protocol.decode_presence_request(await _read_control_json(request))
    server_time = service.presence(worker, entries)
    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "server_time": protocol.format_timestamp(server_time),
    }


@router.post("/lease")
async def lease(request: Request) -> Response:
    service: AilocalsWorkerService = request.app.state.ailocals_service
    worker = service.authenticate(_header(request, protocol.WORKER_TOKEN_HEADER))
    decoded = protocol.decode_lease_request(await _read_control_json(request))
    claimed = service.claim(worker, decoded)
    if claimed is None:
        return Response(status_code=204)
    body = protocol.build_lease_response(
        job_id=claimed.job_id,
        attempt=claimed.attempt,
        lease_token=claimed.lease_token,
        lease_expires_at=claimed.lease_expires_at,
        deadline_at=claimed.deadline_at,
        capability_id=claimed.capability_id,
        payload_base64=claimed.payload_base64,
        payload_sha256=claimed.payload_sha256,
    )
    return _json_response(body)


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat(job_id: str, request: Request) -> dict[str, Any]:
    service: AilocalsWorkerService = request.app.state.ailocals_service
    worker = service.authenticate(_header(request, protocol.WORKER_TOKEN_HEADER))
    _header(request, protocol.LEASE_TOKEN_HEADER)
    decoded = protocol.decode_heartbeat_request(await _read_control_json(request))
    outcome = service.heartbeat(
        worker,
        protocol.parse_identifier(job_id),
        request.headers.get(protocol.LEASE_TOKEN_HEADER, ""),
        decoded.attempt,
        decoded.progress_percent,
    )
    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "lease_expires_at": protocol.format_timestamp(outcome.lease_expires_at),
        "cancel_requested": outcome.cancel_requested,
    }


@router.post("/jobs/{job_id}/complete")
async def complete(job_id: str, request: Request) -> dict[str, Any]:
    service: AilocalsWorkerService = request.app.state.ailocals_service
    worker = service.authenticate(_header(request, protocol.WORKER_TOKEN_HEADER))
    lease_token = _header(request, protocol.LEASE_TOKEN_HEADER)
    total_limit = (
        protocol.METADATA_PART_MAX_BYTES + protocol.RESULT_MAX_BYTES + MULTIPART_OVERHEAD_BYTES
    )
    body = await _read_bounded(request, total_limit, what="completion")
    parts = parse_strict_multipart(body, request.headers.get("content-type", ""))
    metadata = _require_part(parts, "metadata")
    result = _require_part(parts, "result")
    if len(metadata) > protocol.METADATA_PART_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "metadata part exceeds the bound")
    if len(result) > protocol.RESULT_MAX_BYTES:
        raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "result part exceeds the bound")
    if "artifact" in parts:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "artifact parts are not accepted")
    decoded: CompleteMetadataData = protocol.decode_complete_metadata(protocol.parse_json(metadata))
    service.complete(
        worker,
        protocol.parse_identifier(job_id),
        lease_token,
        decoded,
        result,
    )
    return {"protocol_version": protocol.PROTOCOL_VERSION, "accepted": True}


@router.post("/jobs/{job_id}/fail")
async def fail(job_id: str, request: Request) -> dict[str, Any]:
    service: AilocalsWorkerService = request.app.state.ailocals_service
    worker = service.authenticate(_header(request, protocol.WORKER_TOKEN_HEADER))
    lease_token = _header(request, protocol.LEASE_TOKEN_HEADER)
    decoded = protocol.decode_fail_request(await _read_control_json(request))
    service.fail(
        worker,
        protocol.parse_identifier(job_id),
        lease_token,
        decoded.attempt,
        decoded.code,
        decoded.retryable,
    )
    return {"protocol_version": protocol.PROTOCOL_VERSION, "accepted": True}


# ----------------------------------------------------------------------
# Multipart
# ----------------------------------------------------------------------


def _require_part(parts: dict[str, bytes], name: str) -> bytes:
    part = parts.get(name)
    if part is None:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, f"{name} part is required")
    return part


def parse_strict_multipart(data: bytes, content_type: str) -> dict[str, bytes]:
    """Parse exactly one body per named part; reject duplicates and gaps."""

    match = CONTENT_TYPE_RE.match(content_type)
    if match is None:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "content type must be multipart")
    boundary = match.group("boundary").strip().strip('"').encode("ascii", "replace")
    delimiter = b"--" + boundary
    segments = data.split(delimiter)
    if len(segments) < 2 or not segments[0].strip() == b"":
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart body is malformed")
    closing = segments[-1].strip()
    if closing not in {b"--", b""}:
        raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart body is not closed")
    parts: dict[str, bytes] = {}
    for segment in segments[1:-1]:
        if not segment.startswith(b"\r\n"):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart part is malformed")
        segment = segment[2:]
        header_split = segment.find(b"\r\n\r\n")
        if header_split < 0:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart part has no headers")
        raw_headers = segment[:header_split]
        part_body = segment[header_split + 4 :]
        if not part_body.endswith(b"\r\n"):
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart part is not terminated")
        part_body = part_body[:-2]
        name = _disposition_name(raw_headers)
        if name in parts:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "duplicate multipart part")
        parts[name] = part_body
    return parts


def _disposition_name(raw_headers: bytes) -> str:
    for line in raw_headers.split(b"\r\n"):
        if line.lower().startswith(b"content-disposition:"):
            name_match = DISPOSITION_NAME_RE.search(line)
            if name_match is None:
                raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart part has no name")
            return name_match.group("name").decode("utf-8")
    raise AilocalsError(ErrorCode.INVALID_REQUEST, "multipart part has no disposition")

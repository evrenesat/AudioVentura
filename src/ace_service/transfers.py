"""Isolated signed source-download and generated-output upload application."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory, initialize_database_for_settings
from ace_service.models import (
    Output,
    TransferCapability,
    TransferDirection,
    TransferStatus,
    utc_now,
)
from ace_service.repository import (
    consume_transfer,
    create_output,
    get_output_by_path,
    get_transfer_by_token,
    issue_transfer_capability,
)
from ace_service.schemas import normalize_extension, normalize_relative_path, resolve_relative_path

_PART_SUFFIX = ".part"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_OUTPUT_INDEX_RE = re.compile(r"(?:variation|cover)[-_](\d+)", re.IGNORECASE)
_MIME_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav"}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedCapabilityURL:
    """Newly issued URL; the plaintext token is present only in this URL."""

    capability: TransferCapability
    url: str


def issue_transfer_url(
    session: Session,
    settings: ServiceSettings,
    *,
    job_id: str,
    direction: TransferDirection,
    expected_relative_path: str,
    expected_extension: str,
    max_bytes: int,
    expires_at: datetime | None = None,
    token: str | None = None,
) -> IssuedCapabilityURL:
    """Issue a hashed capability and return its one newly usable URL."""

    extension = normalize_extension(expected_extension)
    if direction is TransferDirection.SOURCE_DOWNLOAD and extension != ".mp3":
        raise ValueError("source capabilities must target canonical MP3 files")
    expiry = expires_at or (utc_now() + timedelta(seconds=settings.transfer_token_ttl_seconds))
    issued = issue_transfer_capability(
        session,
        job_id=job_id,
        direction=direction,
        expected_relative_path=expected_relative_path,
        expected_extension=extension,
        max_bytes=min(max_bytes, _global_limit(settings, direction)),
        expires_at=expiry,
        token=token,
    )
    route = "source" if direction is TransferDirection.SOURCE_DOWNLOAD else "output"
    url = f"{settings.transfer_public_base_url}/transfer/v1/{route}/{issued.token}"
    return IssuedCapabilityURL(capability=issued.capability, url=url)


def issue_capability_url(*args: Any, **kwargs: Any) -> IssuedCapabilityURL:
    """Descriptive alias for callers creating Runpod capability URLs."""

    return issue_transfer_url(*args, **kwargs)


def create_transfer_app(
    settings: ServiceSettings,
    *,
    session_factory: SessionFactory | None = None,
) -> FastAPI:
    """Create an app with exactly the two public transfer route shapes."""

    settings.ensure_data_layout()
    from ace_service.logging_config import configure_logging

    configure_logging(settings, component="transfer")
    engine = None
    if session_factory is None:
        engine = initialize_database_for_settings(settings)
        session_factory = _session_factory_from_engine(engine)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.capability_locks = {}
    app.state.capability_locks_guard = asyncio.Lock()

    @app.get("/transfer/v1/source/{token}", include_in_schema=False)
    async def download_source(token: str, request: Request) -> Response:
        del request
        settings = app.state.settings
        with app.state.session_factory() as session:
            capability = _active_capability(
                session,
                token,
                TransferDirection.SOURCE_DOWNLOAD,
                consume=False,
            )
            candidate = _capability_path(settings.paths.incoming, capability)
            _validate_file(
                candidate,
                settings.paths.incoming,
                capability,
                max_bytes=min(capability.max_bytes, settings.transfer_max_source_bytes),
            )
            session.commit()
        LOGGER.info(
            "job=%s stage=source_download bytes=%d",
            capability.job_id,
            candidate.stat().st_size,
            extra={"component": "transfer"},
        )
        return FileResponse(candidate, media_type="audio/mpeg", filename=candidate.name)

    @app.put("/transfer/v1/output/{token}", include_in_schema=False)
    async def upload_output(token: str, request: Request) -> JSONResponse:
        settings = app.state.settings
        with app.state.session_factory() as session:
            capability = _active_capability(
                session,
                token,
                TransferDirection.OUTPUT_UPLOAD,
                consume=True,
            )
            final_path = _capability_path(settings.paths.outputs, capability)
            _validate_target_path(final_path, settings.paths.outputs, capability)
            max_bytes = min(capability.max_bytes, settings.transfer_max_output_bytes)
            capability_id = capability.id

        lock = await _capability_lock(app, capability_id)
        async with lock:
            return await _receive_output(
                app.state.session_factory,
                settings,
                capability_id,
                token,
                final_path,
                max_bytes,
                request,
            )

    return app


def _session_factory_from_engine(engine: Any) -> SessionFactory:
    from ace_service.db import create_session_factory

    return create_session_factory(engine)


async def _capability_lock(app: FastAPI, capability_id: str) -> asyncio.Lock:
    async with app.state.capability_locks_guard:
        return cast(
            asyncio.Lock, app.state.capability_locks.setdefault(capability_id, asyncio.Lock())
        )


def _active_capability(
    session: Session,
    token: str,
    direction: TransferDirection,
    *,
    consume: bool,
) -> TransferCapability:
    capability = get_transfer_by_token(session, token)
    if capability is None or capability.direction is not direction:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability not found")
    current_time = utc_now()
    if (
        capability.status in (TransferStatus.ISSUED, TransferStatus.CONSUMED)
        and capability.expires_at <= current_time
    ):
        if capability.status is TransferStatus.ISSUED:
            capability.status = TransferStatus.EXPIRED
            session.commit()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability expired")
    if capability.status is TransferStatus.ISSUED:
        return capability
    if not consume and capability.status is TransferStatus.CONSUMED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability not found")
    if consume and capability.status is TransferStatus.CONSUMED:
        return capability
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability not found")


def _global_limit(settings: ServiceSettings, direction: TransferDirection) -> int:
    return (
        settings.transfer_max_source_bytes
        if direction is TransferDirection.SOURCE_DOWNLOAD
        else settings.transfer_max_output_bytes
    )


def _capability_path(root: Path, capability: TransferCapability) -> Path:
    relative_path = normalize_relative_path(capability.expected_relative_path)
    root_name = root.name
    parts = PurePosixPath(relative_path).parts
    if parts and parts[0] == root_name:
        relative_path = "/".join(parts[1:])
        if not relative_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability")
    try:
        return resolve_relative_path(root, relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability"
        ) from exc


def _validate_target_path(candidate: Path, root: Path, capability: TransferCapability) -> None:
    expected_extension = normalize_extension(capability.expected_extension)
    if candidate.suffix.lower() != expected_extension:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability")
    if expected_extension not in _MIME_TYPES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability")
    if _has_symlink_component(root, candidate):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability")
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _has_symlink_component(root, candidate):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid capability")


def _validate_file(
    candidate: Path,
    root: Path,
    capability: TransferCapability,
    *,
    max_bytes: int,
) -> int:
    _validate_target_path(candidate, root, capability)
    try:
        file_stat = candidate.lstat()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source not found"
        ) from exc
    if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    if file_stat.st_size <= 0 or file_stat.st_size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="source too large"
        )
    return file_stat.st_size


async def _receive_output(
    session_factory: SessionFactory,
    settings: ServiceSettings,
    capability_id: str,
    token: str,
    final_path: Path,
    max_bytes: int,
    request: Request,
) -> JSONResponse:
    content_length = _content_length(request)
    if content_length is not None and (content_length <= 0 or content_length > max_bytes):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="output exceeds capability byte limit",
        )
    part_path = final_path.with_name(f"{final_path.name}{_PART_SUFFIX}")
    _unlink_quietly(part_path)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with part_path.open("wb") as output_file:
            async for chunk in request.stream():
                if not isinstance(chunk, bytes):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="invalid body"
                    )
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="output exceeds capability byte limit",
                    )
                digest.update(chunk)
                output_file.write(chunk)
            if byte_count <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="output is empty"
                )
            if content_length is not None and content_length != byte_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="body length mismatch"
                )
            output_file.flush()
            os.fsync(output_file.fileno())
        actual_sha256 = digest.hexdigest()
        _verify_optional_digest(request, actual_sha256)
        _finalize_output(
            session_factory,
            settings,
            capability_id,
            token,
            final_path,
            part_path,
            byte_count,
            actual_sha256,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "accepted", "bytes": byte_count, "sha256": actual_sha256},
        )
    except HTTPException:
        _unlink_quietly(part_path)
        raise
    except (OSError, ValueError) as exc:
        _unlink_quietly(part_path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="output rejected"
        ) from exc
    except Exception as exc:
        _unlink_quietly(part_path)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="output rejected"
        ) from exc


def _content_length(request: Request) -> int | None:
    raw_value = request.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content length"
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content length"
        )
    return value


def _verify_optional_digest(request: Request, actual_sha256: str) -> None:
    supplied = request.headers.get("x-ace-output-sha256")
    if supplied is not None and (
        not _SHA256_RE.fullmatch(supplied) or supplied.lower() != actual_sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="output checksum mismatch"
        )


def _finalize_output(
    session_factory: SessionFactory,
    settings: ServiceSettings,
    capability_id: str,
    token: str,
    final_path: Path,
    part_path: Path,
    byte_count: int,
    sha256: str,
) -> None:
    moved = False
    with session_factory() as session:
        capability = session.get(TransferCapability, capability_id)
        if capability is None or capability.token_sha256 != _token_hash(token):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="capability not found"
            )
        existing_output = get_output_by_path(
            session, job_id=capability.job_id, relative_path=capability.expected_relative_path
        )
        if existing_output is not None:
            if _output_matches(existing_output, byte_count, sha256, final_path):
                if capability.status is TransferStatus.ISSUED:
                    consume_transfer(session, capability.id)
                    session.commit()
                else:
                    session.rollback()
                _unlink_quietly(part_path)
                return
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="conflicting output retry"
            )
        if capability.status is not TransferStatus.ISSUED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="capability already consumed"
            )
        if capability.expires_at <= utc_now():
            capability.status = TransferStatus.EXPIRED
            session.commit()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="capability expired")
        if final_path.exists() or final_path.is_symlink():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="output path already exists"
            )
        _validate_target_path(final_path, settings.paths.outputs, capability)
        try:
            part_path.replace(final_path)
            moved = True
            _fsync_directory(final_path.parent)
            variation_index, result_index = _output_indexes(session, capability.job_id, final_path)
            create_output(
                session,
                job_id=capability.job_id,
                variation_index=variation_index,
                result_index=result_index,
                relative_path=capability.expected_relative_path,
                mime_type=_MIME_TYPES[normalize_extension(capability.expected_extension)],
                byte_size=byte_count,
                sha256=sha256,
            )
            consume_transfer(session, capability.id)
            session.commit()
            LOGGER.info(
                "job=%s stage=output_upload bytes=%d sha256=%s",
                capability.job_id,
                byte_count,
                sha256,
                extra={"component": "transfer"},
            )
        except HTTPException:
            session.rollback()
            if moved:
                _unlink_quietly(final_path)
            _unlink_quietly(part_path)
            raise
        except Exception:
            session.rollback()
            if moved:
                _unlink_quietly(final_path)
            _unlink_quietly(part_path)
            raise


def _output_matches(output: Output, byte_count: int, sha256: str, final_path: Path) -> bool:
    if output.byte_size != byte_count or output.sha256 != sha256:
        return False
    try:
        return _file_sha256(final_path) == sha256 and final_path.stat().st_size == byte_count
    except OSError:
        return False


def _output_indexes(session: Session, job_id: str, final_path: Path) -> tuple[int, int]:
    match = _OUTPUT_INDEX_RE.search(final_path.stem)
    variation_index = int(match.group(1)) if match else 1
    result_index = 0
    used = {
        (output.variation_index, output.result_index)
        for output in session.query(Output).filter(Output.job_id == job_id)
    }
    while (variation_index, result_index) in used:
        variation_index += 1
    return variation_index, result_index


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token_hash(token: str) -> str:
    from ace_service.repository import hash_transfer_token

    return hash_transfer_token(token)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

"""Private FastAPI endpoint for home-server cover source preparation."""

from __future__ import annotations

import asyncio
import hmac
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import HomeIngestSettings
from .media import IngestError, PreparedSource, cleanup_job_directory, prepare_source
from .uploader import SFTPUploader


class PrepareCoverRequest(BaseModel):
    """Bounded controller request accepted by the private endpoint."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    url: str = Field(min_length=1, max_length=2048)
    max_duration_seconds: int = Field(default=600, ge=1, le=600)
    max_source_bytes: int = Field(default=268_435_456, ge=1, le=1_073_741_824)


class PrepareCoverResponse(BaseModel):
    """Metadata returned only after SFTP upload has completed."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    video_id: str
    title: str
    canonical_url: str
    duration_seconds: float
    prepared_format: str = "mp3"
    prepared_bytes: int
    prepared_sha256: str


class SourceUploader(Protocol):
    def upload(self, local_path: Path, job_id: str | UUID) -> str:
        """Upload a canonical source to the deterministic incoming path."""


@dataclass(slots=True)
class HomeIngestService:
    settings: HomeIngestSettings
    uploader: SourceUploader
    prepare_source_fn: Callable[..., Awaitable[PreparedSource]] = prepare_source

    async def prepare(self, request: PrepareCoverRequest) -> PrepareCoverResponse:
        if request.max_duration_seconds > self.settings.max_duration_seconds:
            raise IngestError(
                "youtube_duration_exceeded", "the requested duration limit is too high"
            )
        if request.max_source_bytes > self.settings.max_source_bytes:
            raise IngestError("source_size_exceeded", "the requested byte limit is too high")
        job_id = str(request.job_id)
        job_directory = self.settings.paths.job_temporary(job_id)
        self.settings.ensure_data_layout()
        prune_expired_debug_artifacts(self.settings)
        try:
            prepared = await self.prepare_source_fn(
                request.url,
                job_directory,
                max_duration_seconds=request.max_duration_seconds,
                max_source_bytes=request.max_source_bytes,
                command_timeout_seconds=self.settings.command_timeout_seconds,
            )
            if not isinstance(prepared, PreparedSource):
                raise IngestError(
                    "prepared_source_invalid", "the source preparation result was invalid"
                )
            await asyncio.to_thread(self.uploader.upload, prepared.path, job_id)
            return PrepareCoverResponse(
                job_id=request.job_id,
                video_id=prepared.metadata.video_id,
                title=prepared.metadata.title,
                canonical_url=prepared.metadata.canonical_url,
                duration_seconds=prepared.metadata.duration_seconds,
                prepared_bytes=prepared.byte_size,
                prepared_sha256=prepared.sha256,
            )
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError(
                "sftp_upload_failed", "the prepared source could not be uploaded"
            ) from exc
        finally:
            cleanup_job_directory(job_directory, retain=self.settings.retain_debug_artifacts)


def prune_expired_debug_artifacts(settings: HomeIngestSettings) -> None:
    """Bound explicit debug retention by pruning old per-job directories."""

    temporary_root = settings.paths.temporary
    if not temporary_root.is_dir():
        return
    cutoff = time.time() - settings.debug_retention_seconds
    for entry in temporary_root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError:
            continue


def _http_status(code: str) -> int:
    if code == "youtube_url_rejected":
        return 400
    if code in {"youtube_duration_exceeded", "source_size_exceeded"}:
        return 413
    if code in {
        "youtube_metadata_failed",
        "youtube_download_failed",
        "ffprobe_failed",
        "ffmpeg_failed",
    }:
        return 502
    if code == "youtube_blocked_or_login_required":
        return 424
    if code == "sftp_upload_failed":
        return 502
    return 500


def create_app(
    settings: HomeIngestSettings | None = None, *, uploader: SourceUploader | None = None
) -> FastAPI:
    """Create the private API without exposing docs or a catch-all route."""

    resolved_settings = settings or HomeIngestSettings()
    resolved_settings.ensure_data_layout()
    service = HomeIngestService(resolved_settings, uploader or SFTPUploader(resolved_settings))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.post(
        "/v1/prepare-youtube-cover",
        response_model=PrepareCoverResponse,
        response_model_exclude_none=True,
    )
    async def prepare_youtube_cover(
        payload: PrepareCoverRequest, authorization: str | None = Header(default=None)
    ) -> PrepareCoverResponse:
        if not _valid_bearer(authorization, resolved_settings.token):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return await service.prepare(payload)
        except IngestError as exc:
            raise HTTPException(
                status_code=_http_status(exc.code),
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    return app


def _valid_bearer(header: str | None, expected: str) -> bool:
    if header is None:
        return False
    scheme, separator, value = header.partition(" ")
    return bool(separator and scheme.lower() == "bearer" and hmac.compare_digest(value, expected))

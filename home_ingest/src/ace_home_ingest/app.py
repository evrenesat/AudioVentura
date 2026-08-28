"""Private FastAPI endpoint for home-server cover source preparation."""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import re
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cleanup import prune_orphan_job_directories
from .config import HomeIngestSettings
from .logging_config import configure_logging
from .media import (
    IngestError,
    PreparedMedia,
    PreparedSource,
    cleanup_job_directory,
    prepare_clip_local,
    prepare_local_source,
    prepare_source,
)
from .transfer import BoundedTransferClient
from .uploader import SFTPUploader

LOGGER = logging.getLogger(__name__)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PrepareCoverRequest(BaseModel):
    """Bounded controller request accepted by the private endpoint."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    url: str = Field(min_length=1, max_length=2048)
    max_duration_seconds: int = Field(default=600, ge=1, le=600)
    max_source_bytes: int = Field(default=536_870_912, ge=1, le=1_073_741_824)


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


class PrepareSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_id: UUID
    origin: str = Field(pattern="^(youtube|upload)$")
    display_title: str = Field(min_length=1, max_length=300)
    youtube_url: str | None = Field(default=None, max_length=2048)
    raw_download_url: str | None = Field(default=None, max_length=4096)
    raw_byte_size: int | None = Field(default=None, gt=0, le=1_073_741_824)
    raw_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    canonical_upload_url: str = Field(min_length=1, max_length=4096)
    max_raw_bytes: int = Field(default=536_870_912, gt=0, le=1_073_741_824)
    max_canonical_bytes: int = Field(default=536_870_912, gt=0, le=1_073_741_824)

    @model_validator(mode="after")
    def validate_source_shape(self) -> PrepareSourceRequest:
        if self.origin == "youtube":
            if not self.youtube_url or self.raw_download_url is not None:
                raise ValueError("YouTube source requests require only youtube_url")
            if self.raw_byte_size is not None or self.raw_sha256 is not None:
                raise ValueError("YouTube source requests must not include raw upload metadata")
        else:
            if (
                not self.raw_download_url
                or self.youtube_url is not None
                or self.raw_byte_size is None
                or self.raw_sha256 is None
            ):
                raise ValueError("upload source requests require raw transfer metadata")
        if self.raw_sha256 is not None and not _SHA256_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a SHA-256 checksum")
        return self


class PrepareSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_id: UUID
    title: str
    duration_seconds: float
    prepared_format: str = "mp3"
    prepared_bytes: int
    prepared_sha256: str


class PrepareClipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    source_download_url: str = Field(min_length=1, max_length=4096)
    source_byte_size: int = Field(gt=0, le=1_073_741_824)
    source_sha256: str = Field(min_length=64, max_length=64)
    clip_upload_url: str = Field(min_length=1, max_length=4096)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    max_source_bytes: int = Field(default=536_870_912, gt=0, le=1_073_741_824)

    @model_validator(mode="after")
    def validate_clip_shape(self) -> PrepareClipRequest:
        if (
            not math.isfinite(self.start_seconds)
            or not math.isfinite(self.end_seconds)
            or self.end_seconds <= self.start_seconds
        ):
            raise ValueError("clip range must contain finite increasing bounds")
        if not _SHA256_RE.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a SHA-256 checksum")
        return self


class PrepareClipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    prepared_format: str = "mp3"
    prepared_bytes: int
    prepared_sha256: str
    duration_seconds: float


class PrepareDerivativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    derivative_task_id: UUID
    source_download_url: str = Field(min_length=1, max_length=4096)
    source_byte_size: int = Field(gt=0, le=1_073_741_824)
    source_sha256: str = Field(min_length=64, max_length=64)
    source_format: str = Field(pattern="^(flac|wav)$")
    derivative_upload_url: str = Field(min_length=1, max_length=4096)
    max_source_bytes: int = Field(default=536_870_912, gt=0, le=1_073_741_824)
    max_canonical_bytes: int = Field(default=536_870_912, gt=0, le=1_073_741_824)
    expected_duration_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_derivative_shape(self) -> PrepareDerivativeRequest:
        if not _SHA256_RE.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a SHA-256 checksum")
        if self.expected_duration_seconds is not None and not math.isfinite(
            self.expected_duration_seconds
        ):
            raise ValueError("expected duration must be finite")
        return self


class PrepareDerivativeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    derivative_task_id: UUID
    prepared_format: str = "mp3"
    prepared_bytes: int
    prepared_sha256: str
    duration_seconds: float


class SourceUploader(Protocol):
    def upload(self, local_path: Path, job_id: str | UUID) -> str:
        """Upload a canonical source to the deterministic incoming path."""


@dataclass(slots=True)
class HomeIngestService:
    settings: HomeIngestSettings
    uploader: SourceUploader
    prepare_source_fn: Callable[..., Awaitable[PreparedSource]] = prepare_source
    transfer_client: BoundedTransferClient | None = None

    def _transfer(self) -> BoundedTransferClient:
        if self.transfer_client is None:
            self.transfer_client = BoundedTransferClient(self.settings)
        return self.transfer_client

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
        started = time.monotonic()
        LOGGER.info(
            "job=%s stage=prepare component=home_ingest",
            job_id,
            extra={"component": "home_ingest"},
        )
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
            response = PrepareCoverResponse(
                job_id=request.job_id,
                video_id=prepared.metadata.video_id,
                title=prepared.metadata.title,
                canonical_url=prepared.metadata.canonical_url,
                duration_seconds=prepared.metadata.duration_seconds,
                prepared_bytes=prepared.byte_size,
                prepared_sha256=prepared.sha256,
            )
            LOGGER.info(
                "job=%s video_id=%s stage=upload completed bytes=%d duration_seconds=%.3f "
                "elapsed_ms=%d",
                job_id,
                prepared.metadata.video_id,
                prepared.byte_size,
                prepared.metadata.duration_seconds,
                int((time.monotonic() - started) * 1000),
                extra={"component": "home_ingest"},
            )
            return response
        except IngestError as exc:
            LOGGER.warning(
                "job=%s stage=prepare error_code=%s exception_class=%s elapsed_ms=%d",
                job_id,
                exc.code,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "home_ingest"},
            )
            raise
        except Exception as exc:
            LOGGER.warning(
                "job=%s stage=upload error_code=sftp_upload_failed exception_class=%s "
                "elapsed_ms=%d",
                job_id,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
                extra={"component": "home_ingest"},
            )
            raise IngestError(
                "sftp_upload_failed", "the prepared source could not be uploaded"
            ) from exc
        finally:
            cleanup_job_directory(job_directory, retain=self.settings.retain_debug_artifacts)

    async def prepare_source_v2(self, request: PrepareSourceRequest) -> PrepareSourceResponse:
        source_id = str(request.source_asset_id)
        work_directory = self.settings.paths.job_temporary(source_id)
        self.settings.ensure_data_layout()
        client = self._transfer()
        try:
            if request.max_canonical_bytes > self.settings.canonical_source_max_bytes:
                raise IngestError(
                    "canonical_source_size_exceeded", "the canonical source limit is too high"
                )
            if request.max_raw_bytes > self.settings.max_source_bytes:
                raise IngestError("source_size_exceeded", "the raw source limit is too high")
            if request.origin == "youtube":
                if not request.youtube_url or request.raw_download_url is not None:
                    raise IngestError(
                        "source_request_invalid", "the YouTube source request is incomplete"
                    )
                prepared = await self.prepare_source_fn(
                    request.youtube_url,
                    work_directory,
                    max_duration_seconds=None,
                    max_source_bytes=request.max_raw_bytes,
                    command_timeout_seconds=self.settings.command_timeout_seconds,
                    secure_input=True,
                )
                if not isinstance(prepared, PreparedSource):
                    raise IngestError(
                        "prepared_source_invalid", "the source preparation result was invalid"
                    )
                result = PreparedMedia(
                    prepared.metadata.title,
                    prepared.metadata.duration_seconds,
                    prepared.path,
                    prepared.byte_size,
                    prepared.sha256,
                )
            else:
                if not request.raw_download_url or request.youtube_url is not None:
                    raise IngestError(
                        "source_request_invalid", "the upload source request is incomplete"
                    )
                raw_path = work_directory / "source.bin"
                await client.download(
                    request.raw_download_url,
                    raw_path,
                    root=work_directory,
                    max_bytes=min(request.max_raw_bytes, self.settings.max_source_bytes),
                    expected_byte_size=request.raw_byte_size,
                    expected_sha256=request.raw_sha256,
                )
                result = await prepare_local_source(
                    raw_path,
                    work_directory,
                    title=request.display_title,
                    max_canonical_bytes=request.max_canonical_bytes,
                    command_timeout_seconds=self.settings.command_timeout_seconds,
                )
            if result.byte_size > request.max_canonical_bytes:
                raise IngestError(
                    "canonical_source_size_exceeded",
                    "the canonical source exceeds the byte limit",
                )
            await client.upload(
                request.canonical_upload_url,
                result.path,
                max_bytes=request.max_canonical_bytes,
                expected_sha256=result.sha256,
            )
            return PrepareSourceResponse(
                source_asset_id=request.source_asset_id,
                title=result.title,
                duration_seconds=result.duration_seconds,
                prepared_bytes=result.byte_size,
                prepared_sha256=result.sha256,
            )
        except IngestError:
            raise
        except Exception as exc:
            raise IngestError("source_prepare_failed", "the source could not be prepared") from exc
        finally:
            cleanup_job_directory(work_directory, retain=self.settings.retain_debug_artifacts)

    async def prepare_clip_v2(self, request: PrepareClipRequest) -> PrepareClipResponse:
        if request.max_source_bytes > self.settings.max_source_bytes:
            raise IngestError("source_size_exceeded", "the source limit is too high")
        job_id = str(request.job_id)
        work_directory = self.settings.paths.job_temporary(job_id)
        client = self._transfer()
        try:
            source_path = work_directory / "source.mp3"
            await client.download(
                request.source_download_url,
                source_path,
                root=work_directory,
                max_bytes=request.max_source_bytes,
                expected_byte_size=request.source_byte_size,
                expected_sha256=request.source_sha256,
            )
            result = await prepare_clip_local(
                source_path,
                work_directory,
                start_seconds=request.start_seconds,
                end_seconds=request.end_seconds,
                max_bytes=request.max_source_bytes,
                command_timeout_seconds=self.settings.command_timeout_seconds,
            )
            await client.upload(
                request.clip_upload_url,
                result.path,
                max_bytes=request.max_source_bytes,
                expected_sha256=result.sha256,
            )
            return PrepareClipResponse(
                job_id=request.job_id,
                prepared_bytes=result.byte_size,
                prepared_sha256=result.sha256,
                duration_seconds=result.duration_seconds,
            )
        finally:
            cleanup_job_directory(work_directory, retain=self.settings.retain_debug_artifacts)

    async def prepare_derivative_v2(
        self, request: PrepareDerivativeRequest
    ) -> PrepareDerivativeResponse:
        if request.max_source_bytes > self.settings.max_source_bytes:
            raise IngestError("source_size_exceeded", "the source limit is too high")
        if request.max_canonical_bytes > self.settings.canonical_source_max_bytes:
            raise IngestError(
                "canonical_source_size_exceeded", "the canonical source limit is too high"
            )
        task_id = str(request.derivative_task_id)
        work_directory = self.settings.paths.job_temporary(task_id)
        client = self._transfer()
        try:
            primary_path = work_directory / f"primary.{request.source_format}"
            await client.download(
                request.source_download_url,
                primary_path,
                root=work_directory,
                max_bytes=request.max_source_bytes,
                expected_byte_size=request.source_byte_size,
                expected_sha256=request.source_sha256,
            )
            result = await prepare_local_source(
                primary_path,
                work_directory,
                title="playback derivative",
                max_canonical_bytes=request.max_canonical_bytes,
                command_timeout_seconds=self.settings.command_timeout_seconds,
            )
            if request.expected_duration_seconds is not None and abs(
                result.duration_seconds - request.expected_duration_seconds
            ) > min(5.0, max(0.05, request.expected_duration_seconds * 0.01)):
                raise IngestError(
                    "derivative_duration_mismatch", "the playback derivative duration is invalid"
                )
            await client.upload(
                request.derivative_upload_url,
                result.path,
                max_bytes=request.max_canonical_bytes,
                expected_sha256=result.sha256,
            )
            return PrepareDerivativeResponse(
                derivative_task_id=request.derivative_task_id,
                prepared_bytes=result.byte_size,
                prepared_sha256=result.sha256,
                duration_seconds=result.duration_seconds,
            )
        finally:
            cleanup_job_directory(work_directory, retain=self.settings.retain_debug_artifacts)


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


async def _periodic_cleanup(app: FastAPI) -> None:
    settings: HomeIngestSettings = app.state.settings
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        try:
            await asyncio.to_thread(prune_orphan_job_directories, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "periodic cleanup failed stage=cleanup exception_class=%s",
                type(exc).__name__,
                extra={"component": "home_ingest"},
            )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: HomeIngestSettings = app.state.settings
    configure_logging(settings)
    try:
        await asyncio.to_thread(prune_orphan_job_directories, settings)
    except Exception as exc:
        LOGGER.error(
            "startup cleanup failed stage=cleanup exception_class=%s",
            type(exc).__name__,
            extra={"component": "home_ingest"},
        )
    task = asyncio.create_task(_periodic_cleanup(app), name="ace-home-ingest-cleanup")
    app.state.cleanup_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        transfer_client = getattr(app.state.service, "transfer_client", None)
        if transfer_client is not None:
            await transfer_client.aclose()


def _http_status(code: str) -> int:
    if code == "youtube_url_rejected":
        return 400
    if code in {
        "youtube_duration_exceeded",
        "source_size_exceeded",
        "canonical_source_size_exceeded",
    }:
        return 413
    if code in {
        "youtube_metadata_failed",
        "youtube_download_failed",
        "ffprobe_failed",
        "ffmpeg_failed",
        "source_prepare_failed",
        "transfer_download_failed",
        "transfer_upload_failed",
        "source_integrity_mismatch",
        "clip_duration_mismatch",
        "derivative_duration_mismatch",
    }:
        return 502
    if code in {"source_request_invalid", "invalid_source_range", "transfer_url_rejected"}:
        return 400
    if code == "youtube_blocked_or_login_required":
        return 424
    if code == "sftp_upload_failed":
        return 502
    return 500


def create_app(
    settings: HomeIngestSettings | None = None,
    *,
    uploader: SourceUploader | None = None,
    transfer_client: BoundedTransferClient | None = None,
) -> FastAPI:
    """Create the private API without exposing docs or a catch-all route."""

    resolved_settings = settings or HomeIngestSettings()
    resolved_settings.ensure_data_layout()
    service = HomeIngestService(
        resolved_settings,
        uploader or SFTPUploader(resolved_settings),
        transfer_client=transfer_client,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.settings = resolved_settings
    app.state.cleanup_task = None
    app.state.service = service
    app.state.transfer_client = transfer_client

    @app.get("/healthz")
    async def healthz(authorization: str | None = Header(default=None)) -> dict[str, str]:
        if not _valid_bearer(authorization, resolved_settings.token):
            raise HTTPException(status_code=401, detail="authentication required")
        return {"status": "ok"}

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

    @app.post(
        "/v2/prepare-source",
        response_model=PrepareSourceResponse,
        response_model_exclude_none=True,
    )
    async def prepare_source_v2(
        payload: PrepareSourceRequest, authorization: str | None = Header(default=None)
    ) -> PrepareSourceResponse:
        if not _valid_bearer(authorization, resolved_settings.token):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return await service.prepare_source_v2(payload)
        except IngestError as exc:
            raise HTTPException(
                status_code=_http_status(exc.code),
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @app.post(
        "/v2/prepare-clip",
        response_model=PrepareClipResponse,
        response_model_exclude_none=True,
    )
    async def prepare_clip_v2(
        payload: PrepareClipRequest, authorization: str | None = Header(default=None)
    ) -> PrepareClipResponse:
        if not _valid_bearer(authorization, resolved_settings.token):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return await service.prepare_clip_v2(payload)
        except IngestError as exc:
            raise HTTPException(
                status_code=_http_status(exc.code),
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @app.post(
        "/v2/prepare-playback-derivative",
        response_model=PrepareDerivativeResponse,
        response_model_exclude_none=True,
    )
    async def prepare_playback_derivative_v2(
        payload: PrepareDerivativeRequest, authorization: str | None = Header(default=None)
    ) -> PrepareDerivativeResponse:
        if not _valid_bearer(authorization, resolved_settings.token):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return await service.prepare_derivative_v2(payload)
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

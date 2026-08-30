"""Source publication, Home Ingest orchestration, and playback derivatives."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, or_, select

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.home_ingest import HomeIngestError
from ace_service.media_library import MediaLibraryError, verify_media_file
from ace_service.models import (
    AssetTransferCapability,
    AssetTransferDirection,
    AssetTransferPurpose,
    AssetTransferStatus,
    Job,
    JobStatus,
    MediaDerivativeTask,
    MediaFile,
    MediaFileState,
    MediaItem,
    MediaItemKind,
    OutputFormat,
    PlaylistEntry,
    SourceAsset,
    SourceAssetOrigin,
    SourceAssetStatus,
    utc_now,
)
from ace_service.repository import (
    fail_derivative_task,
    get_derivative_task,
    get_job,
    get_source_asset,
    issue_asset_transfer_capability,
    mark_derivative_running,
    mark_source_preparing,
    revoke_asset_transfers,
    transition_job,
    validate_source_range,
)
from ace_service.schemas import resolve_relative_path, validate_sha256

LOGGER = logging.getLogger(__name__)

_PERMANENT_SOURCE_ERRORS = frozenset(
    {
        "youtube_url_rejected",
        "youtube_blocked_or_login_required",
        "source_size_exceeded",
        "canonical_source_size_exceeded",
        "ffprobe_failed",
        "ffmpeg_failed",
        "source_integrity_mismatch",
        "invalid_source_range",
        "transfer_url_rejected",
        "prepared_source_invalid",
    }
)
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _asset_url(settings: ServiceSettings, direction: AssetTransferDirection, token: str) -> str:
    operation = "upload" if direction is AssetTransferDirection.UPLOAD else "download"
    return f"{settings.transfer_public_base_url.rstrip('/')}/asset-transfer/v2/{operation}/{token}"


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _safe_file(path: Path, root: Path, *, expected_size: int, expected_sha256: str) -> None:
    try:
        relative = path.relative_to(root.resolve())
    except ValueError as exc:
        raise MediaLibraryError("asset path escapes its storage root") from exc
    current = root.resolve()
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise MediaLibraryError("asset path contains a symlink")
    if path.is_symlink() or not path.is_file():
        raise MediaLibraryError("asset file is missing")
    size, digest = _sha256(path)
    if size != expected_size or digest != expected_sha256:
        raise MediaLibraryError("asset file metadata does not match its receipt")


def _purge_file(path: Path, root: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return
    if path.is_symlink():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.warning(
            "source raw cleanup deferred stage=source_cleanup exception_class=%s",
            "OSError",
            extra={"component": "controller"},
        )


def purge_source_raw(settings: ServiceSettings, source_asset_id: str | UUID) -> None:
    """Remove only a published direct-upload raw file and its safe part file."""

    directory = settings.paths.source_upload_directory(str(source_asset_id))
    _purge_file(directory / "source.bin.part", settings.paths.uploads)
    _purge_file(directory / "source.bin", settings.paths.uploads)
    try:
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass


def _canonical_capability(
    session: Any, source_asset: SourceAsset
) -> AssetTransferCapability | None:
    return cast(
        AssetTransferCapability | None,
        session.scalar(
            select(AssetTransferCapability)
            .where(
                AssetTransferCapability.source_asset_id == source_asset.id,
                AssetTransferCapability.purpose == AssetTransferPurpose.HOME_SOURCE_MP3_UPLOAD,
                AssetTransferCapability.status == AssetTransferStatus.CONSUMED,
            )
            .order_by(AssetTransferCapability.created_at.desc())
        ),
    )


def publish_ready_source(
    session: Any,
    source_asset_id: str | UUID,
    *,
    settings: ServiceSettings,
    title: str | None = None,
    duration_seconds: float,
    canonical_byte_size: int,
    canonical_sha256: str,
) -> MediaItem:
    """Publish one verified canonical source in one caller-owned transaction."""

    source_asset = get_source_asset(session, source_asset_id)
    if source_asset is None:
        raise KeyError(f"unknown source asset: {source_asset_id}")
    if source_asset.status is SourceAssetStatus.CANCELLED:
        raise ValueError("source asset was cancelled")
    if source_asset.status is SourceAssetStatus.READY:
        existing_item = source_asset.media_item or session.scalar(
            select(MediaItem).where(MediaItem.source_asset_id == source_asset.id)
        )
        if existing_item is not None:
            return existing_item
    if source_asset.status not in {SourceAssetStatus.PREPARING, SourceAssetStatus.READY}:
        raise ValueError("source asset is not being published")
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise ValueError("source duration must be numeric")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("source duration must be finite and positive")
    if (
        isinstance(canonical_byte_size, bool)
        or not isinstance(canonical_byte_size, int)
        or canonical_byte_size <= 0
        or canonical_byte_size > settings.canonical_source_max_bytes
    ):
        raise ValueError("canonical source size is invalid")
    digest = validate_sha256(canonical_sha256)
    capability = _canonical_capability(session, source_asset)
    if capability is None:
        raise ValueError("canonical source upload has not been received")
    if (
        capability.received_byte_size != canonical_byte_size
        or capability.received_sha256 != digest
        or capability.expected_relative_path != f"sources/{source_asset.id}/source.mp3"
    ):
        raise ValueError("canonical source receipt does not match Home Ingest metadata")
    final_path = settings.paths.source_library(source_asset.id)
    _safe_file(
        final_path,
        settings.paths.library,
        expected_size=canonical_byte_size,
        expected_sha256=digest,
    )
    clean_title = (title or source_asset.display_title).strip()
    if not clean_title or len(clean_title) > 300:
        raise ValueError("source title is invalid")
    timestamp = utc_now()
    source_asset.display_title = clean_title
    source_asset.canonical_byte_size = canonical_byte_size
    source_asset.canonical_sha256 = digest
    source_asset.duration_seconds = duration
    source_asset.status = SourceAssetStatus.READY
    source_asset.completed_at = source_asset.completed_at or timestamp
    source_asset.error_code = None
    source_asset.user_facing_error = None
    source_asset.next_attempt_at = None
    source_asset.updated_at = timestamp
    item = source_asset.media_item
    if item is None:
        item = MediaItem(
            id=str(UUID(source_asset.id)),
            project_id=source_asset.project_id,
            source_asset_id=source_asset.id,
            kind=MediaItemKind.SOURCE,
            title=clean_title,
            duration_seconds=duration,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(item)
        session.flush()
        session.add(
            MediaFile(
                media_item_id=item.id,
                storage_namespace="library",
                format=OutputFormat.MP3,
                relative_path=f"sources/{source_asset.id}/source.mp3",
                mime_type="audio/mpeg",
                byte_size=canonical_byte_size,
                sha256=digest,
                is_playback=1,
                is_primary_download=1,
                state=MediaFileState.ACTIVE,
                created_at=timestamp,
            )
        )
        session.flush()
    else:
        if item.kind is not MediaItemKind.SOURCE or item.project_id != source_asset.project_id:
            raise ValueError("source media provenance is inconsistent")
        item.title = clean_title
        item.duration_seconds = duration
        item.updated_at = timestamp
    playlist = next(
        (
            playlist
            for playlist in source_asset.project.playlists
            if playlist.kind.value == "project"
        ),
        None,
    )
    if playlist is None:
        from ace_service.repository import ensure_project_playlist

        playlist = ensure_project_playlist(session, source_asset.project_id, now=timestamp)
    entry_exists = session.scalar(
        select(PlaylistEntry.id).where(
            PlaylistEntry.playlist_id == playlist.id,
            PlaylistEntry.media_item_id == item.id,
        )
    )
    if entry_exists is None:
        from ace_service.repository import add_playlist_entry

        add_playlist_entry(session, playlist.id, item.id, now=timestamp)
    source_asset.project.updated_at = timestamp
    session.flush()
    return item


def _expires(settings: ServiceSettings) -> datetime:
    return utc_now() + timedelta(seconds=settings.transfer_token_ttl_seconds)


def _job_path(settings: ServiceSettings, job_id: str, filename: str) -> Path:
    """Resolve one deterministic incoming-job file without accepting a path."""

    identity = str(UUID(str(job_id)))
    if filename not in {"source.mp3", "source.mp3.part"}:
        raise ValueError("unsupported staged source filename")
    relative = f"{identity}/{filename}"
    current = settings.paths.incoming
    for component in Path(relative).parts:
        current /= component
        if current.is_symlink():
            raise MediaLibraryError("staged source path contains a symlink")
    candidate = resolve_relative_path(settings.paths.incoming, relative)
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.parent.chmod(0o700)
    return candidate


def _staged_file_is_valid(
    path: Path, *, root: Path, byte_size: int | None, sha256: str | None
) -> bool:
    if byte_size is None or sha256 is None:
        return False
    try:
        _safe_file(path, root, expected_size=byte_size, expected_sha256=validate_sha256(sha256))
    except (MediaLibraryError, OSError, ValueError):
        return False
    return True


def _copy_source_to_job(
    settings: ServiceSettings,
    source_path: Path,
    job_id: str,
    *,
    byte_size: int,
    sha256: str,
) -> None:
    target = _job_path(settings, job_id, "source.mp3")
    part = _job_path(settings, job_id, "source.mp3.part")
    if _staged_file_is_valid(
        target, root=settings.paths.incoming, byte_size=byte_size, sha256=sha256
    ):
        return
    for candidate in (part, target):
        if candidate.is_symlink():
            raise MediaLibraryError("staged source path contains a symlink")
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    try:
        with source_path.open("rb") as source, part.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        part.chmod(0o600)
        part.replace(target)
        _fsync_directory(target.parent)
    except OSError as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise MediaLibraryError("source clip could not be staged") from exc
    if not _staged_file_is_valid(
        target, root=settings.paths.incoming, byte_size=byte_size, sha256=sha256
    ):
        raise MediaLibraryError("staged source failed integrity verification")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _set_staged_job_metadata(
    session: Any,
    job: Job,
    *,
    duration_seconds: float,
    byte_size: int,
    sha256: str,
) -> None:
    job.source_duration = duration_seconds
    job.source_byte_size = byte_size
    job.source_sha256 = validate_sha256(sha256)
    normalized = job.normalized_request_json
    if isinstance(normalized, dict):
        normalized = dict(normalized)
        generation = normalized.get("generation")
        if isinstance(generation, dict):
            generation = dict(generation)
            if generation.get("duration_mode") == "source":
                generation["duration_seconds"] = duration_seconds
                generation["duration"] = duration_seconds
            normalized["generation"] = generation
        resolved = normalized.get("resolved_parameters")
        if isinstance(resolved, dict):
            resolved = dict(resolved)
            resolved["source_duration_seconds"] = duration_seconds
            if resolved.get("duration_mode") == "source":
                resolved["duration"] = duration_seconds
                resolved["target_duration_seconds"] = duration_seconds
            normalized["resolved_parameters"] = resolved
        normalized["source_duration_seconds"] = duration_seconds
        normalized["resolved_target_duration_seconds"] = duration_seconds
        normalized["ace_duration_seconds"] = duration_seconds
        job.normalized_request_json = normalized
    job.updated_at = utc_now()


async def stage_source_job(
    settings: ServiceSettings,
    session_factory: SessionFactory,
    home_ingest_client: Any,
    job_id: str,
    *,
    home_ingest_semaphore: asyncio.Semaphore | None = None,
) -> bool:
    """Stage a source-aware cover job, returning only after STAGING is committed."""

    with session_factory() as session:
        job = get_job(session, job_id)
        if job is None or job.source_media_item_id is None:
            return False
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            session.rollback()
            return False
        source_item = job.source_media_item
        if source_item is None or source_item.duration_seconds is None:
            raise HomeIngestError("source_prepare_failed", "the selected source is unavailable")
        snapshot = job.backend_snapshot_json
        if not isinstance(snapshot, dict):
            raise HomeIngestError("invalid_source_range", "the job has no backend range contract")
        if job.source_clip_start_seconds is None or job.source_clip_end_seconds is None:
            raise HomeIngestError("invalid_source_range", "the job has no source range")
        clip_duration = validate_source_range(
            source_item,
            job.source_clip_start_seconds,
            job.source_clip_end_seconds,
            snapshot,
        )
        source_file = session.scalar(
            select(MediaFile).where(
                MediaFile.media_item_id == source_item.id,
                MediaFile.format == OutputFormat.MP3,
                MediaFile.is_playback == 1,
                MediaFile.state == MediaFileState.ACTIVE,
            )
        )
        if source_file is None:
            raise HomeIngestError(
                "source_prepare_failed", "the selected source has no MP3 playback"
            )
        source_path = verify_media_file(settings, source_file)
        source_size = source_file.byte_size
        source_sha256 = validate_sha256(source_file.sha256)
        current_status = job.status
        recovered_upload = session.scalar(
            select(AssetTransferCapability)
            .where(
                AssetTransferCapability.job_id == job.id,
                AssetTransferCapability.purpose == AssetTransferPurpose.HOME_CLIP_UPLOAD,
                AssetTransferCapability.status == AssetTransferStatus.CONSUMED,
            )
            .order_by(AssetTransferCapability.created_at.desc())
        )
        recovered_path = _job_path(settings, job.id, "source.mp3")
        if (
            recovered_upload is not None
            and recovered_upload.received_byte_size is not None
            and recovered_upload.received_sha256 is not None
            and _staged_file_is_valid(
                recovered_path,
                root=settings.paths.incoming,
                byte_size=recovered_upload.received_byte_size,
                sha256=recovered_upload.received_sha256,
            )
        ):
            if current_status is JobStatus.QUEUED:
                transition_job(session, job.id, JobStatus.INGESTING)
            _set_staged_job_metadata(
                session,
                job,
                duration_seconds=clip_duration,
                byte_size=recovered_upload.received_byte_size,
                sha256=recovered_upload.received_sha256,
            )
            transition_job(session, job.id, JobStatus.STAGING)
            revoke_asset_transfers(session, job_id=job.id)
            session.commit()
            return True
        staged = _staged_file_is_valid(
            _job_path(settings, job.id, "source.mp3"),
            root=settings.paths.incoming,
            byte_size=job.source_byte_size,
            sha256=job.source_sha256,
        )
        if staged:
            _set_staged_job_metadata(
                session,
                job,
                duration_seconds=clip_duration,
                byte_size=job.source_byte_size or 0,
                sha256=job.source_sha256 or "",
            )
            if current_status is not JobStatus.STAGING:
                transition_job(session, job.id, JobStatus.STAGING)
            session.commit()
            return True
        if current_status is JobStatus.QUEUED:
            transition_job(session, job.id, JobStatus.INGESTING)
            session.commit()
        elif current_status is not JobStatus.INGESTING:
            session.commit()

    start = float(job.source_clip_start_seconds)
    end = float(job.source_clip_end_seconds)
    full_range = start <= 0.001 and abs(end - float(source_item.duration_seconds)) <= 0.001
    if full_range:
        await asyncio.to_thread(
            _copy_source_to_job,
            settings,
            source_path,
            job_id,
            byte_size=source_size,
            sha256=source_sha256,
        )
        with session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status is not JobStatus.INGESTING:
                session.rollback()
                return False
            _set_staged_job_metadata(
                session,
                job,
                duration_seconds=clip_duration,
                byte_size=source_size,
                sha256=source_sha256,
            )
            transition_job(session, job.id, JobStatus.STAGING)
            session.commit()
        return True

    if source_item.source_asset_id is None:
        raise HomeIngestError(
            "source_prepare_failed", "subrange staging requires a published source asset"
        )
    with session_factory() as session:
        job = get_job(session, job_id)
        if job is None or job.status is not JobStatus.INGESTING:
            session.rollback()
            return False
        download = issue_asset_transfer_capability(
            session,
            purpose=AssetTransferPurpose.HOME_CLIP_DOWNLOAD,
            source_asset_id=source_item.source_asset_id,
            expected_relative_path=source_file.relative_path,
            expected_extension=".mp3",
            expected_mime_type="audio/mpeg",
            expected_byte_size=source_size,
            expected_sha256=source_sha256,
            max_bytes=settings.transfer_max_source_bytes,
            expires_at=_expires(settings),
        )
        upload = issue_asset_transfer_capability(
            session,
            purpose=AssetTransferPurpose.HOME_CLIP_UPLOAD,
            job_id=job.id,
            expected_relative_path=f"{job.id}/source.mp3",
            expected_extension=".mp3",
            expected_mime_type="audio/mpeg",
            max_bytes=settings.transfer_max_source_bytes,
            expires_at=_expires(settings),
        )
        session.commit()
    method = getattr(home_ingest_client, "prepare_clip_v2", None)
    if method is None:
        raise HomeIngestError("home_ingest_unavailable", "home ingest v2 is not configured")
    try:
        call = method(
            job_id=job_id,
            source_download_url=_asset_url(
                settings, AssetTransferDirection.DOWNLOAD, download.token
            ),
            source_byte_size=source_size,
            source_sha256=source_sha256,
            clip_upload_url=_asset_url(settings, AssetTransferDirection.UPLOAD, upload.token),
            start_seconds=start,
            end_seconds=end,
            max_source_bytes=settings.transfer_max_source_bytes,
        )
        if home_ingest_semaphore is None:
            result = await call
        else:
            async with home_ingest_semaphore:
                result = await call
    except asyncio.CancelledError:
        raise
    except Exception:
        with session_factory() as session:
            revoke_asset_transfers(session, job_id=job_id)
            revoke_asset_transfers(session, source_asset_id=source_item.source_asset_id)
            session.commit()
        raise
    with session_factory() as session:
        job = get_job(session, job_id)
        if job is None or job.status is not JobStatus.INGESTING:
            session.rollback()
            return False
        upload_capability = session.get(AssetTransferCapability, upload.capability.id)
        if (
            upload_capability is None
            or upload_capability.status is not AssetTransferStatus.CONSUMED
            or upload_capability.received_byte_size != result.prepared_bytes
            or upload_capability.received_sha256 != result.prepared_sha256
        ):
            raise HomeIngestError(
                "source_integrity_mismatch", "the prepared source clip was not received"
            )
        final_path = _job_path(settings, job.id, "source.mp3")
        _safe_file(
            final_path,
            settings.paths.incoming,
            expected_size=result.prepared_bytes,
            expected_sha256=validate_sha256(result.prepared_sha256),
        )
        _set_staged_job_metadata(
            session,
            job,
            # The range contract is controller-owned.  Home's measured MP3
            # duration is checked against it, but the persisted generation
            # duration remains the exact selected clip duration.
            duration_seconds=clip_duration,
            byte_size=result.prepared_bytes,
            sha256=result.prepared_sha256,
        )
        transition_job(session, job.id, JobStatus.STAGING)
        revoke_asset_transfers(session, job_id=job.id)
        revoke_asset_transfers(session, source_asset_id=source_item.source_asset_id)
        session.commit()
    return True


class SourceIngestCoordinator:
    """Serialize Home Ingest source and derivative work with durable recovery."""

    def __init__(
        self,
        settings: ServiceSettings,
        session_factory: SessionFactory,
        home_ingest_client: Any,
        *,
        home_ingest_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.home_ingest_client = home_ingest_client
        self.home_ingest_semaphore = home_ingest_semaphore or asyncio.Semaphore(1)
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._enqueued: set[tuple[str, str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._semaphore = asyncio.Semaphore(1)

    @property
    def accepting(self) -> bool:
        return self._task is not None and not self._stopping

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("source coordinator is already started")
        self._stopping = False
        self._recover()
        self._task = asyncio.create_task(self._run(), name="ace-source-coordinator")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def enqueue_source(self, source_asset_id: str | UUID) -> bool:
        return self._enqueue("source", str(source_asset_id))

    def enqueue_derivative(self, task_id: str | UUID) -> bool:
        return self._enqueue("derivative", str(task_id))

    def _enqueue(self, kind: str, identity: str) -> bool:
        key = (kind, identity)
        if key in self._enqueued:
            return False
        self._enqueued.add(key)
        self._queue.put_nowait(key)
        return True

    def _recover(self) -> None:
        now = utc_now()
        with self.session_factory() as session:
            for asset in session.scalars(select(SourceAsset)):
                if asset.status in {SourceAssetStatus.QUEUED, SourceAssetStatus.UPLOADED}:
                    self.enqueue_source(asset.id)
                elif asset.status is SourceAssetStatus.PREPARING:
                    asset.status = (
                        SourceAssetStatus.UPLOADED
                        if asset.origin is SourceAssetOrigin.UPLOAD
                        else SourceAssetStatus.QUEUED
                    )
                    asset.next_attempt_at = None
                    asset.updated_at = now
                    self.enqueue_source(asset.id)
                elif (
                    asset.status is SourceAssetStatus.FAILED
                    and asset.next_attempt_at is not None
                    and asset.next_attempt_at <= now
                ):
                    asset.status = (
                        SourceAssetStatus.AWAITING_UPLOAD
                        if asset.origin is SourceAssetOrigin.UPLOAD
                        and asset.raw_relative_path is None
                        else SourceAssetStatus.QUEUED
                    )
                    asset.next_attempt_at = None
                    self.enqueue_source(asset.id)
            for task in session.scalars(select(MediaDerivativeTask)):
                if task.status == "running":
                    task.status = "pending"
                if task.status == "pending" or (
                    task.status == "failed"
                    and task.next_attempt_at is not None
                    and task.next_attempt_at <= now
                ):
                    self.enqueue_derivative(task.id)
            session.commit()

    async def run_once(self, kind: str | None = None, identity: str | None = None) -> bool:
        """Run one requested operation; useful for recovery and integration tests."""

        if kind is not None and identity is not None:
            if kind == "source":
                await self._process_source(identity)
            elif kind == "derivative":
                await self._process_derivative(identity)
            else:
                raise ValueError("unknown source coordinator operation")
            return True
        with self.session_factory() as session:
            source = session.scalar(
                select(SourceAsset)
                .where(
                    or_(
                        SourceAsset.status.in_(
                            (SourceAssetStatus.QUEUED, SourceAssetStatus.UPLOADED)
                        ),
                        and_(
                            SourceAsset.status == SourceAssetStatus.FAILED,
                            SourceAsset.next_attempt_at.is_not(None),
                            SourceAsset.next_attempt_at <= utc_now(),
                        ),
                    )
                )
                .order_by(SourceAsset.created_at, SourceAsset.id)
            )
            task = session.scalar(
                select(MediaDerivativeTask)
                .where(MediaDerivativeTask.status.in_(("pending", "failed")))
                .order_by(MediaDerivativeTask.created_at, MediaDerivativeTask.id)
            )
            if source is not None:
                identity = source.id
                kind = "source"
            elif task is not None:
                identity = task.id
                kind = "derivative"
            else:
                return False
        await self.run_once(kind, identity)
        return True

    async def _run(self) -> None:
        while True:
            try:
                kind, identity = await asyncio.wait_for(self._queue.get(), timeout=5)
            except TimeoutError:
                await self.run_once()
                continue
            self._enqueued.discard((kind, identity))
            try:
                async with self._semaphore:
                    await self.run_once(kind, identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "source coordinator operation failed kind=%s stage=home_ingest "
                    "exception_class=%s",
                    kind,
                    type(exc).__name__,
                    extra={"component": "controller"},
                )
            finally:
                self._queue.task_done()

    async def _process_source(self, source_asset_id: str) -> None:
        with self.session_factory() as session:
            asset = get_source_asset(session, source_asset_id)
            if asset is None or asset.status in {
                SourceAssetStatus.AWAITING_UPLOAD,
                SourceAssetStatus.READY,
                SourceAssetStatus.CANCELLED,
            }:
                session.rollback()
                return
            if asset.status is SourceAssetStatus.FAILED:
                if asset.next_attempt_at is None or asset.next_attempt_at > utc_now():
                    session.rollback()
                    return
                asset.status = (
                    SourceAssetStatus.QUEUED
                    if asset.origin is SourceAssetOrigin.YOUTUBE or asset.raw_relative_path
                    else SourceAssetStatus.AWAITING_UPLOAD
                )
            mark_source_preparing(session, asset.id)
            session.commit()
        try:
            async with self.home_ingest_semaphore:
                result = await self._prepare_source(source_asset_id)
            with self.session_factory() as session:
                current_asset = get_source_asset(session, source_asset_id)
                if current_asset is None or current_asset.status is SourceAssetStatus.CANCELLED:
                    session.rollback()
                    return
                publish_ready_source(
                    session,
                    source_asset_id,
                    settings=self.settings,
                    title=result.title,
                    duration_seconds=result.duration_seconds,
                    canonical_byte_size=result.prepared_bytes,
                    canonical_sha256=result.prepared_sha256,
                )
                revoke_asset_transfers(session, source_asset_id=source_asset_id)
                session.commit()
            purge_source_raw(self.settings, source_asset_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_source_failure(source_asset_id, exc)

    async def _prepare_source(self, source_asset_id: str) -> Any:
        with self.session_factory() as session:
            asset = get_source_asset(session, source_asset_id)
            if asset is None:
                raise ValueError("source asset disappeared")
            # A retry must not leave an earlier canonical or raw capability
            # usable while the new Home Ingest attempt owns the source.
            revoke_asset_transfers(session, source_asset_id=asset.id)
            issued_canonical = issue_asset_transfer_capability(
                session,
                purpose=AssetTransferPurpose.HOME_SOURCE_MP3_UPLOAD,
                source_asset_id=asset.id,
                expected_relative_path=f"sources/{asset.id}/source.mp3",
                expected_extension=".mp3",
                expected_mime_type="audio/mpeg",
                max_bytes=self.settings.canonical_source_max_bytes,
                expires_at=_expires(self.settings),
            )
            raw_download_url = None
            raw_size = None
            raw_sha256 = None
            if asset.origin is SourceAssetOrigin.UPLOAD:
                if (
                    asset.raw_relative_path is None
                    or asset.raw_byte_size is None
                    or asset.raw_sha256 is None
                ):
                    raise HomeIngestError(
                        "source_upload_missing", "the uploaded source is not complete"
                    )
                issued_raw = issue_asset_transfer_capability(
                    session,
                    purpose=AssetTransferPurpose.HOME_SOURCE_DOWNLOAD,
                    source_asset_id=asset.id,
                    expected_relative_path=asset.raw_relative_path,
                    expected_extension=".bin",
                    expected_byte_size=asset.raw_byte_size,
                    expected_sha256=asset.raw_sha256,
                    max_bytes=self.settings.direct_upload_max_bytes,
                    expires_at=_expires(self.settings),
                )
                raw_token = issued_raw.token
                raw_download_url = _asset_url(
                    self.settings, AssetTransferDirection.DOWNLOAD, raw_token
                )
                raw_size = asset.raw_byte_size
                raw_sha256 = asset.raw_sha256
            session.commit()
        kwargs = {
            "source_asset_id": source_asset_id,
            "origin": asset.origin.value,
            "display_title": asset.display_title,
            "canonical_upload_url": _asset_url(
                self.settings, AssetTransferDirection.UPLOAD, issued_canonical.token
            ),
            "youtube_url": asset.youtube_url,
            "raw_download_url": raw_download_url,
            "raw_byte_size": raw_size,
            "raw_sha256": raw_sha256,
            "max_raw_bytes": self.settings.direct_upload_max_bytes,
            "max_canonical_bytes": self.settings.canonical_source_max_bytes,
        }
        method = getattr(self.home_ingest_client, "prepare_source_v2", None)
        if method is None:
            raise HomeIngestError("home_ingest_unavailable", "home ingest v2 is not configured")
        return await method(**kwargs)

    def _record_source_failure(self, source_asset_id: str, exc: Exception) -> None:
        code = getattr(exc, "code", "source_prepare_failed")
        message = getattr(exc, "message", "Source preparation failed. Try again.")
        permanent = code in _PERMANENT_SOURCE_ERRORS
        with self.session_factory() as session:
            asset = get_source_asset(session, source_asset_id)
            if asset is None or asset.status in {
                SourceAssetStatus.READY,
                SourceAssetStatus.CANCELLED,
            }:
                session.rollback()
                return
            retry_at = (
                None
                if permanent
                else utc_now()
                + timedelta(seconds=min(3600, max(5, 2 ** min(asset.attempt_count, 8))))
            )
            asset.status = SourceAssetStatus.FAILED
            asset.error_code = str(code)[:64]
            asset.user_facing_error = str(message)[:500]
            asset.next_attempt_at = retry_at
            asset.updated_at = utc_now()
            revoke_asset_transfers(session, source_asset_id=source_asset_id)
            session.commit()
        if retry_at is not None and self.accepting:
            self.enqueue_source(source_asset_id)

    async def _process_derivative(self, task_id: str) -> None:
        with self.session_factory() as session:
            task = get_derivative_task(session, task_id)
            if task is None or task.status == "ready":
                session.rollback()
                return
            if (
                task.status == "failed"
                and task.next_attempt_at is not None
                and task.next_attempt_at > utc_now()
            ):
                session.rollback()
                return
            mark_derivative_running(session, task.id)
            task_media = task.media_item
            source_file = task.source_media_file
            try:
                source_path = verify_media_file(self.settings, source_file)
            except MediaLibraryError:
                fail_derivative_task(
                    session,
                    task.id,
                    error_code="derivative_source_invalid",
                    user_facing_error="The lossless playback source is unavailable.",
                )
                session.commit()
                return
            revoke_asset_transfers(session, derivative_task_id=task.id)
            source_format = source_file.format.value
            download = issue_asset_transfer_capability(
                session,
                purpose=AssetTransferPurpose.HOME_DERIVATIVE_DOWNLOAD,
                derivative_task_id=task.id,
                expected_relative_path=source_file.relative_path,
                expected_extension=source_format,
                expected_mime_type=source_file.mime_type,
                expected_byte_size=source_file.byte_size,
                expected_sha256=source_file.sha256,
                max_bytes=self.settings.transfer_max_output_bytes,
                expires_at=_expires(self.settings),
            )
            upload = issue_asset_transfer_capability(
                session,
                purpose=AssetTransferPurpose.HOME_DERIVATIVE_UPLOAD,
                derivative_task_id=task.id,
                expected_relative_path=f"generated/{task.media_item_id}/playback.mp3",
                expected_extension=".mp3",
                expected_mime_type="audio/mpeg",
                max_bytes=self.settings.canonical_source_max_bytes,
                expires_at=_expires(self.settings),
            )
            expected_duration = task_media.duration_seconds
            session.commit()
        del source_path
        try:
            method = getattr(self.home_ingest_client, "prepare_derivative_v2", None)
            if method is None:
                raise HomeIngestError("home_ingest_unavailable", "home ingest v2 is not configured")
            async with self.home_ingest_semaphore:
                result = await method(
                    derivative_task_id=task_id,
                    source_download_url=_asset_url(
                        self.settings, AssetTransferDirection.DOWNLOAD, download.token
                    ),
                    source_byte_size=source_file.byte_size,
                    source_sha256=source_file.sha256,
                    source_format=source_format,
                    derivative_upload_url=_asset_url(
                        self.settings, AssetTransferDirection.UPLOAD, upload.token
                    ),
                    max_source_bytes=self.settings.transfer_max_output_bytes,
                    max_canonical_bytes=self.settings.canonical_source_max_bytes,
                    expected_duration_seconds=expected_duration,
                )
            with self.session_factory() as session:
                _publish_ready_derivative(
                    session,
                    task_id,
                    settings=self.settings,
                    byte_size=result.prepared_bytes,
                    sha256=result.prepared_sha256,
                    duration_seconds=result.duration_seconds,
                )
                revoke_asset_transfers(session, derivative_task_id=task_id)
                session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_derivative_failure(task_id, exc)

    def _record_derivative_failure(self, task_id: str, exc: Exception) -> None:
        code = str(getattr(exc, "code", "derivative_prepare_failed"))[:64]
        message = str(getattr(exc, "message", "Preparing player version failed"))[:500]
        with self.session_factory() as session:
            task = get_derivative_task(session, task_id)
            if task is None or task.status == "ready":
                session.rollback()
                return
            retry_at = utc_now() + timedelta(
                seconds=min(3600, max(10, 2 ** min(task.attempt_count, 8)))
            )
            fail_derivative_task(
                session,
                task_id,
                error_code=code,
                user_facing_error=message,
                retry_at=retry_at,
            )
            revoke_asset_transfers(session, derivative_task_id=task_id)
            session.commit()
        if self.accepting:
            self.enqueue_derivative(task_id)


def _publish_ready_derivative(
    session: Any,
    task_id: str,
    *,
    settings: ServiceSettings,
    byte_size: int,
    sha256: str,
    duration_seconds: float,
) -> MediaFile:
    task = get_derivative_task(session, task_id)
    if task is None:
        raise KeyError(f"unknown derivative task: {task_id}")
    if task.status == "ready" and task.output_media_file is not None:
        return task.output_media_file
    digest = validate_sha256(sha256)
    capability = session.scalar(
        select(AssetTransferCapability)
        .where(
            AssetTransferCapability.derivative_task_id == task.id,
            AssetTransferCapability.purpose == AssetTransferPurpose.HOME_DERIVATIVE_UPLOAD,
            AssetTransferCapability.status == AssetTransferStatus.CONSUMED,
        )
        .order_by(AssetTransferCapability.created_at.desc())
    )
    if (
        capability is None
        or capability.received_byte_size != byte_size
        or capability.received_sha256 != digest
    ):
        raise ValueError("playback derivative receipt does not match Home Ingest metadata")
    final_path = settings.paths.generated_playback(task.media_item_id)
    _safe_file(final_path, settings.paths.library, expected_size=byte_size, expected_sha256=digest)
    if not math.isfinite(float(duration_seconds)) or float(duration_seconds) <= 0:
        raise ValueError("playback derivative duration is invalid")
    existing = session.scalar(
        select(MediaFile).where(
            MediaFile.media_item_id == task.media_item_id,
            MediaFile.format == OutputFormat.MP3,
        )
    )
    timestamp = utc_now()
    if existing is None:
        existing = MediaFile(
            media_item_id=task.media_item_id,
            storage_namespace="library",
            format=OutputFormat.MP3,
            relative_path=f"generated/{task.media_item_id}/playback.mp3",
            mime_type="audio/mpeg",
            byte_size=byte_size,
            sha256=digest,
            is_playback=1,
            is_primary_download=0,
            state=MediaFileState.ACTIVE,
            created_at=timestamp,
        )
        session.add(existing)
        session.flush()
    elif (
        existing.byte_size != byte_size
        or existing.sha256 != digest
        or existing.state is not MediaFileState.ACTIVE
    ):
        raise ValueError("playback derivative conflicts with its existing file")
    task.output_media_file_id = existing.id
    task.status = "ready"
    task.completed_at = task.completed_at or timestamp
    task.next_attempt_at = None
    task.error_code = None
    task.user_facing_error = None
    task.updated_at = timestamp
    task.media_item.duration_seconds = float(duration_seconds)
    task.media_item.updated_at = timestamp
    playlist = next(
        (
            playlist
            for playlist in task.media_item.project.playlists
            if playlist.kind.value == "project"
        ),
        None,
    )
    if playlist is None:
        from ace_service.repository import ensure_project_playlist

        playlist = ensure_project_playlist(session, task.media_item.project_id, now=timestamp)
    if (
        session.scalar(
            select(PlaylistEntry.id).where(
                PlaylistEntry.playlist_id == playlist.id,
                PlaylistEntry.media_item_id == task.media_item.id,
            )
        )
        is None
    ):
        from ace_service.repository import add_playlist_entry

        add_playlist_entry(session, playlist.id, task.media_item.id, now=timestamp)
    session.flush()
    return cast(MediaFile, existing)


MediaWorkCoordinator = SourceIngestCoordinator

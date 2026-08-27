"""Verified media-file access and recoverable library deletion coordination."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.models import (
    MediaDeletionState,
    MediaFile,
    MediaFileState,
    MediaItem,
    OutputFormat,
)
from ace_service.repository import (
    get_media_file,
    get_media_item,
    list_media_items_pending_deletion,
    mark_media_item_deleted,
    mark_media_item_deletion_pending,
)
from ace_service.schemas import normalize_relative_path

LOGGER = logging.getLogger(__name__)

_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}
_FORMATS = {OutputFormat.MP3.value, OutputFormat.FLAC.value, OutputFormat.WAV.value}


class MediaLibraryError(RuntimeError):
    """A safe media operation could not complete and should be retried."""


def _storage_root(settings: ServiceSettings, namespace: str) -> Path:
    roots = {
        "outputs": settings.paths.outputs,
        "library": settings.paths.library,
    }
    try:
        return roots[namespace]
    except KeyError as exc:
        raise MediaLibraryError("media file storage namespace is invalid") from exc


def _relative_candidate(root: Path, relative_path: str) -> Path:
    normalized = normalize_relative_path(relative_path)
    candidate = root / normalized
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MediaLibraryError("media file path escapes its storage root") from exc
    current = root
    for component in Path(normalized).parts:
        current = current / component
        if current.is_symlink():
            raise MediaLibraryError("media file path contains a symlink")
    return candidate


def _file_format(media_file: MediaFile) -> str:
    value = (
        media_file.format.value
        if isinstance(media_file.format, OutputFormat)
        else str(media_file.format)
    )
    if value not in _FORMATS:
        raise MediaLibraryError("media file format is invalid")
    return value


def _verify_metadata(media_file: MediaFile) -> None:
    format_value = _file_format(media_file)
    suffix = Path(media_file.relative_path).suffix.lower()
    if suffix != f".{format_value}" or _MIME_TYPES.get(suffix) != media_file.mime_type:
        raise MediaLibraryError("media file extension and MIME type do not agree")
    if media_file.byte_size <= 0 or len(media_file.sha256) != 64:
        raise MediaLibraryError("media file metadata is invalid")
    try:
        int(media_file.sha256, 16)
    except ValueError as exc:
        raise MediaLibraryError("media file hash is invalid") from exc


def verify_media_file(
    settings: ServiceSettings,
    media_file: MediaFile,
    *,
    require_active: bool = True,
    verify_hash: bool = True,
) -> Path:
    """Resolve and verify one active media file without following symlinks."""

    if require_active and (
        media_file.state is not MediaFileState.ACTIVE
        or media_file.media_item.deletion_state is not MediaDeletionState.ACTIVE
    ):
        raise MediaLibraryError("media file is not active")
    _verify_metadata(media_file)
    root = _storage_root(settings, media_file.storage_namespace)
    candidate = _relative_candidate(root, media_file.relative_path)
    if not candidate.is_file() or candidate.is_symlink():
        raise MediaLibraryError("media file is missing")
    stat = candidate.stat()
    if stat.st_size != media_file.byte_size:
        raise MediaLibraryError("media file size does not match its durable metadata")
    if verify_hash:
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != media_file.sha256.lower():
            raise MediaLibraryError("media file hash does not match its durable metadata")
    return candidate


def media_file_content_disposition(media_file: MediaFile, *, attachment: bool) -> str:
    """Return a fixed content disposition with no user-controlled filename."""

    suffix = f".{_file_format(media_file)}"
    disposition = "attachment" if attachment else "inline"
    return f'{disposition}; filename="audioventura-{media_file.id}{suffix}"'


def _safe_identifier(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise MediaLibraryError("media identity is not path-safe")
    return value


def _trash_relative_path(media_item_id: str, media_file: MediaFile) -> str:
    item_part = _safe_identifier(str(media_item_id))
    extension = _file_format(media_file)
    return f"media/{item_part}/{media_file.id}.{extension}"


def _trash_path(settings: ServiceSettings, relative_path: str) -> Path:
    normalized = normalize_relative_path(relative_path)
    candidate = settings.paths.trash / normalized
    try:
        candidate.relative_to(settings.paths.trash)
    except ValueError as exc:
        raise MediaLibraryError("trash path escapes the configured trash root") from exc
    current = settings.paths.trash
    for component in Path(normalized).parts:
        current = current / component
        if current.is_symlink():
            raise MediaLibraryError("trash path contains a symlink")
    return candidate


def _move_to_trash(settings: ServiceSettings, media_item: MediaItem, media_file: MediaFile) -> str:
    relative = _trash_relative_path(media_item.id, media_file)
    destination = _trash_path(settings, relative)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    if destination.exists() or destination.is_symlink():
        source = _relative_candidate(
            _storage_root(settings, media_file.storage_namespace), media_file.relative_path
        )
        if source.exists() or source.is_symlink():
            raise MediaLibraryError("deterministic trash destination already exists")
        return relative
    source = verify_media_file(settings, media_file, require_active=False)
    try:
        os.rename(source, destination)
    except OSError as exc:
        raise MediaLibraryError("media file could not be moved to trash") from exc
    destination.chmod(0o600)
    return relative


class MediaLibraryService:
    """Coordinate database tombstones with deterministic same-filesystem moves."""

    def __init__(self, settings: ServiceSettings, session_factory: SessionFactory) -> None:
        self.settings = settings
        self.session_factory = session_factory
        settings.ensure_data_layout()

    def request_item_deletion(self, media_item_id: str) -> MediaItem:
        with self.session_factory() as session:
            item = mark_media_item_deletion_pending(session, media_item_id)
            session.commit()
            return item

    def reconcile_item_deletion(self, media_item_id: str) -> bool:
        """Move every file, commit the tombstone, then best-effort purge bytes."""

        quarantine_paths: dict[int, str] = {}
        with self.session_factory() as session:
            item = get_media_item(session, media_item_id)
            if item is None:
                return False
            if item.deletion_state is MediaDeletionState.ACTIVE:
                return False
            files = list(
                session.scalars(
                    select(MediaFile)
                    .where(MediaFile.media_item_id == item.id)
                    .order_by(MediaFile.id)
                )
            )
            try:
                for media_file in files:
                    if media_file.state is MediaFileState.ACTIVE:
                        quarantine_paths[media_file.id] = _move_to_trash(
                            self.settings, item, media_file
                        )
                    elif media_file.state is MediaFileState.QUARANTINED:
                        if not media_file.quarantine_relative_path:
                            raise MediaLibraryError("quarantined file has no trash path")
                        _trash_path(self.settings, media_file.quarantine_relative_path)
                        quarantine_paths[media_file.id] = media_file.quarantine_relative_path
                mark_media_item_deleted(session, item.id, quarantine_paths=quarantine_paths)
                session.commit()
            except Exception:
                session.rollback()
                LOGGER.warning(
                    "media deletion is pending stage=media_delete exception_class=%s",
                    "MediaLibraryError",
                    extra={"component": "controller"},
                )
                raise

        self._purge_item(media_item_id)
        return True

    def _purge_item(self, media_item_id: str) -> None:
        with self.session_factory() as session:
            item = get_media_item(session, media_item_id)
            if item is None:
                return
            files = list(
                session.scalars(
                    select(MediaFile).where(
                        MediaFile.media_item_id == item.id,
                        MediaFile.state == MediaFileState.QUARANTINED,
                    )
                )
            )
            purged_ids: list[int] = []
            for media_file in files:
                if not media_file.quarantine_relative_path:
                    continue
                path = _trash_path(self.settings, media_file.quarantine_relative_path)
                try:
                    if path.is_symlink():
                        raise MediaLibraryError("trash file is a symlink")
                    if path.exists():
                        path.unlink()
                    purged_ids.append(media_file.id)
                except OSError as exc:
                    LOGGER.warning(
                        "media trash purge deferred stage=media_delete exception_class=%s",
                        type(exc).__name__,
                        extra={"component": "controller"},
                    )
            for media_file in files:
                if media_file.id in purged_ids:
                    media_file.state = MediaFileState.PURGED
                    media_file.purged_at = media_file.purged_at or datetime.now(UTC)
            session.commit()
        self._remove_empty_trash_dirs(media_item_id)

    def _remove_empty_trash_dirs(self, media_item_id: str) -> None:
        try:
            item_dir = _trash_path(self.settings, f"media/{_safe_identifier(media_item_id)}")
            if item_dir.is_dir() and not item_dir.is_symlink() and not any(item_dir.iterdir()):
                item_dir.rmdir()
            media_dir = self.settings.paths.trash / "media"
            if media_dir.is_dir() and not media_dir.is_symlink() and not any(media_dir.iterdir()):
                media_dir.rmdir()
        except OSError:
            return

    def reconcile_pending_deletions(self) -> int:
        with self.session_factory() as session:
            item_ids = [item.id for item in list_media_items_pending_deletion(session)]
        completed = 0
        for item_id in item_ids:
            try:
                if self.reconcile_item_deletion(item_id):
                    completed += 1
            except MediaLibraryError:
                continue
        return completed


def verified_media_file_path(
    settings: ServiceSettings, session_factory: SessionFactory, media_file_id: int
) -> tuple[MediaFile, Path]:
    """Load and verify a playback file for authenticated media routes."""

    with session_factory() as session:
        media_file = get_media_file(session, media_file_id)
        if media_file is None:
            raise KeyError(f"unknown media file: {media_file_id}")
        path = verify_media_file(settings, media_file)
        return media_file, path


def delete_media_item(
    settings: ServiceSettings, session_factory: SessionFactory, media_item_id: str
) -> bool:
    """Request and converge one media deletion, leaving pending state on failure."""

    service = MediaLibraryService(settings, session_factory)
    service.request_item_deletion(media_item_id)
    return service.reconcile_item_deletion(media_item_id)

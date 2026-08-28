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
    Job,
    MediaDeletionState,
    MediaFile,
    MediaFileState,
    MediaItem,
    Output,
    OutputFormat,
    ProjectDeletionAudit,
)
from ace_service.repository import (
    delete_project_record,
    get_media_file,
    get_media_item,
    get_project,
    list_media_items_pending_deletion,
    mark_media_item_deleted,
    mark_media_item_deletion_pending,
    project_is_deletable,
)
from ace_service.schemas import normalize_relative_path, validate_sha256

LOGGER = logging.getLogger(__name__)

_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}
_FORMATS = {OutputFormat.MP3.value, OutputFormat.FLAC.value, OutputFormat.WAV.value}
_PROJECT_OUTPUT_TRASH_ROOT = "project-outputs"


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


def _output_extension(output: Output) -> str:
    """Validate an output's physical format metadata and return its extension."""

    try:
        normalized_path = normalize_relative_path(output.relative_path)
        expected_sha256 = validate_sha256(output.sha256)
    except (TypeError, ValueError) as exc:
        raise MediaLibraryError("output metadata is invalid") from exc
    suffix = Path(normalized_path).suffix.lower()
    if suffix not in _MIME_TYPES or output.mime_type != _MIME_TYPES[suffix]:
        raise MediaLibraryError("output extension and MIME type do not agree")
    if (
        isinstance(output.byte_size, bool)
        or not isinstance(output.byte_size, int)
        or output.byte_size <= 0
        or output.byte_size > 2**63 - 1
    ):
        raise MediaLibraryError("output byte size is invalid")
    if expected_sha256 != output.sha256.lower():
        raise MediaLibraryError("output hash is not canonical")
    return suffix[1:]


def _verified_output_path(settings: ServiceSettings, output: Output) -> Path:
    """Resolve one output below ``outputs`` without following symlinks."""

    try:
        normalized_path = normalize_relative_path(output.relative_path)
    except ValueError as exc:
        raise MediaLibraryError("output path is invalid") from exc
    _output_extension(output)
    return _relative_candidate(settings.paths.outputs, normalized_path)


def _verify_output_file(path: Path, output: Output) -> None:
    """Verify the file at a previously path-validated output location."""

    if path.is_symlink() or not path.is_file():
        raise MediaLibraryError("output file is missing or is not a regular file")
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise MediaLibraryError("output file could not be inspected") from exc
    if file_stat.st_size != output.byte_size:
        raise MediaLibraryError("output size does not match its durable metadata")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise MediaLibraryError("output file could not be read") from exc
    if digest.hexdigest().lower() != output.sha256.lower():
        raise MediaLibraryError("output hash does not match its durable metadata")


def _project_output_trash_relative_path(project_id: str, output: Output) -> str:
    extension = _output_extension(output)
    project_part = _safe_identifier(str(project_id))
    output_part = _safe_identifier(str(output.id))
    return f"{_PROJECT_OUTPUT_TRASH_ROOT}/{project_part}/{output_part}.{extension}"


def _move_output_to_trash(settings: ServiceSettings, project_id: str, output: Output) -> bool:
    """Move one un-published output to deterministic, retryable project trash."""

    source = _verified_output_path(settings, output)
    relative = _project_output_trash_relative_path(project_id, output)
    destination = _trash_path(settings, relative)
    if destination.exists() or destination.is_symlink():
        if source.exists() or source.is_symlink():
            raise MediaLibraryError("deterministic output trash destination already exists")
        _verify_output_file(destination, output)
        try:
            destination.chmod(0o600)
        except OSError as exc:
            raise MediaLibraryError("output trash file permissions could not be secured") from exc
        return True
    if not source.exists():
        return False
    _verify_output_file(source, output)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    try:
        os.rename(source, destination)
    except OSError as exc:
        raise MediaLibraryError("output file could not be moved to trash") from exc
    try:
        destination.chmod(0o600)
    except OSError as exc:
        raise MediaLibraryError("output trash file permissions could not be secured") from exc
    return True


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
        source_asset_id: str | None = None
        with self.session_factory() as session:
            item = get_media_item(session, media_item_id)
            if item is None:
                return False
            if item.deletion_state is MediaDeletionState.ACTIVE:
                return False
            source_asset_id = item.source_asset_id
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
        if source_asset_id is not None:
            from ace_service.source_assets import purge_source_raw

            purge_source_raw(self.settings, source_asset_id)
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

    def reconcile_project_output_files(self, project_id: str) -> int:
        """Quarantine every project output not already owned by a media item.

        Output rows intentionally remain durable until the project transaction
        deletes them.  The project-scoped deterministic trash path makes a
        crash between the filesystem move and that transaction retryable.
        """

        with self.session_factory() as session:
            published_output_ids = {
                output_id
                for output_id in session.scalars(
                    select(MediaItem.generated_output_id).where(
                        MediaItem.project_id == str(project_id),
                        MediaItem.generated_output_id.is_not(None),
                    )
                )
                if output_id is not None
            }
            outputs = list(
                session.scalars(
                    select(Output)
                    .join(Job, Output.job_id == Job.id)
                    .where(Job.project_id == str(project_id))
                    .order_by(Output.id.asc())
                )
            )
            moved = 0
            for output in outputs:
                if output.id in published_output_ids:
                    continue
                if _move_output_to_trash(self.settings, str(project_id), output):
                    moved += 1
            return moved

    def purge_project_output_trash(self, project_id: str) -> int:
        """Purge only the deterministic trash owned by one deleted project."""

        relative = f"{_PROJECT_OUTPUT_TRASH_ROOT}/{_safe_identifier(str(project_id))}"
        directory = _trash_path(self.settings, relative)
        if not directory.exists():
            return 0
        if directory.is_symlink() or not directory.is_dir():
            raise MediaLibraryError("project output trash is not a directory")
        purged = 0
        for path in list(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise MediaLibraryError("project output trash contains an unsafe entry")
            try:
                path.unlink()
            except OSError as exc:
                raise MediaLibraryError("project output trash could not be purged") from exc
            purged += 1
        try:
            directory.rmdir()
            parent = directory.parent
            if parent.is_dir() and not parent.is_symlink() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError as exc:
            raise MediaLibraryError("project output trash directory could not be removed") from exc
        return purged

    def _remove_empty_output_dirs(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            try:
                candidate = _relative_candidate(
                    self.settings.paths.outputs, _safe_identifier(str(job_id))
                )
                if (
                    candidate.is_dir()
                    and not candidate.is_symlink()
                    and not any(candidate.iterdir())
                ):
                    candidate.rmdir()
            except (MediaLibraryError, OSError):
                continue

    def reconcile_project_deletion(self, project_id: str) -> bool:
        """Converge a confirmed project deletion, including un-published outputs."""

        project_key = str(project_id)
        with self.session_factory() as session:
            audit = session.scalar(
                select(ProjectDeletionAudit).where(ProjectDeletionAudit.project_id == project_key)
            )
            if audit is None:
                raise KeyError(f"unknown project deletion audit: {project_key}")
            project = get_project(session, project_key)
            if project is None:
                self.purge_project_output_trash(project_key)
                return True
            if not project_is_deletable(session, project_key):
                raise ValueError("project has nonterminal jobs")
            item_ids = [item.id for item in project.media_items]
            job_ids = [job.id for job in project.jobs]
            source_asset_ids = [project.source_asset.id] if project.source_asset is not None else []

        for item_id in item_ids:
            with self.session_factory() as session:
                item = get_media_item(session, item_id)
                if item is None:
                    continue
                if item.deletion_state is MediaDeletionState.ACTIVE:
                    mark_media_item_deletion_pending(session, item.id)
                    session.commit()
            self.reconcile_item_deletion(item_id)

        if source_asset_ids:
            from ace_service.source_assets import purge_source_raw

            for source_asset_id in source_asset_ids:
                purge_source_raw(self.settings, source_asset_id)

        self.reconcile_project_output_files(project_key)
        with self.session_factory() as session:
            project = get_project(session, project_key)
            if project is not None:
                remaining = list(
                    session.scalars(
                        select(MediaItem).where(
                            MediaItem.project_id == project_key,
                            MediaItem.deletion_state != MediaDeletionState.DELETED,
                        )
                    )
                )
                if remaining:
                    raise MediaLibraryError("project media deletion is incomplete")
                job_ids = [job.id for job in project.jobs]
                delete_project_record(session, project_key)
                session.commit()

        try:
            self.purge_project_output_trash(project_key)
        except MediaLibraryError:
            LOGGER.warning(
                "project output trash purge deferred stage=project_delete",
                extra={"component": "controller"},
            )
        self._remove_empty_output_dirs(job_ids)
        return True

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

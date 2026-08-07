"""Safe Hetzner-side finalization and cleanup of home-ingested cover sources."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from ace_service.config import ServiceSettings
from ace_service.home_ingest import PreparedCoverSource
from ace_service.schemas import resolve_relative_path, validate_sha256


class CoverSourceError(RuntimeError):
    """Raised when the SFTP source does not match the home metadata handshake."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def finalize_cover_source(
    settings: ServiceSettings, job_id: str, prepared: PreparedCoverSource
) -> Path:
    """Verify the uploaded `.part` file and atomically expose `source.mp3`."""

    if prepared.prepared_format != "mp3":
        raise CoverSourceError("prepared_source_invalid", "home did not prepare an MP3 source")
    if prepared.prepared_bytes > settings.transfer_max_source_bytes:
        raise CoverSourceError("source_size_exceeded", "the prepared source exceeds the byte limit")
    try:
        expected_sha256 = validate_sha256(prepared.prepared_sha256)
    except ValueError as exc:
        raise CoverSourceError(
            "prepared_source_invalid", "home returned an invalid checksum"
        ) from exc
    if not math.isfinite(prepared.duration_seconds) or prepared.duration_seconds <= 0:
        raise CoverSourceError("prepared_source_invalid", "home returned an invalid duration")
    if prepared.duration_seconds > settings.max_source_duration_seconds:
        raise CoverSourceError("youtube_duration_exceeded", "the prepared source is too long")

    part_path = _source_path(settings, job_id, "source.mp3.part")
    final_path = _source_path(settings, job_id, "source.mp3")
    if _valid_file(final_path, prepared.prepared_bytes, expected_sha256):
        _unlink_if_safe(part_path, settings.paths.incoming)
        return final_path
    if final_path.exists() or final_path.is_symlink():
        raise CoverSourceError(
            "prepared_source_invalid", "the final source conflicts with home metadata"
        )
    if not _valid_file(part_path, prepared.prepared_bytes, expected_sha256):
        raise CoverSourceError(
            "source_integrity_mismatch",
            "the uploaded source failed size or checksum verification",
        )
    try:
        part_path.replace(final_path)
    except OSError as exc:
        raise CoverSourceError(
            "prepared_source_invalid", "the uploaded source could not be finalized"
        ) from exc
    if not _valid_file(final_path, prepared.prepared_bytes, expected_sha256):
        _unlink_if_safe(final_path, settings.paths.incoming)
        raise CoverSourceError(
            "source_integrity_mismatch", "the finalized source failed verification"
        )
    _fsync_directory(final_path.parent)
    return final_path


def valid_final_cover_source(
    settings: ServiceSettings, job_id: str, *, byte_size: int, sha256: str
) -> bool:
    """Check a persisted final source before restart can advance its job."""

    try:
        expected_sha256 = validate_sha256(sha256)
    except ValueError:
        return False
    if byte_size <= 0 or byte_size > settings.transfer_max_source_bytes:
        return False
    try:
        return _valid_file(_source_path(settings, job_id, "source.mp3"), byte_size, expected_sha256)
    except CoverSourceError:
        return False


def remove_cover_source(settings: ServiceSettings, job_id: str) -> None:
    """Remove only the deterministic source files when retention is disabled."""

    if settings.retain_cover_source:
        return
    try:
        job_directory = _source_path(settings, job_id, "source.mp3").parent
    except CoverSourceError:
        return
    if _has_symlink_component(settings.paths.incoming, job_directory):
        return
    _unlink_if_safe(job_directory / "source.mp3", settings.paths.incoming)
    _unlink_if_safe(job_directory / "source.mp3.part", settings.paths.incoming)
    try:
        job_directory.rmdir()
    except OSError:
        pass


def _source_path(settings: ServiceSettings, job_id: str, filename: str) -> Path:
    relative_path = f"{job_id}/{filename}"
    try:
        candidate = resolve_relative_path(settings.paths.incoming, relative_path)
    except ValueError as exc:
        raise CoverSourceError("prepared_source_invalid", "the source path is invalid") from exc
    if _has_symlink_component(settings.paths.incoming, candidate.parent):
        raise CoverSourceError("prepared_source_invalid", "the source path uses a symlink")
    return candidate


def _valid_file(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    try:
        stat = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.st_size != expected_bytes:
            return False
        return _sha256(path) == expected_sha256
    except OSError:
        return False


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def _unlink_if_safe(path: Path, root: Path) -> None:
    if _has_symlink_component(root, path.parent):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

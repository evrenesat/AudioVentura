"""Private, bounded, atomic artifact materialization primitives."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_ALLOWED_CDN_HOSTS = frozenset({"storage.googleapis.com", "fal.media", "cdn.fal.ai"})
_MIME_BY_FORMAT = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    path: Path
    byte_size: int
    sha256: str
    content_type: str


def _is_allowed_cdn_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    host = hostname.rstrip(".").lower()
    return host in _ALLOWED_CDN_HOSTS or host.endswith(".fal.media")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_target(root: Path, target: Path) -> Path:
    root = root.resolve()
    raw_target = target if target.is_absolute() else root / target
    raw_target = Path(os.path.abspath(raw_target))
    if not raw_target.parent.is_relative_to(root):
        raise ValueError("artifact target escapes the output directory")
    current = root
    for component in raw_target.relative_to(root).parts:
        current /= component
        if current.is_symlink():
            raise ValueError("artifact target contains a symlink")
    target = raw_target.resolve(strict=False)
    if not target.parent.is_relative_to(root):
        raise ValueError("artifact target escapes the output directory")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    return target


def materialize_stream(
    chunks: Iterable[bytes],
    *,
    root: Path,
    target: Path,
    max_bytes: int,
    content_type: str,
) -> ArtifactReceipt:
    """Write a synchronous byte iterator atomically below ``root``."""

    if max_bytes <= 0 or content_type not in _MIME_BY_FORMAT.values():
        raise ValueError("artifact limits are invalid")
    final_path = _safe_target(root, target)
    if final_path.exists() and final_path.is_file() and not final_path.is_symlink():
        stat = final_path.stat()
        if stat.st_size <= 0 or stat.st_size > max_bytes:
            raise ValueError("existing artifact exceeds the declared byte limit")
        digest = hashlib.sha256()
        with final_path.open("rb") as existing:
            for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                digest.update(chunk)
        final_path.chmod(0o600)
        return ArtifactReceipt(final_path, stat.st_size, digest.hexdigest(), content_type)
    if final_path.exists() or final_path.is_symlink():
        raise ValueError("artifact target already exists")
    part_path = final_path.with_name(f"{final_path.name}.part")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        descriptor = os.open(part_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ValueError("artifact response contained a non-byte chunk")
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise ValueError("artifact exceeds the declared byte limit")
                digest.update(chunk)
                output.write(chunk)
            if byte_count <= 0:
                raise ValueError("artifact is empty")
            output.flush()
            os.fsync(output.fileno())
        part_path.chmod(0o600)
        part_path.replace(final_path)
        _fsync_directory(final_path.parent)
    except Exception:
        try:
            part_path.unlink()
        except OSError:
            pass
        raise
    return ArtifactReceipt(final_path, byte_count, digest.hexdigest(), content_type)


async def materialize_async_stream(
    chunks: AsyncIterable[bytes],
    *,
    root: Path,
    target: Path,
    max_bytes: int,
    content_type: str,
) -> ArtifactReceipt:
    """Write an asynchronous byte stream atomically below ``root``."""

    if max_bytes <= 0 or content_type not in _MIME_BY_FORMAT.values():
        raise ValueError("artifact limits are invalid")
    final_path = _safe_target(root, target)
    if final_path.exists() and final_path.is_file() and not final_path.is_symlink():
        stat = final_path.stat()
        if stat.st_size <= 0 or stat.st_size > max_bytes:
            raise ValueError("existing artifact exceeds the declared byte limit")
        digest = hashlib.sha256()
        with final_path.open("rb") as existing:
            for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                digest.update(chunk)
        final_path.chmod(0o600)
        return ArtifactReceipt(final_path, stat.st_size, digest.hexdigest(), content_type)
    if final_path.exists() or final_path.is_symlink():
        raise ValueError("artifact target already exists")
    part_path = final_path.with_name(f"{final_path.name}.part")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        descriptor = os.open(part_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ValueError("artifact response contained a non-byte chunk")
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise ValueError("artifact exceeds the declared byte limit")
                digest.update(chunk)
                output.write(chunk)
            if byte_count <= 0:
                raise ValueError("artifact is empty")
            output.flush()
            os.fsync(output.fileno())
        part_path.chmod(0o600)
        part_path.replace(final_path)
        _fsync_directory(final_path.parent)
    except Exception:
        try:
            part_path.unlink()
        except OSError:
            pass
        raise
    return ArtifactReceipt(final_path, byte_count, digest.hexdigest(), content_type)


async def materialize_remote_artifact(
    client: httpx.AsyncClient,
    url: str,
    *,
    root: Path,
    target: Path,
    native_format: str,
    max_bytes: int,
    bearer_token: str,
) -> ArtifactReceipt:
    """Stream one private Fal CDN URL with redirect/SSRF protections."""

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not _is_allowed_cdn_host(parsed.hostname) or not parsed.path:
        raise ValueError("artifact URL is not an allowed private CDN URL")
    if native_format not in _MIME_BY_FORMAT:
        raise ValueError("artifact format is unsupported")
    if not bearer_token or len(bearer_token) > 4096:
        raise ValueError("artifact bearer token is invalid")
    async with client.stream(
        "GET", url, headers={"Authorization": f"Bearer {bearer_token}"}, follow_redirects=False
    ) as response:
        if response.is_redirect or response.status_code != 200:
            raise ValueError("artifact CDN response is not successful")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != _MIME_BY_FORMAT[native_format]:
            raise ValueError("artifact CDN content type is not allowed")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) <= 0 or int(content_length) > max_bytes:
                    raise ValueError("artifact CDN content length exceeds the limit")
            except ValueError as exc:
                raise ValueError("artifact CDN content length is invalid") from exc
        final_path = _safe_target(root, target)
        if final_path.exists() and final_path.is_file() and not final_path.is_symlink():
            return materialize_stream(
                (), root=root, target=final_path, max_bytes=max_bytes, content_type=content_type
            )
        if final_path.exists() or final_path.is_symlink():
            raise ValueError("artifact target already exists")
        part_path = final_path.with_name(f"{final_path.name}.part")
        digest = hashlib.sha256()
        byte_count = 0
        try:
            descriptor = os.open(part_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in response.aiter_bytes():
                    if not isinstance(chunk, bytes):
                        raise ValueError("artifact response contained a non-byte chunk")
                    byte_count += len(chunk)
                    if byte_count > max_bytes:
                        raise ValueError("artifact exceeds the declared byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                if byte_count <= 0:
                    raise ValueError("artifact is empty")
                output.flush()
                os.fsync(output.fileno())
            part_path.chmod(0o600)
            part_path.replace(final_path)
            _fsync_directory(final_path.parent)
        except Exception:
            try:
                part_path.unlink()
            except OSError:
                pass
            raise
        return ArtifactReceipt(final_path, byte_count, digest.hexdigest(), content_type)

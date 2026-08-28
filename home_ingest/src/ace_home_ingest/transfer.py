"""Bounded HTTP streaming for controller-owned asset capabilities."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import HomeIngestSettings
from .media import CHUNK_SIZE, IngestError

_MAX_RESPONSE_BYTES = 64 * 1024


class AssetTransferError(IngestError):
    """A safe, bounded error from the v2 transfer boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class BoundedTransferClient:
    """Stream signed URLs while enforcing the configured private origin."""

    def __init__(
        self,
        settings: HomeIngestSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.transfer_connect_timeout_seconds,
                read=settings.transfer_read_timeout_seconds,
                write=settings.transfer_write_timeout_seconds,
                pool=settings.transfer_connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def validate_url(self, url: str, operation: str) -> str:
        try:
            expected = urlsplit(self.settings.transfer_base_url)
            actual = urlsplit(url)
        except ValueError as exc:
            raise AssetTransferError(
                "transfer_url_rejected", "the transfer URL is invalid"
            ) from exc
        if (
            actual.scheme != expected.scheme
            or actual.hostname != expected.hostname
            or actual.port != expected.port
            or actual.username is not None
            or actual.password is not None
            or actual.query
            or actual.fragment
        ):
            raise AssetTransferError("transfer_url_rejected", "the transfer URL is not allowed")
        prefix = expected.path.rstrip("/") + "/asset-transfer/v2/" + operation + "/"
        if not actual.path.startswith(prefix) or not actual.path[len(prefix) :]:
            raise AssetTransferError("transfer_url_rejected", "the transfer URL is not allowed")
        token = actual.path[len(prefix) :]
        if "/" in token or "\\" in token or token in {".", ".."}:
            raise AssetTransferError("transfer_url_rejected", "the transfer URL is not allowed")
        return url

    async def download(
        self,
        url: str,
        target: Path,
        *,
        root: Path,
        max_bytes: int,
        expected_byte_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[int, str]:
        self.validate_url(url, "download")
        if max_bytes <= 0:
            raise AssetTransferError("transfer_size_invalid", "the transfer limit is invalid")
        safe_target = _safe_target(root, target)
        if safe_target.exists() and not safe_target.is_symlink():
            size, digest = _hash_file(safe_target)
            if _matches(size, digest, expected_byte_size, expected_sha256):
                return size, digest
            raise AssetTransferError("transfer_conflict", "the existing transfer file conflicts")
        if safe_target.exists() or safe_target.is_symlink():
            raise AssetTransferError("transfer_conflict", "the transfer target is unsafe")
        part = safe_target.with_name(f"{safe_target.name}.part")
        _unlink(part)
        try:
            async with self.client.stream("GET", url) as response:
                if response.status_code != 200 or response.is_redirect:
                    raise AssetTransferError(
                        "transfer_download_failed", "the source transfer failed"
                    )
                _check_content_length(response.headers.get("content-length"), max_bytes)
                download_digest = hashlib.sha256()
                byte_count = 0
                descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in response.aiter_bytes(CHUNK_SIZE):
                        byte_count += len(chunk)
                        if byte_count > max_bytes:
                            raise AssetTransferError(
                                "source_size_exceeded", "the transfer is too large"
                            )
                        download_digest.update(chunk)
                        output.write(chunk)
                    if byte_count <= 0:
                        raise AssetTransferError(
                            "transfer_download_failed", "the transfer was empty"
                        )
                    output.flush()
                    os.fsync(output.fileno())
                actual_hash = download_digest.hexdigest()
                if not _matches(byte_count, actual_hash, expected_byte_size, expected_sha256):
                    raise AssetTransferError(
                        "source_integrity_mismatch", "the transfer checksum did not match"
                    )
            os.chmod(part, 0o600)
            part.replace(safe_target)
            _fsync_directory(safe_target.parent)
            return byte_count, actual_hash
        except Exception:
            _unlink(part)
            raise

    async def upload(
        self,
        url: str,
        source: Path,
        *,
        max_bytes: int,
        expected_sha256: str | None = None,
    ) -> tuple[int, str]:
        self.validate_url(url, "upload")
        if source.is_symlink() or not source.is_file():
            raise AssetTransferError("transfer_upload_failed", "the upload source is unavailable")
        size, digest = _hash_file(source)
        if size <= 0 or size > max_bytes:
            raise AssetTransferError("source_size_exceeded", "the upload is too large")
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise AssetTransferError(
                "source_integrity_mismatch", "the upload checksum did not match"
            )
        try:
            response = await self.client.put(
                url,
                content=_file_chunks(source),
                headers={
                    "Content-Length": str(size),
                    "X-ACE-Asset-SHA256": digest,
                    "Content-Type": "application/octet-stream",
                },
            )
        except httpx.HTTPError as exc:
            raise AssetTransferError(
                "transfer_upload_failed", "the upload could not be sent"
            ) from exc
        if response.status_code not in {200, 201}:
            raise AssetTransferError("transfer_upload_failed", "the upload was rejected")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise AssetTransferError(
                "transfer_upload_failed", "the transfer response was oversized"
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AssetTransferError(
                "transfer_upload_failed", "the transfer response was invalid"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("bytes") != size
            or payload.get("sha256") != digest
        ):
            raise AssetTransferError("transfer_upload_failed", "the transfer receipt was invalid")
        return size, digest


def _safe_target(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    candidate = Path(os.path.abspath(candidate))
    if not candidate.parent.is_relative_to(resolved_root):
        raise AssetTransferError("transfer_target_invalid", "the transfer target escaped its root")
    current = resolved_root
    for part in candidate.relative_to(resolved_root).parts:
        current /= part
        if current.is_symlink():
            raise AssetTransferError(
                "transfer_target_invalid", "the transfer target uses a symlink"
            )
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.parent.chmod(0o700)
    return candidate


def _check_content_length(value: str | None, max_bytes: int) -> None:
    if value is None:
        return
    try:
        size = int(value)
    except ValueError as exc:
        raise AssetTransferError("transfer_size_invalid", "the transfer length is invalid") from exc
    if size <= 0 or size > max_bytes:
        raise AssetTransferError("source_size_exceeded", "the transfer is too large")


def _matches(
    size: int,
    digest: str,
    expected_size: int | None,
    expected_hash: str | None,
) -> bool:
    return (expected_size is None or size == expected_size) and (
        expected_hash is None or digest == expected_hash.lower()
    )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise AssetTransferError(
            "transfer_target_invalid", "the transfer file is unavailable"
        ) from exc
    return size, digest.hexdigest()


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as source:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

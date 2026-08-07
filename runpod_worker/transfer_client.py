"""Streaming HTTPS transfer operations for Runpod capability URLs."""

from __future__ import annotations

import hashlib
import http.client
import logging
import mimetypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .schemas import ResultUpload, SourceInput, split_capability_url

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


class TransferError(RuntimeError):
    """Raised when a capability transfer cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class DownloadedSource:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UploadedOutput:
    bytes: int
    sha256: str
    status_code: int


class TransferClient:
    """Perform bounded source GETs and streamed output PUTs."""

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        opener: Callable[..., Any] | None = None,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout = timeout
        self._opener = opener or urlopen
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def download_source(self, source: SourceInput, destination: Path) -> DownloadedSource:
        """GET a prepared MP3 into a private temporary path and verify it."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial_path = destination.with_name(f"{destination.name}.part")
        self._unlink_quietly(partial_path)
        request = Request(source.url, method="GET", headers={"Accept": "audio/mpeg"})
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with self._opener(request, timeout=self.timeout) as response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None and int(declared_length) > source.bytes:
                    raise TransferError("source response exceeds declared byte size")
                with partial_path.open("wb") as output:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        byte_count += len(chunk)
                        if byte_count > source.bytes:
                            raise TransferError("source response exceeds declared byte size")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            actual_sha256 = digest.hexdigest()
            if byte_count != source.bytes:
                raise TransferError("source byte size does not match metadata")
            if actual_sha256 != source.sha256:
                raise TransferError("source SHA-256 does not match metadata")
            partial_path.replace(destination)
            return DownloadedSource(path=destination, bytes=byte_count, sha256=actual_sha256)
        except (OSError, ValueError, TransferError) as exc:
            self._unlink_quietly(partial_path)
            if isinstance(exc, TransferError):
                raise
            raise TransferError("source download failed") from exc

    def upload_output(self, upload: ResultUpload, source_path: Path) -> UploadedOutput:
        """Stream one generated file to the signed output capability."""

        try:
            file_size = source_path.stat().st_size
        except OSError as exc:
            raise TransferError("generated output is not readable") from exc
        if not source_path.is_file() or source_path.is_symlink():
            raise TransferError("generated output is not a regular file")
        if file_size <= 0 or file_size > upload.max_bytes:
            raise TransferError("generated output exceeds the capability byte limit")

        output_sha256 = _sha256_file(source_path)

        parsed = split_capability_url(upload.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise TransferError("output capability URL is not HTTPS")
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise TransferError("output capability URL has an invalid port") from exc

        digest = hashlib.sha256()
        connection: Any | None = None
        try:
            connection = self._connection_factory(parsed.hostname, port, timeout=self.timeout)
            connection.putrequest("PUT", parsed.path + (f"?{parsed.query}" if parsed.query else ""))
            connection.putheader("Content-Type", _mime_type(source_path))
            connection.putheader("Content-Length", str(file_size))
            connection.putheader("X-ACE-Output-Bytes", str(file_size))
            connection.putheader("X-ACE-Output-SHA256", output_sha256)
            connection.endheaders()
            with source_path.open("rb") as input_file:
                while True:
                    chunk = input_file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    connection.send(chunk)
            response = connection.getresponse()
            status_code = int(response.status)
            response.read(4096)
            if not 200 <= status_code < 300:
                raise TransferError("output upload was rejected")
        except (OSError, http.client.HTTPException, TransferError) as exc:
            if isinstance(exc, TransferError):
                raise
            raise TransferError("output upload failed") from exc
        finally:
            if connection is not None:
                connection.close()
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != output_sha256:
            raise TransferError("generated output changed during upload")
        return UploadedOutput(bytes=file_size, sha256=actual_sha256, status_code=status_code)

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.debug("could not remove temporary transfer file", exc_info=True)


def _mime_type(path: Path) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
    }.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as input_file:
            while True:
                chunk = input_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise TransferError("generated output is not readable") from exc
    return digest.hexdigest()

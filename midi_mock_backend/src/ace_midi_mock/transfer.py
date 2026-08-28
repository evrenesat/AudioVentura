"""Upload rendered MP3 bytes only through a controller-supplied capability."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import MAX_RESPONSE_BYTES, MockSettings
from .renderer import RenderedOutput


class TransferError(RuntimeError):
    """Safe result-upload failure classification."""

    def __init__(self, code: str, message: str = "result upload failed") -> None:
        self.code = code
        super().__init__(message)


def validate_upload_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise TransferError("upload_url_invalid")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise TransferError("upload_url_invalid")
    return url


class SignedResultUploader:
    """Bounded async PUT uploader with no provider credentials or API calls."""

    def __init__(
        self,
        settings: MockSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=10,
                read=settings.upload_timeout_seconds,
                write=settings.upload_timeout_seconds,
                pool=10,
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _body(self, path: Path) -> AsyncIterator[bytes]:
        with path.open("rb") as source:
            while True:
                block = await asyncio.to_thread(source.read, 64 * 1024)
                if not block:
                    return
                yield block

    async def upload(self, url: str, rendered: RenderedOutput) -> None:
        validate_upload_url(url)
        try:
            stat = rendered.path.stat()
        except OSError as exc:
            raise TransferError("upload_source_missing") from exc
        if (
            rendered.path.suffix.lower() != ".mp3"
            or stat.st_size != rendered.byte_size
            or stat.st_size <= 0
            or stat.st_size > self.settings.max_output_bytes
        ):
            raise TransferError("upload_source_invalid")
        headers = {
            "content-type": "audio/mpeg",
            "content-length": str(rendered.byte_size),
            "x-ace-output-sha256": rendered.sha256,
        }
        try:
            async with self.client.stream(
                "PUT", url, headers=headers, content=self._body(rendered.path)
            ) as response:
                body_size = 0
                async for block in response.aiter_bytes():
                    body_size += len(block)
                    if body_size > MAX_RESPONSE_BYTES:
                        raise TransferError("upload_response_too_large")
                if response.status_code < 200 or response.status_code >= 300:
                    raise TransferError("upload_rejected")
        except TransferError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise TransferError("upload_unavailable") from exc

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from ace_service.artifact_store import materialize_remote_artifact, materialize_stream


def test_materialize_stream_is_bounded_atomic_and_idempotent(tmp_path: Path) -> None:
    receipt = materialize_stream(
        [b"audio", b" bytes"],
        root=tmp_path,
        target=tmp_path / "job" / "variation-01.mp3",
        max_bytes=100,
        content_type="audio/mpeg",
    )
    assert receipt.path.read_bytes() == b"audio bytes"
    assert receipt.sha256 == hashlib.sha256(b"audio bytes").hexdigest()
    assert oct(receipt.path.stat().st_mode & 0o777) == "0o600"
    assert not receipt.path.with_name(receipt.path.name + ".part").exists()
    again = materialize_stream(
        [b"ignored"],
        root=tmp_path,
        target=receipt.path,
        max_bytes=100,
        content_type="audio/mpeg",
    )
    assert again.sha256 == receipt.sha256


def test_remote_artifact_rejects_redirects_and_wrong_content_type(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("redirect"):
                return httpx.Response(302, headers={"location": "https://evil.test/file"})
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"not audio")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="not successful"):
                await materialize_remote_artifact(
                    client,
                    "https://fal.media/redirect",
                    root=tmp_path,
                    target=tmp_path / "redirect.mp3",
                    native_format="mp3",
                    max_bytes=100,
                    bearer_token="token",
                )
            with pytest.raises(ValueError, match="content type"):
                await materialize_remote_artifact(
                    client,
                    "https://fal.media/file",
                    root=tmp_path,
                    target=tmp_path / "wrong.mp3",
                    native_format="mp3",
                    max_bytes=100,
                    bearer_token="token",
                )

    asyncio.run(scenario())


def test_remote_artifact_accepts_versioned_fal_cdn_hosts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"audio")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            receipt = await materialize_remote_artifact(
                client,
                "https://v3b.fal.media/file.mp3",
                root=tmp_path,
                target=tmp_path / "versioned.mp3",
                native_format="mp3",
                max_bytes=100,
                bearer_token="token",
            )
        assert receipt.path.read_bytes() == b"audio"

    asyncio.run(scenario())

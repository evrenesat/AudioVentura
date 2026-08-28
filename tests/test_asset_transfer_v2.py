from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from ace_service.db import create_session_factory
from ace_service.models import (
    AssetTransferPurpose,
    AssetTransferStatus,
    JobType,
    SourceAsset,
    SourceAssetOrigin,
    SourceAssetStatus,
    utc_now,
)
from ace_service.repository import (
    create_project,
    create_source_asset,
    get_asset_transfer_by_token,
    issue_asset_transfer_capability,
)
from ace_service.transfers import create_transfer_app


def _issue_browser_capability(session, settings, *, max_bytes: int, expected_size: int | None):
    project = create_project(session, job_type=JobType.COVER, title="Transfer project")
    asset = create_source_asset(
        session,
        project=project,
        origin=SourceAssetOrigin.UPLOAD,
        display_title="Upload source",
        original_filename="video-with-audio.mkv",
        declared_byte_size=expected_size or 1,
        rights_confirmation_at=utc_now(),
        source_asset_id=str(uuid4()),
    )
    issued = issue_asset_transfer_capability(
        session,
        purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
        source_asset_id=asset.id,
        expected_relative_path=f"{asset.id}/source.bin",
        expected_extension=".bin",
        expected_mime_type="application/octet-stream",
        expected_byte_size=expected_size,
        max_bytes=max_bytes,
        expires_at=utc_now() + timedelta(hours=1),
    )
    session.commit()
    return asset.id, issued.token


async def _request(app, method: str, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://transfer.test") as client:
        return await client.request(method, path, **kwargs)


def test_v2_upload_is_raw_bounded_and_identical_retry_is_idempotent(session, settings) -> None:
    app = create_transfer_app(settings, session_factory=create_session_factory(session.get_bind()))
    asset_id, token = _issue_browser_capability(session, settings, max_bytes=512, expected_size=4)

    response = asyncio.run(
        _request(
            app,
            "PUT",
            f"/asset-transfer/v2/upload/{token}",
            content=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "bytes": 4,
        "sha256": hashlib.sha256(b"data").hexdigest(),
    }
    final_path = settings.paths.source_upload_final(asset_id)
    assert final_path.read_bytes() == b"data"
    with session.begin():
        capability = get_asset_transfer_by_token(session, token)
        assert capability is not None and capability.status is AssetTransferStatus.CONSUMED
        assert session.get(SourceAsset, asset_id).status is SourceAssetStatus.UPLOADED

    retry = asyncio.run(_request(app, "PUT", f"/asset-transfer/v2/upload/{token}", content=b"data"))
    assert retry.status_code == 200
    assert final_path.read_bytes() == b"data"
    assert not final_path.with_name(f".{final_path.name}.{token}.retry").exists()

    conflict = asyncio.run(
        _request(app, "PUT", f"/asset-transfer/v2/upload/{token}", content=b"other")
    )
    assert conflict.status_code == 409
    assert token not in conflict.text
    assert final_path.read_bytes() == b"data"


@pytest.mark.parametrize(
    ("payload", "content_length", "expected_status"),
    [
        (b"", None, 413),
        (b"1234", None, 413),
        (b"123", "2", 400),
    ],
)
def test_v2_upload_rejects_empty_oversize_and_false_lengths(
    session, settings, payload: bytes, content_length: str | None, expected_status: int
) -> None:
    app = create_transfer_app(settings, session_factory=create_session_factory(session.get_bind()))
    _asset_id, token = _issue_browser_capability(session, settings, max_bytes=3, expected_size=3)
    headers = {"Content-Length": content_length} if content_length is not None else {}
    response = asyncio.run(
        _request(app, "PUT", f"/asset-transfer/v2/upload/{token}", content=payload, headers=headers)
    )
    assert response.status_code == expected_status
    assert token not in response.text


def test_v2_upload_accepts_missing_content_length_and_checks_512_mib_header(
    session, settings
) -> None:
    app = create_transfer_app(settings, session_factory=create_session_factory(session.get_bind()))
    _asset_id, token = _issue_browser_capability(session, settings, max_bytes=512, expected_size=4)

    async def chunks():
        yield b"da"
        yield b"ta"

    response = asyncio.run(
        _request(app, "PUT", f"/asset-transfer/v2/upload/{token}", content=chunks())
    )
    assert response.status_code == 200

    _asset_id, large_token = _issue_browser_capability(
        session, settings, max_bytes=settings.direct_upload_max_bytes, expected_size=1
    )
    response = asyncio.run(
        _request(
            app,
            "PUT",
            f"/asset-transfer/v2/upload/{large_token}",
            content=b"x",
            headers={"Content-Length": str(settings.direct_upload_max_bytes + 1)},
        )
    )
    assert response.status_code == 413


def test_v2_download_is_verified_bounded_and_does_not_support_ranges(session, settings) -> None:
    app = create_transfer_app(settings, session_factory=create_session_factory(session.get_bind()))
    asset_id, upload_token = _issue_browser_capability(
        session, settings, max_bytes=32, expected_size=4
    )
    assert (
        asyncio.run(
            _request(app, "PUT", f"/asset-transfer/v2/upload/{upload_token}", content=b"data")
        ).status_code
        == 200
    )
    digest = hashlib.sha256(b"data").hexdigest()
    download = issue_asset_transfer_capability(
        session,
        purpose=AssetTransferPurpose.HOME_SOURCE_DOWNLOAD,
        source_asset_id=asset_id,
        expected_relative_path=f"{asset_id}/source.bin",
        expected_extension=".bin",
        expected_mime_type="application/octet-stream",
        expected_byte_size=4,
        expected_sha256=digest,
        max_bytes=32,
        expires_at=utc_now() + timedelta(hours=1),
    )
    session.commit()

    ranged = asyncio.run(
        _request(
            app,
            "GET",
            f"/asset-transfer/v2/download/{download.token}",
            headers={"Range": "bytes=0-1"},
        )
    )
    assert ranged.status_code == 416
    for _ in range(3):
        response = asyncio.run(
            _request(app, "GET", f"/asset-transfer/v2/download/{download.token}")
        )
        assert response.status_code == 200
        assert response.content == b"data"
        assert response.headers["content-type"].startswith("application/octet-stream")
        assert response.headers["content-length"] == "4"
    exhausted = asyncio.run(_request(app, "GET", f"/asset-transfer/v2/download/{download.token}"))
    assert exhausted.status_code == 404


def test_v2_capability_rejects_path_traversal_and_symlink_targets(
    session, settings, tmp_path: Path
) -> None:
    project = create_project(session, job_type=JobType.COVER, title="Unsafe transfer")
    asset = create_source_asset(
        session,
        project=project,
        origin=SourceAssetOrigin.UPLOAD,
        display_title="Unsafe source",
        original_filename="unsafe.bin",
        declared_byte_size=4,
        rights_confirmation_at=utc_now(),
    )
    with pytest.raises(ValueError, match="path"):
        issue_asset_transfer_capability(
            session,
            purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
            source_asset_id=asset.id,
            expected_relative_path=f"{asset.id}/../source.bin",
            expected_extension=".bin",
            max_bytes=4,
            expires_at=utc_now() + timedelta(hours=1),
        )
    issued = issue_asset_transfer_capability(
        session,
        purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
        source_asset_id=asset.id,
        expected_relative_path=f"{asset.id}/source.bin",
        expected_extension=".bin",
        max_bytes=4,
        expires_at=utc_now() + timedelta(hours=1),
    )
    session.commit()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"data")
    target = settings.paths.source_upload_final(asset.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside)
    app = create_transfer_app(settings, session_factory=create_session_factory(session.get_bind()))
    response = asyncio.run(
        _request(app, "PUT", f"/asset-transfer/v2/upload/{issued.token}", content=b"data")
    )
    assert response.status_code == 404
    assert response.text.find(issued.token) == -1

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from ace_home_ingest.app import create_app as create_home_app
from ace_home_ingest.config import HomeIngestSettings
from ace_home_ingest.media import IngestError, prepare_local_source
from ace_home_ingest.transfer import BoundedTransferClient

from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.home_ingest import HomeIngestClient
from ace_service.models import (
    AssetTransferPurpose,
    JobStatus,
    JobType,
    MediaFile,
    MediaFileState,
    MediaItemKind,
    OutputFormat,
    SourceAssetOrigin,
    SourceAssetStatus,
    utc_now,
)
from ace_service.repository import (
    create_project,
    create_source_asset,
    create_source_remix_job,
    get_job,
    issue_asset_transfer_capability,
)
from ace_service.schemas import CoverRequest
from ace_service.source_assets import SourceIngestCoordinator, stage_source_job
from ace_service.transfers import create_transfer_app


class _NoopUploader:
    def upload(self, local_path: Path, job_id: str) -> str:
        raise AssertionError(f"legacy uploader called for {local_path} and {job_id}")


def _make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x96:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_uploaded_video_crosses_transfer_home_and_controller_pipeline(tmp_path: Path) -> None:
    asyncio.run(_exercise_uploaded_video_pipeline(tmp_path))


def test_local_video_without_audio_is_rejected_by_home_ingest(tmp_path: Path) -> None:
    work_directory = tmp_path / "no-audio-work"
    work_directory.mkdir()
    input_path = work_directory / "silent-video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x96:rate=24",
            "-t",
            "1",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(input_path),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(IngestError, match="source does not contain an audio stream") as error:
        asyncio.run(
            prepare_local_source(
                input_path,
                work_directory,
                title="Silent video",
                max_canonical_bytes=536_870_912,
                command_timeout_seconds=120,
            )
        )
    assert error.value.code == "ffprobe_failed"


async def _exercise_uploaded_video_pipeline(tmp_path: Path) -> None:
    raw_path = tmp_path / "sample-video.mp4"
    _make_video(raw_path)
    raw_payload = raw_path.read_bytes()
    raw_digest = hashlib.sha256(raw_payload).hexdigest()

    controller_settings = ServiceSettings(
        data_root=tmp_path / "controller-data",
        service_password="controller-password",
        home_ingest_token="home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
        home_ingest_base_url="https://home.test",
        transfer_public_base_url="https://transfer.test",
        transfer_max_source_bytes=536_870_912,
        direct_upload_max_bytes=536_870_912,
        canonical_source_max_bytes=536_870_912,
    )
    engine = create_database_engine(controller_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    transfer_app = create_transfer_app(controller_settings, session_factory=factory)
    transfer_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=transfer_app),
        base_url="https://transfer.test",
    )

    home_settings = HomeIngestSettings(
        data_root=tmp_path / "home-data",
        token="home-token",
        transfer_base_url="https://transfer.test",
        max_source_bytes=536_870_912,
        canonical_source_max_bytes=536_870_912,
        command_timeout_seconds=120,
    )
    home_transfer = BoundedTransferClient(home_settings, http_client=transfer_http)
    home_app = create_home_app(
        home_settings,
        uploader=_NoopUploader(),
        transfer_client=home_transfer,
    )
    home_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=home_app),
        base_url="https://home.test",
    )
    controller_home = HomeIngestClient(controller_settings, http_client=home_http)

    try:
        with factory() as session:
            project = create_project(session, job_type=JobType.COVER, title="Uploaded source")
            asset = create_source_asset(
                session,
                project=project,
                origin=SourceAssetOrigin.UPLOAD,
                display_title="Sample video",
                original_filename="sample-video.mp4",
                declared_byte_size=len(raw_payload),
                rights_confirmation_at=utc_now(),
            )
            browser_capability = issue_asset_transfer_capability(
                session,
                purpose=AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
                source_asset_id=asset.id,
                expected_relative_path=f"{asset.id}/source.bin",
                expected_extension=".bin",
                expected_mime_type="application/octet-stream",
                expected_byte_size=len(raw_payload),
                max_bytes=controller_settings.direct_upload_max_bytes,
                expires_at=utc_now() + timedelta(hours=1),
            )
            session.commit()
            asset_id = asset.id

        upload_response = await transfer_http.put(
            f"/asset-transfer/v2/upload/{browser_capability.token}",
            content=raw_payload,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(raw_payload)),
            },
        )
        assert upload_response.status_code == 200
        assert upload_response.json() == {
            "status": "accepted",
            "bytes": len(raw_payload),
            "sha256": raw_digest,
        }

        with factory() as session:
            uploaded = session.get(type(asset), asset_id)
            assert uploaded is not None
            assert uploaded.status is SourceAssetStatus.UPLOADED
            coordinator = SourceIngestCoordinator(
                controller_settings,
                factory,
                controller_home,
            )

        await coordinator.run_once("source", asset_id)

        with factory() as session:
            ready = session.get(type(asset), asset_id)
            assert ready is not None and ready.status is SourceAssetStatus.READY
            assert ready.media_item is not None
            assert ready.media_item.kind is MediaItemKind.SOURCE
            source_file = (
                session.query(MediaFile).filter_by(media_item_id=ready.media_item.id).one()
            )
            assert source_file.format is OutputFormat.MP3
            assert source_file.state is MediaFileState.ACTIVE
            assert source_file.is_playback == 1
            assert source_file.is_primary_download == 1
            canonical_path = controller_settings.paths.source_library(asset_id)
            assert not controller_settings.paths.source_upload_final(asset_id).exists()
            source_duration = float(ready.duration_seconds)
            project_id = ready.project_id
            source_item_id = ready.media_item.id
            assert source_file.byte_size == canonical_path.stat().st_size
            assert source_file.sha256 == hashlib.sha256(canonical_path.read_bytes()).hexdigest()

            snapshot = {
                "backend_id": "mock/midi-sequential",
                "provider": "mock",
                "source_duration_min_seconds": 1,
                "source_duration_max_seconds": 600,
                "output_duration_min_seconds": 1,
                "output_duration_max_seconds": 600,
            }
            clip_start = min(0.5, max(0.0, source_duration - 1.5))
            clip_end = min(source_duration, clip_start + 1.0)
            remix = create_source_remix_job(
                session,
                CoverRequest(target_style="minimal piano", rights_confirmation=True),
                source_media_item_id=source_item_id,
                clip_start_seconds=clip_start,
                clip_end_seconds=clip_end,
                backend_snapshot_json=snapshot,
                project=project_id,
                inference_provider="mock",
                inference_backend="mock/midi-sequential",
            )
            session.commit()
            remix_id = remix.id

        assert (canonical_path).is_file()
        canonical_probe = _probe(canonical_path)
        stream = canonical_probe["streams"][0]
        assert stream["codec_name"] == "mp3"
        assert int(stream["sample_rate"]) == 48_000
        assert int(stream["channels"]) == 2
        assert float(canonical_probe["format"]["duration"]) > 0

        assert await stage_source_job(controller_settings, factory, controller_home, remix_id)
        with factory() as session:
            staged_job = get_job(session, remix_id)
            assert staged_job is not None and staged_job.status is JobStatus.STAGING
            staged_path = controller_settings.paths.job_incoming(remix_id) / "source.mp3"
            assert staged_path.is_file()
            assert staged_path.read_bytes() != b""
            assert hashlib.sha256(staged_path.read_bytes()).hexdigest() == staged_job.source_sha256
            assert staged_job.source_duration == staged_job.source_clip_duration_seconds
    finally:
        await controller_home.aclose()
        await home_http.aclose()
        await transfer_http.aclose()
        engine.dispose()

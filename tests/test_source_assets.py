from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from ace_service.db import create_session_factory
from ace_service.models import (
    AssetTransferPurpose,
    JobStatus,
    JobType,
    MediaFile,
    MediaItemKind,
    OutputFormat,
    PlaylistEntry,
    SourceAsset,
    SourceAssetOrigin,
    SourceAssetStatus,
    utc_now,
)
from ace_service.repository import (
    complete_asset_upload,
    complete_variation_attempt,
    create_original_job,
    create_output,
    create_project,
    create_source_asset,
    create_source_remix_job,
    get_derivative_task,
    issue_asset_transfer_capability,
    mark_media_item_deletion_pending,
    mark_source_preparing,
    prepare_variation_submission,
    publish_completed_variation_media,
    query_media_library,
    retry_source_asset,
    transition_variation_attempt,
)
from ace_service.schemas import CoverRequest, OriginalSongRequest
from ace_service.source_assets import (
    SourceIngestCoordinator,
    _publish_ready_derivative,
    publish_ready_source,
    stage_source_job,
)

SOURCE_ID = "123e4567-e89b-12d3-a456-426614174201"


def _publish_source(session, settings, *, duration: float = 900.0):
    project = create_project(session, job_type=JobType.COVER, title="Source project")
    asset = create_source_asset(
        session,
        project=project,
        origin=SourceAssetOrigin.YOUTUBE,
        display_title="Original source",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        youtube_video_id="abc123",
        rights_confirmation_at=utc_now(),
        source_asset_id=SOURCE_ID,
    )
    mark_source_preparing(session, asset.id)
    payload = b"canonical source mp3"
    digest = hashlib.sha256(payload).hexdigest()
    path = settings.paths.source_library(asset.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    issued = issue_asset_transfer_capability(
        session,
        purpose=AssetTransferPurpose.HOME_SOURCE_MP3_UPLOAD,
        source_asset_id=asset.id,
        expected_relative_path=f"sources/{asset.id}/source.mp3",
        expected_extension=".mp3",
        expected_mime_type="audio/mpeg",
        max_bytes=settings.canonical_source_max_bytes,
        expires_at=utc_now() + timedelta(hours=1),
    )
    complete_asset_upload(
        session,
        issued.capability.id,
        byte_size=len(payload),
        sha256=digest,
    )
    item = publish_ready_source(
        session,
        asset.id,
        settings=settings,
        duration_seconds=duration,
        canonical_byte_size=len(payload),
        canonical_sha256=digest,
    )
    session.commit()
    return project, asset, item, payload, digest


def test_source_publication_is_idempotent_and_allows_long_sources(session, settings) -> None:
    project, asset, item, _payload, _digest = _publish_source(session, settings)

    assert asset.status is SourceAssetStatus.READY
    assert asset.duration_seconds == 900.0
    assert item.id == asset.id
    assert item.kind is MediaItemKind.SOURCE
    assert len(item.files) == 1
    assert item.files[0].relative_path == f"sources/{asset.id}/source.mp3"
    assert item.files[0].is_playback == 1
    assert item.files[0].is_primary_download == 1
    session.expire(project, ["playlists"])
    assert len(project.playlists) == 1
    assert len(project.playlists[0].entries) == 1

    again = publish_ready_source(
        session,
        asset.id,
        settings=settings,
        duration_seconds=900.0,
        canonical_byte_size=item.files[0].byte_size,
        canonical_sha256=item.files[0].sha256,
    )
    session.commit()
    assert again.id == item.id
    assert session.scalar(select(PlaylistEntry).where(PlaylistEntry.media_item_id == item.id))
    assert session.query(MediaFile).filter(MediaFile.media_item_id == item.id).count() == 1


def test_source_preference_normalizes_and_survives_failure_retry_and_cascade(session) -> None:
    project = create_project(session, job_type=JobType.COVER, title="Preference project")
    asset = create_source_asset(
        session,
        project=project,
        origin=SourceAssetOrigin.YOUTUBE,
        display_title="Preferred source",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        youtube_video_id="abc123",
        rights_confirmation_at=utc_now(),
        preferred_remix_backend="mock/midi-sequential",
    )
    assert asset.preferred_remix_backend == "mock/midi-sequential"

    for status in (SourceAssetStatus.FAILED, SourceAssetStatus.CANCELLED):
        asset.status = status
        asset.error_code = "test_failure"
        asset.user_facing_error = "Retryable source state"
        session.flush()
        assert retry_source_asset(session, asset.id).preferred_remix_backend == (
            "mock/midi-sequential"
        )

    source_id = asset.id
    session.delete(project)
    session.commit()
    assert session.scalar(select(SourceAsset.id).where(SourceAsset.id == source_id)) is None


def test_source_preference_rejects_malformed_or_oversized_backend_ids(session) -> None:
    for index, backend in enumerate(("bad\nbackend", "x" * 257), start=1):
        project = create_project(session, job_type=JobType.COVER, title=f"Invalid {index}")
        with pytest.raises(ValueError, match="backend ID"):
            create_source_asset(
                session,
                project=project,
                origin=SourceAssetOrigin.YOUTUBE,
                display_title="Invalid source",
                youtube_url="https://www.youtube.com/watch?v=abc123",
                youtube_video_id="abc123",
                rights_confirmation_at=utc_now(),
                preferred_remix_backend=backend,
            )
        session.rollback()

    project = create_project(session, job_type=JobType.COVER, title="Historical source")
    historical = create_source_asset(
        session,
        project=project,
        origin=SourceAssetOrigin.YOUTUBE,
        display_title="Historical source",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        youtube_video_id="abc123",
        rights_confirmation_at=utc_now(),
    )
    assert historical.preferred_remix_backend is None


def test_source_coordinator_periodic_poll_selects_due_retry_only(session, settings) -> None:
    future_project = create_project(session, job_type=JobType.COVER, title="Future retry")
    future = create_source_asset(
        session,
        project=future_project,
        origin=SourceAssetOrigin.YOUTUBE,
        display_title="Future source",
        youtube_url="https://www.youtube.com/watch?v=future1",
        youtube_video_id="future1",
        rights_confirmation_at=utc_now(),
    )
    future.status = SourceAssetStatus.FAILED
    future.error_code = "youtube_download_failed"
    future.user_facing_error = "YouTube audio could not be downloaded"
    future.next_attempt_at = utc_now() + timedelta(minutes=5)

    due_project = create_project(session, job_type=JobType.COVER, title="Due retry")
    due = create_source_asset(
        session,
        project=due_project,
        origin=SourceAssetOrigin.YOUTUBE,
        display_title="Due source",
        youtube_url="https://www.youtube.com/watch?v=due1234",
        youtube_video_id="due1234",
        rights_confirmation_at=utc_now(),
    )
    due.status = SourceAssetStatus.FAILED
    due.error_code = "youtube_download_failed"
    due.user_facing_error = "YouTube audio could not be downloaded"
    due.next_attempt_at = utc_now() - timedelta(seconds=1)
    session.commit()

    coordinator = SourceIngestCoordinator(
        settings,
        create_session_factory(session.get_bind()),
        object(),
    )
    selected: list[str] = []

    async def record_source(source_asset_id: str) -> None:
        selected.append(source_asset_id)

    coordinator._process_source = record_source  # type: ignore[method-assign]

    assert asyncio.run(coordinator.run_once()) is True
    assert selected == [due.id]


def test_source_range_is_frozen_and_active_source_delete_is_blocked(session, settings) -> None:
    project, _asset, item, _payload, _digest = _publish_source(session, settings)
    snapshot = {
        "backend_id": "mock/midi-sequential",
        "provider": "mock",
        "source_duration_min_seconds": 1,
        "source_duration_max_seconds": 600,
        "output_duration_min_seconds": 1,
        "output_duration_max_seconds": 600,
    }
    request = CoverRequest(
        youtube_url=None,
        target_style="warm synthwave",
        rights_confirmation=True,
    )
    job = create_source_remix_job(
        session,
        request,
        source_media_item_id=item.id,
        clip_start_seconds=100,
        clip_end_seconds=700,
        backend_snapshot_json=snapshot,
        project=project.id,
        inference_provider="mock",
        inference_backend="mock/midi-sequential",
    )
    assert job.source_url is None
    assert job.source_duration == 600
    assert job.source_clip_start_seconds == 100
    assert job.source_clip_end_seconds == 700
    assert job.source_clip_duration_seconds == 600
    assert job.normalized_request_json["source_duration_seconds"] == 600
    with pytest.raises(ValueError, match="nonterminal"):
        mark_media_item_deletion_pending(session, item.id)
    job.status = JobStatus.FAILED
    session.flush()
    mark_media_item_deletion_pending(session, item.id)


def test_lossless_publication_waits_for_one_mp3_derivative_and_is_idempotent(
    session, settings
) -> None:
    job = create_original_job(
        session,
        OriginalSongRequest(description="lossless publication", output_format=OutputFormat.FLAC),
        job_id=str(uuid4()),
    )
    _job, attempt, _nonce = prepare_variation_submission(session, job.id, 1)
    transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
    payload = b"verified flac bytes"
    relative_path = f"{job.id}/variation-01.flac"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=0,
        relative_path=relative_path,
        mime_type="audio/flac",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    complete_variation_attempt(session, attempt.id)
    published = publish_completed_variation_media(session, job.id, 1)
    session.commit()
    assert len(published) == 1
    item = published[0]
    task = get_derivative_task(session, item.derivative_task.id)
    assert task is not None and task.status == "pending"
    assert query_media_library(session).items == ()
    assert item.files[0].format is OutputFormat.FLAC
    assert item.files[0].is_primary_download == 1

    derivative_payload = b"verified playback mp3"
    derivative_digest = hashlib.sha256(derivative_payload).hexdigest()
    derivative_path = settings.paths.generated_playback(item.id)
    derivative_path.parent.mkdir(parents=True, exist_ok=True)
    derivative_path.write_bytes(derivative_payload)
    issued = issue_asset_transfer_capability(
        session,
        purpose=AssetTransferPurpose.HOME_DERIVATIVE_UPLOAD,
        derivative_task_id=task.id,
        expected_relative_path=f"generated/{item.id}/playback.mp3",
        expected_extension=".mp3",
        expected_mime_type="audio/mpeg",
        max_bytes=settings.canonical_source_max_bytes,
        expires_at=utc_now() + timedelta(hours=1),
    )
    complete_asset_upload(
        session,
        issued.capability.id,
        byte_size=len(derivative_payload),
        sha256=derivative_digest,
    )
    _publish_ready_derivative(
        session,
        task.id,
        settings=settings,
        byte_size=len(derivative_payload),
        sha256=derivative_digest,
        duration_seconds=12.0,
    )
    session.commit()
    assert len(query_media_library(session).items) == 1
    session.expire(item.project, ["playlists"])
    assert len(item.project.playlists[0].entries) == 1

    assert (
        _publish_ready_derivative(
            session,
            task.id,
            settings=settings,
            byte_size=len(derivative_payload),
            sha256=derivative_digest,
            duration_seconds=12.0,
        ).id
        == task.output_media_file_id
    )
    session.commit()
    assert session.query(MediaFile).filter(MediaFile.media_item_id == item.id).count() == 2
    assert session.query(PlaylistEntry).filter(PlaylistEntry.media_item_id == item.id).count() == 1


def test_retryable_full_source_stage_copies_only_verified_mp3(session, settings) -> None:
    project, _asset, item, payload, digest = _publish_source(session, settings, duration=30.0)
    request = CoverRequest(target_style="minimal piano", rights_confirmation=True)
    job = create_source_remix_job(
        session,
        request,
        source_media_item_id=item.id,
        clip_start_seconds=0,
        clip_end_seconds=30,
        backend_snapshot_json={
            "backend_id": "runpod/ace-step-v15-xl-turbo",
            "provider": "runpod",
            "source_duration_min_seconds": 1,
            "source_duration_max_seconds": 600,
            "output_duration_min_seconds": 1,
            "output_duration_max_seconds": 600,
        },
        project=project.id,
        inference_provider="runpod",
        inference_backend="runpod/ace-step-v15-xl-turbo",
    )
    session.commit()

    class NoHomeCall:
        async def prepare_clip_v2(self, **_kwargs):
            raise AssertionError("full-range staging must not call Home Ingest")

    factory = create_session_factory(session.get_bind())
    assert asyncio.run(stage_source_job(settings, factory, NoHomeCall(), job.id)) is True
    assert (settings.paths.incoming / job.id / "source.mp3").read_bytes() == payload
    assert (
        hashlib.sha256((settings.paths.incoming / job.id / "source.mp3").read_bytes()).hexdigest()
        == digest
    )
    session.expire_all()
    assert session.get(type(job), job.id).status is JobStatus.STAGING

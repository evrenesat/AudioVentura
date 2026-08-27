from __future__ import annotations

import hashlib

import pytest

from ace_service.media_library import MediaLibraryError, MediaLibraryService, verify_media_file
from ace_service.models import JobStatus, MediaDeletionState, MediaFileState, OutputFormat
from ace_service.repository import (
    MediaLibraryQuery,
    create_original_job,
    create_output,
    get_media_item,
    publish_completed_variation_media,
    query_media_library,
)
from ace_service.schemas import OriginalSongRequest


def _publish_track(session, settings, *, job_id: str, title: str = "A song"):
    job = create_original_job(
        session,
        OriginalSongRequest(description=f"ambient composition {job_id.replace('second', 'beta')}"),
        job_id=job_id,
    )
    job.status = JobStatus.COMPLETED
    payload = f"valid mp3 for {job_id}".encode()
    relative_path = f"{job.id}/variation-01.mp3"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    output = create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=0,
        relative_path=relative_path,
        mime_type="audio/mpeg",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.commit()
    published = publish_completed_variation_media(session, job.id, 1)
    session.commit()
    return job, output, published[0]


def test_successful_mp3_is_published_once_with_project_playlist(session, settings) -> None:
    job, output, item = _publish_track(session, settings, job_id="library-job")

    assert item.generated_output_id == output.id
    assert item.title == f"{job.project.title} · Variation 1"
    assert len(item.files) == 1
    assert item.files[0].format is OutputFormat.MP3
    assert item.files[0].is_playback == 1
    assert item.files[0].is_primary_download == 1
    assert len(job.project.playlists) == 1
    assert len(job.project.playlists[0].entries) == 1

    assert publish_completed_variation_media(session, job.id, 1) == []
    session.commit()
    assert session.query(type(item)).count() == 1
    assert session.query(type(job.project.playlists[0].entries[0])).count() == 1


def test_failed_job_and_non_mp3_output_never_enter_library(session, settings) -> None:
    failed_job = create_original_job(
        session,
        OriginalSongRequest(description="failed"),
        job_id="failed-library-job",
    )
    failed_job.status = JobStatus.FAILED
    session.commit()
    assert publish_completed_variation_media(session, failed_job.id, 1) == []

    job = create_original_job(
        session,
        OriginalSongRequest(description="lossless"),
        job_id="wav-library-job",
    )
    job.status = JobStatus.COMPLETED
    payload = b"valid wav"
    relative_path = f"{job.id}/variation-01.wav"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=0,
        relative_path=relative_path,
        mime_type="audio/wav",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.commit()

    assert publish_completed_variation_media(session, job.id, 1) == []
    assert query_media_library(session).items == ()


def test_library_query_is_bounded_and_excludes_deleted_items(session, settings) -> None:
    _, _, first = _publish_track(session, settings, job_id="query-first", title="First")
    _, _, second = _publish_track(session, settings, job_id="query-second", title="Second")
    assert [item.id for item in query_media_library(session).items] == [second.id, first.id]
    assert [
        item.id for item in query_media_library(session, query=MediaLibraryQuery(q="first")).items
    ] == [first.id]
    with pytest.raises(ValueError, match="200"):
        query_media_library(session, MediaLibraryQuery(q="x" * 201))

    first.deletion_state = MediaDeletionState.DELETED
    assert [item.id for item in query_media_library(session).items] == [second.id]


def test_media_verification_refuses_symlink_and_metadata_mismatch(session, settings) -> None:
    _, _, item = _publish_track(session, settings, job_id="verify-library-job")
    media_file = item.files[0]
    outside = settings.data_root.parent / "outside.mp3"
    outside.write_bytes(b"outside")
    source = settings.paths.outputs / media_file.relative_path
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(MediaLibraryError):
        verify_media_file(settings, media_file)

    source.unlink()
    source.write_bytes(b"changed")
    with pytest.raises(MediaLibraryError):
        verify_media_file(settings, media_file)


def test_deletion_reconciliation_tombstones_and_purges_media(session, settings) -> None:
    _, _, item = _publish_track(session, settings, job_id="delete-library-job")
    service = MediaLibraryService(settings, lambda: session)
    service.request_item_deletion(item.id)
    assert get_media_item(session, item.id).deletion_state is MediaDeletionState.PENDING
    assert service.reconcile_item_deletion(item.id)
    session.expire_all()
    deleted = get_media_item(session, item.id)
    assert deleted is not None and deleted.deletion_state is MediaDeletionState.DELETED
    assert deleted.files[0].state is MediaFileState.PURGED
    assert not (settings.paths.outputs / deleted.files[0].relative_path).exists()

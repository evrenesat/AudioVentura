from __future__ import annotations

import hashlib

from ace_service.models import JobStatus
from ace_service.repository import (
    add_playlist_entry,
    complete_variation_attempt,
    create_custom_playlist,
    create_original_job,
    create_output,
    delete_playlist,
    ensure_project_playlist,
    list_playlist_entries,
    prepare_variation_submission,
    publish_completed_variation_media,
    remove_playlist_entry,
    rename_playlist,
    reorder_playlist_entries,
    transition_variation_attempt,
)
from ace_service.schemas import OriginalSongRequest
from tests.test_media_library import _publish_track


def test_custom_playlist_allows_cross_project_duplicates_and_reorder(session, settings) -> None:
    first_job, _, first = _publish_track(session, settings, job_id="playlist-first", title="First")
    second_job, _, second = _publish_track(
        session, settings, job_id="playlist-second", title="Second"
    )
    playlist = create_custom_playlist(session, "Mixes")
    first_entry = add_playlist_entry(session, playlist.id, first.id)
    duplicate_entry = add_playlist_entry(session, playlist.id, first.id)
    second_entry = add_playlist_entry(session, playlist.id, second.id)
    session.commit()

    assert {first.project_id, second.project_id} == {first_job.project_id, second_job.project_id}
    assert [entry.position for entry in list_playlist_entries(session, playlist.id)] == [
        1024,
        2048,
        3072,
    ]
    reordered = reorder_playlist_entries(
        session, playlist.id, [second_entry.id, first_entry.id, duplicate_entry.id]
    )
    session.commit()
    assert [entry.id for entry in reordered] == [
        second_entry.id,
        first_entry.id,
        duplicate_entry.id,
    ]
    remove_playlist_entry(session, playlist.id, duplicate_entry.id)
    session.commit()
    assert [entry.media_item_id for entry in list_playlist_entries(session, playlist.id)] == [
        second.id,
        first.id,
    ]


def test_project_playlist_recreation_does_not_backfill_old_outputs(session, settings) -> None:
    job, _, old_item = _publish_track(session, settings, job_id="auto-old", title="Old")
    old_playlist = ensure_project_playlist(session, job.project_id)
    assert [entry.media_item_id for entry in old_playlist.entries] == [old_item.id]
    delete_playlist(session, old_playlist.id)
    recreated = ensure_project_playlist(session, job.project_id)
    assert recreated.id != old_playlist.id
    assert recreated.entries == []

    job.status = JobStatus.COMPLETED
    payload = b"new mp3"
    relative_path = f"{job.id}/variation-02.mp3"
    path = settings.paths.outputs / relative_path
    path.write_bytes(payload)
    create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=1,
        relative_path=relative_path,
        mime_type="audio/mpeg",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.commit()
    published = publish_completed_variation_media(session, job.id, 1)
    session.commit()
    assert len(published) == 1
    session.expire(recreated, ["entries"])
    assert [entry.media_item_id for entry in recreated.entries] == [published[0].id]


def test_project_playlist_keeps_edit_operations_and_automatic_publication_append(
    session, settings
) -> None:
    job, _, first = _publish_track(session, settings, job_id="auto-edit-first", title="First")
    playlist = ensure_project_playlist(session, job.project_id)
    first_entry = list_playlist_entries(session, playlist.id)[0]
    duplicate_entry = add_playlist_entry(session, playlist.id, first.id)
    rename_playlist(session, playlist.id, "Renamed project playlist")
    reorder_playlist_entries(session, playlist.id, [duplicate_entry.id, first_entry.id])
    session.commit()

    second = create_original_job(
        session,
        OriginalSongRequest(description="later automatic version"),
        job_id="auto-edit-second",
        project=job.project_id,
    )
    _, attempt, _ = prepare_variation_submission(session, second.id, 1)
    transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
    payload = b"second automatic mp3"
    relative_path = f"{second.id}/variation-01.mp3"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    create_output(
        session,
        job_id=second.id,
        variation_index=1,
        result_index=0,
        relative_path=relative_path,
        mime_type="audio/mpeg",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    complete_variation_attempt(session, attempt.id)
    session.commit()
    published = publish_completed_variation_media(session, second.id, 1)
    session.commit()

    entries = list_playlist_entries(session, playlist.id)
    assert len(published) == 1
    assert playlist.title == "Renamed project playlist"
    assert [entry.id for entry in entries[:2]] == [duplicate_entry.id, first_entry.id]
    assert entries[2].media_item_id == published[0].id

from __future__ import annotations

import hashlib

from ace_service.models import JobStatus
from ace_service.repository import (
    add_playlist_entry,
    create_custom_playlist,
    create_output,
    delete_playlist,
    ensure_project_playlist,
    list_playlist_entries,
    publish_completed_variation_media,
    remove_playlist_entry,
    reorder_playlist_entries,
)
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

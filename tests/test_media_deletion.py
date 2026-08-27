from __future__ import annotations

import pytest

from ace_service.media_library import MediaLibraryError, MediaLibraryService
from ace_service.models import MediaDeletionState, MediaFileState
from ace_service.repository import get_media_item
from tests.test_media_library import _publish_track


def test_failed_move_keeps_media_pending_and_retries_safely(session, settings) -> None:
    _, _, item = _publish_track(session, settings, job_id="pending-delete")
    media_file = item.files[0]
    source = settings.paths.outputs / media_file.relative_path
    outside = settings.data_root.parent / "pending-outside.mp3"
    outside.write_bytes(b"outside")
    source.unlink()
    source.symlink_to(outside)
    service = MediaLibraryService(settings, lambda: session)
    service.request_item_deletion(item.id)
    with pytest.raises(MediaLibraryError):
        service.reconcile_item_deletion(item.id)
    session.expire_all()
    pending = get_media_item(session, item.id)
    assert pending is not None and pending.deletion_state is MediaDeletionState.PENDING
    assert pending.files[0].state is MediaFileState.ACTIVE


def test_reconciliation_removes_all_shared_playlist_references(session, settings) -> None:
    _, _, item = _publish_track(session, settings, job_id="shared-delete")
    playlist = item.project.playlists[0]
    from ace_service.repository import add_playlist_entry

    add_playlist_entry(session, playlist.id, item.id)
    session.commit()
    service = MediaLibraryService(settings, lambda: session)
    service.request_item_deletion(item.id)
    service.reconcile_item_deletion(item.id)
    session.expire_all()
    assert get_media_item(session, item.id).playlist_entries == []

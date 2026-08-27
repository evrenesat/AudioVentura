from __future__ import annotations

import pytest

from ace_service.media_library import MediaLibraryService
from ace_service.models import ProjectDeletionAudit
from ace_service.repository import (
    add_playlist_entry,
    create_custom_playlist,
    create_project_deletion_audit,
    delete_project_record,
    get_project,
    project_is_deletable,
)
from tests.test_media_library import _publish_track


def test_active_project_cannot_be_deleted(session) -> None:
    from ace_service.models import JobType
    from ace_service.repository import create_job

    job = create_job(session, job_type=JobType.ORIGINAL, job_id="active-project")
    with pytest.raises(ValueError, match="nonterminal"):
        create_project_deletion_audit(session, job.project_id)
    assert not project_is_deletable(session, job.project_id)


def test_project_delete_retains_only_redacted_audit_and_custom_playlist(session, settings) -> None:
    job, _, item = _publish_track(session, settings, job_id="deletable-project")
    custom = create_custom_playlist(session, "Keep this playlist")
    add_playlist_entry(session, custom.id, item.id)
    session.commit()
    project_id = job.project_id
    audit = create_project_deletion_audit(
        session,
        project_id,
        cost_summary_json={
            "quoted_amount_micro_usd": 12,
            "prompt": "must not survive",
            "total_micro_usd": 34,
        },
    )
    assert audit.cost_summary_json == {
        "quoted_amount_micro_usd": 12,
        "total_micro_usd": 34,
    }
    session.commit()

    service = MediaLibraryService(settings, lambda: session)
    service.request_item_deletion(item.id)
    service.reconcile_item_deletion(item.id)
    session.expire_all()
    delete_project_record(session, project_id)
    session.commit()
    session.expire_all()

    assert get_project(session, project_id) is None
    assert session.get(ProjectDeletionAudit, audit.id) is not None
    assert session.get(type(custom), custom.id) is not None
    assert session.get(type(custom), custom.id).entries == []

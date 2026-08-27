from __future__ import annotations

import hashlib

import pytest

from ace_service.cleanup import cleanup_controller
from ace_service.db import create_session_factory
from ace_service.media_library import MediaLibraryService
from ace_service.models import JobStatus, OutputFormat, ProjectDeletionAudit
from ace_service.repository import (
    add_playlist_entry,
    create_custom_playlist,
    create_original_job,
    create_output,
    create_project_deletion_audit,
    get_project,
    project_is_deletable,
)
from ace_service.schemas import OriginalSongRequest
from tests.test_media_library import _publish_track


def _create_unpublished_output(
    session, settings, project_id: str, *, job_id: str, format_value: str
):
    output_format = OutputFormat(format_value)
    job = create_original_job(
        session,
        OriginalSongRequest(
            description=f"unpublished {format_value}",
            output_format=output_format,
        ),
        project=project_id,
        job_id=job_id,
    )
    job.status = JobStatus.COMPLETED
    payload = f"unpublished {format_value} output".encode()
    relative_path = f"{job.id}/variation-01.{format_value}"
    path = settings.paths.outputs / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    output = create_output(
        session,
        job_id=job.id,
        variation_index=1,
        result_index=0,
        relative_path=relative_path,
        mime_type={
            "mp3": "audio/mpeg",
            "flac": "audio/flac",
            "wav": "audio/wav",
        }[format_value],
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    return job, output, path


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

    factory = create_session_factory(session.get_bind())
    service = MediaLibraryService(settings, factory)
    assert service.reconcile_project_deletion(project_id)

    with factory() as check:
        assert get_project(check, project_id) is None
        assert check.get(ProjectDeletionAudit, audit.id) is not None
        assert check.get(type(custom), custom.id) is not None
        assert check.get(type(custom), custom.id).entries == []


def test_project_delete_removes_unpublished_legacy_and_lossless_outputs(session, settings) -> None:
    first = create_original_job(
        session,
        OriginalSongRequest(description="legacy MP3 project"),
        job_id="legacy-mp3-project",
    )
    first.status = JobStatus.COMPLETED
    _, _, mp3_path = _create_unpublished_output(
        session, settings, first.project_id, job_id="legacy-mp3-output", format_value="mp3"
    )
    _, _, flac_path = _create_unpublished_output(
        session, settings, first.project_id, job_id="legacy-flac-output", format_value="flac"
    )
    _, _, wav_path = _create_unpublished_output(
        session, settings, first.project_id, job_id="legacy-wav-output", format_value="wav"
    )
    project_id = first.project_id
    create_project_deletion_audit(session, project_id)
    session.commit()

    factory = create_session_factory(session.get_bind())
    service = MediaLibraryService(settings, factory)
    assert service.reconcile_project_deletion(project_id)

    assert not mp3_path.exists()
    assert not flac_path.exists()
    assert not wav_path.exists()
    assert not (settings.paths.trash / "project-outputs" / project_id).exists()
    assert not (settings.paths.outputs / "legacy-mp3-output").exists()
    assert not (settings.paths.outputs / "legacy-flac-output").exists()
    assert not (settings.paths.outputs / "legacy-wav-output").exists()
    with factory() as check:
        assert get_project(check, project_id) is None
        assert check.query(ProjectDeletionAudit).filter_by(project_id=project_id).one()


def test_project_output_deletion_retries_after_partial_quarantine(
    session, settings, monkeypatch
) -> None:
    first = create_original_job(
        session,
        OriginalSongRequest(description="retryable project deletion"),
        job_id="retryable-project",
    )
    first.status = JobStatus.COMPLETED
    _, _, first_path = _create_unpublished_output(
        session, settings, first.project_id, job_id="retryable-output-1", format_value="mp3"
    )
    _, _, second_path = _create_unpublished_output(
        session, settings, first.project_id, job_id="retryable-output-2", format_value="wav"
    )
    project_id = first.project_id
    create_project_deletion_audit(session, project_id)
    session.commit()

    import ace_service.media_library as media_library

    original_rename = media_library.os.rename
    calls = 0

    def fail_second_rename(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected output quarantine failure")
        return original_rename(source, destination)

    monkeypatch.setattr(media_library.os, "rename", fail_second_rename)
    factory = create_session_factory(session.get_bind())
    service = MediaLibraryService(settings, factory)
    with pytest.raises(media_library.MediaLibraryError):
        service.reconcile_project_deletion(project_id)

    assert not first_path.exists()
    assert second_path.exists()
    with factory() as check:
        assert get_project(check, project_id) is not None

    monkeypatch.setattr(media_library.os, "rename", original_rename)
    report = cleanup_controller(settings, factory)
    assert report.reconciled_project_deletions == 1
    assert not first_path.exists()
    assert not second_path.exists()
    assert not (settings.paths.trash / "project-outputs" / project_id).exists()
    with factory() as check:
        assert get_project(check, project_id) is None

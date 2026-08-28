from __future__ import annotations

import uuid

import pytest

from ace_midi_mock.db import MockDatabase, SubmissionConflict


def test_nonce_claim_is_idempotent_and_wraps(fixture_manifest, mock_settings) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    application_id = str(uuid.uuid4())
    urls = [f"https://transfer.test/{index}" for index in range(5)]
    jobs = [
        database.claim_submission(
            application_job_id=application_id,
            variation_index=1,
            submission_nonce=str(uuid.uuid4()),
            result_upload_url=url,
        )[0]
        for url in urls[:4]
    ]
    assert [job.corpus_index for job in jobs] == [0, 1, 2, 0]
    replay, created = database.claim_submission(
        application_job_id=application_id,
        variation_index=1,
        submission_nonce=jobs[0].submission_nonce,
        result_upload_url=urls[0],
    )
    assert not created
    assert replay.external_uuid == jobs[0].external_uuid
    assert database.cursor_snapshot()["last_consumed_index"] == 0


def test_nonce_conflict_does_not_advance_cursor(fixture_manifest, mock_settings) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    nonce = str(uuid.uuid4())
    database.claim_submission(
        application_job_id=str(uuid.uuid4()),
        variation_index=1,
        submission_nonce=nonce,
        result_upload_url="https://transfer.test/one",
    )
    with pytest.raises(SubmissionConflict):
        database.claim_submission(
            application_job_id=str(uuid.uuid4()),
            variation_index=1,
            submission_nonce=nonce,
            result_upload_url="https://transfer.test/two",
        )
    assert database.cursor_snapshot()["last_consumed_index"] == 0


def test_running_jobs_requeue_without_changing_claim(fixture_manifest, mock_settings) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    job, _ = database.claim_submission(
        application_job_id=str(uuid.uuid4()),
        variation_index=2,
        submission_nonce=str(uuid.uuid4()),
        result_upload_url="https://transfer.test/result",
    )
    database.mark_running(job.external_uuid)
    recovered = database.recover_running_jobs()
    assert recovered[0].external_uuid == job.external_uuid
    assert recovered[0].corpus_index == 0
    assert database.cursor_snapshot()["last_consumed_index"] == 0


def test_upload_capability_change_conflicts_without_advancing_cursor(
    fixture_manifest, mock_settings
) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    nonce = str(uuid.uuid4())
    database.claim_submission(
        application_job_id=str(uuid.uuid4()),
        variation_index=1,
        submission_nonce=nonce,
        result_upload_url="https://transfer.test/result",
        result_upload_max_bytes=1000,
    )
    with pytest.raises(SubmissionConflict):
        database.claim_submission(
            application_job_id=str(uuid.uuid4()),
            variation_index=1,
            submission_nonce=nonce,
            result_upload_url="https://transfer.test/result",
            result_upload_max_bytes=2000,
        )
    assert database.cursor_snapshot()["last_consumed_index"] == 0

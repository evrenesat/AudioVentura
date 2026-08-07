from __future__ import annotations

import pytest

from ace_service.models import JobStatus, JobType
from ace_service.repository import (
    create_job,
    create_variation_attempt,
    get_variation_attempt,
    transition_job,
    transition_variation_attempt,
)
from ace_service.state import (
    ControllerAlreadyRunning,
    ControllerLock,
    InvalidJobTransition,
    validate_job_transition,
)


def test_parent_state_machine_rejects_forbidden_edges(session) -> None:
    job = create_job(session, job_type=JobType.ORIGINAL)
    transition_job(session, job.id, JobStatus.CLOUD_QUEUED)
    transition_job(session, job.id, JobStatus.GENERATING)
    transition_job(session, job.id, JobStatus.COMPLETED)
    with pytest.raises(InvalidJobTransition):
        transition_job(session, job.id, JobStatus.FAILED)

    validate_job_transition(JobStatus.QUEUED, JobStatus.QUEUED)


def test_variation_attempt_has_its_own_serialized_state(session) -> None:
    job = create_job(session, job_type=JobType.ORIGINAL, variation_count=2)
    first = create_variation_attempt(session, job_id=job.id, variation_index=1)
    second = create_variation_attempt(session, job_id=job.id, variation_index=2)
    transition_variation_attempt(session, first.id, JobStatus.CLOUD_QUEUED)
    transition_variation_attempt(session, first.id, JobStatus.GENERATING)
    transition_variation_attempt(session, first.id, JobStatus.COMPLETED)
    assert get_variation_attempt(session, job.id, 1) is not None
    assert first.status is JobStatus.COMPLETED
    assert second.status is JobStatus.QUEUED


def test_data_root_lock_rejects_second_owner(tmp_path) -> None:
    first = ControllerLock(tmp_path)
    second = ControllerLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ControllerAlreadyRunning):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()

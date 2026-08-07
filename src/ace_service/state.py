"""Controller lifecycle rules and the single-process ownership lock."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from ace_service.models import JobStatus

ALLOWED_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.INGESTING, JobStatus.CLOUD_QUEUED, JobStatus.FAILED}),
    JobStatus.INGESTING: frozenset({JobStatus.STAGING, JobStatus.FAILED}),
    JobStatus.STAGING: frozenset({JobStatus.CLOUD_QUEUED, JobStatus.FAILED}),
    JobStatus.CLOUD_QUEUED: frozenset(
        {JobStatus.GENERATING, JobStatus.COMPLETED, JobStatus.FAILED}
    ),
    JobStatus.GENERATING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}

ALLOWED_VARIATION_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.CLOUD_QUEUED, JobStatus.FAILED}),
    JobStatus.CLOUD_QUEUED: frozenset(
        {JobStatus.GENERATING, JobStatus.COMPLETED, JobStatus.FAILED}
    ),
    JobStatus.GENERATING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class InvalidJobTransition(ValueError):
    """Raised when a persisted job lifecycle transition is not permitted."""


class ControllerAlreadyRunning(RuntimeError):
    """Raised when another controller owns the configured data root."""


def validate_job_transition(current: JobStatus, target: JobStatus) -> None:
    """Validate one explicit parent-job lifecycle transition."""

    if current is target:
        return
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise InvalidJobTransition(f"cannot transition job from {current.value} to {target.value}")


def validate_variation_transition(current: JobStatus, target: JobStatus) -> None:
    """Validate one individual variation attempt transition."""

    if current is target:
        return
    if target not in ALLOWED_VARIATION_TRANSITIONS[current]:
        raise InvalidJobTransition(
            f"cannot transition variation from {current.value} to {target.value}"
        )


class ControllerLock:
    """POSIX advisory lock held for the lifetime of one controller process."""

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / "controller.lock"
        self._file: TextIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("controller lock is already held by this instance")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            file.close()
            raise ControllerAlreadyRunning("another controller owns this data root") from exc
        os.chmod(self.path, 0o600)
        self._file = file

    def release(self) -> None:
        file = self._file
        self._file = None
        if file is None:
            return
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


@contextmanager
def controller_lock(data_root: Path) -> Iterator[ControllerLock]:
    """Acquire and release the controller lock around a synchronous scope."""

    lock = ControllerLock(data_root)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()

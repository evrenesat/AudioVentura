"""One-process durable controller worker for serialized Runpod jobs."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.models import (
    Job,
    JobStatus,
    JobType,
    Output,
    TransferDirection,
    VariationAttempt,
)
from ace_service.repository import (
    complete_variation_attempt,
    create_variation_attempt,
    fail_variation_attempt,
    get_job,
    get_variation_attempt,
    persist_variation_runpod_job_id,
    prepare_variation_submission,
    recover_uncertain_submissions,
    recover_uncertain_variation_submissions,
    set_variation_runpod_result,
    transition_job,
    transition_variation_attempt,
)
from ace_service.runpod_client import RunpodState, RunpodStatusResult
from ace_service.schemas import normalize_extension, resolve_relative_path, validate_sha256
from ace_service.state import ControllerLock
from ace_service.transfers import issue_transfer_url


class RunpodWorkerClient(Protocol):
    async def submit(
        self, payload: Mapping[str, Any], execution_timeout_ms: int, ttl_ms: int
    ) -> str: ...

    async def status(self, runpod_job_id: str) -> RunpodStatusResult: ...


PayloadBuilder = Callable[[Job, VariationAttempt], Mapping[str, Any]]


class ControllerWorker:
    """Own one durable queue and one serialized Runpod orchestration loop."""

    def __init__(
        self,
        settings: ServiceSettings,
        session_factory: SessionFactory,
        runpod_client: RunpodWorkerClient,
        *,
        payload_builder: PayloadBuilder | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.runpod_client = runpod_client
        self.payload_builder = payload_builder or self._default_payload
        self.poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.runpod_poll_interval_seconds
        )
        if self.poll_interval_seconds < 0:
            raise ValueError("poll interval must not be negative")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueued: set[str] = set()
        self._processing_lock = asyncio.Lock()
        self._controller_lock: ControllerLock | None = None
        self._task: asyncio.Task[None] | None = None
        self._accepting = False

    @property
    def queue(self) -> asyncio.Queue[str]:
        """Expose the one worker queue for diagnostics and white-box tests."""

        return self._queue

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def start(self) -> None:
        """Acquire ownership, recover durable state, then accept new jobs."""

        if self._task is not None:
            raise RuntimeError("controller worker is already started")
        self.settings.ensure_data_layout()
        lock = ControllerLock(self.settings.paths.root)
        lock.acquire()
        self._controller_lock = lock
        try:
            self._recover_on_startup()
            self._accepting = True
            self._task = asyncio.create_task(self._run(), name="ace-controller-worker")
        except BaseException:
            self._accepting = False
            lock.release()
            self._controller_lock = None
            raise

    async def stop(self) -> None:
        """Cancel the queue task and release the data-root ownership lock."""

        self._accepting = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        lock = self._controller_lock
        self._controller_lock = None
        if lock is not None:
            lock.release()

    async def __aenter__(self) -> ControllerWorker:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.stop()

    def enqueue(self, job_id: str) -> bool:
        """Enqueue a job once; return false for an in-process duplicate."""

        if not self._accepting:
            raise RuntimeError("controller worker is not accepting jobs")
        normalized_id = str(job_id)
        if normalized_id in self._enqueued:
            return False
        self._enqueued.add(normalized_id)
        self._queue.put_nowait(normalized_id)
        return True

    async def wait_idle(self) -> None:
        """Wait until the queue has no currently scheduled work."""

        await self._queue.join()

    async def process_job(self, job_id: str) -> None:
        """Process one durable queue item without allowing task errors to escape."""

        async with self._processing_lock:
            try:
                await self._process_one(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._persist_task_failure(job_id, exc)

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            self._enqueued.discard(job_id)
            try:
                await self.process_job(job_id)
                if self._job_needs_poll(job_id) and self._accepting:
                    if self.poll_interval_seconds:
                        await asyncio.sleep(self.poll_interval_seconds)
                    self.enqueue(job_id)
            except asyncio.CancelledError:
                raise
            finally:
                self._queue.task_done()

    async def _process_one(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                return
            status = job.status
            variation_index = job.current_variation or 1

        if status is JobStatus.QUEUED:
            if job.job_type is JobType.COVER:
                with self.session_factory() as session:
                    transition_job(session, job_id, JobStatus.INGESTING)
                    session.commit()
                return
            await self._submit_variation(job_id, variation_index)
            return

        if status is JobStatus.INGESTING:
            # A home-ingest agent owns this state during normal operation. On
            # restart, _recover_on_startup advances it only after validation.
            return

        if status is JobStatus.STAGING:
            if not self._incoming_source_is_valid(job_id):
                raise ValueError("staged cover source is missing or invalid")
            await self._submit_variation(job_id, variation_index)
            return

        if status in {JobStatus.CLOUD_QUEUED, JobStatus.GENERATING}:
            with self.session_factory() as session:
                attempt = get_variation_attempt(session, job_id, variation_index)
                variation_is_queued = attempt is not None and attempt.status is JobStatus.QUEUED
            if variation_is_queued:
                await self._submit_variation(job_id, variation_index)
                return
            await self._poll_variation(job_id, variation_index)

    async def _submit_variation(self, job_id: str, variation_index: int) -> None:
        with self.session_factory() as session:
            job, attempt, nonce = prepare_variation_submission(session, job_id, variation_index)
            payload = dict(self.payload_builder(job, attempt))
            if job.job_type is JobType.ORIGINAL:
                output_path = self._output_relative_path(job, attempt.variation_index)
                issued = issue_transfer_url(
                    session,
                    self.settings,
                    job_id=job.id,
                    direction=TransferDirection.OUTPUT_UPLOAD,
                    expected_relative_path=output_path,
                    expected_extension=job.output_format.value,
                    max_bytes=self.settings.transfer_max_output_bytes,
                )
                payload["result_upload"] = {
                    "url": issued.url,
                    "max_bytes": issued.capability.max_bytes,
                }
            payload["submission_nonce"] = nonce
            session.commit()

        # The nonce-only commit is the no-duplicate boundary. Any exception
        # below is persisted as an uncertain submission by the task handler.
        runpod_job_id = await self.runpod_client.submit(
            payload,
            execution_timeout_ms=self.settings.runpod_execution_timeout_ms,
            ttl_ms=self.settings.runpod_job_ttl_ms,
        )
        with self.session_factory() as session:
            persist_variation_runpod_job_id(
                session,
                attempt.id,
                runpod_job_id,
                submission_nonce=nonce,
            )
            session.commit()

    async def _poll_variation(self, job_id: str, variation_index: int) -> None:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            if job is None or attempt is None:
                raise ValueError("active job has no durable variation attempt")
            runpod_job_id = attempt.runpod_job_id or job.current_runpod_job_id
            if not runpod_job_id:
                if attempt.submission_nonce or job.current_submission_nonce:
                    raise ValueError("uncertain cloud submission has no Runpod job ID")
                raise ValueError("active job is missing its Runpod job ID")

        try:
            result = await self.runpod_client.status(runpod_job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A durable output is stronger evidence than expired Runpod status
            # after controller downtime, but only when every local invariant is
            # independently verified.
            with self.session_factory() as session:
                if self._valid_output(session, job_id, variation_index):
                    complete_variation_attempt(
                        session,
                        attempt.id,
                        note="runpod_status_unavailable_after_output",
                    )
                    session.commit()
                    return
            raise

        with self.session_factory() as session:
            attempt = get_variation_attempt(session, job_id, variation_index)
            job = get_job(session, job_id)
            if attempt is None or job is None:
                raise ValueError("polled job no longer has a durable variation attempt")
            if result.category is RunpodState.CLOUD_QUEUED:
                session.commit()
                return
            if result.category is RunpodState.GENERATING:
                transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
                if job.status is JobStatus.CLOUD_QUEUED:
                    transition_job(session, job.id, JobStatus.GENERATING)
                session.commit()
                return
            if result.category is RunpodState.FAILED:
                fail_variation_attempt(
                    session,
                    attempt.id,
                    error_code="runpod_generation_failed",
                    user_facing_error="Runpod did not complete this generation.",
                )
                session.commit()
                return

            set_variation_runpod_result(session, attempt.id, _runpod_result_metadata(result))
            if result.result is not None:
                attempt = get_variation_attempt(session, job_id, variation_index)
                assert attempt is not None
            if not self._valid_output(session, job_id, variation_index):
                # Keep the attempt active. The output capability may be
                # consumed just before or just after this status observation.
                if attempt.status is JobStatus.CLOUD_QUEUED:
                    transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
                    if job.status is JobStatus.CLOUD_QUEUED:
                        transition_job(session, job.id, JobStatus.GENERATING)
                session.commit()
                return
            complete_variation_attempt(session, attempt.id)
            session.commit()

    def _recover_on_startup(self) -> None:
        with self.session_factory() as session:
            recover_uncertain_submissions(session)
            recover_uncertain_variation_submissions(session)
            session.commit()

        with self.session_factory() as session:
            jobs = list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))
            for job in jobs:
                if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                    continue
                if job.status is JobStatus.QUEUED:
                    self._enqueue_recovered(job.id)
                    continue
                if job.status is JobStatus.INGESTING:
                    if self._incoming_source_is_valid(job.id):
                        transition_job(session, job.id, JobStatus.STAGING)
                        self._enqueue_recovered(job.id)
                    else:
                        transition_job(
                            session,
                            job.id,
                            JobStatus.FAILED,
                            error_code="ingest_interrupted",
                            user_facing_error="Home-server source ingestion was interrupted.",
                        )
                    continue
                if job.status is JobStatus.STAGING:
                    self._enqueue_recovered(job.id)
                    continue
                if job.status in {JobStatus.CLOUD_QUEUED, JobStatus.GENERATING}:
                    attempt = get_variation_attempt(session, job.id, job.current_variation or 1)
                    if attempt is not None and attempt.status is JobStatus.QUEUED:
                        # The previous variation completed durably and the
                        # next one has not crossed its nonce boundary yet.
                        self._enqueue_recovered(job.id)
                    elif attempt is not None and attempt.runpod_job_id:
                        self._enqueue_recovered(job.id)
                    elif job.current_runpod_job_id:
                        self._materialize_legacy_attempt(session, job)
                        self._enqueue_recovered(job.id)
                    elif (
                        attempt is not None and attempt.submission_nonce
                    ) or job.current_submission_nonce:
                        self._fail_recovered_job(
                            session,
                            job,
                            "uncertain_cloud_submission",
                            "Cloud submission outcome is uncertain; "
                            "automatic resubmission was prevented.",
                        )
                    else:
                        self._fail_recovered_job(
                            session,
                            job,
                            "missing_runpod_job_id",
                            "Controller state is missing its Runpod job ID.",
                        )
            session.commit()

    def _enqueue_recovered(self, job_id: str) -> None:
        if job_id in self._enqueued:
            return
        self._enqueued.add(job_id)
        self._queue.put_nowait(job_id)

    def _materialize_legacy_attempt(self, session: Any, job: Job) -> VariationAttempt:
        attempt = create_variation_attempt(
            session, job_id=job.id, variation_index=job.current_variation or 1
        )
        if attempt.status is JobStatus.QUEUED:
            transition_variation_attempt(
                session,
                attempt.id,
                JobStatus.GENERATING
                if job.status is JobStatus.GENERATING
                else JobStatus.CLOUD_QUEUED,
            )
        attempt.runpod_job_id = job.current_runpod_job_id
        attempt.submission_nonce = job.current_submission_nonce
        return attempt

    def _fail_recovered_job(
        self, session: Any, job: Job, error_code: str, user_facing_error: str
    ) -> None:
        transition_job(
            session,
            job.id,
            JobStatus.FAILED,
            error_code=error_code,
            user_facing_error=user_facing_error,
        )
        attempt = get_variation_attempt(session, job.id, job.current_variation or 1)
        if attempt is not None and attempt.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
            transition_variation_attempt(
                session,
                attempt.id,
                JobStatus.FAILED,
                error_code=error_code,
                user_facing_error=user_facing_error,
            )

    def _persist_task_failure(self, job_id: str, exc: Exception) -> None:
        del exc
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                session.rollback()
                return
            attempt = get_variation_attempt(session, job_id, job.current_variation or 1)
            uncertain = bool(
                (attempt is not None and attempt.submission_nonce and not attempt.runpod_job_id)
                or (job.current_submission_nonce and not job.current_runpod_job_id)
            )
            code = "uncertain_cloud_submission" if uncertain else "controller_task_error"
            message = (
                "Cloud submission outcome is uncertain; automatic resubmission was prevented."
                if uncertain
                else "Controller could not complete this generation."
            )
            if attempt is not None and attempt.status not in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
            }:
                fail_variation_attempt(
                    session,
                    attempt.id,
                    error_code=code,
                    user_facing_error=message,
                )
            elif job.status is not JobStatus.FAILED:
                transition_job(
                    session,
                    job.id,
                    JobStatus.FAILED,
                    error_code=code,
                    user_facing_error=message,
                )
            session.commit()

    def _job_needs_poll(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            return job is not None and job.status in {
                JobStatus.CLOUD_QUEUED,
                JobStatus.GENERATING,
            }

    def _incoming_source_is_valid(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                return False
            candidate = self.settings.paths.incoming / job.id / "source.mp3"
            try:
                resolved = resolve_relative_path(
                    self.settings.paths.incoming, f"{job.id}/source.mp3"
                )
                if not resolved.is_relative_to(
                    self.settings.paths.incoming.resolve()
                ) or _has_symlink_component(self.settings.paths.incoming, candidate):
                    return False
                stat = candidate.stat()
                if not candidate.is_file() or stat.st_size <= 0:
                    return False
                if stat.st_size > self.settings.transfer_max_source_bytes:
                    return False
                if job.source_byte_size is None or job.source_byte_size <= 0:
                    return False
                if job.source_sha256 is None or not isinstance(job.source_sha256, str):
                    return False
                try:
                    expected_sha256 = validate_sha256(job.source_sha256)
                except ValueError:
                    return False
                if stat.st_size != job.source_byte_size:
                    return False
                if _file_sha256(candidate) != expected_sha256:
                    return False
                return True
            except (OSError, ValueError):
                return False

    def _valid_output(self, session: Any, job_id: str, variation_index: int) -> bool:
        job = get_job(session, job_id)
        if job is None:
            return False
        output = session.scalar(
            select(Output).where(
                Output.job_id == job_id,
                Output.variation_index == variation_index,
                Output.result_index == 0,
            )
        )
        if output is None or output.byte_size <= 0:
            return False
        try:
            expected_relative_path = (
                f"{job.id}/variation-{variation_index:02d}.{job.output_format.value}"
            )
            if output.relative_path != expected_relative_path:
                return False
            raw_candidate = self.settings.paths.outputs / output.relative_path
            candidate = resolve_relative_path(self.settings.paths.outputs, output.relative_path)
            root = self.settings.paths.outputs.resolve()
            if (
                not candidate.is_relative_to(root)
                or _has_symlink_component(self.settings.paths.outputs, raw_candidate)
                or not candidate.is_file()
            ):
                return False
            if candidate.suffix.lower() != normalize_extension(job.output_format.value):
                return False
            stat = candidate.stat()
            if (
                stat.st_size != output.byte_size
                or stat.st_size > self.settings.transfer_max_output_bytes
            ):
                return False
            return _file_sha256(candidate) == str(output.sha256)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _default_payload(job: Job, attempt: VariationAttempt) -> Mapping[str, Any]:
        payload = dict(job.normalized_request_json or {})
        generation = payload.get("generation")
        if isinstance(generation, Mapping):
            generation_payload = dict(generation)
            seed = generation_payload.get("seed")
            if seed is not None:
                if isinstance(seed, bool) or not isinstance(seed, int):
                    raise ValueError("generation seed must be an integer")
                progressed_seed = seed + attempt.variation_index - 1
                if not 0 <= progressed_seed <= 2_147_483_647:
                    raise ValueError("generation seed progression is out of range")
                generation_payload["seed"] = progressed_seed
            payload["generation"] = generation_payload
        payload.update(
            {
                "job_id": job.id,
                "task_type": job.job_type.value,
                "variation_index": attempt.variation_index,
            }
        )
        return payload

    @staticmethod
    def _output_relative_path(job: Job, variation_index: int) -> str:
        return f"{job.id}/variation-{variation_index:02d}.{job.output_format.value}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runpod_result_metadata(result: RunpodStatusResult) -> dict[str, Any] | None:
    metadata = dict(result.result or {})
    if result.delay_ms is not None:
        metadata["runpod_queue_delay_ms"] = result.delay_ms
    if result.execution_ms is not None:
        metadata["runpod_execution_ms"] = result.execution_ms
    return metadata or None


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    """Reject symlinked directories as well as a symlinked final file."""

    resolved_root = root.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = resolved_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False

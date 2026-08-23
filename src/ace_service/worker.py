"""One-process durable controller worker for serialized Runpod jobs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import select

from ace_service.artifact_store import materialize_remote_artifact
from ace_service.config import ServiceSettings
from ace_service.costs import resolve_gpu_alias, round_half_up_compute_cost_usd
from ace_service.cover import CoverSourceError, finalize_cover_source, remove_cover_source
from ace_service.db import SessionFactory
from ace_service.home_ingest import HomeIngestError, HomeIngestService
from ace_service.models import (
    GpuRateCatalog,
    Job,
    JobStatus,
    JobType,
    Output,
    TransferDirection,
    VariationAttempt,
    utc_now,
)
from ace_service.providers.base import (
    BackendId,
    BackendOperation,
    CancelOutcome,
    GenerationRequest,
    InferenceMode,
    InferenceRequest,
    ProviderError,
    ProviderErrorKind,
    ProviderJobNotComplete,
    ProviderJobRef,
    ProviderName,
    ProviderPhase,
    RequestFeature,
    unsupported_features,
)
from ace_service.providers.fal import FalProvider
from ace_service.providers.registry import ProviderRegistry
from ace_service.providers.runpod import RunpodProvider
from ace_service.quality_profiles import MAX_SEED
from ace_service.repository import (
    attempt_provider_ref,
    attempt_provider_result,
    complete_variation_attempt,
    confirm_cover_job,
    create_output,
    create_variation_attempt,
    fail_variation_attempt,
    finalize_cover_job_duration,
    get_job,
    get_variation_attempt,
    job_backend,
    job_provider,
    persist_variation_provider_job_ref,
    prepare_variation_submission,
    record_attempt_evidence,
    recover_uncertain_submissions,
    recover_uncertain_variation_submissions,
    revoke_active_transfers,
    set_variation_progress,
    set_variation_provider_result,
    transition_job,
    transition_variation_attempt,
)
from ace_service.runpod_client import (
    RunpodHealth,
    RunpodState,
    RunpodStatusResult,
)
from ace_service.schemas import (
    LEGACY_WORKER_SCHEMA_VERSION,
    WORKER_SCHEMA_VERSION,
    normalize_extension,
    resolve_relative_path,
    validate_sha256,
    validate_worker_result_metadata,
)
from ace_service.state import ControllerLock
from ace_service.transfers import issue_transfer_url

LOGGER = logging.getLogger(__name__)
_MODEL_REPO_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$"
)
_MODEL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MODEL_TAG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_MODEL_MANIFEST_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKER_V2_PERSISTED_FIELDS = frozenset(
    {
        "schema_version",
        "task_type",
        "profile_id",
        "resolved_parameters",
        "source_duration_seconds",
        "resolved_target_duration_seconds",
        "ace_duration_seconds",
        "cover_staging",
        "generation",
        "source",
    }
)


class RunpodWorkerClient(Protocol):
    async def submit(
        self, payload: Mapping[str, Any], execution_timeout_ms: int, ttl_ms: int
    ) -> str: ...

    async def status(self, runpod_job_id: str) -> RunpodStatusResult: ...

    async def health(self) -> RunpodHealth: ...


PayloadBuilder = Callable[[Job, VariationAttempt], Mapping[str, Any]]


class ControllerWorker:
    """Own one durable queue and one serialized Runpod orchestration loop."""

    def __init__(
        self,
        settings: ServiceSettings,
        session_factory: SessionFactory,
        provider_registry: ProviderRegistry | RunpodWorkerClient,
        *,
        payload_builder: PayloadBuilder | None = None,
        home_ingest_client: HomeIngestService | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        if isinstance(provider_registry, ProviderRegistry):
            self.provider_registry = provider_registry
            self.runpod_client = None
        else:
            adapter = RunpodProvider(cast(Any, provider_registry))
            self.provider_registry = ProviderRegistry([adapter], default=ProviderName.RUNPOD)
            self.runpod_client = provider_registry
        self.payload_builder = payload_builder or self._default_payload
        self.home_ingest_client = home_ingest_client
        self.poll_interval_seconds = poll_interval_seconds
        if self.poll_interval_seconds is not None and self.poll_interval_seconds < 0:
            raise ValueError("poll interval must not be negative")
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueued: set[str] = set()
        self._processing_lock = asyncio.Lock()
        self._controller_lock: ControllerLock | None = None
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._poll_error_counts: dict[str, int] = {}
        self._poll_delays: dict[str, float] = {}

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
            LOGGER.info(
                "stage=start component=controller",
                extra={"component": "controller"},
            )
        except BaseException:
            self._accepting = False
            lock.release()
            self._controller_lock = None
            raise

    async def stop(self) -> None:
        """Cancel the queue task and release the data-root ownership lock."""

        self._accepting = False
        self._poll_error_counts.clear()
        self._poll_delays.clear()
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
                    delay = self._poll_delays.pop(job_id, self._base_poll_delay(job_id))
                    if delay:
                        await asyncio.sleep(delay)
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
            job_type = job.job_type.value
        LOGGER.info(
            "job=%s stage=%s component=controller job_type=%s variation=%d",
            job_id,
            status.value,
            job_type,
            variation_index,
            extra={"component": "controller"},
        )

        if status is JobStatus.QUEUED:
            if job.job_type is JobType.COVER:
                await self._prepare_cover(job_id)
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
            if job.job_type is JobType.COVER and not self._cover_is_confirmed(job):
                return
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
        started = time.monotonic()
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise ValueError("job is missing")
            provider_name = job_provider(job)
            backend_id = job_backend(job)
            provider = self.provider_registry.get_persisted(backend_id)
            if isinstance(provider, FalProvider):
                if job.job_type is JobType.COVER and (
                    not job.source_sha256 or not job.source_byte_size
                ):
                    raise CoverSourceError(
                        "prepared_source_invalid", "cover source metadata is incomplete"
                    )
                self._validate_fal_edit_request(provider, job)
            job, attempt, nonce = prepare_variation_submission(
                session,
                job_id,
                variation_index,
                inference_provider=provider_name,
                inference_backend=backend_id,
            )
            payload = dict(self.payload_builder(job, attempt))
            fal_source: dict[str, Any] | None = None
            if not isinstance(provider, FalProvider):
                output_path = self._output_relative_path(job, attempt.variation_index)
                output_issued = issue_transfer_url(
                    session,
                    self.settings,
                    job_id=job.id,
                    direction=TransferDirection.OUTPUT_UPLOAD,
                    expected_relative_path=output_path,
                    expected_extension=job.output_format.value,
                    max_bytes=self.settings.transfer_max_output_bytes,
                )
                payload["result_upload"] = {
                    "url": output_issued.url,
                    "max_bytes": output_issued.capability.max_bytes,
                }
            if job.job_type is JobType.COVER:
                if not job.source_sha256 or not job.source_byte_size:
                    raise CoverSourceError(
                        "prepared_source_invalid", "cover source metadata is incomplete"
                    )
                source_issued = issue_transfer_url(
                    session,
                    self.settings,
                    job_id=job.id,
                    direction=TransferDirection.SOURCE_DOWNLOAD,
                    expected_relative_path=f"{job.id}/source.mp3",
                    expected_extension=".mp3",
                    max_bytes=job.source_byte_size,
                )
                if isinstance(provider, FalProvider):
                    fal_source = {"audio_url": source_issued.url}
                else:
                    payload["source"] = {
                        "url": source_issued.url,
                        "sha256": job.source_sha256,
                        "bytes": job.source_byte_size,
                        "format": "mp3",
                    }
            payload["submission_nonce"] = nonce
            session.commit()

        # The nonce-only commit is the no-duplicate boundary. Any exception
        # below is persisted as an uncertain submission by the task handler.
        provider = self.provider_registry.get_persisted(backend_id)
        mode = (
            InferenceMode.AUDIO_TO_AUDIO
            if job.job_type is JobType.COVER
            else InferenceMode.PROMPT_TO_AUDIO
        )
        features = self._requested_features(job, payload)
        generation_request = (
            self._generation_request(job, variation_index)
            if isinstance(provider, FalProvider)
            else None
        )
        request = InferenceRequest(
            application_job_id=job_id,
            variation_index=variation_index,
            submission_nonce=nonce,
            mode=mode,
            requested_features=features,
            worker_payload={} if isinstance(provider, FalProvider) else payload,
            execution_timeout_ms=self.settings.runpod_execution_timeout_ms,
            queue_timeout_ms=self.settings.runpod_job_ttl_ms,
            generation_request=generation_request,
            signed_source=fal_source,
        )
        if not isinstance(provider, FalProvider):
            missing = unsupported_features(provider.capabilities, request)
            worker_schema = request.worker_payload.get(
                "schema_version", LEGACY_WORKER_SCHEMA_VERSION
            )
            if missing or worker_schema not in provider.capabilities.accepts_worker_schema:
                raise ValueError("inference provider does not support requested features")
        provider_ref = await provider.submit(request)
        with self.session_factory() as session:
            persist_variation_provider_job_ref(
                session,
                attempt.id,
                provider_ref,
                submission_nonce=nonce,
            )
            session.commit()
        LOGGER.info(
            "job=%s stage=submit component=controller variation=%d provider=%s elapsed_ms=%d",
            job_id,
            variation_index,
            provider_name.value,
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )

    async def _poll_variation(self, job_id: str, variation_index: int) -> None:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            if job is None or attempt is None:
                raise ValueError("active job has no durable variation attempt")
            ref = attempt_provider_ref(attempt, job)
            if ref is None:
                if attempt.submission_nonce or job.current_submission_nonce:
                    raise ValueError("uncertain cloud submission has no provider job ID")
                raise ValueError("active job is missing its provider job ID")
            deadline = (attempt.started_at or job.started_at or job.created_at) + timedelta(
                seconds=self.settings.inference_job_timeout_seconds
            )

        provider = self.provider_registry.get_persisted(BackendId(str(ref.backend_id)))

        started = time.monotonic()
        try:
            status = await provider.status(ref)
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            await self._handle_provider_uncertainty(
                job_id, variation_index, ref, provider, deadline, exc
            )
            return

        if not self._ref_is_current(job_id, variation_index, ref):
            return
        self._clear_poll_error(job_id)
        if (
            status.phase
            in {
                ProviderPhase.QUEUED,
                ProviderPhase.PROVISIONING,
                ProviderPhase.STARTING,
                ProviderPhase.RUNNING,
                ProviderPhase.UNKNOWN,
            }
            and utc_now() >= deadline
        ):
            if await self._cancel_at_deadline(job_id, variation_index, ref, provider):
                return

        LOGGER.info(
            "job=%s stage=poll component=controller variation=%d provider=%s "
            "status=%s elapsed_ms=%d",
            job_id,
            variation_index,
            ref.provider.value,
            status.phase.value,
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )

        with self.session_factory() as session:
            attempt = get_variation_attempt(session, job_id, variation_index)
            job = get_job(session, job_id)
            if attempt is None or job is None:
                raise ValueError("polled job no longer has a durable variation attempt")
            if attempt_provider_ref(attempt, job) != ref:
                session.rollback()
                return
            if status.phase in {
                ProviderPhase.QUEUED,
                ProviderPhase.PROVISIONING,
                ProviderPhase.STARTING,
                ProviderPhase.UNKNOWN,
            }:
                phase = (
                    "worker_initializing"
                    if status.phase in {ProviderPhase.PROVISIONING, ProviderPhase.STARTING}
                    else "cloud_wait"
                )
                set_variation_progress(
                    session,
                    attempt.id,
                    phase,
                    provider_message=status.message,
                    provider_progress=status.progress,
                    detail_scope=status.detail_scope,
                )
                session.commit()
                return
            if status.phase is ProviderPhase.RUNNING:
                phase = (
                    status.provider_reason
                    if status.provider_reason
                    in {
                        "source_download",
                        "generation",
                        "finalizing",
                        "output_upload",
                    }
                    else "worker_running"
                )
                set_variation_progress(
                    session,
                    attempt.id,
                    phase,
                    provider_message=status.message,
                    provider_progress=status.progress,
                    detail_scope=status.detail_scope,
                )
                transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
                if job.status is JobStatus.CLOUD_QUEUED:
                    transition_job(session, job.id, JobStatus.GENERATING)
                session.commit()
                return
            if status.phase in {ProviderPhase.FAILED, ProviderPhase.CANCELLED}:
                _ensure_terminal_evidence(
                    session, attempt, metadata=None, unavailable_reason="worker_no_evidence"
                )
                failed_job = fail_variation_attempt(
                    session,
                    attempt.id,
                    error_code="provider_generation_failed",
                    user_facing_error="The cloud provider did not complete this generation.",
                )
                if failed_job.job_type is JobType.COVER:
                    revoke_active_transfers(session, failed_job.id)
                session.commit()
                if failed_job.job_type is JobType.COVER:
                    remove_cover_source(self.settings, failed_job.id)
                return

        try:
            provider_result = await provider.result(ref)
        except (ProviderError, ProviderJobNotComplete) as exc:
            await self._handle_provider_uncertainty(
                job_id, variation_index, ref, provider, deadline, exc
            )
            return
        if not self._ref_is_current(job_id, variation_index, ref):
            return
        self._clear_poll_error(job_id)
        with self.session_factory() as session:
            attempt = get_variation_attempt(session, job_id, variation_index)
            job = get_job(session, job_id)
            if attempt is None or job is None or attempt_provider_ref(attempt, job) != ref:
                session.rollback()
                return
            expected_schema_version = (
                None if isinstance(provider, FalProvider) else _stored_schema_version(job)
            )
            metadata = dict(provider_result.metadata)
            if expected_schema_version is not None or "schema_version" in metadata:
                metadata = validate_worker_result_metadata(
                    metadata, expected_schema_version=expected_schema_version
                )
            if expected_schema_version == WORKER_SCHEMA_VERSION:
                if not metadata:
                    raise ValueError("schema-v2 worker completion is missing result metadata")
            output = (
                None
                if isinstance(provider, FalProvider)
                else self._validated_output(session, job, variation_index)
            )
            if expected_schema_version == WORKER_SCHEMA_VERSION:
                assert metadata is not None
                _validate_completion_metadata(metadata, job, attempt, output)
            set_variation_provider_result(
                session,
                attempt.id,
                metadata,
                project_to_output=output is not None,
            )
            if isinstance(provider, FalProvider):
                artifact = provider_result.artifact
                if artifact is None:
                    raise ValueError("Fal completion is missing its declared audio artifact")
                relative_path = f"{job.id}/variation-{variation_index:02d}.{artifact.native_format}"
                existing = session.scalar(
                    select(Output).where(
                        Output.job_id == job.id,
                        Output.variation_index == variation_index,
                        Output.result_index == 0,
                    )
                )
                if existing is None:
                    token = await provider.transport.cdn_token()
                    receipt = await materialize_remote_artifact(
                        provider.transport.client,
                        artifact.url,
                        root=self.settings.paths.outputs,
                        target=self.settings.paths.outputs / relative_path,
                        native_format=artifact.native_format,
                        max_bytes=min(
                            provider.descriptor.output.max_bytes,
                            self.settings.transfer_max_output_bytes,
                        ),
                        bearer_token=token,
                    )
                    metadata.update(
                        {
                            "artifact_byte_size": receipt.byte_size,
                            "artifact_sha256": receipt.sha256,
                            "artifact_native_format": artifact.native_format,
                            **(
                                {"returned_seed": artifact.seed}
                                if artifact.seed is not None
                                else {}
                            ),
                            **(
                                {"returned_duration_seconds": artifact.duration_seconds}
                                if artifact.duration_seconds is not None
                                else {}
                            ),
                        }
                    )
                    set_variation_provider_result(
                        session,
                        attempt.id,
                        metadata,
                        project_to_output=False,
                    )
                    output = create_output(
                        session,
                        job_id=job.id,
                        variation_index=variation_index,
                        result_index=0,
                        relative_path=relative_path,
                        mime_type=receipt.content_type,
                        byte_size=receipt.byte_size,
                        sha256=receipt.sha256,
                        provider_ref=ref,
                        seed_metadata_json={"seed": artifact.seed}
                        if artifact.seed is not None
                        else None,
                        generation_metadata_json=metadata,
                    )
                else:
                    output = existing
                _ensure_terminal_evidence(
                    session,
                    attempt,
                    metadata=metadata,
                    unavailable_reason="provider_managed_pricing",
                )
                completed_job, _, parent_completed = complete_variation_attempt(session, attempt.id)
                if parent_completed and completed_job.job_type is JobType.COVER:
                    revoke_active_transfers(session, completed_job.id)
                session.commit()
                if parent_completed:
                    remove_cover_source(self.settings, job_id)
                return
            if output is None:
                # Keep the attempt active. The output capability may be
                # consumed just before or just after this status observation.
                if attempt.status is JobStatus.CLOUD_QUEUED:
                    transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
                    if job.status is JobStatus.CLOUD_QUEUED:
                        transition_job(session, job.id, JobStatus.GENERATING)
                session.commit()
                return
            if ref.provider is ProviderName.RUNPOD:
                execution_ms = metadata.get("runpod_execution_ms")
                delay_ms = metadata.get("runpod_queue_delay_ms")
                _record_poll_evidence(
                    session,
                    attempt.id,
                    RunpodStatusResult(
                        ref.external_id,
                        RunpodState.COMPLETED,
                        "COMPLETED",
                        metadata,
                        delay_ms=delay_ms if isinstance(delay_ms, int) else None,
                        execution_ms=execution_ms if isinstance(execution_ms, int) else None,
                    ),
                    metadata,
                )
            else:
                _ensure_terminal_evidence(
                    session,
                    attempt,
                    metadata=metadata,
                    unavailable_reason="timing_unavailable",
                )
            completed_job, _, parent_completed = complete_variation_attempt(session, attempt.id)
            if parent_completed and completed_job.job_type is JobType.COVER:
                revoke_active_transfers(session, completed_job.id)
            session.commit()
            if parent_completed:
                remove_cover_source(self.settings, job_id)

    async def _prepare_cover(self, job_id: str) -> None:
        """Run home preparation, atomically stage a confirmed cover, then submit."""

        started = time.monotonic()
        LOGGER.info(
            "job=%s stage=ingest component=controller",
            job_id,
            extra={"component": "controller"},
        )
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.job_type is not JobType.COVER:
                raise ValueError("cover job is missing")
            if job.status is JobStatus.QUEUED:
                transition_job(session, job.id, JobStatus.INGESTING)
            source_url = job.source_url
            session.commit()
        if not source_url:
            raise HomeIngestError("youtube_url_rejected", "cover job has no YouTube URL")
        if self.home_ingest_client is None:
            raise HomeIngestError(
                "home_ingest_unavailable", "the home ingest service is not configured"
            )
        prepared = await self.home_ingest_client.prepare(
            job_id=job_id,
            url=source_url,
            max_duration_seconds=self.settings.max_source_duration_seconds,
            max_source_bytes=self.settings.transfer_max_source_bytes,
        )
        if prepared.job_id != job_id:
            raise HomeIngestError(
                "home_ingest_invalid_response", "home returned metadata for another job"
            )
        finalize_cover_source(self.settings, job_id, prepared)
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status is not JobStatus.INGESTING:
                raise ValueError("cover job is not awaiting source staging")
            job.source_url = prepared.canonical_url
            job.sanitized_source_title = prepared.title
            job.source_sha256 = prepared.prepared_sha256
            job.source_byte_size = prepared.prepared_bytes
            normalized = job.normalized_request_json
            is_v2 = (
                isinstance(normalized, Mapping)
                and normalized.get("schema_version") == WORKER_SCHEMA_VERSION
            )
            if is_v2:
                # The initial rights checkbox is the only user authorization
                # for a new cover. Fail closed when it was not durably
                # persisted, then consume it in the same transaction that
                # freezes the prepared source, so no Runpod submission can
                # precede a durable confirmed-staging state.
                if job.rights_confirmation_at is None:
                    raise HomeIngestError(
                        "rights_confirmation_missing",
                        "the cover request is missing its rights confirmation",
                    )
                finalize_cover_job_duration(session, job.id, prepared.duration_seconds)
                transition_job(session, job.id, JobStatus.STAGING)
                confirm_cover_job(session, job.id)
            else:
                # Legacy rows retain their schema and their submitted-era
                # behavior; only the relational source duration is updated.
                job.source_duration = prepared.duration_seconds
                transition_job(session, job.id, JobStatus.STAGING)
            session.commit()
        await self._submit_variation(job_id, 1)
        LOGGER.info(
            "job=%s stage=staging component=controller elapsed_ms=%d",
            job_id,
            int((time.monotonic() - started) * 1000),
            extra={"component": "controller"},
        )

    def _recover_on_startup(self) -> None:
        with self.session_factory() as session:
            recover_uncertain_submissions(session)
            recover_uncertain_variation_submissions(session)
            session.commit()

        with self.session_factory() as session:
            jobs = list(session.scalars(select(Job).order_by(Job.created_at, Job.id)))
            terminal_cover_ids: list[str] = []
            for job in jobs:
                if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                    if job.job_type is JobType.COVER:
                        revoke_active_transfers(session, job.id)
                        terminal_cover_ids.append(job.id)
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
                    if self._cover_is_confirmed(job):
                        self._enqueue_recovered(job.id)
                    continue
                if job.status in {JobStatus.CLOUD_QUEUED, JobStatus.GENERATING}:
                    attempt = get_variation_attempt(session, job.id, job.current_variation or 1)
                    if attempt is not None and attempt.status is JobStatus.QUEUED:
                        # The previous variation completed durably and the
                        # next one has not crossed its nonce boundary yet.
                        self._enqueue_recovered(job.id)
                    elif attempt is not None and attempt_provider_ref(attempt, job):
                        self._enqueue_recovered(job.id)
                    elif job.current_provider_job_id or job.current_runpod_job_id:
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
                            "missing_provider_job_id",
                            "Controller state is missing its provider job ID.",
                        )
            terminal_cover_ids = list(
                dict.fromkeys(
                    terminal_cover_ids
                    + [
                        job.id
                        for job in jobs
                        if job.job_type is JobType.COVER
                        and job.status in {JobStatus.COMPLETED, JobStatus.FAILED}
                    ]
                )
            )
            session.commit()
        for job_id in terminal_cover_ids:
            remove_cover_source(self.settings, job_id)

    def _enqueue_recovered(self, job_id: str) -> None:
        if job_id in self._enqueued:
            return
        self._enqueued.add(job_id)
        self._queue.put_nowait(job_id)

    @staticmethod
    def _cover_is_confirmed(job: Job) -> bool:
        """Treat legacy staged rows as submitted-era state, but gate v2 rows."""

        normalized = job.normalized_request_json
        if (
            not isinstance(normalized, Mapping)
            or normalized.get("schema_version") != WORKER_SCHEMA_VERSION
        ):
            return True
        staging = normalized.get("cover_staging")
        return isinstance(staging, Mapping) and staging.get("status") == "confirmed"

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
        provider = job_provider(job)
        attempt.inference_provider = provider.value
        attempt.provider_job_id = job.current_provider_job_id or job.current_runpod_job_id
        if provider is ProviderName.RUNPOD:
            attempt.runpod_job_id = attempt.provider_job_id
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
            _ensure_terminal_evidence(
                session,
                attempt,
                metadata=attempt_provider_result(attempt),
                unavailable_reason="worker_no_evidence",
            )
            transition_variation_attempt(
                session,
                attempt.id,
                JobStatus.FAILED,
                error_code=error_code,
                user_facing_error=user_facing_error,
            )
        if job.job_type is JobType.COVER:
            revoke_active_transfers(session, job.id)

    def _persist_task_failure(self, job_id: str, exc: Exception) -> None:
        stable_code = getattr(exc, "code", None)
        stable_message = getattr(exc, "message", None)
        code = stable_code if isinstance(stable_code, str) else "controller_task_error"
        LOGGER.error(
            "job=%s stage=worker error_code=%s exception_class=%s",
            job_id,
            code,
            type(exc).__name__,
            extra={"component": "controller"},
        )
        message = (
            stable_message
            if isinstance(stable_message, str) and stable_message
            else "Controller could not complete this generation."
        )
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                session.rollback()
                return
            attempt = get_variation_attempt(session, job_id, job.current_variation or 1)
            uncertain = bool(
                (attempt is not None and attempt.submission_nonce and not attempt.provider_job_id)
                or (job.current_submission_nonce and not job.current_provider_job_id)
            )
            if uncertain:
                code = "uncertain_cloud_submission"
                message = (
                    "Cloud submission outcome is uncertain; automatic resubmission was prevented."
                )
            if attempt is not None and attempt.status not in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
            }:
                _ensure_terminal_evidence(
                    session,
                    attempt,
                    metadata=attempt_provider_result(attempt),
                    unavailable_reason="worker_no_evidence",
                )
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
            if job.job_type is JobType.COVER:
                revoke_active_transfers(session, job.id)
            session.commit()
            is_cover = job.job_type is JobType.COVER
        if is_cover:
            remove_cover_source(self.settings, job_id)

    def _job_needs_poll(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            return job is not None and job.status in {
                JobStatus.CLOUD_QUEUED,
                JobStatus.GENERATING,
            }

    def _base_poll_delay(self, job_id: str) -> float:
        if self.poll_interval_seconds is not None:
            return self.poll_interval_seconds
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is not None:
                provider = job_provider(job)
                if provider is ProviderName.SALAD:
                    return self.settings.salad_poll_interval_seconds
                if provider is ProviderName.FAL:
                    return self.settings.fal_poll_interval_seconds
        return self.settings.runpod_poll_interval_seconds

    def _record_poll_error(self, job_id: str, provider: ProviderName, exc: Exception) -> None:
        count = self._poll_error_counts.get(job_id, 0) + 1
        self._poll_error_counts[job_id] = count
        delay = min(60.0, max(self._base_poll_delay(job_id), 1.0) * (2 ** (count - 1)))
        self._poll_delays[job_id] = delay
        LOGGER.warning(
            "job=%s provider=%s operation=status exception_class=%s "
            "status=%s count=%d next_delay=%s",
            job_id,
            provider.value,
            type(exc).__name__,
            getattr(exc, "status_code", None),
            count,
            delay,
            extra={"component": "controller"},
        )

    def _clear_poll_error(self, job_id: str) -> None:
        self._poll_error_counts.pop(job_id, None)
        self._poll_delays.pop(job_id, None)

    def _ref_is_current(self, job_id: str, variation_index: int, ref: Any) -> bool:
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            return (
                job is not None
                and attempt is not None
                and attempt_provider_ref(attempt, job) == ref
            )

    async def _handle_provider_uncertainty(
        self,
        job_id: str,
        variation_index: int,
        ref: Any,
        provider: Any,
        deadline: Any,
        exc: Exception,
    ) -> None:
        self._record_poll_error(job_id, ref.provider, exc)
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            output = self._validated_output(session, job, variation_index)
            metadata = (
                _validated_v2_completion_metadata(attempt, job, output)
                if attempt is not None
                and job is not None
                and _stored_schema_version(job) == WORKER_SCHEMA_VERSION
                else None
            )
            legacy_recovery = (
                job is not None and _stored_schema_version(job) != WORKER_SCHEMA_VERSION
            )
            if (
                attempt is not None
                and job is not None
                and output is not None
                and (legacy_recovery or metadata is not None)
            ):
                if metadata is not None:
                    set_variation_provider_result(session, attempt.id, metadata)
                _ensure_terminal_evidence(
                    session,
                    attempt,
                    metadata=metadata,
                    unavailable_reason="timing_unavailable",
                )
                completed, _, parent_completed = complete_variation_attempt(
                    session, attempt.id, note="provider_status_unavailable_after_output"
                )
                remove_completed_cover = parent_completed and completed.job_type is JobType.COVER
                if remove_completed_cover:
                    revoke_active_transfers(session, completed.id)
                session.commit()
                if remove_completed_cover:
                    remove_cover_source(self.settings, completed.id)
                self._clear_poll_error(job_id)
                return
        if utc_now() < deadline:
            return
        if (
            isinstance(exc, ProviderError)
            and exc.kind is ProviderErrorKind.NOT_FOUND
            and provider.capabilities.not_found_after_deadline_is_terminal
        ):
            outcome = CancelOutcome.CANCELLED
            error_code = "provider_job_expired"
        else:
            if not self._ref_is_current(job_id, variation_index, ref):
                return
            try:
                outcome = await provider.cancel(ref)
            except ProviderError as cancel_error:
                self._record_poll_error(job_id, ref.provider, cancel_error)
                return
            if not self._ref_is_current(job_id, variation_index, ref):
                return
            error_code = "provider_job_timeout"
        if outcome is not CancelOutcome.CANCELLED:
            return
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            if job is None or attempt is None or attempt_provider_ref(attempt, job) != ref:
                session.rollback()
                return
            _ensure_terminal_evidence(
                session, attempt, metadata=None, unavailable_reason="timing_unavailable"
            )
            failed = fail_variation_attempt(
                session,
                attempt.id,
                error_code=error_code,
                user_facing_error="The cloud provider did not complete before its deadline.",
            )
            if failed.job_type is JobType.COVER:
                revoke_active_transfers(session, failed.id)
            session.commit()
        self._clear_poll_error(job_id)

    async def _cancel_at_deadline(
        self,
        job_id: str,
        variation_index: int,
        ref: ProviderJobRef,
        provider: Any,
    ) -> bool:
        if not self._ref_is_current(job_id, variation_index, ref):
            return True
        try:
            outcome = await provider.cancel(ref)
        except ProviderError as exc:
            self._record_poll_error(job_id, ref.provider, exc)
            return True
        if not self._ref_is_current(job_id, variation_index, ref):
            return True
        if outcome is not CancelOutcome.CANCELLED:
            return False
        with self.session_factory() as session:
            job = get_job(session, job_id)
            attempt = get_variation_attempt(session, job_id, variation_index)
            if job is None or attempt is None or attempt_provider_ref(attempt, job) != ref:
                session.rollback()
                return True
            _ensure_terminal_evidence(
                session,
                attempt,
                metadata=None,
                unavailable_reason="timing_unavailable",
            )
            failed = fail_variation_attempt(
                session,
                attempt.id,
                error_code="provider_job_timeout",
                user_facing_error="The cloud provider did not complete before its deadline.",
            )
            if failed.job_type is JobType.COVER:
                revoke_active_transfers(session, failed.id)
            session.commit()
        self._clear_poll_error(job_id)
        return True

    @staticmethod
    def _requested_features(job: Job, payload: Mapping[str, Any]) -> frozenset[RequestFeature]:
        features = {RequestFeature.PROMPT}
        if job.lyrics:
            features.add(RequestFeature.LYRICS)
        if job.job_type is JobType.COVER:
            features.update({RequestFeature.SOURCE_AUDIO, RequestFeature.COVER_STRENGTH})
        fields = {
            "bpm": RequestFeature.BPM,
            "key": RequestFeature.KEY,
            "time_signature": RequestFeature.TIME_SIGNATURE,
            "language": RequestFeature.LANGUAGE,
            "instrumental": RequestFeature.INSTRUMENTAL,
            "prompt_mode": RequestFeature.PROMPT_MODE,
            "duration": RequestFeature.CUSTOM_DURATION,
            "duration_seconds": RequestFeature.CUSTOM_DURATION,
        }
        for key, feature in fields.items():
            if payload.get(key) is not None:
                features.add(feature)
        return frozenset(features)

    @staticmethod
    def _validate_fal_edit_request(provider: FalProvider, job: Job) -> None:
        """Recheck cross-field edits after source preparation and before submit."""

        normalized = job.normalized_request_json
        if not isinstance(normalized, Mapping):
            return
        contract = normalized.get("generation_request")
        generation = contract if isinstance(contract, Mapping) else normalized.get("generation")
        if not isinstance(generation, Mapping):
            return
        fields_value = generation.get("fields")
        fields = dict(generation)
        if isinstance(fields_value, Mapping):
            fields.update(fields_value)
        operation = provider.descriptor.operation

        def number(name: str) -> float | None:
            value = fields.get(name)
            if value is None and name == "duration":
                value = fields.get("duration_seconds")
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            result = float(value)
            if not math.isfinite(result):
                raise ValueError(f"{name} must be finite")
            return result

        if operation is BackendOperation.AUDIO_INPAINT:
            start = number("start_seconds")
            end = number("end_seconds")
            if start is None or end is None or not 0 <= start < end:
                raise ValueError("inpaint region must satisfy 0 <= start_seconds < end_seconds")
            if job.source_duration is None or end > job.source_duration:
                raise ValueError("inpaint region exceeds the measured source duration")
        elif operation is BackendOperation.AUDIO_OUTPAINT:
            before = number("before_seconds") or 0.0
            after = number("after_seconds") or 0.0
            if before <= 0 and after <= 0:
                raise ValueError("outpaint must extend before or after the source")
        for name, policy in provider.descriptor.fields.items():
            if policy.type not in {"number", "integer"}:
                continue
            value = number(name)
            if value is None:
                continue
            if policy.type == "integer" and not value.is_integer():
                raise ValueError(f"{policy.ui_name} must be an integer")
            if policy.minimum is not None and value < policy.minimum:
                raise ValueError(f"{policy.ui_name} is below its minimum")
            if policy.maximum is not None and value > policy.maximum:
                raise ValueError(f"{policy.ui_name} is above its maximum")

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

    def _validated_output(
        self, session: Any, job: Job | None, variation_index: int
    ) -> Output | None:
        if job is None:
            return None
        output = session.scalar(
            select(Output).where(
                Output.job_id == job.id,
                Output.variation_index == variation_index,
                Output.result_index == 0,
            )
        )
        if output is None or not isinstance(output, Output) or output.byte_size <= 0:
            return None
        try:
            expected_relative_path = (
                f"{job.id}/variation-{variation_index:02d}.{job.output_format.value}"
            )
            if output.relative_path != expected_relative_path:
                return None
            raw_candidate = self.settings.paths.outputs / output.relative_path
            candidate = resolve_relative_path(self.settings.paths.outputs, output.relative_path)
            root = self.settings.paths.outputs.resolve()
            if (
                not candidate.is_relative_to(root)
                or _has_symlink_component(self.settings.paths.outputs, raw_candidate)
                or not candidate.is_file()
            ):
                return None
            if candidate.suffix.lower() != normalize_extension(job.output_format.value):
                return None
            stat = candidate.stat()
            if (
                stat.st_size != output.byte_size
                or stat.st_size > self.settings.transfer_max_output_bytes
            ):
                return None
            if _file_sha256(candidate) != str(output.sha256):
                return None
            return cast(Output, output)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _generation_request(job: Job, variation_index: int) -> GenerationRequest:
        normalized = (
            job.normalized_request_json if isinstance(job.normalized_request_json, Mapping) else {}
        )
        generation_contract = (
            normalized.get("generation_request") if isinstance(normalized, Mapping) else None
        )
        generation = (
            generation_contract
            if isinstance(generation_contract, Mapping)
            else normalized.get("generation")
            if isinstance(normalized, Mapping)
            else None
        )
        generation = generation if isinstance(generation, Mapping) else {}
        if isinstance(generation.get("fields"), Mapping):
            generation = {**generation, **generation["fields"]}
        mode = (
            InferenceMode.AUDIO_TO_AUDIO
            if job.job_type is JobType.COVER
            else InferenceMode.PROMPT_TO_AUDIO
        )
        duration = generation.get("duration_seconds", generation.get("duration"))
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = None
        seed = generation.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            seed = None
        output_format = generation.get("output_format", job.output_format.value)
        if not isinstance(output_format, str) or output_format not in {"mp3", "flac", "wav"}:
            output_format = job.output_format.value
        source_style = generation.get("source_style")
        return GenerationRequest(
            mode=mode,
            prompt=(
                generation.get("prompt")
                if isinstance(generation.get("prompt"), str)
                else job.prompt
            ),
            lyrics=(
                generation.get("lyrics")
                if isinstance(generation.get("lyrics"), str)
                else job.lyrics
            ),
            instrumental=bool(generation.get("instrumental", False)),
            duration_seconds=float(duration) if duration is not None else None,
            seed=seed + variation_index - 1 if seed is not None else None,
            variation_count=job.variation_count,
            output_format=output_format,
            source_duration_seconds=job.source_duration,
            fields={
                key: generation.get(key)
                for key in (
                    "bpm",
                    "key_scale",
                    "time_signature",
                    "start_seconds",
                    "end_seconds",
                    "before_seconds",
                    "after_seconds",
                    "strength",
                    "source_lyrics",
                )
                if generation.get(key) is not None
            }
            | {"strength": generation.get("strength", generation.get("audio_cover_strength"))}
            | ({"source_style": source_style} if source_style is not None else {}),
        )

    @staticmethod
    def _default_payload(job: Job, attempt: VariationAttempt) -> Mapping[str, Any]:
        normalized = dict(job.normalized_request_json or {})
        payload = (
            {key: value for key, value in normalized.items() if key in _WORKER_V2_PERSISTED_FIELDS}
            if normalized.get("schema_version") == WORKER_SCHEMA_VERSION
            else normalized
        )
        generation = payload.get("generation")
        generation_payload = dict(generation) if isinstance(generation, Mapping) else None
        resolved = payload.get("resolved_parameters")
        resolved_payload = dict(resolved) if isinstance(resolved, Mapping) else None
        supplied_seeds: list[int] = []
        for seed, label in (
            (
                generation_payload.get("seed") if generation_payload is not None else None,
                "generation",
            ),
            (
                resolved_payload.get("seed") if resolved_payload is not None else None,
                "resolved",
            ),
        ):
            if seed is None:
                continue
            if isinstance(seed, bool) or not isinstance(seed, int):
                raise ValueError(f"{label} seed must be an integer")
            supplied_seeds.append(seed)
        if len(set(supplied_seeds)) > 1:
            raise ValueError("generation and resolved seeds must match")
        effective_seed: int | None = None
        if supplied_seeds:
            effective_seed = supplied_seeds[0] + attempt.variation_index - 1
            if not 0 <= effective_seed <= MAX_SEED:
                raise ValueError("generation seed progression is out of range")
        elif payload.get("schema_version") == WORKER_SCHEMA_VERSION:
            if not attempt.submission_nonce:
                raise ValueError(
                    "schema-v2 submission must have a durable nonce before payload build"
                )
            effective_seed = _deterministic_submission_seed(
                job.id, attempt.variation_index, attempt.submission_nonce
            )
        if generation_payload is not None:
            if effective_seed is not None:
                generation_payload["seed"] = effective_seed
            payload["generation"] = generation_payload
        if resolved_payload is not None:
            if effective_seed is not None:
                resolved_payload["seed"] = effective_seed
            payload["resolved_parameters"] = resolved_payload
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


def _deterministic_submission_seed(job_id: str, variation_index: int, nonce: str) -> int:
    """Derive a stable retry-safe seed from one durable submission identity."""

    digest = hashlib.sha256()
    for component in (job_id, str(variation_index), nonce):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return int.from_bytes(digest.digest(), "big") % (MAX_SEED + 1)


def _worker_block_value(metadata: Mapping[str, Any] | None, field_name: str) -> str | None:
    """Extract one bounded worker identity field from validated result metadata."""

    if not isinstance(metadata, Mapping):
        return None
    worker = metadata.get("worker")
    if not isinstance(worker, Mapping):
        return None
    value = worker.get(field_name)
    return value if isinstance(value, str) and value.strip() else None


def _record_poll_evidence(
    session: Any,
    attempt_id: int,
    result: RunpodStatusResult,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Persist immutable attempt cost evidence from one terminal poll.

    A terminal attempt with positive Runpod execution time and a trusted rate
    for the resolved GPU gets one complete immutable snapshot.  Missing
    execution time, an unknown worker GPU, or an unknown/stale rate records an
    explicit unavailable reason instead of inventing a number.  Zero is never
    stored from the polling path: zero requires durable provider proof that a
    submitted attempt never started.  Conflicting evidence raises and fails
    the poll closed without overwriting stored evidence.
    """

    execution_ms = result.execution_ms
    gpu_alias = _worker_block_value(metadata, "gpu")
    canonical_gpu = resolve_gpu_alias(gpu_alias)
    model_identity = _worker_block_value(metadata, "dit_model")
    runtime_image_identity = _worker_block_value(metadata, "image_digest")
    if execution_ms is None or execution_ms <= 0:
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            actual_gpu=canonical_gpu,
            model_identity=model_identity,
            runtime_image_identity=runtime_image_identity,
            execution_ms=execution_ms,
            unavailable_reason="timing_unavailable",
        )
        return
    if canonical_gpu is None:
        reason = "worker_no_evidence" if gpu_alias is None else "rate_unknown"
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            execution_ms=execution_ms,
            model_identity=model_identity,
            runtime_image_identity=runtime_image_identity,
            unavailable_reason=reason,
        )
        return
    rate_row = session.scalar(
        select(GpuRateCatalog)
        .where(
            GpuRateCatalog.gpu_id == canonical_gpu,
            GpuRateCatalog.provider == "runpod",
        )
        .order_by(GpuRateCatalog.calibration_version.desc())
        .limit(1)
    )
    now = utc_now()
    if rate_row is None or rate_row.expires_at <= now:
        reason = "rate_stale" if rate_row is not None else "rate_unknown"
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            actual_gpu=canonical_gpu,
            model_identity=model_identity,
            runtime_image_identity=runtime_image_identity,
            execution_ms=execution_ms,
            unavailable_reason=reason,
        )
        return
    estimate = round_half_up_compute_cost_usd(execution_ms, rate_row.hourly_rate_usd)
    record_attempt_evidence(
        session,
        attempt_id,
        evidence_status="complete",
        actual_gpu=canonical_gpu,
        model_identity=model_identity,
        runtime_image_identity=runtime_image_identity,
        execution_ms=execution_ms,
        hourly_rate_usd=rate_row.hourly_rate_usd,
        hourly_rate_micro_usd=rate_row.rate_micro_usd_per_hour,
        rate_currency="USD",
        rate_source=rate_row.source,
        rate_captured_at=rate_row.captured_at,
        estimated_compute_micro_usd=estimate,
    )


def _ensure_terminal_evidence(
    session: Any,
    attempt: VariationAttempt,
    *,
    metadata: Mapping[str, Any] | None,
    unavailable_reason: str,
) -> None:
    """Close a newly terminal pending attempt without inventing execution."""

    if attempt.evidence_status != "pending":
        return
    model_identity = attempt.model_identity or _worker_block_value(metadata, "dit_model")
    runtime_identity = attempt.runtime_image_identity or _worker_block_value(
        metadata, "image_digest"
    )
    gpu = attempt.actual_gpu
    if gpu is None:
        gpu = resolve_gpu_alias(_worker_block_value(metadata, "gpu"))
    record_attempt_evidence(
        session,
        attempt.id,
        evidence_status="unavailable",
        actual_gpu=gpu,
        model_identity=model_identity,
        runtime_image_identity=runtime_identity,
        execution_ms=attempt.execution_ms,
        hourly_rate_usd=attempt.hourly_rate_usd,
        hourly_rate_micro_usd=attempt.hourly_rate_micro_usd,
        rate_currency=(attempt.rate_currency if attempt.hourly_rate_usd is not None else None),
        rate_source=attempt.rate_source,
        rate_captured_at=attempt.rate_captured_at,
        unavailable_reason=unavailable_reason,
    )


def _runpod_result_metadata(
    result: RunpodStatusResult, *, expected_schema_version: int | None = None
) -> dict[str, Any] | None:
    metadata = dict(result.result or {})
    if result.delay_ms is not None:
        metadata["runpod_queue_delay_ms"] = result.delay_ms
    if result.execution_ms is not None:
        metadata["runpod_execution_ms"] = result.execution_ms
    if not metadata:
        return None
    if "schema_version" not in metadata:
        if expected_schema_version == LEGACY_WORKER_SCHEMA_VERSION:
            legacy_metadata = validate_worker_result_metadata(
                {"schema_version": LEGACY_WORKER_SCHEMA_VERSION, **metadata},
                expected_schema_version=LEGACY_WORKER_SCHEMA_VERSION,
            )
            legacy_metadata.pop("schema_version", None)
            return legacy_metadata
        if expected_schema_version is None:
            return metadata
    return validate_worker_result_metadata(
        metadata, expected_schema_version=expected_schema_version
    )


def _stored_schema_version(job: Job) -> int | None:
    normalized = job.normalized_request_json
    if not isinstance(normalized, Mapping):
        return None
    value = normalized.get("schema_version")
    if isinstance(value, bool):
        return None
    return value if value in {LEGACY_WORKER_SCHEMA_VERSION, WORKER_SCHEMA_VERSION} else None


def _validated_v2_completion_metadata(
    attempt: VariationAttempt, job: Job, output: Output | None
) -> dict[str, Any] | None:
    """Return persisted v2 completion evidence only when it still validates."""

    raw_metadata = attempt_provider_result(attempt)
    if not isinstance(raw_metadata, dict):
        return None
    try:
        metadata = validate_worker_result_metadata(
            raw_metadata, expected_schema_version=WORKER_SCHEMA_VERSION
        )
        _validate_completion_metadata(metadata, job, attempt, output)
    except ValueError:
        return None
    return metadata


def _validate_completion_metadata(
    metadata: Mapping[str, Any],
    job: Job,
    attempt: VariationAttempt,
    output_record: Output | None = None,
) -> None:
    if metadata.get("job_id") != job.id:
        raise ValueError("worker completion job identity does not match the active job")
    submission_nonce = metadata.get("submission_nonce")
    if (
        not isinstance(submission_nonce, str)
        or not submission_nonce
        or attempt.submission_nonce is None
        or submission_nonce != attempt.submission_nonce
    ):
        raise ValueError("worker completion submission nonce does not match the active attempt")
    variation_index = metadata.get("variation_index")
    if (
        isinstance(variation_index, bool)
        or not isinstance(variation_index, int)
        or variation_index != attempt.variation_index
    ):
        raise ValueError("worker completion variation does not match the active attempt")
    if metadata.get("status") != "uploaded":
        raise ValueError("worker completion is missing upload evidence")
    output = metadata.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("worker completion is missing output metadata")
    effective_seed = output.get("effective_seed", output.get("seed"))
    if (
        isinstance(effective_seed, bool)
        or not isinstance(effective_seed, int)
        or effective_seed < 0
    ):
        raise ValueError("worker completion is missing an effective integer seed")
    byte_size = output.get("bytes")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size <= 0:
        raise ValueError("worker completion is missing output byte evidence")
    raw_output_sha256 = output.get("sha256")
    if not isinstance(raw_output_sha256, str):
        raise ValueError("worker completion is missing output checksum evidence")
    try:
        output_sha256 = validate_sha256(raw_output_sha256)
    except ValueError as exc:
        raise ValueError("worker completion is missing output checksum evidence") from exc
    if output_record is not None:
        if byte_size != output_record.byte_size:
            raise ValueError("worker output byte evidence does not match the durable output")
        try:
            durable_sha256 = validate_sha256(output_record.sha256)
        except ValueError as exc:
            raise ValueError("durable output checksum evidence is malformed") from exc
        if output_sha256 != durable_sha256:
            raise ValueError("worker output checksum does not match the durable output")
    worker = metadata.get("worker")
    if not isinstance(worker, Mapping):
        raise ValueError("worker completion is missing worker identity")
    for field_name in ("ace_tag", "dit_model", "lm_model", "image_digest", "gpu"):
        value = worker.get(field_name)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"worker completion is missing worker {field_name}")
    model_bundle = worker.get("model_bundle")
    if not isinstance(model_bundle, Mapping) or set(model_bundle) != {
        "repo",
        "revision",
        "tag",
        "manifest_sha256",
    }:
        raise ValueError("worker completion is missing model bundle identity")
    if not isinstance(model_bundle.get("repo"), str) or not _MODEL_REPO_RE.fullmatch(
        model_bundle["repo"]
    ):
        raise ValueError("worker completion model bundle repo is malformed")
    if not isinstance(model_bundle.get("revision"), str) or not _MODEL_REVISION_RE.fullmatch(
        model_bundle["revision"]
    ):
        raise ValueError("worker completion model bundle revision is malformed")
    if not isinstance(model_bundle.get("tag"), str) or not _MODEL_TAG_RE.fullmatch(
        model_bundle["tag"]
    ):
        raise ValueError("worker completion model bundle tag is malformed")
    if not isinstance(
        model_bundle.get("manifest_sha256"), str
    ) or not _MODEL_MANIFEST_SHA256_RE.fullmatch(model_bundle["manifest_sha256"]):
        raise ValueError("worker completion model bundle manifest digest is malformed")
    actual = output.get("duration_seconds")
    target = output.get("target_duration_seconds")
    if target is None:
        resolved = metadata.get("resolved_parameters")
        if isinstance(resolved, Mapping):
            target = resolved.get("target_duration_seconds")
            if target is None:
                target = resolved.get("duration")
            if (
                isinstance(target, bool)
                or not isinstance(target, (int, float))
                or float(target) <= 0
            ):
                target = None
    if target is None:
        return
    if (
        isinstance(actual, bool)
        or not isinstance(actual, (int, float))
        or not isinstance(target, (int, float))
        or not math.isfinite(float(actual))
        or not math.isfinite(float(target))
        or float(target) <= 0
    ):
        raise ValueError("worker completion has malformed duration evidence")
    tolerance = output.get("duration_tolerance_seconds")
    expected_tolerance = max(2.0, abs(float(target)) * 0.02)
    if tolerance is None or (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(float(tolerance))
        or abs(float(tolerance) - expected_tolerance) > 1e-6
    ):
        raise ValueError("worker completion has malformed duration tolerance")
    if abs(float(actual) - float(target)) > expected_tolerance:
        raise ValueError("worker output duration is outside the accepted tolerance")
    if output.get("duration_within_tolerance") is not True:
        raise ValueError("worker completion did not confirm duration tolerance")


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

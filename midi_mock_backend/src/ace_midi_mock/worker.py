"""One-at-a-time durable rendering queue with restart-safe nonce recovery."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event
from typing import Any

from .config import MockSettings
from .corpus import CorpusManifest
from .db import DatabaseError, MockDatabase, MockJob
from .renderer import MidiRenderer, RenderError
from .transfer import SignedResultUploader, TransferError, validate_upload_url


class SubmissionError(ValueError):
    """Raised when an API submission is malformed or cannot be accepted."""


@dataclass(frozen=True, slots=True)
class Submission:
    application_job_id: str
    variation_index: int
    submission_nonce: str
    result_upload_url: str
    result_upload_max_bytes: int
    source: Mapping[str, Any] | None
    input_payload: Mapping[str, Any]


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SubmissionError(f"{label} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SubmissionError(f"{label} must be a UUID") from exc
    if str(parsed) != value.lower():
        raise SubmissionError(f"{label} must use canonical UUID syntax")
    return str(parsed)


def _bounded_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 128:
        raise SubmissionError(f"{label} must be a bounded object")
    for key in value:
        if not isinstance(key, str) or not key or len(key) > 128:
            raise SubmissionError(f"{label} contains an invalid field name")
    return dict(value)


def parse_submission(payload: Any, *, max_output_bytes: int) -> Submission:
    """Parse only the bounded identity/capability envelope; creative values are ignored."""

    if not isinstance(payload, dict):
        raise SubmissionError("submission body must be a JSON object")
    if payload.get("schema_version") != 2:
        raise SubmissionError("only worker schema version 2 is accepted")
    application_job_id = _canonical_uuid(payload.get("application_job_id"), "application_job_id")
    submission_nonce = _canonical_uuid(payload.get("submission_nonce"), "submission_nonce")
    variation_index = payload.get("variation_index")
    if (
        isinstance(variation_index, bool)
        or not isinstance(variation_index, int)
        or not 1 <= variation_index <= 4
    ):
        raise SubmissionError("variation_index must be between 1 and 4")
    upload = payload.get("result_upload")
    if not isinstance(upload, dict) or set(upload) != {"url", "max_bytes"}:
        raise SubmissionError("result_upload must contain exactly url and max_bytes")
    raw_url = upload.get("url")
    if not isinstance(raw_url, str):
        raise SubmissionError("result_upload.url must be a string")
    url = validate_upload_url(raw_url)
    max_bytes = upload.get("max_bytes")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 0 < max_bytes <= max_output_bytes
    ):
        raise SubmissionError("result_upload.max_bytes is outside the permitted range")
    source_value = payload.get("source")
    source = _bounded_mapping(source_value, "source") if source_value is not None else None
    input_payload = _bounded_mapping(payload.get("input"), "input")
    return Submission(
        application_job_id,
        variation_index,
        submission_nonce,
        url,
        max_bytes,
        source,
        input_payload,
    )


class MockWorker:
    """Background worker that never allocates a replacement corpus index."""

    def __init__(
        self,
        settings: MockSettings,
        manifest: CorpusManifest,
        database: MockDatabase,
        renderer: MidiRenderer | None = None,
        uploader: SignedResultUploader | None = None,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.database = database
        self.renderer = renderer or MidiRenderer(settings, manifest)
        self.uploader = uploader or SignedResultUploader(settings)
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._upload_caps: dict[str, tuple[str, int]] = {}
        self._cancel_events: dict[str, Event] = {}
        self._running_id: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self.settings.ensure_state_layout()
        recovered = self.database.recover_running_jobs()
        # The capability URL is intentionally not durable. The controller
        # retries the same nonce after a restart to reattach it; no token is
        # written to the mock database or logs.
        for job in recovered:
            self._queued.discard(job.external_uuid)
        self._task = asyncio.create_task(self._run(), name="ace-midi-mock-worker")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        for event in self._cancel_events.values():
            event.set()
        if task is not None:
            if self._running_id is None:
                task.cancel()
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                except TimeoutError:
                    task.cancel()
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            elif task.cancelled():
                pass
        self._upload_caps.clear()
        self._cancel_events.clear()
        await self.uploader.aclose()

    @property
    def running_id(self) -> str | None:
        return self._running_id

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit(self, submission: Submission) -> tuple[MockJob, bool]:
        job, created = self.database.claim_submission(
            application_job_id=submission.application_job_id,
            variation_index=submission.variation_index,
            submission_nonce=submission.submission_nonce,
            result_upload_url=submission.result_upload_url,
            result_upload_max_bytes=submission.result_upload_max_bytes,
        )
        self._upload_caps[job.external_uuid] = (
            submission.result_upload_url,
            submission.result_upload_max_bytes,
        )
        if job.state == "queued" and job.external_uuid not in self._queued:
            self._queued.add(job.external_uuid)
            self._queue.put_nowait(job.external_uuid)
        return job, created

    async def _run(self) -> None:
        while True:
            external_uuid = await self._queue.get()
            self._queued.discard(external_uuid)
            try:
                await self._process(external_uuid)
            finally:
                self._queue.task_done()

    async def _process(self, external_uuid: str) -> None:
        job = self.database.get(external_uuid)
        if job is None or job.state != "queued":
            return
        upload_capability = self._upload_caps.get(external_uuid)
        if upload_capability is None:
            # A restart can recover the claimed index but cannot recover a
            # signed capability. Leave it queued until the same nonce is
            # retried with that capability.
            return
        upload_url, upload_max_bytes = upload_capability
        event = Event()
        self._cancel_events[external_uuid] = event
        self._running_id = external_uuid
        job_directory = self.settings.temp_root / external_uuid  # type: ignore[operator]
        try:
            job = self.database.mark_running(external_uuid)
            rendered = await asyncio.wait_for(
                asyncio.to_thread(
                    self.renderer.render,
                    job.corpus_index,
                    job_directory,
                    cancelled=event,
                ),
                timeout=self.settings.render_timeout_seconds,
            )
            if event.is_set() or (self.database.get(external_uuid) or job).cancel_requested:
                self.database.mark_cancelled(external_uuid)
                return
            if rendered.byte_size > upload_max_bytes:
                self.database.mark_failed(external_uuid, "upload_source_invalid")
                return
            await asyncio.wait_for(
                self.uploader.upload(upload_url, rendered),
                timeout=self.settings.upload_timeout_seconds,
            )
            if event.is_set() or (self.database.get(external_uuid) or job).cancel_requested:
                self.database.mark_cancelled(external_uuid)
                return
            self.database.mark_succeeded(
                external_uuid,
                output_bytes=rendered.byte_size,
                output_sha256=rendered.sha256,
                duration_seconds=rendered.duration_seconds,
            )
        except TimeoutError:
            event.set()
            if (self.database.get(external_uuid) or job).state in {"queued", "running"}:
                self.database.mark_failed(external_uuid, "job_deadline")
        except RenderError as exc:
            if exc.code == "cancelled" or event.is_set():
                if (self.database.get(external_uuid) or job).state in {"queued", "running"}:
                    self.database.mark_cancelled(external_uuid)
            elif (self.database.get(external_uuid) or job).state in {"queued", "running"}:
                self.database.mark_failed(external_uuid, exc.code)
        except TransferError as exc:
            if (self.database.get(external_uuid) or job).state in {"queued", "running"}:
                self.database.mark_failed(external_uuid, exc.code)
        except (DatabaseError, OSError, ValueError):
            if (self.database.get(external_uuid) or job).state in {"queued", "running"}:
                self.database.mark_failed(external_uuid, "job_failed")
        finally:
            self._running_id = None
            self._cancel_events.pop(external_uuid, None)
            self._upload_caps.pop(external_uuid, None)
            try:
                for path in sorted(job_directory.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                job_directory.rmdir()
            except OSError:
                pass

    def cancel(self, external_uuid: str) -> tuple[MockJob | None, str]:
        job, outcome = self.database.request_cancel(external_uuid)
        if job is not None and outcome == "requested":
            event = self._cancel_events.get(external_uuid)
            if event is not None:
                event.set()
        return job, outcome

    def metadata(self, job: MockJob) -> dict[str, Any]:
        if job.output_bytes is None or job.output_sha256 is None or job.duration_seconds is None:
            raise DatabaseError("mock result output metadata is incomplete")
        seed_digest = hashlib.sha256(
            f"{job.application_job_id}:{job.variation_index}:{job.submission_nonce}".encode()
        ).digest()
        return {
            "schema_version": 2,
            "job_id": job.application_job_id,
            "variation_index": job.variation_index,
            "submission_nonce": job.submission_nonce,
            "status": "uploaded",
            "output": {
                "format": "mp3",
                "bytes": job.output_bytes,
                "sha256": job.output_sha256,
                "duration_seconds": job.duration_seconds,
                "effective_seed": int.from_bytes(seed_digest[:8], "big"),
                "corpus_index": job.corpus_index,
            },
            "worker": {
                "ace_tag": "mock/midi-sequential",
                "dit_model": "none",
                "lm_model": "none",
                "image_digest": "sha256:mock-midi-renderer",
                "gpu": "cpu",
                "model_bundle": {
                    "repo": "local/audioventura",
                    "revision": "0000000000000000000000000000000000000000",
                    "tag": self.settings.renderer_id,
                    "manifest_sha256": self.manifest.manifest_sha256,
                },
                "renderer": self.settings.renderer_id,
            },
        }

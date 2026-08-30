"""Background initialization and one-at-a-time ACE Node execution."""

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import platform
import queue
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from runpod_worker.handler import GenerationError
from runpod_worker.runtime import (
    DIT_MODEL,
    LM_MODEL,
    WorkerInitializationError,
    compute_local_runtime_receipt,
    initialize_runtime,
)

from .config import NodeSettings
from .db import NodeDatabase, NodeDatabaseError, NodeJob

LOGGER = logging.getLogger(__name__)

HEALTH_PHASES = frozenset(
    {
        "starting",
        "validating_runtime",
        "validating_model",
        "loading_dit",
        "loading_lm",
        "ready",
        "draining",
        "failed",
        "stopping",
    }
)
_INITIALIZATION_PHASES = frozenset(
    {"starting", "validating_runtime", "validating_model", "loading_dit", "loading_lm"}
)


class NodeRuntime(Protocol):
    def execute(self, payload: Mapping[str, Any], node_job_id: str) -> Mapping[str, Any]: ...


class AceStepNodeRuntime:
    """Lazy adapter around the existing strict RunPod handler."""

    def __init__(self, settings: NodeSettings) -> None:
        self.settings = settings
        self._runtime: Any | None = None

    def initialize(
        self, progress_callback: Callable[[str], None] | None = None
    ) -> AceStepNodeRuntime:
        emit = progress_callback or (lambda _phase: None)
        emit("starting")
        if not self.settings.application_revision:
            raise WorkerInitializationError("node application revision is missing")
        emit("validating_runtime")
        lock_path = self.settings.runtime_lock_path
        if not lock_path.is_absolute():
            lock_path = Path.cwd() / lock_path
        derived_receipt = compute_local_runtime_receipt(
            self.settings.application_revision,
            lock_path,
        )
        if (
            self.settings.runtime_receipt is not None
            and self.settings.runtime_receipt != derived_receipt
        ):
            raise WorkerInitializationError("node runtime receipt does not match checkout")
        receipt = derived_receipt
        environment = self.settings.model_environment()
        os.environ.update(environment)
        os.environ["ACE_WORKER_IMAGE_DIGEST"] = receipt
        emit("validating_model")
        self._runtime = initialize_runtime(
            self.settings.worker_hf_cache_root,
            progress_callback=progress_callback,
        )
        emit("ready")
        return self

    def execute(self, payload: Mapping[str, Any], node_job_id: str) -> Mapping[str, Any]:
        if self._runtime is None:
            raise WorkerInitializationError("node runtime is not initialized")
        from runpod_worker.handler import configure_runtime, handler

        configure_runtime(self._runtime)
        return handler({"id": node_job_id, "input": payload})


class NodeWorker:
    """Persistent serial queue with recovery-safe durable state."""

    def __init__(
        self,
        settings: NodeSettings,
        database: NodeDatabase | None = None,
        *,
        runtime_factory: Callable[[], Any] | None = None,
        executor: Callable[..., Mapping[str, Any]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database or NodeDatabase(settings.database_path)
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._executor = executor
        self._clock = clock or time.monotonic
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._payloads: dict[str, Mapping[str, Any]] = {}
        self._queued_ids: set[str] = set()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._init_thread: threading.Thread | None = None
        self._queue_thread: threading.Thread | None = None
        self._runtime: Any | None = None
        self._status = "stopped"
        self._phase = "stopping"
        self._accepting = False
        self._failure_code: str | None = None
        self._running_job_id: str | None = None
        self._running_started_at: float | None = None

    def _default_runtime_factory(self) -> AceStepNodeRuntime:
        return AceStepNodeRuntime(self.settings)

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queued_ids)

    @property
    def running_id(self) -> str | None:
        with self._lock:
            return self._running_job_id

    @property
    def failure_code(self) -> str | None:
        with self._lock:
            return self._failure_code

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def start(self) -> None:
        with self._lock:
            if self._status in {"initializing", "ready"}:
                raise RuntimeError("node worker is already started")
            self.settings.ensure_data_layout()
            self.database.initialize()
            self.database.recover()
            self._stop_event.clear()
            self._failure_code = None
            self._phase = "starting"
            self._accepting = True
            self._runtime = None
            self._running_job_id = None
            self._running_started_at = None
            self._queued_ids.clear()
            self._status = "initializing"
            self._init_thread = threading.Thread(
                target=self._initialize_in_background,
                name="ace-node-runtime-init",
                daemon=True,
            )
            self._init_thread.start()

    def _initialize_in_background(self) -> None:
        try:
            runtime = self._runtime_factory()
            initialize = getattr(runtime, "initialize", None)
            if callable(initialize):
                initialized = self._call_initialize(initialize)
                if initialized is not None:
                    runtime = initialized
        except Exception:
            LOGGER.error(
                "stage=runtime_init error_code=runtime_initialization_failed",
                extra={"component": "ace-node"},
            )
            with self._lock:
                self._failure_code = "runtime_initialization_failed"
                self._phase = "failed"
                self._accepting = False
                self._status = "failed"
            return
        with self._lock:
            if self._stop_event.is_set():
                return
            self._runtime = runtime
            self._phase = "ready" if self._accepting else "draining"
            self._status = "ready"
            self._queue_thread = threading.Thread(
                target=self._run_queue,
                name="ace-node-job-worker",
                daemon=True,
            )
            self._queue_thread.start()
        LOGGER.info("stage=runtime_ready component=ace-node")

    def _call_initialize(self, initialize: Callable[..., Any]) -> Any:
        """Pass the phase callback to new runtimes without breaking test doubles."""

        try:
            parameters = inspect.signature(initialize).parameters
        except (TypeError, ValueError):
            parameters = None
        accepts_callback = parameters is not None and (
            "progress_callback" in parameters
            or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
        )
        if accepts_callback:
            return initialize(progress_callback=self._set_phase)
        return initialize()

    def _set_phase(self, phase: str) -> None:
        if phase not in HEALTH_PHASES:
            raise WorkerInitializationError("node runtime reported an invalid initialization phase")
        with self._lock:
            if self._stop_event.is_set() or self._phase in {"failed", "stopping"}:
                return
            self._phase = phase
            if phase == "ready":
                self._status = "ready"
            elif phase == "draining":
                self._status = "ready"
            elif phase == "failed":
                self._status = "failed"
                self._accepting = False
            else:
                self._status = "initializing"

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._queue_thread
            init_thread = self._init_thread
            self._accepting = False
            self._phase = "stopping"
            self._status = "stopped"
            self._queue.put_nowait(None)
        if init_thread is not None:
            init_thread.join(timeout=5)
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            self._queue_thread = None
            self._init_thread = None
            self._runtime = None
            self._running_job_id = None
            self._running_started_at = None
            self._queued_ids.clear()

    def wait_ready(self, timeout: float = 10.0) -> str:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self.status in {"ready", "failed", "stopped"}:
                return self.status
            time.sleep(0.005)
        return self.status

    def health(self) -> dict[str, object]:
        with self._lock:
            phase = self._phase
            failure = self._failure_code
            running = self._running_job_id is not None
            running_started_at = self._running_started_at
            accepting = self._accepting
            queue_depth = len(self._queued_ids)
        if phase in _INITIALIZATION_PHASES:
            status = "initializing"
        elif phase in {"ready", "draining"}:
            status = "ready"
        elif phase == "failed":
            status = "failed"
        else:
            status = "stopping"
        elapsed: float | None = None
        if running and running_started_at is not None:
            elapsed = round(max(0.0, self._clock() - running_started_at), 3)
        accelerator = self.settings.accelerator
        if accelerator == "auto":
            accelerator = (
                "mps"
                if platform.system() == "Darwin"
                and platform.machine().lower() in {"arm64", "aarch64"}
                else "cuda"
            )
        return {
            "status": status,
            "phase": phase,
            "error_code": failure,
            "queue_depth": queue_depth,
            "running": running,
            "max_concurrency": 1,
            "running_elapsed_seconds": elapsed,
            "accepting": accepting,
            "accelerator": accelerator,
            "model": DIT_MODEL,
            "lm_model": LM_MODEL,
        }

    def drain(self) -> dict[str, object]:
        """Atomically stop accepting submissions while retaining current work."""

        with self._lock:
            self._accepting = False
            if self._phase in _INITIALIZATION_PHASES | {"ready"}:
                self._phase = "draining"
                self._status = "ready"
            return {
                "accepting": False,
                "running": self._running_job_id is not None,
                "queue_depth": len(self._queued_ids),
            }

    def submit(self, payload: Mapping[str, Any]) -> tuple[NodeJob, bool]:
        with self._lock:
            if self._status != "ready" or self._phase != "ready":
                raise RuntimeError("node runtime is not ready")
            if not self._accepting:
                raise RuntimeError("node runtime is draining")
            application_job_id = str(payload["application_job_id"])
            variation_index = int(payload["variation_index"])
            submission_nonce = str(payload["submission_nonce"])
            # The worker lock deliberately covers the durable insert and queue
            # admission so drain can never race a half-accepted submission.
            job, created = self.database.submit(
                application_job_id,
                variation_index,
                submission_nonce,
            )
            if created:
                raw_input = payload.get("input")
                if not isinstance(raw_input, Mapping):
                    raise NodeDatabaseError("validated submission input is missing")
                self._payloads[job.job_id] = copy.deepcopy(dict(raw_input))
                self._queued_ids.add(job.job_id)
                self._queue.put_nowait(job.job_id)
        return job, created

    def get(self, job_id: str) -> NodeJob | None:
        return self.database.get(job_id)

    def result(self, job: NodeJob) -> dict[str, Any] | None:
        return copy.deepcopy(job.result) if job.result is not None else None

    def cancel(self, job_id: str) -> tuple[NodeJob | None, str]:
        job, cancelled = self.database.cancel_pending(job_id)
        if job is None:
            return None, "not_found"
        if cancelled:
            with self._lock:
                self._payloads.pop(job_id, None)
                self._queued_ids.discard(job_id)
            return job, "cancelled"
        if job.state == "running":
            return job, "too_late"
        if job.state == "cancelled":
            return job, "cancelled"
        return job, "too_late"

    def _run_queue(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if job_id is None:
                self._queue.task_done()
                break
            with self._lock:
                self._queued_ids.discard(job_id)
            try:
                self._execute_one(job_id)
            finally:
                self._queue.task_done()

    def _execute_one(self, job_id: str) -> None:
        if not self.database.set_running(job_id):
            with self._lock:
                self._payloads.pop(job_id, None)
            return
        with self._lock:
            payload = self._payloads.get(job_id)
            runtime = self._runtime
            self._running_job_id = job_id
            self._running_started_at = self._clock()
        if payload is None or runtime is None:
            self.database.fail(job_id, "worker_restarted")
            with self._lock:
                self._running_job_id = None
                self._running_started_at = None
            return
        started = self._clock()
        try:
            result = self._invoke(runtime, payload, job_id)
            metadata = _durable_metadata(result)
            if self._clock() - started > self.settings.job_timeout_seconds:
                raise TimeoutError("node job timeout")
            self.database.succeed(job_id, metadata)
        except Exception as exc:
            code = _safe_error_code(exc)
            LOGGER.error(
                "stage=job_failed error_code=%s exception_class=%s",
                code,
                type(exc).__name__,
                extra={"component": "ace-node"},
            )
            try:
                self.database.fail(job_id, code)
            except NodeDatabaseError:
                LOGGER.error(
                    "stage=job_finalize error_code=node_state_update_failed",
                    extra={"component": "ace-node"},
                )
        finally:
            with self._lock:
                self._payloads.pop(job_id, None)
                self._running_job_id = None
                self._running_started_at = None

    def _invoke(self, runtime: Any, payload: Mapping[str, Any], job_id: str) -> Mapping[str, Any]:
        if self._executor is not None:
            try:
                parameters = inspect.signature(self._executor).parameters
                accepts_three = len(parameters) >= 3 or any(
                    parameter.kind is inspect.Parameter.VAR_POSITIONAL
                    for parameter in parameters.values()
                )
            except (TypeError, ValueError):
                accepts_three = True
            result = (
                self._executor(runtime, payload, job_id)
                if accepts_three
                else self._executor(payload, job_id)
            )
        else:
            method = getattr(runtime, "execute", None)
            if method is None:
                if not callable(runtime):
                    raise WorkerInitializationError("node runtime has no execute method")
                method = runtime
            result = method(payload, job_id)
        if not isinstance(result, Mapping):
            raise GenerationError("node runtime returned invalid metadata")
        return result


def _safe_error_code(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError):
        return "worker_timeout"
    if "transfer" in name or "upload" in name:
        return "upload_failed"
    if "schema" in name or "generation" in name:
        return "worker_failed"
    return "worker_failed"


_CREATIVE_KEYS = frozenset(
    {
        "prompt",
        "lyrics",
        "caption",
        "target_style",
        "source_style",
        "source_lyrics",
        "remix_guidance",
        "lm_negative_prompt",
        "input",
        "source",
        "result_upload",
        "effective",
        "generated_metadata",
        "audio",
    }
)
_SENSITIVE_KEY_PARTS = ("url", "path", "token", "secret", "capability", "audio_bytes")


def _durable_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the controller-required receipt while dropping creative secrets."""

    def clean(child: Any, key: str | None = None, depth: int = 0) -> Any:
        if depth > 6:
            raise ValueError("result metadata is too deeply nested")
        normalized = key.lower() if isinstance(key, str) else ""
        if normalized in _CREATIVE_KEYS or any(part in normalized for part in _SENSITIVE_KEY_PARTS):
            return None
        if isinstance(child, Mapping):
            result: dict[str, Any] = {}
            for raw_key, raw_value in child.items():
                if not isinstance(raw_key, str) or len(raw_key) > 128:
                    raise ValueError("result metadata has an invalid key")
                cleaned = clean(raw_value, raw_key, depth + 1)
                if cleaned is not None:
                    result[raw_key] = cleaned
            return result
        if isinstance(child, list):
            if len(child) > 128:
                raise ValueError("result metadata list is too large")
            return [clean(item, None, depth + 1) for item in child]
        if isinstance(child, str):
            if child.startswith(("http://", "https://")) or "/transfer/v1/" in child:
                return None
            if len(child) > 16_384:
                raise ValueError("result metadata text is too large")
            return child
        if child is None or isinstance(child, (bool, int, float)):
            return child
        raise ValueError("result metadata is not JSON-safe")

    cleaned = clean(dict(value))
    if not isinstance(cleaned, dict):
        raise ValueError("result metadata is not an object")
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > 65_536:
        raise ValueError("result metadata is too large")
    return cleaned

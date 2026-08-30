from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from ace_node.config import NodeSettings
from ace_node.db import NodeDatabase
from ace_node.worker import NodeWorker
from runpod_worker.transfer_client import TransferError

APP = "11111111-1111-4111-8111-111111111111"


def _settings(tmp_path: Path) -> NodeSettings:
    return NodeSettings(
        data_root=tmp_path, token="node-secret", runtime_receipt="sha256:" + "a" * 64
    )


def _payload(nonce: str, variation: int = 1) -> dict[str, Any]:
    return {
        "application_job_id": APP,
        "variation_index": variation,
        "submission_nonce": nonce,
        "input": {"schema_version": 2, "prompt": "creative text", "source": None},
        "source": None,
        "result_upload": {
            "url": "https://player.evren.io/transfer/v1/output/secret",
            "max_bytes": 10,
        },
    }


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, payload: dict[str, Any], job_id: str) -> dict[str, Any]:
        self.calls.append(job_id)
        return {
            "schema_version": 2,
            "job_id": payload.get("job_id", APP),
            "status": "uploaded",
            "output": {"bytes": 3, "sha256": "a" * 64},
            "input": {"prompt": "creative text"},
            "result_upload_url": "https://player.evren.io/transfer/v1/output/secret",
            "audio": "encoded-audio-must-not-persist",
        }


def test_worker_initializes_in_background_and_executes_serially(tmp_path: Path) -> None:
    runtime = _Runtime()
    worker = NodeWorker(_settings(tmp_path), runtime_factory=lambda: runtime)
    worker.start()
    assert worker.wait_ready() == "ready"
    first, created = worker.submit(_payload("22222222-2222-4222-8222-222222222222"))
    second, _ = worker.submit(_payload("33333333-3333-4333-8333-333333333333", variation=2))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if worker.get(second.job_id).state == "succeeded":  # type: ignore[union-attr]
            break
        time.sleep(0.01)
    assert created is True
    assert len(runtime.calls) == 2
    assert worker.get(first.job_id).state == "succeeded"  # type: ignore[union-attr]
    assert worker.get(first.job_id).result is not None  # type: ignore[union-attr]
    assert "https://" not in str(worker.get(first.job_id).result)  # type: ignore[union-attr]
    worker.stop()


def test_pending_cancel_does_not_execute(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowRuntime(_Runtime):
        def execute(self, payload: dict[str, Any], job_id: str) -> dict[str, Any]:
            entered.set()
            release.wait(2)
            return super().execute(payload, job_id)

    runtime = SlowRuntime()
    worker = NodeWorker(_settings(tmp_path), runtime_factory=lambda: runtime)
    worker.start()
    assert worker.wait_ready() == "ready"
    first, _ = worker.submit(_payload("22222222-2222-4222-8222-222222222222"))
    assert entered.wait(1)
    running_job, running_outcome = worker.cancel(first.job_id)
    assert running_job is not None and running_outcome == "too_late"
    second, _ = worker.submit(_payload("33333333-3333-4333-8333-333333333333", variation=2))
    job, outcome = worker.cancel(second.job_id)
    assert job is not None and outcome == "cancelled"
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and worker.get(first.job_id).state != "succeeded":  # type: ignore[union-attr]
        time.sleep(0.01)
    assert worker.get(second.job_id).state == "cancelled"  # type: ignore[union-attr]
    assert len(runtime.calls) == 1
    worker.stop()


def test_failed_runtime_keeps_service_state_failed(tmp_path: Path) -> None:
    def broken() -> Any:
        raise RuntimeError("secret path and capability")

    worker = NodeWorker(_settings(tmp_path), runtime_factory=broken)
    worker.start()
    assert worker.wait_ready() == "failed"
    assert worker.health()["error_code"] == "runtime_initialization_failed"
    worker.stop()


def test_process_recovery_fails_nonterminal_rows_without_resubmission(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = NodeDatabase(settings.database_path)
    database.initialize()
    queued, _ = database.submit(APP, 1, "22222222-2222-4222-8222-222222222222")
    running, _ = database.submit(APP, 2, "33333333-3333-4333-8333-333333333333")
    assert database.set_running(running.job_id)

    runtime = _Runtime()
    worker = NodeWorker(settings, database, runtime_factory=lambda: runtime)
    worker.start()
    assert worker.wait_ready() == "ready"
    assert worker.get(queued.job_id).state == "failed"  # type: ignore[union-attr]
    assert worker.get(running.job_id).state == "failed"  # type: ignore[union-attr]
    assert worker.get(queued.job_id).error_code == "worker_restarted"  # type: ignore[union-attr]
    assert worker.get(running.job_id).error_code == "worker_restarted"  # type: ignore[union-attr]
    assert runtime.calls == []
    worker.stop()


def test_upload_failure_is_terminal_and_safe(tmp_path: Path) -> None:
    def failed_upload(_payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        raise TransferError("signed capability must not appear in logs")

    worker = NodeWorker(
        _settings(tmp_path), runtime_factory=lambda: _Runtime(), executor=failed_upload
    )
    worker.start()
    assert worker.wait_ready() == "ready"
    job, _ = worker.submit(_payload("22222222-2222-4222-8222-222222222222"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = worker.get(job.job_id)
        if current is not None and current.state == "failed":
            break
        time.sleep(0.01)
    current = worker.get(job.job_id)
    assert current is not None and current.state == "failed"
    assert current.error_code == "upload_failed"
    worker.stop()

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from ace_node import __main__ as node_main
from ace_node.__main__ import (
    WorkerProcessReceipt,
    read_worker_receipt,
    recover_stale_process,
    write_worker_receipt,
)


def _receipt() -> WorkerProcessReceipt:
    return WorkerProcessReceipt(
        parent_pid=10,
        worker_pid=11,
        process_start_identity="start-identity",
        executable_path="/private/tmp/AudioVentura ACE Node/runtime/python",
        application_revision="a" * 40,
    )


def test_worker_receipt_is_atomic_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "state" / "worker.json"
    receipt = _receipt()
    write_worker_receipt(path, receipt)
    assert read_worker_receipt(path) == receipt
    assert path.stat().st_mode & 0o777 == 0o600

    payload = json.loads(path.read_text())
    payload["unexpected"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="malformed"):
        read_worker_receipt(path)


def test_worker_refuses_to_recover_its_own_supervisor_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "worker.json"
    receipt = WorkerProcessReceipt(
        parent_pid=10,
        worker_pid=42,
        process_start_identity="start-identity",
        executable_path="/private/tmp/AudioVentura ACE Node/runtime/python",
        application_revision="a" * 40,
    )
    write_worker_receipt(path, receipt)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        node_main,
        "os",
        types.SimpleNamespace(
            getpid=lambda: receipt.worker_pid, kill=lambda *args: signals.append(args)
        ),
    )
    monkeypatch.setattr(node_main, "receipt_matches_process", lambda *_args, **_kwargs: True)

    assert (
        recover_stale_process(
            path,
            expected_executable_path=receipt.executable_path,
            expected_application_revision=receipt.application_revision,
        )
        is False
    )
    assert signals == []
    assert path.exists()

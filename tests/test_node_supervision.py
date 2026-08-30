from __future__ import annotations

import json
from pathlib import Path

import pytest

from ace_node.__main__ import (
    WorkerProcessReceipt,
    read_worker_receipt,
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

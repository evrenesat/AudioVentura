from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ace_node.db import NodeDatabase, NodeDatabaseError, SubmissionConflict

APP = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"


def test_node_database_is_idempotent_and_has_no_payload_or_capability_columns(
    tmp_path: Path,
) -> None:
    database = NodeDatabase(tmp_path / "node.sqlite3")
    database.initialize()
    first, created = database.submit(APP, 1, NONCE)
    again, duplicate = database.submit(APP, 1, NONCE)
    assert created is True
    assert duplicate is False
    assert again.job_id == first.job_id
    with pytest.raises(SubmissionConflict):
        database.submit("33333333-3333-4333-8333-333333333333", 1, NONCE)

    connection = sqlite3.connect(str(tmp_path / "node.sqlite3"))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(node_jobs)")}
    connection.close()
    assert not columns & {"input", "source", "result_upload", "url", "audio", "prompt", "lyrics"}


def test_recovery_marks_queued_and_running_jobs_terminal(tmp_path: Path) -> None:
    database = NodeDatabase(tmp_path / "node.sqlite3")
    database.initialize()
    queued, _ = database.submit(APP, 1, NONCE)
    running, _ = database.submit(APP, 2, "33333333-3333-4333-8333-333333333333")
    assert database.set_running(running.job_id)
    assert database.recover() == 2
    assert database.get(queued.job_id).error_code == "worker_restarted"  # type: ignore[union-attr]
    assert database.get(running.job_id).state == "failed"  # type: ignore[union-attr]


def test_database_rejects_oversized_terminal_metadata(tmp_path: Path) -> None:
    database = NodeDatabase(tmp_path / "node.sqlite3")
    database.initialize()
    job, _ = database.submit(APP, 1, NONCE)
    assert database.set_running(job.job_id)
    with pytest.raises(NodeDatabaseError):
        database.succeed(job.job_id, {"audio": "encoded-audio-must-not-persist"})
    with pytest.raises(NodeDatabaseError):
        database.succeed(job.job_id, {"text": "x" * 70_000})

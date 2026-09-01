"""Small durable SQLite state machine for one ACE Node queue."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

NodeState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
NODE_SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 65_536
_DURABLE_SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "lyrics",
        "caption",
        "input",
        "source",
        "result_upload",
        "url",
        "path",
        "token",
        "secret",
        "capability",
        "audio_bytes",
        "audio",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "worker_restarted",
        "worker_timeout",
        "worker_failed",
        "runtime_reload_failed",
        "upload_failed",
    }
)


class NodeDatabaseError(RuntimeError):
    """Raised when durable node state cannot be trusted or updated."""


class SubmissionConflict(NodeDatabaseError):
    """Raised when a nonce is reused for a different immutable identity."""


@dataclass(frozen=True, slots=True)
class NodeJob:
    job_id: str
    application_job_id: str
    variation_index: int
    submission_nonce: str
    state: NodeState
    error_code: str | None
    result: dict[str, object] | None
    created_at: str
    updated_at: str

    def response(self, *, created: bool | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "job_id": self.job_id,
            "application_job_id": self.application_job_id,
            "variation_index": self.variation_index,
            "submission_nonce": self.submission_nonce,
            "status": self.state,
            "error_code": self.error_code,
        }
        if created is not None:
            value["created"] = created
        return value


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class NodeDatabase:
    """Thread-safe SQLite repository; creative payloads never have columns."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(100, min(int(busy_timeout_ms), 60_000))
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS node_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_jobs (
                    job_id TEXT PRIMARY KEY,
                    application_job_id TEXT NOT NULL,
                    variation_index INTEGER NOT NULL CHECK (variation_index BETWEEN 1 AND 4),
                    submission_nonce TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
                    ),
                    error_code TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_node_jobs_identity
                    ON node_jobs(application_job_id, variation_index, submission_nonce);
                INSERT OR IGNORE INTO node_schema(singleton, version, created_at)
                    VALUES (1, 1, '"""
                + utc_timestamp()
                + """');
                """
            )
            row = connection.execute(
                "SELECT version FROM node_schema WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row[0]) != NODE_SCHEMA_VERSION:
                raise NodeDatabaseError("node database schema version is not supported")

    def recover(self) -> int:
        """Mark all work interrupted by process recovery terminal."""

        timestamp = utc_timestamp()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE node_jobs SET state='failed', error_code='worker_restarted',
                   updated_at=? WHERE state IN ('queued', 'running')""",
                (timestamp,),
            )
            connection.commit()
            return int(cursor.rowcount)

    def submit(
        self,
        application_job_id: str,
        variation_index: int,
        submission_nonce: str,
    ) -> tuple[NodeJob, bool]:
        job_id = str(uuid.uuid4())
        timestamp = utc_timestamp()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            nonce_row = connection.execute(
                "SELECT * FROM node_jobs WHERE submission_nonce = ?", (submission_nonce,)
            ).fetchone()
            if nonce_row is not None:
                if (
                    str(nonce_row["application_job_id"]) != application_job_id
                    or int(nonce_row["variation_index"]) != variation_index
                ):
                    connection.rollback()
                    raise SubmissionConflict("submission nonce conflicts with immutable identity")
                connection.commit()
                return self._row_to_job(nonce_row), False
            try:
                connection.execute(
                    """INSERT INTO node_jobs(
                        job_id, application_job_id, variation_index, submission_nonce,
                        state, error_code, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)""",
                    (
                        job_id,
                        application_job_id,
                        variation_index,
                        submission_nonce,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise NodeDatabaseError("node submission could not be recorded") from exc
            row = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
            if row is None:
                raise NodeDatabaseError("node submission was not recorded")
            return self._row_to_job(row), True

    def get(self, job_id: str) -> NodeJob | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._row_to_job(row) if row is not None else None

    def set_running(self, job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE node_jobs SET state='running', updated_at=? "
                "WHERE job_id=? AND state='queued'",
                (utc_timestamp(), job_id),
            )
            return cursor.rowcount == 1

    def succeed(self, job_id: str, result: dict[str, object]) -> NodeJob:
        _validate_durable_result(result)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise NodeDatabaseError("node result metadata is too large")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE node_jobs SET state='succeeded', error_code=NULL, result_json=?, "
                "updated_at=? WHERE job_id=? AND state='running'",
                (encoded, utc_timestamp(), job_id),
            )
            if cursor.rowcount != 1:
                raise NodeDatabaseError("node job is not running")
            row = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NodeDatabaseError("node job disappeared")
            return self._row_to_job(row)

    def fail(self, job_id: str, error_code: str) -> NodeJob:
        safe_code = error_code.strip().lower()
        if safe_code not in _SAFE_ERROR_CODES:
            safe_code = "worker_failed"
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE node_jobs SET state='failed', error_code=?, updated_at=? "
                "WHERE job_id=? AND state NOT IN ('succeeded', 'failed', 'cancelled')",
                (safe_code, utc_timestamp(), job_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise NodeDatabaseError("node job disappeared")
                return self._row_to_job(row)
            row = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NodeDatabaseError("node job disappeared")
            return self._row_to_job(row)

    def cancel_pending(self, job_id: str) -> tuple[NodeJob | None, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None, False
            if row["state"] != "queued":
                connection.commit()
                return self._row_to_job(row), False
            connection.execute(
                "UPDATE node_jobs SET state='cancelled', updated_at=? "
                "WHERE job_id=? AND state='queued'",
                (utc_timestamp(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM node_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            connection.commit()
            if updated is None:
                raise NodeDatabaseError("node job disappeared")
            return self._row_to_job(updated), True

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> NodeJob:
        result: dict[str, object] | None = None
        raw_result = row["result_json"]
        if raw_result is not None:
            try:
                parsed = json.loads(str(raw_result))
            except (TypeError, ValueError) as exc:
                raise NodeDatabaseError("node result metadata is malformed") from exc
            if not isinstance(parsed, dict):
                raise NodeDatabaseError("node result metadata is malformed")
            _validate_durable_result(parsed)
            result = parsed
        state = str(row["state"])
        if state not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise NodeDatabaseError("node job state is invalid")
        return NodeJob(
            job_id=str(row["job_id"]),
            application_job_id=str(row["application_job_id"]),
            variation_index=int(row["variation_index"]),
            submission_nonce=str(row["submission_nonce"]),
            state=cast(NodeState, state),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            result=result,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    # Explicit aliases keep the repository convenient for white-box tests and
    # future operators without exposing a second state implementation.
    create_job = submit
    get_job = get
    recover_nonterminal = recover


def _validate_durable_result(value: object, *, depth: int = 0) -> None:
    """Reject unsafe values before terminal metadata reaches SQLite."""

    if depth > 6:
        raise NodeDatabaseError("node result metadata is too deeply nested")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise NodeDatabaseError("node result metadata has an invalid key")
            normalized = key.lower()
            if normalized in _DURABLE_SENSITIVE_KEYS or any(
                part in normalized for part in ("url", "path", "token", "secret", "capability")
            ):
                raise NodeDatabaseError("node result metadata contains private state")
            _validate_durable_result(child, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise NodeDatabaseError("node result metadata list is too large")
        for child in value:
            _validate_durable_result(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > 16_384 or value.startswith(("http://", "https://")):
            raise NodeDatabaseError("node result metadata contains private state")
        if "/transfer/v1/" in value:
            raise NodeDatabaseError("node result metadata contains private state")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise NodeDatabaseError("node result metadata is not JSON-safe")

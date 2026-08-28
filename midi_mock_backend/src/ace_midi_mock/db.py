"""Durable cursor allocation and job state for one mock instance."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .corpus import CorpusManifest

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class DatabaseError(RuntimeError):
    """Raised when durable mock state cannot be trusted or updated."""


class SubmissionConflict(DatabaseError):
    """Raised when a nonce is reused for a different logical submission."""


@dataclass(frozen=True, slots=True)
class MockJob:
    external_uuid: str
    application_job_id: str
    variation_index: int
    submission_nonce: str
    corpus_index: int
    member_basename: str
    member_sha256: str
    state: JobState
    error_code: str | None
    output_bytes: int | None
    output_sha256: str | None
    duration_seconds: float | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    result_upload_fingerprint: str
    cancel_requested: bool

    @property
    def id(self) -> str:
        return self.external_uuid

    def response(self) -> dict[str, object]:
        output = None
        if self.output_bytes is not None and self.output_sha256 is not None:
            output = {
                "bytes": self.output_bytes,
                "sha256": self.output_sha256,
                "duration_seconds": self.duration_seconds,
            }
        return {
            "job_id": self.external_uuid,
            "application_job_id": self.application_job_id,
            "variation_index": self.variation_index,
            "submission_nonce": self.submission_nonce,
            "corpus_index": self.corpus_index,
            "member_basename": self.member_basename,
            "member_sha256": self.member_sha256,
            "status": self.state,
            "error_code": self.error_code,
            "output": output,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def upload_fingerprint(url: str, max_bytes: int | None = None) -> str:
    identity = url if max_bytes is None else f"{url}\x00{max_bytes}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class MockDatabase:
    """Small SQLite repository with an atomic nonce/cursor claim."""

    def __init__(self, path: Path, manifest: CorpusManifest) -> None:
        self.path = path
        self.manifest = manifest
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS corpus_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    archive_sha256 TEXT NOT NULL,
                    member_count INTEGER NOT NULL CHECK (member_count > 0),
                    manifest_sha256 TEXT NOT NULL,
                    last_consumed_index INTEGER NOT NULL CHECK (last_consumed_index >= -1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    external_uuid TEXT PRIMARY KEY,
                    submission_nonce TEXT NOT NULL UNIQUE,
                    application_job_id TEXT NOT NULL,
                    variation_index INTEGER NOT NULL CHECK (variation_index BETWEEN 1 AND 4),
                    corpus_index INTEGER NOT NULL CHECK (corpus_index >= 0),
                    member_basename TEXT NOT NULL,
                    member_sha256 TEXT NOT NULL,
                    result_upload_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
                    ),
                    error_code TEXT,
                    output_bytes INTEGER,
                    output_sha256 TEXT,
                    duration_seconds REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at);
                """
            )
            row = connection.execute(
                """
                SELECT archive_sha256, member_count, manifest_sha256, last_consumed_index
                FROM corpus_state WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO corpus_state(
                        singleton, archive_sha256, member_count, manifest_sha256,
                        last_consumed_index, updated_at
                    ) VALUES (1, ?, ?, ?, -1, ?)
                    """,
                    (
                        self.manifest.archive_sha256,
                        self.manifest.member_count,
                        self.manifest.manifest_sha256,
                        utc_timestamp(),
                    ),
                )
            elif (
                row["archive_sha256"] != self.manifest.archive_sha256
                or row["member_count"] != self.manifest.member_count
                or row["manifest_sha256"] != self.manifest.manifest_sha256
                or not -1 <= row["last_consumed_index"] < self.manifest.member_count
            ):
                raise DatabaseError("mock database corpus identity does not match the manifest")
        finally:
            connection.close()

    @staticmethod
    def _job(row: sqlite3.Row) -> MockJob:
        return MockJob(
            external_uuid=str(row["external_uuid"]),
            application_job_id=str(row["application_job_id"]),
            variation_index=int(row["variation_index"]),
            submission_nonce=str(row["submission_nonce"]),
            corpus_index=int(row["corpus_index"]),
            member_basename=str(row["member_basename"]),
            member_sha256=str(row["member_sha256"]),
            state=row["state"],
            error_code=row["error_code"],
            output_bytes=int(row["output_bytes"]) if row["output_bytes"] is not None else None,
            output_sha256=row["output_sha256"],
            duration_seconds=(
                float(row["duration_seconds"]) if row["duration_seconds"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result_upload_fingerprint=str(row["result_upload_fingerprint"]),
            cancel_requested=bool(row["cancel_requested"]),
        )

    @staticmethod
    def _validate_identity(application_job_id: str, variation_index: int, nonce: str) -> None:
        try:
            uuid.UUID(application_job_id)
            uuid.UUID(nonce)
        except (ValueError, AttributeError, TypeError) as exc:
            raise DatabaseError("submission identity must contain UUIDs") from exc
        if not 1 <= variation_index <= 4:
            raise DatabaseError("variation index is outside the permitted range")

    def claim_submission(
        self,
        *,
        application_job_id: str,
        variation_index: int,
        submission_nonce: str,
        result_upload_url: str,
        result_upload_max_bytes: int | None = None,
    ) -> tuple[MockJob, bool]:
        """Claim one index and create its job in the same SQLite transaction."""

        self._validate_identity(application_job_id, variation_index, submission_nonce)
        fingerprint = upload_fingerprint(result_upload_url, result_upload_max_bytes)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT * FROM jobs WHERE submission_nonce = ?", (submission_nonce,)
            ).fetchone()
            if existing_row is not None:
                existing = self._job(existing_row)
                if (
                    existing.application_job_id != application_job_id
                    or existing.variation_index != variation_index
                    or existing.result_upload_fingerprint != fingerprint
                ):
                    raise SubmissionConflict(
                        "submission nonce identity conflicts with the original job"
                    )
                connection.commit()
                return existing, False
            state = connection.execute(
                "SELECT last_consumed_index FROM corpus_state WHERE singleton = 1"
            ).fetchone()
            if state is None:
                raise DatabaseError("corpus state is missing")
            corpus_index = (int(state["last_consumed_index"]) + 1) % self.manifest.member_count
            member = self.manifest.member(corpus_index)
            now = utc_timestamp()
            external_uuid = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs(
                    external_uuid, submission_nonce, application_job_id, variation_index,
                    corpus_index, member_basename, member_sha256, result_upload_fingerprint,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    external_uuid,
                    submission_nonce,
                    application_job_id,
                    variation_index,
                    corpus_index,
                    member.basename[:512],
                    member.sha256,
                    fingerprint,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE corpus_state SET last_consumed_index = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (corpus_index, now),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            if row is None:
                raise DatabaseError("claimed mock job disappeared before commit")
            connection.commit()
            return self._job(row), True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, external_uuid: str) -> MockJob | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            return self._job(row) if row is not None else None
        finally:
            connection.close()

    def queued_jobs(self) -> list[MockJob]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at, external_uuid"
            ).fetchall()
            return [self._job(row) for row in rows]
        finally:
            connection.close()

    def recover_running_jobs(self) -> list[MockJob]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_timestamp()
            connection.execute(
                """
                UPDATE jobs SET state = 'queued', started_at = NULL,
                    cancel_requested = 0, updated_at = ? WHERE state = 'running'
                """,
                (now,),
            )
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = 'queued' ORDER BY created_at, external_uuid"
            ).fetchall()
            connection.commit()
            return [self._job(row) for row in rows]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_running(self, external_uuid: str) -> MockJob:
        return self._transition(external_uuid, expected={"queued"}, state="running", started=True)

    def mark_succeeded(
        self, external_uuid: str, *, output_bytes: int, output_sha256: str, duration_seconds: float
    ) -> MockJob:
        return self._transition(
            external_uuid,
            expected={"running"},
            state="succeeded",
            output=(output_bytes, output_sha256, duration_seconds),
        )

    def mark_failed(self, external_uuid: str, error_code: str) -> MockJob:
        safe_code = "".join(
            character for character in error_code if character.isalnum() or character in "_-"
        )[:64]
        return self._transition(
            external_uuid,
            expected={"queued", "running"},
            state="failed",
            error_code=safe_code or "job_failed",
        )

    def mark_cancelled(self, external_uuid: str) -> MockJob:
        return self._transition(
            external_uuid,
            expected={"queued", "running"},
            state="cancelled",
            error_code="cancelled",
        )

    def request_cancel(self, external_uuid: str) -> tuple[MockJob | None, str]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None, "missing"
            state = row["state"]
            now = utc_timestamp()
            if state == "queued":
                connection.execute(
                    """
                    UPDATE jobs SET state = 'cancelled', cancel_requested = 1,
                        updated_at = ?, finished_at = ? WHERE external_uuid = ?
                    """,
                    (now, now, external_uuid),
                )
                outcome = "cancelled"
            elif state == "running":
                connection.execute(
                    "UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE external_uuid = ?",
                    (now, external_uuid),
                )
                outcome = "requested"
            else:
                outcome = "too_late"
            updated = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            connection.commit()
            assert updated is not None
            return self._job(updated), outcome
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _transition(
        self,
        external_uuid: str,
        *,
        expected: set[str],
        state: JobState,
        started: bool = False,
        error_code: str | None = None,
        output: tuple[int, str, float] | None = None,
    ) -> MockJob:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            if row is None:
                raise DatabaseError("mock job does not exist")
            if row["state"] not in expected:
                if row["state"] == state:
                    return self._job(row)
                raise DatabaseError("mock job state transition is not valid")
            now = utc_timestamp()
            values: list[object] = [state, now]
            assignments = ["state = ?", "updated_at = ?", "error_code = ?"]
            values.append(error_code)
            if started:
                assignments.append("started_at = ?")
                values.append(now)
            if state in {"succeeded", "failed", "cancelled"}:
                assignments.append("finished_at = ?")
                values.append(now)
            if output is not None:
                assignments.extend(
                    ["output_bytes = ?", "output_sha256 = ?", "duration_seconds = ?"]
                )
                values.extend(output)
            values.append(external_uuid)
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE external_uuid = ?", values
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE external_uuid = ?", (external_uuid,)
            ).fetchone()
            if updated is None:
                raise DatabaseError("mock job disappeared during transition")
            connection.commit()
            return self._job(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cursor_snapshot(self) -> dict[str, object]:
        connection = self._connect()
        try:
            state = connection.execute("SELECT * FROM corpus_state WHERE singleton = 1").fetchone()
            if state is None:
                raise DatabaseError("corpus state is missing")
            return {
                "archive_sha256": state["archive_sha256"],
                "member_count": state["member_count"],
                "manifest_sha256": state["manifest_sha256"],
                "last_consumed_index": state["last_consumed_index"],
                "updated_at": state["updated_at"],
            }
        finally:
            connection.close()

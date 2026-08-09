"""Ordered, explicit SQLite migration runner for the product database.

The runner is provider-neutral and operates outside the ORM on purpose: DDL
control and crash markers must never depend on model definitions.  Normal
application startup never applies migrations; only the explicit
``python -m ace_service migrate-upgrade --database <path>`` command changes
the schema, and it holds an exclusive sidecar process lock for the whole run.

Crash safety: a short transaction first persists and commits a
``migration_started`` marker, then a separate exclusive transaction applies
the ordered additive schema DDL and records the completed version. A crash between the
two leaves a durable incomplete marker; ``upgrade`` refuses to guess past it.
"""

from __future__ import annotations

import fcntl
import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

CURRENT_SCHEMA_VERSION = 5

_STATE_EXACT_EXPECTED = "exact_expected"
_STATE_UNVERSIONED_LEGACY = "unversioned_legacy"
_STATE_OLDER_VERSION = "older_version"
_STATE_UNKNOWN_NEWER = "unknown_newer"
_STATE_INCOMPLETE_STARTED = "incomplete_started"
_STATE_INCOMPLETE_FAILED = "incomplete_failed"
_STATE_MISSING = "missing"
_STATE_NOT_A_DATABASE = "not_a_database"
_STATE_CORRUPT = "corrupt"

_SQLITE_HEADER = b"SQLite format 3\x00"
_LOCK_SUFFIX = ".migration.lock"


class MigrationError(RuntimeError):
    """Raised when a migration operation must fail closed."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def database_identity_hash(db_path: str) -> str:
    """Return a non-secret identity for one resolved database path."""

    resolved = str(Path(db_path).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _resolve_database_path(db_path: str) -> Path:
    if not db_path or not db_path.strip():
        raise MigrationError("an explicit resolved database path is required")
    return Path(db_path).expanduser().resolve()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    """Open SQLite strictly read-only; no schema or journal writes are possible."""

    encoded = quote(str(path))
    return sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)


def _classify(report: dict[str, Any]) -> dict[str, Any]:
    """Classify a schema_version row into one declared durable state."""

    row = report["row"]
    if row is None:
        return {**report, "state": _STATE_UNVERSIONED_LEGACY, "version": None}
    status = row["status"]
    version = row["version"]
    if status == "migration_started":
        return {**report, "state": _STATE_INCOMPLETE_STARTED, "version": version}
    if status == "migration_failed":
        return {**report, "state": _STATE_INCOMPLETE_FAILED, "version": version}
    if status != "ready":
        return {**report, "state": _STATE_CORRUPT, "version": version}
    if not isinstance(version, int):
        return {**report, "state": _STATE_CORRUPT, "version": version}
    if version == CURRENT_SCHEMA_VERSION:
        return {**report, "state": _STATE_EXACT_EXPECTED, "version": version}
    if version < CURRENT_SCHEMA_VERSION:
        return {**report, "state": _STATE_OLDER_VERSION, "version": version}
    return {**report, "state": _STATE_UNKNOWN_NEWER, "version": version}


def migration_status(db_path: str) -> dict[str, Any]:
    """Read-only schema status for one explicit database path.

    Never writes to the database or its journals.  The returned report carries
    only a path hash (never the path itself), the declared state, the recorded
    version, and bounded marker timestamps.
    """

    path = _resolve_database_path(db_path)
    identity = database_identity_hash(str(path))
    report: dict[str, Any] = {"path_hash": identity, "row": None, "state": None, "version": None}
    if not path.is_file():
        return {**report, "state": _STATE_MISSING}
    try:
        with path.open("rb") as source:
            if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return {**report, "state": _STATE_NOT_A_DATABASE}
    except OSError as exc:
        return {**report, "state": _STATE_CORRUPT, "detail": str(exc)}
    try:
        connection = _read_only_connection(path)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "schema_version" not in tables:
                return {**report, "state": _STATE_UNVERSIONED_LEGACY}
            rows = connection.execute(
                "SELECT version, status, started_at, completed_at "
                "FROM schema_version WHERE singleton = 1"
            ).fetchall()
            if len(rows) != 1:
                return {**report, "state": _STATE_CORRUPT}
            version, status, started_at, completed_at = rows[0]
            return _classify(
                {
                    **report,
                    "row": {
                        "version": version,
                        "status": status,
                        "started_at": started_at,
                        "completed_at": completed_at,
                    },
                }
            )
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return {**report, "state": _STATE_CORRUPT}


def migration_is_expected(db_path: str) -> bool:
    """Return whether the database is at the exact expected schema version."""

    return bool(migration_status(db_path)["state"] == _STATE_EXACT_EXPECTED)


def _cp4_ddl(connection: sqlite3.Connection) -> None:
    """Apply the additive CP4 schema changes idempotently.

    New tables use ``CREATE TABLE IF NOT EXISTS`` and every new column is
    added only when missing, so the same statements are safe against both a
    pure legacy database and a foundation-created (``create_all``) database.
    """

    connection.execute(
        "CREATE TABLE IF NOT EXISTS submission_quotes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id VARCHAR(36) NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE, "
        "cost_fingerprint VARCHAR(64) NOT NULL, "
        "model_identity VARCHAR(256) NOT NULL, "
        "profile_id VARCHAR(128), "
        "duration_mode VARCHAR(32), "
        "duration_value_seconds REAL, "
        "variation_count INTEGER NOT NULL, "
        "eligible_gpu_ids TEXT, "
        "highest_trusted_hourly_rate_micro_usd INTEGER, "
        "highest_trusted_hourly_rate_usd VARCHAR(64), "
        "calibration_version INTEGER, "
        "predicted_execution_range_ms TEXT, "
        "quoted_amount_micro_usd INTEGER, "
        "quoted_range_low_micro_usd INTEGER, "
        "quoted_range_high_micro_usd INTEGER, "
        "currency VARCHAR(8) NOT NULL DEFAULT 'USD', "
        "rate_source VARCHAR(256), "
        "rate_version VARCHAR(64), "
        "unavailable_reason_code VARCHAR(32), "
        "captured_at DATETIME NOT NULL, "
        "CHECK (unavailable_reason_code IS NULL OR unavailable_reason_code IN ("
        "'rate_stale', 'rate_unknown', 'gpu_unknown', 'provider_unreachable', "
        "'calibration_missing')), "
        "CHECK ((unavailable_reason_code IS NULL) = (quoted_amount_micro_usd IS NOT NULL)), "
        "CHECK ((unavailable_reason_code IS NULL) = "
        "(highest_trusted_hourly_rate_micro_usd IS NOT NULL)), "
        "CHECK ((unavailable_reason_code IS NULL) = "
        "(highest_trusted_hourly_rate_usd IS NOT NULL)), "
        "CHECK (variation_count BETWEEN 1 AND 4), "
        "CHECK (currency = 'USD'))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS billing_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "provider VARCHAR(32) NOT NULL DEFAULT 'runpod', "
        "resource_type VARCHAR(32) NOT NULL, "
        "grouping_key VARCHAR(256) NOT NULL, "
        "bucket_start DATETIME NOT NULL, "
        "bucket_size_hours INTEGER NOT NULL DEFAULT 1, "
        "raw_amount VARCHAR(64) NOT NULL, "
        "raw_time_billed VARCHAR(64), "
        "currency VARCHAR(8) NOT NULL DEFAULT 'USD', "
        "fetched_at DATETIME NOT NULL, "
        "response_size_bytes INTEGER, "
        "is_network_volume INTEGER NOT NULL DEFAULT 0, "
        "source_contract VARCHAR(128) NOT NULL, "
        "documented_fields_json TEXT, "
        "checksum VARCHAR(64) NOT NULL UNIQUE, "
        "CHECK (bucket_size_hours IN (1, 24)), "
        "CHECK (currency = 'USD'), "
        "CHECK (is_network_volume IN (0, 1)), "
        "CHECK (length(checksum) = 64))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS billing_projections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "provider VARCHAR(32) NOT NULL, "
        "resource_type VARCHAR(32) NOT NULL, "
        "grouping_key VARCHAR(256) NOT NULL, "
        "bucket_start DATETIME NOT NULL, "
        "bucket_size_hours INTEGER NOT NULL DEFAULT 1, "
        "latest_amount VARCHAR(64) NOT NULL, "
        "latest_time_billed VARCHAR(64), "
        "currency VARCHAR(8) NOT NULL DEFAULT 'USD', "
        "last_updated_at DATETIME NOT NULL, "
        "latest_documented_fields_json TEXT, "
        "UNIQUE (provider, resource_type, grouping_key, bucket_start, "
        "bucket_size_hours, currency), "
        "CHECK (bucket_size_hours IN (1, 24)), "
        "CHECK (currency = 'USD'))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS gpu_rate_catalog ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "gpu_id VARCHAR(64) NOT NULL, "
        "provider VARCHAR(32) NOT NULL DEFAULT 'runpod', "
        "rate_micro_usd_per_hour INTEGER NOT NULL, "
        "hourly_rate_usd VARCHAR(64) NOT NULL, "
        "currency VARCHAR(8) NOT NULL DEFAULT 'USD', "
        "source VARCHAR(128) NOT NULL, "
        "calibration_version INTEGER NOT NULL, "
        "captured_at DATETIME NOT NULL, "
        "expires_at DATETIME NOT NULL, "
        "UNIQUE (gpu_id, provider, calibration_version), "
        "CHECK (rate_micro_usd_per_hour >= 0), "
        "CHECK (currency = 'USD'), "
        "CHECK (calibration_version >= 1))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS runtime_calibrations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version INTEGER NOT NULL UNIQUE, "
        "task_mode VARCHAR(32) NOT NULL, "
        "profile_id VARCHAR(128) NOT NULL, "
        "model_identity VARCHAR(256) NOT NULL, "
        "runtime_identity VARCHAR(256) NOT NULL, "
        "gpu_class VARCHAR(64) NOT NULL, "
        "duration_mode VARCHAR(32) NOT NULL, "
        "duration_band_min_seconds REAL NOT NULL, "
        "duration_band_max_seconds REAL NOT NULL, "
        "output_count INTEGER NOT NULL, "
        "execution_low_ms INTEGER NOT NULL, "
        "execution_high_ms INTEGER NOT NULL, "
        "evidence_source VARCHAR(256) NOT NULL, "
        "conservative_margin VARCHAR(64) NOT NULL, "
        "captured_at DATETIME NOT NULL, "
        "CHECK (version >= 1), "
        "CHECK (output_count BETWEEN 1 AND 4), "
        "CHECK (duration_band_min_seconds >= 0 AND "
        "duration_band_max_seconds >= duration_band_min_seconds), "
        "CHECK (execution_low_ms >= 0 AND execution_high_ms >= execution_low_ms))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS billing_lease ("
        "id INTEGER PRIMARY KEY, "
        "status VARCHAR(16) NOT NULL DEFAULT 'free', "
        "locked_by VARCHAR(128), "
        "locked_at DATETIME, "
        "expires_at DATETIME, "
        "CHECK (id = 1), "
        "CHECK (status IN ('free', 'locked')))"
    )
    connection.execute("INSERT OR IGNORE INTO billing_lease (id, status) VALUES (1, 'free')")

    columns = {
        "actual_gpu": "VARCHAR(128)",
        "model_identity": "VARCHAR(256)",
        "runtime_image_identity": "VARCHAR(256)",
        "execution_ms": "INTEGER",
        "hourly_rate_usd": "VARCHAR(64)",
        "hourly_rate_micro_usd": "INTEGER",
        "rate_currency": "VARCHAR(16) DEFAULT 'USD'",
        "rate_source": "VARCHAR(256)",
        "rate_captured_at": "DATETIME",
        "estimated_compute_micro_usd": "INTEGER",
        "evidence_status": "VARCHAR(16) NOT NULL DEFAULT 'pending'",
        "unavailable_reason": "VARCHAR(64)",
    }
    existing = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(variation_attempts)").fetchall()
    }
    for column_name, declaration in columns.items():
        if column_name not in existing:
            connection.execute(
                f"ALTER TABLE variation_attempts ADD COLUMN {column_name} {declaration}"
            )


def _cp5_ddl(connection: sqlite3.Connection) -> None:
    """Add projection-relative billing evidence identity without rewriting rows."""

    additions = {
        "billing_observations": {"evidence_checksum": "VARCHAR(64)"},
        "billing_projections": {"latest_evidence_checksum": "VARCHAR(64)"},
    }
    for table_name, columns in additions.items():
        existing = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, declaration in columns.items():
            if column_name not in existing:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_billing_observations_evidence_checksum "
        "ON billing_observations (evidence_checksum)"
    )


def _validate_migrated_schema(connection: sqlite3.Connection) -> None:
    """Fail closed unless every current product object and column is present."""

    expected_tables = {
        "submission_quotes",
        "billing_observations",
        "billing_projections",
        "gpu_rate_catalog",
        "runtime_calibrations",
        "billing_lease",
        "schema_version",
    }
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if row[0] != "sqlite_sequence"
    }
    missing_tables = expected_tables - tables
    if missing_tables:
        raise MigrationError(f"migrated schema is missing tables: {sorted(missing_tables)}")
    required_table_columns = {
        "submission_quotes": {
            "model_identity",
            "highest_trusted_hourly_rate_usd",
        },
        "billing_observations": {"documented_fields_json", "evidence_checksum"},
        "billing_projections": {
            "latest_documented_fields_json",
            "latest_evidence_checksum",
        },
        "gpu_rate_catalog": {"hourly_rate_usd"},
        "runtime_calibrations": {
            "version",
            "task_mode",
            "profile_id",
            "model_identity",
            "runtime_identity",
            "gpu_class",
            "duration_mode",
            "duration_band_min_seconds",
            "duration_band_max_seconds",
            "output_count",
            "execution_low_ms",
            "execution_high_ms",
            "evidence_source",
            "conservative_margin",
            "captured_at",
        },
    }
    for table_name, required in required_table_columns.items():
        actual = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        missing = required - actual
        if missing:
            raise MigrationError(
                f"migrated schema table {table_name} is missing columns: {sorted(missing)}"
            )
    observation_indexes = {
        str(row[1])
        for row in connection.execute("PRAGMA index_list(billing_observations)").fetchall()
    }
    if "ix_billing_observations_evidence_checksum" not in observation_indexes:
        raise MigrationError("migrated schema is missing billing evidence checksum index")
    existing_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(variation_attempts)").fetchall()
    }
    missing_columns = {
        "actual_gpu",
        "model_identity",
        "runtime_image_identity",
        "execution_ms",
        "hourly_rate_usd",
        "hourly_rate_micro_usd",
        "rate_currency",
        "rate_source",
        "rate_captured_at",
        "estimated_compute_micro_usd",
        "evidence_status",
        "unavailable_reason",
    } - existing_columns
    if missing_columns:
        raise MigrationError(f"migrated schema is missing columns: {sorted(missing_columns)}")


def _create_schema_version_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "version INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ("
        "'ready', 'migration_started', 'migration_failed')), "
        "started_at TEXT NOT NULL, "
        "completed_at TEXT)"
    )


def _mark_failed(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE schema_version SET status = 'migration_failed' WHERE singleton = 1")
    connection.commit()


def migration_upgrade(db_path: str) -> dict[str, Any]:
    """Upgrade one explicit database to the current schema version.

    The whole run holds an exclusive sidecar process lock.  A short
    transaction commits the durable ``migration_started`` marker first; a
    second exclusive transaction applies the additive DDL and records the
    completed version.  Any step-2 failure rolls back the DDL and persists a
    ``migration_failed`` marker; a crash leaves ``migration_started`` and the
    next upgrade refuses instead of guessing past it.
    """

    path = _resolve_database_path(db_path)
    if not path.is_file():
        raise MigrationError(
            f"database file does not exist (path hash {database_identity_hash(str(path))}); "
            "create and initialize the foundation database before upgrading"
        )
    with path.open("rb") as source:
        if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise MigrationError(
                f"path is not an SQLite database (path hash {database_identity_hash(str(path))})"
            )
    lock_path = Path(f"{path}{_LOCK_SUFFIX}")
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise MigrationError(
            "another migration process holds the exclusive sidecar lock; "
            "concurrent upgrades are refused"
        ) from exc
    try:
        return _upgrade_locked(path)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _upgrade_locked(path: Path) -> dict[str, Any]:
    report = migration_status(str(path))
    state = report["state"]
    if state == _STATE_EXACT_EXPECTED:
        return {
            "path_hash": report["path_hash"],
            "state": state,
            "version": CURRENT_SCHEMA_VERSION,
            "changed": False,
        }
    if state in {_STATE_INCOMPLETE_STARTED, _STATE_INCOMPLETE_FAILED}:
        raise MigrationError(
            f"refusing to upgrade: a previous migration is durably incomplete "
            f"(state={state}, path hash {report['path_hash']}); restore the verified "
            "pre-upgrade backup before retrying"
        )
    if state == _STATE_UNKNOWN_NEWER or (state == _STATE_OLDER_VERSION and report["version"] != 4):
        raise MigrationError(
            f"refusing to upgrade: database records schema version {report['version']} "
            f"which has no migration path to {CURRENT_SCHEMA_VERSION} "
            f"(path hash {report['path_hash']})"
        )
    if state not in {_STATE_UNVERSIONED_LEGACY, _STATE_OLDER_VERSION}:
        raise MigrationError(
            f"refusing to upgrade: database is not upgradeable (state={state}, "
            f"path hash {report['path_hash']})"
        )

    connection = sqlite3.connect(str(path))
    try:
        started_at = _iso(utc_now())
        try:
            # Step 1: short transaction — durable attempt marker.
            connection.execute("BEGIN IMMEDIATE")
            _create_schema_version_table(connection)
            connection.execute(
                "INSERT OR REPLACE INTO schema_version "
                "(singleton, version, status, started_at, completed_at) "
                "VALUES (1, ?, 'migration_started', ?, NULL)",
                (CURRENT_SCHEMA_VERSION, started_at),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise MigrationError(
                "migration attempt marker could not be persisted; nothing was changed"
            ) from exc

        try:
            # Step 2: exclusive transaction — additive DDL plus completion.
            connection.execute("BEGIN IMMEDIATE")
            if state == _STATE_UNVERSIONED_LEGACY:
                _cp4_ddl(connection)
            _cp5_ddl(connection)
            _validate_migrated_schema(connection)
            connection.execute(
                "UPDATE schema_version SET version = ?, status = 'ready', completed_at = ? "
                "WHERE singleton = 1",
                (CURRENT_SCHEMA_VERSION, _iso(utc_now())),
            )
            connection.commit()
        except (sqlite3.Error, MigrationError) as exc:
            connection.rollback()
            try:
                _mark_failed(connection)
            except sqlite3.Error:
                # The failure marker itself could not be written; the durable
                # step-1 marker still blocks automatic retries.
                pass
            raise MigrationError(
                "migration failed and was rolled back; the durable failure marker "
                "blocks automatic retries until a verified backup is restored"
            ) from exc
        return {
            "path_hash": report["path_hash"],
            "state": _STATE_EXACT_EXPECTED,
            "version": CURRENT_SCHEMA_VERSION,
            "changed": True,
        }
    finally:
        connection.close()


def describe_state(state: str) -> str:
    """Human-readable one-line description of one migration state."""

    return {
        _STATE_EXACT_EXPECTED: f"exact expected version ({CURRENT_SCHEMA_VERSION})",
        _STATE_UNVERSIONED_LEGACY: "unversioned legacy database (no schema_version table)",
        _STATE_OLDER_VERSION: "older schema version",
        _STATE_UNKNOWN_NEWER: "unknown/newer schema version",
        _STATE_INCOMPLETE_STARTED: "durable migration_started marker (crash or interruption)",
        _STATE_INCOMPLETE_FAILED: "durable migration_failed marker",
        _STATE_MISSING: "database file does not exist",
        _STATE_NOT_A_DATABASE: "path is not an SQLite database",
        _STATE_CORRUPT: "database is corrupt or unreadable",
    }[state]

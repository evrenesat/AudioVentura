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

CURRENT_SCHEMA_VERSION = 11
PROJECT_TITLE_MAX_LENGTH = 160

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


def _cp6_ddl(connection: sqlite3.Connection) -> None:
    """Add durable projects and backfill one same-type project per existing job."""

    connection.execute(
        "CREATE TABLE IF NOT EXISTS projects ("
        "id VARCHAR(36) PRIMARY KEY, "
        "job_type VARCHAR(8) NOT NULL, "
        f"title VARCHAR({PROJECT_TITLE_MAX_LENGTH}) NOT NULL, "
        "created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL)"
    )
    job_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
    if "project_id" not in job_columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN project_id VARCHAR(36) REFERENCES projects(id)"
        )
    connection.execute(
        "INSERT OR IGNORE INTO projects (id, job_type, title, created_at, updated_at) "
        "SELECT id, job_type, "
        f"substr(COALESCE(NULLIF(trim(sanitized_source_title), ''), "
        "NULLIF(trim(prompt), ''), CASE job_type WHEN 'original' THEN 'Original song' "
        f"ELSE 'Cover' END), 1, {PROJECT_TITLE_MAX_LENGTH}), created_at, updated_at FROM jobs"
    )
    connection.execute("UPDATE jobs SET project_id = id WHERE project_id IS NULL")
    connection.execute("CREATE INDEX IF NOT EXISTS ix_jobs_project_id ON jobs (project_id)")


def _cp7_ddl(connection: sqlite3.Connection) -> None:
    """Add durable provider ownership and backfill legacy Runpod provenance."""

    additions = {
        "jobs": {
            "inference_provider": "VARCHAR(32)",
            "current_provider_job_id": "VARCHAR(128)",
            "provider_result_json": "JSON",
        },
        "variation_attempts": {
            "inference_provider": "VARCHAR(32)",
            "provider_job_id": "VARCHAR(128)",
            "provider_result_json": "JSON",
        },
        "outputs": {
            "inference_provider": "VARCHAR(32)",
            "provider_job_id": "VARCHAR(128)",
        },
    }
    for table_name, columns in additions.items():
        existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {declaration}")

    conflicts = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE "
        "(inference_provider IS NOT NULL AND inference_provider != 'runpod' AND "
        " (NULLIF(current_runpod_job_id, '') IS NOT NULL OR runpod_result_json IS NOT NULL)) OR "
        "(NULLIF(current_provider_job_id, '') IS NOT NULL AND "
        " NULLIF(current_runpod_job_id, '') IS NOT NULL "
        " AND current_provider_job_id != current_runpod_job_id) OR "
        "(provider_result_json IS NOT NULL AND runpod_result_json IS NOT NULL "
        " AND json(provider_result_json) != json(runpod_result_json))"
    ).fetchone()
    if conflicts is None or int(conflicts[0]):
        raise MigrationError("provider migration found conflicting job provenance")
    conflicts = connection.execute(
        "SELECT COUNT(*) FROM variation_attempts WHERE "
        "(inference_provider IS NOT NULL AND inference_provider != 'runpod' AND "
        " (NULLIF(runpod_job_id, '') IS NOT NULL OR runpod_result_json IS NOT NULL)) OR "
        "(NULLIF(provider_job_id, '') IS NOT NULL AND NULLIF(runpod_job_id, '') IS NOT NULL "
        " AND provider_job_id != runpod_job_id) OR "
        "(provider_result_json IS NOT NULL AND runpod_result_json IS NOT NULL "
        " AND json(provider_result_json) != json(runpod_result_json))"
    ).fetchone()
    if conflicts is None or int(conflicts[0]):
        raise MigrationError("provider migration found conflicting attempt provenance")
    conflicts = connection.execute(
        "SELECT COUNT(*) FROM outputs WHERE "
        "(inference_provider IS NOT NULL AND inference_provider != 'runpod' "
        " AND NULLIF(runpod_job_id, '') IS NOT NULL) OR "
        "(NULLIF(provider_job_id, '') IS NOT NULL AND NULLIF(runpod_job_id, '') IS NOT NULL "
        " AND provider_job_id != runpod_job_id)"
    ).fetchone()
    if conflicts is None or int(conflicts[0]):
        raise MigrationError("provider migration found conflicting output provenance")

    connection.execute(
        "UPDATE jobs SET inference_provider = COALESCE(inference_provider, 'runpod'), "
        "current_provider_job_id = COALESCE(current_provider_job_id, "
        " NULLIF(current_runpod_job_id, '')), "
        "provider_result_json = COALESCE(provider_result_json, runpod_result_json)"
    )
    connection.execute(
        "UPDATE variation_attempts SET "
        "inference_provider = COALESCE(inference_provider, "
        " (SELECT inference_provider FROM jobs WHERE jobs.id = variation_attempts.job_id), "
        " 'runpod'), "
        "provider_job_id = COALESCE(provider_job_id, NULLIF(runpod_job_id, '')), "
        "provider_result_json = COALESCE(provider_result_json, runpod_result_json)"
    )
    connection.execute(
        "UPDATE outputs SET inference_provider = CASE "
        "WHEN NULLIF(runpod_job_id, '') IS NOT NULL "
        "THEN COALESCE(inference_provider, 'runpod') ELSE inference_provider END, "
        "provider_job_id = COALESCE(provider_job_id, NULLIF(runpod_job_id, ''))"
    )


def _cp8_ddl(connection: sqlite3.Connection) -> None:
    """Add immutable backend ownership and snapshots without rewriting mirrors."""

    additions = {
        "jobs": {
            "inference_backend": "VARCHAR(256)",
            "backend_snapshot_json": "JSON",
        },
        "variation_attempts": {"inference_backend": "VARCHAR(256)"},
        "outputs": {"inference_backend": "VARCHAR(256)"},
    }
    for table_name, columns in additions.items():
        existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {declaration}")

    for table_name in ("jobs", "variation_attempts", "outputs"):
        unknown = connection.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE inference_provider IS NOT NULL "
            "AND inference_provider NOT IN ('runpod', 'salad')"
        ).fetchone()
        if unknown is None or int(unknown[0]) != 0:
            raise MigrationError(f"backend migration found unsupported provider in {table_name}")

    connection.execute(
        "UPDATE jobs SET inference_provider = COALESCE(inference_provider, 'runpod')"
    )
    connection.execute(
        "UPDATE jobs SET inference_backend = CASE inference_provider "
        "WHEN 'runpod' THEN 'runpod/ace-step-v15-xl-turbo' "
        "WHEN 'salad' THEN 'salad/ace-step-v15-xl-turbo' END "
        "WHERE inference_backend IS NULL"
    )
    connection.execute(
        "UPDATE jobs SET backend_snapshot_json = json_object("
        "'backend_id', inference_backend, 'provider', inference_provider, "
        "'label', CASE inference_provider WHEN 'runpod' THEN 'Runpod · ACE-Step 1.5 XL Turbo' "
        "WHEN 'salad' THEN 'Salad · ACE-Step 1.5 XL Turbo' END, "
        "'catalog_revision', 'builtin-v1') WHERE backend_snapshot_json IS NULL"
    )
    connection.execute(
        "UPDATE variation_attempts SET inference_provider = COALESCE("
        "inference_provider, (SELECT inference_provider FROM jobs WHERE jobs.id = "
        "variation_attempts.job_id), 'runpod')"
    )
    connection.execute(
        "UPDATE variation_attempts SET inference_backend = CASE inference_provider "
        "WHEN 'runpod' THEN 'runpod/ace-step-v15-xl-turbo' "
        "WHEN 'salad' THEN 'salad/ace-step-v15-xl-turbo' END "
        "WHERE inference_backend IS NULL"
    )
    connection.execute(
        "UPDATE outputs SET inference_provider = COALESCE("
        "inference_provider, (SELECT inference_provider FROM jobs WHERE jobs.id = outputs.job_id))"
    )
    connection.execute(
        "UPDATE outputs SET inference_backend = CASE inference_provider "
        "WHEN 'runpod' THEN 'runpod/ace-step-v15-xl-turbo' "
        "WHEN 'salad' THEN 'salad/ace-step-v15-xl-turbo' END "
        "WHERE inference_backend IS NULL AND inference_provider IS NOT NULL"
    )


def _cp9_ddl(connection: sqlite3.Connection) -> None:
    """Add database-owned keep-warm state and the Web Push outbox."""

    connection.execute(
        "CREATE TABLE IF NOT EXISTS controller_settings ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "keep_warm_seconds INTEGER NOT NULL DEFAULT 900, "
        "updated_at DATETIME NOT NULL, "
        "CHECK (keep_warm_seconds IN (0, 60, 120, 180, 300, 600, 900, 1800, "
        "2700, 3600, 7200, 10800, 14400)))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO controller_settings (id, keep_warm_seconds, updated_at) "
        "VALUES (1, 900, ?)",
        (_iso(utc_now()),),
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS capacity_leases ("
        "capacity_key VARCHAR(256) PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
        "state VARCHAR(32) NOT NULL DEFAULT 'cold', session_id VARCHAR(36), "
        "idle_epoch_id VARCHAR(36), last_activity_at DATETIME, release_due_at DATETIME, "
        "warmed_at DATETIME, next_reminder_at DATETIME, release_requested_at DATETIME, "
        "released_at DATETIME, last_reconciled_at DATETIME, last_error_code VARCHAR(64), "
        "action_owner VARCHAR(128), action_lease_expires_at DATETIME, "
        "fencing_token INTEGER NOT NULL DEFAULT 0, "
        "updated_at DATETIME NOT NULL, "
        "CHECK (state IN ('cold', 'warming', 'retained', 'idle', 'releasing', 'release_overdue')), "
        "CHECK (length(capacity_key) BETWEEN 1 AND 256))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notification_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, event_key VARCHAR(256) NOT NULL UNIQUE, "
        "kind VARCHAR(64) NOT NULL, job_id VARCHAR(36) REFERENCES jobs(id) ON DELETE CASCADE, "
        "provider VARCHAR(32), capacity_key VARCHAR(256), title VARCHAR(128) NOT NULL, "
        "body VARCHAR(512) NOT NULL, target_path VARCHAR(1024) NOT NULL, "
        "created_at DATETIME NOT NULL, "
        "CHECK (kind IN ('generation_completed', 'managed_generation_started', "
        "'capacity_retained_reminder', 'capacity_release_warning', 'capacity_released', "
        "'capacity_release_overdue')))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        "id VARCHAR(36) PRIMARY KEY, endpoint VARCHAR(2048) NOT NULL UNIQUE, "
        "endpoint_origin VARCHAR(256) NOT NULL, p256dh VARCHAR(256) NOT NULL, "
        "auth VARCHAR(256) NOT NULL, "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, last_success_at DATETIME, "
        "disabled_at DATETIME)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notification_deliveries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL "
        "REFERENCES notification_events(id) ON DELETE CASCADE, "
        "subscription_id VARCHAR(36) NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE, "
        "status VARCHAR(16) NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at DATETIME NOT NULL, last_status_code INTEGER, delivered_at DATETIME, "
        "claimed_by VARCHAR(128), claim_expires_at DATETIME, "
        "fencing_token INTEGER NOT NULL DEFAULT 0, "
        "UNIQUE (event_id, subscription_id), "
        "CHECK (status IN ('pending', 'delivered', 'abandoned')), "
        "CHECK (attempt_count BETWEEN 0 AND 12))"
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_capacity_leases_state ON capacity_leases (state)",
        "CREATE INDEX IF NOT EXISTS ix_capacity_leases_release_due "
        "ON capacity_leases (release_due_at)",
        "CREATE INDEX IF NOT EXISTS ix_notification_events_created "
        "ON notification_events (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_due "
        "ON notification_deliveries (status, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS ix_push_subscriptions_active "
        "ON push_subscriptions (disabled_at)",
    ):
        connection.execute(statement)


def _cp10_ddl(connection: sqlite3.Connection) -> None:
    """Add the media-library, playlist, deletion-audit, and cancel state tables."""

    job_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, declaration in {
        "cancel_requested_at": "DATETIME",
        "cancel_completed_at": "DATETIME",
        "cancel_outcome": "VARCHAR(16)",
    }.items():
        if name not in job_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")

    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_items ("
        "id VARCHAR(36) PRIMARY KEY, "
        "project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
        "generated_output_id INTEGER UNIQUE REFERENCES outputs(id) ON DELETE SET NULL, "
        "kind VARCHAR(16) NOT NULL DEFAULT 'generated', "
        "title VARCHAR(300) NOT NULL, "
        "duration_seconds REAL, "
        "deletion_state VARCHAR(16) NOT NULL DEFAULT 'active', "
        "deletion_requested_at DATETIME, deleted_at DATETIME, "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "CHECK (kind IN ('generated', 'source')), "
        "CHECK (kind = 'source' OR generated_output_id IS NOT NULL), "
        "CHECK (deletion_state IN ('active', 'pending', 'deleted')), "
        "CHECK (duration_seconds IS NULL OR (duration_seconds > 0 AND "
        "duration_seconds = duration_seconds)))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_files ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "media_item_id VARCHAR(36) NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "storage_namespace VARCHAR(16) NOT NULL DEFAULT 'outputs', "
        "format VARCHAR(8) NOT NULL, relative_path VARCHAR(1024) NOT NULL, "
        "mime_type VARCHAR(128) NOT NULL, byte_size INTEGER NOT NULL, "
        "sha256 VARCHAR(64) NOT NULL, is_playback INTEGER NOT NULL DEFAULT 0, "
        "is_primary_download INTEGER NOT NULL DEFAULT 0, "
        "state VARCHAR(16) NOT NULL DEFAULT 'active', "
        "quarantine_relative_path VARCHAR(1024), deleted_at DATETIME, purged_at DATETIME, "
        "created_at DATETIME NOT NULL, UNIQUE (media_item_id, format), "
        "CHECK (storage_namespace IN ('outputs', 'library')), "
        "CHECK (format IN ('mp3', 'flac', 'wav')), "
        "CHECK (state IN ('active', 'quarantined', 'purged')), "
        "CHECK (byte_size > 0), CHECK (is_playback IN (0, 1)), "
        "CHECK (is_primary_download IN (0, 1)))"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_jobs_cancel_outcome_insert "
        "BEFORE INSERT ON jobs FOR EACH ROW WHEN NEW.cancel_outcome IS NOT NULL "
        "AND NEW.cancel_outcome NOT IN ('cancelled', 'too_late', 'unsupported') "
        "BEGIN SELECT RAISE(ABORT, 'invalid cancellation outcome'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_jobs_cancel_outcome_update "
        "BEFORE UPDATE OF cancel_outcome ON jobs FOR EACH ROW WHEN NEW.cancel_outcome IS NOT NULL "
        "AND NEW.cancel_outcome NOT IN ('cancelled', 'too_late', 'unsupported') "
        "BEGIN SELECT RAISE(ABORT, 'invalid cancellation outcome'); END"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS playlists ("
        "id VARCHAR(36) PRIMARY KEY, kind VARCHAR(16) NOT NULL, "
        "project_id VARCHAR(36) REFERENCES projects(id) ON DELETE CASCADE, "
        "title VARCHAR(160) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "UNIQUE (project_id), CHECK (kind IN ('project', 'custom')), "
        "CHECK ((kind = 'project' AND project_id IS NOT NULL) OR "
        "(kind = 'custom' AND project_id IS NULL)))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS playlist_entries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "playlist_id VARCHAR(36) NOT NULL REFERENCES playlists(id) ON DELETE CASCADE, "
        "media_item_id VARCHAR(36) NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "position INTEGER NOT NULL, created_at DATETIME NOT NULL, "
        "UNIQUE (playlist_id, position))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS project_deletion_audits ("
        "id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL UNIQUE, "
        "job_count INTEGER NOT NULL, media_item_count INTEGER NOT NULL, "
        "provider_summary_json JSON NOT NULL, cost_summary_json JSON, "
        "project_created_at DATETIME NOT NULL, deleted_at DATETIME NOT NULL)"
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_media_items_project_id ON media_items (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_items_generated_output_id "
        "ON media_items (generated_output_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_files_media_item_id ON media_files (media_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_playlists_project_id ON playlists (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_playlist_entries_playlist_id "
        "ON playlist_entries (playlist_id)",
        "CREATE INDEX IF NOT EXISTS ix_playlist_entries_media_item_id "
        "ON playlist_entries (media_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_project_deletion_audits_project_id "
        "ON project_deletion_audits (project_id)",
    ):
        connection.execute(statement)


def _cp11_ddl(connection: sqlite3.Connection) -> None:
    """Add source-first ingestion, asset transfer, and derivative state.

    This step is intentionally additive.  Existing schema-v10 source rows are
    experimental and cannot be assigned an owner without guessing, so the
    migration refuses them before adding the stronger v11 provenance rules.
    """

    existing_source = connection.execute(
        "SELECT COUNT(*) FROM media_items WHERE kind = 'source'"
    ).fetchone()
    if existing_source is None or int(existing_source[0]) != 0:
        raise MigrationError(
            "schema-v11 migration found existing source media rows; no-backfill is required"
        )

    connection.execute(
        "CREATE TABLE IF NOT EXISTS source_assets ("
        "id VARCHAR(36) PRIMARY KEY, "
        "project_id VARCHAR(36) NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE, "
        "origin VARCHAR(16) NOT NULL, "
        "status VARCHAR(24) NOT NULL DEFAULT 'queued', "
        "display_title VARCHAR(300) NOT NULL, "
        "youtube_url VARCHAR(2048), youtube_video_id VARCHAR(128), "
        "original_filename VARCHAR(300), declared_byte_size INTEGER, "
        "raw_relative_path VARCHAR(1024), raw_byte_size INTEGER, raw_sha256 VARCHAR(64), "
        "canonical_byte_size INTEGER, canonical_sha256 VARCHAR(64), duration_seconds REAL, "
        "rights_confirmation_at DATETIME NOT NULL, error_code VARCHAR(64), "
        "user_facing_error VARCHAR(500), attempt_count INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at DATETIME, started_at DATETIME, completed_at DATETIME, "
        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "CHECK (origin IN ('youtube', 'upload')), "
        "CHECK (status IN ('awaiting_upload', 'uploaded', 'queued', 'preparing', "
        "'ready', 'failed', 'cancelled')), "
        "CHECK ((origin = 'youtube' AND youtube_url IS NOT NULL AND youtube_video_id IS NOT NULL) "
        "OR (origin = 'upload' AND original_filename IS NOT NULL "
        "AND declared_byte_size IS NOT NULL)), "
        "CHECK (declared_byte_size IS NULL OR declared_byte_size > 0), "
        "CHECK (raw_byte_size IS NULL OR raw_byte_size > 0), "
        "CHECK (canonical_byte_size IS NULL OR canonical_byte_size > 0), "
        "CHECK (duration_seconds IS NULL OR (duration_seconds > 0 AND "
        "duration_seconds = duration_seconds)), "
        "CHECK ((status = 'ready') = (duration_seconds IS NOT NULL AND "
        "canonical_byte_size IS NOT NULL "
        "AND canonical_sha256 IS NOT NULL)))"
    )

    media_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(media_items)").fetchall()
    }
    if "source_asset_id" not in media_columns:
        connection.execute(
            "ALTER TABLE media_items ADD COLUMN source_asset_id VARCHAR(36) "
            "REFERENCES source_assets(id) ON DELETE CASCADE"
        )
    job_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, declaration in {
        "source_media_item_id": "VARCHAR(36) REFERENCES media_items(id) ON DELETE SET NULL",
        "source_clip_start_seconds": "REAL",
        "source_clip_end_seconds": "REAL",
        "source_clip_duration_seconds": "REAL",
    }.items():
        if name not in job_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")

    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_derivative_tasks ("
        "id VARCHAR(36) PRIMARY KEY, "
        "media_item_id VARCHAR(36) NOT NULL UNIQUE REFERENCES media_items(id) ON DELETE CASCADE, "
        "source_media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE, "
        "output_media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL, "
        "kind VARCHAR(24) NOT NULL DEFAULT 'mp3_playback', "
        "status VARCHAR(16) NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at DATETIME, error_code VARCHAR(64), user_facing_error VARCHAR(500), "
        "started_at DATETIME, completed_at DATETIME, created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL, CHECK (kind IN ('mp3_playback')), "
        "CHECK (status IN ('pending', 'running', 'ready', 'failed')), "
        "CHECK (attempt_count >= 0))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS asset_transfer_capabilities ("
        "id VARCHAR(36) PRIMARY KEY, token_sha256 VARCHAR(64) NOT NULL UNIQUE, "
        "direction VARCHAR(8) NOT NULL, purpose VARCHAR(32) NOT NULL, "
        "source_asset_id VARCHAR(36) REFERENCES source_assets(id) ON DELETE CASCADE, "
        "job_id VARCHAR(36) REFERENCES jobs(id) ON DELETE CASCADE, "
        "derivative_task_id VARCHAR(36) REFERENCES media_derivative_tasks(id) ON DELETE CASCADE, "
        "storage_namespace VARCHAR(16) NOT NULL, expected_relative_path VARCHAR(1024) NOT NULL, "
        "expected_extension VARCHAR(16) NOT NULL, expected_mime_type VARCHAR(128), "
        "expected_byte_size INTEGER, expected_sha256 VARCHAR(64), max_bytes INTEGER NOT NULL, "
        "status VARCHAR(16) NOT NULL DEFAULT 'issued', access_count INTEGER NOT NULL DEFAULT 0, "
        "received_byte_size INTEGER, received_sha256 VARCHAR(64), expires_at DATETIME NOT NULL, "
        "consumed_at DATETIME, created_at DATETIME NOT NULL, "
        "CHECK (direction IN ('upload', 'download')), "
        "CHECK (purpose IN ('browser_source_upload', 'home_source_download', "
        "'home_source_mp3_upload', 'home_clip_download', 'home_clip_upload', "
        "'home_derivative_download', 'home_derivative_upload')), "
        "CHECK (storage_namespace IN ('uploads', 'library', 'incoming', 'outputs')), "
        "CHECK (status IN ('issued', 'consumed', 'expired', 'revoked')), "
        "CHECK (((source_asset_id IS NOT NULL) + (job_id IS NOT NULL) + "
        "(derivative_task_id IS NOT NULL)) = 1), "
        "CHECK (max_bytes > 0), "
        "CHECK (received_byte_size IS NULL OR received_byte_size > 0))"
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_media_items_source_asset_id "
        "ON media_items (source_asset_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_media_items_source_asset_id "
        "ON media_items (source_asset_id) WHERE source_asset_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_jobs_source_media_item_id ON jobs (source_media_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_derivative_tasks_media_item_id "
        "ON media_derivative_tasks (media_item_id)",
        "CREATE INDEX IF NOT EXISTS ix_asset_transfer_capabilities_token_sha256 "
        "ON asset_transfer_capabilities (token_sha256)",
        "CREATE INDEX IF NOT EXISTS ix_asset_transfer_capabilities_source_asset_id "
        "ON asset_transfer_capabilities (source_asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_asset_transfer_capabilities_job_id "
        "ON asset_transfer_capabilities (job_id)",
        "CREATE INDEX IF NOT EXISTS ix_asset_transfer_capabilities_derivative_task_id "
        "ON asset_transfer_capabilities (derivative_task_id)",
    ):
        connection.execute(statement)
    for statement in (
        "CREATE TRIGGER IF NOT EXISTS trg_media_items_provenance_insert "
        "BEFORE INSERT ON media_items FOR EACH ROW WHEN NOT "
        "((NEW.kind = 'source' AND NEW.source_asset_id IS NOT NULL AND "
        "NEW.generated_output_id IS NULL) OR (NEW.kind = 'generated' AND "
        "NEW.source_asset_id IS NULL AND NEW.generated_output_id IS NOT NULL)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid media provenance'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_media_items_provenance_update "
        "BEFORE UPDATE OF kind, source_asset_id, generated_output_id ON media_items "
        "FOR EACH ROW WHEN NOT ((NEW.kind = 'source' AND NEW.source_asset_id IS NOT NULL "
        "AND NEW.generated_output_id IS NULL) OR (NEW.kind = 'generated' AND "
        "NEW.source_asset_id IS NULL AND NEW.generated_output_id IS NOT NULL)) "
        "BEGIN SELECT RAISE(ABORT, 'invalid media provenance'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_media_derivative_ready_insert "
        "BEFORE INSERT ON media_derivative_tasks FOR EACH ROW WHEN NEW.status = 'ready' "
        "AND NEW.output_media_file_id IS NULL BEGIN SELECT RAISE(ABORT, "
        "'ready derivative has no output'); END",
        "CREATE TRIGGER IF NOT EXISTS trg_media_derivative_ready_update "
        "BEFORE UPDATE OF status, output_media_file_id ON media_derivative_tasks "
        "FOR EACH ROW WHEN NEW.status = 'ready' AND NEW.output_media_file_id IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'ready derivative has no output'); END",
    ):
        connection.execute(statement)


def _validate_migrated_schema(connection: sqlite3.Connection) -> None:
    """Fail closed unless every current product object and column is present."""

    expected_tables = {
        "projects",
        "submission_quotes",
        "billing_observations",
        "billing_projections",
        "gpu_rate_catalog",
        "runtime_calibrations",
        "billing_lease",
        "schema_version",
        "controller_settings",
        "capacity_leases",
        "notification_events",
        "push_subscriptions",
        "notification_deliveries",
        "media_items",
        "media_files",
        "source_assets",
        "asset_transfer_capabilities",
        "media_derivative_tasks",
        "playlists",
        "playlist_entries",
        "project_deletion_audits",
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
        "projects": {"id", "job_type", "title", "created_at", "updated_at"},
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
    required_table_columns.update(
        {
            "jobs": {
                "inference_provider",
                "inference_backend",
                "backend_snapshot_json",
                "current_provider_job_id",
                "provider_result_json",
                "cancel_requested_at",
                "cancel_completed_at",
                "cancel_outcome",
                "source_media_item_id",
                "source_clip_start_seconds",
                "source_clip_end_seconds",
                "source_clip_duration_seconds",
            },
            "variation_attempts": {
                "inference_provider",
                "inference_backend",
                "provider_job_id",
                "provider_result_json",
            },
            "outputs": {"inference_provider", "inference_backend", "provider_job_id"},
            "media_items": {
                "id",
                "project_id",
                "generated_output_id",
                "source_asset_id",
                "kind",
                "title",
                "duration_seconds",
                "deletion_state",
                "deletion_requested_at",
                "deleted_at",
                "created_at",
                "updated_at",
            },
            "media_files": {
                "id",
                "media_item_id",
                "storage_namespace",
                "format",
                "relative_path",
                "mime_type",
                "byte_size",
                "sha256",
                "is_playback",
                "is_primary_download",
                "state",
                "quarantine_relative_path",
                "deleted_at",
                "purged_at",
                "created_at",
            },
            "playlists": {"id", "kind", "project_id", "title", "created_at", "updated_at"},
            "playlist_entries": {
                "id",
                "playlist_id",
                "media_item_id",
                "position",
                "created_at",
            },
            "project_deletion_audits": {
                "id",
                "project_id",
                "job_count",
                "media_item_count",
                "provider_summary_json",
                "cost_summary_json",
                "project_created_at",
                "deleted_at",
            },
            "source_assets": {
                "id",
                "project_id",
                "origin",
                "status",
                "display_title",
                "youtube_url",
                "youtube_video_id",
                "original_filename",
                "declared_byte_size",
                "raw_relative_path",
                "raw_byte_size",
                "raw_sha256",
                "canonical_byte_size",
                "canonical_sha256",
                "duration_seconds",
                "rights_confirmation_at",
                "error_code",
                "user_facing_error",
                "attempt_count",
                "next_attempt_at",
                "started_at",
                "completed_at",
                "created_at",
                "updated_at",
            },
            "asset_transfer_capabilities": {
                "id",
                "token_sha256",
                "direction",
                "purpose",
                "source_asset_id",
                "job_id",
                "derivative_task_id",
                "storage_namespace",
                "expected_relative_path",
                "expected_extension",
                "expected_mime_type",
                "expected_byte_size",
                "expected_sha256",
                "max_bytes",
                "status",
                "access_count",
                "received_byte_size",
                "received_sha256",
                "expires_at",
                "consumed_at",
                "created_at",
            },
            "media_derivative_tasks": {
                "id",
                "media_item_id",
                "source_media_file_id",
                "output_media_file_id",
                "kind",
                "status",
                "attempt_count",
                "next_attempt_at",
                "error_code",
                "user_facing_error",
                "started_at",
                "completed_at",
                "created_at",
                "updated_at",
            },
        }
    )
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
    job_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
    if "project_id" not in job_columns:
        raise MigrationError("migrated schema jobs table is missing project_id")
    job_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(jobs)").fetchall()}
    if "ix_jobs_project_id" not in job_indexes:
        raise MigrationError("migrated schema is missing the job membership index")
    required_new_columns = {
        "controller_settings": {"id", "keep_warm_seconds", "updated_at"},
        "capacity_leases": {
            "capacity_key",
            "provider",
            "state",
            "session_id",
            "idle_epoch_id",
            "last_activity_at",
            "release_due_at",
            "warmed_at",
            "next_reminder_at",
            "release_requested_at",
            "released_at",
            "last_reconciled_at",
            "last_error_code",
            "action_owner",
            "action_lease_expires_at",
            "fencing_token",
            "updated_at",
        },
        "notification_events": {
            "id",
            "event_key",
            "kind",
            "job_id",
            "provider",
            "capacity_key",
            "title",
            "body",
            "target_path",
            "created_at",
        },
        "push_subscriptions": {
            "id",
            "endpoint",
            "endpoint_origin",
            "p256dh",
            "auth",
            "created_at",
            "updated_at",
            "last_success_at",
            "disabled_at",
        },
        "notification_deliveries": {
            "id",
            "event_id",
            "subscription_id",
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_status_code",
            "delivered_at",
            "claimed_by",
            "claim_expires_at",
            "fencing_token",
        },
    }
    for table_name, required in required_new_columns.items():
        actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})")}
        missing = required - actual
        if missing:
            raise MigrationError(
                f"migrated schema table {table_name} is missing columns: {sorted(missing)}"
            )
    invalid_membership = connection.execute(
        "SELECT COUNT(*) FROM jobs LEFT JOIN projects ON projects.id = jobs.project_id "
        "WHERE jobs.project_id IS NULL OR projects.id IS NULL "
        "OR projects.job_type != jobs.job_type"
    ).fetchone()
    if invalid_membership is None or int(invalid_membership[0]) != 0:
        raise MigrationError("migrated schema contains invalid project membership")
    invalid_projects = connection.execute(
        "SELECT COUNT(*) FROM projects WHERE job_type NOT IN ('original', 'cover') "
        "OR trim(title) = '' OR length(title) > ?",
        (PROJECT_TITLE_MAX_LENGTH,),
    ).fetchone()
    if invalid_projects is None or int(invalid_projects[0]) != 0:
        raise MigrationError("migrated schema contains invalid project records")
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
    invalid_media = connection.execute(
        "SELECT COUNT(*) FROM media_items WHERE trim(title) = '' "
        "OR length(title) > 300 OR kind NOT IN ('generated', 'source') "
        "OR deletion_state NOT IN ('active', 'pending', 'deleted')"
    ).fetchone()
    if invalid_media is None or int(invalid_media[0]) != 0:
        raise MigrationError("migrated schema contains invalid media items")
    invalid_cancel = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE cancel_outcome IS NOT NULL "
        "AND cancel_outcome NOT IN ('cancelled', 'too_late', 'unsupported')"
    ).fetchone()
    if invalid_cancel is None or int(invalid_cancel[0]) != 0:
        raise MigrationError("migrated schema contains invalid cancellation outcomes")
    invalid_source = connection.execute(
        "SELECT COUNT(*) FROM source_assets WHERE origin NOT IN ('youtube', 'upload') "
        "OR status NOT IN ('awaiting_upload', 'uploaded', 'queued', 'preparing', "
        "'ready', 'failed', 'cancelled') "
        "OR trim(display_title) = '' OR length(display_title) > 300 "
        "OR (status = 'ready' AND (duration_seconds IS NULL OR canonical_byte_size IS NULL "
        "OR canonical_sha256 IS NULL))"
    ).fetchone()
    if invalid_source is None or int(invalid_source[0]) != 0:
        raise MigrationError("migrated schema contains invalid source assets")
    invalid_provenance = connection.execute(
        "SELECT COUNT(*) FROM media_items WHERE "
        "NOT ((kind = 'source' AND source_asset_id IS NOT NULL AND generated_output_id IS NULL) "
        "OR (kind = 'generated' AND source_asset_id IS NULL AND generated_output_id IS NOT NULL))"
    ).fetchone()
    if invalid_provenance is None or int(invalid_provenance[0]) != 0:
        raise MigrationError("migrated schema contains invalid media provenance")
    for table_name, expected in (
        (
            "media_items",
            {"trg_media_items_provenance_insert", "trg_media_items_provenance_update"},
        ),
        (
            "media_derivative_tasks",
            {"trg_media_derivative_ready_insert", "trg_media_derivative_ready_update"},
        ),
    ):
        actual = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name = ?",
                (table_name,),
            )
        }
        if not expected.issubset(actual):
            raise MigrationError(f"migrated schema is missing {table_name} invariants")
    source_indexes = {
        str(row[1]) for row in connection.execute("PRAGMA index_list(media_items)").fetchall()
    }
    if "ux_media_items_source_asset_id" not in source_indexes:
        raise MigrationError("migrated schema is missing source media uniqueness")


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
    if state == _STATE_UNKNOWN_NEWER or (
        state == _STATE_OLDER_VERSION and report["version"] not in {4, 5, 6, 7, 8, 9, 10}
    ):
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
            if state == _STATE_UNVERSIONED_LEGACY or report["version"] == 4:
                _cp5_ddl(connection)
            if state == _STATE_UNVERSIONED_LEGACY or report["version"] in {4, 5}:
                _cp6_ddl(connection)
            _cp7_ddl(connection)
            _cp8_ddl(connection)
            _cp9_ddl(connection)
            _cp10_ddl(connection)
            _cp11_ddl(connection)
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

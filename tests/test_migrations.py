"""Migration runner tests: read-only status, explicit upgrade, crash markers,
sidecar lock, SQLite backup/integrity, and legacy reads on a migrated copy."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from ace_service.costs import observation_checksum
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    database_identity_hash,
    migration_is_expected,
    migration_status,
    migration_upgrade,
)
from ace_service.repository import get_job, record_billing_observation
from tests.conftest import create_legacy_database_copy


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _schema_version_row(path: Path) -> tuple[int, str] | None:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT version, status FROM schema_version WHERE singleton = 1"
        ).fetchall()
        return (int(rows[0][0]), str(rows[0][1])) if rows else None
    finally:
        connection.close()


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(str(path))
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"
    finally:
        connection.close()


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if row[0] != "sqlite_sequence"
        }
    finally:
        connection.close()


def _attempt_columns(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(variation_attempts)").fetchall()
        }
    finally:
        connection.close()


def _columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        connection.close()


def _indexes(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA index_list({table})").fetchall()}
    finally:
        connection.close()


def _prepare_v4_database(path: Path) -> None:
    """Apply the production-shaped CP4 v4 DDL and ready marker."""

    import ace_service.migrations as migrations_module

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        migrations_module._create_schema_version_table(connection)
        migrations_module._cp4_ddl(connection)
        connection.execute(
            "INSERT INTO schema_version "
            "(singleton, version, status, started_at, completed_at) "
            "VALUES (1, 4, 'ready', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


def _prepare_v5_database(path: Path) -> None:
    """Apply the production-shaped v5 DDL and ready marker."""

    import ace_service.migrations as migrations_module

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        migrations_module._create_schema_version_table(connection)
        migrations_module._cp4_ddl(connection)
        migrations_module._cp5_ddl(connection)
        connection.execute(
            "INSERT INTO schema_version "
            "(singleton, version, status, started_at, completed_at) "
            "VALUES (1, 5, 'ready', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


class TestStatusClassification:
    def test_status_is_read_only_for_legacy_database(self, legacy_database_path: Path) -> None:
        report = migration_status(str(legacy_database_path))
        assert report["state"] == "unversioned_legacy"
        assert report["path_hash"] == database_identity_hash(str(legacy_database_path))
        # Read-only probe must not create tables, journals, or the -wal file.
        assert "schema_version" not in _tables(legacy_database_path)
        assert not list(legacy_database_path.parent.glob("service.db-wal"))
        assert not list(legacy_database_path.parent.glob("service.db-journal"))

    def test_status_exact_expected_after_upgrade(self, migrated_database_path: Path) -> None:
        report = migration_status(str(migrated_database_path))
        assert report["state"] == "exact_expected"
        assert report["version"] == CURRENT_SCHEMA_VERSION
        assert migration_is_expected(str(migrated_database_path))

    def test_status_older_newer_and_incomplete_markers(self, legacy_database_path: Path) -> None:
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            connection.execute(
                "CREATE TABLE schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "version INTEGER NOT NULL, "
                "status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, "
                "completed_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_version "
                "(singleton, version, status, started_at, completed_at) "
                "VALUES (1, 3, 'ready', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()
        assert migration_status(str(legacy_database_path))["state"] == "older_version"

        connection = sqlite3.connect(str(legacy_database_path))
        try:
            connection.execute("UPDATE schema_version SET version = 99 WHERE singleton = 1")
            connection.commit()
        finally:
            connection.close()
        assert migration_status(str(legacy_database_path))["state"] == "unknown_newer"

        for status in ("migration_started", "migration_failed"):
            connection = sqlite3.connect(str(legacy_database_path))
            try:
                connection.execute(
                    "UPDATE schema_version SET version = 4, status = ? WHERE singleton = 1",
                    (status,),
                )
                connection.commit()
            finally:
                connection.close()
            assert migration_status(str(legacy_database_path))["state"] == (
                "incomplete_started" if status == "migration_started" else "incomplete_failed"
            )

    def test_status_missing_and_non_database_paths(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.db"
        assert migration_status(str(missing))["state"] == "missing"
        not_a_database = tmp_path / "plain.txt"
        not_a_database.write_text("not sqlite at all")
        assert migration_status(str(not_a_database))["state"] == "not_a_database"


class TestUpgrade:
    def test_upgrade_migrates_legacy_database_exactly_once(
        self, legacy_database_path: Path
    ) -> None:
        result = migration_upgrade(str(legacy_database_path))
        assert result["changed"] is True
        assert result["version"] == CURRENT_SCHEMA_VERSION
        assert _schema_version_row(legacy_database_path) == (CURRENT_SCHEMA_VERSION, "ready")
        assert _integrity_ok(legacy_database_path)
        expected_tables = {
            "jobs",
            "outputs",
            "variation_attempts",
            "transfer_capabilities",
            "schema_version",
            "submission_quotes",
            "billing_observations",
            "billing_projections",
            "gpu_rate_catalog",
            "runtime_calibrations",
            "billing_lease",
            "projects",
            "controller_settings",
            "capacity_leases",
            "notification_events",
            "push_subscriptions",
            "notification_deliveries",
            "media_items",
            "media_files",
            "playlists",
            "playlist_entries",
            "project_deletion_audits",
        }
        assert _tables(legacy_database_path) == expected_tables
        assert {
            "actual_gpu",
            "execution_ms",
            "hourly_rate_micro_usd",
            "hourly_rate_usd",
            "rate_currency",
            "evidence_status",
            "unavailable_reason",
        }.issubset(_attempt_columns(legacy_database_path))
        assert {"model_identity", "highest_trusted_hourly_rate_usd"}.issubset(
            _columns(legacy_database_path, "submission_quotes")
        )
        assert "hourly_rate_usd" in _columns(legacy_database_path, "gpu_rate_catalog")
        assert "conservative_margin" in _columns(legacy_database_path, "runtime_calibrations")
        assert "evidence_checksum" in _columns(legacy_database_path, "billing_observations")
        assert "latest_evidence_checksum" in _columns(legacy_database_path, "billing_projections")
        assert "ix_billing_observations_evidence_checksum" in _indexes(
            legacy_database_path, "billing_observations"
        )
        assert "project_id" in _columns(legacy_database_path, "jobs")
        assert "ix_jobs_project_id" in _indexes(legacy_database_path, "jobs")

    def test_v5_to_v6_backfills_projects_and_preserves_historical_evidence(
        self, legacy_database_path: Path
    ) -> None:
        _prepare_v5_database(legacy_database_path)
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            jobs = (
                ("original-title", "original", "completed", None, "prompt ignored", "Source title"),
                ("original-prompt", "original", "failed", None, "  Prompt title  ", None),
                ("original-label", "original", "queued", None, "   ", None),
                ("original-long", "original", "queued", None, "x" * 200, None),
                ("cover-label", "cover", "failed", "https://youtu.be/a", None, None),
            )
            for offset, (job_id, job_type, status, source_url, prompt, source_title) in enumerate(
                jobs
            ):
                timestamp = f"2026-07-0{offset + 1}T00:00:00Z"
                connection.execute(
                    "INSERT INTO jobs (id, job_type, status, source_url, "
                    "sanitized_source_title, prompt, output_format, variation_count, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'mp3', 1, ?, ?)",
                    (
                        job_id,
                        job_type,
                        status,
                        source_url,
                        source_title,
                        prompt,
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                "INSERT INTO variation_attempts (job_id, variation_index, status, runpod_job_id, "
                "execution_ms, hourly_rate_usd, evidence_status, created_at, updated_at) "
                "VALUES ('original-title', 1, 'completed', 'runpod-1', 1234, '0.75', "
                "'recorded', '2026-07-01T00:00:00Z', '2026-07-01T01:00:00Z')"
            )
            connection.execute(
                "INSERT INTO outputs (job_id, variation_index, result_index, relative_path, "
                "mime_type, byte_size, sha256, created_at) VALUES "
                "('original-title', 1, 0, 'result.mp3', 'audio/mpeg', 42, ?, "
                "'2026-07-01T01:00:00Z')",
                ("a" * 64,),
            )
            job_columns_before = [
                row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            ]
            jobs_before = connection.execute(
                f"SELECT {', '.join(job_columns_before)} FROM jobs ORDER BY id"
            ).fetchall()
            attempts_before = connection.execute(
                "SELECT * FROM variation_attempts ORDER BY id"
            ).fetchall()
            outputs_before = connection.execute("SELECT * FROM outputs ORDER BY id").fetchall()
            connection.commit()
        finally:
            connection.close()

        result = migration_upgrade(str(legacy_database_path))
        assert result == {
            "path_hash": database_identity_hash(str(legacy_database_path)),
            "state": "exact_expected",
            "version": CURRENT_SCHEMA_VERSION,
            "changed": True,
        }
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            assert (
                connection.execute(
                    f"SELECT {', '.join(job_columns_before)} FROM jobs ORDER BY id"
                ).fetchall()
                == jobs_before
            )
            attempt_width = len(attempts_before[0])
            assert [
                row[:attempt_width]
                for row in connection.execute(
                    "SELECT * FROM variation_attempts ORDER BY id"
                ).fetchall()
            ] == attempts_before
            output_width = len(outputs_before[0])
            assert [
                row[:output_width]
                for row in connection.execute("SELECT * FROM outputs ORDER BY id").fetchall()
            ] == outputs_before
            assert connection.execute("SELECT id, project_id FROM jobs ORDER BY id").fetchall() == [
                ("cover-label", "cover-label"),
                ("original-label", "original-label"),
                ("original-long", "original-long"),
                ("original-prompt", "original-prompt"),
                ("original-title", "original-title"),
            ]
            provider_attempt = connection.execute(
                "SELECT inference_provider, provider_job_id FROM variation_attempts"
            ).fetchone()
            assert provider_attempt == ("runpod", "runpod-1")
            assert connection.execute(
                "SELECT id, job_type, title FROM projects ORDER BY id"
            ).fetchall() == [
                ("cover-label", "cover", "Cover"),
                ("original-label", "original", "Original song"),
                ("original-long", "original", "x" * 160),
                ("original-prompt", "original", "Prompt title"),
                ("original-title", "original", "Source title"),
            ]
        finally:
            connection.close()
        assert _integrity_ok(legacy_database_path)

    def test_v5_upgrade_failure_rolls_back_projects_and_marks_failed(
        self, legacy_database_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ace_service.migrations as migrations_module

        _prepare_v5_database(legacy_database_path)
        cp6_ddl = migrations_module._cp6_ddl

        def injected_failure(connection: sqlite3.Connection) -> None:
            cp6_ddl(connection)
            raise sqlite3.OperationalError("injected v6 migration failure")

        monkeypatch.setattr(migrations_module, "_cp6_ddl", injected_failure)
        with pytest.raises(MigrationError, match="rolled back"):
            migration_upgrade(str(legacy_database_path))
        assert "projects" not in _tables(legacy_database_path)
        assert "project_id" not in _columns(legacy_database_path, "jobs")
        assert _schema_version_row(legacy_database_path) == (
            CURRENT_SCHEMA_VERSION,
            "migration_failed",
        )
        with pytest.raises(MigrationError, match="durably incomplete"):
            migration_upgrade(str(legacy_database_path))

    @pytest.mark.parametrize(
        ("resource_type", "grouping_key", "is_network_volume", "source_contract"),
        (
            (
                "endpoint",
                "endpoint-abc",
                False,
                "runpod-endpoints-v1-usd-no-currency",
            ),
            (
                "network_volume",
                "account",
                True,
                "runpod-network-volume-v1-no-volume-id",
            ),
        ),
    )
    def test_upgrade_from_v4_preserves_multi_value_billing_retry_identity(
        self,
        legacy_database_path: Path,
        resource_type: str,
        grouping_key: str,
        is_network_volume: bool,
        source_contract: str,
    ) -> None:
        _prepare_v4_database(legacy_database_path)
        bucket_start = datetime(2026, 8, 8, tzinfo=UTC)
        stored_bucket_start = "2026-08-08 00:00:00.000000"
        stored_t1 = "2026-08-08 01:00:00.000000"
        stored_t2 = "2026-08-08 02:00:00.000000"
        documented_fields = {"diskSpaceBilledGb": "1.25"}
        evidence_checksums = {
            amount: observation_checksum(
                provider="runpod",
                resource_type=resource_type,
                grouping_key=grouping_key,
                bucket_start=bucket_start,
                bucket_size_hours=1,
                raw_amount=amount,
                raw_time_billed="1500",
                currency="USD",
                is_network_volume=is_network_volume,
                source_contract=source_contract,
                documented_fields=documented_fields,
            )
            for amount in ("0.50", "0.75")
        }
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            for amount, fetched_at in (("0.50", stored_t1), ("0.75", stored_t2)):
                connection.execute(
                    "INSERT INTO billing_observations "
                    "(provider, resource_type, grouping_key, bucket_start, bucket_size_hours, "
                    "raw_amount, raw_time_billed, currency, fetched_at, response_size_bytes, "
                    "is_network_volume, source_contract, documented_fields_json, checksum) "
                    "VALUES ('runpod', ?, ?, ?, 1, ?, '1500', 'USD', ?, 128, ?, ?, "
                    "json(?), ?)",
                    (
                        resource_type,
                        grouping_key,
                        stored_bucket_start,
                        amount,
                        fetched_at,
                        int(is_network_volume),
                        source_contract,
                        '{"diskSpaceBilledGb":"1.25"}',
                        evidence_checksums[amount],
                    ),
                )
            connection.execute(
                "INSERT INTO billing_projections "
                "(provider, resource_type, grouping_key, bucket_start, bucket_size_hours, "
                "latest_amount, latest_time_billed, currency, last_updated_at, "
                "latest_documented_fields_json) VALUES "
                "('runpod', ?, ?, ?, 1, '0.75', '1500', 'USD', ?, "
                'json(\'{"diskSpaceBilledGb":"1.25"}\'))',
                (resource_type, grouping_key, stored_bucket_start, stored_t2),
            )
            before = connection.execute("SELECT * FROM billing_observations ORDER BY id").fetchall()
            connection.commit()
        finally:
            connection.close()

        result = migration_upgrade(str(legacy_database_path))
        assert result["changed"] is True
        assert result["version"] == CURRENT_SCHEMA_VERSION
        assert _schema_version_row(legacy_database_path) == (CURRENT_SCHEMA_VERSION, "ready")
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            after_old_columns = connection.execute(
                "SELECT id, provider, resource_type, grouping_key, bucket_start, "
                "bucket_size_hours, raw_amount, raw_time_billed, currency, fetched_at, "
                "response_size_bytes, is_network_volume, source_contract, "
                "documented_fields_json, checksum FROM billing_observations ORDER BY id"
            ).fetchall()
            assert after_old_columns == before
            assert connection.execute(
                "SELECT evidence_checksum FROM billing_observations ORDER BY id"
            ).fetchall() == [(None,), (None,)]
            assert connection.execute(
                "SELECT latest_evidence_checksum FROM billing_projections"
            ).fetchone() == (None,)
        finally:
            connection.close()
        assert _integrity_ok(legacy_database_path)

        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(legacy_database_path)), future=True
        )
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                common = {
                    "provider": "runpod",
                    "resource_type": resource_type,
                    "grouping_key": grouping_key,
                    "bucket_start": bucket_start,
                    "bucket_size_hours": 1,
                    "raw_time_billed": "1500",
                    "currency": "USD",
                    "response_size_bytes": 128,
                    "is_network_volume": is_network_volume,
                    "source_contract": source_contract,
                    "documented_fields": documented_fields,
                }
                returned_ids = []
                for _ in range(2):
                    returned_ids.extend(
                        (
                            record_billing_observation(
                                session,
                                raw_amount="0.50",
                                fetched_at=datetime.fromisoformat("2026-08-07T21:00:00-04:00"),
                                **common,
                            ).id,
                            record_billing_observation(
                                session,
                                raw_amount="0.75",
                                fetched_at=datetime(2026, 8, 8, 2, tzinfo=UTC),
                                **common,
                            ).id,
                        )
                    )
                session.commit()
                assert returned_ids == [1, 2, 1, 2]
                assert (
                    session.execute(text("SELECT COUNT(*) FROM billing_observations")).scalar_one()
                    == 2
                )
                unchanged = session.execute(
                    text(
                        "SELECT id, provider, resource_type, grouping_key, bucket_start, "
                        "bucket_size_hours, raw_amount, raw_time_billed, currency, fetched_at, "
                        "response_size_bytes, is_network_volume, source_contract, "
                        "documented_fields_json, checksum FROM billing_observations ORDER BY id"
                    )
                ).all()
                assert [tuple(row) for row in unchanged] == before
                assert session.execute(
                    text("SELECT evidence_checksum FROM billing_observations ORDER BY id")
                ).all() == [(None,), (None,)]
                projection = session.execute(
                    text(
                        "SELECT latest_amount, last_updated_at, latest_evidence_checksum "
                        "FROM billing_projections"
                    )
                ).one()
                assert projection[0] == "0.75"
                assert projection[1] == stored_t2
                assert projection[2] == evidence_checksums["0.75"]
                endpoint_total = session.execute(
                    text(
                        "SELECT COALESCE(SUM(CAST(latest_amount AS REAL)), 0) "
                        "FROM billing_projections WHERE resource_type = 'endpoint'"
                    )
                ).scalar_one()
                network_total = session.execute(
                    text(
                        "SELECT COALESCE(SUM(CAST(latest_amount AS REAL)), 0) "
                        "FROM billing_projections WHERE resource_type = 'network_volume' "
                        "AND grouping_key = 'account'"
                    )
                ).scalar_one()
                assert endpoint_total == (0.75 if resource_type == "endpoint" else 0)
                assert network_total == (0.75 if is_network_volume else 0)

                reversal = record_billing_observation(
                    session,
                    raw_amount="0.50",
                    fetched_at=datetime(2026, 8, 8, 3, tzinfo=UTC),
                    **common,
                )
                assert reversal.id == 3
                assert (
                    record_billing_observation(
                        session,
                        raw_amount="0.50",
                        fetched_at=datetime.fromisoformat("2026-08-07T21:00:00-04:00"),
                        **common,
                    ).id
                    == 1
                )
                assert (
                    record_billing_observation(
                        session,
                        raw_amount="0.75",
                        fetched_at=datetime(2026, 8, 8, 2, tzinfo=UTC),
                        **common,
                    ).id
                    == 2
                )
                assert (
                    record_billing_observation(
                        session,
                        raw_amount="0.50",
                        fetched_at=datetime(2026, 8, 8, 3, tzinfo=UTC),
                        **common,
                    ).id
                    == 3
                )
                session.commit()
                assert (
                    session.execute(text("SELECT COUNT(*) FROM billing_observations")).scalar_one()
                    == 3
                )
                projected_reversal = session.execute(
                    text("SELECT latest_amount, last_updated_at FROM billing_projections")
                ).one()
                assert projected_reversal == ("0.50", "2026-08-08 03:00:00.000000")
        finally:
            engine.dispose()

    def test_v4_upgrade_failure_rolls_back_additions_and_refuses_retry(
        self, legacy_database_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ace_service.migrations as migrations_module

        _prepare_v4_database(legacy_database_path)

        def injected_failure(connection: sqlite3.Connection) -> None:
            connection.execute(
                "ALTER TABLE billing_observations ADD COLUMN evidence_checksum VARCHAR(64)"
            )
            raise sqlite3.OperationalError("injected v5 migration failure")

        monkeypatch.setattr(migrations_module, "_cp5_ddl", injected_failure)
        with pytest.raises(MigrationError, match="rolled back"):
            migration_upgrade(str(legacy_database_path))
        assert "evidence_checksum" not in _columns(legacy_database_path, "billing_observations")
        assert _schema_version_row(legacy_database_path) == (
            CURRENT_SCHEMA_VERSION,
            "migration_failed",
        )
        with pytest.raises(MigrationError, match="durably incomplete"):
            migration_upgrade(str(legacy_database_path))

    def test_second_upgrade_is_idempotent_noop(self, migrated_database_path: Path) -> None:
        result = migration_upgrade(str(migrated_database_path))
        assert result["changed"] is False
        assert result["version"] == CURRENT_SCHEMA_VERSION
        assert _schema_version_row(migrated_database_path) == (
            CURRENT_SCHEMA_VERSION,
            "ready",
        )

    def test_upgrade_refuses_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="does not exist"):
            migration_upgrade(str(tmp_path / "missing.db"))

    def test_upgrade_refuses_non_database_path(self, tmp_path: Path) -> None:
        path = tmp_path / "plain.txt"
        path.write_text("not sqlite")
        with pytest.raises(MigrationError, match="not an SQLite database"):
            migration_upgrade(str(path))

    def test_concurrent_upgrade_refused_by_sidecar_lock(self, legacy_database_path: Path) -> None:
        import fcntl

        lock_path = Path(f"{legacy_database_path}.migration.lock")
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(MigrationError, match="exclusive sidecar lock"):
                migration_upgrade(str(legacy_database_path))
            assert migration_status(str(legacy_database_path))["state"] == "unversioned_legacy"
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def test_injected_failure_rolls_back_and_marks_failed(
        self, legacy_database_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ace_service.migrations as migrations_module

        def injected_failure(connection: sqlite3.Connection) -> None:
            del connection
            raise sqlite3.OperationalError("injected migration failure")

        monkeypatch.setattr(migrations_module, "_cp4_ddl", injected_failure)
        with pytest.raises(MigrationError, match="rolled back"):
            migration_upgrade(str(legacy_database_path))
        assert _schema_version_row(legacy_database_path) is not None
        assert _schema_version_row(legacy_database_path)[1] == "migration_failed"
        # The step-2 DDL rolled back: no CP4 tables or columns exist.
        assert "submission_quotes" not in _tables(legacy_database_path)
        assert "runtime_calibrations" not in _tables(legacy_database_path)
        assert "actual_gpu" not in _attempt_columns(legacy_database_path)
        assert "hourly_rate_usd" not in _attempt_columns(legacy_database_path)
        assert _integrity_ok(legacy_database_path)
        # The durable failure marker refuses automatic retries.
        with pytest.raises(MigrationError, match="durably incomplete"):
            migration_upgrade(str(legacy_database_path))

    def test_crash_marker_refuses_upgrade(self, legacy_database_path: Path) -> None:
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            connection.execute(
                "CREATE TABLE schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "version INTEGER NOT NULL, "
                "status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, "
                "completed_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_version "
                "(singleton, version, status, started_at, completed_at) "
                "VALUES (1, 4, 'migration_started', '2026-08-08T00:00:00Z', NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(MigrationError, match="durably incomplete"):
            migration_upgrade(str(legacy_database_path))

    def test_upgrade_refuses_unknown_newer(self, legacy_database_path: Path) -> None:
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            connection.execute(
                "CREATE TABLE schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "version INTEGER NOT NULL, "
                "status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, "
                "completed_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_version "
                "(singleton, version, status, started_at, completed_at) "
                "VALUES (1, 99, 'ready', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(MigrationError, match="no migration path"):
            migration_upgrade(str(legacy_database_path))


class TestBackupAndLegacyReads:
    def test_backup_api_copy_migrates_without_touching_source(
        self, legacy_database_path: Path, tmp_path: Path
    ) -> None:
        backup = create_legacy_database_copy(
            legacy_database_path, tmp_path / "production-shaped-copy.db"
        )
        assert _integrity_ok(legacy_database_path)
        assert _integrity_ok(backup)
        migration_upgrade(str(backup))
        assert migration_status(str(backup))["state"] == "exact_expected"
        # The source stays unversioned and untouched.
        assert migration_status(str(legacy_database_path))["state"] == "unversioned_legacy"
        assert _integrity_ok(legacy_database_path)
        assert _integrity_ok(backup)

    def test_legacy_reads_on_migrated_production_shaped_copy(
        self, legacy_database_path: Path, tmp_path: Path
    ) -> None:
        job_id = "legacy-job-0001"
        connection = sqlite3.connect(str(legacy_database_path))
        try:
            connection.execute(
                "INSERT INTO jobs (id, job_type, status, prompt, variation_count, "
                "output_format, created_at, updated_at) "
                "VALUES (?, 'original', 'completed', 'legacy prompt', 1, 'mp3', ?, ?)",
                (
                    job_id,
                    _iso(datetime(2026, 7, 1, tzinfo=UTC)),
                    _iso(datetime(2026, 7, 1, tzinfo=UTC)),
                ),
            )
            connection.execute(
                "INSERT INTO variation_attempts (job_id, variation_index, status, "
                "created_at, updated_at, completed_at) "
                "VALUES (?, 1, 'completed', ?, ?, ?)",
                (
                    job_id,
                    _iso(datetime(2026, 7, 1, 1, tzinfo=UTC)),
                    _iso(datetime(2026, 7, 1, 1, tzinfo=UTC)),
                    _iso(datetime(2026, 7, 1, 1, 30, tzinfo=UTC)),
                ),
            )
            connection.execute(
                "INSERT INTO outputs (job_id, variation_index, result_index, "
                "relative_path, mime_type, byte_size, sha256, created_at) "
                "VALUES (?, 1, 0, ?, 'audio/mpeg', 42, ?, ?)",
                (
                    job_id,
                    f"{job_id}/variation-01.mp3",
                    "a" * 64,
                    _iso(datetime(2026, 7, 1, 1, 30, tzinfo=UTC)),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        backup = create_legacy_database_copy(legacy_database_path, tmp_path / "migrated-copy.db")
        migration_upgrade(str(backup))

        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL

        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(backup)),
            connect_args={"check_same_thread": False, "timeout": 5},
            future=True,
        )
        initialize_database(engine)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                job = get_job(session, job_id)
                assert job is not None
                assert job.prompt == "legacy prompt"
                assert job.status.value == "completed"
                attempts = list(job.variation_attempts)
                assert len(attempts) == 1
                assert attempts[0].evidence_status == "pending"
                assert attempts[0].estimated_compute_micro_usd is None
                assert attempts[0].execution_ms is None
                outputs = list(job.outputs)
                assert len(outputs) == 1
                assert outputs[0].sha256 == "a" * 64
            with factory() as session:
                # Foundation creator on the migrated copy is a no-op.
                session.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    def test_initialize_database_on_fresh_path_then_upgrade_is_exact(
        self, settings, tmp_path: Path
    ) -> None:
        # Fresh deployment flow: foundation creator creates tables, readiness
        # refuses, the explicit upgrade marks the schema ready.
        engine = create_database_engine(settings)
        initialize_database(engine)
        engine.dispose()
        database_path = settings.paths.database
        assert migration_status(str(database_path))["state"] == "unversioned_legacy"
        migration_upgrade(str(database_path))
        assert migration_status(str(database_path))["state"] == "exact_expected"
        assert _schema_version_row(database_path) == (CURRENT_SCHEMA_VERSION, "ready")
        # Startup readiness passes once upgraded.
        from ace_service.db import ensure_schema_readiness

        engine = create_database_engine(settings)
        try:
            ensure_schema_readiness(engine)
        finally:
            engine.dispose()

    def test_startup_readiness_refuses_unversioned_and_incomplete(self, settings) -> None:
        from ace_service.db import ensure_schema_readiness

        engine = create_database_engine(settings)
        initialize_database(engine)
        try:
            with pytest.raises(RuntimeError, match="refusing to start"):
                ensure_schema_readiness(engine)
        finally:
            engine.dispose()

        connection = sqlite3.connect(str(settings.paths.database))
        try:
            connection.execute(
                "CREATE TABLE schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "version INTEGER NOT NULL, status TEXT NOT NULL, "
                "started_at TEXT NOT NULL, completed_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_version VALUES "
                "(1, 3, 'ready', '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()
        for version, status in (
            (3, "ready"),
            (99, "ready"),
            (CURRENT_SCHEMA_VERSION, "migration_started"),
            (CURRENT_SCHEMA_VERSION, "migration_failed"),
        ):
            connection = sqlite3.connect(str(settings.paths.database))
            try:
                connection.execute(
                    "UPDATE schema_version SET version = ?, status = ? WHERE singleton = 1",
                    (version, status),
                )
                connection.commit()
            finally:
                connection.close()
            engine = create_database_engine(settings)
            try:
                with pytest.raises(RuntimeError, match="refusing to start"):
                    ensure_schema_readiness(engine)
            finally:
                engine.dispose()

    def test_startup_never_touches_an_existing_legacy_database(
        self, settings, legacy_database_path: Path
    ) -> None:
        """Production startup on a legacy DB refuses before create_all runs,
        so the foundation creator cannot add CP4 tables to an old database."""

        from ace_service.app import create_app

        with pytest.raises(RuntimeError, match="refusing to start"):
            create_app(settings)
        # The legacy database is untouched: no schema_version table and no
        # CP4 tables were silently created.
        assert "schema_version" not in _tables(legacy_database_path)
        assert "submission_quotes" not in _tables(legacy_database_path)
        assert "billing_observations" not in _tables(legacy_database_path)
        assert _integrity_ok(legacy_database_path)
        assert migration_status(str(legacy_database_path))["state"] == "unversioned_legacy"

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.migrations import migration_upgrade


@pytest.fixture
def settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        data_root=tmp_path / "service-data",
        service_password="test-password",
        home_ingest_token="test-home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
    )


@pytest.fixture
def session(settings: ServiceSettings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory() as database_session:
        yield database_session
    engine.dispose()


def _legacy_ddl() -> str:
    """Production-shaped pre-CP4 schema: foundation tables only, no
    schema_version table, no CP4 cost tables/columns."""

    return """
    CREATE TABLE jobs (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_type VARCHAR(8) NOT NULL,
        status VARCHAR(16) NOT NULL,
        source_url VARCHAR(2048),
        sanitized_source_title VARCHAR(512),
        source_duration FLOAT,
        source_sha256 VARCHAR(64),
        source_byte_size INTEGER,
        prompt TEXT,
        lyrics TEXT,
        rights_confirmation_at DATETIME,
        cover_strength FLOAT,
        output_format VARCHAR(8) NOT NULL,
        variation_count INTEGER NOT NULL,
        current_variation INTEGER,
        normalized_request_json TEXT,
        current_runpod_job_id VARCHAR(128),
        current_submission_nonce VARCHAR(128),
        runpod_result_json TEXT,
        error_code VARCHAR(128),
        user_facing_error TEXT,
        created_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        updated_at DATETIME NOT NULL
    );
    CREATE TABLE outputs (
        id INTEGER NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        variation_index INTEGER NOT NULL,
        result_index INTEGER NOT NULL,
        runpod_job_id VARCHAR(128),
        relative_path VARCHAR(1024) NOT NULL,
        mime_type VARCHAR(128) NOT NULL,
        byte_size INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        seed_metadata_json TEXT,
        generation_metadata_json TEXT,
        created_at DATETIME NOT NULL,
        UNIQUE (job_id, variation_index, result_index)
    );
    CREATE TABLE variation_attempts (
        id INTEGER NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        variation_index INTEGER NOT NULL,
        status VARCHAR(16) NOT NULL,
        runpod_job_id VARCHAR(128),
        submission_nonce VARCHAR(128),
        runpod_result_json TEXT,
        error_code VARCHAR(128),
        user_facing_error TEXT,
        created_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        updated_at DATETIME NOT NULL,
        UNIQUE (job_id, variation_index)
    );
    CREATE TABLE transfer_capabilities (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        direction VARCHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL,
        token_sha256 VARCHAR(64) NOT NULL UNIQUE,
        expected_relative_path VARCHAR(1024) NOT NULL,
        expected_extension VARCHAR(16) NOT NULL,
        max_bytes INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        consumed_at DATETIME,
        revoked_at DATETIME
    );
    """


def create_legacy_database(path: Path) -> Path:
    """Create a production-shaped pre-CP4 SQLite database at ``path``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(_legacy_ddl())
        connection.commit()
    finally:
        connection.close()
    return path


def create_legacy_database_copy(source: Path, target: Path) -> Path:
    """Create a production-shaped copy using the SQLite backup API."""

    source_connection = sqlite3.connect(str(source))
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return target


def utc_dt(offset_hours: float) -> datetime:
    return datetime(2026, 8, 8, 0, 0, tzinfo=UTC) + timedelta(hours=offset_hours)


@pytest.fixture
def legacy_database_path(settings: ServiceSettings) -> Path:
    """One production-shaped pre-CP4 database, never migrated."""

    return create_legacy_database(settings.paths.database)


@pytest.fixture
def migrated_database_path(legacy_database_path: Path) -> Path:
    """A legacy database upgraded exactly once through the migration runner."""

    migration_upgrade(str(legacy_database_path))
    return legacy_database_path


@pytest.fixture
def migrated_session(migrated_database_path: Path):
    engine = create_database_engine(
        ServiceSettings(
            data_root=migrated_database_path.parent,
            service_password="test-password",
            home_ingest_token="test-home-token",
            runpod_api_key="test-runpod-key",
            runpod_endpoint_id="test-endpoint",
        )
    )
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory() as database_session:
        yield database_session
    engine.dispose()

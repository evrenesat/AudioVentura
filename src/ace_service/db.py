"""SQLite engine, connection pragmas, and session factory setup."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from ace_service.config import ServiceSettings
from ace_service.models import Base


def create_database_engine(settings: ServiceSettings) -> Engine:
    """Create a synchronous SQLite engine for the configured data root."""

    settings.ensure_data_layout()
    database_path = Path(settings.paths.database)
    url = URL.create("sqlite+pysqlite", database=str(database_path))
    engine = sqlalchemy_create_engine(
        url,
        connect_args={
            "check_same_thread": False,
            "timeout": settings.sqlite_busy_timeout_ms / 1000,
        },
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection: Any, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
        finally:
            cursor.close()

    return engine


def initialize_database(engine: Engine) -> None:
    """Create all foundation tables without contacting external services.

    This is a foundation creator only: it never migrates an existing schema,
    and normal application startup refuses to serve unless the explicit
    migration runner reports the exact expected schema version.
    """

    Base.metadata.create_all(engine)


def ensure_schema_readiness(engine: Engine) -> None:
    """Refuse startup unless the database is at the exact expected schema version.

    The check is read-only; startup never applies migrations.  Every state
    except ``exact_expected`` (older, newer, incomplete, unversioned legacy,
    missing, corrupt) fails closed with operator guidance.
    """

    from ace_service.migrations import CURRENT_SCHEMA_VERSION, migration_status

    database = engine.url.database
    if not database:
        raise RuntimeError("database URL has no file path; cannot verify schema readiness")
    report = migration_status(str(database))
    state = report["state"]
    if state != "exact_expected":
        raise RuntimeError(
            "refusing to start: database schema is not the exact expected version "
            f"(state={state}, expected={CURRENT_SCHEMA_VERSION}, path hash "
            f"{report['path_hash']}). Startup never migrates automatically; run "
            "'python -m ace_service migrate-status --database <path>' for details and "
            "'python -m ace_service migrate-upgrade --database <path>' only after a "
            "verified pre-upgrade backup."
        )


def initialize_database_for_settings(settings: ServiceSettings) -> Engine:
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a synchronous, non-expiring SQLAlchemy session factory."""

    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


SessionFactory = Callable[[], Session]

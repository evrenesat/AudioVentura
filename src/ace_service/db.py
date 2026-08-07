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
    """Create all foundation tables without contacting external services."""

    Base.metadata.create_all(engine)


def initialize_database_for_settings(settings: ServiceSettings) -> Engine:
    engine = create_database_engine(settings)
    initialize_database(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a synchronous, non-expiring SQLAlchemy session factory."""

    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


SessionFactory = Callable[[], Session]

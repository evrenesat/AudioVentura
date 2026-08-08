"""Read-only operator gate for starting a schema-v1 controller release."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ace_service.repository import check_schema_v1_rollback_readiness


def main(argv: Sequence[str] | None = None) -> int:
    """Print bounded rollback diagnostics and return a shell-friendly exit code."""

    parser = argparse.ArgumentParser(
        description="Check whether schema-v1 controller rollback is locally safe."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=_default_database_path(),
        help="Configured controller SQLite database (default: ACE_SERVICE_DATA_ROOT/service.db)",
    )
    args = parser.parse_args(argv)
    database = args.database
    if not database.is_file():
        print("rollback=indeterminate reason=database_unavailable", file=sys.stderr)
        return 2

    engine = None
    try:
        engine = _create_read_only_engine(database)
        with Session(engine) as session:
            readiness = check_schema_v1_rollback_readiness(session)
    except (OSError, SQLAlchemyError, sqlite3.Error):
        print("rollback=indeterminate reason=database_unavailable", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()

    for diagnostic in readiness.diagnostics:
        print(
            f"job_id={diagnostic.job_id} status={diagnostic.status} "
            f"schema={diagnostic.schema} classification={diagnostic.classification}"
        )
    outcome = "safe" if readiness.safe else "not-safe"
    print(f"rollback={outcome} blockers={len(readiness.blockers)}")
    return 0 if readiness.safe else 1


def _default_database_path() -> Path:
    data_root = os.environ.get("ACE_SERVICE_DATA_ROOT", "/srv/ace-service/data")
    return Path(data_root).expanduser() / "service.db"


def _create_read_only_engine(database: Path) -> Engine:
    resolved = database.expanduser().resolve()
    sqlite_uri = f"file:{resolved.as_posix()}?mode=ro"
    return create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(sqlite_uri, uri=True),
        poolclass=NullPool,
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the private controller UI with Uvicorn, or explicit operator commands.

Without arguments the controller starts with Uvicorn.  The migration commands
are the only way to change the database schema: ``migrate-status`` is a
read-only probe and ``migrate-upgrade`` applies the ordered additive migration
under an exclusive sidecar lock.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections.abc import Sequence
from typing import Any, cast

import uvicorn

from ace_service.app import create_app
from ace_service.config import ServiceSettings


def _capacity_result_exit_code(result: object) -> int:
    """Map one watchdog result to a fail-closed systemd exit status."""

    state = str(getattr(result, "state", ""))
    error = getattr(result, "error", None)
    error_value = None if error is None else str(error)
    if state == "release_overdue" or error_value in {
        "drift",
        "invalid_response",
        "not_found",
    }:
        return 2
    if state == "releasing" or error_value in {"transient", "unsafe_active_work"}:
        return 1
    if error_value is not None:
        return 2
    if state in {"reconciled", "healthy", "cold", "warming", "retained", "idle"}:
        return 0
    return 2


def _print_status(report: dict[str, object]) -> None:
    from ace_service.migrations import describe_state

    state = str(report["state"])
    version = report.get("version")
    print(f"database path hash: {report['path_hash']}")
    print(f"state: {state} — {describe_state(state)}")
    if version is not None:
        print(f"recorded schema version: {version}")


def _migrate_status(database: str) -> int:
    from ace_service.migrations import migration_status

    report = migration_status(database)
    _print_status(report)
    return 1 if report["state"] in {"missing", "not_a_database", "corrupt"} else 0


def _migrate_upgrade(database: str) -> int:
    from ace_service.migrations import migration_upgrade

    result = migration_upgrade(database)
    if result["changed"]:
        print(
            f"upgrade complete: schema now at version {result['version']} "
            f"(path hash {result['path_hash']})"
        )
    else:
        print(f"no change: database already at version {result['version']}")
    return 0


def _capacity_reconcile_once() -> int:
    from ace_service.capacity.controller import CapacityController
    from ace_service.capacity.registry import build_capacity_registry
    from ace_service.db import (
        create_database_engine,
        create_session_factory,
        ensure_schema_readiness,
    )
    from ace_service.migrations import migration_status

    try:
        settings = ServiceSettings()
    except Exception as exc:
        print(f"capacity reconciliation refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    engine = create_database_engine(settings)
    try:
        ensure_schema_readiness(engine)
        registry = build_capacity_registry(settings)
        controller = CapacityController(
            settings,
            create_session_factory(engine),
            registry,
            owner="capacity-watchdog",
        )
        results = asyncio.run(controller.reconcile_once())
        for result in results:
            key_hash = hashlib.sha256(result.capacity_key.encode("utf-8")).hexdigest()[:12]
            print(
                f"capacity={key_hash} state={result.state} floor={result.configured_floor} "
                f"instances={result.observed_instances} active_jobs={result.provider_active_jobs}"
            )
        codes = [_capacity_result_exit_code(result) for result in results]
        return 2 if 2 in codes else 1 if 1 in codes else 0
    except Exception as exc:
        report = migration_status(str(settings.paths.database))
        if report.get("state") != "exact_expected":
            print("capacity reconciliation refused: database schema is not ready", file=sys.stderr)
        else:
            print(f"capacity reconciliation refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


def _capacity_preflight_once(env_file: str | None = None) -> int:
    """Inspect configured capacity managers without changing provider state."""

    from ace_service.capacity.base import CapacityError, CapacityErrorKind, CapacitySnapshot
    from ace_service.capacity.registry import build_capacity_registry
    from ace_service.db import (
        create_database_engine,
        ensure_schema_readiness,
    )

    try:
        settings = cast(Any, ServiceSettings)(_env_file=env_file)
        engine = create_database_engine(settings)
        ensure_schema_readiness(engine)
        registry = build_capacity_registry(settings)

        async def inspect_all() -> list[tuple[str, object]]:
            values: list[tuple[str, object]] = []
            for manager in registry.managers:
                try:
                    values.append((manager.key, await manager.inspect()))
                except CapacityError as exc:
                    values.append((manager.key, exc))
            return values

        results = asyncio.run(inspect_all())
        exit_code = 0
        for key, value in results:
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            if isinstance(value, CapacityError):
                print(f"capacity={key_hash} error={value.kind.value}")
                exit_code = max(
                    exit_code,
                    1
                    if value.kind
                    in {CapacityErrorKind.TRANSIENT, CapacityErrorKind.UNSAFE_ACTIVE_WORK}
                    else 2,
                )
                continue
            if not isinstance(value, CapacitySnapshot):
                print(f"capacity={key_hash} error=invalid_response")
                exit_code = 2
                continue
            print(
                f"capacity={key_hash} phase={value.phase.value} floor={value.configured_floor} "
                f"instances={value.observed_instances} active_jobs={value.provider_active_jobs}"
            )
        return exit_code
    except Exception as exc:
        print(f"capacity preflight refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if "engine" in locals():
            engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ace_service")
    subparsers = parser.add_subparsers(dest="command")
    status_parser = subparsers.add_parser("migrate-status", help="read-only schema status")
    status_parser.add_argument("--database", required=True, help="explicit resolved database path")
    upgrade_parser = subparsers.add_parser(
        "migrate-upgrade", help="apply the ordered additive migration (offline)"
    )
    upgrade_parser.add_argument("--database", required=True, help="explicit resolved database path")
    catalog_parser = subparsers.add_parser("fal-catalog", help="review Fal endpoint metadata")
    catalog_subparsers = catalog_parser.add_subparsers(dest="fal_catalog_command")
    audit_parser = catalog_subparsers.add_parser("audit", help="read-only catalog audit")
    audit_parser.add_argument("--catalog", required=False, help="absolute reviewed catalog path")
    capacity_parser = subparsers.add_parser(
        "capacity-reconcile", help="run one durable capacity reconciliation cycle"
    )
    capacity_parser.add_argument("--once", action="store_true", required=True)
    preflight_parser = subparsers.add_parser(
        "capacity-preflight", help="inspect managed capacity without mutation"
    )
    preflight_parser.add_argument("--once", action="store_true", required=True)
    preflight_parser.add_argument(
        "--env-file",
        required=False,
        help="protected dotenv-compatible settings file used for this read-only inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        settings = ServiceSettings()
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0
    if args.command == "migrate-status":
        return _migrate_status(args.database)
    if args.command == "migrate-upgrade":
        return _migrate_upgrade(args.database)
    if args.command == "capacity-reconcile":
        return _capacity_reconcile_once()
    if args.command == "capacity-preflight":
        return _capacity_preflight_once(args.env_file)
    if args.command == "fal-catalog" and args.fal_catalog_command == "audit":
        from ace_service.providers.fal_catalog import main as fal_catalog_main

        return fal_catalog_main([*(["--catalog", args.catalog] if args.catalog else [])])
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Run the private controller UI with Uvicorn, or explicit operator commands.

Without arguments the controller starts with Uvicorn.  The migration commands
are the only way to change the database schema: ``migrate-status`` is a
read-only probe and ``migrate-upgrade`` applies the ordered additive migration
under an exclusive sidecar lock.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from ace_service.app import create_app
from ace_service.config import ServiceSettings


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
    if args.command == "fal-catalog" and args.fal_catalog_command == "audit":
        from ace_service.providers.fal_catalog import main as fal_catalog_main

        return fal_catalog_main([*(["--catalog", args.catalog] if args.catalog else [])])
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

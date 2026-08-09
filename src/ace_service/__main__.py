"""Run the private controller UI with Uvicorn, or explicit operator commands.

Without arguments the controller starts with Uvicorn.  The migration commands
are the only way to change the database schema: ``migrate-status`` is a
read-only probe and ``migrate-upgrade`` applies the ordered additive migration
under an exclusive sidecar lock.  ``billing-sync`` is the operator-only
billing boundary (no browser route, no in-process scheduler).
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


def _billing_sync(database: str, start: str, end: str) -> int:
    from ace_service.billing_client import sync_billing

    settings = ServiceSettings()
    summary = sync_billing(
        database,
        endpoint_id=settings.runpod_endpoint_id,
        api_key=settings.runpod_api_key,
        start_utc=start,
        end_utc=end,
        request_timeout_seconds=settings.billing_request_timeout_seconds,
        response_max_bytes=settings.billing_response_max_bytes,
        lease_ttl_seconds=settings.billing_lease_ttl_seconds,
        price_max_age_hours=settings.price_max_age_hours,
    )
    print(
        f"billing sync complete: {summary['endpoint_observations']} endpoint observations, "
        f"{summary['network_volume_observations']} network-volume observations, "
        f"cutoff {summary['cutoff_at']}"
    )
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
    billing_parser = subparsers.add_parser(
        "billing-sync", help="operator-only provider billing sync boundary"
    )
    billing_parser.add_argument("--database", required=True, help="explicit resolved database path")
    billing_parser.add_argument("--start", required=True, help="exact UTC start (ISO-8601)")
    billing_parser.add_argument("--end", required=True, help="exact UTC end (ISO-8601)")
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
    if args.command == "billing-sync":
        return _billing_sync(args.database, args.start, args.end)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""CLI surface regression: billing-sync is gone, migration commands remain."""

from __future__ import annotations

import pytest

from ace_service.__main__ import _capacity_result_exit_code, build_parser, main


def test_cli_help_has_no_billing_sync() -> None:
    help_text = build_parser().format_help()
    assert "billing-sync" not in help_text
    assert "migrate-status" in help_text
    assert "migrate-upgrade" in help_text


def test_cli_billing_sync_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["billing-sync", "--database", "x", "--start", "a", "--end", "b"])
    assert exc.value.code == 2


def test_cli_migrate_status_still_dispatches(tmp_path) -> None:
    # A missing database reports the missing state (exit 1) instead of the
    # previous billing-sync branch or an unknown-command error.
    assert main(["migrate-status", "--database", str(tmp_path / "missing.db")]) == 1


def test_capacity_preflight_accepts_an_explicit_protected_env_file() -> None:
    args = build_parser().parse_args(
        ["capacity-preflight", "--once", "--env-file", "/etc/audioventura/controller.env"]
    )
    assert args.command == "capacity-preflight"
    assert args.once is True
    assert args.env_file == "/etc/audioventura/controller.env"


@pytest.mark.parametrize(
    ("state", "error", "expected"),
    [
        ("cold", None, 0),
        ("reconciled", None, 0),
        ("healthy", None, 0),
        ("warming", None, 0),
        ("retained", None, 0),
        ("idle", None, 0),
        ("releasing", None, 1),
        ("release_overdue", None, 2),
        ("idle", "transient", 1),
        ("idle", "unsafe_active_work", 1),
        ("idle", "drift", 2),
        ("idle", "invalid_response", 2),
        ("idle", "not_found", 2),
        ("idle", "future_error", 2),
        ("unknown", None, 2),
    ],
)
def test_capacity_watchdog_exit_mapping(state: str, error: str | None, expected: int) -> None:
    class Result:
        pass

    result = Result()
    result.state = state
    result.error = error
    assert _capacity_result_exit_code(result) == expected

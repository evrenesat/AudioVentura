"""CLI surface regression: billing-sync is gone, migration commands remain."""

from __future__ import annotations

import pytest

from ace_service.__main__ import build_parser, main


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

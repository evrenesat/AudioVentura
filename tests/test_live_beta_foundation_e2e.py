from __future__ import annotations

import argparse

import pytest

from tests.live_beta_foundation_e2e import _parser, run


def _args(*values: str) -> argparse.Namespace:
    return _parser().parse_args(values)


def test_beta_smoke_requires_explicit_paid_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("ACE_SERVICE_USERNAME", "user")
    monkeypatch.setenv("ACE_SERVICE_PASSWORD", "password")
    with pytest.raises(RuntimeError, match="allow-paid"):
        run(_args())


def test_beta_smoke_requires_exact_one_submission_budget() -> None:
    with pytest.raises(RuntimeError, match="exact one-submission"):
        run(_args("--allow-paid", "--max-paid-submissions", "2"))


def test_beta_smoke_rejects_malformed_resume_before_network(monkeypatch) -> None:
    monkeypatch.setenv("ACE_SERVICE_USERNAME", "user")
    monkeypatch.setenv("ACE_SERVICE_PASSWORD", "password")
    with pytest.raises(RuntimeError, match="resume job ID"):
        run(_args("--allow-paid", "--resume-job-id", "not-a-uuid"))

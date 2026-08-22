from __future__ import annotations

import argparse

import pytest

from tests import live_paid_ui_e2e


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "allow_paid": True,
        "max_paid_submissions": 2,
        "resume_initial_job_id": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_resume_requires_exact_one_submission_budget() -> None:
    with pytest.raises(RuntimeError, match="exact 1-submission paid budget"):
        live_paid_ui_e2e.run(_args(resume_initial_job_id="56270787-2460-4633-982d-45c9f759f558"))


def test_resume_rejects_malformed_initial_job_id() -> None:
    with pytest.raises(RuntimeError, match="resume initial job ID is malformed"):
        live_paid_ui_e2e.run(_args(max_paid_submissions=1, resume_initial_job_id="not-a-job"))

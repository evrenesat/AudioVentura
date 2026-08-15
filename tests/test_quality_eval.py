from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from ace_service.campaign import (
    CAMPAIGN_CEILING_MICRO_USD,
    BoundaryEvidence,
    CampaignGateError,
    CampaignStore,
    RateCatalog,
    RateEvidence,
    RemoteChangeAuthorization,
    build_campaign_plan,
    build_confirmation_cases,
    load_fixture_manifest,
    propose_immutable_profile,
    utc_now,
    validate_billing_window,
)
from ace_service.quality_eval import (
    DEFAULT_BLOCKED_ROUTES,
    DEFAULT_TERMINAL_WAIT_MS,
    ControllerQueueSubmitter,
    DurableControllerSubmitter,
    EvaluationController,
    main,
)

MANIFEST = Path("/srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json")


def test_module_entrypoint_is_quarantined(capsys) -> None:
    """Direct module execution must be a no-op while the campaign is
    quarantined (the ``if __name__ == "__main__"`` block is commented out)."""
    import runpy

    runpy.run_module("ace_service.quality_eval", run_name="__main__")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_dry_run_has_no_inference_call_and_reports_budget_gate(tmp_path: Path, capsys) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    output = tmp_path / "dry-run.json"
    assert main(["--manifest", str(MANIFEST), "--dry-run", "--output", str(output)]) == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["runpod_calls"] == 0
    assert value["production_defaults_changed"] is False
    assert value["plan"]["minimum_jobs"] >= 2
    assert value["budget"]["rate_status"] == "unavailable"
    assert "Waltz" not in output.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""


def test_confirmation_expansion_and_profile_proposal_do_not_mutate_defaults() -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    confirmation = build_confirmation_cases(
        manifest,
        plan,
        ("cover-grid-a0.35-n0.10",),
    )
    assert len(confirmation) == 6
    assert {case.seed for case in confirmation} == {1729, 2718, 3141}
    assert all(case.lm_model is None for case in confirmation)

    proposal = propose_immutable_profile(
        "campaign-profile-1",
        "fast-beta-v1",
        {"cover_noise_strength": 0.0},
        {"cover_noise_strength": 0.2},
    )
    assert proposal.materially_different is True
    assert proposal.profile_id.startswith("fast-beta-v1-campaign-")


def test_billing_interval_fails_closed_until_semantics_are_proven() -> None:
    start = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
    end = datetime(2026, 8, 8, 11, 0, tzinfo=UTC)
    evidence = BoundaryEvidence(
        start_inclusive=True,
        end_exclusive=True,
        native_bucket_seconds=3600,
        native_bucket_start_field="startTime",
        empty_response_behavior="unknown",
        current_partial_bucket_behavior="documented",
        late_update_behavior="documented",
        source="fixture",
    )
    with pytest.raises(CampaignGateError):
        validate_billing_window(start, end, evidence)


def test_controller_submitter_requires_terminal_reconciliation_and_serializes() -> None:
    calls: list[str] = []

    def factory(_campaign_id: str, _sample: object) -> str:
        calls.append("factory")
        return "job-opaque-1"

    def enqueue(job_id: str) -> None:
        calls.append(f"enqueue:{job_id}")

    with pytest.raises(CampaignGateError):
        ControllerQueueSubmitter(factory, enqueue).submit("campaign-1", {})

    submitter = ControllerQueueSubmitter(factory, enqueue, lambda *_args: calls.append("terminal"))
    assert (
        submitter.submit(
            "campaign-1", {}, on_submitted=lambda job_id: calls.append(f"submitted:{job_id}")
        )
        == "job-opaque-1"
    )
    assert calls == ["factory", "enqueue:job-opaque-1", "submitted:job-opaque-1", "terminal"]


def test_execution_prepares_campaign_before_remote_gate_and_submitter(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    now = utc_now()
    rates = RateCatalog(
        {
            "rtx-4090-24gb": RateEvidence(
                gpu_id="rtx-4090-24gb",
                hourly_rate_usd="0.49",
                source_url="https://www.runpod.io/pricing",
                source_version="fixture-rate-v1",
                captured_at=now,
            )
        }
    )
    boundary = BoundaryEvidence(
        start_inclusive=True,
        end_exclusive=True,
        native_bucket_seconds=3600,
        native_bucket_start_field="startTime",
        empty_response_behavior="documented",
        current_partial_bucket_behavior="documented",
        late_update_behavior="documented",
        source="fixture-and-live-probe",
    )
    authorization = RemoteChangeAuthorization(
        application_commit="a" * 40,
        worker_digest="sha256:" + "b" * 64,
        endpoint_id="endpoint-1",
        template_id="template-1",
        rollback_target="release-before-campaign",
        evaluation_models=("acestep-v15-xl-turbo",),
        ceiling_micro_usd=CAMPAIGN_CEILING_MICRO_USD,
        authorized_at=now,
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
        edge_guard_verified=True,
    )
    controller = EvaluationController(store, manifest, plan)
    with pytest.raises(CampaignGateError, match="durable controller submitter"):
        controller.execute_screening(
            "campaign-execute-1",
            confirmed=True,
            authorization=authorization,
            rates=rates,
            boundary=boundary,
        )
    campaign = store.get_campaign("campaign-execute-1")
    assert campaign is not None
    assert campaign["authorization_json"] is not None


def _product_session_factory(product_database: Path) -> Any:
    from sqlalchemy import create_engine

    from ace_service.db import create_session_factory
    from ace_service.models import Base

    engine = create_engine(
        f"sqlite:///{product_database}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


class _CompletingDriver:
    """Drives durable controller jobs through the real repository boundaries."""

    def __init__(
        self,
        session_factory: Any,
        *,
        fail: bool = False,
        hang: bool = False,
        fail_with_evidence: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.fail = fail
        self.hang = hang
        self.fail_with_evidence = fail_with_evidence
        self.processed: list[str] = []
        self._pending: set[str] = set()

    async def process_job(self, job_id: str) -> None:
        if self.hang or job_id in self._pending:
            return
        from ace_service.models import JobStatus, JobType
        from ace_service.repository import (
            complete_variation_attempt,
            confirm_cover_job,
            create_output,
            get_job,
            persist_variation_runpod_job_id,
            prepare_variation_submission,
            set_variation_runpod_result,
            transition_job,
        )

        self._pending.add(job_id)
        with self.session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                return
            if job.job_type is JobType.COVER and job.status is JobStatus.STAGING:
                confirm_cover_job(session, job.id)
                session.commit()
                return
            if self.fail:
                if self.fail_with_evidence:
                    _job, attempt, nonce = prepare_variation_submission(session, job.id, 1)
                    persist_variation_runpod_job_id(
                        session, attempt.id, "runpod-campaign-fake-1", submission_nonce=nonce
                    )
                    set_variation_runpod_result(
                        session,
                        attempt.id,
                        {
                            "schema_version": 2,
                            "job_id": job.id,
                            "submission_nonce": nonce,
                            "worker": {
                                "gpu": "NVIDIA GeForce RTX 4090",
                                "image_digest": "sha256:" + "b" * 64,
                                "ace_tag": "v0.1.8",
                                "ace_commit": "c" * 40,
                            },
                            "runpod_execution_ms": 22_499,
                        },
                    )
                transition_job(
                    session,
                    job.id,
                    JobStatus.FAILED,
                    error_code="injected_failure",
                    user_facing_error="injected controller failure",
                )
                session.commit()
                return
            _job, attempt, nonce = prepare_variation_submission(session, job.id, 1)
            persist_variation_runpod_job_id(
                session, attempt.id, "runpod-campaign-fake-1", submission_nonce=nonce
            )
            create_output(
                session,
                job_id=job.id,
                variation_index=1,
                result_index=0,
                relative_path=f"{job.id}/variation-01.mp3",
                mime_type="audio/mpeg",
                byte_size=1234,
                sha256="a" * 64,
                runpod_job_id="runpod-campaign-fake-1",
            )
            set_variation_runpod_result(
                session,
                attempt.id,
                {
                    "schema_version": 2,
                    "job_id": job.id,
                    "submission_nonce": nonce,
                    "output": {
                        "requested_seed": job.normalized_request_json or {},
                        "effective_seed": 1729,
                        "seed": 1729,
                        "sha256": "a" * 64,
                        "bytes": 1234,
                        "format": "mp3",
                        "relative_path": f"{job.id}/variation-01.mp3",
                    },
                    "worker": {
                        "gpu": "NVIDIA GeForce RTX 4090",
                        "image_digest": "sha256:" + "b" * 64,
                        "ace_tag": "v0.1.8",
                        "ace_commit": "c" * 40,
                    },
                    "runpod_execution_ms": 22_499,
                },
            )
            complete_variation_attempt(session, attempt.id)
            session.commit()
            self.processed.append(job_id)


def _authorization(now: datetime) -> RemoteChangeAuthorization:
    return RemoteChangeAuthorization(
        application_commit="a" * 40,
        worker_digest="sha256:" + "b" * 64,
        endpoint_id="endpoint-1",
        template_id="template-1",
        rollback_target="release-before-campaign",
        evaluation_models=("acestep-v15-xl-turbo",),
        ceiling_micro_usd=CAMPAIGN_CEILING_MICRO_USD,
        authorized_at=now,
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
        edge_guard_verified=True,
    )


def _rates(now: datetime) -> RateCatalog:
    return RateCatalog(
        {
            "rtx-4090-24gb": RateEvidence(
                gpu_id="rtx-4090-24gb",
                hourly_rate_usd="0.49",
                source_url="https://www.runpod.io/pricing",
                source_version="fixture-rate-v1",
                captured_at=now,
            )
        }
    )


def _boundary() -> BoundaryEvidence:
    return BoundaryEvidence(
        start_inclusive=True,
        end_exclusive=True,
        native_bucket_seconds=3600,
        native_bucket_start_field="startTime",
        empty_response_behavior="documented",
        current_partial_bucket_behavior="documented",
        late_update_behavior="documented",
        source="fixture-and-live-probe",
    )


def _health(details: dict[str, Any]):
    from ace_service.runpod_client import RunpodHealth

    return RunpodHealth(details=details)


# Representative documented Runpod /health bodies (camelCase job fields).
def _documented_health(*, idle: int = 0, running: int = 0, queued: int = 0, in_progress: int = 0):
    return {
        "workers": {"idle": idle, "running": running},
        "jobs": {
            "completed": 0,
            "failed": 0,
            "inProgress": in_progress,
            "inQueue": queued,
            "retried": 0,
        },
    }


async def _zero_health():
    return _health(_documented_health())


async def _nonzero_health():
    return _health(_documented_health(idle=1))


async def _malformed_health():
    return _health({"unexpected": "shape"})


def _durable_submitter(**kwargs: Any) -> DurableControllerSubmitter:
    return DurableControllerSubmitter(
        session_factory=kwargs["session_factory"],
        driver=kwargs["driver"],
        store=kwargs["store"],
        rates=kwargs["rates"],
        poll_interval_seconds=kwargs.get("poll_interval_seconds", 0.01),
        terminal_timeout_ms=kwargs.get("terminal_timeout_ms", DEFAULT_TERMINAL_WAIT_MS),
        cover_source_url=kwargs.get("cover_source_url"),
        health_provider=kwargs.get("health_provider", _zero_health),
        endpoint_id=kwargs.get("endpoint_id", "endpoint-1"),
    )


def test_execute_screening_submits_one_at_a_time_and_tears_down(
    tmp_path: Path,
) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    driver = _CompletingDriver(session_factory)
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=driver,
        store=store,
        rates=_rates(now),
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    result = controller.execute_screening(
        "campaign-execute-real-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    submitted = result["submitted_samples"]
    assert len(submitted) == plan.minimum_jobs == len(driver.processed)
    samples = store.list_samples("campaign-execute-real-1")
    assert set(submitted) == {str(item["sample_id"]) for item in samples}
    # Every durable controller job carries a generated UUID product job ID,
    # distinct from the opaque campaign sample ID, and the campaign store
    # reconciled exactly those job IDs.
    assert all(item["job_id"] is not None for item in samples)
    assert {str(item["job_id"]) for item in samples} == set(driver.processed)
    assert all(str(item["job_id"]) != str(item["sample_id"]) for item in samples)
    for item in samples:
        from uuid import UUID

        assert str(UUID(str(item["job_id"]))) == str(item["job_id"])
    # Every sample reached terminal evidence with the worker GPU mapped to the
    # rate catalog.
    assert all(item["status"] == "completed" for item in samples)
    assert all(item["actual_gpu"] == "rtx-4090-24gb" for item in samples)
    assert all(item["cost_status"] == "estimated_compute" for item in samples)
    assert all(item["output_path"] for item in samples)
    # One-at-a-time: no sample was in flight when the next reservation opened.
    assert store.current_gate()["active"] == 0
    campaign = store.get_campaign("campaign-execute-real-1")
    assert campaign is not None and campaign["status"] == "awaiting_scores"
    assert store.rollback_readiness().safe is True


def test_execute_screening_failure_fails_closed_and_keeps_gate(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    driver = _CompletingDriver(session_factory, fail=True)
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=driver,
        store=store,
        rates=_rates(now),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError):
        controller.execute_screening(
            "campaign-execute-fail-1",
            confirmed=True,
            authorization=_authorization(now),
            rates=_rates(now),
            boundary=_boundary(),
        )
    campaign = store.get_campaign("campaign-execute-fail-1")
    assert campaign is not None and campaign["status"] == "failed"
    # The failed sample stays terminal, the window remains open, and no
    # teardown clears the durable gate while uncertainty remains.
    assert store.current_gate()["active"] == 1
    assert store.rollback_readiness().safe is False
    samples = store.list_samples("campaign-execute-fail-1")
    assert samples and samples[0]["status"] == "failed"


def test_execute_confirmation_skips_reused_screening_samples(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    rates = _rates(now)
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=rates,
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    controller.execute_screening(
        "campaign-confirm-exec-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=rates,
        boundary=_boundary(),
    )
    finalist = next(
        case.declared_case_id
        for case in plan.cases
        if case.declared_case_id.startswith("cover-grid-")
    )
    controller.prepare_confirmation("campaign-confirm-exec-1", (finalist,), confirmed=True)
    samples = store.list_samples("campaign-confirm-exec-1", include_aliases=True)
    # Seed one is the screening seed: the confirmation roles are aliases of
    # the already-executed screening samples, not new rows.
    reused = [
        item
        for item in samples
        if any(alias.endswith("-confirmation-1729") for alias in item["aliases"])
    ]
    assert len(reused) == 2
    assert all(item["status"] == "completed" for item in reused)
    planned = [item for item in samples if item["status"] == "planned"]
    assert len(planned) == 4
    result = controller.execute_confirmation(
        "campaign-confirm-exec-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=rates,
        boundary=_boundary(),
    )
    # Only the two new payable seeds were submitted and charged; the reused
    # seed-one samples keep exactly their one durable screening submission.
    assert len(result["submitted_samples"]) == 4
    assert set(result["submitted_samples"]) == {item["sample_id"] for item in planned}
    for item in store.list_samples("campaign-confirm-exec-1", include_aliases=True):
        if any(alias.endswith("-confirmation-1729") for alias in item["aliases"]):
            assert item["job_id"] is not None
            reservation = store.reservation_for_sample("campaign-confirm-exec-1", item["sample_id"])
            assert reservation is not None
            assert reservation["state"] == "settled"
    assert store.current_gate()["active"] == 0


def test_execute_terminal_timeout_marks_uncertain_and_fails_closed(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    driver = _CompletingDriver(session_factory, hang=True)
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=driver,
        store=store,
        rates=_rates(now),
        terminal_timeout_ms=50,
        poll_interval_seconds=0.005,
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="terminal evidence"):
        controller.execute_screening(
            "campaign-execute-timeout-1",
            confirmed=True,
            authorization=_authorization(now),
            rates=_rates(now),
            boundary=_boundary(),
        )
    samples = store.list_samples("campaign-execute-timeout-1")
    assert samples and samples[0]["status"] == "uncertain"
    assert store.current_gate()["active"] == 1


def test_backup_cli_refuses_missing_database_without_creating_it(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    missing = tmp_path / "missing-campaign.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-any",
                "--backup",
                str(destination),
            ]
        )
        == 2
    )
    assert not missing.exists()
    assert not destination.exists()


def test_failed_job_with_worker_evidence_is_never_billed(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory, fail=True, fail_with_evidence=True),
        store=store,
        rates=_rates(now),
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    controller.execute_screening(
        "campaign-execute-evidence-fail-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    samples = store.list_samples("campaign-execute-evidence-fail-1")
    assert all(item["status"] == "failed" for item in samples)
    assert all(item["cost_status"] == "unavailable" for item in samples)
    assert all(item["estimated_compute_micro_usd"] is None for item in samples)
    assert all(item["actual_gpu"] is None for item in samples)
    # Failed attempts keep their full original reservation conservatively
    # retained (never presented as executed compute) and, once every attempt
    # is terminal and the documented provider-zero health response is
    # observed, the window may close.
    for item in samples:
        reservation = store.reservation_for_sample(
            "campaign-execute-evidence-fail-1", item["sample_id"]
        )
        assert reservation is not None
        assert reservation["state"] == "conservatively_retained"
        assert reservation["reserved_micro_usd"] > 0
        assert reservation["final_estimate_micro_usd"] is None
    assert store.current_gate()["active"] == 0


def _sample_dict(case: object) -> dict[str, Any]:
    return {
        "sample_id": "s-contract-sample",
        "case_id": case.declared_case_id,  # type: ignore[attr-defined]
        "task_type": case.task_type,  # type: ignore[attr-defined]
        "seed": case.seed,  # type: ignore[attr-defined]
        "duration_seconds": case.duration_seconds,  # type: ignore[attr-defined]
        "resolved_parameters": dict(case.resolved_parameters),  # type: ignore[attr-defined]
    }


def _worker_parsed_payload(
    session_factory: Any,
    normalized: dict[str, Any],
    *,
    job_type: str,
    confirm_cover: bool = False,
) -> Any:
    """Run one stored normalized request through the real worker contract.

    The payload is built with ``ControllerWorker._default_payload`` exactly as
    ``_submit_variation`` does, then transfer capabilities and the submission
    nonce are injected as test values, and the strict worker schema parses it.
    """
    from uuid import uuid4

    from ace_service.models import JobType
    from ace_service.repository import (
        confirm_cover_job,
        create_job,
        create_variation_attempt,
        finalize_cover_job_duration,
        transition_job,
    )
    from ace_service.worker import ControllerWorker
    from runpod_worker.schemas import WorkerRequest

    with session_factory() as session:
        job = create_job(
            session,
            job_type=JobType(job_type),
            output_format="mp3",  # type: ignore[arg-type]
            variation_count=1,
            normalized_request_json=normalized,
        )
        session.commit()
        job_id = str(job.id)
        if confirm_cover:
            from ace_service.models import JobStatus

            transition_job(session, job_id, JobStatus.INGESTING)
            transition_job(session, job_id, JobStatus.STAGING)
            finalize_cover_job_duration(session, job_id, 20.0)
            confirm_cover_job(session, job_id)
            session.commit()
        attempt = create_variation_attempt(session, job_id=job_id, variation_index=1)
        session.commit()
    payload = dict(ControllerWorker._default_payload(job, attempt))
    payload["submission_nonce"] = str(uuid4())
    payload["result_upload"] = {
        "url": "https://transfer.example/transfer/v1/contract-output",
        "max_bytes": 1024,
    }
    if confirm_cover:
        payload["source"] = {
            "url": "https://transfer.example/transfer/v1/contract-source",
            "sha256": "a" * 64,
            "bytes": 1234,
            "format": "mp3",
        }
    return WorkerRequest.from_mapping(payload)


def test_strict_v1_smoke_passes_worker_parser(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    smoke = next(case for case in plan.cases if case.declared_case_id == "compat-v1-smoke")
    from ace_service.quality_eval import _v2_normalized_request

    normalized = _v2_normalized_request(_sample_dict(smoke))
    assert normalized["schema_version"] == 1
    session_factory = _product_session_factory(tmp_path / "service.db")
    request = _worker_parsed_payload(session_factory, normalized, job_type="original")
    assert request.schema_version == 1
    assert request.task_type == "original"
    assert request.generation.seed == 1729
    assert request.generation.duration == 20.0
    assert request.generation.cover_strength == 1.0


def test_strict_v2_smoke_passes_worker_parser(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    smoke = next(case for case in plan.cases if case.declared_case_id == "compat-v2-smoke")
    from ace_service.quality_eval import _v2_normalized_request

    normalized = _v2_normalized_request(_sample_dict(smoke))
    assert normalized["schema_version"] == 2
    assert "resolved_parameters" in normalized
    session_factory = _product_session_factory(tmp_path / "service.db")
    request = _worker_parsed_payload(session_factory, normalized, job_type="original")
    assert request.schema_version == 2
    assert request.profile_id == "fast-beta-v1"
    assert request.resolved_parameters is not None
    assert request.resolved_parameters["seed"] == 1729
    assert request.generation.seed == 1729
    assert request.generation.duration == 20.0


def test_normal_original_passes_worker_parser(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    from ace_service.quality_eval import _v2_normalized_request

    normalized = _v2_normalized_request(_sample_dict(case))
    session_factory = _product_session_factory(tmp_path / "service.db")
    request = _worker_parsed_payload(session_factory, normalized, job_type="original")
    assert request.schema_version == 2
    assert request.resolved_parameters is not None
    assert request.resolved_parameters["caption"] == case.resolved_parameters["caption"]
    assert request.generation.prompt == case.resolved_parameters["caption"]
    assert request.generation.duration == case.duration_seconds


def test_confirmed_cover_passes_worker_parser(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    case = next(item for item in plan.cases if item.declared_case_id == "cover-incumbent")
    from ace_service.quality_eval import _v2_normalized_request

    normalized = _v2_normalized_request(_sample_dict(case))
    session_factory = _product_session_factory(tmp_path / "service.db")
    request = _worker_parsed_payload(
        session_factory, normalized, job_type="cover", confirm_cover=True
    )
    assert request.schema_version == 2
    assert request.source is not None and request.source.sha256 == "a" * 64
    assert request.generation.target_style == case.resolved_parameters["caption"]
    assert request.generation.audio_cover_strength == 0.65
    assert request.generation.cover_noise_strength == 0.0


def test_malformed_smoke_fails_the_real_worker_parser(tmp_path: Path) -> None:
    """A broken compatibility fixture must fail before any mocked completion."""
    from runpod_worker.schemas import SchemaError

    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    smoke = next(case for case in plan.cases if case.declared_case_id == "compat-v2-smoke")
    from ace_service.quality_eval import _v2_normalized_request

    normalized = _v2_normalized_request(_sample_dict(smoke))
    normalized.pop("generation", None)
    session_factory = _product_session_factory(tmp_path / "service.db")
    with pytest.raises(SchemaError):
        _worker_parsed_payload(session_factory, normalized, job_type="original")


def test_product_job_id_is_uuid_distinct_from_sample_id(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    smoke = next(case for case in plan.cases if case.declared_case_id == "compat-v1-smoke")
    store.create_campaign("campaign-identity-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-identity-1", smoke, fixture_id=manifest.fixture_id
    )
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(now),
    )
    job_id = submitter.submit("campaign-identity-1", _sample_dict(smoke) | {"sample_id": sample_id})
    from uuid import UUID

    assert str(UUID(job_id)) == job_id
    assert job_id != sample_id
    assert store.sample(sample_id) is not None
    assert store.sample(sample_id)["job_id"] == job_id  # type: ignore[index]


def test_durable_reconciliation_retains_both_identities(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    smoke = next(case for case in plan.cases if case.declared_case_id == "compat-v2-smoke")
    store.create_campaign("campaign-identity-2", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-identity-2", smoke, fixture_id=manifest.fixture_id
    )
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(now),
    )
    job_id = submitter.submit("campaign-identity-2", _sample_dict(smoke) | {"sample_id": sample_id})
    # The opaque campaign sample ID and the UUID product job ID are both
    # retained and durably linked in the campaign store.
    stored = store.sample(sample_id)
    assert stored is not None
    assert stored["sample_id"] == sample_id
    assert stored["job_id"] == job_id
    assert stored["status"] == "completed"
    assert stored["output_path"] is not None


def _fill_sheet_numeric(
    export: dict[str, object],
    *,
    candidate_ids: set[str],
    primary_candidate: int = 5,
    primary_incumbent: int = 4,
    primary_by_sample: dict[str, int] | None = None,
) -> dict[str, object]:
    """Fill a score sheet with numeric vocal scores (vocals-applicable cases)."""

    def dimensions(primary: int) -> dict[str, object]:
        return {
            "melody_retention": primary,
            "prompt_style_adherence": primary,
            "development": primary,
            "vocal_lyric_adherence": primary,
            "artifacts": 5,
            "ending_quality": primary,
        }

    def primary_for(sample_id: str) -> int:
        if primary_by_sample is not None:
            return primary_by_sample.get(sample_id, primary_incumbent)
        return primary_candidate if sample_id in candidate_ids else primary_incumbent

    sample_order = export["sample_order"]  # type: ignore[index]
    return {
        **export,
        "scores": [
            {
                "opaque_sample_id": sample_id,
                "dimensions": dimensions(primary_for(sample_id)),
            }
            for sample_id in sample_order
        ],
        "preferences": [
            {
                "pair_id": pair["pair_id"],
                "choice": (
                    "left"
                    if pair["left"] in candidate_ids  # type: ignore[index]
                    else "right"
                ),
            }
            for pair in export["pairs"]  # type: ignore[index]
        ],
    }


def _finalize_stage(store: CampaignStore, campaign_id: str, stage: str) -> None:
    samples = store.list_samples(campaign_id)
    candidate_ids = {str(item["sample_id"]) for item in samples if str(item["role"]) == "candidate"}
    if stage == "screening":
        # Exactly the top two candidates (by opaque sample ID) earn the top
        # score, so the frozen cutoff rule selects a clean two-way first tie.
        primary_by_sample = {sample_id: 5 for sample_id in sorted(candidate_ids)[:2]}
    else:
        # Every confirmation finalist was already selected; all earn the top
        # score so the decision gate measures the intended improvement.
        primary_by_sample = {sample_id: 5 for sample_id in candidate_ids}
    for listener in ("listener-a", "listener-b"):
        export = store.export_score_sheet(campaign_id, listener, stage=stage)
        store.import_score_sheet(
            campaign_id,
            listener,
            _fill_sheet_numeric(
                export,
                candidate_ids=candidate_ids,
                primary_by_sample=primary_by_sample,
            ),
            stage=stage,
        )
        store.finalize_score_sheet(campaign_id, listener, stage=stage)


def test_cli_advance_persists_and_reaches_confirmation_execution(tmp_path: Path, capsys) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cli-adv-1", manifest, plan)
    declared: dict[str, str] = {}
    for case in plan.cases:
        if case.conditional_on is not None or case.stage not in {
            "cover-screen",
            "original-screen",
            "compatibility",
        }:
            continue
        sample_id, _created = store.add_sample(
            "campaign-cli-adv-1", case, fixture_id=manifest.fixture_id
        )
        declared[case.declared_case_id] = sample_id
    for sample_id in sorted(set(declared.values())):
        store.reserve(
            "campaign-cli-adv-1",
            f"res-{sample_id}",
            kind="compute",
            reserved_micro_usd=1000,
            sample_id=sample_id,
        )
        store.mark_sample_submitted(
            "campaign-cli-adv-1",
            sample_id,
            f"job-{sample_id[:24]}",
            reservation_id=f"res-{sample_id}",
        )
        store.record_terminal_execution(
            "campaign-cli-adv-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path=f"{sample_id}/variation-01.mp3",
        )
    _finalize_stage(store, "campaign-cli-adv-1", "screening")
    # The operator CLI action derives, persists, and materializes advancement
    # only with the fresh explicit confirmation.
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(store.path),
                "--campaign-id",
                "campaign-cli-adv-1",
                "--advance",
                "--confirm",
            ]
        )
        == 0
    )
    assert "advancement=recorded" in capsys.readouterr().out
    samples = store.list_samples("campaign-cli-adv-1", include_aliases=True)
    planned = [item for item in samples if item["status"] == "planned"]
    assert planned
    assert all(item["stage"] in {"cover-confirmation", "original-confirmation"} for item in planned)
    # Confirmation execution consumes exactly the durable planned samples;
    # seed-one aliases stay non-payable and never receive a second job.
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(now),
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    result = controller.execute_confirmation(
        "campaign-cli-adv-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    assert set(result["submitted_samples"]) == {str(item["sample_id"]) for item in planned}
    for item in store.list_samples("campaign-cli-adv-1", include_aliases=True):
        if any(alias.endswith("-confirmation-1729") for alias in item["aliases"]):
            assert item["status"] == "completed"
    assert store.current_gate()["active"] == 0


def test_full_workflow_to_decision_is_deterministic(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(now),
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    controller.execute_screening(
        "campaign-full-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    _finalize_stage(store, "campaign-full-1", "screening")
    advancement = store.advance_screening_to_confirmation("campaign-full-1", manifest, plan)
    assert advancement["recorded"] is True
    assert advancement["finalists"]["cover"]
    assert advancement["finalists"]["original"]
    result = controller.execute_confirmation(
        "campaign-full-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    assert result["submitted_samples"]
    _finalize_stage(store, "campaign-full-1", "confirmation")
    decision = store.record_quality_decision("campaign-full-1", manifest)
    assert decision["recorded"] is True
    assert decision["decision"]["task_decisions"]
    assert all(item["passed"] is True for item in decision["decision"]["task_decisions"])
    assert decision["decision"]["seeds"] == {
        "cover": [1729, 2718, 3141],
        "original": [1729, 2718, 3141],
    }
    again = store.record_quality_decision("campaign-full-1", manifest)
    assert again["recorded"] is False
    assert again["decision_id"] == decision["decision_id"]
    # Immutable persistence: the stored decision is byte-identical.
    stored = store.get_quality_decision("campaign-full-1")
    assert stored is not None and stored["decision_id"] == decision["decision_id"]


def _screening_complete_store(
    store: CampaignStore, campaign_id: str, manifest: object, plan: object
) -> dict[str, str]:
    """Add every screening sample, complete it, and finalize both sheets."""
    declared: dict[str, str] = {}
    for case in plan.cases:  # type: ignore[attr-defined]
        if case.conditional_on is not None or case.stage not in {
            "cover-screen",
            "original-screen",
            "compatibility",
        }:
            continue
        sample_id, _created = store.add_sample(
            campaign_id,
            case,
            fixture_id=manifest.fixture_id,  # type: ignore[attr-defined]
        )
        declared[case.declared_case_id] = sample_id
    for sample_id in sorted(set(declared.values())):
        store.reserve(
            campaign_id,
            f"res-{sample_id}",
            kind="compute",
            reserved_micro_usd=1000,
            sample_id=sample_id,
        )
        store.mark_sample_submitted(
            campaign_id,
            sample_id,
            f"job-{sample_id[:24]}",
            reservation_id=f"res-{sample_id}",
        )
        store.record_terminal_execution(
            campaign_id,
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path=f"{sample_id}/variation-01.mp3",
        )
    _finalize_stage(store, campaign_id, "screening")
    return declared


def test_cli_advance_without_confirm_is_blocked_without_side_effects(
    tmp_path: Path, capsys
) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cli-adv-gate-1", manifest, plan)
    _screening_complete_store(store, "campaign-cli-adv-gate-1", manifest, plan)
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(store.path),
                "--campaign-id",
                "campaign-cli-adv-gate-1",
                "--advance",
            ]
        )
        == 2
    )
    assert "blocked" in capsys.readouterr().err
    with sqlite3.connect(store.path) as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM campaign_events WHERE campaign_id=? "
            "AND event_type='screening_advanced'",
            ("campaign-cli-adv-gate-1",),
        ).fetchone()[0]
    assert events == 0
    assert not [
        item
        for item in store.list_samples("campaign-cli-adv-gate-1")
        if item["status"] == "planned"
    ]
    # A missing campaign database is never created by an unconfirmed advance.
    missing = tmp_path / "never-created.sqlite3"
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-cli-adv-gate-2",
                "--advance",
            ]
        )
        == 2
    )
    assert not missing.exists()


def test_cli_advance_confirm_retry_is_idempotent(tmp_path: Path, capsys) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cli-adv-retry-1", manifest, plan)
    _screening_complete_store(store, "campaign-cli-adv-retry-1", manifest, plan)
    args = [
        "--manifest",
        str(MANIFEST),
        "--campaign-db",
        str(store.path),
        "--campaign-id",
        "campaign-cli-adv-retry-1",
        "--advance",
        "--confirm",
    ]
    assert main(args) == 0
    first_out = capsys.readouterr().out
    assert "advancement=recorded" in first_out
    first_samples = store.list_samples("campaign-cli-adv-retry-1", include_aliases=True)
    assert main(args) == 0
    capsys.readouterr()
    second_samples = store.list_samples("campaign-cli-adv-retry-1", include_aliases=True)
    assert second_samples == first_samples
    with sqlite3.connect(store.path) as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM campaign_events WHERE campaign_id=? "
            "AND event_type='screening_advanced'",
            ("campaign-cli-adv-retry-1",),
        ).fetchone()[0]
    assert events == 1


def _intent_crash_store(
    tmp_path: Path,
    campaign_id: str,
    *,
    after_product_row: bool,
    after_sample_link: bool,
) -> tuple[Any, str, str, str]:
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign(campaign_id, manifest, plan)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    sample_id, _created = store.add_sample(campaign_id, case, fixture_id=manifest.fixture_id)
    store.reserve(
        campaign_id,
        f"res-{sample_id}",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    sample = _sample_dict(case) | {"sample_id": sample_id, "reservation_id": f"res-{sample_id}"}
    session_factory = _product_session_factory(tmp_path / "service.db")
    product_job_id = str(uuid.uuid4())
    from ace_service.quality_eval import _submission_fingerprint, _v2_normalized_request

    fingerprint = _submission_fingerprint(
        job_type="original",
        source_url=None,
        output_format="mp3",
        variation_count=1,
        normalized_request=_v2_normalized_request(sample),
    )
    store.persist_submission_intent(
        campaign_id,
        sample_id,
        product_job_id,
        fingerprint,
        reservation_id=f"res-{sample_id}",
    )
    if after_product_row:
        from ace_service.models import JobType, OutputFormat
        from ace_service.repository import create_job

        with session_factory() as session:
            create_job(
                session,
                job_type=JobType.ORIGINAL,
                output_format=OutputFormat.MP3,
                variation_count=1,
                normalized_request_json=_v2_normalized_request(sample),
                job_id=product_job_id,
            )
            session.commit()
    if after_sample_link:
        store.mark_sample_submitted(
            campaign_id,
            sample_id,
            product_job_id,
            reservation_id=f"res-{sample_id}",
        )
    return store, session_factory, sample_id, product_job_id


@pytest.mark.parametrize(
    ("after_product_row", "after_sample_link"),
    [
        (False, False),
        (True, False),
        (True, True),
    ],
)
def test_crash_recovery_completes_exactly_one_preassigned_job(
    tmp_path: Path, after_product_row: bool, after_sample_link: bool
) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    campaign_id = "campaign-crash-1"
    store, session_factory, sample_id, product_job_id = _intent_crash_store(
        tmp_path,
        campaign_id,
        after_product_row=after_product_row,
        after_sample_link=after_sample_link,
    )
    driver = _CompletingDriver(session_factory)
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=driver,
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(
        store,
        load_fixture_manifest(MANIFEST),
        build_campaign_plan(load_fixture_manifest(MANIFEST)),
        submitter=submitter,
    )
    result = controller.reconcile(campaign_id)
    assert result["resumed"] == [sample_id]
    assert result["completed"] == [sample_id]
    # Exactly one product job with the preassigned UUID, one campaign link,
    # one settled reservation, and at most one remote-submission call.
    from ace_service.repository import get_job

    with session_factory() as session:
        job = get_job(session, product_job_id)
        assert job is not None
        assert job.id == product_job_id
        from sqlalchemy import text

        total = session.execute(
            text("SELECT COUNT(*) FROM jobs WHERE id=:job_id"), {"job_id": product_job_id}
        ).scalar_one()
    assert total == 1
    sample = store.sample(sample_id)
    assert sample is not None
    assert sample["job_id"] == product_job_id
    assert sample["status"] == "completed"
    intent = store.get_submission_intent(campaign_id, sample_id)
    assert intent is not None and intent["status"] == "submitted"
    reservation = store.reservation_for_sample(campaign_id, sample_id)
    assert reservation is not None and reservation["state"] == "settled"
    assert driver.processed == [product_job_id]
    # A second reconcile is idempotent: nothing is resumed or resubmitted.
    again = controller.reconcile(campaign_id)
    assert again == {
        "campaign_id": campaign_id,
        "pre_intent_reconciled": [],
        "resumed": [],
        "completed": [],
        "failed": [],
        "uncertain": [],
    }
    with session_factory() as session:
        from sqlalchemy import text

        total = session.execute(
            text("SELECT COUNT(*) FROM jobs WHERE id=:job_id"), {"job_id": product_job_id}
        ).scalar_one()
    assert total == 1
    assert driver.processed == [product_job_id]


def test_reconcile_refuses_intent_conflict_with_different_job_link(
    tmp_path: Path,
) -> None:
    """A submission intent whose product job does not match the sample link."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-intent-conflict-1", manifest, plan)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    sample_id, _created = store.add_sample(
        "campaign-intent-conflict-1", case, fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-intent-conflict-1",
        f"res-{sample_id}",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.persist_submission_intent(
        "campaign-intent-conflict-1", sample_id, "job-intent-a", "a" * 64
    )
    # The sample is linked to a different product job than the intent froze:
    # the pre-intent transition must not settle it, and the intent recovery
    # must refuse the contradictory linkage.
    store.mark_sample_submitted(
        "campaign-intent-conflict-1",
        sample_id,
        "job-other-b",
        reservation_id=f"res-{sample_id}",
    )
    session_factory = _product_session_factory(tmp_path / "service.db")
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="no matching frozen submission intent"):
        controller.reconcile("campaign-intent-conflict-1")
    # Nothing was settled: the sample stays submitted and the reservation open.
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "submitted"
    reservation = store.reservation_for_sample("campaign-intent-conflict-1", sample_id)
    assert reservation is not None and reservation["state"] == "open"


def test_reconcile_pre_intent_crash_settles_unsubmitted_without_product_job(
    tmp_path: Path,
) -> None:
    """Confirmed reconcile settles the exact pre-intent crash boundary."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-cli-1", manifest, plan)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    sample_id, _created = store.add_sample(
        "campaign-preintent-cli-1", case, fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-preintent-cli-1",
        f"res-{sample_id}",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    session_factory = _product_session_factory(tmp_path / "service.db")

    class _NeverDriver:
        async def process_job(self, job_id: str) -> None:  # pragma: no cover
            raise AssertionError(f"controller must not be called for {job_id}")

    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_NeverDriver(),
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    result = controller.reconcile("campaign-preintent-cli-1")
    assert result["pre_intent_reconciled"] == [sample_id]
    assert result["resumed"] == [] and result["completed"] == []
    # No product job was created and the sample is proven unsubmitted.
    with session_factory() as session:
        from sqlalchemy import text

        total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    assert total == 0
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "unsubmitted"
    reservation = store.reservation_for_sample("campaign-preintent-cli-1", sample_id)
    assert reservation is not None and reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 0
    # Idempotent: the second reconcile reports nothing new.
    again = controller.reconcile("campaign-preintent-cli-1")
    assert again == {
        "campaign_id": "campaign-preintent-cli-1",
        "pre_intent_reconciled": [],
        "resumed": [],
        "completed": [],
        "failed": [],
        "uncertain": [],
    }
    # Verified teardown may now close the window on provider-zero evidence.
    window_id = store.open_execution_window(
        "campaign-preintent-cli-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    teardown = controller.verified_teardown("campaign-preintent-cli-1", confirmed=True)
    assert teardown["teardown"] == "closed"
    assert teardown["window_id"] == window_id
    assert store.current_gate()["active"] == 0


def test_reconcile_refuses_unknown_product_jobs(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-orphan-1", manifest, plan)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    sample_id, _created = store.add_sample(
        "campaign-orphan-1", case, fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-orphan-1",
        f"res-{sample_id}",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    # A linked product job with no frozen submission intent is a phantom row.
    store.mark_sample_submitted(
        "campaign-orphan-1", sample_id, "job-phantom-1", reservation_id=f"res-{sample_id}"
    )
    session_factory = _product_session_factory(tmp_path / "service.db")
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="no matching frozen submission intent"):
        controller.reconcile("campaign-orphan-1")


def test_verified_teardown_refuses_unresolved_state_and_succeeds_when_complete(
    tmp_path: Path,
) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-teardown-1", manifest, plan)
    case = next(item for item in plan.cases if item.declared_case_id == "original-incumbent")
    sample_id, _created = store.add_sample(
        "campaign-teardown-1", case, fixture_id=manifest.fixture_id
    )
    session_factory = _product_session_factory(tmp_path / "service.db")
    product_job_id = str(uuid.uuid4())
    from ace_service.models import JobType
    from ace_service.repository import create_job

    with session_factory() as session:
        create_job(session, job_type=JobType.ORIGINAL, job_id=product_job_id)
        session.commit()
    store.reserve(
        "campaign-teardown-1",
        "res-teardown-1",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.persist_submission_intent("campaign-teardown-1", sample_id, product_job_id, "a" * 64)
    store.mark_sample_submitted(
        "campaign-teardown-1",
        sample_id,
        product_job_id,
        reservation_id="res-teardown-1",
    )
    store.mark_intent_submitted("campaign-teardown-1", sample_id, product_job_id)
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    window_id = store.open_execution_window(
        "campaign-teardown-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    # A submitted sample is unresolved: teardown is blocked before any health
    # evidence is consulted.
    blocked = controller.verified_teardown("campaign-teardown-1", confirmed=True)
    assert blocked["teardown"] == "blocked"
    assert "terminal samples" in blocked["reason"]
    assert store.current_gate()["active"] == 1
    # Terminal evidence without provider-observed zero workers stays blocked.
    store.record_terminal_execution(
        "campaign-teardown-1",
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path=f"{sample_id}/variation-01.mp3",
    )
    nonzero = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
        health_provider=_nonzero_health,
    )
    blocked = EvaluationController(store, manifest, plan, submitter=nonzero).verified_teardown(
        "campaign-teardown-1", confirmed=True
    )
    assert blocked["teardown"] == "blocked"
    assert "zero workers" in blocked["reason"]
    assert store.current_gate()["active"] == 1
    # Malformed provider health also keeps the gate.
    malformed = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
        health_provider=_malformed_health,
    )
    blocked = EvaluationController(store, manifest, plan, submitter=malformed).verified_teardown(
        "campaign-teardown-1", confirmed=True
    )
    assert blocked["teardown"] == "blocked"
    assert store.current_gate()["active"] == 1
    # Validated zero evidence closes the exact open window.
    zero = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
    )
    result = EvaluationController(store, manifest, plan, submitter=zero).verified_teardown(
        "campaign-teardown-1", confirmed=True
    )
    assert result["teardown"] == "closed"
    assert result["window_id"] == window_id
    assert store.current_gate()["active"] == 0
    not_needed = EvaluationController(store, manifest, plan, submitter=zero).verified_teardown(
        "campaign-teardown-1", confirmed=True
    )
    assert not_needed["teardown"] == "not_needed"


def test_execute_success_path_fails_closed_without_provider_health(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
        cover_source_url="https://example.org/fixture-source",
        health_provider=None,
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="validated provider-health evidence"):
        controller.execute_screening(
            "campaign-no-health-1",
            confirmed=True,
            authorization=_authorization(utc_now()),
            rates=_rates(utc_now()),
            boundary=_boundary(),
        )
    # Every sample reached terminal evidence, but the gate stays active: no
    # provider-observed zero evidence was ever produced.
    assert store.current_gate()["active"] == 1
    assert store.get_campaign("campaign-no-health-1")["status"] == "failed"


class _BlockingSubmitter:
    def submit(self, campaign_id: str, sample: object, *, on_submitted=None) -> str:
        del campaign_id, sample, on_submitted
        raise CampaignGateError("injected submission failure")

    def teardown(self, campaign_id: str, window_id: int, *, reason: str) -> None:
        del campaign_id, window_id, reason
        raise CampaignGateError("teardown requires validated provider-health evidence")

    def reconcile(self, campaign_id: str) -> dict[str, object]:
        del campaign_id
        return {}


def test_execute_finally_never_clears_gate_without_complete_evidence(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    controller = EvaluationController(store, manifest, plan, submitter=_BlockingSubmitter())
    with pytest.raises(CampaignGateError, match="injected submission failure"):
        controller.execute_screening(
            "campaign-finally-1",
            confirmed=True,
            authorization=_authorization(utc_now()),
            rates=_rates(utc_now()),
            boundary=_boundary(),
        )
    # The finally path attempted the verified teardown and failed closed: the
    # durable gate and window remain open for operator recovery.
    assert store.current_gate()["active"] == 1
    assert store.get_campaign("campaign-finally-1")["status"] == "failed"


def test_cli_status_is_read_only(tmp_path: Path, capsys) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cli-status-1", manifest, plan)
    store.open_execution_window(
        "campaign-cli-status-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(store.path),
                "--campaign-id",
                "campaign-cli-status-1",
                "--status",
            ]
        )
        == 0
    )
    value = json.loads(capsys.readouterr().out)
    assert value["campaign_id"] == "campaign-cli-status-1"
    assert value["gate"]["active"] is True
    assert value["window"]["stage"] == "screening"
    assert value["zero_worker_evidence"] is False
    # Read-only: the gate and window are untouched by the status action.
    assert store.current_gate()["active"] == 1
    missing = tmp_path / "missing-status.sqlite3"
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-x",
                "--status",
            ]
        )
        == 2
    )
    assert not missing.exists()


def test_cli_recovery_actions_require_confirm_and_existing_database(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    missing = tmp_path / "missing-recovery.sqlite3"
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-x",
                "--reconcile",
            ]
        )
        == 2
    )
    assert not missing.exists()
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-x",
                "--reconcile",
                "--confirm",
            ]
        )
        == 2
    )
    assert not missing.exists()
    assert (
        main(
            [
                "--manifest",
                str(MANIFEST),
                "--campaign-db",
                str(missing),
                "--campaign-id",
                "campaign-x",
                "--verified-teardown",
                "--confirm",
            ]
        )
        == 2
    )
    assert not missing.exists()


def test_cli_unknown_campaign_rejected_before_any_action_wiring(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """All four recovery/status actions reject an unknown campaign by name."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-known-1", manifest, plan)

    def explode(**kwargs: Any) -> None:
        raise AssertionError("submitter wiring must not run for an unknown campaign")

    monkeypatch.setattr("ace_service.quality_eval._build_durable_submitter", explode)
    base = [
        "--manifest",
        str(MANIFEST),
        "--campaign-db",
        str(store.path),
        "--campaign-id",
        "campaign-unknown-1",
    ]
    assert main([*base, "--status"]) == 2
    assert "CampaignValidationError" in capsys.readouterr().err
    backup_target = tmp_path / "unknown-backup.sqlite3"
    assert main([*base, "--backup", str(backup_target)]) == 2
    assert "CampaignValidationError" in capsys.readouterr().err
    assert not backup_target.exists()
    assert main([*base, "--reconcile", "--confirm"]) == 2
    assert "CampaignValidationError" in capsys.readouterr().err
    assert main([*base, "--verified-teardown", "--confirm"]) == 2
    assert "CampaignValidationError" in capsys.readouterr().err
    # No backup, no product/client state was created for the unknown name.
    assert not backup_target.exists()
    assert store.get_campaign("campaign-known-1") is not None


def test_verified_teardown_refuses_mismatched_active_gate(tmp_path: Path) -> None:
    """An active gate of another campaign is rejected, never not_needed."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-gate-owner-1", manifest, plan)
    store.create_campaign("campaign-gate-other-1", manifest, plan)
    store.open_execution_window(
        "campaign-gate-owner-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    session_factory = _product_session_factory(tmp_path / "service.db")
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory),
        store=store,
        rates=_rates(utc_now()),
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="different campaign"):
        controller.verified_teardown("campaign-gate-other-1", confirmed=True)
    # The owner campaign can still tear down; the gate is untouched by the
    # refused request.
    assert store.current_gate()["active"] == 1
    assert str(store.current_gate()["campaign_id"]) == "campaign-gate-owner-1"
    # A known campaign with no active gate still reports not_needed (after the
    # owner closes its own window on provider-zero evidence).
    closed = controller.verified_teardown("campaign-gate-owner-1", confirmed=True)
    assert closed["teardown"] == "closed"
    assert store.current_gate()["active"] == 0
    not_needed = controller.verified_teardown("campaign-gate-other-1", confirmed=True)
    assert not_needed["teardown"] == "not_needed"


def test_cli_status_and_backup_work_without_readable_manifest(tmp_path: Path, capsys) -> None:
    """Status and backup dispatch before any manifest load, hash, or rebuild."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-no-manifest-1", manifest, plan)
    store.open_execution_window(
        "campaign-no-manifest-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    missing_manifest = tmp_path / "missing" / "manifest.json"
    malformed_manifest = tmp_path / "malformed-manifest.json"
    malformed_manifest.write_text("{not valid json", encoding="utf-8")
    for manifest_path in (missing_manifest, malformed_manifest):
        assert (
            main(
                [
                    "--manifest",
                    str(manifest_path),
                    "--campaign-db",
                    str(store.path),
                    "--campaign-id",
                    "campaign-no-manifest-1",
                    "--status",
                ]
            )
            == 0
        )
        value = json.loads(capsys.readouterr().out)
        assert value["campaign_id"] == "campaign-no-manifest-1"
        assert value["gate"]["active"] is True
        backup = tmp_path / f"no-manifest-backup-{manifest_path.name}.sqlite3"
        assert (
            main(
                [
                    "--manifest",
                    str(manifest_path),
                    "--campaign-db",
                    str(store.path),
                    "--campaign-id",
                    "campaign-no-manifest-1",
                    "--backup",
                    str(backup),
                ]
            )
            == 0
        )
        assert backup.is_file() and backup.stat().st_size > 0
        capsys.readouterr()
    # Read-only recovery: no replacement manifest was created next to the
    # missing one, and the gate/window survive both actions untouched.
    assert not missing_manifest.exists()
    assert store.current_gate()["active"] == 1
    assert store.get_campaign("campaign-no-manifest-1") is not None
    # Unknown campaigns are still refused without a readable manifest.
    assert (
        main(
            [
                "--manifest",
                str(missing_manifest),
                "--campaign-db",
                str(store.path),
                "--campaign-id",
                "campaign-unknown",
                "--status",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_cli_recovery_actions_resume_and_teardown_without_readable_manifest(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Reconcile and verified teardown use frozen durable state, not the manifest."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    missing_manifest = tmp_path / "missing" / "manifest.json"
    campaign_id = "campaign-cli-recovery-nm-1"
    store, session_factory, sample_id, product_job_id = _intent_crash_store(
        tmp_path, campaign_id, after_product_row=False, after_sample_link=False
    )
    window_id = store.open_execution_window(
        campaign_id,
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )

    def fake_build_durable_submitter(**kwargs: Any) -> DurableControllerSubmitter:
        del kwargs
        return _durable_submitter(
            session_factory=session_factory,
            driver=_CompletingDriver(session_factory),
            store=store,
            rates=_rates(utc_now()),
            health_provider=_zero_health,
            endpoint_id="endpoint-1",
        )

    monkeypatch.setattr(
        "ace_service.quality_eval._build_durable_submitter", fake_build_durable_submitter
    )
    base = [
        "--manifest",
        str(missing_manifest),
        "--campaign-db",
        str(store.path),
        "--campaign-id",
        campaign_id,
    ]
    # Reconcile resumes the frozen pending intent into exactly one product
    # job and drives it to terminal evidence without reading the manifest.
    assert main([*base, "--reconcile", "--confirm"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["resumed"] == [sample_id]
    assert result["completed"] == [sample_id]
    with session_factory() as session:
        from sqlalchemy import text

        total = session.execute(
            text("SELECT COUNT(*) FROM jobs WHERE id=:job_id"), {"job_id": product_job_id}
        ).scalar_one()
    assert total == 1
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "completed"
    # Verified teardown closes the open window on the real-contract zero
    # health response, again with no manifest available.
    assert main([*base, "--verified-teardown", "--confirm"]) == 0
    teardown = json.loads(capsys.readouterr().out)
    assert teardown["teardown"] == "closed"
    assert teardown["window_id"] == window_id
    assert store.current_gate()["active"] == 0
    # The missing manifest was never created or read.
    assert not missing_manifest.exists()


def test_cli_recovery_actions_still_refuse_bad_state_without_manifest(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Fail-closed recovery refusals remain intact when the manifest is gone."""
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    missing_manifest = tmp_path / "missing" / "manifest.json"
    campaign_id = "campaign-cli-refuse-nm-1"
    store, session_factory, sample_id, product_job_id = _intent_crash_store(
        tmp_path, campaign_id, after_product_row=False, after_sample_link=False
    )
    store.open_execution_window(
        campaign_id,
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )

    def fake_build_durable_submitter(**kwargs: Any) -> DurableControllerSubmitter:
        health_provider = kwargs.get("health_provider", _zero_health)
        return _durable_submitter(
            session_factory=session_factory,
            driver=_CompletingDriver(session_factory),
            store=store,
            rates=_rates(utc_now()),
            health_provider=health_provider,
            endpoint_id="endpoint-1",
        )

    monkeypatch.setattr(
        "ace_service.quality_eval._build_durable_submitter", fake_build_durable_submitter
    )
    base = [
        "--manifest",
        str(missing_manifest),
        "--campaign-db",
        str(store.path),
        "--campaign-id",
        campaign_id,
    ]
    # A linked product job with no frozen submission intent is refused even
    # without a readable manifest.
    store.mark_sample_submitted(
        campaign_id, sample_id, "job-phantom-1", reservation_id=f"res-{sample_id}"
    )
    assert main([*base, "--reconcile", "--confirm"]) == 2
    assert "blocked" in capsys.readouterr().err
    store = CampaignStore.open_existing(store.path)
    assert store is not None
    # A submitted sample is unresolved: teardown stays blocked without
    # consulting provider health, and the gate remains open.
    assert main([*base, "--verified-teardown", "--confirm"]) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["teardown"] == "blocked"
    assert "terminal samples" in blocked["reason"]
    assert store.current_gate()["active"] == 1
    # Terminal evidence with provider-observed nonzero or malformed health
    # keeps the gate closed as well.
    store.record_terminal_execution(
        campaign_id,
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path=f"{sample_id}/variation-01.mp3",
    )
    for health_provider in (_nonzero_health, _malformed_health):
        monkeypatch.setattr(
            "ace_service.quality_eval._build_durable_submitter",
            partial(fake_build_durable_submitter, health_provider=health_provider),
        )
        assert main([*base, "--verified-teardown", "--confirm"]) == 0
        blocked = json.loads(capsys.readouterr().out)
        assert blocked["teardown"] == "blocked"
        assert store.current_gate()["active"] == 1
    assert not missing_manifest.exists()


def test_gated_actions_rejected_while_recovery_state_is_open(tmp_path: Path, capsys) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-gated-1", manifest, plan)
    _screening_complete_store(store, "campaign-gated-1", manifest, plan)
    store.open_execution_window(
        "campaign-gated-1",
        "screening",
        blocked_routes=DEFAULT_BLOCKED_ROUTES,
        edge_config_sha256="c" * 64,
    )
    base = [
        "--manifest",
        str(MANIFEST),
        "--campaign-db",
        str(store.path),
        "--campaign-id",
        "campaign-gated-1",
    ]
    # Advancement, decisions, and score imports are rejected while the gate is
    # open; backup and read-only status remain available.
    assert main([*base, "--advance", "--confirm"]) == 2
    assert "blocked" in capsys.readouterr().err
    assert main([*base, "--decision"]) == 2
    capsys.readouterr()
    backup = tmp_path / "gated-backup.sqlite3"
    assert main([*base, "--backup", str(backup)]) == 0
    capsys.readouterr()
    assert main([*base, "--status"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["gate"]["active"] is True
    # The gate remains untouched by all of the above.
    assert store.current_gate()["active"] == 1


def test_timeout_uncertain_reservation_stays_unresolved(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    driver = _CompletingDriver(session_factory, hang=True)
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=driver,
        store=store,
        rates=_rates(now),
        terminal_timeout_ms=50,
        poll_interval_seconds=0.005,
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    with pytest.raises(CampaignGateError, match="terminal evidence"):
        controller.execute_screening(
            "campaign-execute-timeout-res-1",
            confirmed=True,
            authorization=_authorization(now),
            rates=_rates(now),
            boundary=_boundary(),
        )
    samples = store.list_samples("campaign-execute-timeout-res-1")
    assert samples and samples[0]["status"] == "uncertain"
    # Through the eval pipeline the timed-out attempt keeps its full
    # reservation unresolved: never conservatively retained, never settled.
    reservation = store.reservation_for_sample(
        "campaign-execute-timeout-res-1", samples[0]["sample_id"]
    )
    assert reservation is not None
    assert reservation["state"] == "unresolved"
    assert reservation["final_estimate_micro_usd"] is None
    assert reservation["settled_at_utc"] is None
    assert reservation["reserved_micro_usd"] > 0
    status = store.campaign_status("campaign-execute-timeout-res-1")
    assert status["reservations"] == {"unresolved": 1}
    assert store.current_gate()["active"] == 1


def test_reconcile_never_rewrites_failed_terminal_identity(tmp_path: Path) -> None:
    if not MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    manifest = load_fixture_manifest(MANIFEST)
    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    session_factory = _product_session_factory(tmp_path / "service.db")
    now = utc_now()
    submitter = _durable_submitter(
        session_factory=session_factory,
        driver=_CompletingDriver(session_factory, fail=True, fail_with_evidence=True),
        store=store,
        rates=_rates(now),
        cover_source_url="https://example.org/fixture-source",
    )
    controller = EvaluationController(store, manifest, plan, submitter=submitter)
    controller.execute_screening(
        "campaign-execute-immutable-eval-1",
        confirmed=True,
        authorization=_authorization(now),
        rates=_rates(now),
        boundary=_boundary(),
    )
    samples = store.list_samples("campaign-execute-immutable-eval-1")
    assert samples and all(str(item["status"]) == "failed" for item in samples)
    # Recovery drives only in-flight samples; a failed terminal identity is
    # never rewritten even though the product job row still exists.
    reconciled = submitter.reconcile("campaign-execute-immutable-eval-1")
    assert reconciled["completed"] == []
    assert reconciled["failed"] == []
    refreshed = store.list_samples("campaign-execute-immutable-eval-1")
    assert [str(item["status"]) for item in refreshed] == [str(item["status"]) for item in samples]
    # The failed identity cannot later become billed completed work.
    with pytest.raises(CampaignGateError, match="terminal sample identity is immutable"):
        store.record_terminal_execution(
            "campaign-execute-immutable-eval-1",
            str(samples[0]["sample_id"]),
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=22_499,
            hourly_rate_usd="0.49",
            output_path=f"{samples[0]['sample_id']}/variation-01.mp3",
        )
    reservation = store.reservation_for_sample(
        "campaign-execute-immutable-eval-1", samples[0]["sample_id"]
    )
    assert reservation is not None
    assert reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None

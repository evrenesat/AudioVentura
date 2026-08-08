from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ace_service.campaign import (
    CAMPAIGN_ADMISSION_STOP_MICRO_USD,
    CampaignBudgetError,
    CampaignCase,
    CampaignGateError,
    CampaignSchemaError,
    CampaignStore,
    CampaignValidationError,
    FixtureManifest,
    ModelPreflight,
    build_confirmation_cases,
    evaluate_confirmation_gate,
    execution_micro_usd,
    load_fixture_manifest,
    parse_endpoint_billing_response,
    parse_network_volume_billing_response,
    primary_dimensions,
    utc_now,
    validate_model_preflight,
)

PRIVATE_MANIFEST = Path("/srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json")


def _manifest() -> FixtureManifest:
    if not PRIVATE_MANIFEST.is_file():
        pytest.skip("private Checkpoint 1 fixture is not mounted")
    return load_fixture_manifest(PRIVATE_MANIFEST)


def _small_plan() -> tuple[FixtureManifest, object, list[CampaignCase]]:
    manifest = _manifest()
    cases = [
        CampaignCase(
            declared_case_id="cover-incumbent",
            fixture_case_id="cover-baseline",
            task_type="cover",
            stage="cover-screen",
            role="incumbent",
            seed=1729,
            profile_id="fast-beta-v1",
            model="acestep-v15-xl-turbo",
            lm_model=None,
            prompt_mode="direct",
            duration_seconds=20.0,
            resolved_parameters={"seed": 1729, "cover_noise_strength": 0.0},
            pair_key="cover-screen",
        ),
        CampaignCase(
            declared_case_id="cover-candidate",
            fixture_case_id="cover-baseline",
            task_type="cover",
            stage="cover-screen",
            role="candidate",
            seed=1729,
            profile_id="quality-v1",
            model="acestep-v15-xl-turbo",
            lm_model=None,
            prompt_mode="direct",
            duration_seconds=20.0,
            resolved_parameters={"seed": 1729, "cover_noise_strength": 0.2},
            pair_key="cover-screen",
        ),
    ]
    from ace_service.campaign import CampaignPlan

    plan = CampaignPlan(
        cases=tuple(cases),
        mandatory_case_count=2,
        maximum_case_count=2,
        mandatory_confirmation_attempts=0,
        maximum_confirmation_attempts=0,
        storage_case_count=0,
    )
    return manifest, plan, cases


def test_micro_usd_rounds_once_and_rejects_binary_float() -> None:
    assert execution_micro_usd(22_499, "0.49") == 3_062
    with pytest.raises(CampaignValidationError):
        execution_micro_usd(22_499, 0.49)


def test_provider_contracts_preserve_server_usd_and_account_wide_volume() -> None:
    observations = parse_endpoint_billing_response(
        [
            {
                "endpointId": "endpoint-1",
                "startTime": "2026-08-08T10:00:00Z",
                "bucketSize": "hour",
                "amount": "0.4900",
                "timeBilled": "3600",
            }
        ],
        endpoint_id="endpoint-1",
    )
    assert observations[0].currency == "USD"
    assert observations[0].raw_amount == "0.4900"
    assert observations[0].amount_micro_usd == 490_000

    volume = parse_network_volume_billing_response(
        [
            {
                "startTime": "2026-08-08T10:00:00Z",
                "bucketSize": "hour",
                "amount": "1.25",
            }
        ]
    )
    assert volume[0].allocatable is False
    assert volume[0].grouping_dimension == "account"
    assert volume[0].unavailable_reason == "provider_response_missing_volume_identifier"


def test_campaign_store_deduplicates_before_execution_and_reopens(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    database = tmp_path / "evaluations" / "campaign.sqlite3"
    store = CampaignStore(database)
    store.create_campaign("campaign-test-1", manifest, plan)
    first, created = store.add_sample("campaign-test-1", cases[0], fixture_id=manifest.fixture_id)
    alias, alias_created = store.add_sample(
        "campaign-test-1",
        replace(cases[0], declared_case_id="cover-corrected-controls"),
        fixture_id=manifest.fixture_id,
    )
    assert created is True
    assert alias_created is False
    assert alias == first
    assert store.list_samples("campaign-test-1", include_aliases=True)[0]["aliases"] == [
        "cover-corrected-controls"
    ]
    assert (database.stat().st_mode & 0o777) == 0o600

    reopened = CampaignStore.open_existing(database)
    assert reopened is not None
    backup = reopened.backup(tmp_path / "backup.sqlite3")
    assert backup.is_file()
    assert (backup.stat().st_mode & 0o777) == 0o600

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(CampaignSchemaError):
        CampaignStore.open_existing(database)


def test_budget_guard_never_admits_above_four_point_five_dollars(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-budget-1", manifest, plan)
    store.reserve(
        "campaign-budget-1",
        "reservation-a",
        kind="compatibility",
        reserved_micro_usd=CAMPAIGN_ADMISSION_STOP_MICRO_USD - 1,
    )
    with pytest.raises(CampaignBudgetError):
        store.reserve(
            "campaign-budget-1",
            "reservation-b",
            kind="compatibility",
            reserved_micro_usd=2,
        )


def test_unavailable_terminal_cost_can_be_completed_by_new_authoritative_evidence(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cost-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-cost-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-cost-1",
        "reservation-cost-1",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-cost-1", sample_id, "job-cost-1", reservation_id="reservation-cost-1"
    )
    with pytest.raises(CampaignValidationError):
        store.record_terminal_execution(
            "campaign-cost-1",
            sample_id,
            status="completed",
            actual_gpu=None,
            execution_ms=100,
            hourly_rate_usd="0.49",
        )
    assert (
        store.record_terminal_execution(
            "campaign-cost-1",
            sample_id,
            status="completed",
            actual_gpu=None,
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-cost-1/variation-01.mp3",
        )
        is None
    )
    estimate = store.record_terminal_execution(
        "campaign-cost-1",
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-cost-1/variation-01.mp3",
    )
    assert estimate == 14
    sample = store.sample(sample_id)
    assert sample is not None and sample["output_path"] == "job-cost-1/variation-01.mp3"


def test_never_submitted_attempt_has_no_compute_record_but_closes_reservation(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-unsubmitted-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-unsubmitted-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-unsubmitted-1",
        "reservation-unsubmitted-1",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    assert (
        store.record_terminal_execution(
            "campaign-unsubmitted-1",
            sample_id,
            status="unsubmitted",
            actual_gpu=None,
            execution_ms=None,
            hourly_rate_usd=None,
        )
        is None
    )
    sample = store.sample(sample_id)
    reservation = store.reservation_for_sample("campaign-unsubmitted-1", sample_id)
    assert sample is not None and sample["estimated_compute_micro_usd"] is None
    assert sample["cost_status"] == "not_submitted"
    assert reservation is not None
    assert reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 0


def test_cover_primary_includes_vocal_dimension_only_when_applicable() -> None:
    assert "vocal_lyric_adherence" in primary_dimensions("cover", vocals_applicable=True)
    assert "vocal_lyric_adherence" not in primary_dimensions("cover", vocals_applicable=False)


def test_conditional_model_preflight_fails_closed_on_hash_or_runtime_contract() -> None:
    with pytest.raises(CampaignValidationError):
        ModelPreflight(
            artifact_hashes={"model": "not-a-hash"},
            expected_hashes={"model": "a" * 64},
            available_bytes=1,
            required_bytes=1,
            gpu_memory_headroom_bytes=1,
            required_memory_bytes=1,
            supported_runtime_contract=True,
            rollback_path_recorded=True,
            reservation_micro_usd=1,
        )
    preflight = ModelPreflight(
        artifact_hashes={"model": "a" * 64},
        expected_hashes={"model": "a" * 64},
        available_bytes=1,
        required_bytes=1,
        gpu_memory_headroom_bytes=1,
        required_memory_bytes=1,
        supported_runtime_contract=False,
        rollback_path_recorded=True,
        reservation_micro_usd=1,
    )
    with pytest.raises(CampaignGateError):
        validate_model_preflight(preflight)


def test_gate_survives_restart_and_blocks_rollback_until_teardown(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-gate-1", manifest, plan)
    window_id = store.open_execution_window(
        "campaign-gate-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    assert store.submission_allowed() is False
    assert store.submission_allowed(campaign_id="campaign-gate-1") is True
    assert store.rollback_readiness().safe is False
    store.recover_after_restart(now=utc_now())
    assert store.current_gate()["active"] == 1
    store.record_edge_guard(
        "campaign-gate-1",
        enabled=True,
        verified=True,
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
        config_sha256="a" * 64,
        rollback_target="release-before-campaign",
    )
    store.close_execution_window(
        "campaign-gate-1",
        window_id,
        health_evidence=_health_evidence(),
        reason="operator teardown complete",
    )
    assert store.submission_allowed() is True
    assert store.rollback_readiness().safe is True


def test_campaign_lease_excludes_concurrent_owner_and_recovers_expiry(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    manifest, plan, _cases = _small_plan()
    store.create_campaign("campaign-lease-1", manifest, plan)
    now = utc_now()
    store.acquire_lease("campaign-runner", "owner-a", campaign_id="campaign-lease-1", now=now)
    with pytest.raises(CampaignGateError):
        store.acquire_lease("campaign-runner", "owner-b", campaign_id="campaign-lease-1", now=now)
    store.acquire_lease(
        "campaign-runner",
        "owner-b",
        campaign_id="campaign-lease-1",
        ttl_seconds=1,
        now=now + timedelta(minutes=6),
    )
    assert store.recover_after_restart(now=now + timedelta(minutes=6, seconds=2)) == (
        "campaign-runner",
    )


def _complete_sample(
    store: CampaignStore, campaign_id: str, sample_id: str, output_path: str
) -> None:
    store.reserve(
        campaign_id,
        f"res-{sample_id}",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        campaign_id, sample_id, f"job-{sample_id[:24]}", reservation_id=f"res-{sample_id}"
    )
    store.record_terminal_execution(
        campaign_id,
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path=output_path,
    )


def _health_evidence(
    *,
    endpoint_id: str = "endpoint-1",
    active: int = 0,
    idle: int = 0,
    running: int = 0,
    queued: int = 0,
    in_progress: int = 0,
    captured_at_utc: str | None = None,
) -> dict[str, object]:
    return {
        "endpoint_id": endpoint_id,
        "captured_at_utc": captured_at_utc or utc_now().isoformat(),
        "active_workers": active,
        "idle_workers": idle,
        "running_workers": running,
        "queued_jobs": queued,
        "in_progress_jobs": in_progress,
        "source": "runpod_health",
    }


def test_two_listener_score_sheets_reject_partial_and_finalize_immutably(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-score-1", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-score-1", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    # Planned samples make the campaign partial: export must fail closed.
    with pytest.raises(CampaignGateError, match="complete output evidence"):
        store.export_score_sheet("campaign-score-1", "listener-a")
    for sample_id in sample_ids:
        _complete_sample(store, "campaign-score-1", sample_id, f"{sample_id}/variation-01.mp3")
    export_a = store.export_score_sheet("campaign-score-1", "listener-a")
    with pytest.raises(CampaignGateError):
        store.finalize_score_sheet("campaign-score-1", "listener-a")
    export_b = store.export_score_sheet("campaign-score-1", "listener-b")

    def complete(export: dict[str, object]) -> dict[str, object]:
        return {
            **export,
            "scores": [
                {
                    "opaque_sample_id": sample_id,
                    "dimensions": {
                        "melody_retention": 4,
                        "prompt_style_adherence": 4,
                        "development": 4,
                        "vocal_lyric_adherence": "not_applicable",
                        "artifacts": 5,
                        "ending_quality": 4,
                    },
                }
                for sample_id in export["sample_order"]  # type: ignore[index]
            ],
            "preferences": [
                {"pair_id": pair["pair_id"], "choice": "tie"}
                for pair in export["pairs"]  # type: ignore[index]
            ],
        }

    malformed = complete(export_a)
    malformed["scores"] = list(malformed["scores"]) + [malformed["scores"][0]]  # type: ignore[index]
    with pytest.raises(CampaignValidationError):
        store.import_score_sheet("campaign-score-1", "listener-a", malformed)
    store.import_score_sheet("campaign-score-1", "listener-a", complete(export_a))
    store.import_score_sheet("campaign-score-1", "listener-b", complete(export_b))
    store.finalize_score_sheet("campaign-score-1", "listener-a")
    store.finalize_score_sheet("campaign-score-1", "listener-b")
    with pytest.raises(CampaignGateError):
        store.import_score_sheet("campaign-score-1", "listener-a", complete(export_a))
    assert set(store.finalized_scores("campaign-score-1")) == {"listener-a", "listener-b"}


def test_cleanup_waits_for_retention_decision_and_removes_only_campaign_copy(
    tmp_path: Path,
) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cleanup-1", manifest, plan)
    media_root = tmp_path / "evaluation-media"
    campaign_media = media_root / "campaign-cleanup-1"
    campaign_media.mkdir(parents=True)
    media_file = campaign_media / "opaque-sample.mp3"
    media_file.write_bytes(b"fixture-output")

    assert store.cleanup_media("campaign-cleanup-1", media_root) == ()
    assert media_file.is_file()
    removed = store.cleanup_media(
        "campaign-cleanup-1",
        media_root,
        now=manifest.retention_deadline + timedelta(seconds=1),
        retention_decision="delete",
    )
    assert removed == ("opaque-sample.mp3",)
    assert not campaign_media.exists()


def test_confirmation_gate_requires_complete_matched_pairs() -> None:
    def score(primary: int, artifact: int = 5) -> dict[str, object]:
        return {
            "dimensions": {
                "melody_retention": primary,
                "prompt_style_adherence": primary,
                "development": primary,
                "vocal_lyric_adherence": "not_applicable",
                "artifacts": artifact,
                "ending_quality": primary,
            }
        }

    candidate = {seed: score(5) for seed in (1729, 2718, 3141)}
    incumbent = {seed: score(4) for seed in (1729, 2718, 3141)}
    decision = evaluate_confirmation_gate(
        candidate_id="candidate-1",
        task_type="cover",
        candidate_by_seed=candidate,
        incumbent_by_seed=incumbent,
        listener_candidate_by_seed=[candidate, candidate],
        listener_incumbent_by_seed=[incumbent, incumbent],
        listener_preferences=[["candidate"] * 3, ["candidate"] * 3],
        vocals_applicable=False,
    )
    assert decision.passed is True

    incomplete = dict(candidate)
    incomplete.pop(3141)
    rejected = evaluate_confirmation_gate(
        candidate_id="candidate-1",
        task_type="cover",
        candidate_by_seed=candidate,
        incumbent_by_seed=incumbent,
        listener_candidate_by_seed=[incomplete, candidate],
        listener_incumbent_by_seed=[incumbent, incumbent],
        listener_preferences=[["candidate"] * 3, ["candidate"] * 3],
        vocals_applicable=False,
    )
    assert rejected.passed is False

    mismatched = dict(candidate)
    mismatched.pop(1729)
    mismatched[9999] = score(5)
    seed_rejected = evaluate_confirmation_gate(
        candidate_id="candidate-1",
        task_type="cover",
        candidate_by_seed=candidate,
        incumbent_by_seed=incumbent,
        listener_candidate_by_seed=[mismatched, candidate],
        listener_incumbent_by_seed=[incumbent, incumbent],
        listener_preferences=[["candidate"] * 3, ["candidate"] * 3],
        vocals_applicable=False,
    )
    assert seed_rejected.reason == "three_complete_confirmation_seeds_required"


def _confirmation_case(
    declared_case_id: str,
    role: str,
    seed: int,
    *,
    stage: str = "cover-confirmation",
    resolved: dict[str, object] | None = None,
) -> CampaignCase:
    return CampaignCase(
        declared_case_id=declared_case_id,
        fixture_case_id="cover-baseline",
        task_type="cover",
        stage=stage,
        role=role,
        seed=seed,
        profile_id="quality-v1",
        model="acestep-v15-xl-turbo",
        lm_model=None,
        prompt_mode="direct",
        duration_seconds=20.0,
        resolved_parameters=resolved or {"seed": seed, "cover_noise_strength": 0.2},
        pair_key=f"cover-confirmation-{seed}",
    )


def test_screening_sample_is_reused_as_confirmation_seed_one(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-reuse-1", manifest, plan)
    candidate_id, _created = store.add_sample(
        "campaign-reuse-1", cases[1], fixture_id=manifest.fixture_id
    )
    _complete_sample(store, "campaign-reuse-1", candidate_id, "candidate/variation-01.mp3")
    # Confirmation seed one carries the exact screening fingerprint and must
    # reuse the executed screening output instead of paying twice.
    confirmation = _confirmation_case("cover-candidate-confirmation-1729", "candidate", 1729)
    reused, created = store.add_sample(
        "campaign-reuse-1", confirmation, fixture_id=manifest.fixture_id
    )
    assert created is False
    assert reused == candidate_id
    assert store.sample(candidate_id) is not None
    assert store.sample(candidate_id)["status"] == "completed"
    aliases = store.list_samples("campaign-reuse-1", include_aliases=True)[0]["aliases"]
    assert "cover-candidate-confirmation-1729" in aliases
    # A later confirmation seed is a genuinely new payable attempt.
    fresh, fresh_created = store.add_sample(
        "campaign-reuse-1",
        _confirmation_case("cover-candidate-confirmation-2718", "candidate", 2718),
        fixture_id=manifest.fixture_id,
    )
    assert fresh_created is True
    assert fresh != candidate_id


def test_confirmation_reuse_rejects_contamination_and_unusable_outputs(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-reuse-2", manifest, plan)
    # A failed screening sample cannot be silently reused as a confirmation.
    failed_id, _created = store.add_sample(
        "campaign-reuse-2",
        _confirmation_case(
            "cover-candidate-failed",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    store.reserve(
        "campaign-reuse-2",
        "reservation-failed",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=failed_id,
    )
    store.mark_sample_submitted(
        "campaign-reuse-2", failed_id, "job-reuse-failed", reservation_id="reservation-failed"
    )
    store.record_terminal_execution(
        "campaign-reuse-2",
        failed_id,
        status="failed",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="injected_failure",
    )
    with pytest.raises(CampaignGateError, match="after execution"):
        store.add_sample(
            "campaign-reuse-2",
            _confirmation_case(
                "cover-candidate-failed-confirmation-1729",
                "candidate",
                1729,
                resolved={"seed": 1729, "cover_noise_strength": 0.30},
            ),
            fixture_id=manifest.fixture_id,
        )
    # A completed sample whose role disagrees with the confirmation role is
    # contamination, not reuse: the fingerprint matches but the roles differ.
    candidate_id, _created = store.add_sample(
        "campaign-reuse-2", cases[1], fixture_id=manifest.fixture_id
    )
    _complete_sample(store, "campaign-reuse-2", candidate_id, "candidate/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="after execution"):
        store.add_sample(
            "campaign-reuse-2",
            _confirmation_case(
                "cover-incumbent-confirmation-1729",
                "incumbent",
                1729,
                resolved={"seed": 1729, "cover_noise_strength": 0.2},
            ),
            fixture_id=manifest.fixture_id,
        )
    # A confirmation case may not reuse a completed compatibility smoke.
    compatibility = _confirmation_case(
        "compat-v1-smoke",
        "candidate",
        1729,
        stage="compatibility",
        resolved={"schema_version": 1, "seed": 1729},
    )
    compat_id, _created = store.add_sample(
        "campaign-reuse-2", compatibility, fixture_id=manifest.fixture_id
    )
    _complete_sample(store, "campaign-reuse-2", compat_id, "compat/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="after execution"):
        store.add_sample(
            "campaign-reuse-2",
            _confirmation_case(
                "compat-v1-smoke-confirmation-1729",
                "candidate",
                1729,
                resolved={"schema_version": 1, "seed": 1729},
            ),
            fixture_id=manifest.fixture_id,
        )


def test_finalize_rechecks_output_evidence_and_stage_scopes_sheets(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-stage-1", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-stage-1", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    for sample_id in sample_ids:
        _complete_sample(store, "campaign-stage-1", sample_id, f"{sample_id}/variation-01.mp3")
    export_a = store.export_score_sheet("campaign-stage-1", "listener-a")
    export_b = store.export_score_sheet("campaign-stage-1", "listener-b")

    def complete(export: dict[str, object]) -> dict[str, object]:
        return {
            **export,
            "scores": [
                {
                    "opaque_sample_id": sample_id,
                    "dimensions": {
                        "melody_retention": 4,
                        "prompt_style_adherence": 4,
                        "development": 4,
                        "vocal_lyric_adherence": "not_applicable",
                        "artifacts": 5,
                        "ending_quality": 4,
                    },
                }
                for sample_id in export["sample_order"]  # type: ignore[index]
            ],
            "preferences": [
                {"pair_id": pair["pair_id"], "choice": "tie"}
                for pair in export["pairs"]  # type: ignore[index]
            ],
        }

    store.import_score_sheet("campaign-stage-1", "listener-a", complete(export_a))
    store.import_score_sheet("campaign-stage-1", "listener-b", complete(export_b))
    # A late planned screening sample makes finalization reject the frozen
    # sheet again: finalization re-requires complete output evidence.
    late, _created = store.add_sample(
        "campaign-stage-1",
        _confirmation_case(
            "cover-candidate-two",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    assert late
    with pytest.raises(CampaignGateError, match="complete output evidence"):
        store.finalize_score_sheet("campaign-stage-1", "listener-a")
    # Even after the late sample completes, the frozen export still omits it,
    # so finalization rejects the stale sheet for coverage rather than
    # silently freezing a partial decision set.
    _complete_sample(store, "campaign-stage-1", late, f"{late}/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.finalize_score_sheet("campaign-stage-1", "listener-a")
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.finalize_score_sheet("campaign-stage-1", "listener-b")
    # Import of a sheet exported before the late sample is also stale.
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet("campaign-stage-1", "listener-b", complete(export_b))
    # The confirmation-stage export must not see screening-stage samples:
    # with none declared it is rejected rather than exporting screening rows.
    with pytest.raises(CampaignGateError, match="before samples are declared"):
        store.export_score_sheet("campaign-stage-1", "listener-a", stage="confirmation")


def test_backup_refuses_missing_campaign_database(tmp_path: Path) -> None:
    missing = tmp_path / "evaluations" / "quality-campaign.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    # The read-only open refuses without creating anything.
    assert CampaignStore.open_existing(missing) is None
    with pytest.raises(CampaignSchemaError):
        CampaignStore(missing, create=False).backup(destination)
    assert not missing.exists()
    assert not destination.exists()
    # Even a freshly created store refuses to present itself as a backup
    # source: the operator typo must not be rewarded with an empty backup.
    created = CampaignStore(missing)
    with pytest.raises(CampaignSchemaError, match="does not exist"):
        created.backup(destination)
    assert not destination.exists()


def _fill_sheet(
    export: dict[str, object],
    *,
    candidate_ids: set[str],
    primary_candidate: int = 5,
    primary_incumbent: int = 4,
) -> dict[str, object]:
    def dimensions(primary: int) -> dict[str, object]:
        return {
            "melody_retention": primary,
            "prompt_style_adherence": primary,
            "development": primary,
            "vocal_lyric_adherence": "not_applicable",
            "artifacts": 5,
            "ending_quality": primary,
        }

    sample_order = export["sample_order"]  # type: ignore[index]
    return {
        **export,
        "scores": [
            {
                "opaque_sample_id": sample_id,
                "dimensions": dimensions(
                    primary_candidate if sample_id in candidate_ids else primary_incumbent
                ),
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


def test_quality_decision_unblinds_pairs_gates_and_persists_immutably(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-decision-1", manifest, plan)
    screening_ids: dict[str, str] = {}
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-decision-1", case, fixture_id=manifest.fixture_id
        )
        screening_ids[case.declared_case_id] = sample_id
    for sample_id in screening_ids.values():
        _complete_sample(store, "campaign-decision-1", sample_id, f"{sample_id}/variation-01.mp3")
    confirmation = build_confirmation_cases(manifest, plan, ("cover-candidate",))
    confirmation_ids: dict[str, tuple[str, bool]] = {}
    for case in confirmation:
        sample_id, created = store.add_sample(
            "campaign-decision-1", case, fixture_id=manifest.fixture_id
        )
        confirmation_ids[case.declared_case_id] = (sample_id, created)
    assert confirmation_ids["cover-candidate-confirmation-1729"] == (
        screening_ids["cover-candidate"],
        False,
    )
    assert confirmation_ids["cover-incumbent-confirmation-1729"] == (
        screening_ids["cover-incumbent"],
        False,
    )
    for _declared, (sample_id, created) in confirmation_ids.items():
        if created:
            _complete_sample(
                store, "campaign-decision-1", sample_id, f"{sample_id}/variation-01.mp3"
            )
    # A decision before both stages are finalized fails closed.
    with pytest.raises(CampaignGateError):
        store.record_quality_decision("campaign-decision-1", manifest)
    screening_a = store.export_score_sheet("campaign-decision-1", "listener-a")
    screening_b = store.export_score_sheet("campaign-decision-1", "listener-b")
    confirmation_a = store.export_score_sheet(
        "campaign-decision-1", "listener-a", stage="confirmation"
    )
    confirmation_b = store.export_score_sheet(
        "campaign-decision-1", "listener-b", stage="confirmation"
    )
    # The confirmation sheet contains only the two new payable seeds.
    assert {pair["pair_id"] for pair in confirmation_a["pairs"]}  # type: ignore[index]
    candidate_ids = {screening_ids["cover-candidate"]}
    for declared, (sample_id, _created) in confirmation_ids.items():
        if declared.startswith("cover-candidate-confirmation-"):
            candidate_ids.add(sample_id)
    for export in (screening_a, screening_b, confirmation_a, confirmation_b):
        store.import_score_sheet(
            "campaign-decision-1",
            str(export["listener_id"]),
            _fill_sheet(export, candidate_ids=candidate_ids),
            stage=str(export["stage"]),
        )
    store.finalize_score_sheet("campaign-decision-1", "listener-a")
    store.finalize_score_sheet("campaign-decision-1", "listener-b")
    store.finalize_score_sheet("campaign-decision-1", "listener-a", stage="confirmation")
    store.finalize_score_sheet("campaign-decision-1", "listener-b", stage="confirmation")
    decision = store.record_quality_decision("campaign-decision-1", manifest)
    assert decision["recorded"] is True
    assert len(str(decision["decision_id"])) == 64
    tasks = decision["decision"]["task_decisions"]
    assert len(tasks) == 1
    cover = tasks[0]
    assert cover["task_type"] == "cover"
    assert cover["candidate_id"] == "cover-candidate"
    assert cover["passed"] is True
    assert cover["reason"] == "passed"
    assert cover["mean_primary_improvement"] == 1.0
    assert cover["listener_preferences"] == [3, 3]
    provenance = decision["decision"]["provenance"]
    assert provenance["screening_sheet_hashes"] and provenance["confirmation_sheet_hashes"]
    assert set(provenance["samples"]) == set(
        list(screening_ids.values()) + [id for id, _c in confirmation_ids.values()]
    )
    assert decision["decision"]["listener_ids"] == ["listener-a", "listener-b"]
    assert decision["decision"]["seeds"] == {"cover": [1729, 2718, 3141]}
    # Repeated identical finalization is idempotent.
    again = store.record_quality_decision("campaign-decision-1", manifest)
    assert again["recorded"] is False
    assert again["decision_id"] == decision["decision_id"]
    stored = store.get_quality_decision("campaign-decision-1")
    assert stored is not None and stored["decision_id"] == decision["decision_id"]


def test_quality_decision_conflict_fails_closed(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-decision-2", manifest, plan)
    screening_ids: dict[str, str] = {}
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-decision-2", case, fixture_id=manifest.fixture_id
        )
        screening_ids[case.declared_case_id] = sample_id
        _complete_sample(store, "campaign-decision-2", sample_id, f"{sample_id}/variation-01.mp3")
    confirmation = build_confirmation_cases(manifest, plan, ("cover-candidate",))
    confirmation_ids: dict[str, tuple[str, bool]] = {}
    for case in confirmation:
        sample_id, created = store.add_sample(
            "campaign-decision-2", case, fixture_id=manifest.fixture_id
        )
        confirmation_ids[case.declared_case_id] = (sample_id, created)
    for _declared, (sample_id, created) in confirmation_ids.items():
        if created:
            _complete_sample(
                store, "campaign-decision-2", sample_id, f"{sample_id}/variation-01.mp3"
            )
    candidate_ids = {screening_ids["cover-candidate"]}
    for declared, (sample_id, _created) in confirmation_ids.items():
        if declared.startswith("cover-candidate-confirmation-"):
            candidate_ids.add(sample_id)
    for stage in ("screening", "confirmation"):
        for listener in ("listener-a", "listener-b"):
            export = store.export_score_sheet("campaign-decision-2", listener, stage=stage)
            store.import_score_sheet(
                "campaign-decision-2",
                listener,
                _fill_sheet(export, candidate_ids=candidate_ids),
                stage=stage,
            )
            store.finalize_score_sheet("campaign-decision-2", listener, stage=stage)
    first = store.record_quality_decision("campaign-decision-2", manifest)
    assert first["recorded"] is True
    # New completed screening evidence after the decision changes the
    # provenance, so a re-decision is a conflict, not an idempotent repeat.
    extra = _confirmation_case(
        "cover-candidate-two",
        "candidate",
        1729,
        stage="cover-screen",
        resolved={"seed": 1729, "cover_noise_strength": 0.30},
    )
    extra_id, _created = store.add_sample(
        "campaign-decision-2", extra, fixture_id=manifest.fixture_id
    )
    _complete_sample(store, "campaign-decision-2", extra_id, f"{extra_id}/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="conflicting quality decision"):
        store.record_quality_decision("campaign-decision-2", manifest)


def test_failed_attempt_with_full_evidence_is_never_billed(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-failed-billing-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-failed-billing-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-failed-billing-1",
        "reservation-failed-billing",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-failed-billing-1",
        sample_id,
        "job-failed-billing",
        reservation_id="reservation-failed-billing",
    )
    assert (
        store.record_terminal_execution(
            "campaign-failed-billing-1",
            sample_id,
            status="failed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=22_499,
            hourly_rate_usd="0.49",
            unavailable_reason="runpod_generation_failed",
        )
        is None
    )
    sample = store.sample(sample_id)
    assert sample is not None
    assert sample["cost_status"] == "unavailable"
    assert sample["estimated_compute_micro_usd"] is None
    reservation = store.reservation_for_sample("campaign-failed-billing-1", sample_id)
    assert reservation is not None and reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None
    assert reservation["reserved_micro_usd"] == 1000
    # The full original reservation still counts against committed spend:
    # recovery can never lower the amount held for the unknown-cost attempt.
    summary = store.admission_summary("campaign-failed-billing-1")
    assert summary.open_reservation_micro_usd == 1000
    window_id = store.open_execution_window(
        "campaign-failed-billing-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    # Nonzero or malformed provider health keeps the gate even when the only
    # reservation is conservatively retained.
    with pytest.raises(CampaignGateError, match="zero workers"):
        store.close_execution_window(
            "campaign-failed-billing-1",
            window_id,
            health_evidence=_health_evidence(active=1, idle=1),
            reason="attempted teardown",
        )
    with pytest.raises(CampaignGateError, match="validated provider-health evidence"):
        store.close_execution_window(
            "campaign-failed-billing-1",
            window_id,
            health_evidence={"unexpected": "shape"},
            reason="attempted teardown",
        )
    assert store.current_gate()["active"] == 1
    # Verified provider-zero evidence closes the window: a terminal attempt
    # with an unknown cost is financially resolved once zero is proven.
    store.close_execution_window(
        "campaign-failed-billing-1",
        window_id,
        health_evidence=_health_evidence(),
        reason="verified teardown",
    )
    assert store.current_gate()["active"] == 0


def test_cancelled_unknown_start_retains_conservatively_and_zero_start_settles(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cancel-retain-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-cancel-retain-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-cancel-retain-1",
        "reservation-cancel-retain",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-cancel-retain-1",
        sample_id,
        "job-cancel-retain",
        reservation_id="reservation-cancel-retain",
    )
    # Cancelled with unknown start time: cost is unknown, the full original
    # reservation is conservatively retained and still counts in the budget.
    assert (
        store.record_terminal_execution(
            "campaign-cancel-retain-1",
            sample_id,
            status="cancelled",
            actual_gpu=None,
            execution_ms=None,
            hourly_rate_usd=None,
            unavailable_reason="operator_cancelled_before_start_confirmed",
        )
        is None
    )
    reservation = store.reservation_for_sample("campaign-cancel-retain-1", sample_id)
    assert reservation is not None and reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None
    assert store.admission_summary("campaign-cancel-retain-1").open_reservation_micro_usd == 1000

    # A proven-not-started cancellation is settled at zero, never retained.
    sample_two, _created = store.add_sample(
        "campaign-cancel-retain-1", cases[1], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-cancel-retain-1",
        "reservation-cancel-zero",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_two,
    )
    store.mark_sample_submitted(
        "campaign-cancel-retain-1",
        sample_two,
        "job-cancel-zero",
        reservation_id="reservation-cancel-zero",
    )
    store.record_terminal_execution(
        "campaign-cancel-retain-1",
        sample_two,
        status="cancelled",
        actual_gpu=None,
        execution_ms=0,
        hourly_rate_usd=None,
    )
    reservation_two = store.reservation_for_sample("campaign-cancel-retain-1", sample_two)
    assert reservation_two is not None and reservation_two["state"] == "settled"
    assert reservation_two["final_estimate_micro_usd"] == 0
    # Only the retained reservation still counts as open spend.
    assert store.admission_summary("campaign-cancel-retain-1").open_reservation_micro_usd == 1000


def test_conservatively_retained_reservation_still_counts_against_admission_guard(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-retain-budget-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-retain-budget-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-retain-budget-1",
        "reservation-retain-budget",
        kind="compute",
        reserved_micro_usd=CAMPAIGN_ADMISSION_STOP_MICRO_USD - 1,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-retain-budget-1",
        sample_id,
        "job-retain-budget",
        reservation_id="reservation-retain-budget",
    )
    store.record_terminal_execution(
        "campaign-retain-budget-1",
        sample_id,
        status="failed",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="runpod_generation_failed",
    )
    # Recovery can never lower committed spend: the retained amount still
    # counts against the admission stop exactly as the original reservation.
    with pytest.raises(CampaignBudgetError):
        store.reserve(
            "campaign-retain-budget-1",
            "reservation-retain-budget-2",
            kind="compatibility",
            reserved_micro_usd=2,
        )


def test_close_execution_window_still_blocked_by_unresolved_reservation(
    tmp_path: Path,
) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-unresolved-block-1", manifest, plan)
    store.reserve(
        "campaign-unresolved-block-1",
        "reservation-unresolved-block",
        kind="compatibility",
        reserved_micro_usd=1000,
    )
    # An unavailable/uncertain reservation (no terminal sample) remains a
    # teardown blocker even with perfect provider-zero evidence.
    store.settle_reservation(
        "campaign-unresolved-block-1",
        "reservation-unresolved-block",
        estimate_micro_usd=None,
        unavailable_reason="provider_evidence_missing",
    )
    window_id = store.open_execution_window(
        "campaign-unresolved-block-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    with pytest.raises(CampaignGateError, match="settled reservations"):
        store.close_execution_window(
            "campaign-unresolved-block-1",
            window_id,
            health_evidence=_health_evidence(),
            reason="attempted teardown",
        )
    assert store.current_gate()["active"] == 1


def test_pre_intent_reconcile_settles_exact_crash_boundary(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-preintent-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-preintent-1",
        "res-preintent-1",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    # Crash after reservation commit, before the submission-intent commit:
    # the sample is still planned with one open compute reservation, no
    # submitted timestamp, no intent, and no product-job link.
    reconciled = store.reconcile_pre_intent_samples("campaign-preintent-1")
    assert reconciled == [sample_id]
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "unsubmitted"
    assert sample["cost_status"] == "not_submitted"
    assert sample["estimated_compute_micro_usd"] is None
    assert sample["job_id"] is None
    reservation = store.reservation_for_sample("campaign-preintent-1", sample_id)
    assert reservation is not None
    assert reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 0
    assert reservation["submitted_at_utc"] is None
    assert reservation["reserved_micro_usd"] == 1000
    # The transition is idempotent and leaves one auditable recovery event.
    assert store.reconcile_pre_intent_samples("campaign-preintent-1") == []
    with sqlite3.connect(store.path) as connection:
        events = connection.execute(
            "SELECT event_type FROM campaign_events WHERE campaign_id=? "
            "AND event_type='pre_intent_reconciled'",
            ("campaign-preintent-1",),
        ).fetchall()
    assert len(events) == 1


def test_pre_intent_reconcile_refuses_submitted_timestamp(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-2", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-preintent-2", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-preintent-2",
        "res-preintent-2",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    # A submitted timestamp on the reservation contradicts the conclusion
    # that the attempt was never submitted; recovery must fail closed.
    store.mark_reservation_submitted("campaign-preintent-2", "res-preintent-2")
    with pytest.raises(CampaignGateError, match="contradicts a never-submitted"):
        store.reconcile_pre_intent_samples("campaign-preintent-2")
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "planned"
    reservation = store.reservation_for_sample("campaign-preintent-2", sample_id)
    assert reservation is not None and reservation["state"] == "open"


def test_pre_intent_reconcile_refuses_duplicate_or_noncompute_reservations(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-3", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-preintent-3", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-preintent-3",
        "res-preintent-3",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    # A second, non-compute reservation for the same planned sample makes the
    # pre-submission conclusion uncertain; nothing may be settled.
    store.reserve(
        "campaign-preintent-3",
        "res-preintent-3-extra",
        kind="compatibility",
        reserved_micro_usd=500,
        sample_id=sample_id,
    )
    with pytest.raises(CampaignGateError, match="contradicts a never-submitted"):
        store.reconcile_pre_intent_samples("campaign-preintent-3")
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "planned"


def test_pre_intent_reconcile_leaves_intent_present_state_to_crash_recovery(
    tmp_path: Path,
) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-4", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-preintent-4", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-preintent-4",
        "res-preintent-4",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.persist_submission_intent("campaign-preintent-4", sample_id, "job-preintent-4", "a" * 64)
    # The intent-present crash state belongs to the existing intent recovery;
    # the pre-intent transition refuses to touch it.
    assert store.reconcile_pre_intent_samples("campaign-preintent-4") == []
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "planned"
    reservation = store.reservation_for_sample("campaign-preintent-4", sample_id)
    assert reservation is not None and reservation["state"] == "open"


def test_pre_intent_reconcile_handles_two_samples_and_records_both(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-preintent-5", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-preintent-5", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
        store.reserve(
            "campaign-preintent-5",
            f"res-{sample_id}",
            kind="compute",
            reserved_micro_usd=1000,
            sample_id=sample_id,
        )
    reconciled = store.reconcile_pre_intent_samples("campaign-preintent-5")
    assert sorted(reconciled) == sorted(sample_ids)
    for sample_id in sample_ids:
        sample = store.sample(sample_id)
        assert sample is not None and sample["status"] == "unsubmitted"
        reservation = store.reservation_for_sample("campaign-preintent-5", sample_id)
        assert reservation is not None and reservation["state"] == "settled"
        assert reservation["final_estimate_micro_usd"] == 0


def test_cancelled_attempt_with_execution_time_is_never_billed(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-cancel-billing-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-cancel-billing-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-cancel-billing-1",
        "reservation-cancel-billing",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-cancel-billing-1",
        sample_id,
        "job-cancel-billing",
        reservation_id="reservation-cancel-billing",
    )
    assert (
        store.record_terminal_execution(
            "campaign-cancel-billing-1",
            sample_id,
            status="cancelled",
            actual_gpu="rtx-4090-24gb",
            execution_ms=22_499,
            hourly_rate_usd="0.49",
            unavailable_reason="operator_cancelled_after_start",
        )
        is None
    )
    sample = store.sample(sample_id)
    assert sample is not None
    assert sample["cost_status"] == "unavailable"
    assert sample["estimated_compute_micro_usd"] is None
    assert sample["cost_reason"] == "operator_cancelled_after_start"


def _fill_sheet_numeric(
    export: dict[str, object],
    *,
    candidate_ids: set[str],
    primary_candidate: int = 5,
    primary_incumbent: int = 4,
) -> dict[str, object]:
    """Fill a sheet with numeric vocal scores (valid for vocals-applicable cases)."""

    def dimensions(primary: int) -> dict[str, object]:
        return {
            "melody_retention": primary,
            "prompt_style_adherence": primary,
            "development": primary,
            "vocal_lyric_adherence": primary,
            "artifacts": 5,
            "ending_quality": primary,
        }

    sample_order = export["sample_order"]  # type: ignore[index]
    return {
        **export,
        "scores": [
            {
                "opaque_sample_id": sample_id,
                "dimensions": dimensions(
                    primary_candidate if sample_id in candidate_ids else primary_incumbent
                ),
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


def _finalize_screening_pair(store: CampaignStore, campaign_id: str) -> None:
    for listener in ("listener-a", "listener-b"):
        export = store.export_score_sheet(campaign_id, listener, stage="screening")
        store.import_score_sheet(
            campaign_id,
            listener,
            _fill_sheet(export, candidate_ids=set()),
            stage="screening",
        )
        store.finalize_score_sheet(campaign_id, listener, stage="screening")


def test_import_rejects_after_new_planned_sample_added(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-coverage-1", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-coverage-1", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    for sample_id in sample_ids:
        _complete_sample(store, "campaign-coverage-1", sample_id, f"{sample_id}/variation-01.mp3")
    export_a = store.export_score_sheet("campaign-coverage-1", "listener-a")
    export_b = store.export_score_sheet("campaign-coverage-1", "listener-b")
    # A new planned scoreable sample after export makes the frozen sheet stale.
    late, _created = store.add_sample(
        "campaign-coverage-1",
        _confirmation_case(
            "cover-candidate-late",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    assert late
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet(
            "campaign-coverage-1", "listener-a", _fill_sheet(export_a, candidate_ids=set())
        )
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet(
            "campaign-coverage-1", "listener-b", _fill_sheet(export_b, candidate_ids=set())
        )


def test_import_rejects_after_late_sample_completes(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-coverage-2", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-coverage-2", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    for sample_id in sample_ids:
        _complete_sample(store, "campaign-coverage-2", sample_id, f"{sample_id}/variation-01.mp3")
    export_a = store.export_score_sheet("campaign-coverage-2", "listener-a")
    export_b = store.export_score_sheet("campaign-coverage-2", "listener-b")
    store.import_score_sheet(
        "campaign-coverage-2", "listener-a", _fill_sheet(export_a, candidate_ids=set())
    )
    store.import_score_sheet(
        "campaign-coverage-2", "listener-b", _fill_sheet(export_b, candidate_ids=set())
    )
    late, _created = store.add_sample(
        "campaign-coverage-2",
        _confirmation_case(
            "cover-candidate-late",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    # Completing the late sample does not repair the stale export: the current
    # scoreable set now contains a sample the frozen sheet omits.
    _complete_sample(store, "campaign-coverage-2", late, f"{late}/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet(
            "campaign-coverage-2", "listener-a", _fill_sheet(export_a, candidate_ids=set())
        )
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.finalize_score_sheet("campaign-coverage-2", "listener-a")
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet(
            "campaign-coverage-2", "listener-b", _fill_sheet(export_b, candidate_ids=set())
        )


def test_import_rejects_with_failed_or_uncertain_sample(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-coverage-3", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-coverage-3", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    for sample_id in sample_ids:
        _complete_sample(store, "campaign-coverage-3", sample_id, f"{sample_id}/variation-01.mp3")
    export_a = store.export_score_sheet("campaign-coverage-3", "listener-a")
    late, _created = store.add_sample(
        "campaign-coverage-3",
        _confirmation_case(
            "cover-candidate-late",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    store.reserve(
        "campaign-coverage-3", "res-late", kind="compute", reserved_micro_usd=1000, sample_id=late
    )
    store.mark_sample_submitted("campaign-coverage-3", late, "job-late", reservation_id="res-late")
    store.record_terminal_execution(
        "campaign-coverage-3",
        late,
        status="failed",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="injected_failure",
    )
    # A failed scoreable sample is still part of the current scoreable set, so
    # the frozen export is stale and import must reject it.
    with pytest.raises(CampaignGateError, match="no longer covers"):
        store.import_score_sheet(
            "campaign-coverage-3", "listener-a", _fill_sheet(export_a, candidate_ids=set())
        )


def test_require_frozen_coverage_rejects_pair_structure_mismatch(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-coverage-4", manifest, plan)
    sample_ids: list[str] = []
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-coverage-4", case, fixture_id=manifest.fixture_id
        )
        sample_ids.append(sample_id)
    second, _created = store.add_sample(
        "campaign-coverage-4",
        _confirmation_case(
            "cover-candidate-second",
            "candidate",
            1729,
            stage="cover-screen",
            resolved={"seed": 1729, "cover_noise_strength": 0.30},
        ),
        fixture_id=manifest.fixture_id,
    )
    with store.read() as connection:
        frozen = {
            "sample_order": list(sample_ids) + [second],
            "pairs": [
                {"pair_id": "pair-a", "left": sample_ids[0], "right": sample_ids[1]},
                {"pair_id": "pair-b", "left": sample_ids[1], "right": sample_ids[1]},
            ],
        }
        with pytest.raises(CampaignGateError, match="pair structure"):
            store._require_frozen_coverage(connection, "campaign-coverage-4", "screening", frozen)
        # Missing sample coverage is also rejected.
        frozen_missing = {
            "sample_order": [sample_ids[0]],
            "pairs": [{"pair_id": "pair-a", "left": sample_ids[0], "right": sample_ids[1]}],
        }
        with pytest.raises(CampaignGateError, match="no longer covers"):
            store._require_frozen_coverage(
                connection, "campaign-coverage-4", "screening", frozen_missing
            )


def test_screening_seed_reuse_creates_no_duplicate_job(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-reuse-jobs-1", manifest, plan)
    screening_ids: dict[str, str] = {}
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-reuse-jobs-1", case, fixture_id=manifest.fixture_id
        )
        screening_ids[case.declared_case_id] = sample_id
    for sample_id in screening_ids.values():
        _complete_sample(store, "campaign-reuse-jobs-1", sample_id, f"{sample_id}/variation-01.mp3")
    confirmation = build_confirmation_cases(manifest, plan, ("cover-candidate",))
    for case in confirmation:
        sample_id, created = store.add_sample(
            "campaign-reuse-jobs-1", case, fixture_id=manifest.fixture_id
        )
        if case.seed == 1729:
            assert created is False
            expected = (
                screening_ids["cover-candidate"]
                if case.declared_case_id == "cover-candidate-confirmation-1729"
                else screening_ids["cover-incumbent"]
            )
            assert sample_id == expected
    samples = store.list_samples("campaign-reuse-jobs-1", include_aliases=True)
    # Seed-one confirmation cases are aliases of the two screening rows and
    # carry no second job or reservation; the other two seeds are new payable
    # rows (candidate + incumbent per seed).
    screening_rows = [item for item in samples if item["stage"] == "cover-screen"]
    assert len(screening_rows) == 2
    assert all(item["job_id"] is not None for item in screening_rows)
    assert len({item["job_id"] for item in screening_rows}) == 2
    assert len([item for item in samples if item["status"] == "planned"]) == 4
    for sample_id in screening_ids.values():
        reservation = store.reservation_for_sample("campaign-reuse-jobs-1", sample_id)
        assert reservation is not None
    assert (
        len(
            [
                item
                for item in samples
                if any(alias.endswith("-confirmation-1729") for alias in item["aliases"])
            ]
        )
        == 2
    )


def test_unblinding_is_orientation_independent_across_listeners(tmp_path: Path) -> None:
    import random

    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-unblind-1", manifest, plan)
    screening_ids: dict[str, str] = {}
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-unblind-1", case, fixture_id=manifest.fixture_id
        )
        screening_ids[case.declared_case_id] = sample_id
    for sample_id in screening_ids.values():
        _complete_sample(store, "campaign-unblind-1", sample_id, f"{sample_id}/variation-01.mp3")
    confirmation = build_confirmation_cases(manifest, plan, ("cover-candidate",))
    confirmation_ids: dict[str, tuple[str, bool]] = {}
    for case in confirmation:
        sample_id, created = store.add_sample(
            "campaign-unblind-1", case, fixture_id=manifest.fixture_id
        )
        confirmation_ids[case.declared_case_id] = (sample_id, created)
    for _declared, (sample_id, created) in confirmation_ids.items():
        if created:
            _complete_sample(
                store, "campaign-unblind-1", sample_id, f"{sample_id}/variation-01.mp3"
            )
    candidate_ids = {screening_ids["cover-candidate"]}
    for declared, (sample_id, _created) in confirmation_ids.items():
        if declared.startswith("cover-candidate-confirmation-"):
            candidate_ids.add(sample_id)
    screening_exports: dict[str, dict[str, object]] = {}
    for stage in ("screening", "confirmation"):
        for listener, randomizer in (
            ("listener-a", random.Random(1)),
            ("listener-b", random.Random(5)),
        ):
            export = store.export_score_sheet(
                "campaign-unblind-1",
                listener,
                stage=stage,
                random_source=randomizer,
            )
            if stage == "screening":
                screening_exports[listener] = export
            store.import_score_sheet(
                "campaign-unblind-1",
                listener,
                _fill_sheet(export, candidate_ids=candidate_ids),
                stage=stage,
            )
            store.finalize_score_sheet("campaign-unblind-1", listener, stage=stage)
    # The two listeners received independently randomized left/right pair
    # orientations; both nevertheless identify the same candidate, so the
    # deterministic unblinding maps each sheet-local side to the same A/B
    # identities and yields identical decisions.
    pair_a = screening_exports["listener-a"]["pairs"][0]  # type: ignore[index]
    pair_b = screening_exports["listener-b"]["pairs"][0]  # type: ignore[index]
    assert isinstance(pair_a, dict) and isinstance(pair_b, dict)
    assert pair_a["left"] != pair_b["left"] or pair_a["right"] != pair_b["right"]
    decision = store.record_quality_decision("campaign-unblind-1", manifest)
    cover = decision["decision"]["task_decisions"][0]
    assert cover["candidate_id"] == "cover-candidate"
    assert cover["listener_preferences"] == [3, 3]
    assert cover["passed"] is True
    again = store.record_quality_decision("campaign-unblind-1", manifest)
    assert again["decision_id"] == decision["decision_id"]


def test_advancement_requires_both_finalized_sheets(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-adv-gate-1", manifest, plan)
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-adv-gate-1", case, fixture_id=manifest.fixture_id
        )
        _complete_sample(store, "campaign-adv-gate-1", sample_id, f"{sample_id}/variation-01.mp3")
    with pytest.raises(CampaignGateError, match="not finalized"):
        store.advance_screening_to_confirmation("campaign-adv-gate-1", manifest, plan)
    export = store.export_score_sheet("campaign-adv-gate-1", "listener-a")
    store.import_score_sheet(
        "campaign-adv-gate-1", "listener-a", _fill_sheet(export, candidate_ids=set())
    )
    store.finalize_score_sheet("campaign-adv-gate-1", "listener-a")
    with pytest.raises(CampaignGateError, match="not finalized"):
        store.advance_screening_to_confirmation("campaign-adv-gate-1", manifest, plan)


def test_advancement_from_two_finalized_screening_sheets(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-adv-1", manifest, plan)
    screening_ids: dict[str, str] = {}
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-adv-1", case, fixture_id=manifest.fixture_id
        )
        screening_ids[case.declared_case_id] = sample_id
    for sample_id in screening_ids.values():
        _complete_sample(store, "campaign-adv-1", sample_id, f"{sample_id}/variation-01.mp3")
    _finalize_screening_pair(store, "campaign-adv-1")
    result = store.advance_screening_to_confirmation("campaign-adv-1", manifest, plan)
    assert result["recorded"] is True
    assert result["finalists"] == {"cover": ["cover-candidate"]}
    samples = store.list_samples("campaign-adv-1", include_aliases=True)
    reused = [
        item
        for item in samples
        if any(alias.endswith("-confirmation-1729") for alias in item["aliases"])
    ]
    assert len(reused) == 2
    assert all(item["status"] == "completed" for item in reused)
    planned = [item for item in samples if item["status"] == "planned"]
    assert len(planned) == 4
    assert all(item["stage"] == "cover-confirmation" for item in planned)
    assert {item["seed"] for item in planned} == {2718, 3141}
    campaign = store.get_campaign("campaign-adv-1")
    assert campaign is not None and campaign["status"] == "awaiting_confirmation"


def test_three_way_first_tie_advances_none(tmp_path: Path) -> None:
    manifest = _manifest()
    from ace_service.campaign import build_campaign_plan

    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-adv-tie-1", manifest, plan)
    declared: dict[str, str] = {}
    for case in plan.cases:
        if case.conditional_on is not None or case.stage not in {
            "cover-screen",
            "original-screen",
        }:
            continue
        sample_id, _created = store.add_sample(
            "campaign-adv-tie-1", case, fixture_id=manifest.fixture_id
        )
        declared[case.declared_case_id] = sample_id
    for sample_id in sorted(set(declared.values())):
        _complete_sample(store, "campaign-adv-tie-1", sample_id, f"{sample_id}/variation-01.mp3")
    # Three cover candidates tie at the top; the frozen rule excludes the
    # entire tied group because it spans the two-finalist cutoff, so no cover
    # candidate advances and no confirmation pair is materialized.
    tied = {
        "cover-grid-a0.35-n0.10",
        "cover-grid-a0.35-n0.30",
        "cover-grid-a0.65-n0.10",
    }
    tied_ids = {declared[name] for name in tied}
    for listener in ("listener-a", "listener-b"):
        export = store.export_score_sheet("campaign-adv-tie-1", listener, stage="screening")
        store.import_score_sheet(
            "campaign-adv-tie-1",
            listener,
            _fill_sheet_numeric(
                export,
                candidate_ids=tied_ids,
                primary_candidate=5,
                primary_incumbent=4,
            ),
            stage="screening",
        )
        store.finalize_score_sheet("campaign-adv-tie-1", listener, stage="screening")
    result = store.advance_screening_to_confirmation("campaign-adv-tie-1", manifest, plan)
    assert result["finalists"]["cover"] == []
    assert all(
        item["stage"] != "cover-confirmation" or item["status"] != "planned"
        for item in store.list_samples("campaign-adv-tie-1")
    )


def test_two_way_first_tie_advances_both(tmp_path: Path) -> None:
    manifest = _manifest()
    from ace_service.campaign import build_campaign_plan

    plan = build_campaign_plan(manifest)
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-adv-tie-2", manifest, plan)
    declared: dict[str, str] = {}
    for case in plan.cases:
        if case.conditional_on is not None or case.stage not in {
            "cover-screen",
            "original-screen",
        }:
            continue
        sample_id, _created = store.add_sample(
            "campaign-adv-tie-2", case, fixture_id=manifest.fixture_id
        )
        declared[case.declared_case_id] = sample_id
    for sample_id in sorted(set(declared.values())):
        _complete_sample(store, "campaign-adv-tie-2", sample_id, f"{sample_id}/variation-01.mp3")
    # An exact two-way tie for first is entirely inside the cutoff and both
    # candidates advance.
    tied = {"cover-grid-a0.35-n0.10", "cover-grid-a0.35-n0.30"}
    tied_ids = {declared[name] for name in tied}
    for listener in ("listener-a", "listener-b"):
        export = store.export_score_sheet("campaign-adv-tie-2", listener, stage="screening")
        store.import_score_sheet(
            "campaign-adv-tie-2",
            listener,
            _fill_sheet_numeric(
                export,
                candidate_ids=tied_ids,
                primary_candidate=5,
                primary_incumbent=4,
            ),
            stage="screening",
        )
        store.finalize_score_sheet("campaign-adv-tie-2", listener, stage="screening")
    result = store.advance_screening_to_confirmation("campaign-adv-tie-2", manifest, plan)
    assert sorted(result["finalists"]["cover"]) == sorted(tied)


def test_rank_screening_candidates_frozen_tie_rule() -> None:
    from ace_service.campaign import rank_screening_candidates

    def scores(primary: int) -> dict[str, object]:
        return {
            "dimensions": {
                "melody_retention": primary,
                "prompt_style_adherence": primary,
                "development": primary,
                "vocal_lyric_adherence": primary,
                "artifacts": 5,
                "ending_quality": primary,
            }
        }

    incumbent = [scores(4), scores(4)]
    rank = lambda candidates: rank_screening_candidates(  # noqa: E731
        task_type="cover",
        incumbent_scores=incumbent,
        candidates=candidates,
        vocals_applicable=True,
    )
    # Two distinct top scores advance both.
    distinct = rank(
        {
            "cand-a": [scores(5), scores(5)],
            "cand-b": [scores(5), scores(4)],
            "cand-c": [scores(4), scores(4)],
        }
    )
    assert [item.candidate_id for item in distinct] == ["cand-a", "cand-b"]
    # An exact two-way first tie advances both.
    two_way = rank(
        {
            "cand-a": [scores(5), scores(5)],
            "cand-b": [scores(5), scores(5)],
            "cand-c": [scores(4), scores(4)],
        }
    )
    assert {item.candidate_id for item in two_way} == {"cand-a", "cand-b"}
    # A three-way first tie spans the cutoff: no member of the tied group
    # advances.
    three_way = rank(
        {
            "cand-a": [scores(5), scores(5)],
            "cand-b": [scores(5), scores(5)],
            "cand-c": [scores(5), scores(5)],
            "cand-d": [scores(4), scores(4)],
        }
    )
    assert three_way == ()
    # A tie spanning positions two and three advances only the untied first.
    second_third = rank(
        {
            "cand-a": [scores(5), scores(5)],
            "cand-b": [scores(5), scores(4)],
            "cand-c": [scores(5), scores(4)],
            "cand-d": [scores(4), scores(4)],
        }
    )
    assert [item.candidate_id for item in second_third] == ["cand-a"]
    # A tied group fully inside the cutoff is selected in its entirety.
    inside = rank_screening_candidates(
        task_type="cover",
        incumbent_scores=incumbent,
        candidates={
            "cand-a": [scores(5), scores(5)],
            "cand-b": [scores(5), scores(4)],
            "cand-c": [scores(5), scores(4)],
            "cand-d": [scores(4), scores(4)],
        },
        vocals_applicable=True,
        maximum_finalists=3,
    )
    assert {item.candidate_id for item in inside} == {"cand-a", "cand-b", "cand-c"}


def test_repeated_advancement_idempotent_and_conflict_rejected(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-adv-repeat-1", manifest, plan)
    for case in cases:
        sample_id, _created = store.add_sample(
            "campaign-adv-repeat-1", case, fixture_id=manifest.fixture_id
        )
        _complete_sample(store, "campaign-adv-repeat-1", sample_id, f"{sample_id}/variation-01.mp3")
    _finalize_screening_pair(store, "campaign-adv-repeat-1")
    first = store.advance_screening_to_confirmation("campaign-adv-repeat-1", manifest, plan)
    assert first["recorded"] is True
    again = store.advance_screening_to_confirmation("campaign-adv-repeat-1", manifest, plan)
    assert again["recorded"] is False
    assert again["finalists"] == first["finalists"]
    # A conflicting frozen advancement record fails closed.
    conflict_payload = (
        '{"advancement": "screening_to_confirmation", '
        '"finalists": {"cover": ["cover-incumbent"]}, "rankings": {}}'
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO campaign_events(campaign_id, event_type, event_json, created_at_utc) "
            "VALUES (?, 'screening_advanced', ?, ?)",
            (
                "campaign-adv-repeat-1",
                conflict_payload,
                utc_now().isoformat(),
            ),
        )
    with pytest.raises(CampaignGateError, match="conflicts with the frozen"):
        store.advance_screening_to_confirmation("campaign-adv-repeat-1", manifest, plan)


def test_submission_intent_store_lifecycle_and_gate(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-intent-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-intent-1", cases[0], fixture_id=manifest.fixture_id
    )
    fingerprint = "a" * 64
    store.persist_submission_intent(
        "campaign-intent-1",
        sample_id,
        "11111111-1111-4111-8111-111111111111",
        fingerprint,
        reservation_id="res-intent-1",
        source_url="https://example.org/fixture-source",
    )
    intent = store.get_submission_intent("campaign-intent-1", sample_id)
    assert intent is not None
    assert intent["product_job_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert intent["status"] == "pending"
    assert intent["request_fingerprint"] == fingerprint
    assert store.list_submission_intents("campaign-intent-1") == [intent]
    with pytest.raises(CampaignGateError, match="ordinary submissions"):
        store.require_ordinary_submissions("campaign-intent-1")
    # Identical persistence is idempotent; a different fingerprint conflicts.
    store.persist_submission_intent(
        "campaign-intent-1",
        sample_id,
        "11111111-1111-4111-8111-111111111111",
        fingerprint,
    )
    with pytest.raises(CampaignGateError, match="conflicts with the frozen"):
        store.persist_submission_intent(
            "campaign-intent-1",
            sample_id,
            "11111111-1111-4111-8111-111111111111",
            "b" * 64,
        )
    # A second intent may never reuse the same product UUID.
    other_sample, _created = store.add_sample(
        "campaign-intent-1", cases[1], fixture_id=manifest.fixture_id
    )
    with pytest.raises(CampaignGateError, match="already reserved"):
        store.persist_submission_intent(
            "campaign-intent-1",
            other_sample,
            "11111111-1111-4111-8111-111111111111",
            fingerprint,
        )
    store.mark_intent_submitted(
        "campaign-intent-1", sample_id, "11111111-1111-4111-8111-111111111111"
    )
    assert store.get_submission_intent("campaign-intent-1", sample_id)["status"] == "submitted"
    # Marking an already-submitted intent is idempotent; an unknown one fails.
    store.mark_intent_submitted(
        "campaign-intent-1", sample_id, "11111111-1111-4111-8111-111111111111"
    )
    with pytest.raises(CampaignGateError, match="not pending"):
        store.mark_intent_submitted(
            "campaign-intent-1", sample_id, "22222222-2222-4222-8222-222222222222"
        )
    store.require_ordinary_submissions("campaign-intent-1")


def test_close_execution_window_requires_validated_health_evidence(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-health-1", manifest, plan)
    window_id = store.open_execution_window(
        "campaign-health-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    close = lambda **kwargs: store.close_execution_window(  # noqa: E731
        "campaign-health-1", window_id, health_evidence=kwargs, reason="teardown"
    )
    for bad in (
        _health_evidence(endpoint_id=""),
        _health_evidence(endpoint_id="x" * 257),
        _health_evidence(active=1),
        _health_evidence(idle=1),
        _health_evidence(running=1),
        _health_evidence(queued=1),
        _health_evidence(in_progress=1),
        _health_evidence(active=0, idle=1, running=1),  # internally inconsistent
    ):
        with pytest.raises(CampaignGateError):
            close(**bad)
    with pytest.raises(CampaignGateError):
        close(**{k: v for k, v in _health_evidence().items() if k != "captured_at_utc"})
    with pytest.raises(CampaignGateError):
        close(**_health_evidence(captured_at_utc="not-a-timestamp"))
    with pytest.raises(CampaignGateError):
        close(**_health_evidence(active="0"))
    with pytest.raises(CampaignGateError):
        close(**_health_evidence(idle=-1))
    with pytest.raises(CampaignGateError):
        close(**_health_evidence(queued=True))
    assert store.current_gate()["active"] == 1
    close(**_health_evidence())
    assert store.current_gate()["active"] == 0
    with sqlite3.connect(store.path) as connection:
        evidence = connection.execute(
            "SELECT health_evidence_json FROM execution_windows WHERE window_id=?",
            (window_id,),
        ).fetchone()[0]
    assert "runpod_health" in evidence
    assert '"active_workers":0' in evidence


def test_health_evidence_must_match_authorized_endpoint(tmp_path: Path) -> None:
    from ace_service.campaign import RemoteChangeAuthorization

    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-health-2", manifest, plan)
    store.record_authorization(
        "campaign-health-2",
        RemoteChangeAuthorization(
            application_commit="a" * 40,
            worker_digest="sha256:" + "b" * 64,
            endpoint_id="endpoint-authorized",
            template_id="template-1",
            rollback_target="release-before-campaign",
            evaluation_models=("acestep-v15-xl-turbo",),
            ceiling_micro_usd=5_000_000,
            authorized_at=utc_now(),
            blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
            edge_config_sha256="c" * 64,
            edge_guard_verified=True,
        ),
    )
    window_id = store.open_execution_window(
        "campaign-health-2",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    with pytest.raises(CampaignGateError, match="different authorized endpoint"):
        store.close_execution_window(
            "campaign-health-2",
            window_id,
            health_evidence=_health_evidence(endpoint_id="endpoint-other"),
            reason="teardown",
        )
    store.close_execution_window(
        "campaign-health-2",
        window_id,
        health_evidence=_health_evidence(endpoint_id="endpoint-authorized"),
        reason="teardown",
    )
    assert store.current_gate()["active"] == 0


def test_campaign_status_is_bounded_and_read_only(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-status-1", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-status-1", cases[0], fixture_id=manifest.fixture_id
    )
    store.persist_submission_intent("campaign-status-1", sample_id, "job-status-1", "a" * 64)
    store.mark_intent_submitted("campaign-status-1", sample_id, "job-status-1")
    store.reserve(
        "campaign-status-1",
        "res-status-1",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(
        "campaign-status-1", sample_id, "job-status-1", reservation_id="res-status-1"
    )
    store.open_execution_window(
        "campaign-status-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    status = store.campaign_status("campaign-status-1")
    assert status["campaign_id"] == "campaign-status-1"
    assert status["gate"]["active"] is True
    assert status["window"]["stage"] == "screening"
    assert status["samples"][0]["sample_id"] == sample_id
    assert status["samples"][0]["product_job_id"] == "job-status-1"
    assert status["reservations"] == {"open": 1}
    assert status["zero_worker_evidence"] is False
    assert status["product_linkage_complete"] is True
    # The status payload never carries prompts, lyrics, URLs, or credentials.
    encoded = json.dumps(status)
    assert "caption" not in encoded and "lyrics" not in encoded
    assert "https://" not in encoded and "secret" not in encoded
    # Read-only: the campaign and its window are untouched.
    campaign = store.get_campaign("campaign-status-1")
    assert campaign is not None and campaign["status"] == "running"
    assert store.current_gate()["active"] == 1


def test_v1_campaign_schema_migrates_to_v3(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-migrate-1", manifest, plan)
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE execution_windows DROP COLUMN health_evidence_json")
        connection.execute("DROP TABLE submission_intents")
        connection.execute("PRAGMA user_version=1")
    reopened = CampaignStore.open_existing(tmp_path / "campaign.sqlite3")
    assert reopened is not None
    with sqlite3.connect(store.path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if row[0] != "sqlite_sequence"
        }
        assert "submission_intents" in tables
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert reopened.get_campaign("campaign-migrate-1") is not None


def _reserve_and_submit(
    store: CampaignStore,
    campaign_id: str,
    case: CampaignCase,
    manifest: FixtureManifest,
    *,
    reservation_id: str,
    job_id: str,
    reserved_micro_usd: int = 1000,
) -> str:
    sample_id, _created = store.add_sample(campaign_id, case, fixture_id=manifest.fixture_id)
    store.reserve(
        campaign_id,
        reservation_id,
        kind="compute",
        reserved_micro_usd=reserved_micro_usd,
        sample_id=sample_id,
    )
    store.mark_sample_submitted(campaign_id, sample_id, job_id, reservation_id=reservation_id)
    return sample_id


def _inject_reservation_state(path: Path, *, state: str) -> None:
    """Corrupt a v3 database after open, bypassing the schema CHECK."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE reservations SET state=?", (state,))


def _rebuild_reservations_without_state_check(
    path: Path, *, corrupt_state: str | None = None, user_version: int = 2
) -> None:
    """Rewrite reservations to the genuine v1/v2 shape (no state CHECK)."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "CREATE TABLE reservations_v2 ("
            "reservation_id TEXT PRIMARY KEY, "
            "campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id), "
            "sample_id TEXT, "
            "kind TEXT NOT NULL, "
            "reserved_micro_usd INTEGER NOT NULL, "
            "state TEXT NOT NULL, "
            "final_estimate_micro_usd INTEGER, "
            "unavailable_reason TEXT, "
            "created_at_utc TEXT NOT NULL, "
            "submitted_at_utc TEXT, "
            "settled_at_utc TEXT, "
            "UNIQUE(campaign_id, sample_id, kind), "
            "CHECK(reserved_micro_usd >= 0))"
        )
        connection.execute(
            "INSERT INTO reservations_v2(reservation_id, campaign_id, sample_id, kind, "
            "reserved_micro_usd, state, final_estimate_micro_usd, unavailable_reason, "
            "created_at_utc, submitted_at_utc, settled_at_utc) "
            "SELECT reservation_id, campaign_id, sample_id, kind, reserved_micro_usd, state, "
            "final_estimate_micro_usd, unavailable_reason, created_at_utc, submitted_at_utc, "
            "settled_at_utc FROM reservations"
        )
        connection.execute("DROP TABLE reservations")
        connection.execute("ALTER TABLE reservations_v2 RENAME TO reservations")
        if corrupt_state is not None:
            connection.execute("UPDATE reservations SET state=?", (corrupt_state,))
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA user_version={user_version}")


def _reservation_rows(path: Path, campaign_id: str) -> dict[str, dict[str, object]]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM reservations WHERE campaign_id=? ORDER BY reservation_id",
            (campaign_id,),
        ).fetchall()
    return {str(row["reservation_id"]): dict(row) for row in rows}


def _four_state_fixture(tmp_path: Path) -> tuple[Path, str, dict[str, dict[str, object]]]:
    """One campaign with reservations in all four declared states."""
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-states-1", manifest, plan)
    store.reserve(
        "campaign-states-1",
        "res-open",
        kind="compatibility",
        reserved_micro_usd=1000,
    )
    store.reserve(
        "campaign-states-1",
        "res-unresolved",
        kind="storage",
        reserved_micro_usd=2000,
    )
    store.settle_reservation(
        "campaign-states-1",
        "res-unresolved",
        estimate_micro_usd=None,
        unavailable_reason="provider_evidence_missing",
    )
    failed_sample = _reserve_and_submit(
        store,
        "campaign-states-1",
        cases[0],
        manifest,
        reservation_id="res-retained",
        job_id="job-retained",
        reserved_micro_usd=3000,
    )
    store.record_terminal_execution(
        "campaign-states-1",
        failed_sample,
        status="failed",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="runpod_generation_failed",
    )
    settled_sample = _reserve_and_submit(
        store,
        "campaign-states-1",
        cases[1],
        manifest,
        reservation_id="res-settled",
        job_id="job-settled",
        reserved_micro_usd=4000,
    )
    store.record_terminal_execution(
        "campaign-states-1",
        settled_sample,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-settled/variation-01.mp3",
    )
    database = tmp_path / "campaign.sqlite3"
    expected = _reservation_rows(database, "campaign-states-1")
    assert {item["state"] for item in expected.values()} == {
        "open",
        "unresolved",
        "conservatively_retained",
        "settled",
    }
    return database, "campaign-states-1", expected


def test_uncertain_sample_stays_unresolved(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-uncertain-1", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-uncertain-1",
        cases[0],
        manifest,
        reservation_id="res-uncertain-1",
        job_id="job-uncertain-1",
    )
    assert (
        store.record_terminal_execution(
            "campaign-uncertain-1",
            sample_id,
            status="uncertain",
            actual_gpu=None,
            execution_ms=None,
            hourly_rate_usd=None,
            unavailable_reason="terminal_evidence_timeout",
        )
        is None
    )
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "uncertain"
    reservation = store.reservation_for_sample("campaign-uncertain-1", sample_id)
    assert reservation is not None
    assert reservation["state"] == "unresolved"
    assert reservation["final_estimate_micro_usd"] is None
    assert reservation["settled_at_utc"] is None
    assert reservation["reserved_micro_usd"] == 1000
    # The full immutable reservation still counts in admission totals and
    # appears as unresolved in bounded status.
    summary = store.admission_summary("campaign-uncertain-1")
    assert summary.open_reservation_micro_usd == 1000
    status = store.campaign_status("campaign-uncertain-1")
    assert status["reservations"] == {"unresolved": 1}
    # In-flight work blocks rollback and teardown even with provider zero.
    assert store.rollback_readiness().safe is False
    window_id = store.open_execution_window(
        "campaign-uncertain-1",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    with pytest.raises(CampaignGateError, match="settled reservations"):
        store.close_execution_window(
            "campaign-uncertain-1",
            window_id,
            health_evidence=_health_evidence(),
            reason="attempted teardown",
        )
    assert store.current_gate()["active"] == 1
    # An exact repeat is idempotent and stays unresolved.
    assert (
        store.record_terminal_execution(
            "campaign-uncertain-1",
            sample_id,
            status="uncertain",
            actual_gpu=None,
            execution_ms=None,
            hourly_rate_usd=None,
            unavailable_reason="terminal_evidence_timeout",
        )
        is None
    )
    reservation = store.reservation_for_sample("campaign-uncertain-1", sample_id)
    assert reservation is not None and reservation["state"] == "unresolved"
    assert reservation["settled_at_utc"] is None


def test_uncertain_to_completed_transition(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-uncertain-2", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-uncertain-2",
        cases[0],
        manifest,
        reservation_id="res-uncertain-2",
        job_id="job-uncertain-2",
    )
    store.record_terminal_execution(
        "campaign-uncertain-2",
        sample_id,
        status="uncertain",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="terminal_evidence_timeout",
    )
    estimate = store.record_terminal_execution(
        "campaign-uncertain-2",
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-uncertain-2/variation-01.mp3",
    )
    assert estimate == 14
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "completed"
    assert sample["cost_status"] == "estimated_compute"
    reservation = store.reservation_for_sample("campaign-uncertain-2", sample_id)
    assert reservation is not None
    assert reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 14
    # The completed transition is idempotent.
    assert (
        store.record_terminal_execution(
            "campaign-uncertain-2",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-uncertain-2/variation-01.mp3",
        )
        == 14
    )


def test_uncertain_to_failed_transition(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-uncertain-3", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-uncertain-3",
        cases[0],
        manifest,
        reservation_id="res-uncertain-3",
        job_id="job-uncertain-3",
    )
    store.record_terminal_execution(
        "campaign-uncertain-3",
        sample_id,
        status="uncertain",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="terminal_evidence_timeout",
    )
    assert (
        store.record_terminal_execution(
            "campaign-uncertain-3",
            sample_id,
            status="failed",
            actual_gpu=None,
            execution_ms=None,
            hourly_rate_usd=None,
            unavailable_reason="runpod_generation_failed",
        )
        is None
    )
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "failed"
    reservation = store.reservation_for_sample("campaign-uncertain-3", sample_id)
    assert reservation is not None
    assert reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None
    assert reservation["reserved_micro_usd"] == 1000


def test_completed_unavailable_to_authoritative(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-complete-fill-1", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-complete-fill-1",
        cases[0],
        manifest,
        reservation_id="res-complete-fill-1",
        job_id="job-complete-fill-1",
    )
    # Completed output with missing cost inputs: terminal but unknown cost.
    assert (
        store.record_terminal_execution(
            "campaign-complete-fill-1",
            sample_id,
            status="completed",
            actual_gpu=None,
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-complete-fill-1/variation-01.mp3",
        )
        is None
    )
    reservation = store.reservation_for_sample("campaign-complete-fill-1", sample_id)
    assert reservation is not None and reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None
    # The missing authoritative GPU/rate evidence may be filled in while the
    # completed identity and prior output stay intact.
    estimate = store.record_terminal_execution(
        "campaign-complete-fill-1",
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-complete-fill-1/variation-01.mp3",
    )
    assert estimate == 14
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "completed"
    assert sample["output_path"] == "job-complete-fill-1/variation-01.mp3"
    reservation = store.reservation_for_sample("campaign-complete-fill-1", sample_id)
    assert reservation is not None
    assert reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 14
    assert reservation["reserved_micro_usd"] == 1000
    assert (
        store.record_terminal_execution(
            "campaign-complete-fill-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-complete-fill-1/variation-01.mp3",
        )
        == 14
    )


def test_failed_terminal_identity_immutable(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-immutable-1", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-immutable-1",
        cases[0],
        manifest,
        reservation_id="res-immutable-1",
        job_id="job-immutable-1",
    )
    store.record_terminal_execution(
        "campaign-immutable-1",
        sample_id,
        status="failed",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="runpod_generation_failed",
    )
    # The reviewed failed-retained-to-completed rewrite probe: later completed
    # evidence must be rejected, never billed as invented compute.
    with pytest.raises(CampaignGateError, match="terminal sample identity is immutable"):
        store.record_terminal_execution(
            "campaign-immutable-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-immutable-1/variation-01.mp3",
        )
    sample = store.sample(sample_id)
    assert sample is not None and sample["status"] == "failed"
    reservation = store.reservation_for_sample("campaign-immutable-1", sample_id)
    assert reservation is not None
    assert reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None
    assert reservation["reserved_micro_usd"] == 1000


def test_cancelled_terminal_identity_immutable(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-immutable-2", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-immutable-2",
        cases[0],
        manifest,
        reservation_id="res-immutable-2",
        job_id="job-immutable-2",
    )
    store.record_terminal_execution(
        "campaign-immutable-2",
        sample_id,
        status="cancelled",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
        unavailable_reason="operator_cancelled_before_start_confirmed",
    )
    with pytest.raises(CampaignGateError, match="terminal sample identity is immutable"):
        store.record_terminal_execution(
            "campaign-immutable-2",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-immutable-2/variation-01.mp3",
        )
    reservation = store.reservation_for_sample("campaign-immutable-2", sample_id)
    assert reservation is not None
    assert reservation["state"] == "conservatively_retained"
    assert reservation["final_estimate_micro_usd"] is None


def test_unsubmitted_terminal_identity_immutable(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-immutable-3", manifest, plan)
    sample_id, _created = store.add_sample(
        "campaign-immutable-3", cases[0], fixture_id=manifest.fixture_id
    )
    store.reserve(
        "campaign-immutable-3",
        "res-immutable-3",
        kind="compute",
        reserved_micro_usd=1000,
        sample_id=sample_id,
    )
    store.record_terminal_execution(
        "campaign-immutable-3",
        sample_id,
        status="unsubmitted",
        actual_gpu=None,
        execution_ms=None,
        hourly_rate_usd=None,
    )
    with pytest.raises(CampaignGateError, match="terminal sample identity is immutable"):
        store.record_terminal_execution(
            "campaign-immutable-3",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-immutable-3/variation-01.mp3",
        )
    reservation = store.reservation_for_sample("campaign-immutable-3", sample_id)
    assert reservation is not None
    assert reservation["state"] == "settled"
    assert reservation["final_estimate_micro_usd"] == 0


def test_v1_to_v3_migration_preserves_state(tmp_path: Path) -> None:
    database, campaign_id, expected = _four_state_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE execution_windows DROP COLUMN health_evidence_json")
        connection.execute("DROP TABLE submission_intents")
        connection.execute("PRAGMA user_version=1")
    reopened = CampaignStore.open_existing(database)
    assert reopened is not None
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert _reservation_rows(database, campaign_id) == expected
    # The rebuilt reservations table enforces the four-state CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO reservations(reservation_id, campaign_id, kind, reserved_micro_usd, "
                "state, created_at_utc) VALUES ('res-bogus', ?, 'compute', 1, 'bogus', ?)",
                (campaign_id, "2026-01-01T00:00:00Z"),
            )


def test_v2_to_v3_migration_preserves_state(tmp_path: Path) -> None:
    database, campaign_id, expected = _four_state_fixture(tmp_path)
    _rebuild_reservations_without_state_check(database)
    reopened = CampaignStore.open_existing(database)
    assert reopened is not None
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert _reservation_rows(database, campaign_id) == expected


def test_unknown_reservation_state_rejected(tmp_path: Path) -> None:
    database, campaign_id, _expected = _four_state_fixture(tmp_path)
    _rebuild_reservations_without_state_check(database, corrupt_state="invented_resolved")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        CampaignStore.open_existing(database)
    # The corrupt database is refused without mutation.
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 2
        state = connection.execute(
            "SELECT state FROM reservations WHERE reservation_id='res-open'"
        ).fetchone()[0]
    assert state == "invented_resolved"


def test_new_db_enforces_state_constraint(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-constraint-1", manifest, plan)
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "INSERT INTO reservations(reservation_id, campaign_id, kind, reserved_micro_usd, "
                "state, created_at_utc) VALUES ('res-bogus', 'campaign-constraint-1', 'compute', "
                "1, 'bogus', '2026-01-01T00:00:00Z')"
            )
    assert store.reservation_for_sample("campaign-constraint-1", "anything") is None


def test_committed_spend_fails_closed_on_unknown_state(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-corrupt-1", manifest, plan)
    store.reserve(
        "campaign-corrupt-1",
        "res-corrupt-1",
        kind="compatibility",
        reserved_micro_usd=1000,
    )
    _inject_reservation_state(store.path, state="invented_resolved")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.admission_summary("campaign-corrupt-1")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.reserve(
            "campaign-corrupt-1",
            "res-corrupt-2",
            kind="compatibility",
            reserved_micro_usd=2,
        )


def test_rollback_readiness_fails_closed_on_unknown_state(tmp_path: Path) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-corrupt-2", manifest, plan)
    store.reserve(
        "campaign-corrupt-2",
        "res-corrupt-3",
        kind="compatibility",
        reserved_micro_usd=1000,
    )
    _inject_reservation_state(store.path, state="invented_resolved")
    readiness = store.rollback_readiness()
    assert readiness.safe is False
    assert any(item.classification == "campaign_store_indeterminate" for item in readiness.blockers)


def test_status_recovery_and_teardown_fail_closed_on_unknown_state(
    tmp_path: Path,
) -> None:
    manifest, plan, _cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-corrupt-3", manifest, plan)
    store.reserve(
        "campaign-corrupt-3",
        "res-corrupt-4",
        kind="compatibility",
        reserved_micro_usd=1000,
    )
    window_id = store.open_execution_window(
        "campaign-corrupt-3",
        "screening",
        blocked_routes=("POST /create", "POST /cover", "POST /cover/{job_id}/confirm"),
    )
    _inject_reservation_state(store.path, state="invented_resolved")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.campaign_status("campaign-corrupt-3")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.recover_after_restart()
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.reconcile_pre_intent_samples("campaign-corrupt-3")
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        store.close_execution_window(
            "campaign-corrupt-3",
            window_id,
            health_evidence=_health_evidence(),
            reason="attempted teardown",
        )


def _evidence_snapshot(store: CampaignStore, campaign_id: str, sample_id: str) -> dict[str, object]:
    """Logical before/after snapshot for immutable-evidence rejections.

    Includes the full sample row (with its ``updated_at_utc``), the full
    reservation row (state, estimate, reason, timestamps), and the campaign
    event count, so equality proves no sample, reservation, timestamp, or
    event mutation.
    """
    with sqlite3.connect(store.path) as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM campaign_events WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]
    return {
        "sample": store.sample(sample_id),
        "reservation": store.reservation_for_sample(campaign_id, sample_id),
        "event_count": events,
    }


def test_completed_unavailable_rejects_conflicting_output_path(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-output-conflict-1", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-output-conflict-1",
        cases[0],
        manifest,
        reservation_id="res-output-conflict-1",
        job_id="job-output-conflict-1",
    )
    # The reviewed probe: completed output with missing GPU evidence.
    store.record_terminal_execution(
        "campaign-output-conflict-1",
        sample_id,
        status="completed",
        actual_gpu=None,
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-a/variation-01.mp3",
    )
    before = _evidence_snapshot(store, "campaign-output-conflict-1", sample_id)
    # A later conflicting output identity is rejected before any sample,
    # reservation, event, or timestamp mutation.
    with pytest.raises(CampaignGateError, match="completed output identity conflicts"):
        store.record_terminal_execution(
            "campaign-output-conflict-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-b/variation-01.mp3",
        )
    assert _evidence_snapshot(store, "campaign-output-conflict-1", sample_id) == before
    # The narrow compatible fill with the exact recorded output identity
    # still works and remains idempotent: the rejection mutated nothing.
    estimate = store.record_terminal_execution(
        "campaign-output-conflict-1",
        sample_id,
        status="completed",
        actual_gpu="rtx-4090-24gb",
        execution_ms=100,
        hourly_rate_usd="0.49",
        output_path="job-a/variation-01.mp3",
    )
    assert estimate == 14
    sample = store.sample(sample_id)
    assert sample is not None and sample["output_path"] == "job-a/variation-01.mp3"
    assert (
        store.record_terminal_execution(
            "campaign-output-conflict-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-a/variation-01.mp3",
        )
        == 14
    )


def test_completed_unavailable_rejects_conflicting_gpu_or_reason(tmp_path: Path) -> None:
    manifest, plan, cases = _small_plan()
    store = CampaignStore(tmp_path / "campaign.sqlite3")
    store.create_campaign("campaign-evidence-conflict-1", manifest, plan)
    sample_id = _reserve_and_submit(
        store,
        "campaign-evidence-conflict-1",
        cases[0],
        manifest,
        reservation_id="res-evidence-conflict-1",
        job_id="job-evidence-conflict-1",
    )
    # Prior completed record carries a GPU but misses execution evidence.
    store.record_terminal_execution(
        "campaign-evidence-conflict-1",
        sample_id,
        status="completed",
        actual_gpu="a100-80gb",
        execution_ms=None,
        hourly_rate_usd="0.49",
        output_path="job-evidence-conflict-1/variation-01.mp3",
    )
    before = _evidence_snapshot(store, "campaign-evidence-conflict-1", sample_id)
    with pytest.raises(CampaignGateError, match="GPU evidence conflicts"):
        store.record_terminal_execution(
            "campaign-evidence-conflict-1",
            sample_id,
            status="completed",
            actual_gpu="rtx-4090-24gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-evidence-conflict-1/variation-01.mp3",
        )
    assert _evidence_snapshot(store, "campaign-evidence-conflict-1", sample_id) == before
    # A conflicting caller-supplied unavailable reason is rejected rather
    # than silently ignored when a prior reason is already recorded.
    with pytest.raises(CampaignGateError, match="reason evidence conflicts"):
        store.record_terminal_execution(
            "campaign-evidence-conflict-1",
            sample_id,
            status="completed",
            actual_gpu="a100-80gb",
            execution_ms=100,
            hourly_rate_usd="0.49",
            output_path="job-evidence-conflict-1/variation-01.mp3",
            unavailable_reason="runpod_generation_failed",
        )
    assert _evidence_snapshot(store, "campaign-evidence-conflict-1", sample_id) == before


def _downgrade_v3_to_v1(path: Path) -> None:
    """Rewrite a v3 database to the genuine v1 shape."""
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE execution_windows DROP COLUMN health_evidence_json")
        connection.execute("DROP TABLE submission_intents")
        connection.execute("PRAGMA user_version=1")


def test_corrupt_v1_no_mutation(tmp_path: Path) -> None:
    database, campaign_id, _expected = _four_state_fixture(tmp_path)
    _downgrade_v3_to_v1(database)
    # Rewrite reservations to the genuine v1 shape (no state CHECK) and
    # inject an invented reservation state into it.
    _rebuild_reservations_without_state_check(
        database, corrupt_state="invented_resolved", user_version=1
    )
    with pytest.raises(CampaignSchemaError, match="invented_resolved"):
        CampaignStore.open_existing(database)
    # The corrupt v1 database is refused before any mutation: version,
    # schema objects, and the corrupt row are all untouched.
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
        window_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(execution_windows)").fetchall()
        }
        assert "health_evidence_json" not in window_columns
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "submission_intents" not in tables
        assert "reservations_v3" not in tables
        state = connection.execute(
            "SELECT state FROM reservations WHERE reservation_id='res-open'"
        ).fetchone()[0]
    assert state == "invented_resolved"


def test_v1_migration_failure_after_v1_to_v2_rolls_back(tmp_path: Path) -> None:
    database, campaign_id, _expected = _four_state_fixture(tmp_path)
    _downgrade_v3_to_v1(database)
    _rebuild_reservations_without_state_check(database, user_version=1)
    # Inject a failure that passes integrity and the state preflight, lets
    # the v1-to-v2 statements run, and then fails the v2-to-v3 rebuild: a
    # pre-existing reservations_v3 table makes the rebuild's CREATE fail.
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reservations_v3 (reservation_id TEXT PRIMARY KEY)")
    with pytest.raises(CampaignSchemaError, match="migration failed"):
        CampaignStore.open_existing(database)
    # The whole migration unit rolled back: still v1, no v2/v3 objects, and
    # every reservation row exactly as it was before the attempt.
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 1
        window_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(execution_windows)").fetchall()
        }
        assert "health_evidence_json" not in window_columns
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "submission_intents" not in tables
    rows = _reservation_rows(database, campaign_id)
    assert {item["state"] for item in rows.values()} == {
        "open",
        "unresolved",
        "conservatively_retained",
        "settled",
    }
    assert {item["reserved_micro_usd"] for item in rows.values()} == {1000, 2000, 3000, 4000}


def _insert_storage_artifact(path: Path, campaign_id: str, *, reservation_id: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO storage_artifacts(artifact_id, campaign_id, path, bytes_count, "
            "reservation_id, state, created_at_utc) VALUES (?, ?, ?, ?, ?, 'reserved', ?)",
            (
                "artifact-preserve-1",
                campaign_id,
                "variation-01.mp3",
                1234,
                reservation_id,
                "2026-08-08T00:00:00Z",
            ),
        )


def _storage_artifact_rows(path: Path, campaign_id: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT artifact_id, campaign_id, path, bytes_count, reservation_id, state, "
                "created_at_utc, removed_at_utc FROM storage_artifacts WHERE campaign_id=? "
                "ORDER BY artifact_id",
                (campaign_id,),
            ).fetchall()
        ]


def test_v1_to_v3_preserves_storage_artifacts(tmp_path: Path) -> None:
    database, campaign_id, expected = _four_state_fixture(tmp_path)
    _insert_storage_artifact(database, campaign_id, reservation_id="res-settled")
    artifact_before = _storage_artifact_rows(database, campaign_id)
    _downgrade_v3_to_v1(database)
    reopened = CampaignStore.open_existing(database)
    assert reopened is not None
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # Every reservation field and the storage child link survive verbatim.
    assert _reservation_rows(database, campaign_id) == expected
    assert _storage_artifact_rows(database, campaign_id) == artifact_before
    # The migrated reservations table enforces the four-state CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO reservations(reservation_id, campaign_id, kind, reserved_micro_usd, "
                "state, created_at_utc) VALUES ('res-bogus', ?, 'compute', 1, 'bogus', ?)",
                (campaign_id, "2026-01-01T00:00:00Z"),
            )


def test_v2_to_v3_preserves_storage_artifacts(tmp_path: Path) -> None:
    database, campaign_id, expected = _four_state_fixture(tmp_path)
    _insert_storage_artifact(database, campaign_id, reservation_id="res-settled")
    artifact_before = _storage_artifact_rows(database, campaign_id)
    _rebuild_reservations_without_state_check(database)
    reopened = CampaignStore.open_existing(database)
    assert reopened is not None
    with sqlite3.connect(database) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _reservation_rows(database, campaign_id) == expected
    assert _storage_artifact_rows(database, campaign_id) == artifact_before

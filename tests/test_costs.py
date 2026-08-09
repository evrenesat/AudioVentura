"""Cost-domain tests: exact rounding, quotes, immutable attempt evidence,
GPU rate freshness, half-open aggregation, and signed reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from ace_service.costs import (
    MICRO_USD_PER_USD,
    MS_PER_HOUR,
    build_cost_fingerprint,
    compute_submission_quote,
    parse_micro_usd_decimal,
    round_half_up_compute_cost,
    round_half_up_compute_cost_usd,
)
from ace_service.models import JobStatus, JobType
from ace_service.repository import (
    EvidenceConflictError,
    create_job,
    create_submission_quote,
    create_variation_attempt,
    get_matching_runtime_calibration,
    get_submission_quote,
    reconcile_delta,
    record_attempt_evidence,
    sum_terminal_attempt_estimates,
    transition_variation_attempt,
    upsert_gpu_rate,
    upsert_runtime_calibration,
)
from tests.conftest import utc_dt

FIXED_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _quote_estimate(**overrides):
    defaults = {
        "profile_id": "fast-beta-v1",
        "duration_mode": "custom",
        "duration_value_seconds": 30.0,
        "variation_count": 2,
        "eligible_gpu_ids": ["RTX4090", "L40S"],
        "fresh_rates": {"RTX4090": 490_000, "L40S": 700_000},
        "fresh_rate_usd": {"RTX4090": "0.4900004", "L40S": "0.7000004"},
        "stale_gpu_ids": set(),
        "calibration_version": 1,
        "predicted_execution_range_ms": (2000, 3000),
        "rate_source": "runpod_flex_api",
        "rate_version": "2026-08-01",
        "captured_at": FIXED_NOW,
        "model_identity": "acestep-v15-xl-turbo",
    }
    defaults.update(overrides)
    return compute_submission_quote(**defaults)


def _available_estimate(**overrides):
    defaults = {
        "profile_id": "fast-beta-v1",
        "duration_mode": "custom",
        "duration_value_seconds": 30.0,
        "variation_count": 2,
        "eligible_gpu_ids": ["RTX4090", "L40S"],
        "model_identity": "acestep-v15-xl-turbo",
        "highest_trusted_hourly_rate_micro_usd": 700_000,
        "highest_trusted_hourly_rate_usd": "0.7000004",
        "calibration_version": 1,
        "predicted_execution_range_ms": (2000, 3000),
        "quoted_amount_micro_usd": 583,
        "quoted_range_low_micro_usd": 389,
        "quoted_range_high_micro_usd": 583,
        "currency": "USD",
        "rate_source": "runpod_flex_api",
        "rate_version": "2026-08-01",
        "unavailable_reason_code": None,
        "captured_at": FIXED_NOW,
    }
    defaults.update(overrides)
    from ace_service.costs import QuoteEstimate

    return QuoteEstimate(
        cost_fingerprint=build_cost_fingerprint(
            profile_id=defaults["profile_id"],
            duration_mode=defaults["duration_mode"],
            duration_value_seconds=defaults["duration_value_seconds"],
            variation_count=defaults["variation_count"],
            eligible_gpu_ids=defaults["eligible_gpu_ids"],
            model_identity=defaults["model_identity"],
        ),
        **defaults,
    )


class TestExactRounding:
    def test_half_up_boundary_values(self) -> None:
        assert round_half_up_compute_cost(0, 1_000_000) == 0
        assert round_half_up_compute_cost(1800, 1_000_000) == 500
        assert round_half_up_compute_cost(3600, 1_000_000) == 1000
        assert round_half_up_compute_cost(3_600_000, 1_000_000) == MICRO_USD_PER_USD
        # Exactly 0.5 micro-USD rounds half-up, never to zero.
        assert round_half_up_compute_cost(1, 1_800_000) == 1
        assert round_half_up_compute_cost(1, 1_799_999) == 0
        # 277.777 -> 278 and 277.5 -> 278 and 277.72 -> 277.
        assert round_half_up_compute_cost(1000, 1_000_000) == 278
        assert round_half_up_compute_cost(999, 1_000_000) == 278
        assert round_half_up_compute_cost(998, 1_000_000) == 277
        # 30-second custom job at 0.70 USD/hour = 5833 micro-USD.
        assert round_half_up_compute_cost(30_000, 700_000) == 5833
        # The formula is exactly execution_ms * rate_micro_usd / MS_PER_HOUR.
        assert (
            round_half_up_compute_cost(123_456, 490_000)
            == (123_456 * 490_000 + MS_PER_HOUR // 2) // MS_PER_HOUR
        )

    def test_rejects_non_integer_inputs(self) -> None:
        with pytest.raises(ValueError):
            round_half_up_compute_cost(1.5, 1_000_000)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            round_half_up_compute_cost(True, 1_000_000)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            round_half_up_compute_cost(-1, 1_000_000)
        with pytest.raises(ValueError):
            round_half_up_compute_cost(1000, -5)

    def test_decimal_parsing_never_uses_floats(self) -> None:
        raw, micro = parse_micro_usd_decimal("1.5", field_name="amount")
        assert raw == "1.5"
        assert micro == 1_500_000
        raw, micro = parse_micro_usd_decimal("0.0000005", field_name="amount")
        assert raw == "0.0000005"
        assert micro == 1  # half-up at exactly 0.5 micro-USD
        raw, micro = parse_micro_usd_decimal(Decimal("2.75"), field_name="amount")
        assert raw == "2.75"
        assert micro == 2_750_000
        raw, micro = parse_micro_usd_decimal(7, field_name="amount")
        assert raw == "7"
        assert micro == 7_000_000
        with pytest.raises(ValueError):
            parse_micro_usd_decimal(1.5, field_name="amount")  # float rejected
        with pytest.raises(ValueError):
            parse_micro_usd_decimal(True, field_name="amount")  # bool rejected
        with pytest.raises(ValueError):
            parse_micro_usd_decimal("-1", field_name="amount")
        with pytest.raises(ValueError):
            parse_micro_usd_decimal("1e3", field_name="amount")
        with pytest.raises(ValueError):
            parse_micro_usd_decimal("1" + "0" * 40, field_name="amount")  # overflow

    def test_exact_hourly_token_is_not_pre_rounded(self) -> None:
        assert round_half_up_compute_cost_usd(1_800_000, "0.0000006") == 0
        # Pre-rounding 0.0000006 USD/hour to one micro-USD/hour would invent 1.
        assert round_half_up_compute_cost(1_800_000, 1) == 1


class TestSubmissionQuotes:
    def test_one_to_one_quote_creation_and_idempotent_repeat(self, session) -> None:
        job = create_job(
            session,
            job_type=JobType.ORIGINAL,
            variation_count=2,
            normalized_request_json={"schema_version": 2, "task_type": "original"},
        )
        estimate = _available_estimate()
        quote = create_submission_quote(session, job.id, estimate, captured_at=FIXED_NOW)
        session.commit()
        assert quote.job_id == job.id
        assert quote.quoted_amount_micro_usd == 583
        assert quote.highest_trusted_hourly_rate_micro_usd == 700_000
        assert quote.highest_trusted_hourly_rate_usd == "0.7000004"

        # An identical repeat (even with a different capture timestamp) is
        # idempotent and returns the stored immutable record.
        repeat = create_submission_quote(
            session, job.id, estimate, captured_at=FIXED_NOW + timedelta(hours=1)
        )
        session.commit()
        assert repeat.id == quote.id
        assert get_submission_quote(session, job.id) is quote

    def test_conflicting_quote_rejected_without_mutation(self, session) -> None:
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        estimate = _available_estimate()
        create_submission_quote(session, job.id, estimate, captured_at=FIXED_NOW)
        session.commit()
        conflicting = _available_estimate(
            highest_trusted_hourly_rate_micro_usd=999_999,
            highest_trusted_hourly_rate_usd="0.999999",
            quoted_amount_micro_usd=832,
            quoted_range_low_micro_usd=555,
            quoted_range_high_micro_usd=832,
        )
        with pytest.raises(EvidenceConflictError):
            create_submission_quote(session, job.id, conflicting, captured_at=FIXED_NOW)
        stored = get_submission_quote(session, job.id)
        assert stored is not None
        assert stored.quoted_amount_micro_usd == 583
        before = (stored.captured_at, stored.model_identity)
        with pytest.raises(EvidenceConflictError):
            create_submission_quote(
                session,
                job.id,
                _available_estimate(model_identity="different-model"),
                captured_at=FIXED_NOW + timedelta(days=1),
            )
        session.rollback()
        stored = get_submission_quote(session, job.id)
        assert stored is not None and (stored.captured_at, stored.model_identity) == before

    def test_unavailable_reason_allow_list_enforced(self, session) -> None:
        for index, reason in enumerate(
            (
                "rate_stale",
                "rate_unknown",
                "gpu_unknown",
                "provider_unreachable",
                "calibration_missing",
            )
        ):
            job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
            quote = create_submission_quote(
                session, job.id, _unavailable_estimate(reason), captured_at=FIXED_NOW
            )
            session.flush()
            assert quote.unavailable_reason_code == reason
            assert quote.quoted_amount_micro_usd is None
            assert quote.id == index + 1
        with pytest.raises(ValueError, match="unavailable_reason_code"):
            create_submission_quote(
                session, "another-job", _unavailable_estimate("invented_reason")
            )

    def test_quote_records_are_secret_free(self, session) -> None:
        job = create_job(
            session,
            job_type=JobType.ORIGINAL,
            source_url="https://example.invalid/song",
            prompt="top-secret prompt",
            lyrics="private lyrics",
            variation_count=1,
            normalized_request_json={"schema_version": 2, "task_type": "original"},
        )
        estimate = _quote_estimate(eligible_gpu_ids=["RTX4090"], fresh_rates={"RTX4090": 490_000})
        create_submission_quote(session, job.id, estimate, captured_at=FIXED_NOW)
        session.commit()
        engine = session.get_bind()
        columns = {
            row[1] for row in engine.connect().execute(text("PRAGMA table_info(submission_quotes)"))
        }
        assert "prompt" not in columns
        assert "lyrics" not in columns
        assert "source_url" not in columns
        assert "normalized_request_json" not in columns
        stored = get_submission_quote(session, job.id)
        assert stored is not None
        serialized = str(
            engine.connect()
            .execute(
                text("SELECT * FROM submission_quotes WHERE job_id = :job_id"),
                {"job_id": job.id},
            )
            .fetchone()
        )
        assert "top-secret prompt" not in serialized
        assert "private lyrics" not in serialized
        assert "https://example.invalid/song" not in serialized

    def test_fingerprint_covers_only_non_secret_drivers(self) -> None:
        base = dict(
            profile_id="fast-beta-v1",
            duration_mode="custom",
            duration_value_seconds=30.0,
            variation_count=2,
            eligible_gpu_ids=["RTX4090", "L40S"],
        )
        same = build_cost_fingerprint(**base)
        assert same == build_cost_fingerprint(**base)
        assert same != build_cost_fingerprint(**{**base, "duration_value_seconds": 31.0})
        assert same != build_cost_fingerprint(**{**base, "eligible_gpu_ids": ["RTX4090"]})
        assert same != build_cost_fingerprint(**{**base, "model_identity": "different-model"})
        assert same == build_cost_fingerprint(**{**base, "eligible_gpu_ids": ["L40S", "RTX4090"]})


class TestQuoteComputation:
    def test_available_quote_uses_highest_trusted_eligible_rate(self) -> None:
        estimate = _quote_estimate()
        assert estimate.available
        assert estimate.highest_trusted_hourly_rate_micro_usd == 700_000
        # round_half_up(3000 * 0.70 USD / 3600000) = 583; low = 389.
        assert estimate.quoted_range_low_micro_usd == 389
        assert estimate.quoted_range_high_micro_usd == 583
        assert estimate.quoted_amount_micro_usd == 583
        assert estimate.predicted_execution_range_ms == (2000, 3000)
        assert estimate.highest_trusted_hourly_rate_usd == "0.7000004"

    @pytest.mark.parametrize(
        "eligible_gpu_ids",
        (["LOW", "HIGH"], ["HIGH", "LOW"]),
    )
    def test_exact_rate_collision_selects_true_highest_independent_of_order(
        self, eligible_gpu_ids: list[str]
    ) -> None:
        estimate = _quote_estimate(
            eligible_gpu_ids=eligible_gpu_ids,
            fresh_rates={"LOW": 490_000, "HIGH": 490_000},
            fresh_rate_usd={"LOW": "0.4900004", "HIGH": "0.49000049"},
            predicted_execution_range_ms=(20_531, 20_531),
        )

        assert estimate.highest_trusted_hourly_rate_micro_usd == 490_000
        assert estimate.highest_trusted_hourly_rate_usd == "0.49000049"
        assert estimate.quoted_amount_micro_usd == 2_795
        assert estimate.quoted_range_low_micro_usd == 2_795
        assert estimate.quoted_range_high_micro_usd == 2_795

    def test_available_quote_requires_matching_exact_token_for_every_eligible_gpu(self) -> None:
        with pytest.raises(ValueError, match="fresh_rate_usd must include every eligible GPU"):
            _quote_estimate(fresh_rate_usd={"RTX4090": "0.4900004"})
        with pytest.raises(ValueError, match="does not match derived"):
            _quote_estimate(fresh_rate_usd={"RTX4090": "0.4900004", "L40S": "0.8000004"})

    def test_unknown_eligible_gpu_makes_quote_unavailable(self) -> None:
        estimate = _quote_estimate(
            eligible_gpu_ids=["RTX4090", "A100"],
            fresh_rates={"RTX4090": 490_000},
            stale_gpu_ids=set(),
        )
        assert not estimate.available
        assert estimate.unavailable_reason_code == "rate_unknown"
        assert estimate.quoted_amount_micro_usd is None
        assert estimate.highest_trusted_hourly_rate_micro_usd is None

    def test_stale_eligible_gpu_makes_quote_unavailable(self) -> None:
        estimate = _quote_estimate(
            eligible_gpu_ids=["RTX4090", "L40S"],
            fresh_rates={"RTX4090": 490_000},
            stale_gpu_ids={"L40S"},
        )
        assert not estimate.available
        assert estimate.unavailable_reason_code == "rate_stale"

    def test_no_eligible_gpus_is_gpu_unknown(self) -> None:
        estimate = _quote_estimate(eligible_gpu_ids=[], fresh_rates={}, stale_gpu_ids=set())
        assert not estimate.available
        assert estimate.unavailable_reason_code == "gpu_unknown"

    def test_missing_calibration_is_calibration_missing(self) -> None:
        estimate = _quote_estimate(calibration_version=None, predicted_execution_range_ms=None)
        assert not estimate.available
        assert estimate.unavailable_reason_code == "calibration_missing"

    def test_never_quotes_from_cheaper_known_subset(self) -> None:
        estimate = _quote_estimate(
            eligible_gpu_ids=["RTX4090", "L40S", "H100"],
            fresh_rates={"RTX4090": 490_000, "L40S": 700_000},
            stale_gpu_ids=set(),
        )
        assert estimate.unavailable_reason_code == "rate_unknown"
        assert estimate.quoted_amount_micro_usd is None


class TestRuntimeCalibration:
    def _values(self, **overrides):
        values = {
            "version": 1,
            "task_mode": "original",
            "profile_id": "fast-beta-v1",
            "model_identity": "acestep-v15-xl-turbo",
            "runtime_identity": "worker-schema-v2",
            "gpu_class": "L40S",
            "duration_mode": "custom",
            "duration_band_min_seconds": 20.0,
            "duration_band_max_seconds": 40.0,
            "output_count": 2,
            "execution_low_ms": 2000,
            "execution_high_ms": 3000,
            "evidence_source": "accepted-local-measurement-v1",
            "conservative_margin": "0.10",
            "captured_at": FIXED_NOW,
        }
        values.update(overrides)
        return values

    def test_exact_repeat_matching_and_no_extrapolation(self, session) -> None:
        row = upsert_runtime_calibration(session, **self._values())
        session.commit()
        repeat = upsert_runtime_calibration(session, **self._values())
        assert repeat.id == row.id
        match = get_matching_runtime_calibration(
            session,
            task_mode="original",
            profile_id="fast-beta-v1",
            model_identity="acestep-v15-xl-turbo",
            runtime_identity="worker-schema-v2",
            gpu_class="L40S",
            duration_mode="custom",
            duration_value_seconds=30.0,
            output_count=2,
        )
        assert match is not None and match.version == 1
        assert (
            get_matching_runtime_calibration(
                session,
                task_mode="original",
                profile_id="fast-beta-v1",
                model_identity="different-model",
                runtime_identity="worker-schema-v2",
                gpu_class="L40S",
                duration_mode="custom",
                duration_value_seconds=30.0,
                output_count=2,
            )
            is None
        )
        assert (
            get_matching_runtime_calibration(
                session,
                task_mode="original",
                profile_id="fast-beta-v1",
                model_identity="acestep-v15-xl-turbo",
                runtime_identity="worker-schema-v2",
                gpu_class="L40S",
                duration_mode="custom",
                duration_value_seconds=41.0,
                output_count=2,
            )
            is None
        )

    def test_conflicting_version_rejected_without_mutation(self, session) -> None:
        row = upsert_runtime_calibration(session, **self._values())
        session.commit()
        before = (row.execution_high_ms, row.captured_at)
        with pytest.raises(EvidenceConflictError):
            upsert_runtime_calibration(session, **self._values(execution_high_ms=9999))
        session.rollback()
        stored = session.get(type(row), row.id)
        assert (stored.execution_high_ms, stored.captured_at) == before

    def test_matching_calibration_can_drive_available_exact_rate_quote(self, session) -> None:
        upsert_runtime_calibration(session, **self._values())
        calibration = get_matching_runtime_calibration(
            session,
            task_mode="original",
            profile_id="fast-beta-v1",
            model_identity="acestep-v15-xl-turbo",
            runtime_identity="worker-schema-v2",
            gpu_class="L40S",
            duration_mode="custom",
            duration_value_seconds=30.0,
            output_count=2,
        )
        assert calibration is not None
        quote = _quote_estimate(
            calibration_version=calibration.version,
            predicted_execution_range_ms=(
                calibration.execution_low_ms,
                calibration.execution_high_ms,
            ),
        )
        assert quote.available and quote.quoted_amount_micro_usd == 583


class TestGpuRateCatalog:
    def test_upsert_is_versioned_and_freshness_gated(self, session) -> None:
        now = FIXED_NOW
        row = upsert_gpu_rate(
            session,
            gpu_id="RTX4090",
            rate_micro_usd_per_hour=490_000,
            hourly_rate_usd="0.4900004",
            source="runpod_flex_api",
            calibration_version=1,
            captured_at=now,
            price_max_age_hours=24,
        )
        session.commit()
        assert row.expires_at == now + timedelta(hours=24)
        # Same exact version is idempotent; conflicting reuse is rejected.
        upsert_gpu_rate(
            session,
            gpu_id="RTX4090",
            rate_micro_usd_per_hour=490_000,
            hourly_rate_usd="0.4900004",
            source="runpod_flex_api",
            calibration_version=1,
            captured_at=now,
            price_max_age_hours=24,
        )
        with pytest.raises(EvidenceConflictError):
            upsert_gpu_rate(
                session,
                gpu_id="RTX4090",
                rate_micro_usd_per_hour=480_000,
                hourly_rate_usd="0.48",
                source="runpod_flex_api",
                calibration_version=1,
                captured_at=now,
                price_max_age_hours=24,
            )
        upsert_gpu_rate(
            session,
            gpu_id="RTX4090",
            rate_micro_usd_per_hour=500_000,
            hourly_rate_usd="0.5000004",
            source="runpod_flex_api",
            calibration_version=2,
            captured_at=now + timedelta(hours=2),
            price_max_age_hours=24,
        )
        session.commit()
        fresh = session.scalar(
            text("SELECT count(*) FROM gpu_rate_catalog WHERE gpu_id = 'RTX4090'")
        )
        assert fresh == 2

        from ace_service.repository import get_current_gpu_rates, get_gpu_rate

        current = get_current_gpu_rates(session, ["RTX4090"], now=now + timedelta(hours=3))
        assert current["RTX4090"].rate_micro_usd_per_hour == 500_000
        assert get_gpu_rate(session, "RTX4090", now=now + timedelta(hours=3)) is not None
        # After expiry the rate is stale and must be treated as unavailable.
        assert get_current_gpu_rates(session, ["RTX4090"], now=now + timedelta(hours=30)) == {}
        assert get_gpu_rate(session, "RTX4090", now=now + timedelta(hours=30)) is None

    def test_rate_validation(self, session) -> None:
        with pytest.raises(ValueError):
            upsert_gpu_rate(
                session,
                gpu_id="RTX4090",
                rate_micro_usd_per_hour=-1,
                hourly_rate_usd="0",
                source="runpod_flex_api",
                calibration_version=1,
                captured_at=FIXED_NOW,
            )
        with pytest.raises(ValueError):
            upsert_gpu_rate(
                session,
                gpu_id="RTX4090",
                rate_micro_usd_per_hour=1.5,  # type: ignore[arg-type]
                hourly_rate_usd="0.0000015",
                source="runpod_flex_api",
                calibration_version=1,
                captured_at=FIXED_NOW,
            )
        with pytest.raises(ValueError):
            upsert_gpu_rate(
                session,
                gpu_id="RTX4090",
                rate_micro_usd_per_hour=100,
                hourly_rate_usd="0.0001",
                source="runpod_flex_api",
                calibration_version=0,
                captured_at=FIXED_NOW,
            )


def _unavailable_estimate(reason: str):
    from ace_service.costs import QuoteEstimate

    return QuoteEstimate(
        cost_fingerprint=build_cost_fingerprint(
            profile_id="fast-beta-v1",
            duration_mode="custom",
            duration_value_seconds=30.0,
            variation_count=2,
            eligible_gpu_ids=["RTX4090", "L40S"],
            model_identity="acestep-v15-xl-turbo",
        ),
        model_identity="acestep-v15-xl-turbo",
        profile_id="fast-beta-v1",
        duration_mode="custom",
        duration_value_seconds=30.0,
        variation_count=2,
        eligible_gpu_ids=["RTX4090", "L40S"],
        highest_trusted_hourly_rate_micro_usd=None,
        highest_trusted_hourly_rate_usd=None,
        calibration_version=1,
        predicted_execution_range_ms=None,
        quoted_amount_micro_usd=None,
        quoted_range_low_micro_usd=None,
        quoted_range_high_micro_usd=None,
        currency="USD",
        rate_source=None,
        rate_version=None,
        unavailable_reason_code=reason,
        captured_at=FIXED_NOW,
    )


def _reload_attempt(session, attempt_id):
    from ace_service.models import VariationAttempt

    return session.get(VariationAttempt, attempt_id)


class TestAttemptEvidence:
    def _attempt(self, session) -> int:
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        session.commit()
        return attempt.id

    def test_pending_to_complete_and_exact_repeat_idempotence(self, session) -> None:
        attempt_id = self._attempt(session)
        now = FIXED_NOW
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="complete",
            actual_gpu="RTX4090",
            model_identity="acestep-v15-xl-turbo",
            runtime_image_identity="sha256:" + "a" * 64,
            execution_ms=30_000,
            hourly_rate_usd="0.7000004",
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=now - timedelta(hours=2),
            estimated_compute_micro_usd=5833,
            now=now,
        )
        session.commit()
        attempt = _reload_attempt(session, attempt_id)
        assert attempt.evidence_status == "complete"
        assert attempt.estimated_compute_micro_usd == 5833
        assert attempt.hourly_rate_usd == "0.7000004"
        first_updated_at = attempt.updated_at

        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="complete",
            actual_gpu="RTX4090",
            model_identity="acestep-v15-xl-turbo",
            runtime_image_identity="sha256:" + "a" * 64,
            execution_ms=30_000,
            hourly_rate_usd="0.7000004",
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=now - timedelta(hours=2),
            estimated_compute_micro_usd=5833,
            now=now + timedelta(hours=5),
        )
        session.commit()
        attempt = _reload_attempt(session, attempt_id)
        assert attempt.updated_at == first_updated_at

    def test_pending_to_unavailable_with_partial_fields(self, session) -> None:
        attempt_id = self._attempt(session)
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            execution_ms=30_000,
            unavailable_reason="rate_unknown",
        )
        session.commit()
        attempt = _reload_attempt(session, attempt_id)
        assert attempt.evidence_status == "unavailable"
        assert attempt.execution_ms == 30_000
        assert attempt.estimated_compute_micro_usd is None

    def test_unavailable_to_complete_fills_missing_inputs(self, session) -> None:
        attempt_id = self._attempt(session)
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            actual_gpu="RTX4090",
            execution_ms=30_000,
            unavailable_reason="rate_unknown",
        )
        session.commit()
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="complete",
            actual_gpu="RTX4090",
            execution_ms=30_000,
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=FIXED_NOW,
            estimated_compute_micro_usd=5833,
        )
        session.commit()
        attempt = _reload_attempt(session, attempt_id)
        assert attempt.evidence_status == "complete"
        assert attempt.estimated_compute_micro_usd == 5833

    def test_unavailable_to_complete_with_conflicting_gpu_rejected(self, session) -> None:
        attempt_id = self._attempt(session)
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            actual_gpu="RTX4090",
            execution_ms=30_000,
            unavailable_reason="rate_unknown",
        )
        session.commit()
        with pytest.raises(EvidenceConflictError):
            record_attempt_evidence(
                session,
                attempt_id,
                evidence_status="complete",
                actual_gpu="L40S",
                execution_ms=30_000,
                hourly_rate_micro_usd=700_000,
                rate_currency="USD",
                rate_source="runpod_flex_api",
                rate_captured_at=FIXED_NOW,
                estimated_compute_micro_usd=5833,
            )
        attempt = _reload_attempt(session, attempt_id)
        assert attempt.evidence_status == "unavailable"
        assert attempt.actual_gpu == "RTX4090"

    def test_conflicting_terminal_evidence_rejected_without_timestamp_mutation(
        self, session
    ) -> None:
        attempt_id = self._attempt(session)
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="complete",
            actual_gpu="RTX4090",
            execution_ms=30_000,
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=FIXED_NOW,
            estimated_compute_micro_usd=5833,
            now=FIXED_NOW,
        )
        session.commit()
        attempt = _reload_attempt(session, attempt_id)
        before = (attempt.updated_at, attempt.execution_ms, attempt.estimated_compute_micro_usd)

        for conflict in (
            dict(execution_ms=31_000, estimated_compute_micro_usd=6028),
            dict(hourly_rate_micro_usd=1, estimated_compute_micro_usd=0),
            dict(actual_gpu="L40S"),
            dict(evidence_status="unavailable", unavailable_reason="timing_unavailable"),
        ):
            kwargs = {
                "evidence_status": "complete",
                "actual_gpu": "RTX4090",
                "execution_ms": 30_000,
                "hourly_rate_micro_usd": 700_000,
                "rate_currency": "USD",
                "rate_source": "runpod_flex_api",
                "rate_captured_at": FIXED_NOW,
                "estimated_compute_micro_usd": 5833,
                "now": FIXED_NOW + timedelta(hours=1),
            }
            kwargs.update(conflict)
            if conflict.get("evidence_status") == "unavailable":
                kwargs.pop("unavailable_reason", None)
                kwargs["unavailable_reason"] = "timing_unavailable"
                kwargs.pop("estimated_compute_micro_usd", None)
            with pytest.raises(EvidenceConflictError):
                record_attempt_evidence(session, attempt_id, **kwargs)
            session.rollback()
            attempt = session.get(
                __import__("ace_service.models", fromlist=["VariationAttempt"]).VariationAttempt,
                attempt_id,
            )
            assert (
                attempt.updated_at,
                attempt.execution_ms,
                attempt.estimated_compute_micro_usd,
            ) == before

    def test_complete_requires_formula_exact_estimate(self, session) -> None:
        attempt_id = self._attempt(session)
        with pytest.raises(ValueError, match="centralized half-up"):
            record_attempt_evidence(
                session,
                attempt_id,
                evidence_status="complete",
                actual_gpu="RTX4090",
                execution_ms=30_000,
                hourly_rate_micro_usd=700_000,
                rate_currency="USD",
                rate_source="runpod_flex_api",
                rate_captured_at=FIXED_NOW,
                estimated_compute_micro_usd=5834,  # wrong by one micro-USD
            )

    def test_pending_cannot_regress_terminal_evidence(self, session) -> None:
        attempt_id = self._attempt(session)
        record_attempt_evidence(
            session,
            attempt_id,
            evidence_status="unavailable",
            unavailable_reason="timing_unavailable",
        )
        session.commit()
        with pytest.raises(EvidenceConflictError):
            record_attempt_evidence(session, attempt_id, evidence_status="pending")
        with pytest.raises(ValueError, match="pending evidence must not carry"):
            record_attempt_evidence(session, attempt_id, evidence_status="pending", execution_ms=1)

    def test_unavailable_reason_allow_list(self, session) -> None:
        attempt_id = self._attempt(session)
        with pytest.raises(ValueError, match="unavailable_reason"):
            record_attempt_evidence(
                session,
                attempt_id,
                evidence_status="unavailable",
                unavailable_reason="invented",
            )


class TestAggregationAndReconciliation:
    def _terminal_attempt(
        self, session, *, completed_at, evidence_status="complete", execution_ms=30_000
    ) -> int:
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.COMPLETED, now=completed_at)
        if evidence_status == "complete":
            record_attempt_evidence(
                session,
                attempt.id,
                evidence_status="complete",
                actual_gpu="RTX4090",
                execution_ms=execution_ms,
                hourly_rate_micro_usd=700_000,
                rate_currency="USD",
                rate_source="runpod_flex_api",
                rate_captured_at=FIXED_NOW,
                estimated_compute_micro_usd=round_half_up_compute_cost(execution_ms, 700_000),
            )
        elif evidence_status == "unavailable":
            record_attempt_evidence(
                session,
                attempt.id,
                evidence_status="unavailable",
                execution_ms=execution_ms,
                unavailable_reason="rate_unknown",
            )
        session.commit()
        return attempt.id

    def test_half_open_interval_aggregation(self, session) -> None:
        start = utc_dt(0)
        self._terminal_attempt(session, completed_at=start)
        self._terminal_attempt(session, completed_at=utc_dt(1))
        self._terminal_attempt(session, completed_at=utc_dt(2))  # excluded (end boundary)
        session.commit()
        summary = sum_terminal_attempt_estimates(
            session, interval_start=start, interval_end=utc_dt(2)
        )
        assert summary.terminal_attempts == 2
        assert summary.attempts_with_estimate == 2
        assert not summary.partial_coverage
        expected = 2 * round_half_up_compute_cost(30_000, 700_000)
        assert summary.summed_estimate_micro_usd == expected

    def test_partial_coverage_when_terminal_attempt_lacks_cost(self, session) -> None:
        start = utc_dt(0)
        self._terminal_attempt(session, completed_at=utc_dt(0))
        self._terminal_attempt(session, completed_at=utc_dt(1), evidence_status="unavailable")
        # A legacy terminal attempt with pending evidence is unavailable too.
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        legacy = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, legacy.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, legacy.id, JobStatus.FAILED, now=utc_dt(2))
        session.commit()
        summary = sum_terminal_attempt_estimates(
            session, interval_start=start, interval_end=utc_dt(3)
        )
        assert summary.terminal_attempts == 3
        assert summary.attempts_without_cost == 2
        assert summary.partial_coverage
        assert summary.summed_estimate_micro_usd == round_half_up_compute_cost(30_000, 700_000)

    def test_signed_delta_preserves_negative_values(self, session) -> None:
        start = utc_dt(0)
        self._terminal_attempt(session, completed_at=start)
        session.commit()
        summary = sum_terminal_attempt_estimates(
            session, interval_start=start, interval_end=utc_dt(1)
        )
        estimate = summary.summed_estimate_micro_usd
        positive = reconcile_delta(
            session,
            interval_start=start,
            interval_end=utc_dt(1),
            actual_endpoint_micro_usd=estimate + 10_000,
            cutoff_at=FIXED_NOW,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        assert positive.available
        assert positive.delta_micro_usd == 10_000
        assert positive.coverage == "complete"
        negative = reconcile_delta(
            session,
            interval_start=start,
            interval_end=utc_dt(1),
            actual_endpoint_micro_usd=estimate - 5_000,
            cutoff_at=FIXED_NOW,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        assert negative.available
        assert negative.delta_micro_usd == -5_000  # never clamped to zero

    def test_reconciliation_unavailable_without_actual(self, session) -> None:
        start = utc_dt(0)
        self._terminal_attempt(session, completed_at=start)
        session.commit()
        result = reconcile_delta(
            session,
            interval_start=start,
            interval_end=utc_dt(1),
            actual_endpoint_micro_usd=None,
        )
        assert not result.available
        assert result.coverage == "unavailable"
        assert result.delta_micro_usd is None

    def test_failed_positive_time_counts_and_never_submitted_has_no_attempt(self, session) -> None:
        start = utc_dt(0)
        # A failed attempt with positive execution time counts.
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        failed = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, failed.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, failed.id, JobStatus.FAILED, now=utc_dt(0))
        record_attempt_evidence(
            session,
            failed.id,
            evidence_status="complete",
            actual_gpu="L40S",
            execution_ms=10_000,
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=FIXED_NOW,
            estimated_compute_micro_usd=1944,
        )
        # A proven never-started attempt stores zero and counts as zero.
        job2 = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        never_started = create_variation_attempt(session, job_id=job2.id, variation_index=1)
        transition_variation_attempt(session, never_started.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, never_started.id, JobStatus.FAILED, now=utc_dt(1))
        record_attempt_evidence(
            session,
            never_started.id,
            evidence_status="complete",
            actual_gpu="RTX4090",
            execution_ms=0,
            hourly_rate_micro_usd=700_000,
            rate_currency="USD",
            rate_source="runpod_flex_api",
            rate_captured_at=FIXED_NOW,
            estimated_compute_micro_usd=0,
        )
        session.commit()
        summary = sum_terminal_attempt_estimates(
            session, interval_start=start, interval_end=utc_dt(2)
        )
        assert summary.terminal_attempts == 2
        assert summary.attempts_with_estimate == 2
        assert not summary.partial_coverage
        assert summary.summed_estimate_micro_usd == 1944

    def test_legacy_attempt_renders_as_unavailable(self, session) -> None:
        start = utc_dt(0)
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.COMPLETED, now=utc_dt(0))
        session.commit()
        summary = sum_terminal_attempt_estimates(
            session, interval_start=start, interval_end=utc_dt(1)
        )
        assert summary.partial_coverage
        assert summary.attempts_without_cost == 1
        assert summary.summed_estimate_micro_usd == 0


class TestWorkerPollEvidence:
    """The terminal polling hook records immutable evidence from one poll."""

    def test_completed_poll_with_trusted_rate_records_complete_evidence(self, session) -> None:
        from ace_service.runpod_client import RunpodState, RunpodStatusResult
        from ace_service.worker import _record_poll_evidence

        upsert_gpu_rate(
            session,
            gpu_id="RTX4090",
            rate_micro_usd_per_hour=700_000,
            hourly_rate_usd="0.7000004",
            source="runpod_flex_api",
            calibration_version=1,
            captured_at=datetime.now(UTC) - timedelta(hours=1),
            price_max_age_hours=24,
        )
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        transition_variation_attempt(session, attempt.id, JobStatus.GENERATING)
        session.commit()
        result = RunpodStatusResult(
            job_id="runpod-1",
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
            execution_ms=30_000,
        )
        metadata = {
            "worker": {
                "gpu": "NVIDIA GeForce RTX 4090",
                "dit_model": "acestep-v15-xl-turbo",
                "image_digest": "sha256:" + "b" * 64,
            }
        }
        _record_poll_evidence(session, attempt.id, result, metadata)
        session.commit()
        from ace_service.models import VariationAttempt

        stored = session.get(VariationAttempt, attempt.id)
        assert stored.evidence_status == "complete"
        assert stored.actual_gpu == "RTX4090"
        assert stored.execution_ms == 30_000
        assert stored.hourly_rate_micro_usd == 700_000
        assert stored.estimated_compute_micro_usd == 5833
        assert stored.model_identity == "acestep-v15-xl-turbo"
        assert stored.runtime_image_identity == "sha256:" + "b" * 64

    def test_failed_poll_without_rate_records_unavailable_not_zero(self, session) -> None:
        from ace_service.runpod_client import RunpodState, RunpodStatusResult
        from ace_service.worker import _record_poll_evidence

        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        session.commit()
        result = RunpodStatusResult(
            job_id="runpod-2",
            category=RunpodState.FAILED,
            raw_status="FAILED",
            execution_ms=5_000,
        )
        _record_poll_evidence(session, attempt.id, result, None)
        session.commit()
        from ace_service.models import VariationAttempt

        stored = session.get(VariationAttempt, attempt.id)
        assert stored.evidence_status == "unavailable"
        assert stored.execution_ms == 5_000
        assert stored.unavailable_reason == "worker_no_evidence"
        assert stored.estimated_compute_micro_usd is None  # never invented

    def test_poll_with_missing_execution_time_is_timing_unavailable(self, session) -> None:
        from ace_service.runpod_client import RunpodState, RunpodStatusResult
        from ace_service.worker import _record_poll_evidence

        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=1)
        attempt = create_variation_attempt(session, job_id=job.id, variation_index=1)
        transition_variation_attempt(session, attempt.id, JobStatus.CLOUD_QUEUED)
        session.commit()
        result = RunpodStatusResult(
            job_id="runpod-3",
            category=RunpodState.COMPLETED,
            raw_status="COMPLETED",
            execution_ms=None,
        )
        _record_poll_evidence(session, attempt.id, result, None)
        session.commit()
        from ace_service.models import VariationAttempt

        stored = session.get(VariationAttempt, attempt.id)
        assert stored.evidence_status == "unavailable"
        assert stored.unavailable_reason == "timing_unavailable"

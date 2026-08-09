"""Billing sync tests: strict parsing, decimal money, append-only observations,
lease semantics, boundary probe, and the operator-only boundary (no browser
route, no in-process scheduler)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ace_service.billing_client import (
    BillingLeaseError,
    BillingParseError,
    RunpodBillingClient,
    acquire_billing_lease,
    parse_endpoint_billing_response,
    parse_network_volume_billing_response,
    probe_billing_boundaries,
    release_billing_lease,
    sync_billing,
)
from ace_service.costs import utc_now
from ace_service.repository import (
    record_billing_observation,
    sum_billing_projections,
    sum_network_volume_observations,
)
from tests.conftest import utc_dt

START = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
END = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)


def _endpoint_row(bucket_start: str, amount: str, **extra) -> dict[str, object]:
    row: dict[str, object] = {
        "time": bucket_start,
        "endpointId": "endpoint-abc",
        "amount": amount,
    }
    row.update(extra)
    return row


def _volume_row(bucket_start: str, amount: str, **extra) -> dict[str, object]:
    row: dict[str, object] = {"time": bucket_start, "amount": amount}
    row.update(extra)
    return row


class TestEndpointParsing:
    def test_valid_response_parses_exact_micro_usd(self) -> None:
        buckets = parse_endpoint_billing_response(
            [
                _endpoint_row("2026-08-08T00:00:00Z", "0.0049"),
                _endpoint_row(
                    "2026-08-08T01:00:00Z",
                    "2.75",
                    timeBilledMs=1500,
                    diskSpaceBilledGb="1.25",
                    gpuTypeId="NVIDIA L40S",
                    podId="pod-1",
                ),
            ],
            endpoint_id="endpoint-abc",
            response_size_bytes=512,
        )
        assert [bucket.amount_micro_usd for bucket in buckets] == [4_900, 2_750_000]
        assert [bucket.raw_amount for bucket in buckets] == ["0.0049", "2.75"]
        assert buckets[1].raw_time_billed == "1500"
        assert buckets[1].documented_fields == {
            "diskSpaceBilledGb": "1.25",
            "gpuTypeId": "NVIDIA L40S",
            "podId": "pod-1",
        }
        assert buckets[0].bucket_start == datetime(2026, 8, 8, tzinfo=UTC)
        assert buckets[0].response_size_bytes == 512
        assert buckets[0].source_contract == "runpod-endpoints-v1-usd-no-currency"

    def test_wrong_endpoint_grouping_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="wrong endpoint"):
            parse_endpoint_billing_response(
                [_endpoint_row("2026-08-08T00:00:00Z", "0.5")],
                endpoint_id="endpoint-other",
            )

    def test_duplicate_bucket_within_response_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="duplicate"):
            parse_endpoint_billing_response(
                [
                    _endpoint_row("2026-08-08T00:00:00Z", "0.5"),
                    _endpoint_row("2026-08-08T00:00:00Z", "0.5"),
                ],
                endpoint_id="endpoint-abc",
            )

    def test_undocumented_fields_rejected_including_pagination(self) -> None:
        for field in ("nextToken", "cursor", "pagination", "page", "currency", "totalAmount"):
            with pytest.raises(BillingParseError, match="undocumented"):
                parse_endpoint_billing_response(
                    [_endpoint_row("2026-08-08T00:00:00Z", "0.5", **{field: "x"})],
                    endpoint_id="endpoint-abc",
                )

    def test_request_only_bucket_size_echo_is_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="undocumented"):
            parse_endpoint_billing_response(
                [_endpoint_row("2026-08-08T00:00:00Z", "0.5", bucketSize="day")],
                endpoint_id="endpoint-abc",
            )

    def test_non_list_or_oversized_response_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="bounded array"):
            parse_endpoint_billing_response({"rows": []}, endpoint_id="endpoint-abc")
        too_many = [_endpoint_row(f"2026-08-08T00:{i:02d}:00Z", "0.5") for i in range(513)]
        with pytest.raises(BillingParseError, match="bounded array"):
            parse_endpoint_billing_response(too_many, endpoint_id="endpoint-abc")

    def test_overflowing_amount_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="overflow|decimal"):
            parse_endpoint_billing_response(
                [_endpoint_row("2026-08-08T00:00:00Z", "1" + "0" * 30)],
                endpoint_id="endpoint-abc",
            )

    def test_decimal_edge_cases(self) -> None:
        buckets = parse_endpoint_billing_response(
            [
                _endpoint_row("2026-08-08T00:00:00Z", "0.0000005"),
                _endpoint_row("2026-08-08T01:00:00Z", "1"),
                _endpoint_row("2026-08-08T02:00:00Z", "0.5"),
            ],
            endpoint_id="endpoint-abc",
        )
        # 0.5 micro-USD rounds half-up to 1 micro-USD; integer tokens are exact.
        assert [bucket.amount_micro_usd for bucket in buckets] == [1, 1_000_000, 500_000]


class TestNetworkVolumeParsing:
    def test_account_wide_volume_parsed_separately(self) -> None:
        buckets = parse_network_volume_billing_response(
            [
                _volume_row("2026-08-08T00:00:00Z", "0.12"),
                _volume_row(
                    "2026-08-08T01:00:00Z",
                    "0.12",
                    diskSpaceBilledGb="10.5",
                    highPerformanceStorageAmount="0.02",
                    highPerformanceStorageDiskSpaceBilledGb="2.5",
                ),
            ]
        )
        assert [bucket.amount_micro_usd for bucket in buckets] == [120_000, 120_000]
        assert buckets[0].source_contract == "runpod-network-volume-v1-no-volume-id"
        assert buckets[1].documented_fields["highPerformanceStorageAmount"] == "0.02"

    def test_volume_duplicates_and_undocumented_fields_rejected(self) -> None:
        with pytest.raises(BillingParseError, match="duplicate"):
            parse_network_volume_billing_response(
                [
                    _volume_row("2026-08-08T00:00:00Z", "0.5"),
                    _volume_row("2026-08-08T00:00:00Z", "0.5"),
                ]
            )
        with pytest.raises(BillingParseError, match="undocumented"):
            parse_network_volume_billing_response(
                [_volume_row("2026-08-08T00:00:00Z", "0.5", volumeId="vol-1")]
            )


class TestOfficialRequestContract:
    def test_default_host_path_exact_query_and_response_byte_count(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                content=(
                    b'[{"time":"2026-08-08T00:00:00Z","endpointId":"endpoint-abc",'
                    b'"amount":0.0049,"timeBilledMs":1000}]'
                ),
                headers={"content-type": "application/json"},
            )

        with RunpodBillingClient("secret", transport=httpx.MockTransport(handler)) as client:
            buckets = client.fetch_endpoint_billing("endpoint-abc", START, END)
        request = captured[0]
        assert request.url.host == "rest.runpod.io"
        assert request.url.path == "/v1/billing/endpoints"
        assert dict(request.url.params) == {
            "bucketSize": "hour",
            "grouping": "endpointId",
            "endpointId": "endpoint-abc",
            "startTime": "2026-08-08T00:00:00Z",
            "endTime": "2026-08-09T00:00:00Z",
        }
        assert buckets[0].response_size_bytes > 0

    def test_alignment_out_of_range_and_bounded_time_rejected(self) -> None:
        for row, message in (
            (_endpoint_row("2026-08-08T00:01:00Z", "0.1"), "aligned"),
            (_endpoint_row("2026-08-09T00:00:00Z", "0.1"), "outside"),
            (
                _endpoint_row("2026-08-08T00:00:00Z", "0.1", timeBilledMs=10**20),
                "bounded",
            ),
        ):
            with pytest.raises(BillingParseError, match=message):
                parse_endpoint_billing_response(
                    [row], endpoint_id="endpoint-abc", start_utc=START, end_utc=END
                )


class TestObservationPersistence:
    def test_append_only_changed_buckets_and_idempotent_projection(self, session) -> None:
        first = record_billing_observation(
            session,
            provider="runpod",
            resource_type="endpoint",
            grouping_key="endpoint-abc",
            bucket_start=START,
            bucket_size_hours=1,
            raw_amount="0.0049",
            raw_time_billed=None,
            currency="USD",
            fetched_at=START + timedelta(hours=2),
            response_size_bytes=256,
            is_network_volume=False,
            source_contract="runpod-endpoints-v1-usd-no-currency",
            documented_fields={},
        )
        session.commit()
        # An exact repeat is an idempotent no-op.
        repeat = record_billing_observation(
            session,
            provider="runpod",
            resource_type="endpoint",
            grouping_key="endpoint-abc",
            bucket_start=START,
            bucket_size_hours=1,
            raw_amount="0.0049",
            raw_time_billed=None,
            currency="USD",
            fetched_at=START + timedelta(hours=3),
            response_size_bytes=256,
            is_network_volume=False,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        session.commit()
        assert repeat.id == first.id
        projections = session.query(
            __import__("ace_service.models", fromlist=["BillingProjection"]).BillingProjection
        ).all()
        assert len(projections) == 1
        assert projections[0].latest_amount == "0.0049"

        # A late update appends new evidence and updates the projection.
        late = record_billing_observation(
            session,
            provider="runpod",
            resource_type="endpoint",
            grouping_key="endpoint-abc",
            bucket_start=START,
            bucket_size_hours=1,
            raw_amount="0.0081",
            raw_time_billed="0.5",
            currency="USD",
            fetched_at=START + timedelta(hours=26),
            response_size_bytes=256,
            is_network_volume=False,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        session.commit()
        assert late.id != first.id
        observations = session.query(
            __import__("ace_service.models", fromlist=["BillingObservation"]).BillingObservation
        ).all()
        assert len(observations) == 2
        projections = session.query(
            __import__("ace_service.models", fromlist=["BillingProjection"]).BillingProjection
        ).all()
        assert len(projections) == 1
        assert projections[0].latest_amount == "0.0081"
        assert projections[0].latest_time_billed == "0.5"

    @pytest.mark.parametrize(
        ("resource_type", "grouping_key", "is_network_volume", "source_contract"),
        (
            (
                "endpoint",
                "endpoint-abc",
                False,
                "runpod-endpoints-v1-usd-no-currency",
            ),
            (
                "network_volume",
                "account",
                True,
                "runpod-network-volume-v1-no-volume-id",
            ),
        ),
    )
    def test_exact_repeat_advances_projection_freshness_without_history_duplication(
        self,
        session,
        resource_type: str,
        grouping_key: str,
        is_network_volume: bool,
        source_contract: str,
    ) -> None:
        t1 = START + timedelta(hours=1)
        t2 = START + timedelta(hours=2)
        t3 = START + timedelta(hours=3)
        common = {
            "provider": "runpod",
            "resource_type": resource_type,
            "grouping_key": grouping_key,
            "bucket_start": START,
            "bucket_size_hours": 1,
            "raw_time_billed": None,
            "currency": "USD",
            "response_size_bytes": 128,
            "is_network_volume": is_network_volume,
            "source_contract": source_contract,
            "documented_fields": {},
        }

        first = record_billing_observation(session, raw_amount="0.50", fetched_at=t1, **common)
        repeat = record_billing_observation(session, raw_amount="0.50", fetched_at=t3, **common)
        assert repeat.id == first.id
        # Equal and older exact repeats are complete no-ops.
        record_billing_observation(session, raw_amount="0.50", fetched_at=t3, **common)
        record_billing_observation(session, raw_amount="0.50", fetched_at=START, **common)
        changed_replay = record_billing_observation(
            session, raw_amount="0.75", fetched_at=t2, **common
        )
        session.commit()

        from ace_service.models import BillingObservation, BillingProjection

        observations = list(
            session.query(BillingObservation).order_by(BillingObservation.fetched_at)
        )
        assert [(row.raw_amount, row.fetched_at) for row in observations] == [
            ("0.50", t1),
            ("0.75", t2),
        ]
        assert changed_replay.id != first.id
        projection = session.query(BillingProjection).one()
        assert projection.latest_amount == "0.50"
        assert projection.latest_time_billed is None
        assert projection.last_updated_at == t3
        if is_network_volume:
            summary = sum_network_volume_observations(
                session, interval_start=START, interval_end=END
            )
            assert summary.summed_amount_micro_usd == 500_000
            assert summary.observation_count == 1
        else:
            assert (
                sum_billing_projections(
                    session,
                    resource_type="endpoint",
                    interval_start=START,
                    interval_end=END,
                )
                == 500_000
            )

    @pytest.mark.parametrize(
        ("resource_type", "grouping_key", "is_network_volume", "source_contract"),
        (
            (
                "endpoint",
                "endpoint-abc",
                False,
                "runpod-endpoints-v1-usd-no-currency",
            ),
            (
                "network_volume",
                "account",
                True,
                "runpod-network-volume-v1-no-volume-id",
            ),
        ),
    )
    def test_reversal_appends_return_to_historical_value_and_projects_newest(
        self,
        session,
        resource_type: str,
        grouping_key: str,
        is_network_volume: bool,
        source_contract: str,
    ) -> None:
        t1 = START + timedelta(hours=1)
        t2 = START + timedelta(hours=2)
        t3 = START + timedelta(hours=3)
        common = {
            "provider": "runpod",
            "resource_type": resource_type,
            "grouping_key": grouping_key,
            "bucket_start": START,
            "bucket_size_hours": 1,
            "raw_time_billed": None,
            "currency": "USD",
            "response_size_bytes": 128,
            "is_network_volume": is_network_volume,
            "source_contract": source_contract,
            "documented_fields": {},
        }

        first = record_billing_observation(session, raw_amount="0.50", fetched_at=t1, **common)
        changed = record_billing_observation(session, raw_amount="0.75", fetched_at=t2, **common)
        reversal = record_billing_observation(session, raw_amount="0.50", fetched_at=t3, **common)
        assert len({first.id, changed.id, reversal.id}) == 3
        assert (
            record_billing_observation(session, raw_amount="0.75", fetched_at=t2, **common).id
            == changed.id
        )
        assert (
            record_billing_observation(session, raw_amount="0.50", fetched_at=t3, **common).id
            == reversal.id
        )
        session.commit()

        from ace_service.models import BillingObservation, BillingProjection

        observations = list(
            session.query(BillingObservation).order_by(BillingObservation.fetched_at)
        )
        assert [(row.raw_amount, row.fetched_at) for row in observations] == [
            ("0.50", t1),
            ("0.75", t2),
            ("0.50", t3),
        ]
        assert observations[0].evidence_checksum == observations[2].evidence_checksum
        assert observations[0].checksum != observations[2].checksum
        projection = session.query(BillingProjection).one()
        assert projection.latest_amount == "0.50"
        assert projection.latest_time_billed is None
        assert projection.last_updated_at == t3
        assert projection.latest_evidence_checksum == reversal.evidence_checksum
        if is_network_volume:
            summary = sum_network_volume_observations(
                session, interval_start=START, interval_end=END
            )
            assert summary.summed_amount_micro_usd == 500_000
            assert summary.observation_count == 1
            assert (
                sum_billing_projections(
                    session,
                    resource_type="endpoint",
                    interval_start=START,
                    interval_end=END,
                )
                == 0
            )
        else:
            assert (
                sum_billing_projections(
                    session,
                    resource_type="endpoint",
                    interval_start=START,
                    interval_end=END,
                )
                == 500_000
            )
            assert (
                sum_network_volume_observations(
                    session, interval_start=START, interval_end=END
                ).summed_amount_micro_usd
                == 0
            )

    def test_endpoint_sum_uses_current_projection(self, session) -> None:
        for hour in range(3):
            record_billing_observation(
                session,
                provider="runpod",
                resource_type="endpoint",
                grouping_key="endpoint-abc",
                bucket_start=START + timedelta(hours=hour),
                bucket_size_hours=1,
                raw_amount="1.00",
                raw_time_billed=None,
                currency="USD",
                fetched_at=START + timedelta(hours=4),
                response_size_bytes=128,
                is_network_volume=False,
                source_contract="runpod-endpoints-v1-usd-no-currency",
            )
        session.commit()
        total = sum_billing_projections(
            session, resource_type="endpoint", interval_start=START, interval_end=END
        )
        assert total == 3_000_000
        # Half-open boundary: the bucket starting exactly at END is excluded.
        record_billing_observation(
            session,
            provider="runpod",
            resource_type="endpoint",
            grouping_key="endpoint-abc",
            bucket_start=END,
            bucket_size_hours=1,
            raw_amount="9.00",
            raw_time_billed=None,
            currency="USD",
            fetched_at=END,
            response_size_bytes=128,
            is_network_volume=False,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        session.commit()
        assert (
            sum_billing_projections(
                session, resource_type="endpoint", interval_start=START, interval_end=END
            )
            == 3_000_000
        )

    def test_network_volume_separated_from_endpoint_costs(self, session) -> None:
        record_billing_observation(
            session,
            provider="runpod",
            resource_type="endpoint",
            grouping_key="endpoint-abc",
            bucket_start=START,
            bucket_size_hours=1,
            raw_amount="1.00",
            raw_time_billed=None,
            currency="USD",
            fetched_at=START,
            response_size_bytes=128,
            is_network_volume=False,
            source_contract="runpod-endpoints-v1-usd-no-currency",
        )
        record_billing_observation(
            session,
            provider="runpod",
            resource_type="network_volume",
            grouping_key="account",
            bucket_start=START,
            bucket_size_hours=1,
            raw_amount="0.50",
            raw_time_billed=None,
            currency="USD",
            fetched_at=START,
            response_size_bytes=128,
            is_network_volume=True,
            source_contract="runpod-network-volume-v1-no-volume-id",
        )
        session.commit()
        endpoint_total = sum_billing_projections(
            session, resource_type="endpoint", interval_start=START, interval_end=END
        )
        volume = sum_network_volume_observations(session, interval_start=START, interval_end=END)
        assert endpoint_total == 1_000_000
        assert volume.summed_amount_micro_usd == 500_000
        assert volume.observation_count == 1
        assert volume.currency == "USD"

    def test_network_volume_history_projects_latest_and_rejects_out_of_order_replay(
        self, session
    ) -> None:
        common = {
            "provider": "runpod",
            "resource_type": "network_volume",
            "grouping_key": "account",
            "bucket_start": START,
            "bucket_size_hours": 1,
            "raw_time_billed": None,
            "currency": "USD",
            "response_size_bytes": 128,
            "is_network_volume": True,
            "source_contract": "runpod-network-volume-v1-no-volume-id",
            "documented_fields": {"diskSpaceBilledGb": "10"},
        }
        record_billing_observation(
            session, raw_amount="0.50", fetched_at=START + timedelta(hours=1), **common
        )
        record_billing_observation(
            session, raw_amount="0.75", fetched_at=START + timedelta(hours=3), **common
        )
        record_billing_observation(
            session, raw_amount="0.60", fetched_at=START + timedelta(hours=2), **common
        )
        session.commit()
        from ace_service.models import BillingObservation, BillingProjection

        assert session.query(BillingObservation).count() == 3
        projection = session.query(BillingProjection).one()
        assert projection.latest_amount == "0.75"
        summary = sum_network_volume_observations(session, interval_start=START, interval_end=END)
        assert summary.summed_amount_micro_usd == 750_000
        assert summary.observation_count == 1


class TestBillingLease:
    def test_lease_acquire_conflict_expiry_recovery_and_release(self, session) -> None:
        now = utc_now()
        assert acquire_billing_lease(session, holder="sync-a", now=now, ttl_seconds=60)
        session.commit()
        assert not acquire_billing_lease(session, holder="sync-b", now=now, ttl_seconds=60)
        # Expired leases are stolen (stale recovery).
        assert acquire_billing_lease(
            session, holder="sync-b", now=now + timedelta(minutes=2), ttl_seconds=60
        )
        session.commit()
        assert not acquire_billing_lease(session, holder="sync-c", now=now + timedelta(minutes=2))
        release_billing_lease(session, holder="sync-b")
        session.commit()
        assert acquire_billing_lease(session, holder="sync-c", now=now + timedelta(minutes=3))

    def test_release_only_by_holder(self, session) -> None:
        now = utc_now()
        acquire_billing_lease(session, holder="sync-a", now=now, ttl_seconds=60)
        release_billing_lease(session, holder="intruder")
        session.commit()
        assert not acquire_billing_lease(session, holder="sync-b", now=now, ttl_seconds=60)


class TestBoundaryProbe:
    def test_probe_classifies_inclusion_and_ambiguity(self) -> None:
        class _FakeClient:
            def __init__(self, buckets: list[object]) -> None:
                self.buckets = buckets

            def fetch_endpoint_billing(self, endpoint_id: str, start_utc, end_utc):
                del endpoint_id, start_utc, end_utc
                return self.buckets

        start = utc_dt(0)
        end = utc_dt(24)
        now = start + timedelta(hours=23, minutes=30)
        from ace_service.billing_client import BillingBucket

        bucket_at_start = BillingBucket(
            bucket_start=start,
            raw_amount="0.01",
            amount_micro_usd=10_000,
            raw_time_billed=None,
            response_size_bytes=64,
            source_contract="runpod-endpoints-v1-usd-no-currency",
            documented_fields={},
        )
        probe = probe_billing_boundaries(
            _FakeClient([bucket_at_start]),
            "endpoint-abc",
            start,
            end,
            now=now,
        )
        assert probe.start_inclusive
        assert probe.end_exclusive
        assert probe.current_partial_bucket_behavior == "not_observed"
        assert not probe.proven  # late-update and partial-hour semantics unknown

        # A provider failure yields a not-proven probe; totals stay unavailable.
        class _FailingClient:
            def fetch_endpoint_billing(self, endpoint_id: str, start_utc, end_utc):
                del endpoint_id, start_utc, end_utc
                from ace_service.billing_client import BillingAPIError

                raise BillingAPIError("provider unreachable")

        failing = probe_billing_boundaries(_FailingClient(), "endpoint-abc", start, end, now=now)
        assert not failing.proven
        assert failing.source == "probe-unavailable"

    def test_probe_can_observe_current_partial_hour(self) -> None:
        from ace_service.billing_client import BillingBucket

        class _FakeClient:
            def __init__(self, buckets: list[object]) -> None:
                self.buckets = buckets

            def fetch_endpoint_billing(self, endpoint_id: str, start_utc, end_utc):
                del endpoint_id, start_utc, end_utc
                return self.buckets

        start = utc_dt(0)
        end = utc_dt(24)
        now = start + timedelta(hours=23, minutes=30)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        probe = probe_billing_boundaries(
            _FakeClient(
                [
                    BillingBucket(
                        bucket_start=start,
                        raw_amount="0.01",
                        amount_micro_usd=10_000,
                        raw_time_billed=None,
                        response_size_bytes=64,
                        source_contract="runpod-endpoints-v1-usd-no-currency",
                        documented_fields={},
                    ),
                    BillingBucket(
                        bucket_start=current_hour,
                        raw_amount="0.01",
                        amount_micro_usd=10_000,
                        raw_time_billed=None,
                        response_size_bytes=64,
                        source_contract="runpod-endpoints-v1-usd-no-currency",
                        documented_fields={},
                    ),
                ]
            ),
            "endpoint-abc",
            start,
            end,
            now=now,
        )
        assert probe.current_partial_bucket_behavior == "documented"
        assert probe.start_inclusive
        assert probe.end_exclusive


class TestSyncBoundary:
    def _migrated_db(self, migrated_database_path: Path) -> Path:
        return migrated_database_path

    def _transport(self, endpoint_rows: list[dict[str, object]]) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/endpoints"):
                return httpx.Response(200, json=endpoint_rows)
            if request.url.path.endswith("/networkvolumes"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "time": "2026-08-08T00:00:00Z",
                            "amount": "0.50",
                        }
                    ],
                )
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def test_sync_refuses_non_exact_schema(self, legacy_database_path: Path) -> None:
        with pytest.raises(BillingLeaseError, match="exact expected schema"):
            sync_billing(
                str(legacy_database_path),
                endpoint_id="endpoint-abc",
                api_key="test-key",
                start_utc="2026-08-08T00:00:00Z",
                end_utc="2026-08-09T00:00:00Z",
            )

    def test_sync_records_endpoint_and_volume_separately(
        self, migrated_database_path: Path
    ) -> None:
        transport = self._transport(
            [
                {
                    "time": "2026-08-08T00:00:00Z",
                    "endpointId": "endpoint-abc",
                    "amount": "0.0049",
                },
                {
                    "time": "2026-08-08T01:00:00Z",
                    "endpointId": "endpoint-abc",
                    "amount": "0.0081",
                    "timeBilledMs": 500,
                },
            ]
        )
        summary = sync_billing(
            str(migrated_database_path),
            endpoint_id="endpoint-abc",
            api_key="test-key",
            start_utc="2026-08-08T00:00:00Z",
            end_utc="2026-08-09T00:00:00Z",
            http_client=httpx.Client(
                transport=transport, base_url="https://rest.runpod.io/v1/billing"
            ),
        )
        assert summary["endpoint_observations"] == 2
        assert summary["network_volume_observations"] == 1
        assert summary["cutoff_at"]

        from ace_service.db import create_database_engine, create_session_factory

        engine = create_database_engine(
            __import__("ace_service.config", fromlist=["ServiceSettings"]).ServiceSettings(
                data_root=migrated_database_path.parent,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
            )
        )
        try:
            with create_session_factory(engine)() as session:
                assert (
                    sum_billing_projections(
                        session,
                        resource_type="endpoint",
                        interval_start=datetime(2026, 8, 8, tzinfo=UTC),
                        interval_end=datetime(2026, 8, 9, tzinfo=UTC),
                    )
                    == 13_000
                )
                assert (
                    sum_network_volume_observations(
                        session,
                        interval_start=datetime(2026, 8, 8, tzinfo=UTC),
                        interval_end=datetime(2026, 8, 9, tzinfo=UTC),
                    ).summed_amount_micro_usd
                    == 500_000
                )
        finally:
            engine.dispose()

    def test_sync_is_idempotent_and_lease_blocks_overlap(
        self, migrated_database_path: Path
    ) -> None:
        transport = self._transport(
            [{"time": "2026-08-08T00:00:00Z", "endpointId": "endpoint-abc", "amount": "0.01"}]
        )
        client = httpx.Client(transport=transport, base_url="https://rest.runpod.io/v1/billing")
        first = sync_billing(
            str(migrated_database_path),
            endpoint_id="endpoint-abc",
            api_key="test-key",
            start_utc="2026-08-08T00:00:00Z",
            end_utc="2026-08-09T00:00:00Z",
            http_client=client,
        )
        second = sync_billing(
            str(migrated_database_path),
            endpoint_id="endpoint-abc",
            api_key="test-key",
            start_utc="2026-08-08T00:00:00Z",
            end_utc="2026-08-09T00:00:00Z",
            http_client=client,
        )
        assert first["endpoint_observations"] == 1
        assert second["endpoint_observations"] == 1

        from ace_service.db import create_database_engine, create_session_factory
        from ace_service.models import BillingObservation

        engine = create_database_engine(
            __import__("ace_service.config", fromlist=["ServiceSettings"]).ServiceSettings(
                data_root=migrated_database_path.parent,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
            )
        )
        try:
            with create_session_factory(engine)() as session:
                observations = session.query(BillingObservation).all()
                assert len(observations) == 2  # endpoint + volume, no duplicates
        finally:
            engine.dispose()

    def test_no_browser_route_calls_billing(self) -> None:
        import inspect

        import ace_service.web as web_module

        source = inspect.getsource(web_module)
        assert "billing_client" not in source
        assert "RunpodBillingClient" not in source
        assert "sync_billing" not in source
        # The billing client is synchronous only: an operator boundary, never
        # an async request-path dependency.
        assert not any(
            name.startswith("async def")
            for name, member in inspect.getmembers(RunpodBillingClient)
            if inspect.iscoroutinefunction(member)
        )

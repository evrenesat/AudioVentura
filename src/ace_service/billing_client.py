"""Operator-only Runpod billing boundary: strict parsers, sync client, lease.

The product controller never calls billing from a browser route and never
starts an in-process scheduler; ``billing-sync`` is an explicit operator
command backed by a database singleton lease with stale recovery.  Parsing
fails closed on undocumented shapes, pagination/continuation fields,
duplicate keys, and overflow, and network-volume evidence is kept separate
from endpoint costs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import sqlalchemy
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ace_service.costs import parse_micro_usd_decimal, utc_now
from ace_service.db import create_session_factory
from ace_service.migrations import CURRENT_SCHEMA_VERSION, migration_status
from ace_service.models import BillingLease
from ace_service.repository import record_billing_observation

LOGGER = logging.getLogger(__name__)

MAX_BILLING_ROWS = 512
_ENDPOINT_ROW_KEYS = frozenset(
    {"time", "endpointId", "amount", "timeBilledMs", "diskSpaceBilledGb", "gpuTypeId", "podId"}
)
_NETWORK_ROW_KEYS = frozenset(
    {
        "time",
        "amount",
        "diskSpaceBilledGb",
        "highPerformanceStorageAmount",
        "highPerformanceStorageDiskSpaceBilledGb",
    }
)
_SOURCE_CONTRACT_ENDPOINT = "runpod-endpoints-v1-usd-no-currency"
_SOURCE_CONTRACT_NETWORK = "runpod-network-volume-v1-no-volume-id"


class BillingParseError(ValueError):
    """Raised when a provider billing response is outside the documented contract."""


class BillingLeaseError(RuntimeError):
    """Raised when the singleton billing sync lease cannot be acquired."""


class BillingAPIError(RuntimeError):
    """Raised when the provider rejects or cannot complete a billing request."""


@dataclass(frozen=True, slots=True)
class BillingBucket:
    """One validated endpoint billing bucket with exact decimal evidence."""

    bucket_start: datetime
    raw_amount: str
    amount_micro_usd: int
    raw_time_billed: str | None
    response_size_bytes: int
    source_contract: str
    documented_fields: dict[str, str]


@dataclass(frozen=True, slots=True)
class NetworkVolumeBucket:
    """One validated account-wide network-volume bucket."""

    bucket_start: datetime
    raw_amount: str
    amount_micro_usd: int
    raw_time_billed: str | None
    response_size_bytes: int
    source_contract: str
    documented_fields: dict[str, str]


def _parse_provider_bucket_start(row: Mapping[str, Any]) -> datetime:
    value = row.get("time")
    if not isinstance(value, str) or not value.strip():
        raise BillingParseError("provider billing bucket start is missing")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BillingParseError("provider billing bucket start is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BillingParseError("provider billing bucket start must include a UTC offset")
    return parsed.astimezone(UTC)


def _parse_amount(row: Mapping[str, Any]) -> tuple[str, int]:
    amount = row.get("amount")
    if amount is None:
        raise BillingParseError("provider billing row is missing amount")
    try:
        return parse_micro_usd_decimal(amount, field_name="billing.amount")
    except ValueError as exc:
        raise BillingParseError(str(exc)) from exc


def _parse_time_billed_ms(row: Mapping[str, Any]) -> str | None:
    time_billed = row.get("timeBilledMs")
    if time_billed is None:
        return None
    if isinstance(time_billed, bool) or not isinstance(time_billed, int) or time_billed < 0:
        raise BillingParseError("billing.timeBilledMs must be a non-negative integer")
    if time_billed > 31 * 24 * 3_600_000:
        raise BillingParseError("billing.timeBilledMs exceeds the bounded limit")
    return str(time_billed)


def _decimal_field(row: Mapping[str, Any], name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    try:
        raw, _ = parse_micro_usd_decimal(value, field_name=f"billing.{name}")
    except ValueError as exc:
        raise BillingParseError(str(exc)) from exc
    return raw


def _text_field(row: Mapping[str, Any], name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BillingParseError(f"billing.{name} must be bounded non-empty text")
    return value.strip()


def _validate_bucket_bounds(
    bucket_start: datetime, *, start_utc: datetime | None, end_utc: datetime | None
) -> None:
    if bucket_start.minute or bucket_start.second or bucket_start.microsecond:
        raise BillingParseError("provider billing bucket is not aligned to an hour")
    if (start_utc is None) != (end_utc is None):
        raise BillingParseError("billing response bounds must be supplied together")
    if start_utc is not None and end_utc is not None:
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise BillingParseError("billing response bounds must be timezone-aware")
        if not start_utc <= bucket_start < end_utc:
            raise BillingParseError("provider billing bucket is outside the requested interval")


def parse_endpoint_billing_response(
    body: Any,
    *,
    endpoint_id: str,
    response_size_bytes: int | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> tuple[BillingBucket, ...]:
    """Strictly parse the documented endpoint billing array.

    USD is the documented source-contract value, never a provider-returned
    currency field; the response must be a bounded array of hourly buckets
    grouped by the exact requested endpoint.  Any undocumented key
    (pagination/continuation tokens included), duplicate bucket, wrong
    endpoint, or overflow fails closed.
    """

    if not isinstance(endpoint_id, str) or not endpoint_id.strip():
        raise BillingParseError("endpoint_id must be non-empty text")
    if not isinstance(body, list) or len(body) > MAX_BILLING_ROWS:
        raise BillingParseError("endpoint billing response must be a bounded array")
    buckets: list[BillingBucket] = []
    seen: set[datetime] = set()
    for row in body:
        if not isinstance(row, Mapping):
            raise BillingParseError("endpoint billing row must be an object")
        unknown_keys = set(row) - _ENDPOINT_ROW_KEYS
        if unknown_keys:
            raise BillingParseError(
                f"endpoint billing row contains undocumented fields: {sorted(unknown_keys)}"
            )
        returned_endpoint = row.get("endpointId", row.get("endpoint_id"))
        if returned_endpoint != endpoint_id:
            raise BillingParseError("endpoint billing row has the wrong endpoint grouping")
        bucket_start = _parse_provider_bucket_start(row)
        _validate_bucket_bounds(bucket_start, start_utc=start_utc, end_utc=end_utc)
        if bucket_start in seen:
            raise BillingParseError("endpoint billing response contains duplicate buckets")
        seen.add(bucket_start)
        raw_amount, amount_micro = _parse_amount(row)
        raw_time_billed = _parse_time_billed_ms(row)
        documented_fields = {
            key: value
            for key, value in {
                "diskSpaceBilledGb": _decimal_field(row, "diskSpaceBilledGb"),
                "gpuTypeId": _text_field(row, "gpuTypeId"),
                "podId": _text_field(row, "podId"),
            }.items()
            if value is not None
        }
        buckets.append(
            BillingBucket(
                bucket_start=bucket_start,
                raw_amount=raw_amount,
                amount_micro_usd=amount_micro,
                raw_time_billed=raw_time_billed,
                response_size_bytes=response_size_bytes if response_size_bytes is not None else 0,
                source_contract=_SOURCE_CONTRACT_ENDPOINT,
                documented_fields=documented_fields,
            )
        )
    return tuple(buckets)


def parse_network_volume_billing_response(
    body: Any,
    *,
    response_size_bytes: int | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> tuple[NetworkVolumeBucket, ...]:
    """Strictly parse account-wide network-volume billing as separate evidence.

    The documented network-volume response has no volume/grouping dimension,
    so buckets are account-wide: they are never allocated to jobs and never
    summed into service totals.
    """

    if not isinstance(body, list) or len(body) > MAX_BILLING_ROWS:
        raise BillingParseError("network-volume billing response must be a bounded array")
    buckets: list[NetworkVolumeBucket] = []
    seen: set[datetime] = set()
    for row in body:
        if not isinstance(row, Mapping):
            raise BillingParseError("network-volume billing row must be an object")
        unknown_keys = set(row) - _NETWORK_ROW_KEYS
        if unknown_keys:
            raise BillingParseError(
                f"network-volume billing row contains undocumented fields: {sorted(unknown_keys)}"
            )
        bucket_start = _parse_provider_bucket_start(row)
        _validate_bucket_bounds(bucket_start, start_utc=start_utc, end_utc=end_utc)
        if bucket_start in seen:
            raise BillingParseError("network-volume billing response contains duplicate buckets")
        seen.add(bucket_start)
        raw_amount, amount_micro = _parse_amount(row)
        documented_fields = {
            key: value
            for key in (
                "diskSpaceBilledGb",
                "highPerformanceStorageAmount",
                "highPerformanceStorageDiskSpaceBilledGb",
            )
            if (value := _decimal_field(row, key)) is not None
        }
        buckets.append(
            NetworkVolumeBucket(
                bucket_start=bucket_start,
                raw_amount=raw_amount,
                amount_micro_usd=amount_micro,
                raw_time_billed=None,
                response_size_bytes=response_size_bytes if response_size_bytes is not None else 0,
                source_contract=_SOURCE_CONTRACT_NETWORK,
                documented_fields=documented_fields,
            )
        )
    return tuple(buckets)


class RunpodBillingClient:
    """Synchronous operator-bound HTTP adapter for Runpod billing endpoints.

    Credentials are never logged; responses are size-bounded and parsed with a
    decimal-aware JSON decoder so monetary tokens never pass through floats.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://rest.runpod.io/v1/billing",
        timeout_seconds: float = 10,
        response_max_bytes: int = 1_048_576,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Runpod API credentials must not be empty")
        if not base_url.startswith("https://"):
            raise ValueError("Runpod billing base URL must use HTTPS")
        if response_max_bytes < 1024:
            raise ValueError("response_max_bytes must be at least 1024")
        self._response_max_bytes = response_max_bytes
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RunpodBillingClient:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _request_json(self, path: str, params: Mapping[str, str]) -> tuple[Any, int]:
        try:
            response = self._client.get(path, params=dict(params))
        except httpx.HTTPError as exc:
            raise BillingAPIError("Runpod billing request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise BillingAPIError(f"Runpod billing request failed with HTTP {response.status_code}")
        if len(response.content) > self._response_max_bytes:
            raise BillingAPIError("Runpod billing response exceeds the size limit")
        try:
            return response.json(parse_float=Decimal), len(response.content)
        except ValueError as exc:
            raise BillingAPIError("Runpod billing response is not valid JSON") from exc

    def fetch_endpoint_billing(
        self, endpoint_id: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[BillingBucket, ...]:
        """Fetch endpoint billing for one exact half-open UTC interval."""

        if not endpoint_id.strip():
            raise ValueError("endpoint_id must not be empty")
        body, response_size = self._request_json(
            "endpoints",
            {
                "endpointId": endpoint_id,
                "grouping": "endpointId",
                "bucketSize": "hour",
                "startTime": _iso(start_utc),
                "endTime": _iso(end_utc),
            },
        )
        return parse_endpoint_billing_response(
            body,
            endpoint_id=endpoint_id,
            response_size_bytes=response_size,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    def fetch_network_volume_billing(
        self, start_utc: datetime, end_utc: datetime
    ) -> tuple[NetworkVolumeBucket, ...]:
        """Fetch account-wide network-volume billing for one UTC interval."""

        body, response_size = self._request_json(
            "networkvolumes",
            {
                "bucketSize": "hour",
                "startTime": _iso(start_utc),
                "endTime": _iso(end_utc),
            },
        )
        return parse_network_volume_billing_response(
            body,
            response_size_bytes=response_size,
            start_utc=start_utc,
            end_utc=end_utc,
        )


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("billing interval boundaries must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def acquire_billing_lease(
    session: Session,
    *,
    holder: str,
    now: datetime | None = None,
    ttl_seconds: int = 900,
) -> bool:
    """Acquire the singleton billing sync lease; steal an expired one.

    Returns False when another live holder owns the lease.  ``holder`` is a
    bounded non-secret operator/process identifier.
    """

    if not isinstance(holder, str) or not holder.strip() or len(holder) > 128:
        raise ValueError("lease holder must be bounded non-empty text")
    if ttl_seconds <= 0:
        raise ValueError("lease TTL must be positive")
    timestamp = now or utc_now()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("lease timestamps must be timezone-aware")
    # Bootstrap the singleton race-free: INSERT OR IGNORE is atomic even when
    # two first-time sync invocations start concurrently.
    session.execute(
        sqlalchemy.text("INSERT OR IGNORE INTO billing_lease (id, status) VALUES (1, 'free')")
    )
    session.flush()
    lease = session.get(BillingLease, 1)
    if lease is None:
        raise BillingLeaseError("billing lease singleton could not be initialized")
    if lease.status == "locked" and lease.expires_at is not None and lease.expires_at > timestamp:
        return False
    lease.status = "locked"
    lease.locked_by = holder.strip()
    lease.locked_at = timestamp
    from datetime import timedelta

    lease.expires_at = timestamp + timedelta(seconds=ttl_seconds)
    session.flush()
    return True


def release_billing_lease(session: Session, *, holder: str) -> None:
    """Release the singleton lease only when held by the named holder."""

    lease = session.get(BillingLease, 1)
    if lease is None or lease.status != "locked" or lease.locked_by != holder:
        return
    lease.status = "free"
    lease.locked_by = None
    lease.locked_at = None
    lease.expires_at = None
    session.flush()


@dataclass(frozen=True, slots=True)
class BoundaryEvidence:
    """Read-only classification of one provider interval boundary observation."""

    start_inclusive: bool
    end_exclusive: bool
    native_bucket_seconds: int
    empty_response_behavior: str
    current_partial_bucket_behavior: str
    late_update_behavior: str
    source: str

    @property
    def proven(self) -> bool:
        """Whether every semantic is documented; ambiguous evidence is not proven."""

        return (
            self.start_inclusive
            and self.end_exclusive
            and self.native_bucket_seconds == 3600
            and self.empty_response_behavior == "documented"
            and self.current_partial_bucket_behavior == "documented"
            and self.late_update_behavior == "documented"
        )


def probe_billing_boundaries(
    client: RunpodBillingClient,
    endpoint_id: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    now: datetime | None = None,
) -> BoundaryEvidence:
    """One read-only boundary probe; ambiguous semantics report as not proven.

    A single fetch can establish bucket start/end inclusion and a current
    partial hour only when the response actually contains the boundary
    buckets; empty-response and late-update semantics are recorded as
    ``unknown`` unless the fixture/probe provides explicit evidence.  Totals
    and reconciliation must stay unavailable while ``proven`` is false.
    """

    if now is None:
        now = utc_now()
    try:
        buckets = client.fetch_endpoint_billing(endpoint_id, start_utc, end_utc)
    except (BillingAPIError, BillingParseError):
        return BoundaryEvidence(
            start_inclusive=False,
            end_exclusive=False,
            native_bucket_seconds=3600,
            empty_response_behavior="unknown",
            current_partial_bucket_behavior="unknown",
            late_update_behavior="unknown",
            source="probe-unavailable",
        )
    starts = {bucket.bucket_start for bucket in buckets}
    start_inclusive = start_utc in starts
    end_exclusive = end_utc not in starts
    current_hour_start = now.replace(minute=0, second=0, microsecond=0)
    partial = current_hour_start in starts
    return BoundaryEvidence(
        start_inclusive=start_inclusive,
        end_exclusive=end_exclusive,
        native_bucket_seconds=3600,
        empty_response_behavior="unknown" if buckets else "documented",
        current_partial_bucket_behavior="documented" if partial else "not_observed",
        late_update_behavior="unknown",
        source="read-only-probe",
    )


def sync_billing(
    db_path: str,
    *,
    endpoint_id: str,
    api_key: str,
    start_utc: str,
    end_utc: str,
    request_timeout_seconds: float = 10,
    response_max_bytes: int = 1_048_576,
    lease_ttl_seconds: int = 900,
    price_max_age_hours: int = 24,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Operator-only idempotent billing sync: lease, fetch, record, release.

    Refuses to run unless the database is at the exact expected schema
    version, and never runs inside a browser route or an in-process
    scheduler.  Provider interval inclusion semantics are the caller's
    responsibility; this command only preserves native UTC buckets.
    """

    del price_max_age_hours
    report = migration_status(db_path)
    if report["state"] != "exact_expected":
        raise BillingLeaseError(
            "billing sync refuses to run: database is not at the exact expected schema "
            f"(state={report['state']}, expected={CURRENT_SCHEMA_VERSION})"
        )
    engine = create_database_engine_for_path(_resolve_engine_path(db_path))
    holder = f"billing-sync-{report['path_hash']}"
    try:
        with create_session_factory(engine)() as session:
            if not acquire_billing_lease(session, holder=holder, ttl_seconds=lease_ttl_seconds):
                raise BillingLeaseError("another billing sync invocation holds the lease")
            session.commit()
            try:
                start = _parse_interval(start_utc)
                end = _parse_interval(end_utc)
                if end <= start:
                    raise ValueError("billing interval end must follow start")
                with RunpodBillingClient(
                    api_key,
                    timeout_seconds=request_timeout_seconds,
                    response_max_bytes=response_max_bytes,
                    http_client=http_client,
                ) as client:
                    endpoint_buckets = client.fetch_endpoint_billing(endpoint_id, start, end)
                    volume_buckets = client.fetch_network_volume_billing(start, end)
                cutoff = utc_now()
                for endpoint_bucket in endpoint_buckets:
                    record_billing_observation(
                        session,
                        provider="runpod",
                        resource_type="endpoint",
                        grouping_key=endpoint_id,
                        bucket_start=endpoint_bucket.bucket_start,
                        bucket_size_hours=1,
                        raw_amount=endpoint_bucket.raw_amount,
                        raw_time_billed=endpoint_bucket.raw_time_billed,
                        currency="USD",
                        fetched_at=cutoff,
                        response_size_bytes=endpoint_bucket.response_size_bytes,
                        is_network_volume=False,
                        source_contract=endpoint_bucket.source_contract,
                        documented_fields=endpoint_bucket.documented_fields,
                    )
                for volume_bucket in volume_buckets:
                    record_billing_observation(
                        session,
                        provider="runpod",
                        resource_type="network_volume",
                        grouping_key="account",
                        bucket_start=volume_bucket.bucket_start,
                        bucket_size_hours=1,
                        raw_amount=volume_bucket.raw_amount,
                        raw_time_billed=volume_bucket.raw_time_billed,
                        currency="USD",
                        fetched_at=cutoff,
                        response_size_bytes=volume_bucket.response_size_bytes,
                        is_network_volume=True,
                        source_contract=volume_bucket.source_contract,
                        documented_fields=volume_bucket.documented_fields,
                    )
                session.commit()
                return {
                    "endpoint_observations": len(endpoint_buckets),
                    "network_volume_observations": len(volume_buckets),
                    "cutoff_at": cutoff.isoformat(),
                }
            except BaseException:
                session.rollback()
                raise
            finally:
                try:
                    release_billing_lease(session, holder=holder)
                    session.commit()
                except sqlalchemy.exc.SQLAlchemyError as exc:
                    LOGGER.error(
                        "stage=billing_sync error_code=lease_release_failed exception_class=%s",
                        type(exc).__name__,
                        extra={"component": "controller"},
                    )
    finally:
        engine.dispose()


def _resolve_engine_path(db_path: str) -> str:
    from pathlib import Path

    return str(Path(db_path).expanduser().resolve())


def create_database_engine_for_path(database_path: str) -> Engine:
    """Small engine factory used by the operator sync boundary."""

    url = sqlalchemy.engine.URL.create("sqlite+pysqlite", database=database_path)
    return sqlalchemy.create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )


def _parse_interval(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("billing interval boundaries must be ISO-8601 text")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("billing interval boundaries must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("billing interval boundaries must include a UTC offset")
    return parsed.astimezone(UTC)

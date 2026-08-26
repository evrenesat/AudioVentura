"""Exact-decimal cost primitives shared by quotes, attempts, and billing.

Money is never routed through binary floats.  Provider/rate inputs are
validated fixed-decimal text or exact integers, converted once to integer
micro-USD, and all estimates use the single centralized half-up formula.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

MICRO_USD_PER_USD = 1_000_000
MS_PER_HOUR = 3_600_000
MAX_AMOUNT_MICRO_USD = 10**15

# Read-only approximate cost display (owner decisions #5-#9).  The rate is a
# fixed exact integer micro-USD value; the display never gates, persists, or
# controls generation.
FIXED_GPU_HOURLY_RATE_MICRO_USD = 500_000  # USD 0.50/GPU-hour
NO_HISTORY_SEED_EXECUTION_MS = 60_000  # 60-second seed per variation
ESTIMATE_HISTORY_SAMPLE_LIMIT = 3
_ESTIMATE_DISPLAY_QUANTUM = Decimal("0.0001")  # 4-decimal USD presentation

_DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_FAL_PRICING_UNIT_ALIASES = {
    "seconds": "second",
    "compute seconds": "compute_second",
    "compute_seconds": "compute_second",
}

# Server-owned alias map: worker-reported GPU identities (torch device
# names) resolve to canonical catalog keys.  An unknown alias has no rate and
# makes the attempt estimate unavailable; it is never guessed.
GPU_ALIASES: dict[str, str] = {
    "NVIDIA GeForce RTX 4090": "RTX4090",
    "NVIDIA RTX 4090": "RTX4090",
    "RTX 4090": "RTX4090",
    "NVIDIA L40S": "L40S",
    "NVIDIA L4": "L4",
    "L4": "L4",
}


@dataclass(frozen=True, slots=True)
class FalPrice:
    """Read-only endpoint price evidence for the selector UI."""

    endpoint_id: str
    unit_price_micro_usd: int
    unit_price_usd: str
    unit: str
    fetched_at: datetime
    stale: bool = False
    total_micro_usd: int | None = None


class FalPricingClient:
    """Small bounded TTL cache for Fal's mutable pricing endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        ttl_seconds: int = 900,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or len(api_key) > 4096:
            raise ValueError("Fal pricing API key is invalid")
        if ttl_seconds <= 0:
            raise ValueError("Fal pricing TTL must be positive")
        self.api_key = api_key
        self.ttl = timedelta(seconds=ttl_seconds)
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self._cache: dict[str, tuple[FalPrice, datetime]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _record(body: Any, endpoint_id: str) -> Mapping[str, Any] | None:
        candidates: list[Any] = []
        if isinstance(body, Mapping):
            values = body.get("prices", body.get("models", body))
            candidates = values if isinstance(values, list) else [values]
        elif isinstance(body, list):
            candidates = body
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            candidate_id = candidate.get("endpoint_id", candidate.get("model_id"))
            if candidate_id == endpoint_id:
                return candidate
        return None

    async def get(self, endpoint_id: str) -> FalPrice | None:
        if not endpoint_id or len(endpoint_id) > 256 or any(char.isspace() for char in endpoint_id):
            raise ValueError("Fal pricing endpoint ID is invalid")
        now = utc_now()
        cached = self._cache.get(endpoint_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            response = await self.client.get(
                "https://api.fal.ai/v1/models/pricing",
                params={"endpoint_id": endpoint_id},
                headers={"Authorization": f"Key {self.api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
            response.raise_for_status()
            body = json.loads(response.content, parse_float=Decimal)
            record = self._record(body, endpoint_id)
            if record is None:
                raise ValueError("pricing endpoint was not returned")
            raw_price = record.get("unit_price", record.get("price"))
            unit_text = record.get("unit", record.get("billing_unit", "request"))
            if not isinstance(unit_text, str) or not unit_text or len(unit_text) > 64:
                raise ValueError("pricing unit is invalid")
            unit_text = _FAL_PRICING_UNIT_ALIASES.get(
                unit_text.strip().lower(), unit_text.strip().lower().replace(" ", "_")
            )
            price_text, price_micro = parse_micro_usd_decimal(
                raw_price, field_name="Fal unit price"
            )
            value = FalPrice(endpoint_id, price_micro, price_text, unit_text, now)
            self._cache[endpoint_id] = (value, now + self.ttl)
            return value
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            if cached is None:
                return None
            old = cached[0]
            return FalPrice(
                old.endpoint_id,
                old.unit_price_micro_usd,
                old.unit_price_usd,
                old.unit,
                old.fetched_at,
                stale=True,
            )

    async def estimate(
        self,
        endpoint_id: str,
        *,
        unit_quantity: Decimal | int,
        declared_unit: str | None,
    ) -> FalPrice | None:
        if isinstance(unit_quantity, bool):
            raise ValueError("pricing unit quantity must be positive")
        try:
            quantity = Decimal(unit_quantity)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("pricing unit quantity must be positive") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("pricing unit quantity must be positive")
        price = await self.get(endpoint_id)
        if price is None or declared_unit is None or price.unit != declared_unit:
            return price
        total = int(
            (Decimal(price.unit_price_micro_usd) * quantity).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        if total > MAX_AMOUNT_MICRO_USD:
            raise ValueError("pricing estimate exceeds the supported amount")
        return FalPrice(
            price.endpoint_id,
            price.unit_price_micro_usd,
            price.unit_price_usd,
            price.unit,
            price.fetched_at,
            price.stale,
            total,
        )


def utc_now() -> datetime:
    """Return an explicitly UTC-aware timestamp."""

    return datetime.now(UTC)


def round_half_up_compute_cost(execution_ms: int, hourly_rate_micro_usd: int) -> int:
    """Centralize ``round_half_up(execution_ms * rate_micro_usd / 3_600_000)``.

    Integer half-up arithmetic only; the hourly rate is already exact integer
    micro-USD, so no binary float ever touches the money.
    """

    if isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms < 0:
        raise ValueError("execution_ms must be a non-negative integer")
    if (
        isinstance(hourly_rate_micro_usd, bool)
        or not isinstance(hourly_rate_micro_usd, int)
        or hourly_rate_micro_usd < 0
    ):
        raise ValueError("hourly_rate_micro_usd must be a non-negative integer")
    numerator = execution_ms * hourly_rate_micro_usd
    return (numerator + MS_PER_HOUR // 2) // MS_PER_HOUR


def round_half_up_compute_cost_usd(execution_ms: int, hourly_rate_usd: Any) -> int:
    """Compute once from the preserved exact hourly-USD token."""

    if isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms < 0:
        raise ValueError("execution_ms must be a non-negative integer")
    raw_rate, _ = parse_micro_usd_decimal(hourly_rate_usd, field_name="hourly_rate_usd")
    amount = Decimal(execution_ms) * Decimal(raw_rate) * MICRO_USD_PER_USD / Decimal(MS_PER_HOUR)
    result = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result > MAX_AMOUNT_MICRO_USD:
        raise ValueError("computed amount overflows the micro-USD bound")
    return result


def apply_conservative_margin(
    execution_range_ms: tuple[int, int], conservative_margin: str
) -> tuple[int, int]:
    """Apply a persisted non-negative fractional margin without binary float."""

    low, high = execution_range_ms
    if low < 0 or high < low:
        raise ValueError("execution range must be ordered and non-negative")
    raw_margin, _ = parse_micro_usd_decimal(conservative_margin, field_name="conservative_margin")
    multiplier = Decimal("1") + Decimal(raw_margin)
    return (
        int((Decimal(low) * multiplier).quantize(Decimal("1"), rounding=ROUND_CEILING)),
        int((Decimal(high) * multiplier).quantize(Decimal("1"), rounding=ROUND_CEILING)),
    )


def parse_micro_usd_decimal(value: Any, *, field_name: str) -> tuple[str, int]:
    """Parse a JSON monetary token as exact decimal text and integer micro-USD.

    Accepts fixed-decimal strings, integers, or exact ``Decimal`` tokens
    (produced by a decimal-aware JSON decoder).  Floats and booleans are
    rejected so money can never enter through a binary round trip.  The
    returned text is the canonical fixed-decimal form of the original token.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a decimal string or integer")
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must be a decimal string")
    else:
        raise ValueError(f"{field_name} must be a decimal string or integer")
    if not _DECIMAL_RE.fullmatch(text):
        raise ValueError(f"{field_name} must use fixed non-negative decimal text")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    try:
        micro_usd = int((parsed * MICRO_USD_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} overflows the micro-USD bound") from exc
    if micro_usd > MAX_AMOUNT_MICRO_USD:
        raise ValueError(f"{field_name} overflows the micro-USD bound")
    return text, micro_usd


def build_cost_fingerprint(
    *,
    profile_id: str | None,
    duration_mode: str,
    duration_value_seconds: float | None,
    variation_count: int,
    eligible_gpu_ids: list[str],
    model_identity: str = "unknown",
) -> str:
    """Hash only the non-sensitive cost drivers of one submission quote.

    Prompt, lyrics, source URL, transfer capability, and the raw normalized
    request are never part of the fingerprint or any quote/billing record.
    """

    payload = {
        "profile_id": profile_id,
        "duration_mode": duration_mode,
        "duration_value_seconds": duration_value_seconds,
        "variation_count": variation_count,
        "eligible_gpu_ids": sorted(eligible_gpu_ids),
        "model_identity": model_identity,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_cost_fingerprint(value: str) -> bool:
    """Return whether a stored fingerprint is a canonical sha256 hex digest."""

    return isinstance(value, str) and bool(_FINGERPRINT_RE.fullmatch(value))


def resolve_gpu_alias(value: str | None) -> str | None:
    """Resolve a worker-reported GPU identity to a canonical catalog key."""

    if value is None:
        return None
    key = " ".join(value.strip().split())
    return GPU_ALIASES.get(key)


def observation_checksum(
    *,
    provider: str,
    resource_type: str,
    grouping_key: str,
    bucket_start: datetime,
    bucket_size_hours: int,
    raw_amount: str,
    raw_time_billed: str | None,
    currency: str,
    is_network_volume: bool,
    source_contract: str,
    documented_fields: Mapping[str, str] | None = None,
) -> str:
    """Deterministic sha256 over the evidence fields of one billing observation.

    Identical checksums identify equal evidence values. Durable fetch-event
    identity additionally includes ``fetched_at`` so A -> B -> A can retain
    both changes while exact retries of one changed event remain idempotent.
    """

    payload = {
        "provider": provider,
        "resource_type": resource_type,
        "grouping_key": grouping_key,
        "bucket_start": bucket_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "bucket_size_hours": bucket_size_hours,
        "raw_amount": raw_amount,
        "raw_time_billed": raw_time_billed,
        "currency": currency,
        "is_network_volume": is_network_volume,
        "source_contract": source_contract,
        "documented_fields": dict(sorted((documented_fields or {}).items())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def observation_event_checksum(*, evidence_checksum: str, fetched_at: datetime) -> str:
    """Return a durable identity for one value-change event at one fetch time."""

    payload = {
        "evidence_checksum": evidence_checksum,
        "fetched_at": fetched_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CostSummary:
    """Summed terminal-attempt estimates over one half-open UTC interval."""

    interval_start: datetime
    interval_end: datetime
    summed_estimate_micro_usd: int
    terminal_attempts: int
    attempts_with_estimate: int
    attempts_without_cost: int
    partial_coverage: bool

    @property
    def available(self) -> bool:
        return self.terminal_attempts > 0 and not self.partial_coverage


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Signed endpoint-level actual-vs-estimate reconciliation for one interval."""

    interval_start: datetime
    interval_end: datetime
    actual_endpoint_micro_usd: int | None
    summed_estimates_micro_usd: int | None
    delta_micro_usd: int | None
    coverage: str
    cutoff_at: datetime | None
    source_contract: str | None

    @property
    def available(self) -> bool:
        return (
            self.actual_endpoint_micro_usd is not None
            and self.summed_estimates_micro_usd is not None
            and self.delta_micro_usd is not None
            and self.coverage == "complete"
        )


@dataclass(frozen=True, slots=True)
class NetworkVolumeSummary:
    """Account-wide network-volume evidence, never attributed to the service."""

    interval_start: datetime
    interval_end: datetime
    summed_amount_micro_usd: int
    observation_count: int
    currency: str


@dataclass(frozen=True, slots=True)
class QuoteEstimate:
    """Server-computed submission quote before persistence."""

    cost_fingerprint: str
    model_identity: str
    profile_id: str | None
    duration_mode: str | None
    duration_value_seconds: float | None
    variation_count: int
    eligible_gpu_ids: list[str]
    highest_trusted_hourly_rate_micro_usd: int | None
    highest_trusted_hourly_rate_usd: str | None
    calibration_version: int | None
    predicted_execution_range_ms: tuple[int, int] | None
    quoted_amount_micro_usd: int | None
    quoted_range_low_micro_usd: int | None
    quoted_range_high_micro_usd: int | None
    currency: str
    rate_source: str | None
    rate_version: str | None
    unavailable_reason_code: str | None
    captured_at: datetime

    @property
    def available(self) -> bool:
        return self.unavailable_reason_code is None


def compute_submission_quote(
    *,
    profile_id: str | None,
    duration_mode: str | None,
    duration_value_seconds: float | None,
    variation_count: int,
    eligible_gpu_ids: list[str],
    fresh_rates: Mapping[str, int],
    stale_gpu_ids: set[str],
    calibration_version: int | None,
    predicted_execution_range_ms: tuple[int, int] | None,
    rate_source: str | None,
    rate_version: str | None,
    captured_at: datetime,
    model_identity: str = "unknown",
    fresh_rate_usd: Mapping[str, str] | None = None,
) -> QuoteEstimate:
    """Compute one server-owned submission quote from fresh trusted inputs.

    The quote amount is available only when every eligible GPU has a fresh
    trusted rate; an unknown or stale eligible GPU makes the whole quote
    unavailable instead of quoting from the cheaper known subset.  A missing
    calibration keeps the amount unavailable (no undocumented constants).
    """

    fingerprint = build_cost_fingerprint(
        profile_id=profile_id,
        duration_mode=duration_mode or "unknown",
        duration_value_seconds=duration_value_seconds,
        variation_count=variation_count,
        eligible_gpu_ids=eligible_gpu_ids,
        model_identity=model_identity,
    )
    unavailable_reason: str | None = None
    if not eligible_gpu_ids:
        unavailable_reason = "gpu_unknown"
    elif any(gpu_id not in fresh_rates for gpu_id in eligible_gpu_ids):
        stale = any(
            gpu_id in stale_gpu_ids for gpu_id in eligible_gpu_ids if gpu_id not in fresh_rates
        )
        unavailable_reason = "rate_stale" if stale else "rate_unknown"
    elif calibration_version is None or predicted_execution_range_ms is None:
        unavailable_reason = "calibration_missing"
    if unavailable_reason is not None:
        return QuoteEstimate(
            cost_fingerprint=fingerprint,
            model_identity=model_identity,
            profile_id=profile_id,
            duration_mode=duration_mode,
            duration_value_seconds=duration_value_seconds,
            variation_count=variation_count,
            eligible_gpu_ids=list(eligible_gpu_ids),
            highest_trusted_hourly_rate_micro_usd=None,
            highest_trusted_hourly_rate_usd=None,
            calibration_version=calibration_version,
            predicted_execution_range_ms=None,
            quoted_amount_micro_usd=None,
            quoted_range_low_micro_usd=None,
            quoted_range_high_micro_usd=None,
            currency="USD",
            rate_source=None,
            rate_version=None,
            unavailable_reason_code=unavailable_reason,
            captured_at=captured_at,
        )
    assert predicted_execution_range_ms is not None
    low_ms, high_ms = predicted_execution_range_ms
    if fresh_rate_usd is None:
        raise ValueError("fresh_rate_usd must include every eligible GPU")
    highest_gpu = select_highest_exact_rate_gpu(
        eligible_gpu_ids=eligible_gpu_ids,
        fresh_rates=fresh_rates,
        fresh_rate_usd=fresh_rate_usd,
    )
    highest_rate = fresh_rates[highest_gpu]
    highest_rate_usd = fresh_rate_usd[highest_gpu]
    range_low = round_half_up_compute_cost_usd(low_ms, highest_rate_usd)
    range_high = round_half_up_compute_cost_usd(high_ms, highest_rate_usd)
    return QuoteEstimate(
        cost_fingerprint=fingerprint,
        model_identity=model_identity,
        profile_id=profile_id,
        duration_mode=duration_mode,
        duration_value_seconds=duration_value_seconds,
        variation_count=variation_count,
        eligible_gpu_ids=list(eligible_gpu_ids),
        highest_trusted_hourly_rate_micro_usd=highest_rate,
        highest_trusted_hourly_rate_usd=highest_rate_usd,
        calibration_version=calibration_version,
        predicted_execution_range_ms=(low_ms, high_ms),
        quoted_amount_micro_usd=range_high,
        quoted_range_low_micro_usd=range_low,
        quoted_range_high_micro_usd=range_high,
        currency="USD",
        rate_source=rate_source,
        rate_version=rate_version,
        unavailable_reason_code=None,
        captured_at=captured_at,
    )


def select_highest_exact_rate_gpu(
    *,
    eligible_gpu_ids: list[str],
    fresh_rates: Mapping[str, int],
    fresh_rate_usd: Mapping[str, str],
) -> str:
    """Select the highest eligible GPU by its validated exact decimal token."""

    if not eligible_gpu_ids:
        raise ValueError("eligible_gpu_ids must not be empty")
    if any(gpu_id not in fresh_rates for gpu_id in eligible_gpu_ids):
        raise ValueError("fresh_rates must include every eligible GPU")
    if any(gpu_id not in fresh_rate_usd for gpu_id in eligible_gpu_ids):
        raise ValueError("fresh_rate_usd must include every eligible GPU")

    exact_rates: dict[str, Decimal] = {}
    for gpu_id in eligible_gpu_ids:
        exact_token, derived_rate = parse_micro_usd_decimal(
            fresh_rate_usd[gpu_id], field_name=f"fresh_rate_usd[{gpu_id}]"
        )
        if derived_rate != fresh_rates[gpu_id]:
            raise ValueError("exact hourly USD rate does not match derived micro-USD rate")
        exact_rates[gpu_id] = Decimal(exact_token)
    return max(eligible_gpu_ids, key=exact_rates.__getitem__)


def format_micro_usd(micro_usd: int) -> str:
    """Present an integer micro-USD amount as fixed 4-decimal USD text.

    Kept for preserved integer micro-USD fields; informational estimate
    labels never pass through here.  The integer micro-USD value is already
    rounded by the centralized half-up formula; this presentation truncates
    at four decimal places so the preserved display never inflates an amount.
    No binary float is used.
    """

    if isinstance(micro_usd, bool) or not isinstance(micro_usd, int) or micro_usd < 0:
        raise ValueError("micro_usd must be a non-negative integer")
    amount = Decimal(micro_usd) / Decimal(MICRO_USD_PER_USD)
    return f"USD {amount.quantize(_ESTIMATE_DISPLAY_QUANTUM, rounding=ROUND_DOWN)}"


def format_exact_usd_half_up(numerator: int, denominator: int) -> str:
    """Present an exact rational micro-USD amount as fixed 4-decimal USD text.

    This is the single final display boundary: ``ROUND_HALF_UP`` is applied
    exactly once when quantizing the raw numerator/denominator value to
    ``0.0001`` USD.  A pre-rounded integer micro-USD amount is never routed
    back through a four-decimal label, so the label cannot disagree with the
    exact rational value.  No binary float is used.
    """

    if isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0:
        raise ValueError("numerator must be a non-negative integer")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise ValueError("denominator must be a positive integer")
    amount = Decimal(numerator) / Decimal(denominator) / Decimal(MICRO_USD_PER_USD)
    return f"USD {amount.quantize(_ESTIMATE_DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)}"


def build_cost_estimate_view(
    *,
    execution_ms_samples: Sequence[int],
    variation_count: int,
    kind_label: str,
) -> dict[str, Any]:
    """Compute the read-only approximate estimate view from attempt durations.

    Pure integer/rational arithmetic at the fixed ``USD 0.50/GPU-hour`` rate;
    binary float never enters.  Raw numerators are carried through every
    sample, the average (sum of the raw sample numerators divided by the
    sample count, never from rounded display strings), and the request total
    (the unrounded average, or the unrounded 60-second seed with no history,
    multiplied by the variation count).  Each label applies ``ROUND_HALF_UP``
    exactly once at the final four-decimal USD display boundary; integer
    micro-USD fields are preserved for callers that need them and are never
    fed back into a label.  The seed is never blended into partial history
    and nothing here is persisted.
    """

    if isinstance(variation_count, bool) or not isinstance(variation_count, int):
        raise ValueError("variation_count must be an integer")
    if variation_count < 1 or variation_count > 4:
        raise ValueError("variation_count must be between 1 and 4")
    samples_ms = list(execution_ms_samples)[:ESTIMATE_HISTORY_SAMPLE_LIMIT]
    samples: list[dict[str, Any]] = []
    numerator_sum = 0
    for execution_ms in samples_ms:
        if isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms < 0:
            raise ValueError("execution_ms samples must be non-negative integers")
        numerator = execution_ms * FIXED_GPU_HOURLY_RATE_MICRO_USD
        numerator_sum += numerator
        cost_micro_usd = (numerator + MS_PER_HOUR // 2) // MS_PER_HOUR
        samples.append(
            {
                "execution_ms": execution_ms,
                "cost_micro_usd": cost_micro_usd,
                "cost_label": format_exact_usd_half_up(numerator, MS_PER_HOUR),
            }
        )
    sample_count = len(samples)
    if sample_count == 0:
        used_seed = True
        seed_note = "no completed history of this kind yet — using a 60-second seed"
        average_micro_usd: int | None = None
        average_label: str | None = None
        seed_numerator = NO_HISTORY_SEED_EXECUTION_MS * FIXED_GPU_HOURLY_RATE_MICRO_USD
        per_variation_label = format_exact_usd_half_up(seed_numerator, MS_PER_HOUR)
        request_numerator = seed_numerator * variation_count
        request_denominator = MS_PER_HOUR
    else:
        used_seed = False
        seed_note = None
        request_denominator = sample_count * MS_PER_HOUR
        average_micro_usd = (numerator_sum + request_denominator // 2) // request_denominator
        average_label = format_exact_usd_half_up(numerator_sum, request_denominator)
        per_variation_label = average_label
        request_numerator = numerator_sum * variation_count
    request_micro_usd = (request_numerator + request_denominator // 2) // request_denominator
    return {
        "approximate": True,
        "informational": True,
        "rate_label": "USD 0.50/GPU-hour",
        "kind_label": kind_label,
        "variation_count": variation_count,
        "used_seed": used_seed,
        "seed_note": seed_note,
        "samples": samples,
        "sample_count": sample_count,
        "average_micro_usd": average_micro_usd,
        "average_label": average_label,
        "per_variation_label": per_variation_label,
        "request_estimate_micro_usd": request_micro_usd,
        "request_estimate_label": format_exact_usd_half_up(request_numerator, request_denominator),
    }

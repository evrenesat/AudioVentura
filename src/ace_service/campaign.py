"""Private, checkpoint-scoped quality campaign state and decision logic.

The quality campaign deliberately does not use the product SQLAlchemy database.
It is a small operator-only SQLite store with its own schema contract.  This
module contains the bounded persistence, decimal accounting, fixture contract,
blinded scoring, and maintenance-gate primitives used by the local operator
CLI.  It must remain safe for the previous controller to ignore.
"""

# The store keeps long SQL statements readable as adjacent strings.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx

from ace_service.quality_profiles import MAX_CAPTION_LENGTH, MAX_LYRICS_LENGTH, resolve_parameters

CAMPAIGN_SCHEMA_VERSION = 3
CAMPAIGN_CEILING_MICRO_USD = 5_000_000
CAMPAIGN_ADMISSION_STOP_MICRO_USD = 4_500_000
MICRO_USD = Decimal("1000000")
HOUR_MILLISECONDS = Decimal("3600000")
GIBIBYTE = Decimal(1024**3)
MAX_CAMPAIGN_ID_LENGTH = 96
MAX_FIELD_LENGTH = 512
MAX_JSON_BYTES = 256 * 1024
MAX_BILLING_ROWS = 512
SCORE_SHEET_VERSION = 1
MAX_STORAGE_BYTES = 2**50

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,95}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_UTC_RE = re.compile(r"Z$|[+]00:00$")
_TERMINAL_SAMPLE_STATES = frozenset({"completed", "failed", "cancelled", "unsubmitted"})
_OPEN_RESERVATION_STATES = frozenset({"open", "unresolved"})
CONSERVATIVELY_RETAINED_STATE = "conservatively_retained"
# Exactly four reservation states exist; anything else is corrupt and must
# fail closed before status, admission, teardown, recovery, or rollback can
# omit or reinterpret it.
_RESERVATION_STATES = frozenset({"open", "unresolved", CONSERVATIVELY_RETAINED_STATE, "settled"})
# Budget retention is deliberately wider than teardown blocking: a terminal
# attempt whose attributable compute is unknown keeps its full immutable
# original reservation counted against every later admission/budget check
# (so recovery can never lower committed spend), yet the state itself is
# financially resolved for verified window teardown once provider zero is
# proven.  Genuinely open/in-flight/uncertain reservations still block.
_BUDGET_RETENTION_STATES = frozenset({"open", "unresolved", CONSERVATIVELY_RETAINED_STATE})
_DIMENSIONS = (
    "melody_retention",
    "prompt_style_adherence",
    "development",
    "vocal_lyric_adherence",
    "artifacts",
    "ending_quality",
)
_PRIMARY_COVER_DIMENSIONS = (
    "melody_retention",
    "prompt_style_adherence",
    "development",
    "vocal_lyric_adherence",
    "ending_quality",
)
_PRIMARY_ORIGINAL_DIMENSIONS = (
    "prompt_style_adherence",
    "development",
    "vocal_lyric_adherence",
    "ending_quality",
)


class CampaignError(RuntimeError):
    """Base class for safe operator-campaign failures."""


class CampaignSchemaError(CampaignError):
    """The private campaign database is absent, corrupt, or not this version."""


class CampaignValidationError(CampaignError, ValueError):
    """An operator input cannot be admitted to the campaign contract."""


class CampaignBudgetError(CampaignError):
    """A reservation would cross the campaign admission guard."""


class CampaignGateError(CampaignError):
    """A durable maintenance or rollback gate is not satisfied."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CampaignValidationError("campaign timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CampaignValidationError("timestamp must be non-empty ISO-8601 UTC text")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CampaignValidationError("timestamp must be valid ISO-8601 text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignValidationError("timestamp must include an explicit UTC offset")
    return parsed.astimezone(UTC)


def _bounded_id(value: Any, field_name: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise CampaignValidationError(f"{field_name} must be a bounded opaque identifier")
    return value


def _bounded_text(value: Any, field_name: str, *, max_length: int = MAX_FIELD_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise CampaignValidationError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise CampaignValidationError(f"{field_name} contains a control character")
    return value


def _bounded_json(value: Any, field_name: str = "metadata", *, limit: int = MAX_JSON_BYTES) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CampaignValidationError(f"{field_name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise CampaignValidationError(f"{field_name} exceeds its bounded size")
    return encoded


def _load_json(value: str, field_name: str = "metadata") -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CampaignSchemaError(f"stored {field_name} is not valid JSON") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_fixed_decimal(value: Any, *, field_name: str, minimum: Decimal = Decimal("0")) -> Decimal:
    """Parse a provider/rate decimal without accepting binary floats."""

    if isinstance(value, bool) or isinstance(value, float):
        raise CampaignValidationError(f"{field_name} must be a decimal string")
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise CampaignValidationError(f"{field_name} must be a decimal string")
    if not _DECIMAL_RE.fullmatch(text):
        raise CampaignValidationError(f"{field_name} must use fixed non-negative decimal text")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise CampaignValidationError(f"{field_name} is not a decimal") from exc
    if parsed < minimum:
        raise CampaignValidationError(f"{field_name} is below its allowed minimum")
    return parsed


def decimal_text(value: Decimal) -> str:
    """Return canonical fixed decimal text without exponent notation."""

    return format(value, "f")


def execution_micro_usd(execution_ms: int, hourly_rate_usd: Any) -> int:
    """Apply the single required half-up micro-USD calculation."""

    if isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms < 0:
        raise CampaignValidationError("execution_ms must be a non-negative integer")
    rate = parse_fixed_decimal(hourly_rate_usd, field_name="hourly_rate_usd")
    amount = (Decimal(execution_ms) * rate * MICRO_USD / HOUR_MILLISECONDS).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(amount)


def storage_micro_usd(
    bytes_count: int,
    rate_micro_usd_per_gib_month: Any,
    *,
    start: datetime,
    removal_deadline: datetime,
) -> int:
    """Conservatively reserve capacity-rate spend through the removal deadline."""

    if (
        isinstance(bytes_count, bool)
        or not isinstance(bytes_count, int)
        or not 0 < bytes_count <= MAX_STORAGE_BYTES
    ):
        raise CampaignValidationError("storage bytes must be a positive integer")
    if removal_deadline <= start:
        raise CampaignValidationError("storage removal deadline must be after reservation time")
    rate = parse_fixed_decimal(
        rate_micro_usd_per_gib_month,
        field_name="rate_micro_usd_per_gib_month",
    )
    duration_days = Decimal(str((removal_deadline - start).total_seconds())) / Decimal("86400")
    amount = (Decimal(bytes_count) / GIBIBYTE * rate * duration_days / Decimal("30")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(amount)


@dataclass(frozen=True, slots=True)
class FixtureCase:
    case_id: str
    task_type: str
    prompt: str
    lyrics: str | None
    duration_seconds: float
    output_format: str
    screening_seed: int
    confirmation_seeds: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    """Validated fixture data; source URLs are intentionally not retained."""

    fixture_id: str
    manifest_sha256: str
    manifest_path: Path
    media_path: Path
    media_sha256: str
    media_bytes: int
    source_duration_seconds: float
    clip_start_seconds: float
    clip_end_seconds: float
    clip_duration_seconds: float
    cases: tuple[FixtureCase, ...]
    rubric_id: str
    listener_count: int
    budget_currency: str
    ceiling_micro_usd: int
    admission_stop_micro_usd: int
    retention_deadline: datetime
    delete_after_scores_final: bool
    extension_requires_operator_decision: bool

    def case(self, case_id: str) -> FixtureCase:
        for item in self.cases:
            if item.case_id == case_id:
                return item
        raise CampaignValidationError("manifest case is not declared")


def _resolve_manifest_media(manifest_path: Path, local_media_path: str) -> Path:
    relative = Path(local_media_path)
    candidates = [manifest_path.parent / relative]
    parts = relative.parts
    if parts and parts[0] == "evaluations":
        candidates.append(manifest_path.parent.parent.parent / relative)
    candidates.append(manifest_path.parent / relative.name)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(
            manifest_path.parent.parent.parent.resolve()
        ):
            return resolved
    raise CampaignValidationError("fixture media path is missing or outside the private data root")


def _number(value: Any, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignValidationError(f"{field_name} must be a finite number")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise CampaignValidationError(f"{field_name} must be finite")
    if positive and result <= 0:
        raise CampaignValidationError(f"{field_name} must be positive")
    return result


def load_fixture_manifest(path: Path, *, now: datetime | None = None) -> FixtureManifest:
    """Validate the frozen private manifest and the local media hash."""

    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise CampaignValidationError("fixture manifest does not exist")
    mode = stat.S_IMODE(resolved_path.stat().st_mode)
    if mode & 0o022:
        raise CampaignValidationError("fixture manifest must not be group/world writable")
    raw_bytes = resolved_path.read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignValidationError("fixture manifest must be UTF-8 JSON") from exc
    if not isinstance(raw, dict) or raw.get("manifest_schema") != "quality-fixture-manifest-v1":
        raise CampaignValidationError("unsupported fixture manifest schema")
    if raw.get("status") != "frozen":
        raise CampaignValidationError("fixture manifest must be frozen")
    fixture_id = _bounded_id(raw.get("fixture_id"), "fixture_id")
    source = raw.get("source")
    if not isinstance(source, dict):
        raise CampaignValidationError("manifest source is missing")
    license_name = str(source.get("license", "")).lower()
    if not any(
        marker in license_name
        for marker in ("cc0", "public domain", "creative commons", "user-owned")
    ):
        raise CampaignValidationError("manifest license is not an allowed evaluation license")
    if not isinstance(source.get("license_proof_url"), str) or not str(
        source["license_proof_url"]
    ).startswith("https://"):
        raise CampaignValidationError("manifest license proof must be an HTTPS URL")
    _bounded_text(source.get("license_evidence"), "license_evidence", max_length=2048)
    media_path_value = source.get("local_media_path")
    if not isinstance(media_path_value, str) or not media_path_value:
        raise CampaignValidationError("manifest local media path is missing")
    media_path = _resolve_manifest_media(resolved_path, media_path_value)
    media_sha256 = str(source.get("local_media_sha256", "")).lower()
    if not _SHA256_RE.fullmatch(media_sha256):
        raise CampaignValidationError("manifest media SHA-256 is malformed")
    media_bytes = source.get("source_bytes")
    if isinstance(media_bytes, bool) or not isinstance(media_bytes, int) or media_bytes <= 0:
        raise CampaignValidationError("manifest media byte count is malformed")
    if media_path.stat().st_size != media_bytes or _sha256(media_path) != media_sha256:
        raise CampaignValidationError("fixture media hash or byte count does not match manifest")
    source_duration = _number(
        source.get("source_duration_seconds"), "source_duration_seconds", positive=True
    )
    clip = raw.get("clip")
    if not isinstance(clip, dict):
        raise CampaignValidationError("manifest clip is missing")
    clip_start = _number(clip.get("start_seconds"), "clip.start_seconds")
    clip_end = _number(clip.get("end_seconds"), "clip.end_seconds")
    clip_duration = _number(clip.get("duration_seconds"), "clip.duration_seconds", positive=True)
    if (
        clip_start < 0
        or clip_end <= clip_start
        or abs((clip_end - clip_start) - clip_duration) > 1e-6
    ):
        raise CampaignValidationError("manifest clip boundaries are inconsistent")
    if clip_end > source_duration:
        raise CampaignValidationError("manifest clip exceeds source duration")
    cases_value = raw.get("cases")
    if not isinstance(cases_value, list) or not cases_value:
        raise CampaignValidationError("manifest must declare at least one case")
    cases: list[FixtureCase] = []
    case_ids: set[str] = set()
    for item in cases_value:
        if not isinstance(item, dict):
            raise CampaignValidationError("manifest case must be an object")
        case_id = _bounded_id(item.get("case_id"), "case_id")
        if case_id in case_ids:
            raise CampaignValidationError("manifest case IDs must be unique")
        case_ids.add(case_id)
        task_type = item.get("task_type")
        if task_type not in {"cover", "original"}:
            raise CampaignValidationError("manifest case task_type is unsupported")
        prompt = _bounded_text(item.get("prompt"), "case.prompt", max_length=MAX_CAPTION_LENGTH)
        lyrics_value = item.get("lyrics")
        lyrics = (
            None
            if lyrics_value is None
            else _bounded_text(lyrics_value, "case.lyrics", max_length=MAX_LYRICS_LENGTH)
        )
        duration = _number(item.get("duration_seconds"), "case.duration_seconds", positive=True)
        if not 10 <= duration <= 600:
            raise CampaignValidationError("case duration must be between 10 and 600 seconds")
        if task_type == "cover" and abs(duration - clip_duration) > 1e-6:
            raise CampaignValidationError("cover duration must equal the fixed clip duration")
        if item.get("output_format") not in {"mp3", "flac", "wav"}:
            raise CampaignValidationError("case output format is unsupported")
        screening_seed = item.get("screening_seed")
        confirmation_seeds = item.get("confirmation_seeds")
        if (
            isinstance(screening_seed, bool)
            or not isinstance(screening_seed, int)
            or not isinstance(confirmation_seeds, list)
            or len(confirmation_seeds) != 3
            or any(
                isinstance(seed, bool) or not isinstance(seed, int) for seed in confirmation_seeds
            )
        ):
            raise CampaignValidationError(
                "case seeds must contain one screening and three confirmations"
            )
        if screening_seed != confirmation_seeds[0] or any(
            seed < 0 or seed > 2_147_483_647 for seed in confirmation_seeds
        ):
            raise CampaignValidationError("case seeds are inconsistent or out of range")
        cases.append(
            FixtureCase(
                case_id=case_id,
                task_type=task_type,
                prompt=prompt,
                lyrics=lyrics,
                duration_seconds=duration,
                output_format=str(item["output_format"]),
                screening_seed=screening_seed,
                confirmation_seeds=tuple(confirmation_seeds),
            )
        )
    if {case.task_type for case in cases} != {"cover", "original"}:
        raise CampaignValidationError("manifest must contain one cover and one original case")
    budget = raw.get("budget")
    if not isinstance(budget, dict) or budget.get("currency") != "USD":
        raise CampaignValidationError("manifest budget must be in USD")
    ceiling = budget.get("ceiling_micro_usd")
    admission = budget.get("admission_stop_micro_usd")
    if ceiling != CAMPAIGN_CEILING_MICRO_USD or admission != CAMPAIGN_ADMISSION_STOP_MICRO_USD:
        raise CampaignValidationError("manifest budget does not match the fixed campaign ceiling")
    blinding = raw.get("blinding")
    if not isinstance(blinding, dict) or blinding.get("listener_count") not in {None, 2}:
        raise CampaignValidationError("manifest must declare two blinded listeners")
    listener_count = int(blinding.get("listener_count", 2))
    if listener_count != 2:
        raise CampaignValidationError("exactly two listeners are required")
    retention = raw.get("retention")
    if not isinstance(retention, dict):
        raise CampaignValidationError("manifest retention policy is missing")
    retention_deadline = _parse_utc(str(retention.get("retention_deadline_utc", "")))
    reference_now = now or utc_now()
    if retention_deadline <= reference_now:
        raise CampaignValidationError("fixture retention deadline has passed")
    manifest_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return FixtureManifest(
        fixture_id=fixture_id,
        manifest_sha256=manifest_sha256,
        manifest_path=resolved_path,
        media_path=media_path,
        media_sha256=media_sha256,
        media_bytes=media_bytes,
        source_duration_seconds=source_duration,
        clip_start_seconds=clip_start,
        clip_end_seconds=clip_end,
        clip_duration_seconds=clip_duration,
        cases=tuple(cases),
        rubric_id=_bounded_id(str(raw.get("rubric_id")), "rubric_id"),
        listener_count=listener_count,
        budget_currency="USD",
        ceiling_micro_usd=ceiling,
        admission_stop_micro_usd=admission,
        retention_deadline=retention_deadline,
        delete_after_scores_final=bool(retention.get("delete_after_scores_final")),
        extension_requires_operator_decision=bool(
            retention.get("extension_requires_operator_decision")
        ),
    )


@dataclass(frozen=True, slots=True)
class CampaignCase:
    """One predeclared comparison role, before exact-fingerprint deduplication."""

    declared_case_id: str
    fixture_case_id: str | None
    task_type: str
    stage: str
    role: str
    seed: int
    profile_id: str
    model: str
    lm_model: str | None
    prompt_mode: str
    duration_seconds: float
    resolved_parameters: Mapping[str, Any]
    pair_key: str
    conditional_on: str | None = None
    requires_storage: bool = False

    def fingerprint(
        self,
        *,
        fixture_id: str,
        runtime_id: str = "ace-step-v0.1.8",
        image_digest: str = "unrecorded",
    ) -> str:
        payload = {
            "fixture_id": fixture_id,
            "fixture_case_id": self.fixture_case_id,
            "task_type": self.task_type,
            "resolved_parameters": dict(self.resolved_parameters),
            "seed": self.seed,
            "model": self.model,
            "lm_model": self.lm_model,
            "runtime_id": runtime_id,
            "image_digest": image_digest,
            "duration_seconds": self.duration_seconds,
        }
        return hashlib.sha256(_bounded_json(payload, "case fingerprint").encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    cases: tuple[CampaignCase, ...]
    mandatory_case_count: int
    maximum_case_count: int
    mandatory_confirmation_attempts: int
    maximum_confirmation_attempts: int
    storage_case_count: int

    @property
    def minimum_jobs(self) -> int:
        return self.mandatory_case_count + self.mandatory_confirmation_attempts

    @property
    def maximum_jobs(self) -> int:
        return self.maximum_case_count + self.maximum_confirmation_attempts

    @property
    def minimum_paid_attempts(self) -> int:
        return self.minimum_jobs

    @property
    def maximum_paid_attempts(self) -> int:
        return self.maximum_jobs


def _resolved_cover(
    case: FixtureCase, profile_id: str, audio: float, noise: float
) -> dict[str, Any]:
    return resolve_parameters(
        profile_id,
        task_type="cover",
        prompt_mode="direct",
        duration_mode="source",
        duration=case.duration_seconds,
        caption=case.prompt,
        lyrics=case.lyrics,
        seed=case.screening_seed,
        audio_cover_strength=audio,
        cover_noise_strength=noise,
        source_duration_seconds=case.duration_seconds,
    )


def _resolved_original(
    case: FixtureCase, profile_id: str, prompt_mode: str, seed: int
) -> dict[str, Any]:
    return resolve_parameters(
        profile_id,
        task_type="original",
        prompt_mode=prompt_mode,
        duration_mode="custom",
        duration=case.duration_seconds,
        caption=case.prompt,
        lyrics=case.lyrics,
        seed=seed,
    )


_COMPATIBILITY_SMOKE_CAPTION = "Strict worker compatibility smoke for the original task type"


def _compatibility_smoke_parameters(schema_version: int, *, seed: int) -> dict[str, Any]:
    """Freeze the complete canonical compatibility inputs for one strict schema.

    The campaign store records the campaign-only identity fields (schema
    version, model, LM model) alongside the fully resolved worker parameters so
    the smoke keeps a stable fingerprint.  The worker envelope built from this
    record carries only the strict schema fields the production parser accepts.
    """

    parameters = resolve_parameters(
        "fast-beta-v1",
        task_type="original",
        prompt_mode="direct",
        duration_mode="custom",
        duration=20.0,
        caption=_COMPATIBILITY_SMOKE_CAPTION,
        lyrics="",
        seed=seed,
    )
    parameters["schema_version"] = schema_version
    parameters["model"] = "acestep-v15-xl-turbo"
    parameters["lm_model"] = "acestep-5Hz-lm-1.7B"
    return parameters


def build_campaign_plan(manifest: FixtureManifest) -> CampaignPlan:
    """Expand the frozen ordered decision tree without inspecting results."""

    cover = next(case for case in manifest.cases if case.task_type == "cover")
    original = next(case for case in manifest.cases if case.task_type == "original")
    cases: list[CampaignCase] = []

    cases.extend(
        (
            CampaignCase(
                declared_case_id="compat-v1-smoke",
                fixture_case_id=None,
                task_type="original",
                stage="compatibility",
                role="compatibility",
                seed=1729,
                profile_id="fast-beta-v1",
                model="acestep-v15-xl-turbo",
                lm_model="acestep-5Hz-lm-1.7B",
                prompt_mode="direct",
                duration_seconds=20.0,
                resolved_parameters=_compatibility_smoke_parameters(1, seed=1729),
                pair_key="compatibility",
            ),
            CampaignCase(
                declared_case_id="compat-v2-smoke",
                fixture_case_id=None,
                task_type="original",
                stage="compatibility",
                role="compatibility",
                seed=1729,
                profile_id="fast-beta-v1",
                model="acestep-v15-xl-turbo",
                lm_model="acestep-5Hz-lm-1.7B",
                prompt_mode="direct",
                duration_seconds=20.0,
                resolved_parameters=_compatibility_smoke_parameters(2, seed=1729),
                pair_key="compatibility",
            ),
        )
    )

    def add_cover(
        name: str,
        role: str,
        audio: float,
        noise: float,
        *,
        stage: str = "cover-screen",
        seed: int = cover.screening_seed,
        conditional_on: str | None = None,
    ) -> None:
        cases.append(
            CampaignCase(
                declared_case_id=name,
                fixture_case_id=cover.case_id,
                task_type="cover",
                stage=stage,
                role=role,
                seed=seed,
                profile_id="quality-v1" if noise == 0.20 else "fast-beta-v1",
                model="acestep-v15-xl-turbo",
                lm_model=None,
                prompt_mode="direct",
                duration_seconds=cover.duration_seconds,
                resolved_parameters=_resolved_cover(
                    cover, "quality-v1" if noise == 0.20 else "fast-beta-v1", audio, noise
                ),
                pair_key="cover-screen" if stage == "cover-screen" else "cover-confirmation",
                conditional_on=conditional_on,
            )
        )

    # The incumbent and corrected-controls role intentionally share an exact
    # fingerprint when the explicit noise value is the worker's old default.
    add_cover("cover-incumbent", "incumbent", 0.65, 0.0)
    add_cover("cover-corrected-controls", "corrected-controls", 0.65, 0.0)
    for noise in (0.0, 0.10, 0.20, 0.30):
        for audio in (0.35, 0.65, 0.85):
            name = f"cover-grid-a{audio:.2f}-n{noise:.2f}"
            if audio == 0.65 and noise == 0.0:
                continue
            add_cover(name, "candidate", audio, noise)

    # Original screening is deliberately ordered before any larger planner.
    cases.extend(
        (
            CampaignCase(
                declared_case_id="original-incumbent",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-screen",
                role="incumbent",
                seed=original.screening_seed,
                profile_id="fast-beta-v1",
                model="acestep-v15-xl-turbo",
                lm_model="acestep-5Hz-lm-1.7B",
                prompt_mode="enhance",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "fast-beta-v1", "enhance", original.screening_seed
                ),
                pair_key="original-screen",
            ),
            CampaignCase(
                declared_case_id="original-direct-no-lm",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-screen",
                role="candidate",
                seed=original.screening_seed,
                profile_id="fast-beta-v1",
                model="acestep-v15-xl-turbo",
                lm_model=None,
                prompt_mode="direct",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "fast-beta-v1", "direct", original.screening_seed
                ),
                pair_key="original-screen",
            ),
            CampaignCase(
                declared_case_id="original-structured-1.7b",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-screen",
                role="candidate",
                seed=original.screening_seed,
                profile_id="quality-v1",
                model="acestep-v15-xl-turbo",
                lm_model="acestep-5Hz-lm-1.7B",
                prompt_mode="enhance",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "quality-v1", "enhance", original.screening_seed
                ),
                pair_key="original-screen",
            ),
        )
    )
    # Conditional branches are declared now, but the executor cannot submit
    # them without a fresh advancement confirmation after the prior stage.
    cases.extend(
        (
            CampaignCase(
                declared_case_id="original-structured-direct",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-conditional-model",
                role="candidate",
                seed=original.screening_seed,
                profile_id="quality-v1",
                model="acestep-v15-xl-turbo",
                lm_model=None,
                prompt_mode="direct",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "quality-v1", "direct", original.screening_seed
                ),
                pair_key="original-screen",
                conditional_on="original-screen-failed",
            ),
            CampaignCase(
                declared_case_id="original-4b-planner",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-conditional-model",
                role="candidate",
                seed=original.screening_seed,
                profile_id="quality-v1",
                model="acestep-v15-xl-turbo",
                lm_model="acestep-5Hz-lm-4B",
                prompt_mode="enhance",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "quality-v1", "enhance", original.screening_seed
                ),
                pair_key="original-screen",
                conditional_on="original-screen-failed",
                requires_storage=True,
            ),
            CampaignCase(
                declared_case_id="original-xl-sft-4b",
                fixture_case_id=original.case_id,
                task_type="original",
                stage="original-conditional-model",
                role="candidate",
                seed=original.screening_seed,
                profile_id="quality-v1",
                model="acestep-v15-xl-sft",
                lm_model="acestep-5Hz-lm-4B",
                prompt_mode="enhance",
                duration_seconds=original.duration_seconds,
                resolved_parameters=_resolved_original(
                    original, "quality-v1", "enhance", original.screening_seed
                ),
                pair_key="original-screen",
                conditional_on="original-screen-failed",
                requires_storage=True,
            ),
        )
    )
    for name, model, steps_or_cfg, conditional in (
        ("cover-turbo-8", "acestep-v15-xl-turbo", "steps-8-shift-3", "cover-screen-failed"),
        ("cover-turbo-12", "acestep-v15-xl-turbo", "steps-12-shift-3", "cover-screen-failed"),
        ("cover-xl-sft-cfg-7", "acestep-v15-xl-sft", "cfg-7", "cover-screen-failed"),
        ("cover-xl-sft-cfg-9", "acestep-v15-xl-sft", "cfg-9", "cover-screen-failed"),
    ):
        params = dict(_resolved_cover(cover, "quality-v1", 0.65, 0.20))
        params["conditional_variant"] = steps_or_cfg
        cases.append(
            CampaignCase(
                declared_case_id=name,
                fixture_case_id=cover.case_id,
                task_type="cover",
                stage="cover-conditional-model",
                role="candidate",
                seed=cover.screening_seed,
                profile_id="quality-v1",
                model=model,
                lm_model=None,
                prompt_mode="direct",
                duration_seconds=cover.duration_seconds,
                resolved_parameters=params,
                pair_key="cover-screen",
                conditional_on=conditional,
                requires_storage=True,
            )
        )
    # Confirmation is generated from ranked finalists, not inspected here.
    mandatory_cases = [
        item for item in cases if item.stage in {"compatibility", "cover-screen", "original-screen"}
    ]
    mandatory = len({item.fingerprint(fixture_id=manifest.fixture_id) for item in mandatory_cases})
    maximum = len({item.fingerprint(fixture_id=manifest.fixture_id) for item in cases})
    return CampaignPlan(
        cases=tuple(cases),
        mandatory_case_count=mandatory,
        maximum_case_count=maximum,
        mandatory_confirmation_attempts=0,
        maximum_confirmation_attempts=18,
        storage_case_count=sum(item.requires_storage for item in cases),
    )


def build_confirmation_cases(
    manifest: FixtureManifest,
    plan: CampaignPlan,
    finalist_case_ids: Sequence[str],
) -> tuple[CampaignCase, ...]:
    """Expand only preselected finalists and incumbents over the three frozen seeds."""

    if not finalist_case_ids or len(finalist_case_ids) > 2:
        raise CampaignGateError("confirmation requires one or two preselected finalists")
    requested = set(finalist_case_ids)
    requested_cases = [case for case in plan.cases if case.declared_case_id in requested]
    if len(requested_cases) != len(requested):
        raise CampaignGateError("confirmation finalist is not declared")
    task_types = {case.task_type for case in requested_cases}
    if len(task_types) != 1:
        raise CampaignGateError("confirmation finalists must belong to one independent task gate")
    task_type = next(iter(task_types))
    selected = [
        case
        for case in plan.cases
        if case.task_type == task_type
        and (
            case.declared_case_id in requested
            or (case.role == "incumbent" and case.stage in {"cover-screen", "original-screen"})
        )
    ]
    if len(selected) < 2:
        raise CampaignGateError("confirmation finalists or incumbents are not declared")
    fixture_case = manifest.case(next(iter(selected)).fixture_case_id or "")
    result: list[CampaignCase] = []
    for seed in fixture_case.confirmation_seeds:
        for case in selected:
            resolved = dict(case.resolved_parameters)
            resolved["seed"] = seed
            result.append(
                CampaignCase(
                    declared_case_id=f"{case.declared_case_id}-confirmation-{seed}",
                    fixture_case_id=case.fixture_case_id,
                    task_type=case.task_type,
                    stage="cover-confirmation"
                    if case.task_type == "cover"
                    else "original-confirmation",
                    role=case.role,
                    seed=seed,
                    profile_id=case.profile_id,
                    model=case.model,
                    lm_model=case.lm_model,
                    prompt_mode=case.prompt_mode,
                    duration_seconds=case.duration_seconds,
                    resolved_parameters=resolved,
                    pair_key=f"{case.task_type}-confirmation-{seed}",
                    requires_storage=case.requires_storage,
                )
            )
    return tuple(result)


def validate_plan_safety(plan: CampaignPlan) -> None:
    """Check the frozen plan's model ordering and cover LM prohibition."""

    original_stages = [
        case.stage
        for case in plan.cases
        if case.task_type == "original" and case.stage != "compatibility"
    ]
    if original_stages and original_stages[0] != "original-screen":
        raise CampaignGateError("original planner A/B must start with the declared screening stage")
    if any(case.task_type == "cover" and case.lm_model is not None for case in plan.cases):
        raise CampaignGateError("a larger language model cannot be used as a cover remedy")


@dataclass(frozen=True, slots=True)
class ProfileProposal:
    profile_id: str
    campaign_id: str
    resolved_parameters_sha256: str
    materially_different: bool


def propose_immutable_profile(
    campaign_id: str,
    existing_profile_id: str,
    existing_parameters: Mapping[str, Any],
    winner_parameters: Mapping[str, Any],
) -> ProfileProposal:
    """Return a reviewed proposal ID without mutating the production catalog/default."""

    identifier = _bounded_id(campaign_id, "campaign_id")
    existing_hash = hashlib.sha256(
        _bounded_json(dict(existing_parameters), "profile").encode()
    ).hexdigest()
    winner_hash = hashlib.sha256(
        _bounded_json(dict(winner_parameters), "profile").encode()
    ).hexdigest()
    if existing_hash == winner_hash:
        return ProfileProposal(existing_profile_id, identifier, winner_hash, False)
    return ProfileProposal(
        profile_id=f"{existing_profile_id}-campaign-{identifier[:16]}",
        campaign_id=identifier,
        resolved_parameters_sha256=winner_hash,
        materially_different=True,
    )


@dataclass(frozen=True, slots=True)
class RateEvidence:
    gpu_id: str
    hourly_rate_usd: str
    source_url: str
    source_version: str
    captured_at: datetime

    def __post_init__(self) -> None:
        _bounded_id(self.gpu_id, "gpu_id")
        parse_fixed_decimal(self.hourly_rate_usd, field_name="hourly_rate_usd")
        if not self.source_url.startswith("https://"):
            raise CampaignValidationError("rate source URL must use HTTPS")
        _bounded_text(self.source_version, "source_version")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise CampaignValidationError("rate capture time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RateCatalog:
    rates: Mapping[str, RateEvidence]
    max_age: timedelta = timedelta(hours=24)

    def require_fresh(
        self, gpu_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, RateEvidence]:
        reference = now or utc_now()
        result: dict[str, RateEvidence] = {}
        for gpu_id in gpu_ids:
            evidence = self.rates.get(gpu_id)
            if evidence is None:
                raise CampaignGateError(f"missing trusted Flex rate for eligible GPU {gpu_id}")
            age = reference.astimezone(UTC) - evidence.captured_at.astimezone(UTC)
            if age < timedelta(0) or age > self.max_age:
                raise CampaignGateError(f"stale trusted Flex rate for eligible GPU {gpu_id}")
            result[gpu_id] = evidence
        return result

    @classmethod
    def from_mapping(cls, value: Any, *, max_age_hours: int = 24) -> RateCatalog:
        if not isinstance(value, dict):
            raise CampaignValidationError("rate catalog must be an object")
        entries = value.get("rates", value)
        if isinstance(entries, dict):
            entries = [dict(item, gpu_id=gpu_id) for gpu_id, item in entries.items()]
        if not isinstance(entries, list):
            raise CampaignValidationError("rate catalog rates must be an array")
        rates: dict[str, RateEvidence] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise CampaignValidationError("rate catalog entry must be an object")
            evidence = RateEvidence(
                gpu_id=_bounded_id(item.get("gpu_id"), "gpu_id"),
                hourly_rate_usd=decimal_text(
                    parse_fixed_decimal(item.get("hourly_rate_usd"), field_name="hourly_rate_usd")
                ),
                source_url=_bounded_text(item.get("source_url"), "source_url", max_length=2048),
                source_version=_bounded_text(item.get("source_version"), "source_version"),
                captured_at=_parse_utc(str(item.get("captured_at_utc", ""))),
            )
            if evidence.gpu_id in rates:
                raise CampaignValidationError("rate catalog contains duplicate GPU IDs")
            rates[evidence.gpu_id] = evidence
        if not isinstance(max_age_hours, int) or not 0 < max_age_hours <= 168:
            raise CampaignValidationError("rate catalog max age is out of bounds")
        return cls(rates=rates, max_age=timedelta(hours=max_age_hours))


@dataclass(frozen=True, slots=True)
class RuntimeRule:
    task_type: str
    duration_seconds: float
    max_execution_ms: int
    error_margin_percent: int
    source_sample_ids: tuple[str, ...]

    @property
    def reserved_execution_ms(self) -> int:
        return int(
            (
                Decimal(self.max_execution_ms)
                * (Decimal(100 + self.error_margin_percent) / Decimal(100))
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )


def freeze_runtime_rule(
    task_type: str,
    duration_seconds: float,
    observations: Sequence[tuple[str, int]],
    *,
    error_margin_percent: int = 25,
) -> RuntimeRule:
    if task_type not in {"cover", "original"} or duration_seconds <= 0:
        raise CampaignValidationError("runtime rule task/duration is invalid")
    if not observations:
        raise CampaignGateError("runtime rule requires a measured incumbent observation")
    if not 0 <= error_margin_percent <= 100:
        raise CampaignValidationError("runtime error margin is out of bounds")
    sample_ids: list[str] = []
    max_execution = 0
    for sample_id, execution_ms in observations:
        _bounded_id(sample_id, "sample_id")
        if isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms <= 0:
            raise CampaignValidationError("runtime observation must have positive execution time")
        sample_ids.append(sample_id)
        max_execution = max(max_execution, execution_ms)
    return RuntimeRule(
        task_type=task_type,
        duration_seconds=float(duration_seconds),
        max_execution_ms=max_execution,
        error_margin_percent=error_margin_percent,
        source_sample_ids=tuple(sample_ids),
    )


@dataclass(frozen=True, slots=True)
class BoundaryEvidence:
    start_inclusive: bool
    end_exclusive: bool
    native_bucket_seconds: int
    native_bucket_start_field: str
    empty_response_behavior: str
    current_partial_bucket_behavior: str
    late_update_behavior: str
    source: str

    @property
    def proven(self) -> bool:
        return (
            self.start_inclusive
            and self.end_exclusive
            and self.native_bucket_seconds > 0
            and self.native_bucket_start_field in {"startTime", "bucketStart"}
            and self.empty_response_behavior == "documented"
            and self.current_partial_bucket_behavior == "documented"
            and self.late_update_behavior == "documented"
        )


@dataclass(frozen=True, slots=True)
class BillingObservation:
    provider: str
    resource_type: str
    grouping_dimension: str
    grouping_value: str | None
    bucket_start_utc: datetime
    bucket_size_seconds: int
    currency: str
    raw_amount: str
    amount_micro_usd: int
    fetched_at: datetime
    source_contract: str
    raw_time_billed: str | None = None
    allocatable: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.provider, "billing provider")
        _bounded_id(self.resource_type, "billing resource type")
        _bounded_id(self.grouping_dimension, "billing grouping dimension")
        if self.grouping_value is not None:
            _bounded_text(self.grouping_value, "billing grouping value")
        if self.bucket_start_utc.tzinfo is None or self.bucket_start_utc.utcoffset() is None:
            raise CampaignValidationError("billing bucket start must be timezone-aware")
        if self.bucket_size_seconds not in {60, 900, 1800, 3600, 86400}:
            raise CampaignValidationError("billing bucket size is unsupported")
        if self.currency != "USD":
            raise CampaignValidationError("campaign billing currency must be USD")
        raw_amount, amount_micro = _decimal_from_provider(self.raw_amount, "billing.raw_amount")
        if raw_amount != self.raw_amount or amount_micro != self.amount_micro_usd:
            raise CampaignValidationError("billing amount is not a canonical micro-USD observation")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise CampaignValidationError("billing fetch time must be timezone-aware")
        _bounded_text(self.source_contract, "billing source contract")
        if self.raw_time_billed is not None:
            parse_fixed_decimal(self.raw_time_billed, field_name="billing.timeBilled")
        if self.unavailable_reason is not None:
            _bounded_text(self.unavailable_reason, "billing unavailable reason", max_length=256)

    @property
    def key(self) -> tuple[str, str, str, str, str, int, str]:
        return (
            self.provider,
            self.resource_type,
            self.grouping_dimension,
            self.grouping_value or "",
            _iso(self.bucket_start_utc),
            self.bucket_size_seconds,
            self.currency,
        )


def _decimal_from_provider(value: Any, field_name: str) -> tuple[str, int]:
    parsed = parse_fixed_decimal(value, field_name=field_name)
    raw = value if isinstance(value, str) else decimal_text(parsed)
    return str(raw), int((parsed * MICRO_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _provider_bucket(row: Mapping[str, Any]) -> tuple[datetime, int]:
    start = row.get("startTime", row.get("bucketStart"))
    if not isinstance(start, str):
        raise CampaignValidationError("provider billing bucket start is missing")
    parsed = _parse_utc(start)
    raw_size = row.get("bucketSize", "hour")
    if raw_size == "hour":
        seconds = 3600
    elif isinstance(raw_size, int) and raw_size in {60, 900, 1800, 3600, 86400}:
        seconds = raw_size
    else:
        raise CampaignValidationError("provider billing bucket size is undocumented")
    return parsed, seconds


def parse_endpoint_billing_response(
    body: Any,
    *,
    endpoint_id: str,
    fetched_at: datetime | None = None,
    source_contract: str = "runpod-endpoints-v1-usd-no-currency",
) -> tuple[BillingObservation, ...]:
    """Parse the documented endpoint array; USD is a server contract value."""

    _bounded_id(endpoint_id, "endpoint_id")
    if not isinstance(body, list) or len(body) > MAX_BILLING_ROWS:
        raise CampaignValidationError("Runpod endpoint billing response must be a bounded array")
    observations: list[BillingObservation] = []
    seen: dict[tuple[str, ...], str] = {}
    captured = fetched_at or utc_now()
    for row in body:
        if not isinstance(row, Mapping):
            raise CampaignValidationError("Runpod endpoint billing row must be an object")
        returned_endpoint = row.get("endpointId", row.get("endpoint_id"))
        if returned_endpoint != endpoint_id:
            raise CampaignValidationError("Runpod endpoint billing row has the wrong endpoint")
        if "currency" in row and row.get("currency") not in {None, "USD"}:
            raise CampaignValidationError("Runpod endpoint billing currency contract changed")
        bucket_start, bucket_seconds = _provider_bucket(row)
        raw_amount, amount_micro = _decimal_from_provider(row.get("amount"), "billing.amount")
        time_billed = row.get("timeBilled")
        raw_time = None
        if time_billed is not None:
            raw_time = str(time_billed)
            parse_fixed_decimal(time_billed, field_name="billing.timeBilled")
        observation = BillingObservation(
            provider="runpod",
            resource_type="endpoint",
            grouping_dimension="endpointId",
            grouping_value=endpoint_id,
            bucket_start_utc=bucket_start,
            bucket_size_seconds=bucket_seconds,
            currency="USD",
            raw_amount=raw_amount,
            amount_micro_usd=amount_micro,
            fetched_at=captured,
            source_contract=source_contract,
            raw_time_billed=raw_time,
        )
        key = observation.key
        prior = seen.get(tuple(str(part) for part in key))
        if prior is not None and prior != raw_amount:
            raise CampaignValidationError("provider returned conflicting duplicate billing buckets")
        seen[tuple(str(part) for part in key)] = raw_amount
        if prior is None:
            observations.append(observation)
    return tuple(observations)


def parse_network_volume_billing_response(
    body: Any,
    *,
    fetched_at: datetime | None = None,
    source_contract: str = "runpod-network-volume-v1-no-volume-id",
) -> tuple[BillingObservation, ...]:
    """Preserve account-wide volume evidence without allocating it to the service."""

    if not isinstance(body, list) or len(body) > MAX_BILLING_ROWS:
        raise CampaignValidationError("Runpod network-volume response must be a bounded array")
    captured = fetched_at or utc_now()
    observations: list[BillingObservation] = []
    for row in body:
        if not isinstance(row, Mapping):
            raise CampaignValidationError("network-volume billing row must be an object")
        bucket_start, bucket_seconds = _provider_bucket(row)
        raw_amount, amount_micro = _decimal_from_provider(row.get("amount"), "volume.amount")
        volume_id = row.get("volumeId", row.get("volume_id"))
        grouping_value = volume_id if isinstance(volume_id, str) and volume_id else None
        observations.append(
            BillingObservation(
                provider="runpod",
                resource_type="network_volume",
                grouping_dimension="volumeId" if grouping_value else "account",
                grouping_value=grouping_value,
                bucket_start_utc=bucket_start,
                bucket_size_seconds=bucket_seconds,
                currency="USD",
                raw_amount=raw_amount,
                amount_micro_usd=amount_micro,
                fetched_at=captured,
                source_contract=source_contract,
                allocatable=grouping_value is not None,
                unavailable_reason=None
                if grouping_value is not None
                else "provider_response_missing_volume_identifier",
            )
        )
    return tuple(observations)


def validate_billing_window(
    start: datetime,
    end: datetime,
    evidence: BoundaryEvidence,
) -> None:
    """Require a proven half-open interval without shifting native buckets."""

    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
    ):
        raise CampaignValidationError("billing windows must use aware UTC timestamps")
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise CampaignValidationError("billing window must be non-empty")
    if not evidence.proven:
        raise CampaignGateError("provider interval semantics are ambiguous")
    start_epoch = int(start.astimezone(UTC).timestamp())
    end_epoch = int(end.astimezone(UTC).timestamp())
    if start_epoch % evidence.native_bucket_seconds != 0:
        raise CampaignGateError("billing window start is not aligned to a native bucket")
    if end_epoch % evidence.native_bucket_seconds != 0:
        if evidence.current_partial_bucket_behavior != "documented":
            raise CampaignGateError("billing window end is not aligned to a native bucket")


class CampaignBillingClient:
    """Read-only Runpod billing adapter used by the operator process only."""

    def __init__(
        self,
        api_key: str,
        endpoint_id: str,
        *,
        base_url: str = "https://api.runpod.io",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or not endpoint_id.strip():
            raise CampaignValidationError("billing credentials must be configured server-side")
        if not base_url.startswith("https://") or timeout_seconds <= 0:
            raise CampaignValidationError("billing client configuration is unsafe")
        self.endpoint_id = endpoint_id
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_seconds),
        )
        if http_client is not None:
            http_client.headers.update(
                {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_endpoint_billing(
        self,
        start: datetime,
        end: datetime,
        evidence: BoundaryEvidence,
        *,
        fetched_at: datetime | None = None,
    ) -> tuple[BillingObservation, ...]:
        validate_billing_window(start, end, evidence)
        try:
            response = await self._client.get(
                "/v1/billing/endpoints",
                params={
                    "bucketSize": "hour",
                    "grouping": "endpointId",
                    "endpointId": self.endpoint_id,
                    "startTime": _iso(start),
                    "endTime": _iso(end),
                },
            )
        except httpx.HTTPError as exc:
            raise CampaignError("Runpod billing request failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise CampaignError("Runpod billing request returned an error")
        if len(response.content) > 1_048_576:
            raise CampaignError("Runpod billing response exceeded the bounded limit")
        try:
            body = json.loads(
                response.content.decode("utf-8"), parse_float=Decimal, parse_int=Decimal
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CampaignError("Runpod billing response was not bounded JSON") from exc
        return parse_endpoint_billing_response(
            body,
            endpoint_id=self.endpoint_id,
            fetched_at=fetched_at,
        )


@dataclass(frozen=True, slots=True)
class ModelPreflight:
    artifact_hashes: Mapping[str, str]
    expected_hashes: Mapping[str, str]
    available_bytes: int
    required_bytes: int
    gpu_memory_headroom_bytes: int
    required_memory_bytes: int
    supported_runtime_contract: bool
    rollback_path_recorded: bool
    reservation_micro_usd: int

    def __post_init__(self) -> None:
        if not self.artifact_hashes or set(self.artifact_hashes) != set(self.expected_hashes):
            raise CampaignValidationError("conditional model artifact hash set is incomplete")
        for name, actual in self.artifact_hashes.items():
            _bounded_id(name, "model artifact name")
            if not isinstance(actual, str) or not _SHA256_RE.fullmatch(actual):
                raise CampaignValidationError("conditional model artifact hash is malformed")
            expected = self.expected_hashes.get(name)
            if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
                raise CampaignValidationError("conditional model expected hash is malformed")
        for field_name, value in (
            ("available_bytes", self.available_bytes),
            ("required_bytes", self.required_bytes),
            ("gpu_memory_headroom_bytes", self.gpu_memory_headroom_bytes),
            ("required_memory_bytes", self.required_memory_bytes),
            ("reservation_micro_usd", self.reservation_micro_usd),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CampaignValidationError(f"{field_name} must be a non-negative integer")

    @property
    def admissible(self) -> bool:
        if self.available_bytes < self.required_bytes:
            return False
        if self.gpu_memory_headroom_bytes < self.required_memory_bytes:
            return False
        if not self.supported_runtime_contract or not self.rollback_path_recorded:
            return False
        if self.reservation_micro_usd < 0:
            return False
        return all(
            actual == self.expected_hashes[name] for name, actual in self.artifact_hashes.items()
        )


def validate_model_preflight(preflight: ModelPreflight) -> None:
    if not preflight.admissible:
        raise CampaignGateError("conditional model preflight is not admissible")


@dataclass(frozen=True, slots=True)
class RemoteChangeAuthorization:
    application_commit: str
    worker_digest: str
    endpoint_id: str
    template_id: str
    rollback_target: str
    evaluation_models: tuple[str, ...]
    ceiling_micro_usd: int
    authorized_at: datetime
    blocked_routes: tuple[str, ...]
    edge_config_sha256: str = ""
    edge_guard_verified: bool = False

    @classmethod
    def from_mapping(cls, value: Any) -> RemoteChangeAuthorization:
        if not isinstance(value, Mapping):
            raise CampaignGateError("remote-change authorization must be an object")
        required = (
            "application_commit",
            "worker_digest",
            "endpoint_id",
            "template_id",
            "rollback_target",
            "evaluation_models",
            "ceiling_micro_usd",
            "authorized_at_utc",
            "blocked_routes",
            "edge_config_sha256",
            "edge_guard_verified",
        )
        if any(key not in value for key in required):
            raise CampaignGateError("remote-change authorization is incomplete")
        models = value["evaluation_models"]
        routes = value["blocked_routes"]
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(item, str) for item in models)
        ):
            raise CampaignGateError("remote authorization models are malformed")
        if (
            not isinstance(routes, list)
            or not routes
            or not all(isinstance(item, str) for item in routes)
        ):
            raise CampaignGateError("remote authorization blocked routes are malformed")
        ceiling = value["ceiling_micro_usd"]
        if (
            isinstance(ceiling, bool)
            or not isinstance(ceiling, int)
            or not 0 < ceiling <= CAMPAIGN_CEILING_MICRO_USD
        ):
            raise CampaignGateError("remote authorization ceiling is unsafe")
        edge_config_sha256 = value["edge_config_sha256"]
        if not isinstance(edge_config_sha256, str) or not _SHA256_RE.fullmatch(edge_config_sha256):
            raise CampaignGateError("remote authorization edge configuration hash is malformed")
        if value["edge_guard_verified"] is not True:
            raise CampaignGateError("remote authorization must verify the edge rollback guard")
        return cls(
            application_commit=_bounded_text(value["application_commit"], "application_commit"),
            worker_digest=_bounded_text(value["worker_digest"], "worker_digest"),
            endpoint_id=_bounded_id(value["endpoint_id"], "endpoint_id"),
            template_id=_bounded_text(value["template_id"], "template_id"),
            rollback_target=_bounded_text(value["rollback_target"], "rollback_target"),
            evaluation_models=tuple(_bounded_text(item, "evaluation_model") for item in models),
            ceiling_micro_usd=ceiling,
            authorized_at=_parse_utc(str(value["authorized_at_utc"])),
            blocked_routes=tuple(_bounded_text(item, "blocked_route") for item in routes),
            edge_config_sha256=edge_config_sha256,
            edge_guard_verified=True,
        )

    def to_private_mapping(self) -> dict[str, Any]:
        return {
            "application_commit": self.application_commit,
            "worker_digest": self.worker_digest,
            "endpoint_id": self.endpoint_id,
            "template_id": self.template_id,
            "rollback_target": self.rollback_target,
            "evaluation_models": list(self.evaluation_models),
            "ceiling_micro_usd": self.ceiling_micro_usd,
            "authorized_at_utc": _iso(self.authorized_at),
            "blocked_routes": list(self.blocked_routes),
            "edge_config_sha256": self.edge_config_sha256,
            "edge_guard_verified": self.edge_guard_verified,
        }


class CampaignSubmitter(Protocol):
    def submit(
        self,
        campaign_id: str,
        sample: Mapping[str, Any],
        *,
        on_submitted: Callable[[str], None] | None = None,
    ) -> str: ...

    def teardown(self, campaign_id: str, window_id: int, *, reason: str) -> None: ...

    def reconcile(self, campaign_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AdmissionSummary:
    provider_micro_usd: int
    settled_estimate_micro_usd: int
    open_reservation_micro_usd: int
    next_reservation_micro_usd: int

    @property
    def admission_total_micro_usd(self) -> int:
        return (
            max(self.provider_micro_usd, self.settled_estimate_micro_usd)
            + self.open_reservation_micro_usd
            + self.next_reservation_micro_usd
        )


@dataclass(frozen=True, slots=True)
class RollbackDiagnostic:
    classification: str
    detail: str
    blocks_rollback: bool


@dataclass(frozen=True, slots=True)
class RollbackReadiness:
    diagnostics: tuple[RollbackDiagnostic, ...]

    @property
    def safe(self) -> bool:
        return not any(item.blocks_rollback for item in self.diagnostics)

    @property
    def blockers(self) -> tuple[RollbackDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.blocks_rollback)


_SCHEMA_TABLES = frozenset(
    {
        "campaigns",
        "execution_windows",
        "maintenance_gate",
        "reservations",
        "provider_billing_observations",
        "rate_evidence",
        "samples",
        "sample_aliases",
        "runtime_rules",
        "score_sheets",
        "storage_artifacts",
        "quality_decisions",
        "leases",
        "campaign_events",
        "submission_intents",
    }
)

_SCHEMA_COLUMNS = {
    "campaigns": frozenset(
        {
            "campaign_id",
            "fixture_id",
            "manifest_sha256",
            "status",
            "ceiling_micro_usd",
            "admission_stop_micro_usd",
            "decision",
            "listener_ids_json",
            "plan_json",
            "authorization_json",
            "retention_deadline_utc",
            "created_at_utc",
            "updated_at_utc",
            "reason",
        }
    ),
    "execution_windows": frozenset(
        {
            "window_id",
            "campaign_id",
            "stage",
            "start_utc",
            "end_utc",
            "state",
            "contaminated",
            "blocked_routes_json",
            "edge_config_sha256",
            "health_evidence_json",
            "reason",
        }
    ),
    "maintenance_gate": frozenset(
        {
            "singleton",
            "active",
            "campaign_id",
            "window_id",
            "bypass_campaign_id",
            "edge_guard_enabled",
            "edge_guard_verified",
            "edge_config_sha256",
            "blocked_routes_json",
            "rollback_target",
            "updated_at_utc",
        }
    ),
    "reservations": frozenset(
        {
            "reservation_id",
            "campaign_id",
            "sample_id",
            "kind",
            "reserved_micro_usd",
            "state",
            "final_estimate_micro_usd",
            "unavailable_reason",
            "created_at_utc",
            "submitted_at_utc",
            "settled_at_utc",
        }
    ),
    "provider_billing_observations": frozenset(
        {
            "observation_id",
            "campaign_id",
            "provider",
            "resource_type",
            "grouping_dimension",
            "grouping_value",
            "bucket_start_utc",
            "bucket_size_seconds",
            "currency",
            "raw_amount",
            "amount_micro_usd",
            "raw_time_billed",
            "fetched_at_utc",
            "source_contract",
            "allocatable",
            "unavailable_reason",
            "observation_hash",
        }
    ),
    "rate_evidence": frozenset(
        {
            "evidence_id",
            "campaign_id",
            "gpu_id",
            "hourly_rate_usd",
            "source_url",
            "source_version",
            "captured_at_utc",
            "evidence_hash",
        }
    ),
    "samples": frozenset(
        {
            "sample_id",
            "campaign_id",
            "declared_case_id",
            "fixture_case_id",
            "task_type",
            "stage",
            "role",
            "pair_key",
            "seed",
            "profile_id",
            "model",
            "lm_model",
            "duration_seconds",
            "resolved_parameters_json",
            "fingerprint",
            "status",
            "job_id",
            "actual_gpu",
            "execution_ms",
            "estimated_compute_micro_usd",
            "cost_status",
            "cost_reason",
            "output_path",
            "created_at_utc",
            "updated_at_utc",
        }
    ),
    "sample_aliases": frozenset(
        {"alias_id", "campaign_id", "declared_case_id", "canonical_sample_id"}
    ),
    "runtime_rules": frozenset(
        {
            "campaign_id",
            "task_type",
            "duration_seconds",
            "max_execution_ms",
            "error_margin_percent",
            "source_sample_ids_json",
            "created_at_utc",
        }
    ),
    "score_sheets": frozenset(
        {
            "sheet_id",
            "campaign_id",
            "listener_id",
            "stage",
            "sheet_version",
            "rubric_sha256",
            "export_json",
            "imported_json",
            "state",
            "exported_at_utc",
            "finalized_at_utc",
        }
    ),
    "quality_decisions": frozenset(
        {
            "decision_id",
            "campaign_id",
            "decision_json",
            "created_at_utc",
        }
    ),
    "storage_artifacts": frozenset(
        {
            "artifact_id",
            "campaign_id",
            "path",
            "bytes_count",
            "reservation_id",
            "state",
            "created_at_utc",
            "removed_at_utc",
        }
    ),
    "leases": frozenset(
        {"lease_name", "campaign_id", "owner_id", "acquired_at_utc", "expires_at_utc"}
    ),
    "campaign_events": frozenset(
        {"event_id", "campaign_id", "event_type", "event_json", "created_at_utc"}
    ),
    "submission_intents": frozenset(
        {
            "intent_id",
            "campaign_id",
            "sample_id",
            "reservation_id",
            "product_job_uuid",
            "request_fingerprint",
            "source_url",
            "status",
            "created_at_utc",
            "updated_at_utc",
        }
    ),
}


def _assert_known_reservation_states(connection: sqlite3.Connection, *, context: str) -> None:
    """Fail closed when any reservation state outside the four declared states exists.

    The schema CHECK constrains new writes, but a database modified after it
    was opened could still carry a foreign state; every budget, teardown,
    status, recovery, and rollback path that trusts reservation state must
    reject such corruption before it can omit a reservation from totals or
    treat it as resolved by default.
    """

    placeholders = ", ".join("?" for _ in _RESERVATION_STATES)
    row = connection.execute(
        f"SELECT reservation_id, state FROM reservations WHERE state NOT IN ({placeholders}) "
        "LIMIT 1",
        tuple(sorted(_RESERVATION_STATES)),
    ).fetchone()
    if row is not None:
        raise CampaignSchemaError(
            f"corrupt reservation state '{row['state']}' in reservation "
            f"{row['reservation_id']} ({context})"
        )


class CampaignStore:
    """Transactional SQLite store for the private quality campaign."""

    def __init__(self, path: Path, *, create: bool = True, busy_timeout_ms: int = 5000) -> None:
        self.path = path.expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._created = False
        if create:
            self._initialize_or_validate()
        else:
            self._validate_existing()

    @classmethod
    def open_existing(cls, path: Path, *, busy_timeout_ms: int = 5000) -> CampaignStore | None:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return None
        return cls(resolved, create=False, busy_timeout_ms=busy_timeout_ms)

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode=WAL")
            return connection
        except sqlite3.Error as exc:
            raise CampaignSchemaError("campaign database could not be opened") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_or_validate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if self.path.exists() and self.path.stat().st_size > 0:
            self._validate_existing()
            return
        self._created = True
        connection = self._connect()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self._create_schema(connection)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise CampaignSchemaError("campaign database schema creation failed") from exc
        finally:
            connection.close()
        self._chmod_private()

    def _validate_existing(self) -> None:
        if not self.path.is_file():
            raise CampaignSchemaError("campaign database is not a regular file")
        connection = self._connect()
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise CampaignSchemaError("campaign database integrity check failed")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 1 or version == 2:
                # The ordered v1/v2-to-v3 migration is one rollback-safe
                # unit: preflight, every schema/data change, and final
                # validation all succeed before the single commit, so any
                # rejection leaves the source schema version, objects,
                # rows, reservation state, timestamps, and child links
                # exactly as they were.
                self._migrate_legacy_schema(connection, version)
                version = CAMPAIGN_SCHEMA_VERSION
            self._validate_schema_shape(connection, version)
        except CampaignSchemaError:
            raise
        except sqlite3.Error as exc:
            raise CampaignSchemaError("campaign database could not be validated") from exc
        finally:
            connection.close()
        self._chmod_private()

    @staticmethod
    def _validate_schema_shape(connection: sqlite3.Connection, version: int) -> None:
        """Refuse unknown/unsupported schemas and foreign reservation states."""
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if row[0] != "sqlite_sequence"
        }
        if version != CAMPAIGN_SCHEMA_VERSION or tables != _SCHEMA_TABLES:
            raise CampaignSchemaError("campaign database has an unknown or unsupported schema")
        for table_name, expected_columns in _SCHEMA_COLUMNS.items():
            actual_columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            if actual_columns != expected_columns:
                raise CampaignSchemaError("campaign database has an unknown or unsupported schema")
        gate = connection.execute("SELECT singleton FROM maintenance_gate").fetchall()
        if [int(row[0]) for row in gate] != [1]:
            raise CampaignSchemaError("campaign maintenance gate singleton is malformed")
        # Even a current-version database is refused when a reservation
        # state outside the four declared states was injected after the
        # schema was validated.
        _assert_known_reservation_states(connection, context="schema validation")

    @classmethod
    def _migrate_legacy_schema(cls, connection: sqlite3.Connection, version: int) -> None:
        """Upgrade a v1/v2 database to v3 as one rollback-safe migration unit.

        The optional v1-to-v2 additions, the v2-to-v3 reservation rebuild
        with its four-state ``CHECK``, ``PRAGMA user_version=3``, final
        schema/column validation, reservation-state validation, and the
        child-foreign-key check all complete inside one transaction before
        the single commit.  Any rejection rolls the whole unit back and
        restores the source schema version, schema objects, rows,
        reservation state, timestamps, child foreign keys, and temporary
        table state exactly.
        """
        # Preflight before any mutation: foreign reservation states are
        # refused while the source schema still permits reading them, so a
        # corrupt database is rejected with nothing changed.
        _assert_known_reservation_states(connection, context="schema migration")
        allowed = "', '".join(sorted(_RESERVATION_STATES))
        # SQLite requires the foreign-key pragma to change outside a
        # transaction; the parent drop below would otherwise trip
        # storage_artifact foreign keys while they are enforced.  The child
        # REFERENCES clause still resolves against the renamed table
        # afterward, and enforcement is restored once the unit finishes.
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if version == 1:
                    connection.execute(
                        "ALTER TABLE execution_windows ADD COLUMN health_evidence_json TEXT"
                    )
                    connection.execute(
                        "CREATE TABLE submission_intents ("
                        "intent_id TEXT PRIMARY KEY, "
                        "campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id), "
                        "sample_id TEXT NOT NULL REFERENCES samples(sample_id), "
                        "reservation_id TEXT, "
                        "product_job_uuid TEXT NOT NULL UNIQUE, "
                        "request_fingerprint TEXT NOT NULL, "
                        "source_url TEXT, "
                        "status TEXT NOT NULL DEFAULT 'pending', "
                        "created_at_utc TEXT NOT NULL, "
                        "updated_at_utc TEXT NOT NULL, "
                        "CHECK(status IN ('pending', 'submitted')), "
                        "CHECK(length(product_job_uuid) BETWEEN 3 AND 96), "
                        "CHECK(length(request_fingerprint) = 64))"
                    )
                # v2-to-v3: rebuild reservations with the four-state CHECK
                # while copying every reservation ID, campaign/sample link,
                # kind, original amount, state, estimate, unavailable
                # reason, and timestamp verbatim.
                connection.execute(
                    "CREATE TABLE reservations_v3 ("
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
                    "CHECK(reserved_micro_usd >= 0), "
                    f"CHECK(state IN ('{allowed}')))"
                )
                connection.execute(
                    "INSERT INTO reservations_v3(reservation_id, campaign_id, sample_id, kind, "
                    "reserved_micro_usd, state, final_estimate_micro_usd, unavailable_reason, "
                    "created_at_utc, submitted_at_utc, settled_at_utc) "
                    "SELECT reservation_id, campaign_id, sample_id, kind, reserved_micro_usd, state, "
                    "final_estimate_micro_usd, unavailable_reason, created_at_utc, submitted_at_utc, "
                    "settled_at_utc FROM reservations"
                )
                connection.execute("DROP TABLE reservations")
                connection.execute("ALTER TABLE reservations_v3 RENAME TO reservations")
                connection.execute("PRAGMA user_version=3")
                # Final validation must succeed before the migration
                # commits; a violation here rolls the whole unit back.
                cls._validate_schema_shape(connection, CAMPAIGN_SCHEMA_VERSION)
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise CampaignSchemaError(
                        "campaign database migration left foreign-key violations"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            raise CampaignSchemaError("campaign database migration failed") from exc

    def _chmod_private(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                try:
                    candidate.chmod(0o600)
                except OSError:
                    pass

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE campaigns (
                campaign_id TEXT PRIMARY KEY,
                fixture_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                ceiling_micro_usd INTEGER NOT NULL,
                admission_stop_micro_usd INTEGER NOT NULL,
                decision TEXT NOT NULL,
                listener_ids_json TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                authorization_json TEXT,
                retention_deadline_utc TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                reason TEXT,
                CHECK(length(campaign_id) BETWEEN 3 AND 96),
                CHECK(length(fixture_id) BETWEEN 3 AND 96),
                CHECK(length(manifest_sha256) = 64),
                CHECK(ceiling_micro_usd = 5000000),
                CHECK(admission_stop_micro_usd = 4500000)
            );
            CREATE TABLE execution_windows (
                window_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                stage TEXT NOT NULL,
                start_utc TEXT NOT NULL,
                end_utc TEXT,
                state TEXT NOT NULL,
                contaminated INTEGER NOT NULL DEFAULT 0,
                blocked_routes_json TEXT NOT NULL,
                edge_config_sha256 TEXT,
                health_evidence_json TEXT,
                reason TEXT,
                UNIQUE(campaign_id, stage, start_utc)
            );
            CREATE TABLE maintenance_gate (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                active INTEGER NOT NULL DEFAULT 0,
                campaign_id TEXT REFERENCES campaigns(campaign_id),
                window_id INTEGER REFERENCES execution_windows(window_id),
                bypass_campaign_id TEXT,
                edge_guard_enabled INTEGER NOT NULL DEFAULT 0,
                edge_guard_verified INTEGER NOT NULL DEFAULT 0,
                edge_config_sha256 TEXT,
                blocked_routes_json TEXT NOT NULL DEFAULT '[]',
                rollback_target TEXT,
                updated_at_utc TEXT NOT NULL
            );
            INSERT INTO maintenance_gate(singleton, updated_at_utc) VALUES(1, '1970-01-01T00:00:00Z');
            CREATE TABLE reservations (
                reservation_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                sample_id TEXT,
                kind TEXT NOT NULL,
                reserved_micro_usd INTEGER NOT NULL,
                state TEXT NOT NULL,
                final_estimate_micro_usd INTEGER,
                unavailable_reason TEXT,
                created_at_utc TEXT NOT NULL,
                submitted_at_utc TEXT,
                settled_at_utc TEXT,
                UNIQUE(campaign_id, sample_id, kind),
                CHECK(reserved_micro_usd >= 0),
                CHECK(state IN ('open', 'unresolved', 'conservatively_retained', 'settled'))
            );
            CREATE TABLE provider_billing_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                provider TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                grouping_dimension TEXT NOT NULL,
                grouping_value TEXT NOT NULL DEFAULT '',
                bucket_start_utc TEXT NOT NULL,
                bucket_size_seconds INTEGER NOT NULL,
                currency TEXT NOT NULL,
                raw_amount TEXT NOT NULL,
                amount_micro_usd INTEGER NOT NULL,
                raw_time_billed TEXT,
                fetched_at_utc TEXT NOT NULL,
                source_contract TEXT NOT NULL,
                allocatable INTEGER NOT NULL DEFAULT 1,
                unavailable_reason TEXT,
                observation_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE rate_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                gpu_id TEXT NOT NULL,
                hourly_rate_usd TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_version TEXT NOT NULL,
                captured_at_utc TEXT NOT NULL,
                evidence_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE samples (
                sample_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                declared_case_id TEXT NOT NULL,
                fixture_case_id TEXT,
                task_type TEXT NOT NULL,
                stage TEXT NOT NULL,
                role TEXT NOT NULL,
                pair_key TEXT NOT NULL,
                seed INTEGER NOT NULL,
                profile_id TEXT NOT NULL,
                model TEXT NOT NULL,
                lm_model TEXT,
                duration_seconds REAL NOT NULL,
                resolved_parameters_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT,
                actual_gpu TEXT,
                execution_ms INTEGER,
                estimated_compute_micro_usd INTEGER,
                cost_status TEXT,
                cost_reason TEXT,
                output_path TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE(campaign_id, fingerprint),
                CHECK(length(sample_id) BETWEEN 3 AND 96)
            );
            CREATE TABLE sample_aliases (
                alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                declared_case_id TEXT NOT NULL,
                canonical_sample_id TEXT NOT NULL REFERENCES samples(sample_id),
                UNIQUE(campaign_id, declared_case_id)
            );
            CREATE TABLE runtime_rules (
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                task_type TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                max_execution_ms INTEGER NOT NULL,
                error_margin_percent INTEGER NOT NULL,
                source_sample_ids_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(campaign_id, task_type, duration_seconds)
            );
            CREATE TABLE score_sheets (
                sheet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                listener_id TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'screening',
                sheet_version INTEGER NOT NULL,
                rubric_sha256 TEXT NOT NULL,
                export_json TEXT NOT NULL,
                imported_json TEXT,
                state TEXT NOT NULL,
                exported_at_utc TEXT NOT NULL,
                finalized_at_utc TEXT,
                CHECK(stage IN ('screening', 'confirmation')),
                UNIQUE(campaign_id, listener_id, stage)
            );
            CREATE TABLE quality_decisions (
                decision_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                decision_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(campaign_id, decision_id),
                CHECK(length(decision_id) = 64)
            );
            CREATE TABLE storage_artifacts (
                artifact_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                path TEXT NOT NULL,
                bytes_count INTEGER NOT NULL,
                reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
                state TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                removed_at_utc TEXT,
                CHECK(bytes_count > 0)
            );
            CREATE TABLE leases (
                lease_name TEXT PRIMARY KEY,
                campaign_id TEXT,
                owner_id TEXT NOT NULL,
                acquired_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL
            );
            CREATE TABLE campaign_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
            CREATE TABLE submission_intents (
                intent_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                sample_id TEXT NOT NULL REFERENCES samples(sample_id),
                reservation_id TEXT,
                product_job_uuid TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                source_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                CHECK(status IN ('pending', 'submitted')),
                CHECK(length(product_job_uuid) BETWEEN 3 AND 96),
                CHECK(length(request_fingerprint) = 64)
            );
            PRAGMA user_version=3;
            """
        )

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else {key: row[key] for key in row.keys()}

    def _event(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO campaign_events(campaign_id, event_type, event_json, created_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (
                campaign_id,
                _bounded_text(event_type, "event_type"),
                _bounded_json(payload),
                _iso(now),
            ),
        )

    def create_campaign(
        self,
        campaign_id: str,
        manifest: FixtureManifest,
        plan: CampaignPlan,
        *,
        listener_ids: Sequence[str] = ("listener-a", "listener-b"),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        identifier = _bounded_id(campaign_id, "campaign_id")
        if len(listener_ids) != 2 or len(set(listener_ids)) != 2:
            raise CampaignValidationError(
                "exactly two distinct pseudonymous listeners are required"
            )
        listeners = [_bounded_id(listener_id, "listener_id") for listener_id in listener_ids]
        timestamp = now or utc_now()
        plan_mapping = {
            "cases": [
                {
                    "declared_case_id": case.declared_case_id,
                    "fixture_case_id": case.fixture_case_id,
                    "task_type": case.task_type,
                    "stage": case.stage,
                    "role": case.role,
                    "seed": case.seed,
                    "profile_id": case.profile_id,
                    "model": case.model,
                    "lm_model": case.lm_model,
                    "prompt_mode": case.prompt_mode,
                    "duration_seconds": case.duration_seconds,
                    "resolved_parameters": dict(case.resolved_parameters),
                    "pair_key": case.pair_key,
                    "conditional_on": case.conditional_on,
                    "requires_storage": case.requires_storage,
                }
                for case in plan.cases
            ],
            "minimum_jobs": plan.minimum_jobs,
            "maximum_jobs": plan.maximum_jobs,
            "minimum_paid_attempts": plan.minimum_paid_attempts,
            "maximum_paid_attempts": plan.maximum_paid_attempts,
        }
        plan_json = _bounded_json(plan_mapping, "campaign plan")
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO campaigns(campaign_id, fixture_id, manifest_sha256, status, "
                    "ceiling_micro_usd, admission_stop_micro_usd, decision, listener_ids_json, "
                    "plan_json, retention_deadline_utc, created_at_utc, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identifier,
                        manifest.fixture_id,
                        manifest.manifest_sha256,
                        "planned",
                        manifest.ceiling_micro_usd,
                        manifest.admission_stop_micro_usd,
                        "not_admitted",
                        _bounded_json(listeners, "listener IDs"),
                        plan_json,
                        _iso(manifest.retention_deadline),
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignValidationError("campaign ID is already in use") from exc
            self._event(
                connection,
                identifier,
                "campaign_created",
                {"fixture_id": manifest.fixture_id, "manifest_sha256": manifest.manifest_sha256},
                now=timestamp,
            )
        self._chmod_private()
        return self.get_campaign(identifier) or {}

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id=?", (identifier,)
            ).fetchone()
        return self._row_dict(row)

    def campaign_plan(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignValidationError("unknown campaign")
        value = _load_json(str(campaign["plan_json"]), "campaign plan")
        if not isinstance(value, dict):
            raise CampaignSchemaError("stored campaign plan is malformed")
        return value

    def set_campaign_status(
        self,
        campaign_id: str,
        status: str,
        *,
        decision: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        allowed = {
            "planned",
            "running",
            "awaiting_scores",
            "screening_complete",
            "awaiting_confirmation",
            "complete",
            "failed",
            "stopped",
        }
        if status not in allowed:
            raise CampaignValidationError("campaign status is unsupported")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM campaigns WHERE campaign_id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise CampaignValidationError("unknown campaign")
            connection.execute(
                "UPDATE campaigns SET status=?, decision=COALESCE(?, decision), reason=?, updated_at_utc=? "
                "WHERE campaign_id=?",
                (status, decision, reason, _iso(timestamp), identifier),
            )
            self._event(
                connection,
                identifier,
                "campaign_status",
                {"status": status, "decision": decision, "reason": reason},
                now=timestamp,
            )

    def record_authorization(
        self,
        campaign_id: str,
        authorization: RemoteChangeAuthorization,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        payload = authorization.to_private_mapping()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT authorization_json FROM campaigns WHERE campaign_id=?", (identifier,)
            ).fetchone()
            if existing is None:
                raise CampaignValidationError("unknown campaign")
            if existing["authorization_json"] is not None:
                if str(existing["authorization_json"]) != _bounded_json(payload, "authorization"):
                    raise CampaignGateError("remote authorization is immutable")
                return
            result = connection.execute(
                "UPDATE campaigns SET authorization_json=?, updated_at_utc=? WHERE campaign_id=?",
                (_bounded_json(payload, "authorization"), _iso(timestamp), identifier),
            )
            if result.rowcount != 1:
                raise CampaignValidationError("unknown campaign")
            self._event(connection, identifier, "remote_authorization_recorded", {}, now=timestamp)

    def add_sample(
        self,
        campaign_id: str,
        case: CampaignCase,
        *,
        fixture_id: str,
        runtime_id: str = "ace-step-v0.1.8",
        image_digest: str = "unrecorded",
        now: datetime | None = None,
    ) -> tuple[str, bool]:
        identifier = _bounded_id(campaign_id, "campaign_id")
        fingerprint = case.fingerprint(
            fixture_id=fixture_id, runtime_id=runtime_id, image_digest=image_digest
        )
        timestamp = now or utc_now()
        sample_id = f"s-{secrets.token_urlsafe(18).replace('-', '_')}"[:64]
        _bounded_id(sample_id, "sample_id")
        params_json = _bounded_json(dict(case.resolved_parameters), "resolved parameters")
        with self.transaction() as connection:
            declared = connection.execute(
                "SELECT sample_id, fingerprint FROM samples WHERE campaign_id=? AND declared_case_id=?",
                (identifier, case.declared_case_id),
            ).fetchone()
            if declared is not None:
                if str(declared["fingerprint"]) != fingerprint:
                    raise CampaignValidationError(
                        "declared case ID is already mapped to another fingerprint"
                    )
                return str(declared["sample_id"]), False
            existing = connection.execute(
                "SELECT sample_id, declared_case_id, status, stage, role, seed, task_type FROM samples "
                "WHERE campaign_id=? AND fingerprint=?",
                (identifier, fingerprint),
            ).fetchone()
            if existing is not None:
                existing_id = str(existing["sample_id"])
                existing_status = str(existing["status"])
                if existing_status in {"planned", "deduplicated"}:
                    return self._record_alias(connection, identifier, existing_id, case, timestamp)
                if self._confirmation_reuse_allowed(existing, case):
                    # The screening seed is confirmation seed one, so the
                    # confirmation case for that seed is the already-executed
                    # screening sample.  Reuse its exact fingerprint instead of
                    # paying to regenerate an identical sample.
                    return self._record_alias(connection, identifier, existing_id, case, timestamp)
                raise CampaignGateError(
                    "exact-fingerprint deduplication was requested after execution"
                )
            connection.execute(
                "INSERT INTO samples(sample_id, campaign_id, declared_case_id, fixture_case_id, task_type, "
                "stage, role, pair_key, seed, profile_id, model, lm_model, duration_seconds, "
                "resolved_parameters_json, fingerprint, status, created_at_utc, updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
                (
                    sample_id,
                    identifier,
                    case.declared_case_id,
                    case.fixture_case_id,
                    case.task_type,
                    case.stage,
                    case.role,
                    case.pair_key,
                    case.seed,
                    case.profile_id,
                    case.model,
                    case.lm_model,
                    case.duration_seconds,
                    params_json,
                    fingerprint,
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )
            self._event(
                connection,
                identifier,
                "sample_declared",
                {"sample_id": sample_id, "stage": case.stage, "role": case.role},
                now=timestamp,
            )
        self._chmod_private()
        return sample_id, True

    @staticmethod
    def _confirmation_reuse_allowed(existing: sqlite3.Row, case: CampaignCase) -> bool:
        """Allow only the declared screening-seed reuse, never contamination.

        The screening seed is confirmation seed one, so a confirmation case may
        reuse the already-executed screening sample only when the fingerprint
        matched exactly (checked by the caller), the existing sample is a
        completed screening output, and the task type, role, and seed all agree
        with the confirmation role being declared.  A failed/uncertain
        screening sample, an incompatible profile/fixture fingerprint, or a
        mismatched role is rejected rather than silently reused or recharged.
        """

        if case.stage not in {"cover-confirmation", "original-confirmation"}:
            return False
        if case.conditional_on is not None:
            return False
        if str(existing["status"]) != "completed":
            return False
        if str(existing["stage"]) not in {"cover-screen", "original-screen"}:
            return False
        if str(existing["task_type"]) != case.task_type:
            return False
        if str(existing["role"]) != case.role:
            return False
        if isinstance(existing["seed"], bool) or not isinstance(existing["seed"], int):
            return False
        return int(existing["seed"]) == case.seed

    @staticmethod
    def _alias_sample(
        connection: sqlite3.Connection,
        campaign_id: str,
        canonical_sample_id: str,
        case: CampaignCase,
        timestamp: datetime,
    ) -> tuple[str, bool]:
        """Map one declared role to the canonical sample, refusing unsafe aliases."""

        try:
            connection.execute(
                "INSERT INTO sample_aliases(campaign_id, declared_case_id, canonical_sample_id) "
                "VALUES (?, ?, ?)",
                (campaign_id, case.declared_case_id, canonical_sample_id),
            )
        except sqlite3.IntegrityError as exc:
            alias = connection.execute(
                "SELECT canonical_sample_id FROM sample_aliases WHERE campaign_id=? AND declared_case_id=?",
                (campaign_id, case.declared_case_id),
            ).fetchone()
            if alias is None or str(alias["canonical_sample_id"]) != canonical_sample_id:
                raise CampaignValidationError("declared case ID is already mapped") from exc
        return canonical_sample_id, False

    def _record_alias(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
        canonical_sample_id: str,
        case: CampaignCase,
        timestamp: datetime,
    ) -> tuple[str, bool]:
        sample_id, created = self._alias_sample(
            connection, campaign_id, canonical_sample_id, case, timestamp
        )
        self._event(
            connection,
            campaign_id,
            "sample_alias_created",
            {"declared_case_id": case.declared_case_id, "canonical_sample_id": sample_id},
            now=timestamp,
        )
        return sample_id, created

    def list_samples(
        self, campaign_id: str, *, include_aliases: bool = False
    ) -> list[dict[str, Any]]:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM samples WHERE campaign_id=? ORDER BY rowid", (identifier,)
            ).fetchall()
            aliases = (
                connection.execute(
                    "SELECT declared_case_id, canonical_sample_id FROM sample_aliases "
                    "WHERE campaign_id=? ORDER BY alias_id",
                    (identifier,),
                ).fetchall()
                if include_aliases
                else []
            )
        result = [self._row_dict(row) or {} for row in rows]
        if include_aliases:
            for item in result:
                item["aliases"] = [
                    str(alias["declared_case_id"])
                    for alias in aliases
                    if alias["canonical_sample_id"] == item["sample_id"]
                ]
        return result

    def sample(self, sample_id: str) -> dict[str, Any] | None:
        identifier = _bounded_id(sample_id, "sample_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM samples WHERE sample_id=?", (identifier,)
            ).fetchone()
        return self._row_dict(row)

    def add_rate_evidence(
        self,
        campaign_id: str,
        evidence: RateEvidence,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        evidence_hash = hashlib.sha256(
            _bounded_json(
                {
                    "gpu_id": evidence.gpu_id,
                    "hourly_rate_usd": evidence.hourly_rate_usd,
                    "source_url": evidence.source_url,
                    "source_version": evidence.source_version,
                    "captured_at_utc": _iso(evidence.captured_at),
                },
                "rate evidence",
            ).encode()
        ).hexdigest()
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO rate_evidence(campaign_id, gpu_id, hourly_rate_usd, source_url, "
                "source_version, captured_at_utc, evidence_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    evidence.gpu_id,
                    evidence.hourly_rate_usd,
                    evidence.source_url,
                    evidence.source_version,
                    _iso(evidence.captured_at),
                    evidence_hash,
                ),
            )
            self._event(
                connection,
                identifier,
                "rate_evidence_recorded",
                {"gpu_id": evidence.gpu_id},
                now=timestamp,
            )

    def set_runtime_rule(
        self,
        campaign_id: str,
        rule: RuntimeRule,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT max_execution_ms, error_margin_percent, source_sample_ids_json "
                "FROM runtime_rules WHERE campaign_id=? AND task_type=? AND duration_seconds=?",
                (identifier, rule.task_type, rule.duration_seconds),
            ).fetchone()
            source_json = _bounded_json(list(rule.source_sample_ids), "runtime rule sources")
            if existing is not None:
                if (
                    int(existing["max_execution_ms"]) != rule.max_execution_ms
                    or int(existing["error_margin_percent"]) != rule.error_margin_percent
                    or str(existing["source_sample_ids_json"]) != source_json
                ):
                    raise CampaignGateError("runtime rule is immutable")
                return
            connection.execute(
                "INSERT INTO runtime_rules(campaign_id, task_type, duration_seconds, max_execution_ms, "
                "error_margin_percent, source_sample_ids_json, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    rule.task_type,
                    rule.duration_seconds,
                    rule.max_execution_ms,
                    rule.error_margin_percent,
                    source_json,
                    _iso(timestamp),
                ),
            )
            self._event(
                connection,
                identifier,
                "runtime_rule_frozen",
                {
                    "task_type": rule.task_type,
                    "duration_seconds": rule.duration_seconds,
                    "reserved_execution_ms": rule.reserved_execution_ms,
                },
                now=timestamp,
            )

    def get_runtime_rule(
        self, campaign_id: str, task_type: str, duration_seconds: float
    ) -> RuntimeRule | None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_rules WHERE campaign_id=? AND task_type=? AND duration_seconds=?",
                (identifier, task_type, duration_seconds),
            ).fetchone()
        if row is None:
            return None
        values = _load_json(str(row["source_sample_ids_json"]), "runtime rule sources")
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise CampaignSchemaError("stored runtime rule sources are malformed")
        return RuntimeRule(
            task_type=str(row["task_type"]),
            duration_seconds=float(row["duration_seconds"]),
            max_execution_ms=int(row["max_execution_ms"]),
            error_margin_percent=int(row["error_margin_percent"]),
            source_sample_ids=tuple(values),
        )

    def _provider_total(self, connection: sqlite3.Connection, campaign_id: str) -> int:
        rows = connection.execute(
            "SELECT provider, resource_type, grouping_dimension, grouping_value, bucket_start_utc, "
            "bucket_size_seconds, currency, amount_micro_usd, fetched_at_utc "
            "FROM provider_billing_observations WHERE campaign_id=? AND allocatable=1 "
            "AND resource_type='endpoint' "
            "ORDER BY fetched_at_utc, observation_id",
            (campaign_id,),
        ).fetchall()
        current: dict[tuple[str, str, str, str, str, int, str], int] = {}
        fetched: dict[tuple[str, str, str, str, str, int, str], str] = {}
        for row in rows:
            key = (
                str(row["provider"]),
                str(row["resource_type"]),
                str(row["grouping_dimension"]),
                str(row["grouping_value"]),
                str(row["bucket_start_utc"]),
                int(row["bucket_size_seconds"]),
                str(row["currency"]),
            )
            if key not in fetched or str(row["fetched_at_utc"]) >= fetched[key]:
                fetched[key] = str(row["fetched_at_utc"])
                current[key] = int(row["amount_micro_usd"])
        return sum(current.values())

    @staticmethod
    def _settled_total(connection: sqlite3.Connection, campaign_id: str) -> int:
        _assert_known_reservation_states(connection, context="committed spend")
        row = connection.execute(
            "SELECT COALESCE(SUM(final_estimate_micro_usd), 0) AS total FROM reservations "
            "WHERE campaign_id=? AND state='settled'",
            (campaign_id,),
        ).fetchone()
        return int(row["total"] if row is not None else 0)

    @staticmethod
    def _open_reservation_total(connection: sqlite3.Connection, campaign_id: str) -> int:
        _assert_known_reservation_states(connection, context="committed spend")
        retention = ", ".join("?" for _ in sorted(_BUDGET_RETENTION_STATES))
        row = connection.execute(
            "SELECT COALESCE(SUM(reserved_micro_usd), 0) AS total FROM reservations "
            f"WHERE campaign_id=? AND state IN ({retention})",
            (campaign_id, *sorted(_BUDGET_RETENTION_STATES)),
        ).fetchone()
        return int(row["total"] if row is not None else 0)

    def admission_summary(
        self,
        campaign_id: str,
        next_reservation_micro_usd: int = 0,
    ) -> AdmissionSummary:
        identifier = _bounded_id(campaign_id, "campaign_id")
        if isinstance(next_reservation_micro_usd, bool) or next_reservation_micro_usd < 0:
            raise CampaignValidationError("next reservation must be non-negative micro-USD")
        with self.read() as connection:
            return AdmissionSummary(
                provider_micro_usd=self._provider_total(connection, identifier),
                settled_estimate_micro_usd=self._settled_total(connection, identifier),
                open_reservation_micro_usd=self._open_reservation_total(connection, identifier),
                next_reservation_micro_usd=next_reservation_micro_usd,
            )

    def reserve(
        self,
        campaign_id: str,
        reservation_id: str,
        *,
        kind: str,
        reserved_micro_usd: int,
        sample_id: str | None = None,
        now: datetime | None = None,
    ) -> AdmissionSummary:
        identifier = _bounded_id(campaign_id, "campaign_id")
        reservation_identifier = _bounded_id(reservation_id, "reservation_id")
        if kind not in {"compatibility", "compute", "storage"}:
            raise CampaignValidationError("reservation kind is unsupported")
        if (
            isinstance(reserved_micro_usd, bool)
            or not isinstance(reserved_micro_usd, int)
            or reserved_micro_usd < 0
        ):
            raise CampaignValidationError("reservation amount must be non-negative micro-USD")
        if sample_id is not None:
            _bounded_id(sample_id, "sample_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            campaign = connection.execute(
                "SELECT ceiling_micro_usd, admission_stop_micro_usd, status FROM campaigns "
                "WHERE campaign_id=?",
                (identifier,),
            ).fetchone()
            if campaign is None:
                raise CampaignValidationError("unknown campaign")
            if str(campaign["status"]) in {"failed", "stopped", "complete"}:
                raise CampaignGateError("campaign is not accepting reservations")
            if sample_id is not None:
                duplicate = connection.execute(
                    "SELECT reservation_id FROM reservations WHERE campaign_id=? AND sample_id=? AND kind=?",
                    (identifier, sample_id, kind),
                ).fetchone()
                if duplicate is not None:
                    raise CampaignValidationError("reservation already exists for this sample/kind")
            provider = self._provider_total(connection, identifier)
            settled = self._settled_total(connection, identifier)
            open_total = self._open_reservation_total(connection, identifier)
            summary = AdmissionSummary(provider, settled, open_total, reserved_micro_usd)
            if provider >= int(campaign["ceiling_micro_usd"]):
                raise CampaignBudgetError(
                    "fetched provider billing has reached the campaign ceiling"
                )
            if summary.admission_total_micro_usd > int(campaign["admission_stop_micro_usd"]):
                raise CampaignBudgetError("reservation exceeds the campaign admission stop")
            connection.execute(
                "INSERT INTO reservations(reservation_id, campaign_id, sample_id, kind, reserved_micro_usd, "
                "state, created_at_utc) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (
                    reservation_identifier,
                    identifier,
                    sample_id,
                    kind,
                    reserved_micro_usd,
                    _iso(timestamp),
                ),
            )
            self._event(
                connection,
                identifier,
                "reservation_opened",
                {
                    "reservation_id": reservation_identifier,
                    "sample_id": sample_id,
                    "kind": kind,
                    "reserved_micro_usd": reserved_micro_usd,
                    "admission_total_micro_usd": summary.admission_total_micro_usd,
                },
                now=timestamp,
            )
        self._chmod_private()
        return summary

    def mark_reservation_submitted(
        self,
        campaign_id: str,
        reservation_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        reservation_identifier = _bounded_id(reservation_id, "reservation_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE reservations SET submitted_at_utc=? WHERE campaign_id=? AND reservation_id=? "
                "AND state='open'",
                (_iso(timestamp), identifier, reservation_identifier),
            )
            if result.rowcount != 1:
                raise CampaignGateError("reservation is not open for submission")

    def settle_reservation(
        self,
        campaign_id: str,
        reservation_id: str,
        *,
        estimate_micro_usd: int | None,
        unavailable_reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        reservation_identifier = _bounded_id(reservation_id, "reservation_id")
        if estimate_micro_usd is not None and (
            isinstance(estimate_micro_usd, bool)
            or not isinstance(estimate_micro_usd, int)
            or estimate_micro_usd < 0
        ):
            raise CampaignValidationError("terminal estimate must be non-negative micro-USD")
        if estimate_micro_usd is None:
            reason = _bounded_text(unavailable_reason, "unavailable_reason", max_length=256)
            state = "unresolved"
        else:
            reason = None
            state = "settled"
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state, final_estimate_micro_usd, unavailable_reason FROM reservations "
                "WHERE campaign_id=? AND reservation_id=?",
                (identifier, reservation_identifier),
            ).fetchone()
            if row is None:
                raise CampaignValidationError("unknown reservation")
            if row["final_estimate_micro_usd"] is not None or row["unavailable_reason"] is not None:
                if (
                    row["final_estimate_micro_usd"] != estimate_micro_usd
                    or row["unavailable_reason"] != reason
                ):
                    raise CampaignGateError("terminal reservation evidence is immutable")
                return
            connection.execute(
                "UPDATE reservations SET state=?, final_estimate_micro_usd=?, unavailable_reason=?, "
                "settled_at_utc=? WHERE campaign_id=? AND reservation_id=?",
                (
                    state,
                    estimate_micro_usd,
                    reason,
                    _iso(timestamp),
                    identifier,
                    reservation_identifier,
                ),
            )
            self._event(
                connection,
                identifier,
                "reservation_settled",
                {
                    "reservation_id": reservation_identifier,
                    "state": state,
                    "estimate_micro_usd": estimate_micro_usd,
                    "reason": reason,
                },
                now=timestamp,
            )

    def reservation_for_sample(self, campaign_id: str, sample_id: str) -> dict[str, Any] | None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM reservations WHERE campaign_id=? AND sample_id=? ORDER BY created_at_utc DESC LIMIT 1",
                (identifier, sample_identifier),
            ).fetchone()
        return self._row_dict(row)

    def record_terminal_execution(
        self,
        campaign_id: str,
        sample_id: str,
        *,
        status: str,
        actual_gpu: str | None,
        execution_ms: int | None,
        hourly_rate_usd: Any | None,
        unavailable_reason: str | None = None,
        output_path: str | None = None,
        now: datetime | None = None,
    ) -> int | None:
        """Persist immutable attempt evidence; unknown never becomes invented zero."""

        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        if status not in _TERMINAL_SAMPLE_STATES | {"uncertain"}:
            raise CampaignValidationError("sample terminal status is unsupported")
        if actual_gpu is not None:
            _bounded_id(actual_gpu, "actual_gpu")
        if execution_ms is not None and (
            isinstance(execution_ms, bool) or not isinstance(execution_ms, int) or execution_ms < 0
        ):
            raise CampaignValidationError("execution_ms must be a non-negative integer")
        if status == "completed" and (
            not isinstance(output_path, str) or not output_path.strip() or len(output_path) > 1024
        ):
            raise CampaignValidationError("completed samples require generated-output evidence")
        if status != "completed" and output_path is not None:
            raise CampaignValidationError("non-completed samples cannot carry an output path")
        estimate: int | None = None
        cost_status = "unavailable"
        reason = unavailable_reason
        if status == "unsubmitted":
            if execution_ms not in {None, 0}:
                raise CampaignValidationError("an unsubmitted attempt cannot have execution time")
            if actual_gpu is not None:
                raise CampaignValidationError("an unsubmitted attempt cannot have a GPU")
            execution_ms = None
            estimate = None
            cost_status = "not_submitted"
            reason = None
        elif (
            status == "completed"
            and actual_gpu is not None
            and execution_ms is not None
            and execution_ms > 0
            and hourly_rate_usd is not None
        ):
            # Only completed attempts with authoritative GPU/execution/rate
            # evidence are ever billed; a failed, cancelled, or uncertain
            # attempt stays unavailable even if evidence later arrives.
            estimate = execution_micro_usd(execution_ms, hourly_rate_usd)
            cost_status = "estimated_compute"
            reason = None
        elif execution_ms == 0 and status == "cancelled":
            estimate = 0
            cost_status = "zero_not_started"
            reason = None
        else:
            reason = reason or "missing_gpu_execution_or_trusted_rate"
            reason = _bounded_text(reason, "unavailable_reason", max_length=256)
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM samples WHERE campaign_id=? AND sample_id=?",
                (identifier, sample_identifier),
            ).fetchone()
            if row is None:
                raise CampaignValidationError("unknown sample")
            prior_status = str(row["status"])
            terminal_prior = prior_status in _TERMINAL_SAMPLE_STATES
            # An uncertain sample may advance once to a compatible terminal
            # outcome, and a completed sample with missing cost inputs may
            # fill those inputs in place; every other prior record is
            # immutable, so a later conflicting status, output, GPU,
            # execution, reason, or estimate is refused instead of rewriting
            # the terminal identity.
            uncertain_reconciling = prior_status == "uncertain" and status in (
                _TERMINAL_SAMPLE_STATES - {"unsubmitted"}
            )
            existing_values = (
                row["actual_gpu"],
                row["execution_ms"],
                row["estimated_compute_micro_usd"],
                row["cost_status"],
                row["cost_reason"],
                row["output_path"],
            )
            new_values = (actual_gpu, execution_ms, estimate, cost_status, reason, output_path)
            existing_estimate = row["estimated_compute_micro_usd"]
            completing_unavailable = (
                existing_estimate is None
                and estimate is not None
                and row["cost_status"] == "unavailable"
                and prior_status in {"completed", "uncertain"}
            )
            if terminal_prior and status != prior_status:
                raise CampaignGateError("terminal sample identity is immutable")
            if prior_status == "uncertain" and status == "unsubmitted":
                raise CampaignGateError("terminal sample identity is immutable")
            if any(value is not None for value in existing_values):
                if existing_values == new_values and status == prior_status:
                    value = row["estimated_compute_micro_usd"]
                    return int(value) if value is not None else None
                if not completing_unavailable and not uncertain_reconciling:
                    raise CampaignGateError("terminal sample evidence is immutable")
            if (
                status != "unsubmitted"
                and not completing_unavailable
                and not uncertain_reconciling
                and prior_status
                not in {
                    "submitted",
                    "running",
                    "uncertain",
                }
            ):
                raise CampaignGateError("terminal evidence requires a submitted campaign sample")
            if completing_unavailable or uncertain_reconciling:
                # Preserve compatible prior identity while adding the missing
                # evidence; conflicting fields fail closed before any sample,
                # reservation, event, or timestamp mutation.
                if row["actual_gpu"] is not None and row["actual_gpu"] != actual_gpu:
                    raise CampaignGateError(
                        "authoritative GPU evidence conflicts with prior evidence"
                    )
                if row["execution_ms"] is not None and row["execution_ms"] != execution_ms:
                    raise CampaignGateError(
                        "authoritative execution evidence conflicts with prior evidence"
                    )
                if row["output_path"] is not None and row["output_path"] != output_path:
                    raise CampaignGateError(
                        "completed output identity conflicts with prior evidence"
                    )
                if (
                    prior_status == "completed"
                    and row["cost_reason"] is not None
                    and unavailable_reason is not None
                    and unavailable_reason != row["cost_reason"]
                ):
                    raise CampaignGateError("reason evidence conflicts with prior evidence")
                actual_gpu = row["actual_gpu"] or actual_gpu
                execution_ms = (
                    row["execution_ms"] if row["execution_ms"] is not None else execution_ms
                )
                output_path = row["output_path"] or output_path
            reservation_estimate = 0 if status == "unsubmitted" else estimate
            if status == "unsubmitted":
                reservation_state = "settled"
            elif status == "uncertain":
                # In-flight/uncertain work is not terminal for financial
                # purposes: the reservation stays unresolved with its full
                # immutable amount and keeps blocking teardown and rollback.
                reservation_state = "unresolved"
            elif estimate is None:
                # Durable terminal attempt whose attributable compute is
                # unknown: keep the full immutable original reservation
                # counted in budget totals, but do not present it as executed
                # compute or an in-flight/unresolved reservation.  Verified
                # teardown may treat it as financially resolved only after
                # provider zero.
                reservation_state = CONSERVATIVELY_RETAINED_STATE
            else:
                reservation_state = "settled"
            reservation_reason = None if status == "unsubmitted" else reason
            connection.execute(
                "UPDATE samples SET status=?, actual_gpu=?, execution_ms=?, estimated_compute_micro_usd=?, "
                "cost_status=?, cost_reason=?, output_path=?, updated_at_utc=? "
                "WHERE campaign_id=? AND sample_id=?",
                (
                    status,
                    actual_gpu,
                    execution_ms,
                    estimate,
                    cost_status,
                    reason,
                    output_path,
                    _iso(timestamp),
                    identifier,
                    sample_identifier,
                ),
            )
            reservation = connection.execute(
                "SELECT reservation_id FROM reservations WHERE campaign_id=? AND sample_id=? "
                "AND kind='compute' ORDER BY created_at_utc DESC LIMIT 1",
                (identifier, sample_identifier),
            ).fetchone()
            if reservation is not None:
                if status == "uncertain":
                    # Uncertain work stays unresolved: no final estimate, no
                    # unavailable reason, and no settlement timestamp.
                    connection.execute(
                        "UPDATE reservations SET state='unresolved' WHERE reservation_id=? AND "
                        "(final_estimate_micro_usd IS NULL OR state='unresolved' "
                        "OR state='conservatively_retained')",
                        (reservation["reservation_id"],),
                    )
                else:
                    connection.execute(
                        "UPDATE reservations SET state=?, final_estimate_micro_usd=?, unavailable_reason=?, "
                        "settled_at_utc=? WHERE reservation_id=? AND "
                        "(final_estimate_micro_usd IS NULL OR state='unresolved' "
                        "OR state='conservatively_retained')",
                        (
                            reservation_state,
                            reservation_estimate,
                            reservation_reason,
                            _iso(timestamp),
                            reservation["reservation_id"],
                        ),
                    )
            self._event(
                connection,
                identifier,
                "terminal_execution_recorded",
                {
                    "sample_id": sample_identifier,
                    "status": status,
                    "cost_status": cost_status,
                    "estimate_micro_usd": estimate,
                    "reason": reason,
                },
                now=timestamp,
            )
        return None if status == "unsubmitted" else estimate

    def record_operational_evidence(
        self,
        campaign_id: str,
        sample_id: str,
        *,
        actual_gpu: str,
        image_digest: str,
        cold_start_ms: int | None,
        warm_start: bool,
        peak_vram_bytes: int | None,
        returned_to_zero: bool,
        timeout_accepted: bool,
        now: datetime | None = None,
    ) -> None:
        """Persist bounded worker/endpoint acceptance evidence privately."""

        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        gpu = _bounded_id(actual_gpu, "actual_gpu")
        digest = _bounded_text(image_digest, "image_digest", max_length=256)
        if cold_start_ms is not None and (
            isinstance(cold_start_ms, bool)
            or not isinstance(cold_start_ms, int)
            or cold_start_ms < 0
        ):
            raise CampaignValidationError("cold-start time must be a non-negative integer")
        if peak_vram_bytes is not None and (
            isinstance(peak_vram_bytes, bool)
            or not isinstance(peak_vram_bytes, int)
            or peak_vram_bytes <= 0
        ):
            raise CampaignValidationError("peak VRAM must be a positive integer")
        timestamp = now or utc_now()
        payload = {
            "sample_id": sample_identifier,
            "actual_gpu": gpu,
            "image_digest": digest,
            "cold_start_ms": cold_start_ms,
            "warm_start": bool(warm_start),
            "peak_vram_bytes": peak_vram_bytes,
            "returned_to_zero": bool(returned_to_zero),
            "timeout_accepted": bool(timeout_accepted),
        }
        with self.transaction() as connection:
            sample = connection.execute(
                "SELECT sample_id FROM samples WHERE campaign_id=? AND sample_id=?",
                (identifier, sample_identifier),
            ).fetchone()
            if sample is None:
                raise CampaignValidationError("unknown sample")
            self._event(connection, identifier, "operational_evidence", payload, now=timestamp)

    def mark_sample_submitted(
        self,
        campaign_id: str,
        sample_id: str,
        job_id: str,
        *,
        reservation_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        job_identifier = _bounded_id(job_id, "job_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, job_id FROM samples WHERE campaign_id=? AND sample_id=?",
                (identifier, sample_identifier),
            ).fetchone()
            if row is None:
                raise CampaignValidationError("unknown sample")
            if row["job_id"] is not None:
                if row["job_id"] != job_identifier:
                    raise CampaignGateError("sample job identity is immutable")
                if reservation_id is not None:
                    reservation_identifier = _bounded_id(reservation_id, "reservation_id")
                    result = connection.execute(
                        "UPDATE reservations SET submitted_at_utc=? WHERE campaign_id=? AND reservation_id=? "
                        "AND sample_id=? AND state='open'",
                        (_iso(timestamp), identifier, reservation_identifier, sample_identifier),
                    )
                    if result.rowcount != 1:
                        # An identical retry after a crash may find the
                        # reservation already linked to this same job; that is
                        # idempotent recovery, not a new submission.
                        existing_reservation = connection.execute(
                            "SELECT submitted_at_utc FROM reservations WHERE campaign_id=? "
                            "AND reservation_id=? AND sample_id=?",
                            (identifier, reservation_identifier, sample_identifier),
                        ).fetchone()
                        if existing_reservation is None or existing_reservation[0] is None:
                            raise CampaignGateError("sample reservation is not open")
                return
            if row["status"] not in {"planned", "deduplicated"}:
                raise CampaignGateError("sample is not eligible for first submission")
            connection.execute(
                "UPDATE samples SET status='submitted', job_id=?, updated_at_utc=? "
                "WHERE campaign_id=? AND sample_id=?",
                (job_identifier, _iso(timestamp), identifier, sample_identifier),
            )
            if reservation_id is not None:
                reservation_identifier = _bounded_id(reservation_id, "reservation_id")
                result = connection.execute(
                    "UPDATE reservations SET submitted_at_utc=? WHERE campaign_id=? AND reservation_id=? "
                    "AND sample_id=? AND state='open'",
                    (_iso(timestamp), identifier, reservation_identifier, sample_identifier),
                )
                if result.rowcount != 1:
                    raise CampaignGateError("sample reservation is not open")
            self._event(
                connection,
                identifier,
                "sample_submitted",
                {"sample_id": sample_identifier, "job_id": job_identifier},
                now=timestamp,
            )

    def persist_submission_intent(
        self,
        campaign_id: str,
        sample_id: str,
        product_job_uuid: str,
        request_fingerprint: str,
        *,
        reservation_id: str | None = None,
        source_url: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Persist the frozen preassigned product UUID before any product row exists.

        The intent row is the durable no-duplicate boundary between the two
        SQLite databases: recovery accepts an existing product row only when
        it matches this frozen intent exactly.
        """

        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        product_identifier = _bounded_id(product_job_uuid, "product_job_uuid")
        if not _SHA256_RE.fullmatch(request_fingerprint):
            raise CampaignValidationError("submission request fingerprint is malformed")
        reservation_identifier = (
            _bounded_id(reservation_id, "reservation_id") if reservation_id is not None else None
        )
        bounded_source = (
            _bounded_text(source_url, "source_url", max_length=1024)
            if source_url is not None
            else None
        )
        intent_id = f"int-{product_identifier}"
        timestamp = now or utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT request_fingerprint FROM submission_intents "
                "WHERE campaign_id=? AND sample_id=?",
                (identifier, sample_identifier),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise CampaignGateError("submission intent conflicts with the frozen request")
                return
            conflict = connection.execute(
                "SELECT campaign_id, sample_id FROM submission_intents WHERE product_job_uuid=?",
                (product_identifier,),
            ).fetchone()
            if conflict is not None:
                raise CampaignGateError(
                    "product job UUID is already reserved by another submission intent"
                )
            connection.execute(
                "INSERT INTO submission_intents(intent_id, campaign_id, sample_id, reservation_id, "
                "product_job_uuid, request_fingerprint, source_url, status, created_at_utc, "
                "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    intent_id,
                    identifier,
                    sample_identifier,
                    reservation_identifier,
                    product_identifier,
                    request_fingerprint,
                    bounded_source,
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )

    def get_submission_intent(self, campaign_id: str, sample_id: str) -> dict[str, Any] | None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE campaign_id=? AND sample_id=?",
                (identifier, sample_identifier),
            ).fetchone()
        return self._row_dict(row)

    def list_submission_intents(self, campaign_id: str) -> list[dict[str, Any]]:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM submission_intents WHERE campaign_id=? "
                "ORDER BY created_at_utc, intent_id",
                (identifier,),
            ).fetchall()
        return [self._row_dict(row) or {} for row in rows]

    def reconcile_pre_intent_samples(
        self, campaign_id: str, *, now: datetime | None = None
    ) -> list[str]:
        """Settle exactly the post-reservation/pre-intent crash boundary as unsubmitted.

        The enforced submission ordering persists the compute reservation
        before the submission intent, so a frozen sample that is still
        ``planned`` with exactly one open compute reservation, no
        ``submitted_at_utc``, no submission intent, and no product-job link
        proves that no remote submission or product job was ever created.
        This transition records that exact state as proven unsubmitted and
        settles the reservation at zero without creating a product job,
        calling the controller, or contacting the provider.  Any planned
        sample with an open compute reservation that deviates from that exact
        state (a submitted timestamp, duplicate reservations, non-compute
        reservations) is contradictory and fails closed.  Samples with a job
        link or a submission intent belong to the intent-present recovery
        phase and are left untouched.  The transition is atomic and
        idempotent: a second call finds no matching planned samples.
        """

        identifier = _bounded_id(campaign_id, "campaign_id")
        if self.get_campaign(identifier) is None:
            raise CampaignValidationError("unknown campaign")
        timestamp = now or utc_now()
        reconciled: list[str] = []
        with self.transaction() as connection:
            _assert_known_reservation_states(connection, context="pre-intent recovery")
            rows = connection.execute(
                "SELECT * FROM samples WHERE campaign_id=? AND status='planned' ORDER BY sample_id",
                (identifier,),
            ).fetchall()
            for row in rows:
                sample_id = str(row["sample_id"])
                if row["job_id"] is not None:
                    continue
                intent = connection.execute(
                    "SELECT intent_id FROM submission_intents WHERE campaign_id=? AND sample_id=?",
                    (identifier, sample_id),
                ).fetchone()
                if intent is not None:
                    continue
                reservations = connection.execute(
                    "SELECT * FROM reservations WHERE campaign_id=? AND sample_id=? "
                    "ORDER BY created_at_utc, reservation_id",
                    (identifier, sample_id),
                ).fetchall()
                open_compute = [
                    reservation
                    for reservation in reservations
                    if str(reservation["kind"]) == "compute" and str(reservation["state"]) == "open"
                ]
                if not open_compute:
                    continue
                if (
                    len(reservations) != 1
                    or len(open_compute) != 1
                    or open_compute[0]["submitted_at_utc"] is not None
                ):
                    raise CampaignGateError(
                        f"pre-intent recovery is blocked for sample {sample_id}: "
                        "reservation evidence contradicts a never-submitted attempt"
                    )
                reservation = open_compute[0]
                result = connection.execute(
                    "UPDATE reservations SET state='settled', final_estimate_micro_usd=0, "
                    "unavailable_reason=NULL, settled_at_utc=? WHERE campaign_id=? "
                    "AND reservation_id=? AND state='open'",
                    (_iso(timestamp), identifier, reservation["reservation_id"]),
                )
                if result.rowcount != 1:
                    raise CampaignGateError(
                        f"pre-intent recovery could not settle reservation for sample {sample_id}"
                    )
                connection.execute(
                    "UPDATE samples SET status='unsubmitted', cost_status='not_submitted', "
                    "cost_reason=NULL, updated_at_utc=? WHERE campaign_id=? AND sample_id=?",
                    (_iso(timestamp), identifier, sample_id),
                )
                self._event(
                    connection,
                    identifier,
                    "pre_intent_reconciled",
                    {
                        "sample_id": sample_id,
                        "reservation_id": str(reservation["reservation_id"]),
                        "reserved_micro_usd": int(reservation["reserved_micro_usd"]),
                    },
                    now=timestamp,
                )
                reconciled.append(sample_id)
        self._chmod_private()
        return reconciled

    def mark_intent_submitted(
        self, campaign_id: str, sample_id: str, product_job_uuid: str
    ) -> None:
        """Mark one intent ready only after both durable records agree."""
        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        product_identifier = _bounded_id(product_job_uuid, "product_job_uuid")
        timestamp = utc_now()
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE submission_intents SET status='submitted', updated_at_utc=? "
                "WHERE campaign_id=? AND sample_id=? AND product_job_uuid=? AND status!='submitted'",
                (_iso(timestamp), identifier, sample_identifier, product_identifier),
            )
            if result.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM submission_intents WHERE campaign_id=? AND sample_id=? "
                    "AND product_job_uuid=?",
                    (identifier, sample_identifier, product_identifier),
                ).fetchone()
                if row is None or row["status"] != "submitted":
                    raise CampaignGateError("submission intent is not pending")

    def require_ordinary_submissions(self, campaign_id: str) -> None:
        """Refuse ordinary mutations while a window, gate, or intent is open."""
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            gate = connection.execute(
                "SELECT active FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            if gate is not None and int(gate["active"]) == 1:
                raise CampaignGateError("campaign recovery is required before ordinary submissions")
            pending = connection.execute(
                "SELECT COUNT(*) FROM submission_intents WHERE campaign_id=? "
                "AND status!='submitted'",
                (identifier,),
            ).fetchone()
            if pending is not None and int(pending[0]) > 0:
                raise CampaignGateError("campaign recovery is required before ordinary submissions")

    def campaign_status(self, campaign_id: str) -> dict[str, Any]:
        """Bounded read-only campaign/window/gate/reservation/sample state.

        Never returns URLs, prompts, lyrics, capabilities, listener mappings,
        credentials, or raw provider bodies.
        """

        identifier = _bounded_id(campaign_id, "campaign_id")
        campaign = self.get_campaign(identifier)
        if campaign is None:
            raise CampaignValidationError("unknown campaign")
        with self.read() as connection:
            _assert_known_reservation_states(connection, context="campaign status")
            gate = connection.execute(
                "SELECT active, campaign_id, window_id FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            window = connection.execute(
                "SELECT window_id, stage, start_utc, state, health_evidence_json "
                "FROM execution_windows WHERE campaign_id=? AND state='open' "
                "ORDER BY window_id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            reservation_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM reservations WHERE campaign_id=? "
                "GROUP BY state",
                (identifier,),
            ).fetchall()
            sample_rows = connection.execute(
                "SELECT sample_id, declared_case_id, stage, role, status, job_id, cost_status "
                "FROM samples WHERE campaign_id=? ORDER BY sample_id",
                (identifier,),
            ).fetchall()
            intent_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM submission_intents "
                "WHERE campaign_id=? GROUP BY status",
                (identifier,),
            ).fetchall()
        samples = [
            {
                "sample_id": str(row["sample_id"]),
                "declared_case_id": str(row["declared_case_id"]),
                "stage": str(row["stage"]),
                "role": str(row["role"]),
                "status": str(row["status"]),
                "product_job_id": row["job_id"],
                "cost_status": row["cost_status"],
            }
            for row in sample_rows
        ]
        intents = self.list_submission_intents(identifier)
        linked_job_ids = {str(intent["product_job_uuid"]) for intent in intents}
        linkage_complete = all(
            item["product_job_id"] is None or item["product_job_id"] in linked_job_ids
            for item in samples
        )
        window_value: dict[str, Any] | None = None
        if window is not None:
            window_value = {
                "window_id": int(window["window_id"]),
                "stage": str(window["stage"]),
                "start_utc": str(window["start_utc"]),
                "state": str(window["state"]),
                "health_evidence_captured_at_utc": (
                    str(
                        (
                            _load_json(str(window["health_evidence_json"]), "health evidence") or {}
                        ).get("captured_at_utc", "")
                    )
                    if window["health_evidence_json"] is not None
                    else None
                ),
            }
        return {
            "campaign_id": identifier,
            "status": str(campaign["status"]),
            "gate": {
                "active": bool(gate is not None and int(gate["active"]) == 1),
                "campaign_id": gate["campaign_id"] if gate is not None else None,
                "window_id": gate["window_id"] if gate is not None else None,
            },
            "window": window_value,
            "reservations": {str(row["state"]): int(row["count"]) for row in reservation_rows},
            "samples": samples,
            "submission_intents": {str(row["status"]): int(row["count"]) for row in intent_rows},
            "product_linkage_complete": linkage_complete,
            "zero_worker_evidence": (window is None or window["health_evidence_json"] is not None),
        }

    def mark_sample_running(
        self, campaign_id: str, sample_id: str, *, now: datetime | None = None
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        sample_identifier = _bounded_id(sample_id, "sample_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE samples SET status='running', updated_at_utc=? WHERE campaign_id=? AND sample_id=? "
                "AND status='submitted'",
                (_iso(timestamp), identifier, sample_identifier),
            )
            if result.rowcount != 1:
                raise CampaignGateError("sample is not in submitted state")

    def reserve_storage_artifact(
        self,
        campaign_id: str,
        artifact_id: str,
        path: str,
        bytes_count: int,
        *,
        reservation_id: str,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        artifact_identifier = _bounded_id(artifact_id, "artifact_id")
        bounded_path = _bounded_text(path, "artifact_path", max_length=1024)
        if (
            isinstance(bytes_count, bool)
            or not isinstance(bytes_count, int)
            or not 0 < bytes_count <= MAX_STORAGE_BYTES
        ):
            raise CampaignValidationError("artifact bytes must be positive")
        reservation_identifier = _bounded_id(reservation_id, "reservation_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            reservation = connection.execute(
                "SELECT state, kind FROM reservations WHERE campaign_id=? AND reservation_id=?",
                (identifier, reservation_identifier),
            ).fetchone()
            if (
                reservation is None
                or reservation["kind"] != "storage"
                or reservation["state"] != "open"
            ):
                raise CampaignGateError("storage reservation is not open")
            connection.execute(
                "INSERT INTO storage_artifacts(artifact_id, campaign_id, path, bytes_count, reservation_id, state, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, 'reserved', ?)",
                (
                    artifact_identifier,
                    identifier,
                    bounded_path,
                    bytes_count,
                    reservation_identifier,
                    _iso(timestamp),
                ),
            )

    def mark_storage_removed(
        self,
        campaign_id: str,
        artifact_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        artifact_identifier = _bounded_id(artifact_id, "artifact_id")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state, reservation_id FROM storage_artifacts WHERE campaign_id=? AND artifact_id=?",
                (identifier, artifact_identifier),
            ).fetchone()
            if row is None:
                raise CampaignValidationError("unknown storage artifact")
            if row["state"] == "removed":
                return
            if row["state"] != "reserved":
                raise CampaignGateError("storage artifact is not reserved")
            connection.execute(
                "UPDATE storage_artifacts SET state='removed', removed_at_utc=? WHERE campaign_id=? AND artifact_id=?",
                (_iso(timestamp), identifier, artifact_identifier),
            )
            connection.execute(
                "UPDATE reservations SET state='settled', final_estimate_micro_usd=0, settled_at_utc=? "
                "WHERE campaign_id=? AND reservation_id=? AND state='open'",
                (_iso(timestamp), identifier, row["reservation_id"]),
            )

    def record_boundary_evidence(
        self,
        campaign_id: str,
        evidence: BoundaryEvidence,
        *,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        payload = {
            "start_inclusive": evidence.start_inclusive,
            "end_exclusive": evidence.end_exclusive,
            "native_bucket_seconds": evidence.native_bucket_seconds,
            "native_bucket_start_field": evidence.native_bucket_start_field,
            "empty_response_behavior": evidence.empty_response_behavior,
            "current_partial_bucket_behavior": evidence.current_partial_bucket_behavior,
            "late_update_behavior": evidence.late_update_behavior,
            "source": evidence.source,
            "proven": evidence.proven,
        }
        with self.transaction() as connection:
            self._event(connection, identifier, "billing_boundary_evidence", payload, now=timestamp)
            if not evidence.proven:
                self._event(
                    connection,
                    identifier,
                    "billing_totals_unavailable",
                    {"reason": "ambiguous_provider_interval_semantics"},
                    now=timestamp,
                )

    def record_unrelated_endpoint_work(self, campaign_id: str, window_id: int, reason: str) -> None:
        self.mark_window_contaminated(campaign_id, window_id, reason)

    def record_provider_observations(
        self,
        campaign_id: str,
        observations: Sequence[BillingObservation],
        *,
        now: datetime | None = None,
    ) -> int:
        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        inserted = 0
        with self.transaction() as connection:
            for observation in observations:
                payload = {
                    "provider": observation.provider,
                    "resource_type": observation.resource_type,
                    "grouping_dimension": observation.grouping_dimension,
                    "grouping_value": observation.grouping_value or "",
                    "bucket_start_utc": _iso(observation.bucket_start_utc),
                    "bucket_size_seconds": observation.bucket_size_seconds,
                    "currency": observation.currency,
                    "raw_amount": observation.raw_amount,
                    "amount_micro_usd": observation.amount_micro_usd,
                    "raw_time_billed": observation.raw_time_billed,
                    "fetched_at_utc": _iso(observation.fetched_at),
                    "source_contract": observation.source_contract,
                    "allocatable": observation.allocatable,
                    "unavailable_reason": observation.unavailable_reason,
                }
                observation_hash = hashlib.sha256(
                    _bounded_json(payload, "billing observation").encode()
                ).hexdigest()
                result = connection.execute(
                    "INSERT OR IGNORE INTO provider_billing_observations(campaign_id, provider, resource_type, "
                    "grouping_dimension, grouping_value, bucket_start_utc, bucket_size_seconds, currency, "
                    "raw_amount, amount_micro_usd, raw_time_billed, fetched_at_utc, source_contract, "
                    "allocatable, unavailable_reason, observation_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identifier,
                        observation.provider,
                        observation.resource_type,
                        observation.grouping_dimension,
                        observation.grouping_value or "",
                        _iso(observation.bucket_start_utc),
                        observation.bucket_size_seconds,
                        observation.currency,
                        observation.raw_amount,
                        observation.amount_micro_usd,
                        observation.raw_time_billed,
                        _iso(observation.fetched_at),
                        observation.source_contract,
                        int(observation.allocatable),
                        observation.unavailable_reason,
                        observation_hash,
                    ),
                )
                inserted += int(result.rowcount == 1)
            if observations:
                self._event(
                    connection,
                    identifier,
                    "provider_billing_observations_recorded",
                    {"count": len(observations), "inserted": inserted},
                    now=timestamp,
                )
        return inserted

    def provider_total_micro_usd(self, campaign_id: str) -> int:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            return self._provider_total(connection, identifier)

    def open_execution_window(
        self,
        campaign_id: str,
        stage: str,
        *,
        blocked_routes: Sequence[str],
        edge_config_sha256: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Open one durable non-overlapping window and activate its gate."""

        identifier = _bounded_id(campaign_id, "campaign_id")
        _bounded_text(stage, "stage")
        routes = [_bounded_text(route, "blocked_route", max_length=256) for route in blocked_routes]
        if not routes:
            raise CampaignGateError("a maintenance window must enumerate blocked enqueue routes")
        if edge_config_sha256 is not None and not _SHA256_RE.fullmatch(edge_config_sha256):
            raise CampaignValidationError("edge configuration hash is malformed")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT window_id, campaign_id FROM execution_windows WHERE state='open'"
            ).fetchone()
            if active is not None:
                raise CampaignGateError("another campaign execution window is open")
            prior_windows = connection.execute(
                "SELECT end_utc FROM execution_windows WHERE end_utc IS NOT NULL"
            ).fetchall()
            if any(
                timestamp.astimezone(UTC) < _parse_utc(str(row["end_utc"])) for row in prior_windows
            ):
                raise CampaignGateError("execution windows must not overlap")
            gate = connection.execute(
                "SELECT active FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            if gate is None or int(gate["active"]) == 1:
                raise CampaignGateError("maintenance gate is already active")
            campaign = connection.execute(
                "SELECT status FROM campaigns WHERE campaign_id=?", (identifier,)
            ).fetchone()
            if campaign is None:
                raise CampaignValidationError("unknown campaign")
            if str(campaign["status"]) in {"failed", "stopped", "complete"}:
                raise CampaignGateError("campaign cannot open a window in its terminal state")
            start = _iso(timestamp)
            result = connection.execute(
                "INSERT INTO execution_windows(campaign_id, stage, start_utc, state, blocked_routes_json, "
                "edge_config_sha256) VALUES (?, ?, ?, 'open', ?, ?)",
                (
                    identifier,
                    stage,
                    start,
                    _bounded_json(routes, "blocked routes"),
                    edge_config_sha256,
                ),
            )
            if result.lastrowid is None:
                raise CampaignSchemaError("execution window insert did not return an ID")
            window_id = int(result.lastrowid)
            connection.execute(
                "UPDATE maintenance_gate SET active=1, campaign_id=?, window_id=?, bypass_campaign_id=?, "
                "edge_guard_enabled=0, edge_guard_verified=0, rollback_target=NULL, "
                "updated_at_utc=?, blocked_routes_json=?, edge_config_sha256=? WHERE singleton=1",
                (
                    identifier,
                    window_id,
                    identifier,
                    start,
                    _bounded_json(routes, "blocked routes"),
                    edge_config_sha256,
                ),
            )
            connection.execute(
                "UPDATE campaigns SET status='running', updated_at_utc=? WHERE campaign_id=?",
                (start, identifier),
            )
            self._event(
                connection,
                identifier,
                "execution_window_opened",
                {"window_id": window_id, "stage": stage, "blocked_routes": routes},
                now=timestamp,
            )
        self._chmod_private()
        return window_id

    def current_gate(self) -> dict[str, Any]:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM maintenance_gate WHERE singleton=1").fetchone()
        return self._row_dict(row) or {}

    def submission_allowed(self, *, campaign_id: str | None = None) -> bool:
        """Return whether a normal enqueue mutation may proceed."""

        gate = self.current_gate()
        if not gate or int(gate.get("active", 0)) == 0:
            return True
        return campaign_id is not None and campaign_id == gate.get("bypass_campaign_id")

    def require_submission_allowed(self, *, campaign_id: str | None = None) -> None:
        if not self.submission_allowed(campaign_id=campaign_id):
            raise CampaignGateError("ordinary submissions are paused for the evaluation window")

    def record_edge_guard(
        self,
        campaign_id: str,
        *,
        enabled: bool,
        verified: bool,
        blocked_routes: Sequence[str],
        config_sha256: str,
        rollback_target: str,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        if not _SHA256_RE.fullmatch(config_sha256):
            raise CampaignValidationError("edge configuration hash is malformed")
        routes = [_bounded_text(route, "blocked_route", max_length=256) for route in blocked_routes]
        if not routes or not rollback_target:
            raise CampaignGateError("edge rollback guard requires routes and a rollback target")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            gate = connection.execute(
                "SELECT active, campaign_id FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            if gate is None or int(gate["active"]) != 1 or str(gate["campaign_id"]) != identifier:
                raise CampaignGateError(
                    "edge guard must be recorded for the active campaign window"
                )
            connection.execute(
                "UPDATE maintenance_gate SET edge_guard_enabled=?, edge_guard_verified=?, "
                "edge_config_sha256=?, blocked_routes_json=?, rollback_target=?, updated_at_utc=? "
                "WHERE singleton=1",
                (
                    int(enabled),
                    int(verified),
                    config_sha256,
                    _bounded_json(routes, "blocked routes"),
                    _bounded_text(rollback_target, "rollback_target"),
                    _iso(timestamp),
                ),
            )
            self._event(
                connection,
                identifier,
                "edge_guard_recorded",
                {"enabled": enabled, "verified": verified, "route_count": len(routes)},
                now=timestamp,
            )

    def close_execution_window(
        self,
        campaign_id: str,
        window_id: int,
        *,
        health_evidence: Mapping[str, Any],
        unresolved_sample_ids: Sequence[str] = (),
        contaminated: bool = False,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        if isinstance(window_id, bool) or not isinstance(window_id, int) or window_id <= 0:
            raise CampaignValidationError("window ID is malformed")
        unresolved = [_bounded_id(sample_id, "sample_id") for sample_id in unresolved_sample_ids]
        if unresolved:
            raise CampaignGateError("window teardown cannot discard uncertain samples")
        timestamp = now or utc_now()
        with self.transaction() as connection:
            _assert_known_reservation_states(connection, context="window teardown")
            row = connection.execute(
                "SELECT state FROM execution_windows WHERE campaign_id=? AND window_id=?",
                (identifier, window_id),
            ).fetchone()
            if row is None or row["state"] != "open":
                raise CampaignGateError("execution window is not open")
            gate = connection.execute(
                "SELECT active, campaign_id, window_id FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            if (
                gate is None
                or int(gate["active"]) != 1
                or str(gate["campaign_id"]) != identifier
                or gate["window_id"] is None
                or int(gate["window_id"]) != window_id
            ):
                raise CampaignGateError("maintenance gate does not match the execution window")
            validated_evidence = self._validated_health_evidence(
                connection, identifier, health_evidence
            )
            active_samples = connection.execute(
                "SELECT sample_id FROM samples WHERE campaign_id=? "
                "AND status IN ('submitted', 'running', 'uncertain') ORDER BY sample_id",
                (identifier,),
            ).fetchall()
            open_reservations = connection.execute(
                "SELECT reservation_id FROM reservations WHERE campaign_id=? "
                "AND state IN ('open', 'unresolved') ORDER BY reservation_id",
                (identifier,),
            ).fetchall()
            if active_samples or open_reservations:
                raise CampaignGateError(
                    "window teardown requires terminal samples and settled reservations"
                )
            connection.execute(
                "UPDATE execution_windows SET end_utc=?, state=?, contaminated=?, reason=?, "
                "health_evidence_json=? WHERE campaign_id=? AND window_id=?",
                (
                    _iso(timestamp),
                    "contaminated" if contaminated else "closed",
                    int(contaminated),
                    _bounded_text(reason, "reason", max_length=1024),
                    _bounded_json(validated_evidence, "health evidence"),
                    identifier,
                    window_id,
                ),
            )
            connection.execute(
                "UPDATE maintenance_gate SET active=0, campaign_id=NULL, window_id=NULL, bypass_campaign_id=NULL, "
                "updated_at_utc=? WHERE singleton=1",
                (_iso(timestamp),),
            )
            self._event(
                connection,
                identifier,
                "execution_window_closed",
                {"window_id": window_id, "contaminated": contaminated, "reason": reason},
                now=timestamp,
            )
        self._chmod_private()

    @staticmethod
    def _validated_health_evidence(
        connection: sqlite3.Connection,
        campaign_id: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate immutable timestamped provider-health evidence for the window.

        Zero-at-rest is provider-observed only: the evidence must name the
        authorized endpoint, carry a parseable UTC capture time, and report
        exactly zero active workers and zero queued/in-progress jobs.  Any
        missing, malformed, inconsistent, or nonzero evidence fails closed.
        """

        if not isinstance(evidence, Mapping):
            raise CampaignGateError("window teardown requires validated provider-health evidence")
        endpoint_id = evidence.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id.strip() or len(endpoint_id) > 256:
            raise CampaignGateError("window teardown requires validated provider-health evidence")
        captured_at = evidence.get("captured_at_utc")
        if not isinstance(captured_at, str):
            raise CampaignGateError("window teardown requires validated provider-health evidence")
        try:
            _parse_utc(captured_at)
        except ValueError as exc:
            raise CampaignGateError(
                "window teardown requires validated provider-health evidence"
            ) from exc
        counts: dict[str, int] = {}
        for field in (
            "active_workers",
            "idle_workers",
            "running_workers",
            "queued_jobs",
            "in_progress_jobs",
        ):
            value = evidence.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CampaignGateError(
                    "window teardown requires validated provider-health evidence"
                )
            counts[field] = value
        if counts["active_workers"] != counts["idle_workers"] + counts["running_workers"]:
            raise CampaignGateError("window teardown requires validated provider-health evidence")
        if (
            counts["active_workers"] != 0
            or counts["queued_jobs"] != 0
            or counts["in_progress_jobs"] != 0
        ):
            raise CampaignGateError(
                "window teardown requires provider-observed zero workers and "
                "zero queued/in-progress work"
            )
        authorization = connection.execute(
            "SELECT authorization_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if authorization is not None and authorization["authorization_json"] is not None:
            frozen = _load_json(str(authorization["authorization_json"]), "authorization")
            if isinstance(frozen, Mapping) and frozen.get("endpoint_id") != endpoint_id:
                raise CampaignGateError(
                    "window teardown evidence is for a different authorized endpoint"
                )
        return {
            "endpoint_id": endpoint_id,
            "captured_at_utc": captured_at,
            "active_workers": counts["active_workers"],
            "idle_workers": counts["idle_workers"],
            "running_workers": counts["running_workers"],
            "queued_jobs": counts["queued_jobs"],
            "in_progress_jobs": counts["in_progress_jobs"],
            "source": "runpod_health",
        }

    def mark_window_contaminated(self, campaign_id: str, window_id: int, reason: str) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE execution_windows SET contaminated=1, state='contaminated', reason=? "
                "WHERE campaign_id=? AND window_id=? AND state='open'",
                (_bounded_text(reason, "reason", max_length=1024), identifier, window_id),
            )
            if result.rowcount != 1:
                raise CampaignGateError("open execution window was not found")

    def acquire_lease(
        self,
        lease_name: str,
        owner_id: str,
        *,
        campaign_id: str | None = None,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> None:
        _bounded_id(lease_name, "lease_name")
        _bounded_id(owner_id, "owner_id")
        if campaign_id is not None:
            _bounded_id(campaign_id, "campaign_id")
        if not 1 <= ttl_seconds <= 86_400:
            raise CampaignValidationError("lease TTL is out of bounds")
        timestamp = now or utc_now()
        expiry = timestamp + timedelta(seconds=ttl_seconds)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, expires_at_utc FROM leases WHERE lease_name=?", (lease_name,)
            ).fetchone()
            if (
                row is not None
                and _parse_utc(str(row["expires_at_utc"])) > timestamp
                and row["owner_id"] != owner_id
            ):
                raise CampaignGateError("another process owns the campaign lease")
            connection.execute(
                "INSERT INTO leases(lease_name, campaign_id, owner_id, acquired_at_utc, expires_at_utc) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(lease_name) DO UPDATE SET campaign_id=excluded.campaign_id, "
                "owner_id=excluded.owner_id, acquired_at_utc=excluded.acquired_at_utc, expires_at_utc=excluded.expires_at_utc",
                (lease_name, campaign_id, owner_id, _iso(timestamp), _iso(expiry)),
            )

    def release_lease(self, lease_name: str, owner_id: str) -> None:
        _bounded_id(lease_name, "lease_name")
        _bounded_id(owner_id, "owner_id")
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM leases WHERE lease_name=? AND owner_id=?", (lease_name, owner_id)
            )

    def recover_after_restart(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Expire leases only; retain active gates/reservations for safe operator teardown."""

        timestamp = now or utc_now()
        with self.transaction() as connection:
            _assert_known_reservation_states(connection, context="restart recovery")
            rows = connection.execute("SELECT lease_name, expires_at_utc FROM leases").fetchall()
            expired = tuple(
                str(row["lease_name"])
                for row in rows
                if _parse_utc(str(row["expires_at_utc"])) <= timestamp
            )
            for lease_name in expired:
                connection.execute("DELETE FROM leases WHERE lease_name=?", (lease_name,))
            gate = connection.execute(
                "SELECT active, campaign_id FROM maintenance_gate WHERE singleton=1"
            ).fetchone()
            if gate is not None and int(gate["active"]) == 1:
                campaign_id = gate["campaign_id"]
                if campaign_id:
                    self._event(
                        connection,
                        str(campaign_id),
                        "restart_recovery_requires_teardown",
                        {"active_gate": True},
                        now=timestamp,
                    )
        return expired

    def rollback_readiness(self, *, require_edge_guard: bool = True) -> RollbackReadiness:
        diagnostics: list[RollbackDiagnostic] = []
        try:
            with self.read() as connection:
                _assert_known_reservation_states(connection, context="rollback readiness")
                gate = connection.execute(
                    "SELECT * FROM maintenance_gate WHERE singleton=1"
                ).fetchone()
                if gate is not None and int(gate["active"]) == 1:
                    diagnostics.append(
                        RollbackDiagnostic(
                            "active_maintenance_gate", "campaign window is still open", True
                        )
                    )
                if require_edge_guard and gate is not None:
                    active_campaign = connection.execute(
                        "SELECT COUNT(*) FROM campaigns WHERE status IN ('running', 'awaiting_scores', 'screening_complete', 'awaiting_confirmation')"
                    ).fetchone()[0]
                    if int(active_campaign) and not (
                        int(gate["edge_guard_enabled"]) == 1
                        and int(gate["edge_guard_verified"]) == 1
                    ):
                        diagnostics.append(
                            RollbackDiagnostic(
                                "missing_verified_edge_guard",
                                "a controller without the durable gate cannot be activated",
                                True,
                            )
                        )
                uncertain = connection.execute(
                    "SELECT sample_id FROM samples WHERE status IN ('submitted', 'running', 'uncertain') "
                    "ORDER BY sample_id LIMIT 16"
                ).fetchall()
                if uncertain:
                    diagnostics.append(
                        RollbackDiagnostic(
                            "evaluation_work_in_flight",
                            "opaque samples remain submitted, running, or uncertain",
                            True,
                        )
                    )
                unresolved = connection.execute(
                    "SELECT reservation_id FROM reservations WHERE state IN ('open', 'unresolved') LIMIT 16"
                ).fetchall()
                if unresolved:
                    diagnostics.append(
                        RollbackDiagnostic(
                            "open_campaign_reservations",
                            "campaign reservations still require reconciliation",
                            True,
                        )
                    )
        except (sqlite3.Error, CampaignError) as exc:
            diagnostics.append(
                RollbackDiagnostic("campaign_store_indeterminate", type(exc).__name__, True)
            )
        if not diagnostics:
            diagnostics.append(
                RollbackDiagnostic("campaign_idle", "no active campaign state", False)
            )
        return RollbackReadiness(tuple(diagnostics))

    def _listener_ids(self, connection: sqlite3.Connection, campaign_id: str) -> tuple[str, ...]:
        row = connection.execute(
            "SELECT listener_ids_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise CampaignValidationError("unknown campaign")
        values = _load_json(str(row["listener_ids_json"]), "listener IDs")
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(isinstance(item, str) for item in values)
        ):
            raise CampaignSchemaError("stored listener IDs are malformed")
        return tuple(values)

    @staticmethod
    def _rubric_payload() -> dict[str, Any]:
        return {
            "rubric_id": "quality-rubric-v1",
            "direction": "1 worst, 5 best",
            "artifacts_direction": "higher means cleaner",
            "dimensions": list(_DIMENSIONS),
            "anchors": {
                "melody_retention": [
                    "source melody absent or unrecognizable",
                    "isolated fragments only; main contour substantially lost",
                    "main melody recognizable with noticeable omissions or changes",
                    "melody clear and mostly complete with minor changes",
                    "melody immediately recognizable, coherent, and retained throughout",
                ],
                "prompt_style_adherence": [
                    "requested style not perceptible",
                    "few requested traits; dominated by another style",
                    "central style present but inconsistent or generic",
                    "most requested traits clear and sustained",
                    "strong, consistent realization of the style and brief",
                ],
                "development": [
                    "static, unfinished, or disconnected",
                    "little progression; accidental repetition",
                    "usable arc with limited contrast or weak transitions",
                    "clear contrast and mostly purposeful transitions",
                    "compelling complete arc with purposeful development",
                ],
                "vocal_lyric_adherence": [
                    "vocals/lyrics absent, unintelligible, or unrelated",
                    "few words or gestures recognizable; most unusable",
                    "broad lyric/vocal content understandable with material errors",
                    "lyrics and vocal character clear with minor errors",
                    "lyrics consistently intelligible and delivery fits the brief",
                ],
                "artifacts": [
                    "severe clipping, warbling, noise, broken audio, or artifacts dominate",
                    "frequent severe artifacts make sections unusable",
                    "noticeable artifacts but broadly usable",
                    "minor occasional artifacts do not distract",
                    "clean, stable audio with no distracting artifacts",
                ],
                "ending_quality": [
                    "cuts off, loops, or clearly fails",
                    "abrupt or incomplete; requires repair",
                    "acceptable but weak, generic, or slightly truncated",
                    "intentional ending with a minor weakness",
                    "resolved, intentional, and matched to the piece",
                ],
            },
        }

    @staticmethod
    def _rubric_hash() -> str:
        return hashlib.sha256(
            _bounded_json(CampaignStore._rubric_payload(), "rubric").encode()
        ).hexdigest()

    @staticmethod
    def _score_stage_samples_query(stage: str) -> tuple[str, ...]:
        if stage == "screening":
            return ("cover-screen", "original-screen")
        if stage == "confirmation":
            return ("cover-confirmation", "original-confirmation")
        raise CampaignValidationError("score sheet stage is unsupported")

    @staticmethod
    def _bounded_stage(stage: str) -> str:
        if stage not in {"screening", "confirmation"}:
            raise CampaignValidationError("score sheet stage is unsupported")
        return stage

    @staticmethod
    def _require_complete_output_evidence(
        connection: sqlite3.Connection, campaign_id: str, stage: str
    ) -> None:
        """Reject export/finalization while any expected blinded sample is unready.

        Every incumbent/candidate/corrected-controls sample of the stage must
        have reached the complete generated-output state: terminal with a
        recorded output path.  Planned, in-flight, uncertain, failed, or
        output-less samples make the campaign partial and are rejected
        deterministically.
        """

        stage_names = CampaignStore._score_stage_samples_query(stage)
        placeholders = ", ".join("?" for _ in stage_names)
        incomplete = connection.execute(
            f"SELECT sample_id, status, output_path FROM samples WHERE campaign_id=? "
            f"AND role IN ('incumbent', 'candidate', 'corrected-controls') "
            f"AND stage IN ({placeholders}) AND (status != 'completed' OR output_path IS NULL) "
            "ORDER BY sample_id",
            (campaign_id, *stage_names),
        ).fetchall()
        if incomplete:
            first = incomplete[0]
            raise CampaignGateError(
                f"score sheet stage requires complete output evidence; "
                f"sample {first['sample_id']} is {first['status']}"
            )

    @staticmethod
    def _current_scoreable_set(
        connection: sqlite3.Connection, campaign_id: str, stage: str
    ) -> tuple[set[str], list[frozenset[str]]]:
        """Return the canonical scoreable sample IDs and matched-pair memberships.

        This is exactly the sample/pair population ``export_score_sheet``
        derives from current campaign state: every incumbent, candidate, and
        corrected-controls sample of the stage, plus one opaque incumbent-vs-
        candidate membership per declared candidate.  Confirmation seed-one
        aliases reuse their screening rows and therefore never appear here.
        """

        stage_names = CampaignStore._score_stage_samples_query(stage)
        placeholders = ", ".join("?" for _ in stage_names)
        rows = connection.execute(
            f"SELECT sample_id, pair_key, role FROM samples WHERE campaign_id=? "
            f"AND role IN ('incumbent', 'candidate', 'corrected-controls') "
            f"AND stage IN ({placeholders}) ORDER BY sample_id",
            (campaign_id, *stage_names),
        ).fetchall()
        sample_ids = {str(row["sample_id"]) for row in rows}
        by_pair: dict[str, dict[str, list[str]]] = {}
        for row in rows:
            group = by_pair.setdefault(str(row["pair_key"]), {"incumbent": [], "candidate": []})
            group.setdefault(str(row["role"]), []).append(str(row["sample_id"]))
        pairs: list[frozenset[str]] = []
        for _pair_key, group in sorted(by_pair.items()):
            incumbents = group.get("incumbent", [])
            for candidate_id in group.get("candidate", []):
                if not incumbents:
                    continue
                pairs.append(frozenset({incumbents[0], candidate_id}))
        return sample_ids, pairs

    @staticmethod
    def _require_frozen_coverage(
        connection: sqlite3.Connection,
        campaign_id: str,
        stage: str,
        frozen_export: Mapping[str, Any],
    ) -> None:
        """Reject a frozen sheet that no longer covers the current scoreable set.

        A newly declared scoreable sample, a missing sample, a stale pair, or
        a duplicate alias after export makes the frozen sheet partial; import
        and finalization must both refuse it deterministically.
        """

        current_ids, expected_pairs = CampaignStore._current_scoreable_set(
            connection, campaign_id, stage
        )
        order = frozen_export.get("sample_order")
        frozen_pairs = frozen_export.get("pairs")
        if not isinstance(order, list) or not isinstance(frozen_pairs, list):
            raise CampaignSchemaError("frozen score sheet is malformed")
        frozen_ids = {str(item) for item in order}
        if frozen_ids != current_ids:
            missing = sorted(current_ids - frozen_ids)
            extra = sorted(frozen_ids - current_ids)
            detail = ""
            if missing:
                detail = f"; sample {missing[0]} is missing from the sheet"
            elif extra:
                detail = f"; sample {extra[0]} is not scoreable"
            raise CampaignGateError(
                "score sheet no longer covers the current scoreable sample set" + detail
            )
        memberships: set[frozenset[str]] = set()
        for pair in frozen_pairs:
            if not isinstance(pair, Mapping):
                raise CampaignSchemaError("frozen score sheet pair is malformed")
            left = pair.get("left")
            right = pair.get("right")
            if not isinstance(left, str) or not isinstance(right, str):
                raise CampaignSchemaError("frozen score sheet pair is malformed")
            memberships.add(frozenset({left, right}))
        if memberships != set(expected_pairs):
            raise CampaignGateError(
                "score sheet pair structure no longer matches the current sample set"
            )

    def export_score_sheet(
        self,
        campaign_id: str,
        listener_id: str,
        *,
        stage: str = "screening",
        now: datetime | None = None,
        random_source: Any | None = None,
    ) -> dict[str, Any]:
        """Export only opaque sample IDs, rubric, randomized order, and pair choices."""

        identifier = _bounded_id(campaign_id, "campaign_id")
        listener = _bounded_id(listener_id, "listener_id")
        sheet_stage = self._bounded_stage(stage)
        timestamp = now or utc_now()
        randomizer = random_source or secrets.SystemRandom()
        with self.transaction() as connection:
            listeners = self._listener_ids(connection, identifier)
            if listener not in listeners:
                raise CampaignGateError("listener is not predeclared for this campaign")
            prior = connection.execute(
                "SELECT state FROM score_sheets WHERE campaign_id=? AND listener_id=? AND stage=?",
                (identifier, listener, sheet_stage),
            ).fetchone()
            if prior is not None:
                raise CampaignGateError("listener score sheet already exists")
            self._require_complete_output_evidence(connection, identifier, sheet_stage)
            samples = connection.execute(
                "SELECT sample_id, task_type, pair_key, role, status FROM samples WHERE campaign_id=? "
                "AND role IN ('incumbent', 'candidate', 'corrected-controls') "
                "AND stage IN (?, ?) ORDER BY sample_id",
                (identifier, *self._score_stage_samples_query(sheet_stage)),
            ).fetchall()
            if not samples:
                raise CampaignGateError("cannot export a score sheet before samples are declared")
            opaque_ids = [str(row["sample_id"]) for row in samples]
            randomizer.shuffle(opaque_ids)
            pairs: list[dict[str, str]] = []
            by_pair: dict[str, dict[str, list[str]]] = {}
            for row in samples:
                group = by_pair.setdefault(str(row["pair_key"]), {"incumbent": [], "candidate": []})
                group.setdefault(str(row["role"]), []).append(str(row["sample_id"]))
            for _pair_key, group in sorted(by_pair.items()):
                incumbents = group.get("incumbent", [])
                for candidate_id in group.get("candidate", []):
                    if not incumbents:
                        continue
                    shuffled = [incumbents[0], candidate_id]
                    randomizer.shuffle(shuffled)
                    pair_id = secrets.token_urlsafe(12).replace("-", "_")[:48]
                    pairs.append(
                        {
                            "pair_id": pair_id,
                            "left": shuffled[0],
                            "right": shuffled[1],
                        }
                    )
            export = {
                "sheet_schema": "quality-score-sheet-v1",
                "campaign_id": identifier,
                "sheet_version": SCORE_SHEET_VERSION,
                "stage": sheet_stage,
                "listener_id": listener,
                "rubric": self._rubric_payload(),
                "sample_order": opaque_ids,
                "pairs": pairs,
                "scores": [],
                "preferences": [],
            }
            export_json = _bounded_json(export, "score sheet")
            connection.execute(
                "INSERT INTO score_sheets(campaign_id, listener_id, stage, sheet_version, rubric_sha256, "
                "export_json, state, exported_at_utc) VALUES (?, ?, ?, ?, ?, ?, 'exported', ?)",
                (
                    identifier,
                    listener,
                    sheet_stage,
                    SCORE_SHEET_VERSION,
                    self._rubric_hash(),
                    export_json,
                    _iso(timestamp),
                ),
            )
        self._chmod_private()
        return export

    @staticmethod
    def _validate_score_value(value: Any, *, allow_not_applicable: bool) -> int | str:
        if allow_not_applicable and value == "not_applicable":
            return "not_applicable"
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise CampaignValidationError("listener score must be an integer from 1 through 5")
        return int(value)

    @staticmethod
    def _sanitize_comment(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 500:
            raise CampaignValidationError("listener comments must be at most 500 characters")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
            raise CampaignValidationError("listener comments contain a control character")
        # Comments are intentionally retained as sound observations only.  Do
        # not accept common contact/identity forms into the private store.
        if "@" in value or "https://" in value.lower() or "http://" in value.lower():
            raise CampaignValidationError("listener comments must not contain personal links")
        normalized = " ".join(value.split())
        return normalized or None

    def import_score_sheet(
        self,
        campaign_id: str,
        listener_id: str,
        payload: Mapping[str, Any],
        *,
        stage: str = "screening",
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        listener = _bounded_id(listener_id, "listener_id")
        sheet_stage = self._bounded_stage(stage)
        timestamp = now or utc_now()
        if not isinstance(payload, Mapping):
            raise CampaignValidationError("score sheet must be an object")
        if (
            payload.get("campaign_id") != identifier
            or payload.get("sheet_version") != SCORE_SHEET_VERSION
            or payload.get("stage") != sheet_stage
        ):
            raise CampaignValidationError("score sheet campaign, version, or stage does not match")
        if (
            payload.get("listener_id") != listener
            or payload.get("sheet_schema") != "quality-score-sheet-v1"
        ):
            raise CampaignValidationError("score sheet listener/schema does not match")
        if payload.get("rubric") != self._rubric_payload():
            raise CampaignValidationError("score sheet rubric was altered")
        scores = payload.get("scores")
        order = payload.get("sample_order")
        pairs = payload.get("pairs")
        preferences = payload.get("preferences", [])
        if (
            not isinstance(scores, list)
            or not isinstance(order, list)
            or not isinstance(pairs, list)
            or not isinstance(preferences, list)
        ):
            raise CampaignValidationError("score sheet score/order/pair fields are malformed")
        if len(order) != len(set(order)) or not all(isinstance(item, str) for item in order):
            raise CampaignValidationError("score sheet sample order contains duplicates")
        with self.transaction() as connection:
            stored = connection.execute(
                "SELECT export_json, state FROM score_sheets WHERE campaign_id=? AND listener_id=? AND stage=?",
                (identifier, listener, sheet_stage),
            ).fetchone()
            if stored is None:
                raise CampaignGateError("export the operator score sheet before importing it")
            if stored["state"] == "finalized":
                raise CampaignGateError("finalized score sheets are immutable")
            exported = _load_json(str(stored["export_json"]), "exported score sheet")
            if (
                not isinstance(exported, dict)
                or exported.get("sample_order") != order
                or exported.get("pairs") != pairs
            ):
                raise CampaignValidationError(
                    "score sheet order or pair structure does not match export"
                )
            # The campaign may have evolved since export: a new planned or
            # later-completed scoreable sample (or a stale pair) makes the
            # frozen sheet partial, so import refuses it deterministically.
            self._require_frozen_coverage(connection, identifier, sheet_stage, exported)
            expected_ids = set(str(item) for item in order)
            seen_ids: set[str] = set()
            normalized_scores: list[dict[str, Any]] = []
            for score in scores:
                if not isinstance(score, Mapping):
                    raise CampaignValidationError("score entry must be an object")
                sample_id = score.get("opaque_sample_id")
                if (
                    not isinstance(sample_id, str)
                    or sample_id not in expected_ids
                    or sample_id in seen_ids
                ):
                    raise CampaignValidationError(
                        "score sheet contains an unknown or duplicate sample"
                    )
                seen_ids.add(sample_id)
                dimensions = score.get("dimensions")
                if not isinstance(dimensions, Mapping):
                    raise CampaignValidationError("score dimensions are missing")
                normalized_dimensions: dict[str, int | str] = {}
                for dimension in _DIMENSIONS:
                    if dimension not in dimensions:
                        raise CampaignValidationError("score sheet is missing a rubric dimension")
                    normalized_dimensions[dimension] = self._validate_score_value(
                        dimensions[dimension],
                        allow_not_applicable=dimension == "vocal_lyric_adherence",
                    )
                normalized_scores.append(
                    {
                        "opaque_sample_id": sample_id,
                        "dimensions": normalized_dimensions,
                        "comment": self._sanitize_comment(score.get("comment")),
                    }
                )
            complete = seen_ids == expected_ids and len(scores) == len(expected_ids)
            normalized_preferences: list[dict[str, str]] = []
            pair_ids = {
                str(pair["pair_id"])
                for pair in pairs
                if isinstance(pair, Mapping) and isinstance(pair.get("pair_id"), str)
            }
            if preferences:
                seen_pair_ids: set[str] = set()
                for preference in preferences:
                    if not isinstance(preference, Mapping):
                        raise CampaignValidationError("pair preference must be an object")
                    pair_id = preference.get("pair_id")
                    choice = preference.get("choice")
                    if (
                        not isinstance(pair_id, str)
                        or pair_id in seen_pair_ids
                        or pair_id not in pair_ids
                        or choice not in {"left", "right", "tie"}
                    ):
                        raise CampaignValidationError(
                            "pair preference is unknown, duplicate, or malformed"
                        )
                    seen_pair_ids.add(pair_id)
                    normalized_preferences.append({"pair_id": pair_id, "choice": choice})
                if len(normalized_preferences) != len(pairs):
                    complete = False
            elif pairs:
                complete = False
            state = "complete_pending_finalization" if complete else "partial"
            imported = {
                "sheet_schema": "quality-score-sheet-v1",
                "campaign_id": identifier,
                "sheet_version": SCORE_SHEET_VERSION,
                "stage": sheet_stage,
                "listener_id": listener,
                "rubric": self._rubric_payload(),
                "sample_order": order,
                "pairs": pairs,
                "scores": normalized_scores,
                "preferences": normalized_preferences,
            }
            connection.execute(
                "UPDATE score_sheets SET imported_json=?, state=? WHERE campaign_id=? AND listener_id=? AND stage=?",
                (
                    _bounded_json(imported, "imported score sheet"),
                    state,
                    identifier,
                    listener,
                    sheet_stage,
                ),
            )
            self._event(
                connection,
                identifier,
                "score_sheet_imported",
                {"listener_id": listener, "stage": sheet_stage, "complete": complete},
                now=timestamp,
            )

    def finalize_score_sheet(
        self,
        campaign_id: str,
        listener_id: str,
        *,
        stage: str = "screening",
        now: datetime | None = None,
    ) -> None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        listener = _bounded_id(listener_id, "listener_id")
        sheet_stage = self._bounded_stage(stage)
        timestamp = now or utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state, imported_json, export_json FROM score_sheets WHERE campaign_id=? AND listener_id=? AND stage=?",
                (identifier, listener, sheet_stage),
            ).fetchone()
            if row is None:
                raise CampaignGateError("score sheet was not exported")
            if row["state"] == "finalized":
                return
            if row["state"] != "complete_pending_finalization" or row["imported_json"] is None:
                raise CampaignGateError("partial score sheet cannot be finalized")
            # The campaign may have evolved since export; re-require the
            # complete generated-output evidence before freezing the sheet,
            # and refuse a frozen sheet whose scoreable sample/pair coverage
            # no longer matches the current campaign state (for example a
            # late sample that completed after export).
            self._require_complete_output_evidence(connection, identifier, sheet_stage)
            exported = _load_json(str(row["export_json"]), "exported score sheet")
            if not isinstance(exported, dict):
                raise CampaignSchemaError("stored exported score sheet is malformed")
            self._require_frozen_coverage(connection, identifier, sheet_stage, exported)
            connection.execute(
                "UPDATE score_sheets SET state='finalized', finalized_at_utc=? WHERE campaign_id=? AND listener_id=? AND stage=?",
                (_iso(timestamp), identifier, listener, sheet_stage),
            )
            final_count = connection.execute(
                "SELECT COUNT(*) FROM score_sheets WHERE campaign_id=? AND stage=? AND state='finalized'",
                (identifier, sheet_stage),
            ).fetchone()[0]
            if int(final_count) == 2 and sheet_stage == "screening":
                connection.execute(
                    "UPDATE campaigns SET status='awaiting_confirmation', updated_at_utc=? WHERE campaign_id=? "
                    "AND status NOT IN ('complete', 'failed', 'stopped')",
                    (_iso(timestamp), identifier),
                )
            self._event(
                connection,
                identifier,
                "score_sheet_finalized",
                {
                    "listener_id": listener,
                    "stage": sheet_stage,
                    "both_finalized": int(final_count) == 2,
                },
                now=timestamp,
            )

    def finalized_scores(self, campaign_id: str, *, stage: str = "screening") -> dict[str, Any]:
        identifier = _bounded_id(campaign_id, "campaign_id")
        sheet_stage = self._bounded_stage(stage)
        with self.read() as connection:
            rows = connection.execute(
                "SELECT listener_id, imported_json, state FROM score_sheets "
                "WHERE campaign_id=? AND stage=? ORDER BY listener_id",
                (identifier, sheet_stage),
            ).fetchall()
        if len(rows) != 2 or any(row["state"] != "finalized" for row in rows):
            raise CampaignGateError("both listener score sheets are not finalized")
        result: dict[str, Any] = {}
        for row in rows:
            value = _load_json(str(row["imported_json"]), "imported score sheet")
            result[str(row["listener_id"])] = value
        return result

    def advance_screening_to_confirmation(
        self,
        campaign_id: str,
        manifest: FixtureManifest,
        plan: CampaignPlan,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Deterministically rank both finalized screening sheets and advance.

        Requires both finalized screening sheets, derives per-task-type
        rankings through ``rank_screening_candidates`` with the frozen
        completeness/severe-artifact/tie/top-two rules, persists the exact
        advancement result as a durable event, and only then materializes the
        confirmation alias/payable cases through ``build_confirmation_cases``.
        Retrying with the same frozen inputs is idempotent; a different
        finalist set is a conflict and fails closed.
        """

        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        campaign = self.get_campaign(identifier)
        if campaign is None:
            raise CampaignValidationError("unknown campaign")
        if campaign["manifest_sha256"] != manifest.manifest_sha256:
            raise CampaignValidationError("campaign manifest fingerprint does not match")
        stored_plan = self.campaign_plan(identifier)
        stored_ids = sorted(
            str(item.get("declared_case_id", "")) for item in stored_plan.get("cases", [])
        )
        plan_ids = sorted(case.declared_case_id for case in plan.cases)
        if stored_ids != plan_ids:
            raise CampaignGateError("campaign plan no longer matches the frozen campaign record")
        screening = self.finalized_scores(identifier, stage="screening")
        listener_ids = sorted(screening)
        with self.read() as connection:
            rows = connection.execute(
                "SELECT sample_id, declared_case_id, fixture_case_id, task_type, role FROM samples "
                "WHERE campaign_id=? AND role IN ('incumbent', 'candidate') "
                "AND stage IN ('cover-screen', 'original-screen') ORDER BY sample_id",
                (identifier,),
            ).fetchall()
        finalists: dict[str, list[str]] = {}
        rankings_by_task: dict[str, list[dict[str, Any]]] = {}
        for task_type in sorted({str(row["task_type"]) for row in rows}):
            task_rows = [row for row in rows if str(row["task_type"]) == task_type]
            incumbent_rows = [row for row in task_rows if str(row["role"]) == "incumbent"]
            candidate_rows = [row for row in task_rows if str(row["role"]) == "candidate"]
            if not incumbent_rows or not candidate_rows:
                continue
            fixture_case_id = str(incumbent_rows[0]["fixture_case_id"] or "")
            fixture_case = manifest.case(fixture_case_id)
            incumbent_scores = [
                self._sheet_entry(
                    screening[listener],
                    str(incumbent_rows[0]["sample_id"]),
                    f"{task_type} incumbent screening",
                )
                for listener in listener_ids
            ]
            candidates: dict[str, Sequence[Mapping[str, Any]]] = {}
            for row in candidate_rows:
                declared = str(row["declared_case_id"])
                candidates[declared] = [
                    self._sheet_entry(
                        screening[listener],
                        str(row["sample_id"]),
                        f"{declared} screening",
                    )
                    for listener in listener_ids
                ]
            rankings = rank_screening_candidates(
                task_type=task_type,
                incumbent_scores=incumbent_scores,
                candidates=candidates,
                vocals_applicable=fixture_case.lyrics is not None,
            )
            rankings_by_task[task_type] = [
                {
                    "candidate_id": ranking.candidate_id,
                    "mean_primary_score": ranking.mean_primary_score,
                    "severe_artifacts": ranking.severe_artifacts,
                    "eligible": ranking.eligible,
                }
                for ranking in rankings
            ]
            finalists[task_type] = [ranking.candidate_id for ranking in rankings]
        event_payload: dict[str, Any] = {
            "advancement": "screening_to_confirmation",
            "finalists": finalists,
            "rankings": rankings_by_task,
        }
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT event_json FROM campaign_events WHERE campaign_id=? AND event_type='screening_advanced' "
                "ORDER BY event_id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            if prior is not None:
                frozen = _load_json(str(prior["event_json"]), "advancement event")
                if (
                    not isinstance(frozen, dict)
                    or frozen.get("finalists") != finalists
                    or frozen.get("rankings") != rankings_by_task
                ):
                    raise CampaignGateError(
                        "screening advancement conflicts with the frozen advancement record"
                    )
                repeated = True
            else:
                self._event(
                    connection,
                    identifier,
                    "screening_advanced",
                    event_payload,
                    now=timestamp,
                )
                repeated = False
        materialized: dict[str, str] = {}
        for task_type in sorted(finalists):
            task_finalists = finalists[task_type]
            if not task_finalists:
                continue
            cases = build_confirmation_cases(manifest, plan, tuple(task_finalists))
            for case in cases:
                sample_id, _created = self.add_sample(
                    identifier, case, fixture_id=manifest.fixture_id
                )
                materialized[case.declared_case_id] = sample_id
        self.set_campaign_status(
            identifier,
            "awaiting_confirmation",
            reason="screening advancement finalized",
            now=timestamp,
        )
        return {
            "campaign_id": identifier,
            "finalists": finalists,
            "materialized_samples": materialized,
            "recorded": not repeated,
        }

    @staticmethod
    def _sheet_entry(sheet: Mapping[str, Any], sample_id: str, label: str) -> Mapping[str, Any]:
        scores = sheet.get("scores")
        if not isinstance(scores, list):
            raise CampaignSchemaError("finalized score sheet is malformed")
        for entry in scores:
            if not isinstance(entry, Mapping) or entry.get("opaque_sample_id") != sample_id:
                continue
            return entry
        raise CampaignGateError(f"{label} sample is missing from the finalized score sheet")

    @staticmethod
    def _sheet_pair_choice(
        sheet: Mapping[str, Any],
        *,
        candidate_id: str,
        incumbent_id: str,
        candidate_label: str,
    ) -> str:
        """Translate one opaque A/B preference into candidate/incumbent/tie."""

        pairs = sheet.get("pairs")
        preferences = sheet.get("preferences")
        if not isinstance(pairs, list) or not isinstance(preferences, list):
            raise CampaignSchemaError("finalized score sheet is malformed")
        matched: tuple[str, str, str] | None = None
        for pair in pairs:
            if not isinstance(pair, Mapping):
                continue
            left = pair.get("left")
            right = pair.get("right")
            pair_id = pair.get("pair_id")
            if (
                not isinstance(left, str)
                or not isinstance(right, str)
                or not isinstance(pair_id, str)
            ):
                continue
            if {left, right} == {candidate_id, incumbent_id}:
                matched = (pair_id, left, right)
                break
        if matched is None:
            raise CampaignGateError(f"unblinding requires the matched pair for {candidate_label}")
        pair_id, left, right = matched
        for preference in preferences:
            if not isinstance(preference, Mapping) or preference.get("pair_id") != pair_id:
                continue
            choice = preference.get("choice")
            if choice == "tie":
                return "tie"
            if choice in {"left", "right"}:
                chosen_is_candidate = (choice == "left") == (left == candidate_id)
                return "candidate" if chosen_is_candidate else "incumbent"
        raise CampaignGateError(
            f"unblinding requires a preference for the matched pair of {candidate_label}"
        )

    def record_quality_decision(
        self,
        campaign_id: str,
        manifest: FixtureManifest,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Unblind the complete matched pairs and persist one immutable decision.

        Both listener sheets must be finalized for the screening and the
        confirmation stage, every expected blinded sample must carry complete
        generated-output evidence, and the screening seed must be confirmation
        seed one (its screening scores are the seed-one confirmation scores).
        The deterministic promotion gate from the frozen plan is applied per
        finalist; the resulting record commits to the exact fixture, listeners,
        seeds, profiles, models, and sample fingerprints.  Repeating the same
        finalization is idempotent; any conflicting decision fails closed.
        """

        identifier = _bounded_id(campaign_id, "campaign_id")
        timestamp = now or utc_now()
        screening = self.finalized_scores(identifier, stage="screening")
        confirmation = self.finalized_scores(identifier, stage="confirmation")
        campaign = self.get_campaign(identifier)
        if campaign is None:
            raise CampaignValidationError("unknown campaign")
        listener_ids = _load_json(str(campaign["listener_ids_json"]), "listener IDs")
        if (
            not isinstance(listener_ids, list)
            or len(listener_ids) != 2
            or not all(isinstance(item, str) for item in listener_ids)
        ):
            raise CampaignSchemaError("stored listener IDs are malformed")
        with self.read() as connection:
            self._require_complete_output_evidence(connection, identifier, "screening")
            self._require_complete_output_evidence(connection, identifier, "confirmation")
            rows = connection.execute(
                "SELECT sample_id, declared_case_id, fixture_case_id, task_type, stage, role, "
                "pair_key, seed, profile_id, model, lm_model, fingerprint FROM samples "
                "WHERE campaign_id=? AND role IN ('incumbent', 'candidate', 'corrected-controls') "
                "ORDER BY sample_id",
                (identifier,),
            ).fetchall()
        if not rows:
            raise CampaignGateError("quality decision requires declared samples")
        samples: dict[str, dict[str, Any]] = {}
        screening_rows: list[dict[str, Any]] = []
        confirmation_rows: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_dict(row) or {}
            samples[str(item["sample_id"])] = item
            stage_value = str(item["stage"])
            if stage_value in {"cover-screen", "original-screen"}:
                screening_rows.append(item)
            elif stage_value in {"cover-confirmation", "original-confirmation"}:
                confirmation_rows.append(item)
        if not confirmation_rows:
            raise CampaignGateError("quality decision requires finalized confirmation pairs")
        task_types = {str(item["task_type"]) for item in confirmation_rows}
        decisions: list[dict[str, Any]] = []
        task_seeds: dict[str, list[int]] = {}
        provenance_samples: dict[str, dict[str, Any]] = {}
        for task_type in sorted(task_types):
            task_screening = [item for item in screening_rows if item["task_type"] == task_type]
            if not task_screening:
                raise CampaignGateError(f"{task_type} confirmation requires its screening samples")
            task_confirmation = [
                item for item in confirmation_rows if item["task_type"] == task_type
            ]
            fixture_case_id = str(task_screening[0]["fixture_case_id"] or "")
            fixture_case = manifest.case(fixture_case_id)
            if fixture_case.screening_seed not in fixture_case.confirmation_seeds:
                raise CampaignGateError("screening seed is not confirmation seed one")
            seeds = sorted(set(fixture_case.confirmation_seeds))
            task_seeds[task_type] = seeds
            incumbent_confirmation = [
                item for item in task_confirmation if item["role"] == "incumbent"
            ]
            if not incumbent_confirmation:
                raise CampaignGateError(f"{task_type} confirmation is missing its incumbent")
            incumbent_base = _confirmation_base_id(
                str(incumbent_confirmation[0]["declared_case_id"])
            )
            incumbent_screening = [
                item
                for item in task_screening
                if item["declared_case_id"] == incumbent_base and item["role"] == "incumbent"
            ]
            if len(incumbent_screening) != 1:
                raise CampaignGateError(
                    f"{task_type} confirmation requires the incumbent screening sample"
                )
            incumbent_screening_sample = incumbent_screening[0]
            candidate_confirmation = [
                item for item in task_confirmation if item["role"] == "candidate"
            ]
            candidate_bases: list[str] = []
            for item in candidate_confirmation:
                base = _confirmation_base_id(str(item["declared_case_id"]))
                if base not in candidate_bases:
                    candidate_bases.append(base)
            if not candidate_bases:
                raise CampaignGateError(f"{task_type} confirmation has no finalist candidates")
            for base in sorted(candidate_bases):
                decision, involved = self._unblind_candidate_decision(
                    identifier=identifier,
                    task_type=task_type,
                    candidate_base=base,
                    incumbent_base=incumbent_base,
                    screening_sheet=screening,
                    confirmation_sheet=confirmation,
                    screening_rows=task_screening,
                    confirmation_rows=task_confirmation,
                    seeds=seeds,
                    incumbent_screening_sample=incumbent_screening_sample,
                    fixture_case=fixture_case,
                )
                decisions.append(decision)
                provenance_samples.update(involved)
        for sample in samples.values():
            provenance_samples.setdefault(
                str(sample["sample_id"]),
                {
                    key: sample[key]
                    for key in (
                        "declared_case_id",
                        "stage",
                        "role",
                        "seed",
                        "profile_id",
                        "model",
                        "lm_model",
                        "fingerprint",
                    )
                },
            )
        rubric_sha256 = self._rubric_hash()
        payload: dict[str, Any] = {
            "decision_schema": "quality-decision-v1",
            "campaign_id": identifier,
            "fixture_id": campaign["fixture_id"],
            "manifest_sha256": campaign["manifest_sha256"],
            "listener_ids": listener_ids,
            "seeds": task_seeds,
            "task_decisions": decisions,
            "provenance": {
                "rubric_sha256": rubric_sha256,
                "screening_sheet_hashes": {
                    str(listener): hashlib.sha256(
                        _bounded_json(sheet, "finalized score sheet").encode()
                    ).hexdigest()
                    for listener, sheet in sorted(screening.items())
                },
                "confirmation_sheet_hashes": {
                    str(listener): hashlib.sha256(
                        _bounded_json(sheet, "finalized score sheet").encode()
                    ).hexdigest()
                    for listener, sheet in sorted(confirmation.items())
                },
                "samples": provenance_samples,
            },
        }
        encoded = _bounded_json(payload, "quality decision")
        decision_id = hashlib.sha256(encoded.encode()).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT decision_id FROM quality_decisions WHERE campaign_id=?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                if str(existing["decision_id"]) == decision_id:
                    stored = connection.execute(
                        "SELECT decision_json, created_at_utc FROM quality_decisions "
                        "WHERE campaign_id=? AND decision_id=?",
                        (identifier, decision_id),
                    ).fetchone()
                    if stored is None:
                        raise CampaignSchemaError("stored quality decision is malformed")
                    return {
                        "decision_id": decision_id,
                        "campaign_id": identifier,
                        "decision": _load_json(str(stored["decision_json"]), "quality decision"),
                        "created_at_utc": str(stored["created_at_utc"]),
                        "recorded": False,
                    }
                raise CampaignGateError(
                    "a conflicting quality decision is already recorded for this campaign"
                )
            connection.execute(
                "INSERT INTO quality_decisions(decision_id, campaign_id, decision_json, created_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (decision_id, identifier, encoded, _iso(timestamp)),
            )
            self._event(
                connection,
                identifier,
                "quality_decision_recorded",
                {"decision_id": decision_id, "task_decisions": decisions},
                now=timestamp,
            )
        self._chmod_private()
        return {
            "decision_id": decision_id,
            "campaign_id": identifier,
            "decision": payload,
            "created_at_utc": _iso(timestamp),
            "recorded": True,
        }

    def get_quality_decision(self, campaign_id: str) -> dict[str, Any] | None:
        identifier = _bounded_id(campaign_id, "campaign_id")
        with self.read() as connection:
            row = connection.execute(
                "SELECT decision_id, decision_json, created_at_utc FROM quality_decisions "
                "WHERE campaign_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": str(row["decision_id"]),
            "campaign_id": identifier,
            "decision": _load_json(str(row["decision_json"]), "quality decision"),
            "created_at_utc": str(row["created_at_utc"]),
        }

    def _unblind_candidate_decision(
        self,
        *,
        identifier: str,
        task_type: str,
        candidate_base: str,
        incumbent_base: str,
        screening_sheet: Mapping[str, Any],
        confirmation_sheet: Mapping[str, Any],
        screening_rows: Sequence[Mapping[str, Any]],
        confirmation_rows: Sequence[Mapping[str, Any]],
        seeds: Sequence[int],
        incumbent_screening_sample: Mapping[str, Any],
        fixture_case: FixtureCase,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Unblind one finalist's complete three-seed matched pairs and gate it."""

        screening_seed = fixture_case.screening_seed
        candidate_screening = [
            item
            for item in screening_rows
            if item["declared_case_id"] == candidate_base and item["role"] == "candidate"
        ]
        if len(candidate_screening) != 1:
            raise CampaignGateError(f"{candidate_base} confirmation requires the screening sample")
        candidate_screening_sample = candidate_screening[0]
        listener_ids = sorted(screening_sheet)
        candidate_by_seed: dict[int, Mapping[str, Any]] = {}
        incumbent_by_seed: dict[int, Mapping[str, Any]] = {}
        listener_candidate_by_seed: list[dict[int, Mapping[str, Any]]] = []
        listener_incumbent_by_seed: list[dict[int, Mapping[str, Any]]] = []
        listener_preferences: list[list[str]] = []
        involved: dict[str, dict[str, Any]] = {}
        for _listener in listener_ids:
            candidate_scores: dict[int, Mapping[str, Any]] = {}
            incumbent_scores: dict[int, Mapping[str, Any]] = {}
            preferences: list[str] = []
            for seed in seeds:
                if seed == screening_seed:
                    candidate_sample = candidate_screening_sample
                    incumbent_sample = incumbent_screening_sample
                    sheet = screening_sheet[_listener]
                    candidate_label = f"{candidate_base} screening"
                else:
                    candidate_rows = [
                        item
                        for item in confirmation_rows
                        if item["declared_case_id"] == f"{candidate_base}-confirmation-{seed}"
                    ]
                    incumbent_rows = [
                        item
                        for item in confirmation_rows
                        if item["declared_case_id"] == f"{incumbent_base}-confirmation-{seed}"
                    ]
                    if len(candidate_rows) != 1 or len(incumbent_rows) != 1:
                        raise CampaignGateError(
                            f"{candidate_base} confirmation is missing seed {seed}"
                        )
                    candidate_sample = candidate_rows[0]
                    incumbent_sample = incumbent_rows[0]
                    sheet = confirmation_sheet[_listener]
                    candidate_label = f"{candidate_base} confirmation seed {seed}"
                candidate_sample_id = str(candidate_sample["sample_id"])
                incumbent_sample_id = str(incumbent_sample["sample_id"])
                candidate_score = self._sheet_entry(sheet, candidate_sample_id, candidate_label)
                incumbent_score = self._sheet_entry(
                    sheet, incumbent_sample_id, _incumbent_label(task_type)
                )
                candidate_scores[seed] = candidate_score
                incumbent_scores[seed] = incumbent_score
                preferences.append(
                    self._sheet_pair_choice(
                        sheet,
                        candidate_id=candidate_sample_id,
                        incumbent_id=incumbent_sample_id,
                        candidate_label=candidate_label,
                    )
                )
                for sample in (candidate_sample, incumbent_sample):
                    involved.setdefault(
                        str(sample["sample_id"]),
                        {
                            key: sample[key]
                            for key in (
                                "declared_case_id",
                                "stage",
                                "role",
                                "seed",
                                "profile_id",
                                "model",
                                "lm_model",
                                "fingerprint",
                            )
                        },
                    )
            listener_candidate_by_seed.append(candidate_scores)
            listener_incumbent_by_seed.append(incumbent_scores)
            listener_preferences.append(preferences)
        candidate_by_seed = listener_candidate_by_seed[0]
        incumbent_by_seed = listener_incumbent_by_seed[0]
        for seed in candidate_by_seed:
            if seed not in incumbent_by_seed:
                raise CampaignGateError("unblinded matched pairs are incomplete")
        vocals_applicable = fixture_case.lyrics is not None
        result = evaluate_confirmation_gate(
            candidate_id=candidate_base,
            task_type=task_type,
            candidate_by_seed=candidate_by_seed,
            incumbent_by_seed=incumbent_by_seed,
            listener_candidate_by_seed=listener_candidate_by_seed,
            listener_incumbent_by_seed=listener_incumbent_by_seed,
            listener_preferences=listener_preferences,
            vocals_applicable=vocals_applicable,
        )
        decision = {
            "task_type": task_type,
            "candidate_id": candidate_base,
            "profile_id": str(candidate_screening_sample["profile_id"]),
            "model": str(candidate_screening_sample["model"]),
            "lm_model": candidate_screening_sample["lm_model"],
            "passed": result.passed,
            "reason": result.reason,
            "mean_primary_improvement": result.mean_primary_improvement,
            "listener_dimension_regressions": dict(result.listener_dimension_regressions),
            "severe_artifact_delta": result.severe_artifact_delta,
            "listener_preferences": (
                list(result.listener_preferences)
                if result.listener_preferences is not None
                else None
            ),
        }
        return decision, involved

    def backup(self, destination: Path) -> Path:
        """Create a consistent SQLite-API backup, never a raw WAL copy.

        A missing or empty campaign database is refused before any SQLite
        connection opens it, so a typo cannot create an empty database and
        then present it as a valid backup target.
        """

        target = destination.expanduser().resolve()
        if target == self.path:
            raise CampaignValidationError("campaign backup destination must differ from source")
        if not self.path.is_file() or self.path.stat().st_size == 0 or self._created:
            raise CampaignSchemaError(
                "campaign database does not exist; refusing to back up an empty database"
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            raise CampaignValidationError("campaign backup destination already exists")
        source = sqlite3.connect(self.path)
        destination_connection = sqlite3.connect(target)
        try:
            source.backup(destination_connection)
            destination_connection.commit()
        except sqlite3.Error as exc:
            raise CampaignError("campaign SQLite backup failed") from exc
        finally:
            destination_connection.close()
            source.close()
        target.chmod(0o600)
        return target

    def cleanup_media(
        self,
        campaign_id: str,
        media_root: Path,
        *,
        now: datetime | None = None,
        retention_decision: str | None = None,
    ) -> tuple[str, ...]:
        """Delete only campaign-created evaluation copies after the safe gate."""

        identifier = _bounded_id(campaign_id, "campaign_id")
        root = media_root.expanduser().resolve()
        timestamp = now or utc_now()
        campaign = self.get_campaign(identifier)
        if campaign is None:
            raise CampaignValidationError("unknown campaign")
        finalized = False
        with self.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM score_sheets WHERE campaign_id=? AND state='finalized'",
                (identifier,),
            ).fetchone()[0]
            finalized = int(count) == 2
            active_samples = connection.execute(
                "SELECT COUNT(*) FROM samples WHERE campaign_id=? "
                "AND status IN ('submitted', 'running', 'uncertain')",
                (identifier,),
            ).fetchone()[0]
        if int(active_samples) > 0:
            raise CampaignGateError("evaluation media cannot be removed while work is uncertain")
        deadline = timestamp >= _parse_utc(str(campaign["retention_deadline_utc"]))
        if not finalized and not deadline:
            return ()
        if not finalized:
            if retention_decision == "retain_public_or_user_owned":
                with self.transaction() as connection:
                    self._event(
                        connection,
                        identifier,
                        "evaluation_media_retained_by_operator_decision",
                        {},
                        now=timestamp,
                    )
                return ()
            if retention_decision != "delete":
                raise CampaignGateError(
                    "media retention deadline requires an explicit delete/retain decision"
                )
        candidate = (root / identifier).resolve()
        if not candidate.is_relative_to(root) or candidate == root:
            raise CampaignGateError("campaign media path escapes the configured root")
        if not candidate.exists():
            return ()
        removed: list[str] = []
        for item in candidate.rglob("*"):
            if item.is_file():
                removed.append(item.name[:128])
        shutil.rmtree(candidate)
        with self.transaction() as connection:
            self._event(
                connection,
                identifier,
                "evaluation_media_removed",
                {"file_count": len(removed)},
                now=timestamp,
            )
        return tuple(removed)


def primary_dimensions(task_type: str, *, vocals_applicable: bool) -> tuple[str, ...]:
    if task_type == "cover":
        base = list(_PRIMARY_COVER_DIMENSIONS)
    elif task_type == "original":
        base = list(_PRIMARY_ORIGINAL_DIMENSIONS)
    else:
        raise CampaignValidationError("task_type is unsupported")
    if not vocals_applicable and "vocal_lyric_adherence" in base:
        base.remove("vocal_lyric_adherence")
    return tuple(base)


def _score_value(scores: Mapping[str, Any], dimension: str) -> float:
    value = scores.get(dimension)
    if value == "not_applicable":
        raise CampaignValidationError("not_applicable cannot be used in a primary dimension")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise CampaignValidationError("score is outside the anchored 1-5 range")
    return float(value)


def _score_dimensions(score: Mapping[str, Any]) -> Mapping[str, Any]:
    value = score.get("dimensions", score)
    if not isinstance(value, Mapping):
        raise CampaignValidationError("score dimensions are malformed")
    return value


def primary_score(
    task_type: str,
    dimensions: Mapping[str, Any],
    *,
    vocals_applicable: bool,
) -> float:
    names = primary_dimensions(task_type, vocals_applicable=vocals_applicable)
    if any(name not in dimensions for name in names):
        raise CampaignValidationError("primary score has an incomplete dimension set")
    return sum(_score_value(dimensions, name) for name in names) / len(names)


@dataclass(frozen=True, slots=True)
class ScreeningRanking:
    candidate_id: str
    mean_primary_score: float
    severe_artifacts: int
    eligible: bool


def _score_equivalence_groups(
    rankings: Sequence[ScreeningRanking],
) -> list[list[ScreeningRanking]]:
    """Split score-descending rankings into equal-mean-score groups."""
    groups: list[list[ScreeningRanking]] = []
    for item in rankings:
        if groups and groups[-1][0].mean_primary_score == item.mean_primary_score:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def rank_screening_candidates(
    *,
    task_type: str,
    incumbent_scores: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    vocals_applicable: bool,
    maximum_finalists: int = 2,
) -> tuple[ScreeningRanking, ...]:
    """Rank only complete screening score sets and apply the severe-artifact rule."""

    if len(incumbent_scores) != 2:
        return ()
    incumbent_complete = all(
        all(name in _score_dimensions(score) for name in _DIMENSIONS) for score in incumbent_scores
    )
    if not incumbent_complete:
        return ()
    incumbent_artifacts = sum(
        _score_value(_score_dimensions(score), "artifacts") <= 2 for score in incumbent_scores
    )
    rankings: list[ScreeningRanking] = []
    for candidate_id, score_sets in candidates.items():
        if len(score_sets) != 2 or any(
            any(name not in _score_dimensions(score) for name in _DIMENSIONS)
            for score in score_sets
        ):
            continue
        severe = sum(
            _score_value(_score_dimensions(score), "artifacts") <= 2 for score in score_sets
        )
        rankings.append(
            ScreeningRanking(
                candidate_id=candidate_id,
                mean_primary_score=sum(
                    primary_score(
                        task_type,
                        _score_dimensions(score),
                        vocals_applicable=vocals_applicable,
                    )
                    for score in score_sets
                )
                / 2,
                severe_artifacts=severe,
                eligible=severe <= incumbent_artifacts,
            )
        )
    eligible = [item for item in rankings if item.eligible]
    # Deterministic candidate-ID ordering applies only after advancement
    # eligibility is decided; it never breaks a score-equivalence group.
    eligible.sort(key=lambda item: (-item.mean_primary_score, item.candidate_id))
    # Frozen cutoff-tie rule: if a score-equivalence group crosses the
    # maximum_finalists boundary, the entire tied group is excluded.  Thus an
    # exact two-way tie for first advances both, a three-way tie for first
    # advances none, and a tie spanning positions two and three advances only
    # an untied first-place candidate.
    selected: list[ScreeningRanking] = []
    for group in _score_equivalence_groups(eligible):
        if len(selected) + len(group) <= maximum_finalists:
            selected.extend(group)
        else:
            break
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class ConfirmationDecision:
    candidate_id: str
    passed: bool
    reason: str
    mean_primary_improvement: float | None
    listener_dimension_regressions: Mapping[str, float]
    severe_artifact_delta: int | None
    listener_preferences: tuple[int, int] | None


def evaluate_confirmation_gate(
    *,
    candidate_id: str,
    task_type: str,
    candidate_by_seed: Mapping[int, Mapping[str, Any]],
    incumbent_by_seed: Mapping[int, Mapping[str, Any]],
    listener_candidate_by_seed: Sequence[Mapping[int, Mapping[str, Any]]],
    listener_incumbent_by_seed: Sequence[Mapping[int, Mapping[str, Any]]],
    listener_preferences: Sequence[Sequence[str]],
    vocals_applicable: bool,
) -> ConfirmationDecision:
    """Apply the complete matched three-seed, two-listener promotion gate."""

    if (
        len(listener_candidate_by_seed) != 2
        or len(listener_incumbent_by_seed) != 2
        or len(listener_preferences) != 2
    ):
        return ConfirmationDecision(
            candidate_id, False, "both_listener_sets_required", None, {}, None, None
        )
    if (
        len(candidate_by_seed) != 3
        or len(incumbent_by_seed) != 3
        or set(candidate_by_seed) != set(incumbent_by_seed)
    ):
        return ConfirmationDecision(
            candidate_id,
            False,
            "three_complete_confirmation_seeds_required",
            None,
            {},
            None,
            None,
        )
    expected_seeds = set(candidate_by_seed)
    all_listener_sets = list(listener_candidate_by_seed) + list(listener_incumbent_by_seed)
    if any(set(values) != expected_seeds for values in all_listener_sets):
        return ConfirmationDecision(
            candidate_id, False, "three_complete_confirmation_seeds_required", None, {}, None, None
        )
    dimensions = primary_dimensions(task_type, vocals_applicable=vocals_applicable)
    listener_regressions: dict[str, float] = {}
    candidate_primary: list[float] = []
    incumbent_primary: list[float] = []
    severe_candidate = 0
    severe_incumbent = 0
    for listener_index in range(2):
        candidate_scores = listener_candidate_by_seed[listener_index]
        incumbent_scores = listener_incumbent_by_seed[listener_index]
        listener_deltas: dict[str, list[float]] = {dimension: [] for dimension in dimensions}
        for seed in sorted(candidate_scores):
            if seed not in incumbent_scores:
                return ConfirmationDecision(
                    candidate_id, False, "unmatched_seed_pair", None, {}, None, None
                )
            candidate = candidate_scores[seed]
            incumbent = incumbent_scores[seed]
            candidate_dimensions = _score_dimensions(candidate)
            incumbent_dimensions = _score_dimensions(incumbent)
            if set(candidate_dimensions) != set(incumbent_dimensions):
                return ConfirmationDecision(
                    candidate_id, False, "dimension_set_mismatch", None, {}, None, None
                )
            candidate_primary.append(
                primary_score(task_type, candidate_dimensions, vocals_applicable=vocals_applicable)
            )
            incumbent_primary.append(
                primary_score(task_type, incumbent_dimensions, vocals_applicable=vocals_applicable)
            )
            severe_candidate += _score_value(candidate_dimensions, "artifacts") <= 2
            severe_incumbent += _score_value(incumbent_dimensions, "artifacts") <= 2
            for dimension in dimensions:
                delta = _score_value(candidate_dimensions, dimension) - _score_value(
                    incumbent_dimensions, dimension
                )
                listener_deltas[dimension].append(delta)
        for dimension, deltas in listener_deltas.items():
            mean_delta = sum(deltas) / len(deltas)
            listener_regressions[dimension] = max(
                listener_regressions.get(dimension, 0.0), -mean_delta
            )
    improvement = sum(candidate_primary) / len(candidate_primary) - sum(incumbent_primary) / len(
        incumbent_primary
    )
    preferences: list[int] = []
    for values in listener_preferences:
        candidate_preferred = sum(value == "candidate" for value in values)
        incumbent_preferred = sum(value == "incumbent" for value in values)
        ties = sum(value == "tie" for value in values)
        if len(values) != 3 or candidate_preferred + incumbent_preferred + ties != 3:
            return ConfirmationDecision(
                candidate_id,
                False,
                "invalid_preference_set",
                improvement,
                listener_regressions,
                severe_candidate - severe_incumbent,
                None,
            )
        preferences.append(candidate_preferred)
    passed = (
        improvement >= 0.5
        and all(value <= 0.5 for value in listener_regressions.values())
        and severe_candidate <= severe_incumbent
        and all(value >= 2 for value in preferences)
        and all(value != "tie" for values in listener_preferences for value in values)
    )
    return ConfirmationDecision(
        candidate_id=candidate_id,
        passed=passed,
        reason="passed" if passed else "quality_gate_failed",
        mean_primary_improvement=improvement,
        listener_dimension_regressions=listener_regressions,
        severe_artifact_delta=severe_candidate - severe_incumbent,
        listener_preferences=(preferences[0], preferences[1]),
    )


def require_complete_matched_pairs(
    candidate_seeds: Mapping[int, Any], incumbent_seeds: Mapping[int, Any]
) -> None:
    if set(candidate_seeds) != set(incumbent_seeds) or len(candidate_seeds) != 3:
        raise CampaignGateError("quality decision requires complete matched three-seed pairs")


def _confirmation_base_id(declared_case_id: str) -> str:
    """Strip the ``-confirmation-<seed>`` suffix from a confirmation sample ID."""

    marker = "-confirmation-"
    if marker not in declared_case_id:
        raise CampaignGateError("confirmation sample has no declared base ID")
    base, _separator, seed_text = declared_case_id.rpartition(marker)
    if not base or not seed_text.isdigit():
        raise CampaignGateError("confirmation sample ID is malformed")
    return base


def _incumbent_label(task_type: str) -> str:
    return f"{task_type} incumbent"


def remove_evaluation_copy(path: Path, *, media_root: Path) -> None:
    """Remove one explicitly named evaluation copy under the private root."""

    root = media_root.expanduser().resolve()
    candidate = path.expanduser().resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise CampaignGateError("evaluation copy path escapes the private media root")
    if candidate.exists():
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()

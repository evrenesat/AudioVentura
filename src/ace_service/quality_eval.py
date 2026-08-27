"""Local operator CLI for the fixed-input ACE-Step quality campaign.

The module intentionally has no FastAPI route and imports no Runpod client on
the dry-run path.  Paid execution is admitted only after the durable campaign
store, rate evidence, billing-boundary evidence, maintenance gate, and
explicit remote-change authorization all agree.  Execution creates ordinary
durable controller jobs through ``repository.create_job`` and drives each job
through the controller's own queue/transfer machinery (the same
``ControllerWorker.process_job`` boundary with the real Runpod and
signed-transfer clients), one at a time, awaiting terminal evidence before the
next submission and confirming zero-worker teardown.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from ace_service.campaign import (
    CAMPAIGN_ADMISSION_STOP_MICRO_USD,
    CAMPAIGN_CEILING_MICRO_USD,
    BoundaryEvidence,
    CampaignCase,
    CampaignError,
    CampaignGateError,
    CampaignPlan,
    CampaignSchemaError,
    CampaignStore,
    CampaignSubmitter,
    CampaignValidationError,
    FixtureManifest,
    RateCatalog,
    RemoteChangeAuthorization,
    _load_json,
    build_campaign_plan,
    build_confirmation_cases,
    execution_micro_usd,
    load_fixture_manifest,
    utc_now,
    validate_plan_safety,
)

DEFAULT_BLOCKED_ROUTES = (
    "POST /create",
    "POST /cover",
    "POST /cover/{job_id}/confirm",
)
DEFAULT_EXECUTION_TIMEOUT_MS = 1_200_000
DEFAULT_TERMINAL_WAIT_MS = 3_600_000
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_ELIGIBLE_GPU_IDS = ("rtx-4090-24gb",)

# Server-owned alias map from the worker-returned device name to the campaign
# GPU identifiers used by the rate catalog.  An unmapped device name keeps
# cost evidence unavailable instead of guessing a GPU class.
_WORKER_GPU_ALIASES: Mapping[str, str] = {
    "NVIDIA GeForce RTX 4090": "rtx-4090-24gb",
    "NVIDIA RTX 4090": "rtx-4090-24gb",
    "NVIDIA GeForce RTX 3090": "rtx-3090-24gb",
    "NVIDIA RTX A6000": "rtx-a6000-48gb",
    "NVIDIA L40S": "l40s-48gb",
}


class CampaignJobDriver(Protocol):
    """The durable controller queue/transfer boundary driven by the CLI."""

    async def process_job(self, job_id: str) -> None: ...


class ControllerQueueSubmitter:
    """Adapter that can only submit through a durable job factory and queue."""

    def __init__(
        self,
        job_factory: Callable[[str, Mapping[str, Any]], str],
        enqueue: Callable[[str], None],
        wait_for_terminal: Callable[[str, str, Mapping[str, Any]], None] | None = None,
        *,
        teardown: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self._job_factory = job_factory
        self._enqueue = enqueue
        self._wait_for_terminal = wait_for_terminal
        self._teardown = teardown
        self._busy = False

    def submit(
        self,
        campaign_id: str,
        sample: Mapping[str, Any],
        *,
        on_submitted: Callable[[str], None] | None = None,
    ) -> str:
        if self._busy:
            raise CampaignGateError("campaign submitter received concurrent work")
        if self._wait_for_terminal is None:
            raise CampaignGateError("campaign submitter must provide terminal-job reconciliation")
        self._busy = True
        try:
            job_id = self._job_factory(campaign_id, sample)
            if not isinstance(job_id, str) or not job_id.strip():
                raise CampaignGateError("durable controller job factory returned no ID")
            self._enqueue(job_id)
            if on_submitted is not None:
                on_submitted(job_id)
            self._wait_for_terminal(campaign_id, job_id, sample)
            return job_id
        finally:
            self._busy = False

    def teardown(self, campaign_id: str, window_id: int, *, reason: str) -> None:
        if self._teardown is None:
            raise CampaignGateError("submitter cannot confirm zero-worker teardown")
        self._teardown(campaign_id, window_id, reason)


def _default_data_root() -> Path:
    return (
        Path(os.environ.get("ACE_SERVICE_DATA_ROOT", "/srv/ace-service/data"))
        .expanduser()
        .resolve()
    )


def _default_manifest() -> Path:
    return _default_data_root() / "evaluations" / "quality-fixture-v1" / "manifest.json"


def _default_campaign_database() -> Path:
    explicit = os.environ.get("ACE_EVALUATION_CAMPAIGN_DATABASE")
    return (
        Path(explicit).expanduser().resolve()
        if explicit
        else _default_data_root() / "evaluations" / "quality-campaign.sqlite3"
    )


def _load_json_file(path: Path, label: str) -> Any:
    try:
        raw = path.expanduser().resolve().read_bytes()
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignValidationError(f"{label} could not be read") from exc


def _load_rates(path: Path | None) -> RateCatalog:
    if path is None:
        return RateCatalog.from_mapping({"rates": []})
    return RateCatalog.from_mapping(_load_json_file(path, "rate catalog"))


def _load_boundary(path: Path | None) -> BoundaryEvidence | None:
    if path is None:
        return None
    value = _load_json_file(path, "billing boundary evidence")
    if not isinstance(value, Mapping):
        raise CampaignValidationError("billing boundary evidence must be an object")
    return BoundaryEvidence(
        start_inclusive=value.get("start_inclusive") is True,
        end_exclusive=value.get("end_exclusive") is True,
        native_bucket_seconds=int(value.get("native_bucket_seconds", 0)),
        native_bucket_start_field=str(value.get("native_bucket_start_field", "")),
        empty_response_behavior=str(value.get("empty_response_behavior", "")),
        current_partial_bucket_behavior=str(value.get("current_partial_bucket_behavior", "")),
        late_update_behavior=str(value.get("late_update_behavior", "")),
        source=str(value.get("source", "fixture-and-live-probe")),
    )


def _load_authorization(path: Path | None) -> RemoteChangeAuthorization | None:
    if path is None:
        return None
    return RemoteChangeAuthorization.from_mapping(_load_json_file(path, "remote authorization"))


def _highest_rate(
    catalog: RateCatalog, eligible_gpu_ids: Sequence[str] = DEFAULT_ELIGIBLE_GPU_IDS
) -> tuple[str, Any] | None:
    eligible = {
        gpu_id: catalog.rates[gpu_id] for gpu_id in eligible_gpu_ids if gpu_id in catalog.rates
    }
    if not eligible:
        return None
    gpu_id, evidence = max(
        eligible.items(), key=lambda item: execution_micro_usd(1, item[1].hourly_rate_usd)
    )
    return gpu_id, evidence


def _case_reservation_micro_usd(
    case: CampaignCase,
    catalog: RateCatalog,
    eligible_gpu_ids: Sequence[str] = DEFAULT_ELIGIBLE_GPU_IDS,
) -> int:
    return _timeout_reservation_micro_usd(catalog, eligible_gpu_ids)


def _timeout_reservation_micro_usd(
    catalog: RateCatalog,
    eligible_gpu_ids: Sequence[str] = DEFAULT_ELIGIBLE_GPU_IDS,
) -> int:
    highest = _highest_rate(catalog, eligible_gpu_ids)
    if highest is None:
        raise CampaignGateError("no trusted eligible GPU rate is available")
    return execution_micro_usd(DEFAULT_EXECUTION_TIMEOUT_MS, highest[1].hourly_rate_usd)


def _dry_run_summary(
    manifest: FixtureManifest,
    plan: CampaignPlan,
    catalog: RateCatalog,
    eligible_gpu_ids: Sequence[str] = DEFAULT_ELIGIBLE_GPU_IDS,
) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    rate_status = "available"
    try:
        catalog.require_fresh(eligible_gpu_ids)
    except CampaignGateError:
        highest = None
    else:
        highest = _highest_rate(catalog, eligible_gpu_ids)
    if highest is None:
        rate_status = "unavailable"
        reason = (
            "missing_trusted_flex_rate_for_eligible_gpu"
            if any(gpu_id not in catalog.rates for gpu_id in eligible_gpu_ids)
            else "stale_trusted_flex_rate_for_eligible_gpu"
        )
        blocking_reasons.append(reason)
    reservations: list[int] = []
    if highest is not None:
        reservations = [
            _case_reservation_micro_usd(case, catalog, eligible_gpu_ids)
            for case in plan.cases
            if case.stage
            in {
                "compatibility",
                "cover-screen",
                "original-screen",
                "cover-conditional-model",
                "original-conditional-model",
            }
        ]
        # Confirmation jobs use the same conservative timeout until the
        # incumbent observations freeze a narrower case rule.
        compatibility_reservation = _case_reservation_micro_usd(
            plan.cases[0], catalog, eligible_gpu_ids
        )
        worst_case = sum(reservations) + (
            compatibility_reservation * plan.maximum_confirmation_attempts
        )
        if worst_case > CAMPAIGN_CEILING_MICRO_USD:
            blocking_reasons.append("worst_case_reservation_exceeds_campaign_ceiling")
    else:
        worst_case = None
    if any(case.requires_storage for case in plan.cases):
        blocking_reasons.append("conditional_model_storage_evidence_required")
    return {
        "mode": "dry-run",
        "fixture_id": manifest.fixture_id,
        "manifest_sha256": manifest.manifest_sha256,
        "plan": {
            "minimum_jobs": plan.minimum_jobs,
            "maximum_jobs": plan.maximum_jobs,
            "minimum_paid_attempts": plan.minimum_paid_attempts,
            "maximum_paid_attempts": plan.maximum_paid_attempts,
            "conditional_stages": sorted(
                {case.stage for case in plan.cases if case.conditional_on}
            ),
            "storage_branches": plan.storage_case_count,
        },
        "budget": {
            "currency": "USD",
            "ceiling_micro_usd": CAMPAIGN_CEILING_MICRO_USD,
            "admission_stop_micro_usd": CAMPAIGN_ADMISSION_STOP_MICRO_USD,
            "worst_case_reserved_micro_usd": worst_case,
            "rate_status": rate_status,
        },
        "admissible": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "runpod_calls": 0,
        "production_defaults_changed": False,
    }


def _v2_normalized_request(sample: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact metadata-only worker shape for one campaign sample.

    Full resolved parameters are wrapped into the worker v2 normalized shape.
    Strict compatibility smoke payloads (already carrying a schema version and
    no resolved duration) are expanded into the complete strict-v1 or strict-v2
    worker envelope so the durable smoke passes the real worker parser.
    """

    resolved = dict(sample["resolved_parameters"])
    task_type = str(sample["task_type"])
    # Strict compatibility smokes are the only campaign records carrying a
    # schema-version marker inside their resolved parameters; the complete
    # frozen inputs are expanded into the strict v1/v2 worker envelope.
    if resolved.get("schema_version") in {1, 2}:
        return _compatibility_normalized_request(sample, resolved, int(resolved["schema_version"]))
    duration = resolved.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise CampaignValidationError("sample resolved duration is malformed")
    seed = sample.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise CampaignValidationError("sample seed is malformed")
    duration_seconds = sample.get("duration_seconds")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise CampaignValidationError("sample duration seconds are malformed")
    generation: dict[str, Any] = {
        "prompt": str(resolved.get("caption", "")),
        "lyrics": resolved.get("lyrics") or "",
        "instrumental": False,
        "vocal_language": "auto",
        "prompt_mode": str(resolved.get("prompt_mode", "direct")),
        "duration_mode": str(resolved.get("duration_mode", "custom")),
        "duration_seconds": float(duration_seconds),
        "duration": float(duration),
        "bpm": None,
        "key_scale": None,
        "time_signature": None,
        "seed": seed,
        "output_format": "mp3",
        "audio_cover_strength": float(resolved.get("audio_cover_strength", 1.0)),
        "cover_noise_strength": float(resolved.get("cover_noise_strength", 0.0)),
    }
    if task_type == "cover":
        # The strict worker schema requires the source-style text and both
        # cover controls on every cover generation.
        generation["target_style"] = str(resolved.get("caption", ""))
    return {
        "schema_version": 2,
        "task_type": task_type,
        "profile_id": str(resolved.get("profile_id", "fast-beta-v1")),
        "resolved_parameters": resolved,
        "generation": generation,
        "source": None,
    }


# Campaign-only identity fields recorded alongside the resolved parameters;
# the strict worker schema rejects them inside ``resolved_parameters``.
_SMOKE_CAMPAIGN_ONLY_KEYS = frozenset({"schema_version", "model", "lm_model"})


def _compatibility_normalized_request(
    sample: Mapping[str, Any], resolved: dict[str, Any], schema_version: int
) -> dict[str, Any]:
    """Build the complete strict-v1/v2 worker envelope for one compatibility smoke.

    The v1 envelope carries only the legacy generation shape; the v2 envelope
    carries the full generation plus ``profile_id`` and the worker-safe
    resolved parameters.  ``job_id``, ``task_type``, ``variation_index``,
    ``submission_nonce``, and transfer capabilities stay at the ordinary
    controller worker boundary (``ControllerWorker._default_payload`` and
    ``_submit_variation``).
    """

    seed = sample.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise CampaignValidationError("sample seed is malformed")
    duration = resolved.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise CampaignValidationError("sample resolved duration is malformed")
    caption = str(resolved.get("caption", ""))
    lyrics = resolved.get("lyrics") or ""
    prompt_mode = str(resolved.get("prompt_mode", "direct"))
    duration_mode = str(resolved.get("duration_mode", "custom"))
    if schema_version == 1:
        generation = {
            "prompt": caption,
            "lyrics": lyrics,
            "instrumental": False,
            "vocal_language": "en",
            "duration": float(duration),
            "bpm": None,
            "key_scale": None,
            "time_signature": None,
            "seed": seed,
            "output_format": "mp3",
            "cover_strength": 1.0,
        }
        return {"schema_version": 1, "generation": generation, "source": None}
    worker_resolved = {
        key: value for key, value in resolved.items() if key not in _SMOKE_CAMPAIGN_ONLY_KEYS
    }
    generation = {
        "prompt": caption,
        "lyrics": lyrics,
        "instrumental": False,
        "vocal_language": "en",
        "prompt_mode": prompt_mode,
        "duration_mode": duration_mode,
        "duration_seconds": float(duration),
        "duration": float(duration),
        "bpm": None,
        "key_scale": None,
        "time_signature": None,
        "seed": seed,
        "output_format": "mp3",
        "audio_cover_strength": float(resolved.get("audio_cover_strength", 1.0)),
        "cover_noise_strength": float(resolved.get("cover_noise_strength", 0.0)),
    }
    return {
        "schema_version": 2,
        "profile_id": str(resolved.get("profile_id", "fast-beta-v1")),
        "resolved_parameters": worker_resolved,
        "generation": generation,
        "source": None,
    }


def _map_worker_gpu(device_name: Any) -> str | None:
    if not isinstance(device_name, str) or not device_name.strip():
        return None
    normalized = " ".join(device_name.split())
    return _WORKER_GPU_ALIASES.get(normalized)


def _submission_fingerprint(
    *,
    job_type: str,
    source_url: str | None,
    output_format: str,
    variation_count: int,
    normalized_request: Mapping[str, Any],
) -> str:
    """Non-sensitive SHA-256 fingerprint of one frozen durable submission.

    The fingerprint covers the job type, immutable normalized request, source
    semantics, variation count, and output format so crash recovery can prove
    an existing product row is exactly the frozen intent.
    """

    payload = {
        "job_type": job_type,
        "source_url": source_url or "",
        "output_format": output_format,
        "variation_count": variation_count,
        "normalized_request": normalized_request,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _job_matches_fingerprint(job: Any, fingerprint: str) -> bool:
    """Prove an existing product row is byte-for-byte the frozen intent."""
    return (
        _submission_fingerprint(
            job_type=str(job.job_type.value),
            source_url=job.source_url,
            output_format=str(job.output_format.value),
            variation_count=int(job.variation_count),
            normalized_request=job.normalized_request_json or {},
        )
        == fingerprint
    )


class DurableControllerSubmitter:
    """Executable campaign submission path through the durable boundaries.

    ``submit`` creates an ordinary durable controller job in the product
    database (``repository.create_job``), verifies it entered the durable
    controller queue, drives it to terminal evidence through the controller's
    own ``process_job`` machinery one at a time, and reconciles the campaign
    sample.  ``teardown`` refuses to close a window while any sample or
    reservation is unresolved and records the zero-worker assertion durably.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        driver: CampaignJobDriver,
        store: CampaignStore,
        rates: RateCatalog,
        terminal_timeout_ms: int = DEFAULT_TERMINAL_WAIT_MS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        cover_source_url: str | None = None,
        health_provider: Callable[[], Awaitable[Any]] | None = None,
        endpoint_id: str = "",
    ) -> None:
        self._session_factory = session_factory
        self._driver = driver
        self._store = store
        self._rates = rates
        self._terminal_timeout_ms = terminal_timeout_ms
        self._poll_interval_seconds = poll_interval_seconds
        self._cover_source_url = cover_source_url
        self._health_provider = health_provider
        self._endpoint_id = endpoint_id
        self._inner = ControllerQueueSubmitter(
            self._job_factory,
            self._enqueue,
            self._wait_for_terminal,
            teardown=self._teardown,
        )

    def submit(
        self,
        campaign_id: str,
        sample: Mapping[str, Any],
        *,
        on_submitted: Callable[[str], None] | None = None,
    ) -> str:
        return self._inner.submit(campaign_id, sample, on_submitted=on_submitted)

    def teardown(self, campaign_id: str, window_id: int, *, reason: str) -> None:
        self._inner.teardown(campaign_id, window_id, reason=reason)

    def reconcile(self, campaign_id: str) -> dict[str, Any]:
        """Resume only frozen UUID-linked product jobs through the durable boundary.

        The pre-intent crash boundary (reservation committed, submission
        intent not yet persisted) is reconciled first: the exact planned
        sample with one open compute reservation, no submitted timestamp, no
        submission intent, and no product-job link is recorded as proven
        unsubmitted and its reservation settled at zero, without creating a
        product job or calling the provider.  Pending submission intents then
        complete their product-row/campaign linkage with the preassigned UUID
        (either crash order, never a second product job), and every
        submitted/running/uncertain sample is driven through the ordinary
        controller polling/transfer boundary.  Unknown or conflicting product
        rows are refused; no new sample is admitted.
        """

        pre_intent = self._store.reconcile_pre_intent_samples(campaign_id)
        intents = self._store.list_submission_intents(campaign_id)
        samples = self._store.list_samples(campaign_id)
        intent_by_sample = {str(intent["sample_id"]): intent for intent in intents}
        intent_job_ids = {str(intent["product_job_uuid"]) for intent in intents}
        sample_by_id = {str(item["sample_id"]): item for item in samples}
        for item in samples:
            job_id = item.get("job_id")
            if job_id is None:
                continue
            if str(job_id) not in intent_job_ids:
                raise CampaignGateError(
                    f"product job {job_id} has no matching frozen submission intent"
                )
        resumed: list[str] = []
        completed: list[str] = []
        failed: list[str] = []
        uncertain: list[str] = []
        for sample_id, intent in sorted(intent_by_sample.items()):
            sample_row = sample_by_id.get(sample_id)
            if sample_row is None:
                raise CampaignGateError(
                    f"submission intent references an unknown sample: {sample_id}"
                )
            job_id = str(intent["product_job_uuid"])
            if str(intent["status"]) != "submitted" or sample_row["job_id"] is None:
                self._resume_intent(campaign_id, sample_row, intent)
                resumed.append(sample_id)
                refreshed = self._store.sample(sample_id)
                if refreshed is None:
                    raise CampaignGateError("resumed campaign sample disappeared")
                sample_row = refreshed
            if str(sample_row["status"]) in {"submitted", "running", "uncertain"}:
                sample = self._sample_mapping(sample_row)
                try:
                    asyncio.run(self._drive_and_reconcile(campaign_id, job_id, sample))
                except CampaignGateError:
                    current = self._store.sample(sample_id)
                    if current is not None and str(current["status"]) == "uncertain":
                        uncertain.append(sample_id)
                        continue
                    raise
                current = self._store.sample(sample_id)
                if current is None:
                    raise CampaignGateError("reconciled campaign sample disappeared")
                if str(current["status"]) == "completed":
                    completed.append(sample_id)
                elif str(current["status"]) == "failed":
                    failed.append(sample_id)
                else:
                    uncertain.append(sample_id)
        return {
            "campaign_id": campaign_id,
            "pre_intent_reconciled": pre_intent,
            "resumed": resumed,
            "completed": completed,
            "failed": failed,
            "uncertain": uncertain,
        }

    @staticmethod
    def _sample_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sample_id": str(row["sample_id"]),
            "case_id": str(row["declared_case_id"]),
            "task_type": str(row["task_type"]),
            "seed": int(row["seed"]),
            "duration_seconds": float(row["duration_seconds"]),
            "resolved_parameters": _load_json(
                str(row["resolved_parameters_json"]), "sample parameters"
            ),
        }

    def _resume_intent(
        self,
        campaign_id: str,
        sample_row: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> None:
        """Complete the durable linkage for one frozen pending intent."""
        from ace_service.models import JobType, OutputFormat
        from ace_service.repository import create_job, get_job

        sample_id = str(sample_row["sample_id"])
        job_id = str(intent["product_job_uuid"])
        job_type = JobType(str(sample_row["task_type"]))
        source_url = intent.get("source_url") or None
        normalized = _v2_normalized_request(self._sample_mapping(sample_row))
        fingerprint = _submission_fingerprint(
            job_type=job_type.value,
            source_url=source_url,
            output_format=OutputFormat.MP3.value,
            variation_count=1,
            normalized_request=normalized,
        )
        if str(intent["request_fingerprint"]) != fingerprint:
            raise CampaignGateError("submission intent conflicts with the frozen request")
        with self._session_factory() as session:
            existing = get_job(session, job_id)
            if existing is None:
                create_job(
                    session,
                    job_type=job_type,
                    source_url=source_url,
                    output_format=OutputFormat.MP3,
                    variation_count=1,
                    normalized_request_json=normalized,
                    job_id=job_id,
                )
                session.commit()
            elif not _job_matches_fingerprint(existing, fingerprint):
                raise CampaignGateError("existing product job conflicts with the submission intent")
        reservation_id = intent.get("reservation_id")
        self._store.mark_sample_submitted(
            campaign_id,
            sample_id,
            job_id,
            reservation_id=str(reservation_id) if reservation_id is not None else None,
        )
        self._store.mark_intent_submitted(campaign_id, sample_id, job_id)

    def _job_factory(self, campaign_id: str, sample: Mapping[str, Any]) -> str:
        from ace_service.models import JobType, OutputFormat
        from ace_service.repository import create_job, get_job

        task_type = str(sample["task_type"])
        if task_type not in {"original", "cover"}:
            raise CampaignValidationError("sample task type is unsupported")
        job_type = JobType(task_type)
        sample_id = str(sample["sample_id"])
        source_url = None
        if job_type is JobType.COVER:
            if not self._cover_source_url or not self._cover_source_url.startswith("https://"):
                raise CampaignGateError("cover execution requires --cover-source-url")
            source_url = self._cover_source_url
        normalized = _v2_normalized_request(sample)
        fingerprint = _submission_fingerprint(
            job_type=job_type.value,
            source_url=source_url,
            output_format=OutputFormat.MP3.value,
            variation_count=1,
            normalized_request=normalized,
        )
        reservation_id = sample.get("reservation_id")
        reservation_identifier = str(reservation_id) if reservation_id is not None else None
        # The product UUID is preassigned before either database commit; the
        # campaign submission intent is the durable no-duplicate boundary.
        intent = self._store.get_submission_intent(campaign_id, sample_id)
        if intent is None:
            product_job_id = str(uuid.uuid4())
            self._store.persist_submission_intent(
                campaign_id,
                sample_id,
                product_job_id,
                fingerprint,
                reservation_id=reservation_identifier,
                source_url=source_url,
            )
        else:
            product_job_id = str(intent["product_job_uuid"])
            if str(intent["request_fingerprint"]) != fingerprint:
                raise CampaignGateError("submission intent conflicts with the frozen request")
        with self._session_factory() as session:
            existing = get_job(session, product_job_id)
            if existing is None:
                job = create_job(
                    session,
                    job_type=job_type,
                    source_url=source_url,
                    output_format=OutputFormat.MP3,
                    variation_count=1,
                    normalized_request_json=normalized,
                    job_id=product_job_id,
                )
                session.commit()
                job_id = str(job.id)
            else:
                if not _job_matches_fingerprint(existing, fingerprint):
                    raise CampaignGateError(
                        "existing product job conflicts with the submission intent"
                    )
                job_id = product_job_id
        self._store.mark_sample_submitted(
            campaign_id,
            sample_id,
            job_id,
            reservation_id=reservation_identifier,
        )
        self._store.mark_intent_submitted(campaign_id, sample_id, job_id)
        return job_id

    def _enqueue(self, job_id: str) -> None:
        """Verify the durable controller queue accepted the job row."""
        from ace_service.models import JobStatus
        from ace_service.repository import get_job

        with self._session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                raise CampaignGateError("durable controller job is not queued for submission")

    def _wait_for_terminal(self, campaign_id: str, job_id: str, sample: Mapping[str, Any]) -> None:
        asyncio.run(self._drive_and_reconcile(campaign_id, job_id, sample))

    async def _drive_and_reconcile(
        self, campaign_id: str, job_id: str, sample: Mapping[str, Any]
    ) -> None:
        deadline = time.monotonic() + self._terminal_timeout_ms / 1000
        confirmed_cover = False
        while True:
            await self._driver.process_job(job_id)
            status, evidence = self._job_status(job_id)
            if status == "completed":
                self._reconcile(campaign_id, job_id, sample, "completed", evidence)
                return
            if status == "failed":
                self._reconcile(campaign_id, job_id, sample, "failed", evidence)
                return
            if status == "cancelled":
                self._reconcile(campaign_id, job_id, sample, "cancelled", evidence)
                return
            if status == "staging" and not confirmed_cover:
                confirmed_cover = self._confirm_cover(job_id)
                continue
            if time.monotonic() > deadline:
                self._store.record_terminal_execution(
                    campaign_id,
                    str(sample["sample_id"]),
                    status="uncertain",
                    actual_gpu=None,
                    execution_ms=None,
                    hourly_rate_usd=None,
                    unavailable_reason="terminal_evidence_timeout",
                )
                raise CampaignGateError("campaign job did not reach terminal evidence in time")
            await asyncio.sleep(self._poll_interval_seconds)

    def _job_status(self, job_id: str) -> tuple[str, dict[str, Any]]:
        from sqlalchemy import select

        from ace_service.models import JobStatus, Output
        from ace_service.repository import get_job, get_variation_attempt

        with self._session_factory() as session:
            job = get_job(session, job_id)
            if job is None:
                raise CampaignGateError("durable controller job disappeared")
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                attempt = get_variation_attempt(session, job_id, 1)
                evidence: dict[str, Any] = {
                    "output_path": None,
                    "actual_gpu": None,
                    "execution_ms": None,
                }
                if job.status is JobStatus.COMPLETED:
                    output = session.scalar(
                        select(Output).where(
                            Output.job_id == job.id,
                            Output.variation_index == 1,
                            Output.result_index == 0,
                        )
                    )
                    if output is None or not output.relative_path:
                        raise CampaignGateError(
                            "completed campaign job has no validated output evidence"
                        )
                    evidence["output_path"] = str(output.relative_path)
                    if attempt is not None and isinstance(attempt.runpod_result_json, dict):
                        metadata = attempt.runpod_result_json
                        execution_ms = metadata.get("runpod_execution_ms")
                        if isinstance(execution_ms, bool) or not isinstance(execution_ms, int):
                            execution_ms = None
                        evidence["execution_ms"] = execution_ms
                        worker = metadata.get("worker")
                        if isinstance(worker, Mapping):
                            evidence["actual_gpu"] = _map_worker_gpu(worker.get("gpu"))
                else:
                    if attempt is not None and isinstance(attempt.runpod_result_json, dict):
                        execution_ms = attempt.runpod_result_json.get("runpod_execution_ms")
                        if isinstance(execution_ms, bool) or not isinstance(execution_ms, int):
                            execution_ms = None
                        evidence["execution_ms"] = execution_ms
                return job.status.value, evidence
            return job.status.value, {}

    def _confirm_cover(self, job_id: str) -> bool:
        """Freeze the staged campaign cover through the durable confirmation path."""
        from ace_service.models import JobStatus
        from ace_service.repository import confirm_cover_job, get_job

        with self._session_factory() as session:
            job = get_job(session, job_id)
            if job is None or job.status is not JobStatus.STAGING:
                return False
            confirm_cover_job(session, job_id)
            session.commit()
            return True

    def _reconcile(
        self,
        campaign_id: str,
        job_id: str,
        sample: Mapping[str, Any],
        status: str,
        evidence: Mapping[str, Any],
    ) -> None:
        del job_id
        sample_id = str(sample["sample_id"])
        actual_gpu = evidence.get("actual_gpu")
        execution_ms = evidence.get("execution_ms")
        hourly_rate: Any = None
        if isinstance(actual_gpu, str) and actual_gpu in self._rates.rates:
            hourly_rate = self._rates.rates[actual_gpu].hourly_rate_usd
        unavailable = (
            None
            if (
                status == "completed"
                and actual_gpu is not None
                and execution_ms is not None
                and hourly_rate is not None
            )
            else "missing_gpu_execution_or_trusted_rate"
        )
        output_path = str(evidence["output_path"]) if status == "completed" else None
        self._store.record_terminal_execution(
            campaign_id,
            sample_id,
            status=status,
            actual_gpu=actual_gpu,
            execution_ms=execution_ms,
            hourly_rate_usd=hourly_rate,
            unavailable_reason=unavailable,
            output_path=output_path,
        )

    def _teardown(self, campaign_id: str, window_id: int, reason: str) -> None:
        from ace_service.runpod_client import RunpodError, parse_worker_counts

        samples = self._store.list_samples(campaign_id)
        in_flight = [
            item for item in samples if item["status"] in {"submitted", "running", "uncertain"}
        ]
        if in_flight:
            raise CampaignGateError("window teardown requires terminal samples")
        if self._health_provider is None:
            raise CampaignGateError("teardown requires validated provider-health evidence")
        try:
            from ace_service.runpod_client import RunpodHealth

            health = asyncio.run(cast("Coroutine[Any, Any, Any]", self._health_provider()))
            counts = parse_worker_counts(cast(RunpodHealth, health))
        except (RunpodError, OSError, ValueError) as exc:
            raise CampaignGateError("teardown requires validated provider-health evidence") from exc
        if counts.active > 0 or counts.has_pending_work:
            raise CampaignGateError(
                "window teardown requires provider-observed zero workers and "
                "zero queued/in-progress work"
            )
        if not self._endpoint_id:
            raise CampaignGateError("teardown requires the authorized endpoint identity")
        evidence = {
            "endpoint_id": self._endpoint_id,
            "captured_at_utc": utc_now().isoformat(),
            "active_workers": counts.active,
            "idle_workers": counts.idle,
            "running_workers": counts.running,
            "queued_jobs": counts.queued,
            "in_progress_jobs": counts.in_progress,
            "source": "runpod_health",
        }
        # The store refuses malformed evidence and any open sample or
        # reservation; this close is the durable zero-at-rest teardown record.
        self._store.close_execution_window(
            campaign_id,
            window_id,
            health_evidence=evidence,
            reason=reason,
        )


class EvaluationController:
    """Store-backed sequential executor; each submitter call must await terminal evidence."""

    def __init__(
        self,
        store: CampaignStore,
        manifest: FixtureManifest | None = None,
        plan: CampaignPlan | None = None,
        *,
        submitter: CampaignSubmitter | None = None,
        runtime_id: str = "ace-step-v0.1.8",
        image_digest: str = "unrecorded",
        eligible_gpu_ids: Sequence[str] = DEFAULT_ELIGIBLE_GPU_IDS,
    ) -> None:
        self.store = store
        self.manifest = manifest
        self.plan = plan
        if plan is not None:
            validate_plan_safety(plan)
        self.submitter = submitter
        self.runtime_id = runtime_id
        self.image_digest = image_digest
        self.eligible_gpu_ids = tuple(eligible_gpu_ids)

    def _require_manifest(
        self,
    ) -> tuple[FixtureManifest, CampaignPlan]:
        """Recovery-only controllers never load the external fixture manifest."""
        if self.manifest is None or self.plan is None:
            raise CampaignGateError(
                "this action requires the frozen fixture manifest and campaign plan"
            )
        return self.manifest, self.plan

    def prepare(self, campaign_id: str) -> dict[str, Any]:
        manifest, plan = self._require_manifest()
        existing = self.store.get_campaign(campaign_id)
        if existing is None:
            self.store.create_campaign(campaign_id, manifest, plan)
        elif existing["manifest_sha256"] != manifest.manifest_sha256:
            raise CampaignValidationError("campaign manifest fingerprint does not match")
        sample_ids: dict[str, str] = {}
        for case in plan.cases:
            if case.conditional_on is not None:
                continue
            sample_id, _created = self.store.add_sample(
                campaign_id,
                case,
                fixture_id=manifest.fixture_id,
                runtime_id=self.runtime_id,
                image_digest=self.image_digest,
            )
            sample_ids[case.declared_case_id] = sample_id
        return {"campaign_id": campaign_id, "sample_ids": sample_ids}

    def prepare_confirmation(
        self,
        campaign_id: str,
        finalist_case_ids: Sequence[str],
        *,
        confirmed: bool,
    ) -> dict[str, str]:
        """Materialize confirmation pairs only after a fresh operator decision."""

        if not confirmed:
            raise CampaignGateError("confirmation stage requires a fresh explicit confirmation")
        manifest, plan = self._require_manifest()
        cases = build_confirmation_cases(manifest, plan, finalist_case_ids)
        result: dict[str, str] = {}
        for case in cases:
            sample_id, _created = self.store.add_sample(
                campaign_id,
                case,
                fixture_id=manifest.fixture_id,
                runtime_id=self.runtime_id,
                image_digest=self.image_digest,
            )
            result[case.declared_case_id] = sample_id
        self.store.set_campaign_status(campaign_id, "awaiting_confirmation")
        return result

    def _check_execution_inputs(
        self,
        campaign_id: str,
        *,
        authorization: RemoteChangeAuthorization,
        rates: RateCatalog,
        boundary: BoundaryEvidence,
    ) -> None:
        manifest, _plan = self._require_manifest()
        if authorization.ceiling_micro_usd > manifest.ceiling_micro_usd:
            raise CampaignGateError("remote authorization exceeds the frozen campaign ceiling")
        if authorization.endpoint_id == "":
            raise CampaignGateError("remote authorization endpoint is missing")
        fresh_rates = rates.require_fresh(self.eligible_gpu_ids)
        if not boundary.proven:
            raise CampaignGateError("provider interval semantics are ambiguous")
        if set(authorization.blocked_routes) != set(DEFAULT_BLOCKED_ROUTES):
            raise CampaignGateError(
                "remote authorization must enumerate every enqueue-capable route"
            )
        if not authorization.edge_guard_verified or not authorization.edge_config_sha256:
            raise CampaignGateError("the authenticated edge rollback guard is not verified")
        for evidence in fresh_rates.values():
            self.store.add_rate_evidence(campaign_id, evidence)
        self.store.record_boundary_evidence(campaign_id, boundary)
        self.store.record_authorization(campaign_id, authorization)

    def execute_screening(
        self,
        campaign_id: str,
        *,
        confirmed: bool,
        authorization: RemoteChangeAuthorization | None,
        rates: RateCatalog,
        boundary: BoundaryEvidence | None,
    ) -> dict[str, Any]:
        """Run only the first declared screening stage, never auto-advance conditionals."""

        if not confirmed:
            raise CampaignGateError("execute requires an explicit confirmation flag")
        if authorization is None or boundary is None:
            raise CampaignGateError(
                "remote authorization and billing-boundary evidence are required"
            )
        self.prepare(campaign_id)
        self._check_execution_inputs(
            campaign_id, authorization=authorization, rates=rates, boundary=boundary
        )
        _manifest, plan = self._require_manifest()
        if self.submitter is None:
            raise CampaignGateError(
                "a running durable controller submitter is required for execute"
            )
        window_id = self.store.open_execution_window(
            campaign_id,
            "screening",
            blocked_routes=DEFAULT_BLOCKED_ROUTES,
            edge_config_sha256=authorization.edge_config_sha256,
        )
        self.store.record_edge_guard(
            campaign_id,
            enabled=True,
            verified=True,
            blocked_routes=DEFAULT_BLOCKED_ROUTES,
            config_sha256=authorization.edge_config_sha256,
            rollback_target=authorization.rollback_target,
        )
        submitted: list[str] = []
        try:
            samples = self.store.list_samples(campaign_id, include_aliases=True)
            canonical_by_declared: dict[str, str] = {}
            for item in samples:
                canonical_id = str(item["sample_id"])
                canonical_by_declared[str(item["declared_case_id"])] = canonical_id
                for alias in item.get("aliases", []):
                    canonical_by_declared[str(alias)] = canonical_id
            for case in plan.cases:
                if case.stage not in {"compatibility", "cover-screen", "original-screen"}:
                    continue
                sample_id = canonical_by_declared[case.declared_case_id]
                if sample_id in submitted:
                    continue
                self.store.require_submission_allowed(campaign_id=campaign_id)
                reservation_id = f"res-{secrets.token_urlsafe(12).replace('-', '_')}"
                amount = _case_reservation_micro_usd(case, rates)
                self.store.reserve(
                    campaign_id,
                    reservation_id,
                    kind="compute",
                    reserved_micro_usd=amount,
                    sample_id=sample_id,
                )

                def mark_submitted(
                    submitted_job_id: str,
                    *,
                    bound_sample_id: str = sample_id,
                    bound_reservation_id: str = reservation_id,
                ) -> None:
                    self.store.mark_sample_submitted(
                        campaign_id,
                        bound_sample_id,
                        submitted_job_id,
                        reservation_id=bound_reservation_id,
                    )

                self.submitter.submit(
                    campaign_id,
                    {
                        "sample_id": sample_id,
                        "case_id": case.declared_case_id,
                        "task_type": case.task_type,
                        "seed": case.seed,
                        "duration_seconds": case.duration_seconds,
                        "resolved_parameters": dict(case.resolved_parameters),
                        "reservation_id": reservation_id,
                    },
                    on_submitted=mark_submitted,
                )
                submitted.append(sample_id)
            assert self.submitter is not None
            self.submitter.teardown(
                campaign_id,
                window_id,
                reason="screening stage complete; endpoint returned to zero",
            )
            self.store.set_campaign_status(
                campaign_id,
                "awaiting_scores",
                reason="both listener score sheets are required",
            )
            return {
                "campaign_id": campaign_id,
                "window_id": window_id,
                "submitted_samples": submitted,
            }
        except Exception:
            self.store.set_campaign_status(
                campaign_id, "failed", reason="screening execution failed"
            )
            raise
        finally:
            # The gate clears only through the verified teardown primitive
            # (provider-observed zero evidence); reaching finally never clears
            # the gate by itself.
            if not submitted:
                assert self.submitter is not None
                try:
                    self.submitter.teardown(
                        campaign_id,
                        window_id,
                        reason="no sample was submitted",
                    )
                except Exception:
                    pass

    def execute_confirmation(
        self,
        campaign_id: str,
        *,
        confirmed: bool,
        authorization: RemoteChangeAuthorization | None,
        rates: RateCatalog,
        boundary: BoundaryEvidence | None,
    ) -> dict[str, Any]:
        """Run only the declared confirmation stage for preselected finalists.

        Screening-seed confirmation cases reuse the already-executed screening
        samples (exact-fingerprint aliases); only the planned confirmation
        samples are reserved and submitted, so no sample is ever charged
        twice.
        """

        if not confirmed:
            raise CampaignGateError("execute requires an explicit confirmation flag")
        if authorization is None or boundary is None:
            raise CampaignGateError(
                "remote authorization and billing-boundary evidence are required"
            )
        if self.submitter is None:
            raise CampaignGateError(
                "a running durable controller submitter is required for execute"
            )
        self._check_execution_inputs(
            campaign_id, authorization=authorization, rates=rates, boundary=boundary
        )
        window_id = self.store.open_execution_window(
            campaign_id,
            "confirmation",
            blocked_routes=DEFAULT_BLOCKED_ROUTES,
            edge_config_sha256=authorization.edge_config_sha256,
        )
        self.store.record_edge_guard(
            campaign_id,
            enabled=True,
            verified=True,
            blocked_routes=DEFAULT_BLOCKED_ROUTES,
            config_sha256=authorization.edge_config_sha256,
            rollback_target=authorization.rollback_target,
        )
        submitted: list[str] = []
        try:
            samples = self.store.list_samples(campaign_id)
            planned = [
                item
                for item in samples
                if item["stage"] in {"cover-confirmation", "original-confirmation"}
                and item["status"] == "planned"
            ]
            if not planned:
                raise CampaignGateError("confirmation stage has no payable samples")
            for sample in planned:
                sample_id = str(sample["sample_id"])
                self.store.require_submission_allowed(campaign_id=campaign_id)
                reservation_id = f"res-{secrets.token_urlsafe(12).replace('-', '_')}"
                amount = _timeout_reservation_micro_usd(rates, self.eligible_gpu_ids)
                self.store.reserve(
                    campaign_id,
                    reservation_id,
                    kind="compute",
                    reserved_micro_usd=amount,
                    sample_id=sample_id,
                )

                def mark_submitted(
                    submitted_job_id: str,
                    *,
                    bound_sample_id: str = sample_id,
                    bound_reservation_id: str = reservation_id,
                ) -> None:
                    self.store.mark_sample_submitted(
                        campaign_id,
                        bound_sample_id,
                        submitted_job_id,
                        reservation_id=bound_reservation_id,
                    )

                self.submitter.submit(
                    campaign_id,
                    {
                        "sample_id": sample_id,
                        "case_id": str(sample["declared_case_id"]),
                        "task_type": str(sample["task_type"]),
                        "seed": int(sample["seed"]),
                        "duration_seconds": float(sample["duration_seconds"]),
                        "resolved_parameters": _load_json(
                            str(sample["resolved_parameters_json"]), "sample parameters"
                        ),
                        "reservation_id": reservation_id,
                    },
                    on_submitted=mark_submitted,
                )
                submitted.append(sample_id)
            assert self.submitter is not None
            self.submitter.teardown(
                campaign_id,
                window_id,
                reason="confirmation stage complete; endpoint returned to zero",
            )
            self.store.set_campaign_status(
                campaign_id,
                "awaiting_scores",
                reason="confirmation score sheets are required",
            )
            return {
                "campaign_id": campaign_id,
                "window_id": window_id,
                "submitted_samples": submitted,
            }
        except Exception:
            self.store.set_campaign_status(
                campaign_id, "failed", reason="confirmation execution failed"
            )
            raise
        finally:
            if not submitted:
                assert self.submitter is not None
                try:
                    self.submitter.teardown(
                        campaign_id,
                        window_id,
                        reason="no confirmation sample was submitted",
                    )
                except Exception:
                    pass

    def reconcile(self, campaign_id: str) -> dict[str, Any]:
        """Resume only the campaign's frozen UUID-linked product jobs."""
        if self.submitter is None:
            raise CampaignGateError(
                "a running durable controller submitter is required for reconcile"
            )
        return self.submitter.reconcile(campaign_id)

    def verified_teardown(self, campaign_id: str, *, confirmed: bool) -> dict[str, Any]:
        """Close the open window only on complete evidence; otherwise stay blocked."""
        if not confirmed:
            raise CampaignGateError("verified teardown requires an explicit confirmation flag")
        if self.submitter is None:
            raise CampaignGateError(
                "a running durable controller submitter is required for verified teardown"
            )
        gate = self.store.current_gate()
        if not gate or int(gate.get("active", 0)) != 1:
            return {"campaign_id": campaign_id, "window_id": None, "teardown": "not_needed"}
        # An active gate/window belongs to exactly one campaign: a mismatched
        # gate must never be closed or reported not_needed for another
        # campaign's teardown request.
        if str(gate.get("campaign_id", "")) != campaign_id:
            raise CampaignGateError("active maintenance gate belongs to a different campaign")
        window_id = int(gate["window_id"])
        try:
            self.submitter.teardown(
                campaign_id,
                window_id,
                reason="verified operator teardown",
            )
        except CampaignGateError as exc:
            return {
                "campaign_id": campaign_id,
                "window_id": window_id,
                "teardown": "blocked",
                "reason": str(exc),
            }
        return {
            "campaign_id": campaign_id,
            "window_id": window_id,
            "teardown": "closed",
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operator-only ACE-Step quality campaign")
    parser.add_argument("--manifest", type=Path, default=_default_manifest())
    parser.add_argument("--campaign-db", type=Path, default=_default_campaign_database())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--advance", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    mode.add_argument("--verified-teardown", action="store_true")
    mode.add_argument("--export-score-sheet", metavar="LISTENER_ID")
    mode.add_argument("--import-score-sheet", type=Path)
    mode.add_argument("--finalize-score-sheet", metavar="LISTENER_ID")
    mode.add_argument("--decision", action="store_true")
    mode.add_argument("--backup", type=Path)
    parser.add_argument("--campaign-id")
    parser.add_argument("--listener-id")
    parser.add_argument("--stage", choices=("screening", "confirmation"), default="screening")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--rate-catalog", type=Path)
    parser.add_argument("--boundary-evidence", type=Path)
    parser.add_argument("--remote-authorization", type=Path)
    parser.add_argument("--cover-source-url")
    parser.add_argument("--terminal-timeout-ms", type=int, default=DEFAULT_TERMINAL_WAIT_MS)
    parser.add_argument(
        "--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--eligible-gpu", action="append", dest="eligible_gpus")
    return parser


def _write_output(value: Mapping[str, Any], output: Path | None) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output is None:
        print(encoded, end="")
        return
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(encoded, encoding="utf-8")
    target.chmod(0o600)


def _build_durable_submitter(
    *,
    store: CampaignStore,
    rates: RateCatalog,
    terminal_timeout_ms: int,
    poll_interval_seconds: float,
    cover_source_url: str | None,
) -> CampaignSubmitter:
    """Wire the executable path through the repository's durable boundaries.

    The controller worker, Runpod client, and signed-transfer machinery are
    imported only on the execute path so dry-run never touches them.
    """

    from ace_service.config import ServiceSettings
    from ace_service.db import create_database_engine, create_session_factory
    from ace_service.home_ingest import HomeIngestClient
    from ace_service.runpod_client import RunpodClient
    from ace_service.worker import ControllerWorker

    settings = ServiceSettings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    runpod_client = RunpodClient.from_settings(settings)
    home_ingest_client = HomeIngestClient(settings)
    worker = ControllerWorker(
        settings,
        session_factory,
        runpod_client,
        home_ingest_client=home_ingest_client,
    )
    return DurableControllerSubmitter(
        session_factory=session_factory,
        driver=worker,
        store=store,
        rates=rates,
        terminal_timeout_ms=terminal_timeout_ms,
        poll_interval_seconds=poll_interval_seconds,
        cover_source_url=cover_source_url,
        health_provider=runpod_client.health,
        endpoint_id=settings.runpod_endpoint_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        eligible_gpu_ids = tuple(args.eligible_gpus or DEFAULT_ELIGIBLE_GPU_IDS)
        # Recovery-only actions run from frozen durable state and must never
        # depend on the external fixture manifest being readable.
        if args.status:
            if not args.campaign_id:
                raise CampaignValidationError("this operator action requires --campaign-id")
            store = CampaignStore.open_existing(args.campaign_db)
            if store is None:
                raise CampaignSchemaError(
                    "campaign database does not exist; refusing to report status"
                )
            _write_output(store.campaign_status(args.campaign_id), args.output)
            return 0
        if args.backup:
            if not args.campaign_id:
                raise CampaignValidationError("this operator action requires --campaign-id")
            store = CampaignStore.open_existing(args.campaign_db)
            if store is None:
                raise CampaignSchemaError(
                    "campaign database does not exist; refusing to back it up"
                )
            # The campaign ID is an operator target guard: an unknown campaign
            # is blocked before any backup file (or other state) is created.
            if store.get_campaign(args.campaign_id) is None:
                raise CampaignValidationError("unknown campaign; refusing backup")
            backup = store.backup(args.backup)
            print(f"campaign_backup={backup.name}")
            return 0
        if args.reconcile or args.verified_teardown:
            if not args.campaign_id or not args.confirm:
                raise CampaignGateError("recovery actions require --campaign-id and --confirm")
            if args.terminal_timeout_ms <= 0 or args.poll_interval_seconds <= 0:
                raise CampaignValidationError("terminal timeout and poll interval must be positive")
            store = CampaignStore.open_existing(args.campaign_db)
            if store is None:
                raise CampaignSchemaError(
                    "campaign database does not exist; refusing recovery actions"
                )
            # The named campaign must exist before any controller/provider
            # wiring: an unknown campaign creates no product engine, worker,
            # Runpod client, or other external/client state.
            if store.get_campaign(args.campaign_id) is None:
                raise CampaignValidationError("unknown campaign; refusing recovery action")
            submitter = _build_durable_submitter(
                store=store,
                rates=_load_rates(args.rate_catalog),
                terminal_timeout_ms=args.terminal_timeout_ms,
                poll_interval_seconds=args.poll_interval_seconds,
                cover_source_url=args.cover_source_url,
            )
            # Recovery uses only the frozen campaign/sample/submission-intent
            # state plus the ordinary product-controller and provider-health
            # contracts; the fixture manifest is never loaded or rebuilt here.
            controller = EvaluationController(store, submitter=submitter)
            if args.reconcile:
                result = controller.reconcile(args.campaign_id)
            else:
                result = controller.verified_teardown(args.campaign_id, confirmed=True)
            _write_output(result, args.output)
            return 0
        # Every remaining mode derives its semantics from the frozen fixture
        # manifest, so it is loaded, hashed, and validated only here.
        manifest = load_fixture_manifest(args.manifest)
        plan = build_campaign_plan(manifest)
        validate_plan_safety(plan)
        if args.dry_run:
            summary = _dry_run_summary(
                manifest, plan, _load_rates(args.rate_catalog), eligible_gpu_ids
            )
            _write_output(summary, args.output)
            return 0
        if (
            args.advance
            or args.export_score_sheet
            or args.import_score_sheet
            or args.finalize_score_sheet
            or args.decision
        ):
            if not args.campaign_id:
                raise CampaignValidationError("this operator action requires --campaign-id")
            if args.advance and not args.confirm:
                raise CampaignGateError("advancement requires --confirm")
            store = CampaignStore(args.campaign_db)
            store.require_ordinary_submissions(args.campaign_id)
            if args.advance:
                result = store.advance_screening_to_confirmation(args.campaign_id, manifest, plan)
                _write_output(result, args.output)
                finalist_names = sorted(
                    item for items in result["finalists"].values() for item in items
                )
                print(f"advancement=recorded finalists={','.join(finalist_names) or 'none'}")
                return 0
            if args.export_score_sheet:
                value = store.export_score_sheet(
                    args.campaign_id, args.export_score_sheet, stage=args.stage
                )
                _write_output(value, args.output)
                return 0
            if args.import_score_sheet:
                if not args.listener_id:
                    raise CampaignValidationError("score import requires --listener-id")
                payload = _load_json_file(args.import_score_sheet, "score sheet")
                store.import_score_sheet(
                    args.campaign_id, args.listener_id, payload, stage=args.stage
                )
                print("score_sheet=imported")
                return 0
            if args.finalize_score_sheet:
                store.finalize_score_sheet(
                    args.campaign_id, args.finalize_score_sheet, stage=args.stage
                )
                print("score_sheet=finalized")
                return 0
            decision = store.record_quality_decision(args.campaign_id, manifest)
            _write_output(decision, args.output)
            print(f"quality_decision={decision['decision_id']} recorded={decision['recorded']}")
            return 0
        if not args.execute:
            raise CampaignValidationError("one operator mode is required")
        if not args.campaign_id or not args.confirm:
            raise CampaignGateError("execute requires --campaign-id and --confirm")
        if args.terminal_timeout_ms <= 0 or args.poll_interval_seconds <= 0:
            raise CampaignValidationError("terminal timeout and poll interval must be positive")
        authorization = _load_authorization(args.remote_authorization)
        boundary = _load_boundary(args.boundary_evidence)
        rates = _load_rates(args.rate_catalog)
        # Authorization and boundary evidence are validated before any
        # campaign store or controller worker is opened, so an absent gate
        # input cannot even create state.
        if authorization is None or boundary is None:
            raise CampaignGateError(
                "execute requires --remote-authorization and --boundary-evidence"
            )
        store = CampaignStore(args.campaign_db)
        store.require_ordinary_submissions(args.campaign_id)
        submitter = _build_durable_submitter(
            store=store,
            rates=rates,
            terminal_timeout_ms=args.terminal_timeout_ms,
            poll_interval_seconds=args.poll_interval_seconds,
            cover_source_url=args.cover_source_url,
        )
        controller = EvaluationController(store, manifest, plan, submitter=submitter)
        if args.stage == "screening":
            result = controller.execute_screening(
                args.campaign_id,
                confirmed=args.confirm,
                authorization=authorization,
                rates=rates,
                boundary=boundary,
            )
        else:
            result = controller.execute_confirmation(
                args.campaign_id,
                confirmed=args.confirm,
                authorization=authorization,
                rates=rates,
                boundary=boundary,
            )
        _write_output(result, args.output)
        return 0
    except (CampaignError, OSError, ValueError, sqlite3.Error) as exc:
        # Do not echo paths, URLs, prompts, lyrics, credentials, or provider
        # response bodies to the operator console.
        print(f"quality_eval=blocked reason={type(exc).__name__}", file=sys.stderr)
        return 2


# TODO(re-enable): the quality-campaign executable entrypoint is quarantined
# during the usability recovery. Re-enable after ordinary original and cover
# generation is stable (owner decision #10). The campaign implementation
# (main, EvaluationController, CampaignStore) stays importable and unit-tested;
# only direct module execution is disabled.
# if __name__ == "__main__":
#     raise SystemExit(main())

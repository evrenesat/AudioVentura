"""Durable universal-worker service for the ailocals.v1 facade.

Owns enrollment, authentication, presence, atomic claims with lease-time
transfer issuance, heartbeats, completion, failure, and revocation. Queue rows
hold bounded safe identity and state only: no creative payload, transfer URL,
or audio byte is ever persisted by this module.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from ace_service.ailocals import protocol
from ace_service.ailocals.protocol import (
    CAPABILITY_ACE,
    AilocalsError,
    CapabilityEntry,
    ErrorCode,
    LeaseRequestData,
    PresenceEntry,
)
from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.models import (
    AilocalsEnrollment,
    AilocalsJob,
    AilocalsWorker,
    Job,
    JobStatus,
    Output,
    TransferCapability,
    TransferDirection,
    VariationAttempt,
)
from ace_service.transfers import issue_transfer_url

CLAIM_POLL_INTERVAL_SECONDS = 0.25
UPLOAD_GRACE_SECONDS = 300
ACTIVE_LEASE_STATES: tuple[str, ...] = ("leased", "running")
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "canceled"})
WORKER_LOST_CODE = "ailocals_worker_lost"
IDENTITY_MISMATCH_CODE = "ailocals_identity_mismatch"

PayloadBuilder = Callable[[Job, VariationAttempt], Mapping[str, Any]]
CompletionValidator = Callable[[Mapping[str, Any], Job, VariationAttempt, Output | None], None]


def capability_entry_to_json(entry: CapabilityEntry) -> dict[str, Any]:
    """Serialize an enrolled capability entry for durable storage."""

    parameters = entry.parameters
    if isinstance(parameters, protocol.AceCapabilityParameters):
        raw_parameters: dict[str, Any] = {
            "worker_schema": parameters.worker_schema,
            "model_bundle_revision": parameters.model_bundle_revision,
            "manifest_sha256": parameters.manifest_sha256,
            "accelerator": parameters.accelerator,
            "formats": list(parameters.formats),
        }
    elif isinstance(parameters, protocol.TtsCapabilityParameters):
        raw_parameters = {
            "engine": parameters.engine,
            "languages": list(parameters.languages),
            "unit_kinds": list(parameters.unit_kinds),
            "max_bytes": parameters.max_bytes,
            "max_duration_ms": parameters.max_duration_ms,
        }
    else:
        raw_parameters = {
            "max_completion_bytes": parameters.max_completion_bytes,
            "operations": list(parameters.operations),
        }
    return {"id": entry.id, "category": entry.category, "parameters": raw_parameters}


def _enrolled_capability_ids(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        entry["id"] for entry in raw if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    )


@dataclass(frozen=True, slots=True)
class EnrollOutcome:
    worker_id: str
    worker_token: str
    environment: str


@dataclass(frozen=True, slots=True)
class ClaimedLease:
    job_id: str
    attempt: int
    lease_token: str
    lease_expires_at: datetime
    deadline_at: datetime | None
    capability_id: str
    payload_base64: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class HeartbeatOutcome:
    lease_expires_at: datetime
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class WorkerSummary:
    id: str
    name: str
    software_version: str
    capabilities_json: list[dict[str, Any]]
    presence_json: dict[str, Any]
    last_seen_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AilocalsWorkerService:
    """Facade over the durable universal-worker rows."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: ServiceSettings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self.payload_builder: PayloadBuilder | None = None

    @property
    def session_factory(self) -> SessionFactory:
        return self._session_factory

    # ------------------------------------------------------------------
    # Owner surface
    # ------------------------------------------------------------------

    def create_enrollment(self) -> tuple[str, datetime]:
        """Create one single-use enrollment token valid for 30 minutes."""

        token = protocol.new_worker_token()
        now = protocol.utc_now()
        expires_at = now + protocol.ENROLLMENT_LIFETIME
        with self._session_factory() as session, session.begin():
            session.add(
                AilocalsEnrollment(
                    id=str(uuid.uuid4()),
                    token_hash=protocol.token_hash(token),
                    created_at=now,
                    expires_at=expires_at,
                )
            )
        return token, expires_at

    def list_workers(self) -> list[WorkerSummary]:
        with self._session_factory() as session:
            rows = session.scalars(select(AilocalsWorker).order_by(AilocalsWorker.created_at)).all()
            return [
                WorkerSummary(
                    id=row.id,
                    name=row.name,
                    software_version=row.software_version,
                    capabilities_json=cast("list[dict[str, Any]]", row.capabilities_json or []),
                    presence_json=dict(row.presence_json or {}),
                    last_seen_at=row.last_seen_at,
                    revoked_at=row.revoked_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    def revoke(self, worker_id: str) -> None:
        """Revoke a worker immediately and make its owned leases terminal."""

        now = protocol.utc_now()
        with self._session_factory() as session, session.begin():
            worker = session.get(AilocalsWorker, worker_id)
            if worker is None:
                raise AilocalsError(ErrorCode.UNAUTHORIZED, "worker is not enrolled")
            worker.revoked_at = now
            worker.updated_at = now
            session.execute(
                update(AilocalsJob)
                .where(
                    AilocalsJob.worker_id == worker_id,
                    AilocalsJob.state.in_((*ACTIVE_LEASE_STATES, "queued")),
                )
                .values(state="canceled", cancel_requested=True, updated_at=now)
            )

    # ------------------------------------------------------------------
    # Worker surface
    # ------------------------------------------------------------------

    def enroll(
        self,
        token: str,
        worker_name: str,
        software_version: str,
        capabilities: tuple[CapabilityEntry, ...],
    ) -> EnrollOutcome:
        if not token or not token.strip():
            raise AilocalsError(ErrorCode.UNAUTHORIZED, "enrollment token is required")
        now = protocol.utc_now()
        stored_entries = [capability_entry_to_json(entry) for entry in capabilities]
        with self._session_factory() as session, session.begin():
            active_count = session.execute(
                select(func.count())
                .select_from(AilocalsWorker)
                .where(AilocalsWorker.revoked_at.is_(None))
            ).scalar_one()
            if active_count:
                raise AilocalsError(
                    ErrorCode.CLIENT_ALREADY_ENROLLED,
                    "a universal worker is already enrolled; revoke it before re-enrolling",
                )
            consumed = cast(
                Any,
                session.execute(
                    update(AilocalsEnrollment)
                    .where(
                        AilocalsEnrollment.token_hash == protocol.token_hash(token),
                        AilocalsEnrollment.used_at.is_(None),
                        AilocalsEnrollment.expires_at > now,
                    )
                    .values(used_at=now)
                ),
            )
            if consumed.rowcount != 1:
                raise AilocalsError(
                    ErrorCode.ENROLLMENT_INVALID, "enrollment token is invalid or expired"
                )
            worker_token = protocol.new_worker_token()
            worker = AilocalsWorker(
                id=str(uuid.uuid4()),
                token_hash=protocol.token_hash(worker_token),
                name=worker_name,
                software_version=software_version,
                capabilities_json=stored_entries,
                presence_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(worker)
            session.flush()
            worker_id = worker.id
        return EnrollOutcome(
            worker_id=worker_id,
            worker_token=worker_token,
            environment=(self._settings.ailocals_environment),
        )

    def authenticate(self, worker_token: str) -> AilocalsWorker:
        """Return the active worker row for a credential, or raise 401."""

        if not worker_token:
            raise AilocalsError(ErrorCode.UNAUTHORIZED, "worker credential is required")
        with self._session_factory() as session:
            worker = session.scalar(
                select(AilocalsWorker).where(
                    AilocalsWorker.token_hash == protocol.token_hash(worker_token),
                    AilocalsWorker.revoked_at.is_(None),
                )
            )
            if worker is None:
                raise AilocalsError(ErrorCode.UNAUTHORIZED, "worker credential is not valid")
            session.expunge(worker)
            return worker

    def presence(self, worker: AilocalsWorker, entries: tuple[PresenceEntry, ...]) -> datetime:
        """Record the full replacement capability snapshot and last-seen time."""

        enrolled = _enrolled_capability_ids(worker.capabilities_json)
        for entry in entries:
            if entry.id not in enrolled:
                raise AilocalsError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "presence advertises a capability outside the enrollment",
                )
        now = protocol.utc_now()
        snapshot = {
            "capabilities": [
                {
                    "id": entry.id,
                    "state": entry.state.value,
                    "accepting": entry.accepting,
                    "active_jobs": entry.active_jobs,
                    "reason": entry.reason,
                }
                for entry in entries
            ],
            "server_time": protocol.format_timestamp(now),
        }
        with self._session_factory() as session, session.begin():
            updated = cast(
                Any,
                session.execute(
                    update(AilocalsWorker)
                    .where(
                        AilocalsWorker.id == worker.id,
                        AilocalsWorker.revoked_at.is_(None),
                    )
                    .values(presence_json=snapshot, last_seen_at=now, updated_at=now)
                ),
            )
            if updated.rowcount != 1:
                raise AilocalsError(ErrorCode.UNAUTHORIZED, "worker credential is not valid")
        return now

    def claim(self, worker: AilocalsWorker, request: LeaseRequestData) -> ClaimedLease | None:
        """Claim at most one queued row, waiting up to wait_seconds."""

        enrolled = _enrolled_capability_ids(worker.capabilities_json)
        if request.capability_id not in enrolled or request.capability_id != CAPABILITY_ACE:
            raise AilocalsError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "capability is outside this connection's enrollment",
            )
        wait_deadline = time.monotonic() + request.wait_seconds
        if self._has_active_lease(worker.id):
            raise AilocalsError(
                ErrorCode.WORKER_BUSY,
                "a second active lease for the same capability is not allowed",
            )
        while True:
            lease = self._try_claim(worker, request.capability_id)
            if lease is not None:
                return lease
            if time.monotonic() >= wait_deadline:
                return None
            time.sleep(CLAIM_POLL_INTERVAL_SECONDS)

    def _has_active_lease(self, worker_id: str) -> bool:
        from sqlalchemy import exists as _exists

        with self._session_factory() as session:
            present = session.scalar(
                select(
                    _exists(
                        select(AilocalsJob.id).where(
                            AilocalsJob.worker_id == worker_id,
                            AilocalsJob.state.in_(ACTIVE_LEASE_STATES),
                        )
                    )
                )
            )
        return bool(present)

    def _try_claim(self, worker: AilocalsWorker, capability_id: str) -> ClaimedLease | None:
        now = protocol.utc_now()
        lease_token = protocol.new_worker_token()
        with self._session_factory() as session, session.begin():
            candidate_id = session.scalar(
                select(AilocalsJob.id)
                .where(
                    AilocalsJob.state == "queued",
                    AilocalsJob.cancel_requested.is_(False),
                    AilocalsJob.queue_deadline_at > now,
                )
                .order_by(AilocalsJob.created_at)
                .limit(1)
            )
            if candidate_id is None:
                return None
            candidate = session.get(AilocalsJob, candidate_id)
            if candidate is None:
                return None
            deadline_at = now + timedelta(milliseconds=candidate.execution_timeout_ms)
            required_lifetime = (deadline_at - now).total_seconds() + UPLOAD_GRACE_SECONDS
            if required_lifetime > self._settings.transfer_token_ttl_seconds:
                raise AilocalsError(
                    ErrorCode.INTERNAL_ERROR,
                    "transfer capability lifetime is shorter than the execution window",
                )
            busy = cast(
                Any,
                session.execute(
                    update(AilocalsJob)
                    .where(
                        AilocalsJob.id == candidate_id,
                        AilocalsJob.state == "queued",
                        AilocalsJob.cancel_requested.is_(False),
                        ~exists(
                            select(AilocalsJob.id).where(
                                AilocalsJob.worker_id == worker.id,
                                AilocalsJob.state.in_(ACTIVE_LEASE_STATES),
                            )
                        ),
                    )
                    .values(
                        state="leased",
                        worker_id=worker.id,
                        attempt=1,
                        lease_token_hash=protocol.token_hash(lease_token),
                        lease_expires_at=now + timedelta(seconds=protocol.LEASE_SECONDS),
                        deadline_at=deadline_at,
                        updated_at=now,
                    )
                ),
            )
            if busy.rowcount != 1:
                return None
            if not self._product_attempt_current(session, candidate_id):
                session.execute(
                    update(AilocalsJob)
                    .where(AilocalsJob.id == candidate_id)
                    .values(
                        state="canceled",
                        cancel_requested=True,
                        error_code=IDENTITY_MISMATCH_CODE,
                        updated_at=protocol.utc_now(),
                    )
                )
                return None
            payload = self._reconstruct_claim_payload(session, candidate_id, deadline_at)
            identity = protocol.canonical_request_identity(self._claim_identity_input(payload))
            stored_identity = session.scalar(
                select(AilocalsJob.request_identity_sha256).where(AilocalsJob.id == candidate_id)
            )
            if stored_identity != identity:
                session.execute(
                    update(AilocalsJob)
                    .where(AilocalsJob.id == candidate_id)
                    .values(
                        state="canceled",
                        cancel_requested=True,
                        error_code=IDENTITY_MISMATCH_CODE,
                        updated_at=protocol.utc_now(),
                    )
                )
                return None
            payload_base64, payload_sha256 = protocol.encode_lease_payload(payload)
        lease_expires_at = now + timedelta(seconds=protocol.LEASE_SECONDS)
        return ClaimedLease(
            job_id=candidate_id,
            attempt=1,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            deadline_at=deadline_at,
            capability_id=capability_id,
            payload_base64=payload_base64,
            payload_sha256=payload_sha256,
        )

    @staticmethod
    def _claim_identity_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Identity covers the worker input without transfer URLs."""

        raw = payload.get("input")
        if isinstance(raw, Mapping):
            return raw
        return payload

    def _reconstruct_claim_payload(
        self, session: Session, job_row_id: str, deadline_at: datetime
    ) -> dict[str, Any]:
        """Rebuild the worker envelope and issue fresh transfer capabilities."""

        if self.payload_builder is None:
            raise AilocalsError(ErrorCode.INTERNAL_ERROR, "payload builder is not configured")
        row = session.get(AilocalsJob, job_row_id)
        if row is None:
            raise AilocalsError(ErrorCode.INTERNAL_ERROR, "queue row disappeared")
        job = session.get(Job, row.application_job_id)
        if job is None:
            raise AilocalsError(ErrorCode.INTERNAL_ERROR, "product job is missing")
        attempt = session.scalar(
            select(VariationAttempt).where(
                VariationAttempt.job_id == job.id,
                VariationAttempt.variation_index == row.variation_index,
                VariationAttempt.submission_nonce == row.submission_nonce,
            )
        )
        if attempt is None:
            raise AilocalsError(ErrorCode.INTERNAL_ERROR, "product attempt is missing")
        worker_payload = dict(self.payload_builder(job, attempt))
        worker_payload["submission_nonce"] = row.submission_nonce
        expires_at = deadline_at + timedelta(seconds=UPLOAD_GRACE_SECONDS)
        output_issued = issue_transfer_url(
            session,
            self._settings,
            job_id=job.id,
            direction=TransferDirection.OUTPUT_UPLOAD,
            expected_relative_path=_output_relative_path(job, row.variation_index),
            expected_extension=job.output_format.value,
            max_bytes=self._settings.transfer_max_output_bytes,
            expires_at=expires_at,
        )
        worker_payload["result_upload"] = {
            "url": output_issued.url,
            "max_bytes": output_issued.capability.max_bytes,
        }
        source_capability: TransferCapability | None = None
        if job.job_type.value == "cover":
            if not job.source_sha256 or not job.source_byte_size:
                raise AilocalsError(ErrorCode.INTERNAL_ERROR, "cover source metadata is incomplete")
            source_issued = issue_transfer_url(
                session,
                self._settings,
                job_id=job.id,
                direction=TransferDirection.SOURCE_DOWNLOAD,
                expected_relative_path=f"{job.id}/source.mp3",
                expected_extension=".mp3",
                max_bytes=job.source_byte_size,
                expires_at=expires_at,
            )
            worker_payload["source"] = {
                "url": source_issued.url,
                "sha256": job.source_sha256,
                "bytes": job.source_byte_size,
                "format": "mp3",
            }
            source_capability = source_issued.capability
        output_capability = output_issued.capability
        output_capability.ailocals_job_id = row.id
        output_capability.submission_nonce = row.submission_nonce
        if source_capability is not None:
            source_capability.ailocals_job_id = row.id
            source_capability.submission_nonce = row.submission_nonce
        session.flush()
        envelope = {
            "schema_version": 2,
            "application_job_id": job.id,
            "variation_index": row.variation_index,
            "submission_nonce": row.submission_nonce,
            "input": worker_payload,
            "source": worker_payload.get("source"),
            "result_upload": dict(worker_payload["result_upload"]),
        }
        return envelope

    @staticmethod
    def _product_attempt_current(session: Session, job_row_id: str) -> bool:
        row = session.get(AilocalsJob, job_row_id)
        if row is None:
            return False
        attempt = session.scalar(
            select(VariationAttempt).where(
                VariationAttempt.job_id == row.application_job_id,
                VariationAttempt.variation_index == row.variation_index,
                VariationAttempt.submission_nonce == row.submission_nonce,
            )
        )
        if attempt is None:
            return False
        if attempt.status in {
            JobStatus.CANCELLED,
            JobStatus.FAILED,
            JobStatus.COMPLETED,
        }:
            return False
        return True

    def heartbeat(
        self,
        worker: AilocalsWorker,
        job_id: str,
        lease_token: str,
        attempt: int,
        progress_percent: int,
    ) -> HeartbeatOutcome:
        """Renew a live lease and surface cancellation requests."""

        del progress_percent
        now = protocol.utc_now()
        with self._session_factory() as session, session.begin():
            row = self._require_live_lease(
                session.get(AilocalsJob, job_id), worker.id, lease_token, attempt, now
            )
            cancel_requested = bool(row.cancel_requested) or not self._product_attempt_current(
                session, job_id
            )
            new_expiry = now + timedelta(seconds=protocol.LEASE_SECONDS)
            if row.deadline_at is not None:
                new_expiry = min(new_expiry, row.deadline_at)
            session.execute(
                update(AilocalsJob)
                .where(AilocalsJob.id == job_id)
                .values(
                    state="running",
                    lease_expires_at=new_expiry,
                    cancel_requested=cancel_requested,
                    updated_at=now,
                )
            )
        return HeartbeatOutcome(lease_expires_at=new_expiry, cancel_requested=cancel_requested)

    def fail(
        self,
        worker: AilocalsWorker,
        job_id: str,
        lease_token: str,
        attempt: int,
        code: str,
        retryable: bool,
    ) -> None:
        """Record a terminal execution failure; success is never overwritten."""

        del retryable
        now = protocol.utc_now()
        error_code = (
            "ailocals_canceled" if code == protocol.FailureCode.CANCELED else f"ailocals_{code}"
        )
        with self._session_factory() as session, session.begin():
            row = session.get(AilocalsJob, job_id)
            if row is None:
                raise AilocalsError(ErrorCode.LEASE_LOST, "job is not known")
            if row.state == "succeeded":  # noqa: SIM102
                return
            if (
                row.worker_id == worker.id
                and row.attempt == attempt
                and row.state in TERMINAL_STATES
            ):
                # Identical terminal acknowledgement is idempotent; a
                # cancellation already recorded always wins.
                return
            self._require_live_lease(row, worker.id, lease_token, attempt, now)
            state = (
                "canceled"
                if code == protocol.FailureCode.CANCELED or row.cancel_requested
                else "failed"
            )
            session.execute(
                update(AilocalsJob)
                .where(AilocalsJob.id == job_id)
                .values(
                    state=state,
                    error_code=error_code,
                    lease_token_hash=None,
                    updated_at=now,
                )
            )

    def complete(
        self,
        worker: AilocalsWorker,
        job_id: str,
        lease_token: str,
        metadata: protocol.CompleteMetadataData,
        result_bytes: bytes,
    ) -> None:
        """Validate and durably accept worker result metadata."""

        now = protocol.utc_now()
        if len(result_bytes) > protocol.RESULT_MAX_BYTES:
            raise AilocalsError(ErrorCode.PAYLOAD_TOO_LARGE, "result exceeds the byte bound")
        if protocol.sha256_bytes(result_bytes) != metadata.result_sha256:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "result hash does not match")
        parsed = protocol.parse_json(result_bytes)
        metadata_dict = self._validated_result_metadata(parsed)
        with self._session_factory() as session, session.begin():
            row = session.get(AilocalsJob, job_id)
            if row is None:
                raise AilocalsError(ErrorCode.LEASE_LOST, "job is not known")
            if row.state == "succeeded":  # noqa: SIM102
                if (
                    row.worker_id == worker.id
                    and row.attempt == metadata.attempt
                    and row.result_sha256 == metadata.result_sha256
                ):
                    return
                raise AilocalsError(ErrorCode.RESULT_CONFLICT, "result conflicts with acceptance")
            self._require_live_lease(row, worker.id, lease_token, metadata.attempt, now)
            output = self._committed_output(session, row, metadata_dict)
            if output is None:
                raise AilocalsError(
                    ErrorCode.INVALID_REQUEST, "committed transfer output is missing"
                )
            self._validate_completion(session, row, metadata_dict, output)
            session.execute(
                update(AilocalsJob)
                .where(AilocalsJob.id == job_id)
                .values(
                    state="succeeded",
                    result_json=metadata_dict,
                    result_sha256=metadata.result_sha256,
                    lease_token_hash=None,
                    updated_at=now,
                )
            )

    @staticmethod
    def _validated_result_metadata(parsed: Any) -> dict[str, Any]:
        from ace_service.schemas import validate_worker_result_metadata

        try:
            validated = validate_worker_result_metadata(parsed, expected_schema_version=2)
        except ValueError as exc:
            raise AilocalsError(ErrorCode.INVALID_REQUEST, "result metadata is invalid") from exc
        return dict(validated)

    @staticmethod
    def _committed_output(
        session: Session, row: AilocalsJob, metadata: Mapping[str, Any]
    ) -> Output | None:
        output_records = session.scalars(
            select(Output)
            .where(
                Output.job_id == row.application_job_id,
                Output.variation_index == row.variation_index,
            )
            .order_by(Output.result_index)
        ).all()
        if not output_records:
            return None
        output_section = metadata.get("output")
        if not isinstance(output_section, Mapping):
            return None
        expected_sha = output_section.get("sha256")
        expected_bytes = output_section.get("bytes")
        for record in output_records:
            if record.sha256 == expected_sha and record.byte_size == expected_bytes:
                return record
        return None

    @staticmethod
    def _validate_completion(
        session: Session,
        row: AilocalsJob,
        metadata: Mapping[str, Any],
        output: Output | None,
    ) -> None:
        from ace_service.worker import _validate_completion_metadata

        job = session.get(Job, row.application_job_id)
        attempt = session.scalar(
            select(VariationAttempt).where(
                VariationAttempt.job_id == row.application_job_id,
                VariationAttempt.variation_index == row.variation_index,
                VariationAttempt.submission_nonce == row.submission_nonce,
            )
        )
        if job is None or attempt is None:
            raise AilocalsError(ErrorCode.LEASE_LOST, "product attempt is missing")
        try:
            _validate_completion_metadata(metadata, job, attempt, output)
        except ValueError as exc:
            raise AilocalsError(
                ErrorCode.INVALID_REQUEST, "completion evidence does not match the job"
            ) from exc

    def reap_expired_leases(self) -> int:
        """Make expired leases terminal without regenerating inference."""

        now = protocol.utc_now()
        with self._session_factory() as session, session.begin():
            expired_ids = session.scalars(
                select(AilocalsJob.id).where(
                    AilocalsJob.state.in_(ACTIVE_LEASE_STATES),
                    AilocalsJob.lease_expires_at.is_not(None),
                    AilocalsJob.lease_expires_at < now,
                )
            ).all()
            if not expired_ids:
                return 0
            session.execute(
                update(AilocalsJob)
                .where(AilocalsJob.id.in_(expired_ids))
                .values(state="failed", error_code=WORKER_LOST_CODE, updated_at=now)
            )
            return len(expired_ids)

    # ------------------------------------------------------------------

    @staticmethod
    def _require_live_lease(
        row: AilocalsJob | None,
        worker_id: str,
        lease_token: str,
        attempt: int,
        now: datetime,
    ) -> AilocalsJob:
        if (
            row is None
            or row.worker_id != worker_id
            or row.state not in ACTIVE_LEASE_STATES
            or row.attempt != attempt
            or row.lease_token_hash is None
            or not protocol_sha_matches(row.lease_token_hash, lease_token)
        ):
            raise AilocalsError(ErrorCode.LEASE_LOST, "lease is no longer valid")
        if row.lease_expires_at is None or row.lease_expires_at <= now:
            raise AilocalsError(ErrorCode.LEASE_LOST, "lease is no longer valid")
        return row


def protocol_sha_matches(stored_hash: str, token: str) -> bool:
    return protocol.token_hash(token) == stored_hash


def _output_relative_path(job: Job, variation_index: int) -> str:
    from ace_service.worker import ControllerWorker

    return ControllerWorker._output_relative_path(job, variation_index)

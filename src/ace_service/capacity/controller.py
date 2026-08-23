"""Durable keep-warm reconciliation and provider-independent lease timing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.models import CapacityLease, Job, JobStatus, JobType, utc_now
from ace_service.providers.base import BackendId, ProviderName
from ace_service.repository import (
    acquire_capacity_action_lease,
    ensure_capacity_lease,
    get_keep_warm_seconds,
    insert_notification_event,
    mark_capacity_action_started,
    update_capacity_lease_if_owner,
)

from .base import CapacityError, CapacityErrorKind, CapacityManager, CapacityPhase, CapacitySnapshot
from .registry import CapacityRegistry

LOGGER = logging.getLogger(__name__)
RECONCILE_INTERVAL_SECONDS = 15
REMINDER_INTERVAL_SECONDS = 5 * 60
RELEASE_WARNING_SECONDS = 60


@dataclass(frozen=True, slots=True)
class CapacityReconcileResult:
    capacity_key: str
    state: str
    configured_floor: int
    observed_instances: int
    provider_active_jobs: int
    error: str | None = None


class _CapacityActionLost(RuntimeError):
    """The provider action was fenced by a newer controller or watchdog."""


def _is_confirmed_cover(job: Job) -> bool:
    if job.job_type is not JobType.COVER:
        return True
    value = job.normalized_request_json or {}
    staging = value.get("cover_staging") if isinstance(value, dict) else None
    return isinstance(staging, dict) and staging.get("status") == "confirmed"


def count_eligible_work(session: Any, backend_ids: frozenset[BackendId]) -> int:
    """Count generation-ready work without treating page presence as activity."""

    ids = [str(value) for value in backend_ids]
    if not ids:
        return 0
    jobs = session.scalars(
        select(Job).where(
            Job.inference_backend.in_(ids),
            Job.status.in_(
                (JobStatus.QUEUED, JobStatus.STAGING, JobStatus.CLOUD_QUEUED, JobStatus.GENERATING)
            ),
        )
    )
    return sum(1 for job in jobs if _is_confirmed_cover(job))


def latest_terminal_activity(session: Any, backend_ids: frozenset[BackendId]) -> datetime | None:
    ids = [str(value) for value in backend_ids]
    if not ids:
        return None
    jobs = session.scalars(
        select(Job).where(
            Job.inference_backend.in_(ids),
            Job.status.in_((JobStatus.COMPLETED, JobStatus.FAILED)),
            Job.completed_at.is_not(None),
        )
    )
    values = [job.completed_at for job in jobs if job.completed_at is not None]
    return max(values) if values else None


def _provider_label(provider: ProviderName) -> str:
    return "Salad" if provider is ProviderName.SALAD else "RunPod"


class CapacityController:
    """One primary 15-second reconciler; safe to invoke as a one-shot worker."""

    def __init__(
        self,
        settings: ServiceSettings,
        session_factory: SessionFactory,
        registry: CapacityRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
        owner: str = "controller",
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.registry = registry
        self.clock = clock
        self.owner = owner
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self.run(), name="ace-capacity-controller")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        while True:
            await self.reconcile_once()
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    async def ensure_retained(self, backend_id: BackendId | str) -> None:
        """Strictly retain exactly one worker before an inference nonce is committed."""

        manager = self.registry.for_backend(backend_id)
        if manager is None:
            return
        with self.session_factory() as session:
            keep_warm = get_keep_warm_seconds(session)
        if keep_warm == 0:
            return
        try:
            before = await manager.inspect()
            if before.configured_maximum != 1:
                raise CapacityError(
                    CapacityErrorKind.DRIFT, "retain", "capacity maximum is not one"
                )
            if before.configured_floor == 1:
                now = self.clock().astimezone(UTC)
                with self.session_factory() as session:
                    lease = ensure_capacity_lease(session, manager.key, manager.provider, now=now)
                    new_session = lease.state == "cold" or lease.session_id is None
                    if new_session:
                        lease.session_id = str(uuid4())
                        lease.warmed_at = None
                        lease.next_reminder_at = None
                    if before.phase is CapacityPhase.READY and lease.warmed_at is None:
                        lease.warmed_at = now
                        lease.next_reminder_at = now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                    lease.state = "retained" if before.phase is CapacityPhase.READY else "warming"
                    lease.released_at = None
                    lease.release_requested_at = None
                    lease.last_error_code = None
                    lease.updated_at = now
                    session.commit()
                return
            action = await self._provider_action(manager, "retain", before)
            if action is None:
                raise CapacityError(
                    CapacityErrorKind.TRANSIENT, "retain", "capacity action is already owned"
                )
            after, owner, token, now = action
            if after.configured_floor != 1:
                raise CapacityError(
                    CapacityErrorKind.INVALID_RESPONSE, "retain", "capacity floor was not retained"
                )
            with self.session_factory() as session:
                lease = ensure_capacity_lease(session, manager.key, manager.provider, now=now)
                new_session = (
                    lease.state == "cold"
                    or lease.session_id is None
                    or before.observed_instances == 0
                )
                session_id = str(uuid4()) if new_session else lease.session_id
                warmed_at = None if new_session else lease.warmed_at
                next_reminder_at = None if new_session else lease.next_reminder_at
                if after.phase is CapacityPhase.READY and warmed_at is None:
                    warmed_at = now
                    next_reminder_at = now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                updated = update_capacity_lease_if_owner(
                    session,
                    manager.key,
                    owner=owner,
                    fencing_token=token,
                    now=now,
                    state="retained" if after.phase is CapacityPhase.READY else "warming",
                    session_id=session_id,
                    warmed_at=warmed_at,
                    next_reminder_at=next_reminder_at,
                    released_at=None,
                    release_requested_at=None,
                    last_error_code=None,
                )
                session.commit()
            if updated is None:
                raise CapacityError(
                    CapacityErrorKind.TRANSIENT, "retain", "capacity action became stale"
                )
        except CapacityError:
            raise
        except Exception as exc:
            raise CapacityError(
                CapacityErrorKind.TRANSIENT, "retain", "capacity retain failed"
            ) from exc

    async def reconcile_once(self) -> tuple[CapacityReconcileResult, ...]:
        results: list[CapacityReconcileResult] = []
        for manager in self.registry.managers:
            results.append(await self._reconcile_manager(manager))
        return tuple(results)

    async def _reconcile_manager(self, manager: CapacityManager) -> CapacityReconcileResult:
        now = self.clock().astimezone(UTC)
        with self.session_factory() as session:
            lease = ensure_capacity_lease(session, manager.key, manager.provider, now=now)
            # A successful fresh inspection resolves any prior inspection
            # failure. Later branches may set a new active-work error or keep
            # the independent release_overdue state.
            lease.last_error_code = None
            keep_warm = get_keep_warm_seconds(session)
            active = count_eligible_work(session, manager.backend_ids)
            if active:
                lease.last_activity_at = now
                lease.release_due_at = None
                lease.idle_epoch_id = None
                lease.last_error_code = None
                session.commit()
            else:
                if lease.last_activity_at is None:
                    lease.last_activity_at = (
                        latest_terminal_activity(session, manager.backend_ids) or now
                    )
                terminal_activity = latest_terminal_activity(session, manager.backend_ids)
                if terminal_activity is not None and terminal_activity > lease.last_activity_at:
                    lease.last_activity_at = terminal_activity
                if lease.idle_epoch_id is None and lease.state != "cold":
                    lease.idle_epoch_id = str(uuid4())
                    reminder_base = lease.warmed_at or now
                    lease.next_reminder_at = reminder_base + timedelta(
                        seconds=REMINDER_INTERVAL_SECONDS
                    )
                if lease.idle_epoch_id is not None:
                    lease.release_due_at = lease.last_activity_at + timedelta(seconds=keep_warm)
                session.commit()

        try:
            before = await manager.inspect()
        except CapacityError as exc:
            return self._record_error(manager, exc, now)
        except Exception:
            return self._record_error(
                manager,
                CapacityError(CapacityErrorKind.TRANSIENT, "inspect", "capacity inspection failed"),
                now,
            )

        with self.session_factory() as session:
            lease = ensure_capacity_lease(session, manager.key, manager.provider, now=now)
            keep_warm = get_keep_warm_seconds(session)
            active = count_eligible_work(session, manager.backend_ids)
            if active and keep_warm and before.configured_floor != 1:
                session.commit()
                try:
                    action = await self._provider_action(manager, "retain", before, now=now)
                except CapacityError as exc:
                    return self._record_error(manager, exc, now)
                if action is not None:
                    after, owner, token, action_now = action
                    session.refresh(lease)
                    new_session = (
                        lease.state == "cold"
                        or lease.session_id is None
                        or before.observed_instances == 0
                    )
                    session_id = str(uuid4()) if new_session else lease.session_id
                    warmed_at = None if new_session else lease.warmed_at
                    next_reminder_at = None if new_session else lease.next_reminder_at
                    if after.phase is CapacityPhase.READY and warmed_at is None:
                        warmed_at = action_now
                        next_reminder_at = action_now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                    updated = update_capacity_lease_if_owner(
                        session,
                        manager.key,
                        owner=owner,
                        fencing_token=token,
                        now=action_now,
                        state="retained" if after.phase is CapacityPhase.READY else "warming",
                        session_id=session_id,
                        warmed_at=warmed_at,
                        next_reminder_at=next_reminder_at,
                        released_at=None,
                        release_requested_at=None,
                        last_error_code=None,
                    )
                    if updated is None:
                        session.rollback()
                        return self._current_result(manager, before)
                    session.refresh(lease)
            elif active:
                if before.configured_floor == 1:
                    new_session = lease.state == "cold" or lease.session_id is None
                    if new_session:
                        lease.session_id = str(uuid4())
                        lease.warmed_at = None
                        lease.next_reminder_at = None
                    lease.state = "retained" if before.phase is CapacityPhase.READY else "warming"
                    if before.phase is CapacityPhase.READY and lease.warmed_at is None:
                        lease.warmed_at = now
                        lease.next_reminder_at = now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                else:
                    lease.state = "cold"
            elif lease.state == "cold" and before.configured_floor == 1:
                lease.session_id = str(uuid4())
                lease.warmed_at = None
                lease.next_reminder_at = None
                lease.state = "idle" if before.phase is CapacityPhase.READY else "warming"
                if before.phase is CapacityPhase.READY:
                    lease.warmed_at = now
                    lease.next_reminder_at = now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                lease.idle_epoch_id = lease.idle_epoch_id or str(uuid4())
                lease.release_due_at = (lease.last_activity_at or now) + timedelta(
                    seconds=keep_warm
                )
                try:
                    lease = await self._idle_progress(session, lease, before, keep_warm, now)
                except _CapacityActionLost:
                    session.rollback()
                    return self._current_result(manager, before)
            elif lease.state != "cold":
                try:
                    lease = await self._idle_progress(session, lease, before, keep_warm, now)
                except _CapacityActionLost:
                    session.rollback()
                    return self._current_result(manager, before)
                except CapacityError as exc:
                    session.rollback()
                    return self._record_error(manager, exc, now)
            lease.last_reconciled_at = now
            lease.updated_at = now
            session.commit()
            return CapacityReconcileResult(
                manager.key,
                lease.state,
                before.configured_floor,
                before.observed_instances,
                before.provider_active_jobs,
                lease.last_error_code,
            )

    async def _idle_progress(
        self,
        session: Any,
        lease: CapacityLease,
        snapshot: CapacitySnapshot,
        keep_warm: int,
        now: datetime,
    ) -> CapacityLease:
        if lease.idle_epoch_id is None:
            lease.idle_epoch_id = str(uuid4())
            lease.release_due_at = (lease.last_activity_at or now) + timedelta(seconds=keep_warm)
        due = lease.release_due_at or now
        if snapshot.phase is CapacityPhase.READY:
            lease.state = "idle"
            if lease.warmed_at is None:
                lease.warmed_at = now
                lease.next_reminder_at = now + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
            slot = max(0, int((now - lease.warmed_at).total_seconds()) // REMINDER_INTERVAL_SECONDS)
            next_reminder_at = lease.next_reminder_at
            if next_reminder_at is None:
                next_reminder_at = lease.warmed_at + timedelta(seconds=REMINDER_INTERVAL_SECONDS)
                lease.next_reminder_at = next_reminder_at
            if next_reminder_at <= now:
                insert_notification_event(
                    session,
                    event_key=f"retained-reminder:{lease.capacity_key}:{lease.session_id}:{slot}",
                    kind="capacity_retained_reminder",
                    title="GPU kept warm",
                    body=(
                        "AudioVentura has kept one "
                        f"{_provider_label(snapshot.provider)} worker ready "
                        f"for {slot * 5} minutes."
                    ),
                    target_path="/",
                    provider=snapshot.provider,
                    capacity_key=lease.capacity_key,
                    created_at=now,
                )
                lease.next_reminder_at = lease.warmed_at + timedelta(minutes=(slot + 1) * 5)
        if keep_warm and now < due and due - now <= timedelta(seconds=RELEASE_WARNING_SECONDS):
            insert_notification_event(
                session,
                event_key=f"release-warning:{lease.capacity_key}:{lease.idle_epoch_id}",
                kind="capacity_release_warning",
                title="GPU release in 1 minute",
                body=(
                    "No generation is active. The warm "
                    f"{_provider_label(snapshot.provider)} worker "
                    "will be released in 1 minute."
                ),
                target_path="/",
                provider=snapshot.provider,
                capacity_key=lease.capacity_key,
                created_at=now,
            )
        if now < due:
            return lease
        if snapshot.provider_active_jobs or snapshot.observed_instances > 1:
            lease.last_error_code = "provider_active_work"
            return lease
        if snapshot.configured_floor != 0:
            manager = self.registry.for_key(lease.capacity_key)
            if manager is None:
                raise CapacityError(
                    CapacityErrorKind.INVALID_RESPONSE, "release", "capacity manager disappeared"
                )
            session.commit()
            action = await self._provider_action(manager, "release", snapshot, now=now)
            if action is None:
                session.rollback()
                raise _CapacityActionLost
            after, owner, token, action_now = action
            session.refresh(lease)
            if lease.action_owner != owner or lease.fencing_token != token:
                session.rollback()
                raise _CapacityActionLost
            lease.release_requested_at = lease.release_requested_at or action_now
            lease.state = "releasing"
            if (
                after.configured_floor == 0
                and not after.provider_active_jobs
                and not after.observed_instances
            ):
                self._mark_cold(session, lease, after, action_now)
            elif lease.release_requested_at and (
                action_now - lease.release_requested_at
                >= timedelta(seconds=getattr(manager, "release_grace_seconds", 300))
            ):
                lease.state = "release_overdue"
                insert_notification_event(
                    session,
                    event_key=(
                        f"release-overdue:{lease.capacity_key}:{lease.idle_epoch_id}:"
                        f"{int((action_now - lease.release_requested_at).total_seconds()) // 300}"
                    ),
                    kind="capacity_release_overdue",
                    title="GPU release needs attention",
                    body="AudioVentura requested release, but zero workers has not been confirmed.",
                    target_path="/",
                    provider=snapshot.provider,
                    capacity_key=lease.capacity_key,
                    created_at=action_now,
                )
            if (
                update_capacity_lease_if_owner(
                    session,
                    manager.key,
                    owner=owner,
                    fencing_token=token,
                    now=action_now,
                )
                is None
            ):
                session.rollback()
                raise _CapacityActionLost
            return lease
        if snapshot.provider_active_jobs == 0 and snapshot.observed_instances == 0:
            return self._mark_cold(session, lease, snapshot, now)
        lease.state = "releasing"
        if lease.release_requested_at and now - lease.release_requested_at >= timedelta(
            seconds=getattr(self.registry.for_key(lease.capacity_key), "release_grace_seconds", 300)
        ):
            lease.state = "release_overdue"
            insert_notification_event(
                session,
                event_key=(
                    f"release-overdue:{lease.capacity_key}:{lease.idle_epoch_id}:"
                    f"{int((now - lease.release_requested_at).total_seconds()) // 300}"
                ),
                kind="capacity_release_overdue",
                title="GPU release needs attention",
                body="AudioVentura requested release, but zero workers has not been confirmed.",
                target_path="/",
                provider=snapshot.provider,
                capacity_key=lease.capacity_key,
                created_at=now,
            )
        return lease

    def _current_result(
        self, manager: CapacityManager, snapshot: CapacitySnapshot
    ) -> CapacityReconcileResult:
        with self.session_factory() as session:
            lease = ensure_capacity_lease(session, manager.key, manager.provider)
            return CapacityReconcileResult(
                manager.key,
                lease.state,
                snapshot.configured_floor,
                snapshot.observed_instances,
                snapshot.provider_active_jobs,
                lease.last_error_code,
            )

    async def _provider_action(
        self,
        manager: CapacityManager,
        operation: str,
        snapshot: CapacitySnapshot,
        *,
        now: datetime | None = None,
    ) -> tuple[CapacitySnapshot, str, int, datetime] | None:
        """Run one provider mutation outside SQL and fence its result."""

        action_now = (now or self.clock()).astimezone(UTC)
        owner = f"{self.owner[:90]}:{uuid4()}"
        with self.session_factory() as session:
            ensure_capacity_lease(session, manager.key, manager.provider, now=action_now)
            token = acquire_capacity_action_lease(
                session, manager.key, owner, now=action_now, ttl_seconds=300
            )
            session.commit()
        if token is None:
            return None
        if operation == "release":
            with self.session_factory() as session:
                marked = mark_capacity_action_started(
                    session,
                    manager.key,
                    owner=owner,
                    fencing_token=token,
                    operation=operation,
                    now=action_now,
                )
                if marked is None:
                    session.rollback()
                    return None
                session.commit()
        try:
            if operation == "retain":
                after = await manager.retain_one(snapshot)
            elif operation == "release":
                after = await manager.release_one(snapshot)
            else:
                raise CapacityError(
                    CapacityErrorKind.INVALID_RESPONSE, operation, "unknown capacity action"
                )
            if operation == "retain" and after.configured_floor != 1:
                raise CapacityError(
                    CapacityErrorKind.INVALID_RESPONSE,
                    operation,
                    "capacity floor was not retained",
                )
        except CapacityError as exc:
            with self.session_factory() as session:
                update_capacity_lease_if_owner(
                    session,
                    manager.key,
                    owner=owner,
                    fencing_token=token,
                    now=action_now,
                    last_error_code=exc.kind.value,
                )
                session.commit()
            raise
        except Exception as exc:
            error = CapacityError(
                CapacityErrorKind.TRANSIENT, operation, "capacity provider action failed"
            )
            with self.session_factory() as session:
                update_capacity_lease_if_owner(
                    session,
                    manager.key,
                    owner=owner,
                    fencing_token=token,
                    now=action_now,
                    last_error_code=error.kind.value,
                )
                session.commit()
            raise error from exc
        return after, owner, token, action_now

    def _mark_cold(
        self, session: Any, lease: CapacityLease, snapshot: CapacitySnapshot, now: datetime
    ) -> CapacityLease:
        lease.state = "cold"
        lease.released_at = now
        lease.release_due_at = None
        lease.release_requested_at = None
        lease.warmed_at = None
        lease.next_reminder_at = None
        lease.last_error_code = None
        if lease.idle_epoch_id:
            insert_notification_event(
                session,
                event_key=f"released:{lease.capacity_key}:{lease.idle_epoch_id}",
                kind="capacity_released",
                title="GPU released",
                body="The warm worker has been released.",
                target_path="/",
                provider=snapshot.provider,
                capacity_key=lease.capacity_key,
                created_at=now,
            )
        return lease

    def _record_error(
        self, manager: CapacityManager, error: CapacityError, now: datetime
    ) -> CapacityReconcileResult:
        with self.session_factory() as session:
            lease = ensure_capacity_lease(session, manager.key, manager.provider, now=now)
            if (
                lease.action_owner is not None
                and lease.action_lease_expires_at is not None
                and lease.action_lease_expires_at > now
            ):
                return CapacityReconcileResult(
                    manager.key,
                    lease.state,
                    0,
                    0,
                    0,
                    lease.last_error_code,
                )
            lease.last_error_code = error.kind.value
            lease.last_reconciled_at = now
            lease.updated_at = now
            if error.kind is CapacityErrorKind.DRIFT:
                lease.state = "release_overdue" if lease.release_requested_at else lease.state
            session.commit()
            return CapacityReconcileResult(manager.key, lease.state, 0, 0, 0, error.kind.value)

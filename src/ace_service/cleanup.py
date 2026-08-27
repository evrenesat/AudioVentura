"""Bounded, repeatable cleanup for controller files and transfer records."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from ace_service.config import ServiceSettings
from ace_service.cover import remove_cover_source
from ace_service.db import SessionFactory
from ace_service.media_library import MediaLibraryError, MediaLibraryService
from ace_service.models import (
    Job,
    JobStatus,
    JobType,
    NotificationDelivery,
    NotificationEvent,
    PushSubscription,
    TransferCapability,
    TransferStatus,
)
from ace_service.repository import revoke_active_transfers, transition_job

LOGGER = logging.getLogger(__name__)
_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_RETAINABLE_TRANSFER_STATUSES = frozenset(
    {TransferStatus.CONSUMED, TransferStatus.EXPIRED, TransferStatus.REVOKED}
)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """Counts from one cleanup pass, useful for logs and operational checks."""

    stale_part_files: int = 0
    expired_capabilities: int = 0
    revoked_capabilities: int = 0
    deleted_capability_records: int = 0
    removed_cover_sources: int = 0
    expired_cover_staging: int = 0
    deleted_notification_events: int = 0
    deleted_notification_subscriptions: int = 0
    reconciled_media_items: int = 0


def cleanup_controller(
    settings: ServiceSettings,
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
) -> CleanupReport:
    """Run one safe cleanup pass; repeated passes are intentionally idempotent."""

    current_time = _utc(now)
    stale_cutoff = current_time - timedelta(seconds=settings.cleanup_stale_after_seconds)
    retention_cutoff = current_time - timedelta(seconds=settings.transfer_record_retention_seconds)
    stale_part_files = _remove_stale_part_files(settings.paths.root, stale_cutoff)
    reconciled_media_items = 0
    try:
        reconciled_media_items = MediaLibraryService(
            settings, session_factory
        ).reconcile_pending_deletions()
    except MediaLibraryError:
        LOGGER.warning(
            "media deletion reconciliation deferred stage=cleanup",
            extra={"component": "controller"},
        )
    revoked = 0
    expired = 0
    deleted = 0
    terminal_cover_ids: list[str] = []
    expired_staging = 0
    deleted_notification_events = 0
    deleted_notification_subscriptions = 0

    with session_factory() as session:
        jobs = list(session.scalars(select(Job)))
        for job in jobs:
            if (
                job.job_type is JobType.COVER
                and job.status is JobStatus.STAGING
                and job.updated_at <= stale_cutoff
                and not _cover_is_confirmed(job)
            ):
                transition_job(
                    session,
                    job.id,
                    JobStatus.FAILED,
                    now=current_time,
                    error_code="cover_staging_expired",
                    user_facing_error=(
                        "Cover preparation expired before confirmation; submit the cover again."
                    ),
                )
                expired_staging += 1
            if job.status not in _TERMINAL_STATUSES:
                continue
            revoked += len(revoke_active_transfers(session, job.id, now=current_time))
            if job.job_type is JobType.COVER and not settings.retain_cover_source:
                terminal_cover_ids.append(job.id)

        capabilities = list(session.scalars(select(TransferCapability)))
        for capability in capabilities:
            if capability.status is TransferStatus.ISSUED and capability.expires_at <= current_time:
                capability.status = TransferStatus.EXPIRED
                expired += 1
            if (
                capability.status in _RETAINABLE_TRANSFER_STATUSES
                and _capability_timestamp(capability) <= retention_cutoff
            ):
                session.delete(capability)
                deleted += 1
        notification_cutoff = current_time - timedelta(days=30)
        events = list(
            session.scalars(
                select(NotificationEvent).where(NotificationEvent.created_at <= notification_cutoff)
            )
        )
        for event in events:
            deliveries = list(
                session.scalars(
                    select(NotificationDelivery).where(NotificationDelivery.event_id == event.id)
                )
            )
            if not deliveries or all(
                item.status in {"delivered", "abandoned"} for item in deliveries
            ):
                for delivery in deliveries:
                    session.delete(delivery)
                session.delete(event)
                deleted_notification_events += 1
        subscriptions = list(session.scalars(select(PushSubscription)))
        for subscription in subscriptions:
            if subscription.disabled_at is None:
                continue
            delivery_exists = session.scalar(
                select(NotificationDelivery.id)
                .where(NotificationDelivery.subscription_id == subscription.id)
                .limit(1)
            )
            if delivery_exists is None:
                session.delete(subscription)
                deleted_notification_subscriptions += 1
        session.commit()

    removed_sources = 0
    for job_id in terminal_cover_ids:
        before = _source_exists(settings, job_id)
        remove_cover_source(settings, job_id)
        if before and not _source_exists(settings, job_id):
            removed_sources += 1

    report = CleanupReport(
        stale_part_files=stale_part_files,
        expired_capabilities=expired,
        revoked_capabilities=revoked,
        deleted_capability_records=deleted,
        removed_cover_sources=removed_sources,
        expired_cover_staging=expired_staging,
        deleted_notification_events=deleted_notification_events,
        deleted_notification_subscriptions=deleted_notification_subscriptions,
        reconciled_media_items=reconciled_media_items,
    )
    LOGGER.info(
        "cleanup complete stage=cleanup stale_part_files=%d expired_capabilities=%d "
        "revoked_capabilities=%d deleted_capability_records=%d removed_cover_sources=%d "
        "expired_cover_staging=%d",
        report.stale_part_files,
        report.expired_capabilities,
        report.revoked_capabilities,
        report.deleted_capability_records,
        report.removed_cover_sources,
        report.expired_cover_staging,
        extra={"component": "controller"},
    )
    return report


def _remove_stale_part_files(root: Path, cutoff: datetime) -> int:
    removed = 0
    if not root.is_dir():
        return removed
    for candidate in root.rglob("*.part"):
        if candidate.is_dir() or not _inside_root(root, candidate):
            continue
        try:
            if candidate.stat().st_mtime >= cutoff.timestamp():
                continue
            candidate.unlink()
            removed += 1
        except OSError as exc:
            LOGGER.warning(
                "cleanup could not remove stale part file stage=cleanup exception_class=%s",
                type(exc).__name__,
                extra={"component": "controller"},
            )
    return removed


def _capability_timestamp(capability: TransferCapability) -> datetime:
    return capability.consumed_at or capability.revoked_at or capability.expires_at


def _source_exists(settings: ServiceSettings, job_id: str) -> bool:
    path = settings.paths.incoming / job_id / "source.mp3"
    return path.is_file() or path.is_symlink()


def _cover_is_confirmed(job: Job) -> bool:
    normalized = job.normalized_request_json
    if not isinstance(normalized, dict) or normalized.get("schema_version") != 2:
        return True
    staging = normalized.get("cover_staging")
    return isinstance(staging, dict) and staging.get("status") == "confirmed"


def _inside_root(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("cleanup timestamps must be timezone-aware")
    return result.astimezone(UTC)


def cleanup_loop_interval(settings: ServiceSettings) -> float:
    """Expose the configured interval without exposing settings internals to tasks."""

    return float(settings.cleanup_interval_seconds)

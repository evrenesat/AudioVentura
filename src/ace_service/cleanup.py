"""Bounded, repeatable cleanup for controller files and transfer records."""

from __future__ import annotations

import hashlib
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
    AssetTransferCapability,
    AssetTransferPurpose,
    AssetTransferStatus,
    Job,
    JobStatus,
    JobType,
    NotificationDelivery,
    NotificationEvent,
    ProjectDeletionAudit,
    PushSubscription,
    SourceAsset,
    SourceAssetStatus,
    TransferCapability,
    TransferStatus,
)
from ace_service.repository import (
    mark_source_uploaded,
    revoke_active_transfers,
    revoke_asset_transfers,
    transition_job,
)

LOGGER = logging.getLogger(__name__)
_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_RETAINABLE_TRANSFER_STATUSES = frozenset(
    {TransferStatus.CONSUMED, TransferStatus.EXPIRED, TransferStatus.REVOKED}
)
_RETAINABLE_ASSET_TRANSFER_STATUSES = frozenset(
    {AssetTransferStatus.CONSUMED, AssetTransferStatus.EXPIRED, AssetTransferStatus.REVOKED}
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
    reconciled_project_deletions: int = 0
    expired_asset_capabilities: int = 0
    revoked_asset_capabilities: int = 0
    deleted_asset_capability_records: int = 0
    reconciled_source_uploads: int = 0
    expired_source_uploads: int = 0


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
    media_service = MediaLibraryService(settings, session_factory)
    reconciled_media_items = 0
    try:
        reconciled_media_items = media_service.reconcile_pending_deletions()
    except MediaLibraryError:
        LOGGER.warning(
            "media deletion reconciliation deferred stage=cleanup",
            extra={"component": "controller"},
        )
    reconciled_project_deletions = 0
    with session_factory() as session:
        project_deletion_ids = list(session.scalars(select(ProjectDeletionAudit.project_id)))
    for project_id in project_deletion_ids:
        try:
            if media_service.reconcile_project_deletion(project_id):
                reconciled_project_deletions += 1
        except (MediaLibraryError, ValueError):
            LOGGER.warning(
                "project deletion reconciliation deferred stage=cleanup",
                extra={"component": "controller"},
            )
    revoked = 0
    expired = 0
    deleted = 0
    terminal_cover_ids: list[str] = []
    expired_staging = 0
    deleted_notification_events = 0
    deleted_notification_subscriptions = 0
    expired_asset_capabilities = 0
    revoked_asset_capabilities = 0
    deleted_asset_capability_records = 0
    reconciled_source_uploads = 0
    expired_source_uploads = 0
    source_raw_cleanup_ids: list[str] = []

    with session_factory() as session:
        source_assets = list(session.scalars(select(SourceAsset)))
        for asset in source_assets:
            if asset.status is SourceAssetStatus.AWAITING_UPLOAD:
                consumed_upload = session.scalar(
                    select(AssetTransferCapability)
                    .where(
                        AssetTransferCapability.source_asset_id == asset.id,
                        AssetTransferCapability.purpose
                        == AssetTransferPurpose.BROWSER_SOURCE_UPLOAD,
                        AssetTransferCapability.status == AssetTransferStatus.CONSUMED,
                    )
                    .order_by(AssetTransferCapability.created_at.desc())
                )
                if consumed_upload is not None and _source_upload_matches(
                    settings, asset, consumed_upload
                ):
                    mark_source_uploaded(
                        session,
                        asset.id,
                        raw_relative_path=consumed_upload.expected_relative_path,
                        raw_byte_size=consumed_upload.received_byte_size or 0,
                        raw_sha256=consumed_upload.received_sha256 or "",
                    )
                    reconciled_source_uploads += 1
                elif asset.created_at <= stale_cutoff:
                    asset.status = SourceAssetStatus.CANCELLED
                    asset.error_code = "upload_expired"
                    asset.user_facing_error = "The upload expired before it was completed."
                    asset.next_attempt_at = None
                    asset.updated_at = current_time
                    revoke_asset_transfers(session, source_asset_id=asset.id, now=current_time)
                    source_raw_cleanup_ids.append(asset.id)
                    expired_source_uploads += 1

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
        asset_capabilities = list(session.scalars(select(AssetTransferCapability)))
        for asset_capability in asset_capabilities:
            if (
                asset_capability.status is AssetTransferStatus.ISSUED
                and asset_capability.expires_at <= current_time
            ):
                asset_capability.status = AssetTransferStatus.EXPIRED
                expired_asset_capabilities += 1
            if asset_capability.status is AssetTransferStatus.ISSUED and _asset_owner_is_terminal(
                asset_capability
            ):
                asset_capability.status = AssetTransferStatus.REVOKED
                asset_capability.consumed_at = current_time
                revoked_asset_capabilities += 1
            if (
                asset_capability.status in _RETAINABLE_ASSET_TRANSFER_STATUSES
                and _asset_capability_timestamp(asset_capability) <= retention_cutoff
            ):
                session.delete(asset_capability)
                deleted_asset_capability_records += 1
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

    if source_raw_cleanup_ids:
        from ace_service.source_assets import purge_source_raw

        for source_asset_id in source_raw_cleanup_ids:
            purge_source_raw(settings, source_asset_id)

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
        reconciled_project_deletions=reconciled_project_deletions,
        expired_asset_capabilities=expired_asset_capabilities,
        revoked_asset_capabilities=revoked_asset_capabilities,
        deleted_asset_capability_records=deleted_asset_capability_records,
        reconciled_source_uploads=reconciled_source_uploads,
        expired_source_uploads=expired_source_uploads,
    )
    LOGGER.info(
        "cleanup complete stage=cleanup stale_part_files=%d expired_capabilities=%d "
        "revoked_capabilities=%d deleted_capability_records=%d removed_cover_sources=%d "
        "expired_cover_staging=%d reconciled_project_deletions=%d "
        "expired_asset_capabilities=%d revoked_asset_capabilities=%d "
        "deleted_asset_capability_records=%d reconciled_source_uploads=%d "
        "expired_source_uploads=%d",
        report.stale_part_files,
        report.expired_capabilities,
        report.revoked_capabilities,
        report.deleted_capability_records,
        report.removed_cover_sources,
        report.expired_cover_staging,
        report.reconciled_project_deletions,
        report.expired_asset_capabilities,
        report.revoked_asset_capabilities,
        report.deleted_asset_capability_records,
        report.reconciled_source_uploads,
        report.expired_source_uploads,
        extra={"component": "controller"},
    )
    return report


def _source_upload_matches(
    settings: ServiceSettings,
    asset: SourceAsset,
    capability: AssetTransferCapability,
) -> bool:
    if (
        capability.received_byte_size is None
        or capability.received_sha256 is None
        or capability.expected_relative_path != f"{asset.id}/source.bin"
        or capability.received_byte_size != capability.expected_byte_size
        or capability.received_sha256 != capability.expected_sha256
    ):
        return False
    path = settings.paths.source_upload_final(asset.id)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size != capability.received_byte_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == capability.received_sha256
    except OSError:
        return False


def _asset_owner_is_terminal(capability: AssetTransferCapability) -> bool:
    if capability.source_asset is not None:
        return capability.source_asset.status in {
            SourceAssetStatus.READY,
            SourceAssetStatus.FAILED,
            SourceAssetStatus.CANCELLED,
        }
    if capability.job is not None:
        return capability.job.status in _TERMINAL_STATUSES
    if capability.derivative_task is not None:
        return capability.derivative_task.status in {"ready", "failed"}
    return True


def _asset_capability_timestamp(capability: AssetTransferCapability) -> datetime:
    return capability.consumed_at or capability.expires_at


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
    if job.source_media_item_id is not None:
        return True
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

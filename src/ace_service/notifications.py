"""Durable Web Push outbox, subscription validation, and bounded dispatcher."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select

from ace_service.config import ServiceSettings
from ace_service.db import SessionFactory
from ace_service.models import NotificationDelivery, NotificationEvent, PushSubscription, utc_now
from ace_service.repository import insert_notification_event

LOGGER = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 4096
MAX_ATTEMPTS = 12
MAX_RETRY_SECONDS = 15 * 60
ALLOWED_EVENT_KINDS = frozenset(
    {
        "generation_completed",
        "managed_generation_started",
        "capacity_retained_reminder",
        "capacity_release_warning",
        "capacity_released",
        "capacity_release_overdue",
    }
)


class SubscriptionValidationError(ValueError):
    """Raised for a malformed or disallowed browser subscription."""


def _endpoint_origin(endpoint: str) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise SubscriptionValidationError("push endpoint is invalid")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise SubscriptionValidationError("push endpoint is invalid")
    return f"https://{parsed.netloc.lower()}"


def _decode_key(value: Any, name: str, *, exact_length: int) -> str:
    if not isinstance(value, str) or len(value) > 256 or not value:
        raise SubscriptionValidationError(f"push {name} key is invalid")
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise SubscriptionValidationError(f"push {name} key is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise SubscriptionValidationError(f"push {name} key is invalid") from exc
    if len(decoded) != exact_length:
        raise SubscriptionValidationError(f"push {name} key is invalid")
    return value


def validate_subscription(
    endpoint: str,
    p256dh: Any,
    auth: Any,
    allowed_origins: frozenset[str] | set[str] | tuple[str, ...],
) -> str:
    origin = _endpoint_origin(endpoint)
    if origin not in set(allowed_origins):
        raise SubscriptionValidationError("push endpoint origin is not allowed")
    _decode_key(p256dh, "p256dh", exact_length=65)
    _decode_key(auth, "auth", exact_length=16)
    return origin


def create_or_replace_subscription(
    session: Any,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    allowed_origins: frozenset[str] | set[str] | tuple[str, ...],
    now: datetime | None = None,
) -> PushSubscription:
    origin = validate_subscription(endpoint, p256dh, auth, allowed_origins)
    timestamp = now.astimezone(UTC) if now is not None else utc_now()
    subscription = session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if subscription is None:
        subscription = PushSubscription(
            id=str(uuid4()),
            endpoint=endpoint,
            endpoint_origin=origin,
            p256dh=p256dh,
            auth=auth,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(subscription)
    else:
        subscription.endpoint_origin = origin
        subscription.p256dh = p256dh
        subscription.auth = auth
        subscription.updated_at = timestamp
        subscription.disabled_at = None
    session.flush()
    return cast(PushSubscription, subscription)


def disable_subscription(
    session: Any, subscription_id: str, *, now: datetime | None = None
) -> bool:
    subscription = session.get(PushSubscription, subscription_id)
    if subscription is None:
        return False
    subscription.disabled_at = now.astimezone(UTC) if now is not None else utc_now()
    subscription.updated_at = subscription.disabled_at
    session.flush()
    return True


def event_payload(event: NotificationEvent) -> dict[str, str]:
    if event.kind not in ALLOWED_EVENT_KINDS:
        raise ValueError("notification event kind is not allowed")
    if not event.target_path.startswith("/") or event.target_path.startswith("//"):
        raise ValueError("notification target is not same-origin")
    payload = {
        "kind": event.kind,
        "event_key": event.event_key,
        "title": event.title,
        "body": event.body,
        "path": event.target_path,
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) >= MAX_PAYLOAD_BYTES:
        raise ValueError("notification payload is too large")
    return payload


async def _default_sender(
    subscription: PushSubscription,
    payload: dict[str, str],
    settings: ServiceSettings,
    ttl: int,
) -> int:
    if not settings.web_push_enabled:
        return 204
    try:
        import aiohttp
        from pywebpush import webpush_async  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Web Push dependency is unavailable") from exc
    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.web_push_send_timeout_seconds)
    ) as session:
        adapter = NoRedirectAiohttpSession(session)
        response = await webpush_async(
            subscription_info,
            data=json.dumps(payload, separators=(",", ":")),
            vapid_private_key=settings.web_push_vapid_private_key,
            vapid_claims={"sub": settings.web_push_vapid_subject},
            ttl=ttl,
            timeout=settings.web_push_send_timeout_seconds,
            aiohttp_session=adapter,
        )
        return int(getattr(response, "status", 201))


class NoRedirectAiohttpSession:
    """Adapter that makes pywebpush's POST redirect policy explicit."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def post(self, endpoint: str, **kwargs: Any) -> Any:
        kwargs["allow_redirects"] = False
        return await self.session.post(endpoint, **kwargs)


Sender = Callable[[PushSubscription, dict[str, str], ServiceSettings, int], Awaitable[int]]


class NotificationDispatcher:
    """Claim and deliver due rows without making notifications control-plane critical."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: ServiceSettings,
        *,
        sender: Sender | None = None,
        owner: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.sender = sender or _default_sender
        self.owner = owner or secrets.token_hex(16)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="ace-notification-dispatcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run(self) -> None:
        while True:
            await self.dispatch_once()
            await asyncio.sleep(5)

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        current = now.astimezone(UTC) if now is not None else utc_now()
        with self.session_factory() as session:
            delivery = session.scalar(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status == "pending",
                    NotificationDelivery.next_attempt_at <= current,
                )
                .order_by(NotificationDelivery.id)
            )
            if delivery is None:
                return 0
            if (
                delivery.claimed_by
                and delivery.claim_expires_at
                and delivery.claim_expires_at > current
            ):
                return 0
            delivery.claimed_by = self.owner
            delivery.claim_expires_at = current + timedelta(seconds=30)
            delivery.fencing_token += 1
            fencing_token = delivery.fencing_token
            session.commit()

        with self.session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery.id)
            if (
                delivery is None
                or delivery.claimed_by != self.owner
                or delivery.fencing_token != fencing_token
            ):
                return 0
            event = session.get(NotificationEvent, delivery.event_id)
            subscription = session.get(PushSubscription, delivery.subscription_id)
            if event is None or subscription is None or subscription.disabled_at is not None:
                delivery.status = "abandoned"
                session.commit()
                return 1
            try:
                payload = event_payload(event)
            except ValueError:
                delivery.status = "abandoned"
                delivery.claimed_by = None
                delivery.claim_expires_at = None
                session.commit()
                return 1
            ttl = (
                86_400
                if event.kind in {"generation_completed", "managed_generation_started"}
                else 600
            )
            attempt = delivery.attempt_count + 1
        status_code: int | None
        try:
            status_code = await self.sender(subscription, payload, self.settings, ttl)
        except Exception as exc:
            response = getattr(exc, "response", None)
            raw_status = getattr(response, "status", None)
            status_code = raw_status if isinstance(raw_status, int) else None

        with self.session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery.id)
            if (
                delivery is None
                or delivery.claimed_by != self.owner
                or delivery.fencing_token != fencing_token
            ):
                return 1
            delivery.last_status_code = status_code
            delivery.attempt_count = attempt
            delivery.claimed_by = None
            delivery.claim_expires_at = None
            if status_code is not None and 200 <= status_code < 300:
                delivery.status = "delivered"
                delivery.delivered_at = current
                subscription = session.get(PushSubscription, delivery.subscription_id)
                if subscription is not None:
                    subscription.last_success_at = current
                    subscription.updated_at = current
            elif status_code in {404, 410}:
                delivery.status = "abandoned"
                subscription = session.get(PushSubscription, delivery.subscription_id)
                if subscription is not None:
                    subscription.disabled_at = current
                    subscription.updated_at = current
            elif status_code is None or status_code in {408, 429} or status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    delivery.status = "abandoned"
                else:
                    delay = min(MAX_RETRY_SECONDS, 2 ** min(attempt, 10))
                    delivery.next_attempt_at = current + timedelta(seconds=delay)
            else:
                delivery.status = "abandoned"
            session.commit()
        return 1


def create_event(session: Any, **kwargs: Any) -> NotificationEvent:
    """Compatibility entry point used by worker and capacity code."""

    return insert_notification_event(session, **kwargs)

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

from ace_service.models import NotificationDelivery
from ace_service.notifications import NotificationDispatcher, create_or_replace_subscription
from ace_service.repository import insert_notification_event


def _key(size: int) -> str:
    return base64.urlsafe_b64encode(bytes(range(size))).decode().rstrip("=")


def test_event_dedupe_and_subscription_fanout(session) -> None:
    subscription = create_or_replace_subscription(
        session,
        endpoint="https://push.example.test/send/abc",
        p256dh=_key(65),
        auth=_key(16),
        allowed_origins={"https://push.example.test"},
    )
    event = insert_notification_event(
        session,
        event_key="generation-completed:job-1",
        kind="generation_completed",
        title="Generation complete",
        body="Your AudioVentura generation is ready.",
        target_path="/jobs/job-1",
        job_id=None,
    )
    duplicate = insert_notification_event(
        session,
        event_key="generation-completed:job-1",
        kind="generation_completed",
        title="Different copy is ignored",
        body="Different copy is ignored",
        target_path="/jobs/job-1",
    )
    session.commit()
    assert duplicate.id == event.id
    assert len(event.deliveries) == 1
    assert event.deliveries[0].subscription_id == subscription.id


def test_dispatcher_disables_gone_subscription(settings) -> None:
    from ace_service.db import create_database_engine, create_session_factory, initialize_database

    engine = create_database_engine(settings)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        create_or_replace_subscription(
            session,
            endpoint="https://push.example.test/send/gone",
            p256dh=_key(65),
            auth=_key(16),
            allowed_origins={"https://push.example.test"},
        )
        insert_notification_event(
            session,
            event_key="capacity-released:key:epoch",
            kind="capacity_released",
            title="GPU released",
            body="The warm worker has been released.",
            target_path="/",
        )
        session.commit()

    async def sender(*args):
        del args
        return 410

    dispatcher = NotificationDispatcher(session_factory, settings, sender=sender, owner="test")
    assert asyncio.run(dispatcher.dispatch_once()) == 1
    with session_factory() as session:
        subscription = session.query(
            __import__("ace_service.models", fromlist=["PushSubscription"]).PushSubscription
        ).one()
        assert subscription.disabled_at is not None
    engine.dispose()


def test_dispatcher_retries_only_retryable_failures(settings) -> None:
    from ace_service.db import create_database_engine, create_session_factory, initialize_database

    engine = create_database_engine(settings)
    initialize_database(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as session:
        create_or_replace_subscription(
            session,
            endpoint="https://push.example.test/send/retry-contract",
            p256dh=_key(65),
            auth=_key(16),
            allowed_origins={"https://push.example.test"},
            now=now,
        )
        insert_notification_event(
            session,
            event_key="capacity-released:key:retry-contract",
            kind="capacity_released",
            title="GPU released",
            body="The warm worker has been released.",
            target_path="/",
            created_at=now,
        )
        session.commit()

    statuses = iter((503, 400))

    async def sender(*args):
        del args
        return next(statuses)

    dispatcher = NotificationDispatcher(session_factory, settings, sender=sender, owner="test")
    assert asyncio.run(dispatcher.dispatch_once(now=now)) == 1
    with session_factory() as session:
        delivery = session.query(NotificationDelivery).one()
        assert delivery.status == "pending"
        assert delivery.attempt_count == 1
        assert delivery.next_attempt_at == now + timedelta(seconds=2)

    assert asyncio.run(dispatcher.dispatch_once(now=now + timedelta(seconds=2))) == 1
    with session_factory() as session:
        delivery = session.query(NotificationDelivery).one()
        assert delivery.status == "abandoned"
        assert delivery.attempt_count == 2
        assert delivery.last_status_code == 400
    engine.dispose()

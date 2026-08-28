from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any, cast

from fastapi.testclient import TestClient

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import (
    ControllerSetting,
    NotificationDelivery,
    NotificationEvent,
    PushSubscription,
)
from ace_service.notifications import MAX_ATTEMPTS
from ace_service.repository import get_keep_warm_seconds
from ace_service.web import _readiness


class _Worker:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue(self, job_id: str) -> bool:
        del job_id
        return True


class _ProviderClient:
    async def health(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _HomeClient:
    async def health(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _key(size: int) -> str:
    return base64.urlsafe_b64encode(bytes(range(size))).decode().rstrip("=")


def _auth() -> tuple[str, str]:
    return ("change-me", "test-password")


def _build_app(settings: ServiceSettings, *, root_path: str = "") -> tuple[Any, Any, Any]:
    values = settings.model_dump()
    values.update(
        service_root_path=root_path,
        web_push_vapid_public_key="public-key",
        web_push_vapid_private_key="private-key",
        web_push_vapid_subject="mailto:operator@example.test",
        web_push_allowed_endpoint_origins="https://push.example.test",
    )
    configured = ServiceSettings(**values)
    engine = create_database_engine(configured)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        configured,
        session_factory=factory,
        runpod_client=cast(Any, _ProviderClient()),
        home_ingest_client=_HomeClient(),
        worker=_Worker(),
    )
    return app, factory, engine


def test_push_routes_require_auth_csrf_and_never_return_subscription_secrets(
    settings: ServiceSettings,
) -> None:
    app, _, engine = _build_app(settings)
    try:
        with TestClient(app) as client:
            assert client.get("/notifications/config").status_code == 401
            page = client.get("/", auth=_auth())
            assert page.status_code == 200
            csrf = client.cookies.get("ace_csrf")
            assert csrf
            assert client.get("/notifications/config", auth=_auth()).json() == {
                "enabled": True,
                "public_key": "public-key",
            }
            payload = {
                "endpoint": "https://push.example.test/send/subscription-1",
                "keys": {"p256dh": _key(65), "auth": _key(16)},
                "csrf_token": csrf,
            }
            response = client.post("/notifications/subscriptions", auth=_auth(), json=payload)
            assert response.status_code == 200
            subscription_id = response.json()["subscription_id"]
            assert _key(65) not in response.text
            assert _key(16) not in response.text
            assert (
                client.delete(
                    f"/notifications/subscriptions/{subscription_id}",
                    auth=_auth(),
                    headers={"X-CSRF-Token": "wrong"},
                ).status_code
                == 403
            )
            disabled = client.delete(
                f"/notifications/subscriptions/{subscription_id}",
                auth=_auth(),
                headers={"X-CSRF-Token": csrf},
            )
            assert disabled.status_code == 200
            assert disabled.json() == {"subscription_id": subscription_id, "enabled": False}
    finally:
        asyncio.run(app.state.notification_dispatcher.stop())
        engine.dispose()


def test_service_worker_and_keep_warm_round_trip_at_beta_root(settings: ServiceSettings) -> None:
    app, factory, engine = _build_app(settings, root_path="/beta")
    values = (0, 60, 120, 180, 300, 600, 900, 1800, 2700, 3600, 7200, 10800, 14400)
    try:
        with TestClient(app) as client:
            page = client.get("/beta/", auth=_auth())
            assert page.status_code == 200
            assert 'action="/beta/settings/keep-warm"' in page.text
            csrf = client.cookies.get("ace_csrf")
            assert csrf
            worker = client.get("/beta/notification-worker.js", auth=_auth())
            assert worker.status_code == 200
            assert worker.headers["service-worker-allowed"] == "/beta/"
            assert worker.headers["cache-control"] == "no-cache"
            assert "self.registration.scope" in worker.text
            assert "new URL(path, self.location.origin)" in worker.text
            assert "https://" not in worker.text
            notifications_js = client.get("/beta/static/notifications.js", auth=_auth())
            assert notifications_js.status_code == 200
            assert "Uint8Array.from" in notifications_js.text
            assert "applicationServerKey(key)" in notifications_js.text
            assert '<div class="notification-control" hidden>' in page.text
            with factory() as session:
                assert get_keep_warm_seconds(session) == 900
                session.commit()
            unchanged = client.post(
                "/beta/settings/keep-warm",
                auth=_auth(),
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert unchanged.status_code == 303
            with factory() as session:
                assert session.get(ControllerSetting, 1).keep_warm_seconds == 900
            for value in values:
                response = client.post(
                    "/beta/settings/keep-warm",
                    auth=_auth(),
                    data={"csrf_token": csrf, "keep_warm_seconds": str(value)},
                    follow_redirects=False,
                )
                assert response.status_code == 303
                assert response.headers["location"] == "/beta/"
            invalid = client.post(
                "/beta/settings/keep-warm",
                auth=_auth(),
                data={"csrf_token": csrf, "keep_warm_seconds": "61"},
            )
            assert invalid.status_code == 422
        with factory() as session:
            assert session.get(ControllerSetting, 1).keep_warm_seconds == 14400
    finally:
        asyncio.run(app.state.notification_dispatcher.stop())
        engine.dispose()


def test_exhausted_push_retry_degrades_readiness_but_gone_subscription_does_not(
    settings: ServiceSettings,
) -> None:
    app, factory, engine = _build_app(settings)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with factory() as session:
            subscription = PushSubscription(
                id="subscription-1",
                endpoint="https://push.example.test/send/subscription-1",
                endpoint_origin="https://push.example.test",
                p256dh=_key(65),
                auth=_key(16),
                created_at=now,
                updated_at=now,
            )
            event = NotificationEvent(
                event_key="generation-completed:job-1",
                kind="generation_completed",
                title="Generation complete",
                body="Your AudioVentura generation is ready.",
                target_path="/jobs/job-1",
                created_at=now,
            )
            session.add_all((subscription, event))
            session.flush()
            delivery = NotificationDelivery(
                event_id=event.id,
                subscription_id=subscription.id,
                status="abandoned",
                attempt_count=MAX_ATTEMPTS,
                next_attempt_at=now,
                last_status_code=503,
            )
            session.add(delivery)
            session.commit()

        exhausted = asyncio.run(_readiness(app, only={"web_push"}))
        assert exhausted["components"]["web_push"] == {
            "ok": False,
            "message": "delivery retry exhausted",
        }

        with factory() as session:
            delivery = session.query(NotificationDelivery).one()
            delivery.last_status_code = 410
            session.commit()
        gone = asyncio.run(_readiness(app, only={"web_push"}))
        assert gone["components"]["web_push"] == {"ok": True, "message": "ready"}
    finally:
        asyncio.run(app.state.notification_dispatcher.stop())
        engine.dispose()

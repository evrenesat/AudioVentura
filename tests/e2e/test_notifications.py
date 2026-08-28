from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.conftest import E2EServer

_BROWSER_STUB = """
({ permission, subscribed }) => {
  let currentPermission = permission;
  const pushSubscription = {
    endpoint: 'https://push.example.test/send/e2e',
    toJSON: () => ({
      endpoint: 'https://push.example.test/send/e2e',
      keys: { p256dh: 'e2e-p256dh', auth: 'e2e-auth' },
    }),
    unsubscribe: async () => true,
  };
  class FakeNotification {}
  Object.defineProperty(FakeNotification, 'permission', {
    get: () => currentPermission,
  });
  FakeNotification.requestPermission = async () => {
    currentPermission = 'granted';
    return currentPermission;
  };
  const pushManager = {
    getSubscription: async () => subscribed ? pushSubscription : null,
    subscribe: async () => pushSubscription,
  };
  Object.defineProperty(window, 'Notification', {
    configurable: true,
    value: FakeNotification,
  });
  Object.defineProperty(window, 'PushManager', {
    configurable: true,
    value: class FakePushManager {},
  });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      register: async (url, options) => {
        window.__notificationRegistration = { url, options };
        return { pushManager };
      },
    },
  });
}
"""


def _prepare_page(
    page: Page,
    server: E2EServer,
    *,
    permission: str,
    subscribed: bool,
    configured: bool,
) -> None:
    credentials = f"{server.username}:{server.password}".encode()
    token = base64.b64encode(credentials).decode("ascii")
    page.context.set_extra_http_headers({"Authorization": f"Basic {token}"})
    page.add_init_script(
        script=(
            f"({_BROWSER_STUB})({json.dumps({'permission': permission, 'subscribed': subscribed})})"
        )
    )
    if configured:
        page.route(
            "**/beta/notifications/config",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"enabled": True, "public_key": "AQIDBA"}),
            ),
        )
    page.goto(f"{server.base_url}/beta/", wait_until="domcontentloaded")


def test_native_browser_registers_public_worker(page: Page, e2e_server: E2EServer) -> None:
    credentials = f"{e2e_server.username}:{e2e_server.password}".encode()
    token = base64.b64encode(credentials).decode("ascii")
    page.context.set_extra_http_headers({"Authorization": f"Basic {token}"})
    page.goto(f"{e2e_server.base_url}/beta/", wait_until="domcontentloaded")
    scope = page.evaluate(
        "navigator.serviceWorker.register('/beta/notification-worker.js', "
        "{scope: '/beta/'}).then((registration) => registration.scope)"
    )
    assert scope == f"{e2e_server.base_url}/beta/"


@pytest.mark.parametrize(
    ("configured", "permission", "subscribed", "control_visible", "button_visible", "status"),
    [
        (False, "default", False, False, False, ""),
        (True, "default", False, True, True, ""),
        (True, "granted", False, True, True, ""),
        (True, "granted", True, False, False, ""),
        (True, "denied", False, True, False, "Notifications blocked in browser"),
    ],
)
def test_notification_control_reflects_actionable_browser_state(
    page: Page,
    e2e_server: E2EServer,
    configured: bool,
    permission: str,
    subscribed: bool,
    control_visible: bool,
    button_visible: bool,
    status: str,
) -> None:
    _prepare_page(
        page,
        e2e_server,
        permission=permission,
        subscribed=subscribed,
        configured=configured,
    )
    control = page.locator(".notification-control")
    button = page.get_by_role("button", name="Enable notifications")
    if control_visible:
        expect(control).to_be_visible()
    else:
        expect(control).to_be_hidden()
    if button_visible:
        expect(button).to_be_visible()
        expect(button).to_be_enabled()
    else:
        expect(button).to_be_hidden()
    if status:
        expect(page.get_by_text(status, exact=True)).to_be_visible()


def test_notification_enrollment_posts_subscription_and_hides_control(
    page: Page, e2e_server: E2EServer
) -> None:
    posted: list[dict[str, Any]] = []

    def fulfill_subscription(route: Route) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"subscription_id": "subscription-e2e", "enabled": True}),
        )

    page.route("**/beta/notifications/subscriptions", fulfill_subscription)
    _prepare_page(
        page,
        e2e_server,
        permission="default",
        subscribed=False,
        configured=True,
    )
    page.get_by_role("button", name="Enable notifications").click()
    expect(page.locator(".notification-control")).to_be_hidden()
    assert posted == [
        {
            "endpoint": "https://push.example.test/send/e2e",
            "keys": {"p256dh": "e2e-p256dh", "auth": "e2e-auth"},
            "csrf_token": page.locator('meta[name="csrf-token"]').get_attribute("content"),
        }
    ]
    assert page.evaluate("localStorage.getItem('ace_push_subscription_id')") == ("subscription-e2e")

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import E2EServer


def test_mobile_shell_has_no_horizontal_overflow_and_touch_targets(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    e2e_page.set_viewport_size({"width": 412, "height": 915})
    e2e_page.goto(f"{e2e_server.base_url}/beta/library", wait_until="domcontentloaded")
    expect(e2e_page.get_by_role("heading", name="Media library")).to_be_visible()
    assert e2e_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    sizes = e2e_page.locator("[data-player-control], .button.danger").evaluate_all(
        "nodes => nodes.map(node => ({"
        "width: node.getBoundingClientRect().width, "
        "height: node.getBoundingClientRect().height}))"
    )
    assert sizes and all(item["width"] >= 44 and item["height"] >= 44 for item in sizes)
    play_button = e2e_page.get_by_role(
        "button", name="Play Alpha ambient composition · Variation 1"
    )
    play_button.focus()
    expect(play_button).to_be_focused()

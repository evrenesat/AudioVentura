from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e.conftest import E2EServer


def test_deleted_track_is_tombstoned_and_queued_job_cancels(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    e2e_page.goto(f"{e2e_server.base_url}/beta/library", wait_until="domcontentloaded")
    alpha = e2e_page.locator(f'[data-media-id="{e2e_server.alpha_media_id}"]')
    alpha.get_by_role("button", name="Delete").click()
    expect(e2e_page.locator(f'[data-media-id="{e2e_server.alpha_media_id}"]')).to_have_count(0)

    e2e_page.goto(
        f"{e2e_server.base_url}/beta/projects/{e2e_server.alpha_project_id}",
        wait_until="domcontentloaded",
    )
    expect(e2e_page.get_by_text("Deleted audio · unavailable")).to_be_visible()

    e2e_page.goto(
        f"{e2e_server.base_url}/beta/jobs/{e2e_server.cancellable_job_id}",
        wait_until="domcontentloaded",
    )
    e2e_page.get_by_role("button", name="Cancel generation").click()
    expect(e2e_page.locator("#job-status")).to_have_text("Cancelled")


def test_active_project_is_protected_and_too_late_cancel_continues(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    e2e_page.goto(
        f"{e2e_server.base_url}/beta/jobs/{e2e_server.too_late_job_id}",
        wait_until="domcontentloaded",
    )
    e2e_page.get_by_role("button", name="Cancel generation").click()
    expect(
        e2e_page.get_by_text("Cancellation arrived too late; generation continues.")
    ).to_be_visible()
    expect(e2e_page.get_by_role("button", name="Cancel generation")).to_have_count(0)

    e2e_page.goto(f"{e2e_server.base_url}/beta/projects/{e2e_server.too_late_project_id}")
    expect(e2e_page.get_by_text("Project deletion becomes available")).to_be_visible()
    expect(e2e_page.get_by_text("Delete project")).to_have_count(0)

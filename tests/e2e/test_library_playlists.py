from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e.conftest import E2EServer


def _library(page: Page, server: E2EServer) -> None:
    page.goto(f"{server.base_url}/beta/library", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Media library")).to_be_visible()


def test_library_search_sort_filter_and_custom_playlist_editing(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    _library(e2e_page, e2e_server)
    expect(e2e_page.locator("[data-media-id]")).to_have_count(2)
    e2e_page.get_by_label("Search library").fill("Alpha")
    e2e_page.get_by_role("button", name="Filter").click()
    expect(e2e_page.locator("[data-media-id]")).to_have_count(1)
    expect(e2e_page.get_by_role("heading", name=re.compile("Alpha"))).to_be_visible()

    e2e_page.get_by_label("Search library").fill("")
    e2e_page.get_by_label("Project").select_option(e2e_server.beta_project_id)
    e2e_page.get_by_label("Sort").select_option("title")
    e2e_page.get_by_role("button", name="Filter").click()
    expect(e2e_page.locator("[data-media-id]")).to_have_count(1)
    expect(e2e_page.get_by_role("heading", name=re.compile("Beta"))).to_be_visible()

    e2e_page.get_by_role("link", name="Playlists", exact=True).click()
    expect(e2e_page.get_by_role("heading", name="Playlists")).to_be_visible()
    e2e_page.get_by_label("New custom playlist").fill("Road Mix")
    e2e_page.get_by_role("button", name="Create playlist").click()
    expect(e2e_page.get_by_role("heading", name="Road Mix")).to_be_visible()

    add_select = e2e_page.get_by_label("Library track")
    add_select.select_option(e2e_server.alpha_media_id)
    e2e_page.get_by_role("button", name="Add track").click()
    add_select = e2e_page.get_by_label("Library track")
    add_select.select_option(e2e_server.alpha_media_id)
    e2e_page.get_by_role("button", name="Add track").click()
    add_select = e2e_page.get_by_label("Library track")
    add_select.select_option(e2e_server.beta_media_id)
    e2e_page.get_by_role("button", name="Add track").click()
    expect(e2e_page.locator("[data-playlist-entry]")).to_have_count(3)

    e2e_page.get_by_label("Playlist title").fill("Road Mix Renamed")
    e2e_page.get_by_role("button", name="Rename").click()
    expect(e2e_page.get_by_role("heading", name="Road Mix Renamed")).to_be_visible()

    beta_entry = e2e_page.locator("[data-playlist-entry]").filter(has_text="Beta")
    with e2e_page.expect_response(
        lambda response: response.url.endswith("/entries/reorder")
    ) as event:
        beta_entry.get_by_role("button", name=re.compile("Move .* up")).click()
    assert event.value.status == 200, event.value.text()
    expect(e2e_page.locator("[data-playlist-entry]").nth(1)).to_contain_text("Beta")

    alpha_entries = e2e_page.locator("[data-playlist-entry]").filter(has_text="Alpha")
    expect(alpha_entries).to_have_count(2)
    alpha_entries.nth(1).get_by_role("button", name="Remove").click()
    expect(e2e_page.locator("[data-playlist-entry]")).to_have_count(2)
    expect(e2e_page.locator("[data-playlist-entry]").filter(has_text="Alpha")).to_have_count(1)

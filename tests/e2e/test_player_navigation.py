from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e.conftest import E2EServer


def _play_first_track(page: Page, server: E2EServer) -> None:
    page.goto(f"{server.base_url}/beta/library", wait_until="domcontentloaded")
    page.locator(f'[data-media-id="{server.beta_media_id}"] [data-player-play]').click()
    expect(page.locator("#global-audio")).to_have_attribute(
        "src", re.compile(r"/beta/media/library/")
    )


def test_player_controls_and_soft_navigation_preserve_playback(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    _play_first_track(e2e_page, e2e_server)
    assert e2e_page.locator("audio#global-audio").count() == 1
    for _ in range(100):
        duration = e2e_page.locator("#global-audio").evaluate("(audio) => audio.duration")
        if duration > 0:
            break
        e2e_page.wait_for_timeout(100)
    else:
        raise AssertionError("test MP3 did not expose playable duration")
    for _ in range(100):
        seek_max = e2e_page.locator('[data-player-control="seek"]').get_attribute("max")
        if seek_max and float(seek_max) > 0:
            break
        e2e_page.wait_for_timeout(100)
    else:
        raise AssertionError("player seek control did not expose playable duration")
    source = e2e_page.locator("#global-audio").get_attribute("src")
    assert source and source.startswith("/beta/media/library/")

    e2e_page.locator('[data-player-control="rate"]').select_option("1.5")
    expect(e2e_page.locator("#global-audio")).to_have_js_property("playbackRate", 1.5)
    e2e_page.locator('[data-player-control="seek"]').fill("1")
    next_source = e2e_page.locator(f'[data-media-id="{e2e_server.alpha_media_id}"]').get_attribute(
        "data-player-src"
    )
    assert next_source
    e2e_page.get_by_role("button", name="Next track").click()
    expect(e2e_page.locator("#global-audio")).to_have_attribute("src", next_source)
    e2e_page.get_by_role("button", name="Previous track").click()
    expect(e2e_page.locator("#global-audio")).to_have_attribute("src", source)
    e2e_page.locator("#global-audio").evaluate("(audio) => audio.pause()")

    e2e_page.get_by_role("button", name="Toggle shuffle").click()
    expect(e2e_page.get_by_role("button", name="Toggle shuffle")).to_have_attribute(
        "aria-pressed", "true"
    )
    e2e_page.get_by_role("button", name="Repeat mode: off").click()
    expect(e2e_page.get_by_role("button", name="Repeat mode: one")).to_have_attribute(
        "aria-pressed", "true"
    )
    e2e_page.get_by_role("button", name="Repeat mode: one").click()
    expect(e2e_page.get_by_role("button", name="Repeat mode: all")).to_have_attribute(
        "aria-pressed", "true"
    )

    e2e_page.locator("#global-audio").evaluate(
        "(audio) => { audio.currentTime = 1.1; audio.dispatchEvent(new Event('timeupdate')); }"
    )
    e2e_page.get_by_role("link", name="Playlists", exact=True).click()
    expect(e2e_page.get_by_role("heading", name="Playlists")).to_be_visible()
    assert e2e_page.locator("#global-audio").get_attribute("src") == source
    e2e_page.get_by_role("link", name="Projects", exact=True).click()
    expect(e2e_page.get_by_role("heading", name="Your projects")).to_be_visible()
    assert e2e_page.locator("#global-audio").get_attribute("src") == source
    e2e_page.locator(f'a.project-card[href="/beta/projects/{e2e_server.alpha_project_id}"]').click()
    expect(e2e_page.get_by_role("heading", name="Alpha ambient composition")).to_be_visible()
    assert e2e_page.locator("#global-audio").get_attribute("src") == source

    e2e_page.reload(wait_until="domcontentloaded")
    expect(e2e_page.locator("#global-audio")).to_have_attribute(
        "src", re.compile(r"/beta/media/library/"), timeout=10_000
    )
    restored = e2e_page.locator("#global-audio").evaluate("(audio) => audio.currentTime")
    assert restored >= 0.8

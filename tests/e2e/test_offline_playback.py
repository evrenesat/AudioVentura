from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect

from tests.e2e.conftest import E2EServer


def _open_playlists(page: Page, server: E2EServer) -> None:
    page.goto(f"{server.base_url}/beta/playlists", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Playlists")).to_be_visible()


def _create_duplicate_playlist(page: Page, server: E2EServer) -> None:
    _open_playlists(page, server)
    page.get_by_label("New custom playlist").fill("Offline duplicate mix")
    page.get_by_role("button", name="Create playlist").click()
    expect(page.get_by_role("heading", name="Offline duplicate mix")).to_be_visible()
    media_select = page.get_by_label("Library track")
    media_select.select_option(server.alpha_media_id)
    page.get_by_role("button", name="Add track").click()
    media_select = page.get_by_label("Library track")
    media_select.select_option(server.alpha_media_id)
    page.get_by_role("button", name="Add track").click()
    media_select = page.get_by_label("Library track")
    media_select.select_option(server.beta_media_id)
    page.get_by_role("button", name="Add track").click()
    expect(page.locator("[data-playlist-entry]")).to_have_count(3)


def _create_custom_playlist(page: Page, server: E2EServer, title: str, media_ids: list[str]) -> str:
    _open_playlists(page, server)
    page.get_by_label("New custom playlist").fill(title)
    page.get_by_role("button", name="Create playlist").click()
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    for media_id in media_ids:
        page.get_by_label("Library track").select_option(media_id)
        page.get_by_role("button", name="Add track").click()
    expect(page.locator("[data-playlist-entry]")).to_have_count(len(media_ids))
    return page.url.rsplit("/", 1)[-1]


def _wait_for_owner(
    page: Page, playlist_id: str, state: str = "ready", title: str | None = None
) -> None:
    for _ in range(300):
        owner = page.evaluate(
            "async (playlistId) => await window.AudioventuraOffline?.getOwner(playlistId)",
            playlist_id,
        )
        playlist = (owner or {}).get("playlist") or {}
        if playlist.get("state") == state and (title is None or playlist.get("title") == title):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"offline owner {playlist_id} did not reach {state}")


def _mark_worker_offline(page: Page) -> None:
    page.context.set_offline(True)
    page.evaluate(
        """
        () => new Promise((resolve) => {
          const controller = navigator.serviceWorker.controller;
          if (!controller) { resolve(false); return; }
          const channel = new MessageChannel();
          channel.port1.onmessage = () => resolve(true);
          controller.postMessage({ type: "offline-mode", enabled: true }, [channel.port2]);
        })
        """
    )


def _owner_entries_in_order(page: Page, playlist_id: str) -> list[dict[str, object]]:
    return page.evaluate(
        """
        async (playlistId) => (await window.AudioventuraOffline.getOwner(playlistId)).entries
          .sort((left, right) => left.position - right.position)
        """,
        playlist_id,
    )


def _offline_snapshot(page: Page) -> dict[str, object]:
    return page.evaluate(
        """
        async () => {
          const store = window.AudioventuraOfflineStore;
          const offline = window.AudioventuraOffline;
          const handle = await store.open(offline.scope);
          const owners = await store.listOwners(handle);
          const blobs = await store.listBlobs(handle);
          const refs = await store.listRefs(handle);
          const entries = await store.listEntries(handle);
          const cache = await caches.open(offline.MEDIA_CACHE_NAME);
          return {
            owners,
            blobs,
            refs,
            entries,
            cacheKeys: (await cache.keys()).map((request) => request.url),
          };
        }
        """
    )


def _wait_for_offline_ready(page: Page) -> None:
    page.wait_for_function("window.AudioventuraOffline && window.AudioventuraOfflineStore")
    page.evaluate("navigator.serviceWorker?.ready.catch(() => null)")
    page.wait_for_timeout(300)


def test_keep_offline_deduplicates_duplicate_entries_and_supports_offline_reload(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    _create_duplicate_playlist(e2e_page, e2e_server)
    _wait_for_offline_ready(e2e_page)
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )

    snapshot = _offline_snapshot(e2e_page)
    owners = snapshot["owners"]
    assert len(owners) == 1
    owner = owners[0]
    owner_id = owner["id"]
    assert owner["state"] == "ready"
    assert owner["track_count"] == 3
    assert owner["ready_track_count"] == 3
    assert len({entry["sha256"] for entry in snapshot["entries"]}) == 2
    assert len(snapshot["blobs"]) == 2
    assert len(snapshot["refs"]) == 2
    assert len(snapshot["cacheKeys"]) == 2

    shell = e2e_page.evaluate(
        """
        async () => {
          const cache = await caches.open("audioventura:beta:shell:v2");
          const response = await cache.match("/beta/offline-shell");
          return response ? await response.text() : "";
        }
        """
    )
    assert 'name="csrf-token"' not in shell
    assert "Alpha ambient composition" not in shell

    first_entry = snapshot["entries"][0]
    media_file_id = first_entry["media_file_id"]
    byte_size = first_entry["byte_size"]
    _mark_worker_offline(e2e_page)
    e2e_page.goto(f"{e2e_server.base_url}/beta/library", wait_until="domcontentloaded")
    expect(e2e_page.get_by_role("heading", name="Offline", exact=True)).to_be_visible(
        timeout=15_000
    )
    expect(e2e_page.locator("[data-offline-play]")).to_be_visible()
    range_results = e2e_page.evaluate(
        """
        async ({ mediaFileId, byteSize }) => {
          const read = async (range) => {
            let response;
            try {
              response = await fetch(`/beta/media/library/${mediaFileId}`, {
                headers: { Range: range },
              });
            } catch (error) {
              return { error: `${error.name}: ${error.message}` };
            }
            return {
              status: response.status,
              length: response.headers.get("content-length"),
              range: response.headers.get("content-range"),
              etag: response.headers.get("etag"),
            };
          };
          return {
            open: await read("bytes=0-9"),
            suffix: await read("bytes=-10"),
            invalid: await read(`bytes=${byteSize}-`),
            multiple: await read("bytes=0-1,4-5"),
          };
        }
        """,
        {"mediaFileId": media_file_id, "byteSize": byte_size},
    )
    assert range_results["open"]["status"] == 206
    assert range_results["open"]["length"] == "10"
    assert range_results["open"]["range"] == f"bytes 0-9/{byte_size}"
    assert range_results["suffix"]["status"] == 206
    assert range_results["suffix"]["range"] == f"bytes {byte_size - 10}-{byte_size - 1}/{byte_size}"
    assert range_results["invalid"]["status"] == 416
    assert range_results["multiple"]["status"] == 416
    e2e_page.locator("[data-offline-play]").click()
    for _ in range(100):
        if e2e_page.evaluate("window.AudioventuraPlayer?.getState().current?.queue_entry_id"):
            break
        e2e_page.wait_for_timeout(50)
    assert e2e_page.evaluate("window.AudioventuraPlayer?.getState().current?.queue_entry_id")
    expect(e2e_page.locator("#global-audio")).to_have_attribute(
        "src", re.compile(r"/beta/media/library/"), timeout=10_000
    )
    e2e_page.get_by_role("button", name="Toggle shuffle").click()
    expect(e2e_page.get_by_role("button", name="Toggle shuffle")).to_have_attribute(
        "aria-pressed", "true"
    )
    e2e_page.get_by_role("button", name="Toggle shuffle").click()
    expect(e2e_page.get_by_role("button", name="Toggle shuffle")).to_have_attribute(
        "aria-pressed", "false"
    )
    e2e_page.get_by_role("button", name="Repeat mode: off").click()
    expect(e2e_page.get_by_role("button", name="Repeat mode: one")).to_have_attribute(
        "aria-pressed", "true"
    )
    e2e_page.get_by_role("button", name="Repeat mode: one").click()
    expect(e2e_page.get_by_role("button", name="Repeat mode: all")).to_have_attribute(
        "aria-pressed", "true"
    )
    e2e_page.get_by_role("button", name="Repeat mode: all").click()
    expect(e2e_page.get_by_role("button", name="Repeat mode: off")).to_have_attribute(
        "aria-pressed", "false"
    )
    e2e_page.locator('[data-player-control="rate"]').select_option("1.5")
    expect(e2e_page.locator("#global-audio")).to_have_js_property("playbackRate", 1.5)
    e2e_page.wait_for_function("document.querySelector('#global-audio').duration > 0")
    e2e_page.locator('[data-player-control="seek"]').fill("0.5")
    assert e2e_page.locator("#global-audio").evaluate("(audio) => audio.currentTime") >= 0.4
    initial_entry_id = e2e_page.evaluate("AudioventuraPlayer.getState().current.queue_entry_id")
    e2e_page.get_by_role("button", name="Next track").click()
    expect(e2e_page.locator("#global-audio")).to_have_attribute(
        "src", re.compile(r"/beta/media/library/"), timeout=10_000
    )
    e2e_page.locator("#global-audio").evaluate("(audio) => audio.pause()")
    after_next = e2e_page.evaluate("AudioventuraPlayer.getState()")
    assert after_next["current"]["queue_entry_id"] != initial_entry_id
    expected_previous = after_next["queue"][max(0, after_next["index"] - 1)]["queue_entry_id"]
    e2e_page.get_by_role("button", name="Previous track").click()
    for _ in range(100):
        if (
            e2e_page.evaluate("AudioventuraPlayer.getState().current.queue_entry_id")
            == expected_previous
        ):
            break
        e2e_page.wait_for_timeout(50)
    assert (
        e2e_page.evaluate("AudioventuraPlayer.getState().current.queue_entry_id")
        == expected_previous
    )
    e2e_page.locator(f'[data-offline-owner-id="{owner_id}"]').get_by_role(
        "button", name="Remove download"
    ).click()
    expect(e2e_page.locator(f'[data-offline-owner-id="{owner_id}"]')).to_have_count(0)
    expect(e2e_page.locator("[data-offline-storage-message]")).to_be_visible()


def test_auto_cache_assigns_playlist_and_played_track_owners(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    playlist_id = _create_custom_playlist(
        e2e_page, e2e_server, "Automatic cache mix", [e2e_server.alpha_media_id]
    )
    e2e_page.locator("[data-playlist-entry]").get_by_role(
        "button", name=re.compile("Play Alpha")
    ).click()
    _wait_for_owner(e2e_page, playlist_id)
    e2e_page.wait_for_timeout(300)
    snapshot = _offline_snapshot(e2e_page)
    assert {owner["id"] for owner in snapshot["owners"]} == {playlist_id}
    assert snapshot["owners"][0]["intent"] == "automatic"
    assert snapshot["owners"][0]["kind"] == "custom"

    e2e_page.goto(
        f"{e2e_server.base_url}/beta/projects/{e2e_server.alpha_project_id}",
        wait_until="domcontentloaded",
    )
    e2e_page.locator(".version-outputs [data-player-play]").first.click()
    _wait_for_owner(e2e_page, "played-tracks")
    snapshot = _offline_snapshot(e2e_page)
    assert {owner["id"] for owner in snapshot["owners"]} == {playlist_id, "played-tracks"}
    played = next(owner for owner in snapshot["owners"] if owner["id"] == "played-tracks")
    assert played["kind"] == "local-played"
    assert len(snapshot["blobs"]) == 1
    assert len(snapshot["refs"]) == 2


def test_media_delete_invalidates_local_entries_and_retains_shared_body(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    _, shared_media_id = e2e_server.seed_track(
        job_id="123e4567-e89b-12d3-a456-426614174106",
        description="Retained shared ambient composition",
        frequency=440,
    )
    playlist_id = _create_custom_playlist(
        e2e_page, e2e_server, "Deleted media mix", [e2e_server.alpha_media_id]
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, playlist_id)
    retained_playlist_id = _create_custom_playlist(
        e2e_page, e2e_server, "Retained media mix", [shared_media_id]
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, retained_playlist_id)

    e2e_page.goto(f"{e2e_server.base_url}/beta/library", wait_until="domcontentloaded")
    e2e_page.locator(f'[data-media-id="{e2e_server.alpha_media_id}"]').get_by_role(
        "button", name="Delete"
    ).click()
    expect(e2e_page.get_by_role("heading", name="Media library")).to_be_visible()

    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    card = e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')
    expect(card).to_be_visible()
    assert _owner_entries_in_order(e2e_page, playlist_id) == []
    expect(card.get_by_role("button", name="Play")).to_be_disabled()
    retained_card = e2e_page.locator(f'[data-offline-owner-id="{retained_playlist_id}"]')
    expect(retained_card).to_be_visible()
    expect(retained_card.get_by_role("button", name="Play")).to_be_enabled()
    snapshot = _offline_snapshot(e2e_page)
    assert len(snapshot["refs"]) == 1
    assert len(snapshot["blobs"]) == 1
    assert len(snapshot["cacheKeys"]) == 1


def test_project_playlist_can_be_kept_offline(e2e_page: Page, e2e_server: E2EServer) -> None:
    _open_playlists(e2e_page, e2e_server)
    project_card = e2e_page.locator(".playlist-card").filter(has_text="Beta ambient composition")
    project_card.get_by_role("link", name="Beta ambient composition", exact=True).click()
    expect(
        e2e_page.get_by_role("heading", name="Beta ambient composition", exact=True)
    ).to_be_visible()
    detail_tools = e2e_page.locator(".offline-playlist-tools[data-offline-playlist]")
    playlist_id = detail_tools.get_attribute("data-offline-playlist")
    assert playlist_id
    detail_tools.get_by_role("button", name="Keep offline").click()
    expect(detail_tools.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, playlist_id)
    owner = _offline_snapshot(e2e_page)["owners"][0]
    assert owner["id"] == playlist_id
    assert owner["kind"] == "project"

    e2e_page.goto(
        f"{e2e_server.base_url}/beta/projects/{e2e_server.beta_project_id}",
        wait_until="domcontentloaded",
    )
    e2e_page.locator(".danger-zone summary").click()
    e2e_page.locator("#confirm-project-title").fill("Beta ambient composition")
    e2e_page.get_by_role("button", name="Delete project").click()
    expect(e2e_page.get_by_role("heading", name="Your projects")).to_be_visible()
    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    expect(e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')).to_have_count(0)


def test_shared_hash_cleanup_retains_then_removes_one_cached_body(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    _, shared_media_id = e2e_server.seed_track(
        job_id="123e4567-e89b-12d3-a456-426614174105",
        description="Shared Alpha ambient composition",
        frequency=440,
    )
    first_id = _create_custom_playlist(
        e2e_page, e2e_server, "Shared first", [e2e_server.alpha_media_id, shared_media_id]
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, first_id)
    second_id = _create_custom_playlist(e2e_page, e2e_server, "Shared second", [shared_media_id])
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, second_id)

    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    expect(e2e_page.locator(f'[data-offline-owner-id="{first_id}"]')).to_be_visible()
    expect(e2e_page.locator(f'[data-offline-owner-id="{second_id}"]')).to_be_visible()
    snapshot = _offline_snapshot(e2e_page)
    assert len(snapshot["blobs"]) == 1
    assert len(snapshot["refs"]) == 2
    assert len(snapshot["cacheKeys"]) == 1

    e2e_page.locator(f'[data-offline-owner-id="{first_id}"]').get_by_role(
        "button", name="Remove download"
    ).click()
    expect(e2e_page.locator(f'[data-offline-owner-id="{first_id}"]')).to_have_count(0)
    expect(e2e_page.locator("[data-offline-storage-message]")).to_contain_text("retained")
    expect(e2e_page.locator("[data-offline-storage-message]")).to_be_visible()
    snapshot = _offline_snapshot(e2e_page)
    assert len(snapshot["blobs"]) == 1
    assert len(snapshot["refs"]) == 1
    assert len(snapshot["cacheKeys"]) == 1

    e2e_page.locator(f'[data-offline-owner-id="{second_id}"]').get_by_role(
        "button", name="Remove download"
    ).click()
    expect(e2e_page.locator(f'[data-offline-owner-id="{second_id}"]')).to_have_count(0)
    snapshot = _offline_snapshot(e2e_page)
    assert snapshot["blobs"] == []
    assert snapshot["refs"] == []
    assert snapshot["cacheKeys"] == []


def test_partial_playlist_plays_later_cached_track_and_retry_reuses_completed_hashes(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    playlist_id = _create_custom_playlist(
        e2e_page,
        e2e_server,
        "Interrupted mix",
        [e2e_server.alpha_media_id, e2e_server.beta_media_id],
    )
    requests: dict[str, int] = {}
    interrupt = True
    failed_path = e2e_page.locator("[data-playlist-entry]").first.get_attribute("data-player-src")
    assert failed_path

    def interrupt_first_media_request(route) -> None:
        nonlocal interrupt
        headers = route.request.headers
        if headers.get("range") or headers.get("cache-control") != "no-store":
            route.continue_()
            return
        path = urlsplit(route.request.url).path
        requests[path] = requests.get(path, 0) + 1
        if interrupt and path == failed_path:
            interrupt = False
            route.abort()
        else:
            route.continue_()

    e2e_page.route("**/beta/media/library/*", interrupt_first_media_request)
    e2e_page.get_by_role("button", name="Keep offline").click()
    _wait_for_owner(e2e_page, playlist_id, "partial")
    e2e_page.wait_for_timeout(300)
    snapshot = _offline_snapshot(e2e_page)
    assert sum(requests.values()) == 2
    assert len([blob for blob in snapshot["blobs"] if blob["state"] == "ready"]) == 1

    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    card = e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')
    expect(card.get_by_role("button", name="Retry")).to_be_visible()
    expect(card.get_by_role("button", name="Play")).to_be_enabled()
    card.get_by_role("button", name="Play").click()
    expect(e2e_page.locator("[data-player-title]")).to_have_text(
        "Beta ambient composition · Variation 1", timeout=10_000
    )
    assert (
        e2e_page.evaluate("window.AudioventuraPlayer.getState().current.media_item_id")
        == e2e_server.beta_media_id
    )
    before_retry = dict(requests)
    card.get_by_role("button", name="Retry").click()
    _wait_for_owner(e2e_page, playlist_id)
    snapshot = _offline_snapshot(e2e_page)
    assert len(snapshot["blobs"]) == 2
    assert all(blob["state"] == "ready" for blob in snapshot["blobs"])
    assert requests[failed_path] == before_retry[failed_path] + 1
    assert {path: count for path, count in requests.items() if path != failed_path} == {
        path: count for path, count in before_retry.items() if path != failed_path
    }
    e2e_page.unroute("**/beta/media/library/*", interrupt_first_media_request)


def test_quota_failure_is_actionable_and_missing_body_is_retryable(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    playlist_id = _create_custom_playlist(
        e2e_page, e2e_server, "Storage failure mix", [e2e_server.alpha_media_id]
    )
    e2e_page.evaluate(
        """
        () => Object.defineProperty(navigator, "storage", {
          configurable: true,
          value: {
            estimate: async () => ({ usage: 1024, quota: 1024 }),
            persisted: async () => true,
            persist: async () => false,
          },
        })
        """
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        "not enough", timeout=10_000
    )

    e2e_page.evaluate(
        """
        () => Object.defineProperty(navigator, "storage", {
          configurable: true,
          value: {
            estimate: async () => ({ usage: 0, quota: 64 * 1024 * 1024 }),
            persisted: async () => true,
            persist: async () => true,
          },
        })
        """
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    e2e_page.evaluate(
        """
        async (playlistId) => {
          const offline = window.AudioventuraOffline;
          const cache = await caches.open(offline.MEDIA_CACHE_NAME);
          const owner = await offline.getOwner(playlistId);
          await cache.delete(offline.scope + "__offline/media/sha256/" + owner.entries[0].sha256);
        }
        """,
        playlist_id,
    )
    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    card = e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')
    expect(card).to_contain_text(re.compile(r"partial|failed"))
    expect(card.get_by_role("button", name="Retry")).to_be_visible()
    expect(card.get_by_role("button", name="Play")).to_be_disabled()


def test_online_playlist_changes_refresh_atomically_and_deleted_server_owner_is_invalidated(
    e2e_page: Page, e2e_server: E2EServer
) -> None:
    playlist_id = _create_custom_playlist(
        e2e_page,
        e2e_server,
        "Refreshable mix",
        [e2e_server.alpha_media_id, e2e_server.beta_media_id],
    )
    e2e_page.get_by_role("button", name="Keep offline").click()
    expect(e2e_page.locator("[data-offline-playlist-status]")).to_contain_text(
        re.compile(r"ready|available offline"), timeout=30_000
    )
    _wait_for_owner(e2e_page, playlist_id)

    e2e_page.get_by_label("Playlist title").fill("Refreshable renamed")
    e2e_page.get_by_role("button", name="Rename").click()
    expect(e2e_page.get_by_role("heading", name="Refreshable renamed", exact=True)).to_be_visible()
    e2e_page.get_by_label("Library track").select_option(e2e_server.alpha_media_id)
    e2e_page.get_by_role("button", name="Add track").click()
    beta_entry = e2e_page.locator("[data-playlist-entry]").filter(has_text="Beta")
    beta_entry.get_by_role("button", name=re.compile("Move .* up")).click()
    expect(e2e_page.locator("[data-playlist-entry]").first).to_contain_text("Beta")
    e2e_page.locator("[data-playlist-entry]").last.get_by_role("button", name="Remove").click()
    expect(e2e_page.locator("[data-playlist-entry]")).to_have_count(2)

    e2e_page.get_by_role("button", name="Refresh").click()
    _wait_for_owner(e2e_page, playlist_id, title="Refreshable renamed")
    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    card = e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')
    expect(card).to_contain_text("Refreshable renamed")
    expect(card).to_contain_text("Last refreshed")
    titles = [entry["title"] for entry in _owner_entries_in_order(e2e_page, playlist_id)]
    assert titles == [
        "Beta ambient composition · Variation 1",
        "Alpha ambient composition · Variation 1",
    ]

    e2e_page.goto(
        f"{e2e_server.base_url}/beta/playlists/{playlist_id}", wait_until="domcontentloaded"
    )
    e2e_page.get_by_role("button", name="Delete playlist").click()
    expect(e2e_page.get_by_role("heading", name="Playlists")).to_be_visible()
    e2e_page.goto(f"{e2e_server.base_url}/beta/offline", wait_until="domcontentloaded")
    expect(e2e_page.locator(f'[data-offline-owner-id="{playlist_id}"]')).to_have_count(0)

(() => {
  "use strict";
  const init = () => {
    const roots = [...document.querySelectorAll(".media-list")];
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
    roots.forEach((root) => {
      if (root.dataset.libraryBound === "true") return;
      root.dataset.libraryBound = "true";
      root.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        const target = event.target.closest("[data-player-play]");
        if (!target) return;
        event.preventDefault();
        target.click();
      });
      root.addEventListener("click", async (event) => {
        const target = event.target.closest("[data-playlist-move]");
        if (!target) return;
        event.preventDefault();
        const entry = target.closest("[data-playlist-entry]");
        const reorderUrl = root.dataset.reorderUrl;
        if (!entry || !reorderUrl) return;
        const entries = [...root.querySelectorAll("[data-playlist-entry]")];
        const index = entries.indexOf(entry);
        const adjacentIndex = target.dataset.playlistMove === "up" ? index - 1 : index + 1;
        if (index < 0 || adjacentIndex < 0 || adjacentIndex >= entries.length) return;
        if (adjacentIndex < index) entries[adjacentIndex].before(entry);
        else entry.after(entries[adjacentIndex]);
        target.disabled = true;
        try {
          const response = await fetch(reorderUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({
              csrf_token: csrf,
              entry_ids: [...root.querySelectorAll("[data-playlist-entry]")].map((item) => Number(item.dataset.entryId)),
            }),
          });
          if (!response.ok) window.location.reload();
        } catch (_) {
          window.location.reload();
        } finally {
          target.disabled = false;
        }
      });
    });
  };
  init();
  window.addEventListener("audioventura:navigation", init);
})();

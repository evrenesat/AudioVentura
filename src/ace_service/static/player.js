(() => {
  "use strict";

  const playerRoot = document.querySelector("[data-persistent-player]");
  const audio = playerRoot?.querySelector("#global-audio");
  if (!audio || audio.dataset.playerBound === "true") return;
  audio.dataset.playerBound = "true";
  audio.crossOrigin = "use-credentials";

  const titleNode = playerRoot.querySelector("[data-player-title]");
  const projectNode = playerRoot.querySelector("[data-player-project]");
  const currentNode = playerRoot.querySelector("[data-player-current]");
  const durationNode = playerRoot.querySelector("[data-player-duration]");
  const seek = playerRoot.querySelector('[data-player-control="seek"]');
  const playControl = playerRoot.querySelector('[data-player-control="play"]');
  const shuffleControl = playerRoot.querySelector('[data-player-control="shuffle"]');
  const repeatControl = playerRoot.querySelector('[data-player-control="repeat"]');
  const rateControl = playerRoot.querySelector('[data-player-control="rate"]');
  const announcement = playerRoot.querySelector("[data-player-announcement]");
  const storageKey = "audioventura:player:v2";
  const legacyStorageKey = "audioventura:player:v1";
  const validRates = new Set(["0.75", "1", "1.25", "1.5", "2"]);
  let state = {
    queue: [], original: [], index: -1, shuffle: false, repeat: "off", history: [], position: 0,
  };
  let restorePosition = 0;
  let generation = 0;
  let programmaticPauseGeneration = null;
  let manuallyPausedGeneration = null;
  let metadataGeneration = 0;
  const cacheAttempts = new Set();
  let offlineReadyListenerBound = false;

  const finiteNumber = (value, fallback = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };

  const absoluteUrl = (value) => {
    if (!value) return "";
    try { return new URL(value, window.location.href).href; } catch (_) { return ""; }
  };

  const mediaSource = (value) => {
    try {
      const url = new URL(value, window.location.href);
      return `${url.pathname}${url.search}`;
    } catch (_) {
      return value || "";
    }
  };

  const positiveInteger = (value) => {
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : null;
  };

  const contextFromNode = (node) => {
    const source = node.closest("[data-player-context-type]") || node;
    const type = source.dataset.playerContextType || "direct";
    if (type === "playlist") {
      const playlistId = source.dataset.playerPlaylistId;
      if (!playlistId) return { type: "direct" };
      return {
        type: "playlist",
        playlist_id: playlistId,
        playlist_title: source.dataset.playerPlaylistTitle || "Playlist",
        playlist_kind: source.dataset.playerPlaylistKind || "custom",
        server_revision: source.dataset.playerServerRevision || "0".repeat(64),
      };
    }
    return { type: type === "library" ? "library" : "direct" };
  };

  const trackFromNode = (node) => {
    const source = node.closest("[data-player-track]") || node;
    if (!source?.dataset) return null;
    const src = absoluteUrl(source.dataset.playerSrc);
    if (!src) return null;
    return {
      id: source.dataset.mediaId || src,
      title: source.dataset.playerTitle || "Untitled audio",
      project: source.dataset.playerProject || "",
      project_title: source.dataset.playerProject || "",
      project_id: source.dataset.playerProjectId || "",
      src,
      download: absoluteUrl(source.dataset.playerDownload),
      media_url: src,
      download_url: absoluteUrl(source.dataset.playerDownload),
      queue_entry_id: positiveInteger(source.dataset.playerQueueEntryId),
      position: positiveInteger(source.dataset.playerPosition),
      media_item_id: source.dataset.mediaId || null,
      media_file_id: positiveInteger(source.dataset.playerMediaFileId),
      byte_size: positiveInteger(source.dataset.playerByteSize),
      sha256: source.dataset.playerSha256 || "",
      mime_type: source.dataset.playerMimeType || "",
      updated_at: source.dataset.playerUpdatedAt || "",
      media_updated_at: source.dataset.playerUpdatedAt || "",
      _offlineContext: contextFromNode(source),
    };
  };

  const tracksFromDom = () => [...document.querySelectorAll("[data-player-track]")]
    .map(trackFromNode)
    .filter(Boolean);

  const normalizeStoredTrack = (track) => {
    if (!track || typeof track !== "object") return null;
    const src = absoluteUrl(track.src);
    if (!src) return null;
    return {
      ...track,
      id: track.id || src,
      title: track.title || "Untitled audio",
      project: track.project || "",
      project_title: track.project_title || track.project || "",
      project_id: track.project_id || "",
      src,
      download: absoluteUrl(track.download),
      media_url: src,
      download_url: absoluteUrl(track.download_url || track.download),
      queue_entry_id: positiveInteger(track.queue_entry_id),
      media_item_id: track.media_item_id || track.id || null,
      media_file_id: positiveInteger(track.media_file_id),
      byte_size: positiveInteger(track.byte_size),
      sha256: typeof track.sha256 === "string" ? track.sha256 : "",
      mime_type: typeof track.mime_type === "string" ? track.mime_type : "",
      updated_at: typeof track.updated_at === "string" ? track.updated_at : "",
      media_updated_at: typeof track.media_updated_at === "string" ? track.media_updated_at : "",
      _offlineContext: track._offlineContext || { type: "direct" },
    };
  };

  const trackKey = (track) => track?.queue_entry_id
    ? `entry:${track.queue_entry_id}`
    : `media:${track?.media_item_id || track?.id || ""}|${track?.src || ""}`;

  const sameTrack = (left, right) => trackKey(left) === trackKey(right);
  const currentTrack = () => state.queue[state.index] || null;

  const readState = () => {
    let value = null;
    try {
      value = JSON.parse(window.localStorage.getItem(storageKey) || window.localStorage.getItem(legacyStorageKey) || "{}");
    } catch (_) {}
    if (value && typeof value === "object") state = { ...state, ...value };
    state.queue = Array.isArray(state.queue) ? state.queue.map(normalizeStoredTrack).filter(Boolean) : [];
    state.original = Array.isArray(state.original) ? state.original.map(normalizeStoredTrack).filter(Boolean) : [];
    state.history = Array.isArray(state.history) ? state.history.map(normalizeStoredTrack).filter(Boolean).slice(0, 100) : [];
    state.index = Number.isSafeInteger(Number(state.index)) && Number(state.index) >= -1 && Number(state.index) < state.queue.length
      ? Number(state.index) : -1;
    state.shuffle = Boolean(state.shuffle);
    if (state.repeat === true) state.repeat = "one";
    if (!["off", "one", "all"].includes(state.repeat)) state.repeat = "off";
    state.position = Math.max(0, finiteNumber(state.position));
    if (rateControl && validRates.has(String(state.rate))) rateControl.value = String(state.rate);
    audio.defaultPlaybackRate = Number(state.rate) || 1;
    audio.playbackRate = audio.defaultPlaybackRate;
    const savedTrack = currentTrack();
    if (savedTrack) {
      restorePosition = state.position;
      generation += 1;
      metadataGeneration = generation;
      programmaticPauseGeneration = generation;
      audio.pause();
      audio.src = mediaSource(savedTrack.src);
      audio.load();
    }
  };

  const saveState = () => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({
        ...state,
        queue: state.queue.slice(0, 200),
        original: state.original.slice(0, 200),
        history: state.history.slice(0, 100),
        rate: audio.playbackRate,
      }));
    } catch (_) {}
  };

  const formatTime = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };

  const syncProgress = () => {
    if (seek && Number.isFinite(audio.duration) && audio.duration >= 0) seek.max = String(audio.duration);
    if (seek && Number.isFinite(audio.currentTime) && audio.currentTime >= 0) seek.value = String(audio.currentTime);
    if (durationNode) durationNode.textContent = formatTime(audio.duration);
    if (currentNode) currentNode.textContent = formatTime(audio.currentTime);
  };

  const announce = (message) => { if (announcement) announcement.textContent = message; };

  const updateButtons = () => {
    if (playControl) {
      playControl.textContent = audio.paused ? "▶" : "Ⅱ";
      playControl.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
      playControl.title = audio.paused ? "Play" : "Pause";
    }
    if (shuffleControl) shuffleControl.setAttribute("aria-pressed", String(Boolean(state.shuffle)));
    if (repeatControl) {
      const label = state.repeat === "one" ? "Repeat mode: one" : state.repeat === "all" ? "Repeat mode: all" : "Repeat mode: off";
      repeatControl.setAttribute("aria-pressed", String(state.repeat !== "off"));
      repeatControl.setAttribute("aria-label", label);
      repeatControl.title = label;
    }
  };

  const renderTrack = () => {
    const track = currentTrack();
    if (!track) {
      if (titleNode) titleNode.textContent = "Nothing selected";
      if (projectNode) projectNode.textContent = "";
      syncProgress();
      return;
    }
    if (titleNode) titleNode.textContent = track.title;
    if (projectNode) projectNode.textContent = track.project;
    syncProgress();
  };

  const remember = (track) => {
    state.history = [track, ...state.history.filter((item) => !sameTrack(item, track))].slice(0, 100);
  };

  const cacheCurrentTrack = (track = currentTrack()) => {
    if (!track || !track.sha256 || !track.byte_size || track.mime_type !== "audio/mpeg") return;
    const offline = window.AudioventuraOffline;
    if (!offline) {
      if (!offlineReadyListenerBound) {
        offlineReadyListenerBound = true;
        window.addEventListener("audioventura:offline-ready", () => {
          offlineReadyListenerBound = false;
          if (!audio.paused) cacheCurrentTrack();
        }, { once: true });
      }
      return;
    }
    const attempt = `${trackKey(track)}|${track.sha256}`;
    if (cacheAttempts.has(attempt)) return;
    cacheAttempts.add(attempt);
    announce("Saving for offline");
    const context = { ...(track._offlineContext || { type: "direct" }) };
    offline.cacheTrack(track, context)
      .then(() => {
        if (sameTrack(currentTrack(), track)) announce("Available offline");
        window.dispatchEvent(new CustomEvent("audioventura:offline-updated"));
      })
      .catch((error) => {
        if (sameTrack(currentTrack(), track)) {
          const detail = offline.message?.(error?.code) || "Offline copy was not saved";
          announce(detail);
        }
      });
  };

  const playTrack = (track, autoplay) => {
    if (!track) return;
    const nextGeneration = generation + 1;
    programmaticPauseGeneration = generation;
    audio.pause();
    generation = nextGeneration;
    metadataGeneration = generation;
    manuallyPausedGeneration = null;
    state.index = state.queue.findIndex((candidate) => sameTrack(candidate, track));
    state.position = 0;
    restorePosition = 0;
    audio.src = mediaSource(track.src);
    audio.load();
    renderTrack();
    remember(track);
    saveState();
    announce(`${track.title} selected`);
    if (autoplay) {
      const request = audio.play();
      if (request?.catch) request.catch(() => {
        if (generation === nextGeneration) announce("Press play to start audio");
      });
    }
  };

  const setQueueFromDom = (clickedNode = null) => {
    const tracks = tracksFromDom();
    if (!tracks.length) return state.index;
    const clicked = clickedNode ? trackFromNode(clickedNode) : null;
    const clickedKey = clicked && trackKey(clicked);
    const existingIndex = clickedKey ? state.queue.findIndex((track) => trackKey(track) === clickedKey) : -1;
    if (!state.queue.length) {
      state.original = tracks;
      state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - 0.5) : tracks;
      state.index = -1;
    } else if (clicked && existingIndex < 0) {
      // A click on another playlist/detail view establishes that view's queue.
      state.original = tracks;
      state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - 0.5) : tracks;
      state.index = -1;
    }
    if (clickedKey) return state.queue.findIndex((track) => trackKey(track) === clickedKey);
    return state.index;
  };

  const playClicked = (node) => {
    const index = setQueueFromDom(node);
    if (index < 0) return;
    const track = state.queue[index];
    if (state.index === index && audio.src === track.src) {
      manuallyPausedGeneration = null;
      const request = audio.play();
      request?.catch?.(() => {});
    } else {
      playTrack(track, true);
    }
  };

  const playAll = (node) => {
    const tracks = tracksFromDom();
    if (!tracks.length) return;
    state.original = tracks;
    state.shuffle = node.dataset.playerPlayAllShuffle === "true";
    state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - 0.5) : tracks;
    state.index = -1;
    playTrack(state.queue[0], true);
  };

  const move = (step) => {
    if (!state.queue.length) setQueueFromDom();
    if (!state.queue.length) return;
    let next = state.index + step;
    if (state.index < 0) next = step > 0 ? 0 : state.queue.length - 1;
    if (next >= state.queue.length) next = state.repeat === "all" ? 0 : state.queue.length - 1;
    if (next < 0) next = state.repeat === "all" ? state.queue.length - 1 : 0;
    playTrack(state.queue[next], true);
  };

  const loadOfflineOwner = async (playlistId) => {
    const offline = window.AudioventuraOffline;
    if (!offline) return;
    try {
      const owner = await offline.getOwner(playlistId);
      if (!owner?.playlist || !owner.entries?.length) {
        announce("No saved tracks are available");
        return;
      }
      const context = owner.playlist.kind === "local-played"
        ? { type: "direct" }
        : {
          type: "playlist",
          playlist_id: owner.playlist.id,
          playlist_title: owner.playlist.title,
          playlist_kind: owner.playlist.kind,
          server_revision: owner.playlist.server_revision || "0".repeat(64),
        };
      const tracks = owner.entries.map((entry) => ({
        ...normalizeStoredTrack({
          ...entry,
          id: entry.media_item_id,
          src: entry.media_url,
          download: entry.download_url,
          project: entry.project_title,
          _offlineContext: context,
        }),
        _offlineContext: context,
      })).filter(Boolean);
      state.original = tracks;
      state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - 0.5) : tracks;
      state.index = -1;
      playTrack(state.queue[0], false);
      announce(`${owner.playlist.title} loaded from this device`);
    } catch (error) {
      announce(offline.message?.(error?.code) || "Saved tracks could not be opened");
    }
  };

  audio.addEventListener("loadedmetadata", () => {
    if (metadataGeneration !== generation) return;
    syncProgress();
    if (restorePosition > 0 && restorePosition < audio.duration) audio.currentTime = restorePosition;
    restorePosition = 0;
    renderTrack();
  });
  audio.addEventListener("durationchange", syncProgress);
  audio.addEventListener("loadeddata", syncProgress);
  audio.addEventListener("canplay", syncProgress);
  audio.addEventListener("timeupdate", () => {
    state.position = audio.currentTime || 0;
    syncProgress();
    saveState();
  });
  audio.addEventListener("play", () => {
    if (manuallyPausedGeneration === generation && !audio.ended) {
      audio.pause();
      return;
    }
    manuallyPausedGeneration = null;
    updateButtons();
    cacheCurrentTrack();
  });
  audio.addEventListener("pause", () => {
    if (programmaticPauseGeneration === generation) {
      programmaticPauseGeneration = null;
    } else if (!audio.ended) {
      manuallyPausedGeneration = generation;
    }
    updateButtons();
  });
  audio.addEventListener("error", () => {
    if (!audio.src) return;
    announce(navigator.onLine ? "This track could not be played" : "This track is unavailable offline");
    updateButtons();
  });
  audio.addEventListener("ended", () => {
    if (manuallyPausedGeneration === generation || !audio.ended) return;
    if (state.repeat === "one") playTrack(currentTrack(), true);
    else if (state.index + 1 < state.queue.length) playTrack(state.queue[state.index + 1], true);
    else if (state.repeat === "all") playTrack(state.queue[0], true);
    else updateButtons();
  });

  document.addEventListener("click", (event) => {
    const playAllControl = event.target.closest("[data-player-play-all]");
    if (playAllControl) { event.preventDefault(); playAll(playAllControl); return; }
    const target = event.target.closest("[data-player-play]");
    if (target && !target.disabled) { event.preventDefault(); playClicked(target); }
  });
  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-player-control]");
    if (!control) return;
    const action = control.dataset.playerControl;
    if (action === "play") {
      if (!state.queue.length) setQueueFromDom();
      if (audio.src && !audio.paused) audio.pause();
      else if (audio.src) { manuallyPausedGeneration = null; audio.play().catch(() => {}); }
      else if (state.queue.length) playTrack(state.queue[0], true);
    }
    if (action === "previous") move(-1);
    if (action === "next") move(1);
    if (action === "shuffle") {
      const current = currentTrack();
      state.shuffle = !state.shuffle;
      const base = state.original.length ? state.original : tracksFromDom();
      state.original = base;
      state.queue = state.shuffle ? [...base].sort(() => Math.random() - 0.5) : [...base];
      state.index = current ? state.queue.findIndex((track) => sameTrack(track, current)) : -1;
      saveState();
    }
    if (action === "repeat") {
      state.repeat = state.repeat === "off" ? "one" : state.repeat === "one" ? "all" : "off";
      announce(`Repeat mode: ${state.repeat}`);
      saveState();
    }
    updateButtons();
  });
  seek?.addEventListener("input", () => {
    if (Number.isFinite(audio.duration)) {
      audio.currentTime = Number(seek.value);
      state.position = audio.currentTime;
      saveState();
    }
  });
  const setPlaybackRate = () => {
    const rate = Number(rateControl?.value) || 1;
    audio.defaultPlaybackRate = rate;
    audio.playbackRate = rate;
    saveState();
  };
  rateControl?.addEventListener("input", setPlaybackRate);
  rateControl?.addEventListener("change", setPlaybackRate);
  document.addEventListener("change", (event) => { if (event.target === rateControl) setPlaybackRate(); });
  window.addEventListener("audioventura:navigation", () => {
    if (!state.queue.length) setQueueFromDom();
    renderTrack();
    updateButtons();
  });
  window.addEventListener("audioventura:offline-play", (event) => loadOfflineOwner(event.detail?.playlistId));
  window.addEventListener("audioventura:offline-unavailable", () => {
    if (audio.paused) announce("Offline storage is unavailable; online playback remains available");
  });

  readState();
  updateButtons();
  renderTrack();
  window.AudioventuraPlayer = {
    refresh: () => window.dispatchEvent(new Event("audioventura:navigation")),
    loadOfflineOwner,
    getState: () => ({ ...state, current: currentTrack() }),
  };
})();

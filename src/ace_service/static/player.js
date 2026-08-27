(() => {
  "use strict";

  const playerRoot = document.querySelector("[data-persistent-player]");
  const audio = playerRoot?.querySelector("#global-audio");
  if (!audio || audio.dataset.playerBound === "true") return;
  audio.dataset.playerBound = "true";
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
  const storageKey = "audioventura:player:v1";
  let state = { queue: [], original: [], index: -1, shuffle: false, repeat: "off", history: [], position: 0 };
  let restorePosition = 0;

  const readState = () => {
    try {
      const value = JSON.parse(window.localStorage.getItem(storageKey) || "{}");
      if (value && typeof value === "object") state = { ...state, ...value };
    } catch (_) {}
    if (!Array.isArray(state.history)) state.history = [];
    if (state.repeat === true) state.repeat = "one";
    if (!["off", "one", "all"].includes(state.repeat)) state.repeat = "off";
    if (!Number.isFinite(Number(state.position)) || Number(state.position) < 0) state.position = 0;
    if (rateControl && ["0.75", "1", "1.25", "1.5", "2"].includes(String(state.rate))) rateControl.value = String(state.rate);
    audio.playbackRate = Number(state.rate) || 1;
    const savedTrack = state.queue[state.index];
    if (savedTrack && savedTrack.src) {
      restorePosition = Number(state.position) || 0;
      audio.src = savedTrack.src;
      audio.load();
    }
  };
  const saveState = () => {
    try { window.localStorage.setItem(storageKey, JSON.stringify({ ...state, rate: audio.playbackRate })); } catch (_) {}
  };
  const formatTime = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const total = Math.floor(seconds);
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };
  const syncProgress = () => {
    if (seek && Number.isFinite(audio.duration) && audio.duration >= 0) {
      seek.max = String(audio.duration);
    }
    if (seek && Number.isFinite(audio.currentTime) && audio.currentTime >= 0) {
      seek.value = String(audio.currentTime);
    }
    if (durationNode) durationNode.textContent = formatTime(audio.duration);
    if (currentNode) currentNode.textContent = formatTime(audio.currentTime);
  };
  const announce = (message) => { if (announcement) announcement.textContent = message; };
  const tracksFromDom = () => [...document.querySelectorAll("[data-player-track]")]
    .map((node) => ({
      id: node.dataset.mediaId || node.dataset.playerSrc,
      title: node.dataset.playerTitle || "Untitled audio",
      project: node.dataset.playerProject || "",
      src: node.dataset.playerSrc || "",
      download: node.dataset.playerDownload || "",
    }))
    .filter((track) => track.src);
  const updateButtons = () => {
    if (playControl) {
      playControl.textContent = audio.paused ? "▶" : "Ⅱ";
      playControl.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
      playControl.title = audio.paused ? "Play" : "Pause";
    }
    if (shuffleControl) shuffleControl.setAttribute("aria-pressed", String(Boolean(state.shuffle)));
    if (repeatControl) {
      repeatControl.setAttribute("aria-pressed", String(state.repeat !== "off"));
      const label = state.repeat === "one" ? "Repeat mode: one" : state.repeat === "all" ? "Repeat mode: all" : "Repeat mode: off";
      repeatControl.setAttribute("aria-label", label);
      repeatControl.title = label;
    }
  };
  const renderTrack = () => {
    const track = state.queue[state.index];
    if (!track) {
      if (titleNode) titleNode.textContent = "Nothing selected";
      if (projectNode) projectNode.textContent = "";
      return;
    }
    if (titleNode) titleNode.textContent = track.title;
    if (projectNode) projectNode.textContent = track.project;
    syncProgress();
  };
  const remember = (track) => {
    state.history = [track, ...state.history.filter((item) => item.src !== track.src)].slice(0, 100);
  };
  const loadTrack = (index, autoplay) => {
    const track = state.queue[index];
    if (!track) return;
    state.index = index;
    state.position = 0;
    restorePosition = 0;
    audio.src = track.src;
    audio.load();
    renderTrack();
    remember(track);
    saveState();
    announce(`${track.title} selected`);
    if (autoplay) audio.play().catch(() => announce("Press play to start audio"));
  };
  const setQueueFromDom = (clicked) => {
    const tracks = tracksFromDom();
    if (!tracks.length) return -1;
    const clickedId = clicked && (clicked.dataset.mediaId || clicked.dataset.playerSrc);
    const existing = state.queue[state.index];
    const sameQueue = state.queue.length && tracks.length === state.queue.length && tracks.every((item, index) => item.src === state.queue[index].src);
    if (!sameQueue && (!existing || audio.paused)) {
      state.original = tracks;
      state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - .5) : tracks;
      state.index = -1;
    }
    if (!state.queue.length) { state.original = tracks; state.queue = tracks; }
    return state.queue.findIndex((item) => clickedId && (item.id === clickedId || item.src === clickedId));
  };
  const playClicked = (node) => {
    const index = setQueueFromDom(node.closest("[data-player-track]") || node);
    if (index < 0) return;
    const track = state.queue[index];
    if (state.index === index && audio.src === new URL(track.src, window.location.href).href) audio.play().catch(() => {});
    else loadTrack(index, true);
  };
  const playAll = (node) => {
    const tracks = tracksFromDom();
    if (!tracks.length) return;
    const shuffled = node.dataset.playerPlayAllShuffle === "true";
    state.original = tracks;
    state.shuffle = shuffled;
    state.queue = shuffled ? [...tracks].sort(() => Math.random() - .5) : tracks;
    loadTrack(0, true);
  };
  const move = (step) => {
    if (!state.queue.length) setQueueFromDom(null);
    if (!state.queue.length) return;
    let next = state.index + step;
    if (next >= state.queue.length) next = state.repeat === "all" ? 0 : state.queue.length - 1;
    if (next < 0) next = state.repeat === "all" ? state.queue.length - 1 : 0;
    loadTrack(next, true);
  };

  readState();
  document.addEventListener("click", (event) => {
    const playAllControl = event.target.closest("[data-player-play-all]");
    if (playAllControl) { event.preventDefault(); playAll(playAllControl); return; }
    const target = event.target.closest("[data-player-play]");
    if (target) { event.preventDefault(); playClicked(target); }
  });
  document.addEventListener("click", (event) => {
    const control = event.target.closest("[data-player-control]");
    if (!control) return;
    const action = control.dataset.playerControl;
    if (action === "play") {
      if (!state.queue.length) setQueueFromDom(null);
      if (audio.src && !audio.paused) audio.pause();
      else if (audio.src) audio.play().catch(() => {});
      else if (state.queue.length) loadTrack(0, true);
    }
    if (action === "previous") move(-1);
    if (action === "next") move(1);
    if (action === "shuffle") {
      const currentSrc = state.queue[state.index]?.src || "";
      state.shuffle = !state.shuffle;
      const base = state.original.length ? state.original : tracksFromDom();
      state.original = base;
      state.queue = state.shuffle ? [...base].sort(() => Math.random() - .5) : [...base];
      state.index = currentSrc ? state.queue.findIndex((item) => item.src === currentSrc) : -1;
      saveState();
    }
    if (action === "repeat") {
      state.repeat = state.repeat === "off" ? "one" : state.repeat === "one" ? "all" : "off";
      announce(`Repeat mode: ${state.repeat}`);
      saveState();
    }
    updateButtons();
  });
  seek?.addEventListener("input", () => { if (Number.isFinite(audio.duration)) audio.currentTime = Number(seek.value); });
  const setPlaybackRate = () => {
    const rate = Number(rateControl?.value) || 1;
    audio.defaultPlaybackRate = rate;
    audio.playbackRate = rate;
    saveState();
  };
  rateControl?.addEventListener("input", setPlaybackRate);
  rateControl?.addEventListener("change", setPlaybackRate);
  document.addEventListener("change", (event) => {
    if (event.target === rateControl) setPlaybackRate();
  });
  audio.addEventListener("loadedmetadata", () => {
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
  audio.addEventListener("play", updateButtons);
  audio.addEventListener("pause", updateButtons);
  audio.addEventListener("ended", () => {
    if (state.repeat === "one") loadTrack(state.index, true);
    else if (state.index + 1 < state.queue.length) loadTrack(state.index + 1, true);
    else if (state.repeat === "all") loadTrack(0, true);
    else updateButtons();
  });
  window.addEventListener("audioventura:navigation", () => {
    if (!state.queue.length || audio.paused) {
      const tracks = tracksFromDom();
      if (tracks.length) { state.original = tracks; state.queue = state.shuffle ? [...tracks].sort(() => Math.random() - .5) : tracks; state.index = -1; }
    }
  });
  updateButtons();
  renderTrack();
  window.AudioventuraPlayer = { refresh: () => window.dispatchEvent(new Event("audioventura:navigation")) };
})();

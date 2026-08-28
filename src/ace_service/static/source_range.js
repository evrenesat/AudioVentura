(() => {
  "use strict";

  const range = document.querySelector("[data-source-range]");
  const form = document.querySelector("[data-remix-form]");
  const select = form?.querySelector("#backend");
  const start = document.querySelector("#clip-start-seconds");
  const end = document.querySelector("#clip-end-seconds");
  const limit = range?.querySelector("[data-range-limit]");
  const confirmation = form?.querySelector("[data-long-range-confirm]");
  const generate = form?.querySelector("[data-generate-remix]");
  const audio = document.querySelector("#global-audio");
  if (!range || !form || !select || !start || !end || !generate) return;

  let choices = [];
  try { choices = JSON.parse(select.dataset.backendCapabilities || "[]"); } catch (_) { choices = []; }
  const duration = Number(range.dataset.sourceDuration);
  const selectedChoice = () => choices.find((item) => item.backend_id === select.value);
  const number = (input) => {
    const value = Number(input.value);
    return Number.isFinite(value) ? value : null;
  };
  const update = () => {
    const choice = selectedChoice();
    const snapshot = choice?.snapshot || {};
    const minimum = Number(snapshot.source_duration_min_seconds);
    const maximum = Number(snapshot.source_duration_max_seconds);
    const validBounds = Number.isFinite(minimum) && Number.isFinite(maximum) && maximum >= minimum;
    const sourceIsLong = validBounds && duration > maximum + 0.001;
    const startValue = number(start);
    const endValue = number(end);
    const validRange = validBounds && startValue !== null && endValue !== null && startValue >= 0 && endValue > startValue && endValue <= duration + 0.001 && endValue - startValue >= minimum - 0.001 && endValue - startValue <= maximum + 0.001;
    if (limit) {
      limit.textContent = validBounds
        ? `${choice.label}: accepted clip length ${minimum}–${maximum} seconds${sourceIsLong ? ". This source is longer; confirm the selected range." : "."}`
        : "This backend has no reviewed source-duration contract.";
    }
    if (confirmation) confirmation.hidden = !sourceIsLong;
    const confirmed = !sourceIsLong || confirmation?.querySelector("input")?.checked;
    generate.disabled = !(validRange && confirmed);
    generate.title = validRange ? (confirmed ? "Generate the selected range" : "Confirm the selected range first") : "Choose a backend-valid source range";
  };
  const setCurrent = (which) => {
    if (!audio || !Number.isFinite(audio.currentTime)) return;
    const input = which === "start" ? start : end;
    input.value = Math.max(0, Math.min(duration, Math.round(audio.currentTime * 10) / 10)).toFixed(1);
    update();
  };
  const preview = () => {
    if (!audio) return;
    const track = document.querySelector("[data-source-range] [data-player-src]");
    if (track?.dataset.playerSrc && (!audio.src || audio.src !== new URL(track.dataset.playerSrc, window.location.href).href)) {
      audio.src = track.dataset.playerSrc;
      audio.load();
    }
    const startValue = number(start);
    const endValue = number(end);
    if (startValue === null || endValue === null || endValue <= startValue) return;
    const seek = () => { audio.currentTime = startValue; audio.play().catch(() => {}); };
    if (audio.readyState >= 1) seek(); else audio.addEventListener("loadedmetadata", seek, { once: true });
  };
  document.querySelectorAll("[data-set-range]").forEach((button) => button.addEventListener("click", () => setCurrent(button.dataset.setRange)));
  document.querySelector("[data-preview-range]")?.addEventListener("click", preview);
  [start, end].forEach((input) => input.addEventListener("input", update));
  confirmation?.querySelector("input")?.addEventListener("change", update);
  select.addEventListener("change", update);
  update();
})();

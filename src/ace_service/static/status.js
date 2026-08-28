(() => {
  "use strict";
  if (window.__audioventuraStatusCleanup) window.__audioventuraStatusCleanup();
  const detail = document.querySelector("[data-status-url]");
  if (!detail) { window.__audioventuraStatusCleanup = null; return; }
  const statusNode = document.querySelector("#job-status");
  const progressNode = document.querySelector("#job-progress");
  const phaseNode = document.querySelector("#job-phase");
  const outputsNode = document.querySelector("#outputs");
  const terminal = new Set(["completed", "failed", "cancelled"]);
  let confirmationReloaded = false;
  let timer = null;
  let stopped = false;
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
  const outputHtml = (output) => {
    if (output.deleted) return `<div class="output-card"><div><strong>Deleted audio</strong><span>Variation ${escapeHtml(output.variation_index)}</span></div><span class="muted">This media was deleted.</span></div>`;
    const title = output.legacy ? `Variation ${escapeHtml(output.variation_index)}` : escapeHtml(output.title);
    const source = escapeHtml(output.media_url || "");
    const download = output.download_url ? `<a href="${escapeHtml(output.download_url)}">Download primary</a>` : "";
    if (!output.legacy && output.preparing_mp3) {
      const error = output.derivative_error ? `<span class="muted">Preparing player version failed: ${escapeHtml(output.derivative_error)}</span>` : `<span class="muted">MP3 player version is being prepared.</span>`;
      const retry = output.derivative_retry_url ? `<form method="post" action="${escapeHtml(output.derivative_retry_url)}"><input type="hidden" name="csrf_token" value="${escapeHtml(detail.dataset.csrfToken || "")}"><button class="button small secondary" type="submit">Retry MP3</button></form>` : "";
      return `<div class="output-card"><div><strong>Preparing MP3</strong><span>${escapeHtml(output.size_label)} · ${escapeHtml(output.mime_type)}</span></div>${error}${download}${retry}</div>`;
    }
    const play = output.media_url ? `<button class="play-button" type="button" data-player-track data-player-title="${title}" data-player-src="${source}"${output.legacy ? ` src="${source}"` : ""} data-player-play aria-label="Play ${title}">▶ Play</button>` : "";
    return `<div class="output-card"><div><strong>${title}</strong><span>${escapeHtml(output.size_label)} · ${escapeHtml(output.mime_type)}</span></div>${play}${download}</div>`;
  };
  const refresh = async () => {
    if (stopped) return;
    try {
      const response = await fetch(detail.dataset.statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!response.ok) return;
      const job = await response.json();
      if (job.cover_confirmation_status === "awaiting_confirmation" && !document.querySelector("[data-cover-confirmation-form]") && !confirmationReloaded) { confirmationReloaded = true; window.location.reload(); return; }
      if (statusNode) { statusNode.textContent = job.status_label; statusNode.className = `status status-${job.status}`; }
      if (progressNode) progressNode.textContent = `${job.completed_variations}/${job.variation_count} variations`;
      if (phaseNode) { phaseNode.hidden = !(job.phase && job.phase_label); phaseNode.textContent = job.phase && job.phase_label ? `${job.phase_detail_label || job.phase_label} · ${job.elapsed_seconds} seconds elapsed` : ""; }
      if (job.error) {
        let errorNode = document.querySelector("#job-error");
        if (!errorNode) { errorNode = document.createElement("div"); errorNode.id = "job-error"; errorNode.className = "banner error"; detail.prepend(errorNode); }
        errorNode.textContent = job.error;
      }
      if (outputsNode && job.outputs && job.outputs.length) outputsNode.innerHTML = job.outputs.map(outputHtml).join("");
      if (!terminal.has(job.status)) timer = window.setTimeout(refresh, 2000);
    } catch (_) { if (!stopped) timer = window.setTimeout(refresh, 5000); }
  };
  window.__audioventuraStatusCleanup = () => { stopped = true; if (timer) window.clearTimeout(timer); };
  timer = window.setTimeout(refresh, 1000);
})();

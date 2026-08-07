(() => {
  const detail = document.querySelector("[data-status-url]");
  if (!detail) return;
  const statusNode = document.querySelector("#job-status");
  const progressNode = document.querySelector("#job-progress");
  const outputsNode = document.querySelector("#outputs");
  const terminal = new Set(["completed", "failed"]);
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
  const refresh = async () => {
    try {
      const response = await fetch(detail.dataset.statusUrl, {headers: {Accept: "application/json"}, credentials: "same-origin"});
      if (!response.ok) return;
      const job = await response.json();
      statusNode.textContent = job.status_label;
      statusNode.className = `status status-${job.status}`;
      progressNode.textContent = `${job.completed_variations}/${job.variation_count} variations`;
      if (job.error) {
        let errorNode = document.querySelector("#job-error");
        if (!errorNode) { errorNode = document.createElement("div"); errorNode.id = "job-error"; errorNode.className = "banner error"; detail.prepend(errorNode); }
        errorNode.textContent = job.error;
      }
      if (job.outputs && job.outputs.length) {
        outputsNode.innerHTML = job.outputs.map((output) => `<div class="output-card"><div><strong>Variation ${escapeHtml(output.variation_index)}</strong><span>${escapeHtml(output.size_label)} · ${escapeHtml(output.mime_type)}</span></div><audio controls preload="metadata" src="${escapeHtml(output.media_url)}">Your browser cannot play this audio.</audio><a href="${escapeHtml(output.download_url)}">Download</a></div>`).join("");
      }
      if (!terminal.has(job.status)) window.setTimeout(refresh, 2000);
    } catch (_) { window.setTimeout(refresh, 5000); }
  };
  window.setTimeout(refresh, 1000);
})();

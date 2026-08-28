(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const uploadForm = document.querySelector("[data-source-upload-form]");
  const statusPage = document.querySelector("[data-source-status-url]");
  const panels = [...document.querySelectorAll("[data-source-panel]")];
  const tabs = [...document.querySelectorAll("[data-source-tab]")];
  const terminal = new Set(["ready", "failed", "cancelled"]);
  let timer = null;
  let xhr = null;
  let sourceFile = null;
  let uploadURL = null; // Capability URLs exist only in this closure.
  let statusURL = statusPage?.dataset.sourceStatusUrl || null;
  let completeURL = null;
  let cancelURL = null;
  let lastStatus = null;

  const formatBytes = (value) => {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  };
  const statusLabel = document.querySelector("[data-source-status-label]");
  const statusMessage = document.querySelector("[data-source-status-message]");
  const progress = document.querySelector("[data-source-progress]");
  const progressLabel = document.querySelector("[data-source-progress-label]");
  const fileLabel = document.querySelector("[data-source-file-label]");
  const uploadStatus = uploadForm?.querySelector("[data-source-status]");
  const cancelButton = uploadForm?.querySelector("[data-upload-cancel]");
  const retryButton = uploadForm?.querySelector("[data-upload-retry]");
  const startButton = uploadForm?.querySelector("[data-upload-start]");
  const fileInput = uploadForm?.querySelector("[data-source-file]");

  const setText = (node, value) => { if (node) node.textContent = value; };
  const show = (node, visible) => { if (node) node.hidden = !visible; };
  const setProgress = (loaded, total) => {
    const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
    if (progress) progress.value = percent;
    setText(progressLabel, `${percent}% · ${formatBytes(loaded)}${total ? ` of ${formatBytes(total)}` : ""}`);
  };
  const renderStatus = (body) => {
    if (!body || typeof body !== "object") return;
    lastStatus = body.status || lastStatus;
    if (body.status_url) statusURL = body.status_url;
    if (body.cancel_url) cancelURL = body.cancel_url;
    setText(statusLabel, body.status_label || body.status || "Processing source");
    const declared = Number(body.declared_byte_size) || 0;
    const received = Number(body.received_byte_size) || 0;
    const uploadPercent = Number.isFinite(Number(body.upload_progress)) ? Number(body.upload_progress) : (declared ? Math.round((received / declared) * 100) : 0);
    if (progress) progress.value = Math.max(0, Math.min(100, uploadPercent));
    setText(progressLabel, `${Math.max(0, Math.min(100, uploadPercent))}% · ${formatBytes(received)}${declared ? ` of ${formatBytes(declared)}` : ""}`);
    if (fileLabel && body.filename) fileLabel.textContent = `${body.filename}${body.declared_size_label ? ` · ${body.declared_size_label}` : ""}`;
    if (statusMessage) {
      statusMessage.textContent = body.error || ({
        preparing: "Extracting the first audio stream and creating a canonical MP3 at Home Ingest.",
        uploaded: "Upload received. Preparing the complete source before generation.",
        queued: "Source queued for preparation.",
        ready: "The complete source is ready to play and remix.",
        cancelled: "This source was cancelled.",
      }[body.status] || "");
    }
    show(cancelButton, ["awaiting_upload", "uploaded", "queued", "preparing"].includes(body.status));
    show(retryButton, ["failed", "cancelled"].includes(body.status));
    if (body.status === "ready" && uploadForm) show(uploadForm.querySelector("[data-upload-start]"), false);
    if (body.status === "ready" || body.status === "failed" || body.status === "cancelled") {
      if (timer) window.clearTimeout(timer);
      timer = null;
    }
  };
  const poll = async () => {
    if (!statusURL) return;
    try {
      const response = await fetch(statusURL, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.ok) {
        const body = await response.json();
        renderStatus(body);
        if (body.status && !terminal.has(body.status)) timer = window.setTimeout(poll, 2000);
      }
    } catch (_) {
      timer = window.setTimeout(poll, 5000);
    }
  };
  const postJSON = async (url, payload) => fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify({ csrf_token: csrf, ...(payload || {}) }),
  });
  const showUploadError = (message) => {
    setText(statusLabel, "Upload needs attention");
    setText(statusMessage, message);
    show(retryButton, true);
    show(cancelButton, false);
  };
  const sendUpload = () => {
    if (!sourceFile || !uploadURL) return;
    xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadURL, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) setProgress(event.loaded, event.total);
    });
    xhr.onload = async () => {
      const responseStatus = xhr.status;
      xhr = null;
      if (responseStatus < 200 || responseStatus >= 300) {
        showUploadError(responseStatus === 413 ? "This file is larger than the 512 MiB limit." : "The upload did not complete. You can retry from byte zero.");
        return;
      }
      if (completeURL) {
        try { await postJSON(completeURL); } catch (_) { /* status polling is authoritative */ }
      }
      show(cancelButton, false);
      show(retryButton, false);
      poll();
    };
    xhr.onerror = () => { xhr = null; showUploadError("The upload connection was interrupted. Retry to send the file again."); };
    xhr.onabort = () => { xhr = null; showUploadError("Upload cancelled."); };
    show(cancelButton, true);
    show(retryButton, false);
    setText(statusLabel, "Uploading source");
    xhr.send(sourceFile);
  };
  const startUpload = async () => {
    if (!uploadForm || !fileInput?.files?.length) return;
    sourceFile = fileInput.files[0];
    const maxBytes = 536870912;
    if (!sourceFile.size || sourceFile.size > maxBytes) {
      show(uploadStatus, true);
      showUploadError(sourceFile.size ? "This file is larger than the 512 MiB limit." : "Empty files cannot be uploaded.");
      return;
    }
    if (!uploadForm.reportValidity()) return;
    show(uploadStatus, true);
    if (fileLabel) fileLabel.textContent = `${sourceFile.name} · ${formatBytes(sourceFile.size)}`;
    setProgress(0, sourceFile.size);
    show(startButton, false);
    try {
      const response = await fetch(uploadForm.dataset.uploadInitUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ csrf_token: csrf, project_title: uploadForm.elements.project_title.value, filename: sourceFile.name, byte_size: sourceFile.size, rights_confirmation: uploadForm.elements.rights_confirmation.checked }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.upload_url) throw new Error("Upload initialization failed. Check the project title and rights confirmation.");
      statusURL = body.status_url;
      completeURL = body.upload_complete_url;
      cancelURL = body.cancel_url || cancelURL;
      // Never put the capability URL in a DOM attribute, form value, history entry, or log.
      uploadURL = body.upload_url;
      sendUpload();
    } catch (error) {
      show(startButton, true);
      showUploadError(error.message || "Upload initialization failed.");
    }
  };
  const cancelUpload = async () => {
    if (xhr) xhr.abort();
    if (cancelURL) {
      try { await postJSON(cancelURL); } catch (_) { /* a later cleanup pass remains safe */ }
    }
    show(cancelButton, false);
    show(retryButton, true);
  };
  const retryUpload = () => {
    if (sourceFile && uploadURL) sendUpload();
    else showUploadError("Choose the original file again to retry this upload.");
  };

  tabs.forEach((tab) => tab.addEventListener("click", () => {
    const selected = tab.dataset.sourceTab;
    tabs.forEach((item) => { const active = item === tab; item.setAttribute("aria-selected", String(active)); item.classList.toggle("secondary", !active); });
    panels.forEach((panel) => { panel.hidden = panel.dataset.sourcePanel !== selected; });
  }));
  uploadForm?.addEventListener("submit", (event) => { event.preventDefault(); startUpload(); });
  cancelButton?.addEventListener("click", cancelUpload);
  retryButton?.addEventListener("click", retryUpload);
  if (statusURL) poll();
})();

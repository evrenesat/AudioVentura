(() => {
  "use strict";

  if (window.AudioventuraOffline) return;
  const Store = window.AudioventuraOfflineStore;
  if (!Store) return;

  const scope = Store.scopeFromPage();
  const scopeKey = Store.scopeKey(scope);
  const MEDIA_CACHE_NAME = `audioventura:${scopeKey}:media:v1`;
  const SHELL_CACHE_PREFIX = `audioventura:${scopeKey}:shell:`;
  const isOfflineShell = document.querySelector('meta[name="offline-shell"]')?.content === "true";
  const TAB_OWNER = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const LEASE_MS = 30_000;
  const DOWNLOAD_CONCURRENCY = 2;
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(`audioventura:${scopeKey}:offline`) : null;
  let handle = null;
  let mediaCache = null;
  let initialized = null;
  const activeOperations = new Map();
  let lastStorageStatus = null;
  let sawOfflineSignal = navigator.onLine === false;

  const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  const nowIso = () => new Date().toISOString();
  const message = (code) => ({
    storage_unavailable: "Offline storage is unavailable in this browser profile.",
    bad_queue: "This playlist cannot be saved because its metadata is invalid.",
    bad_url: "This track is not a same-origin library MP3.",
    bad_type: "Only verified MP3 playback files can be saved offline.",
    bad_length: "The server returned an unexpected MP3 size.",
    bad_identity: "The server returned an unexpected MP3 identity.",
    quota: "The browser stopped the download because storage is full.",
    quota_insufficient: "There is not enough reported browser storage for this download.",
    offline: "Reconnect to save this track offline.",
    unauthorized: "Your session expired. Reconnect and sign in again.",
    not_found: "This track is no longer available online.",
    server: "The server could not provide this track right now.",
    missing: "The saved MP3 is missing from browser storage.",
    lease_expired: "A previous download stopped and can be retried.",
    cancelled: "Download cancelled; completed tracks remain saved.",
    network: "The network interrupted this download.",
  }[code] || "Offline download could not be completed.");

  const errorCode = (error, fallback = "unknown") => {
    if (error?.code) return error.code;
    if (error?.name === "QuotaExceededError") return "quota";
    if (error?.name === "AbortError") return "cancelled";
    return fallback;
  };

  const cacheRequest = (hash) => new Request(Store.cacheUrl(scope, hash), { credentials: "same-origin" });
  const etagFor = (hash) => `"sha256-${hash}"`;

  const responseMatches = (response, item) => {
    if (!response || response.status !== 200) return false;
    const type = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
    const length = response.headers.get("content-length");
    return type === "audio/mpeg" && length === String(item.byte_size) && response.headers.get("etag") === etagFor(item.sha256);
  };

  const itemFromEntry = (entry) => ({
    id: entry.media_item_id,
    queue_entry_id: entry.queue_entry_id,
    position: entry.position,
    media_item_id: entry.media_item_id,
    media_file_id: entry.media_file_id,
    title: entry.title,
    project_id: entry.project_id,
    project_title: entry.project_title,
    duration_seconds: entry.duration_seconds,
    mime_type: entry.mime_type,
    byte_size: entry.byte_size,
    sha256: entry.sha256,
    updated_at: entry.updated_at,
    media_updated_at: entry.updated_at,
    media_url: entry.media_url,
    download_url: entry.download_url,
  });

  const openStorage = async () => {
    if (initialized) return initialized;
    initialized = (async () => {
      handle = await Store.open(scope);
      if (!window.caches) throw Store.boundedError("storage_unavailable");
      mediaCache = await window.caches.open(MEDIA_CACHE_NAME);
      await reconcile();
      return handle;
    })().catch((error) => {
      initialized = null;
      throw error;
    });
    return initialized;
  };

  const markMissing = async (item) => {
    try { await Store.updateBlob(handle, item.sha256, { state: "failed", last_error_code: "missing", lease_owner: null, lease_expires_at: 0 }); } catch (_) {}
  };

  const cachedItem = async (item) => {
    const row = await Store.getBlob(handle, item.sha256);
    if (!row || row.state !== "ready") return false;
    const response = await mediaCache.match(cacheRequest(item.sha256));
    if (!responseMatches(response, item)) {
      await markMissing(item);
      return false;
    }
    await Store.updateBlob(handle, item.sha256, { last_used_at: nowIso(), last_error_code: null });
    return true;
  };

  const statusForResponse = (response) => {
    if (response.status === 401 || response.status === 403) return "unauthorized";
    if (response.status === 404) return "not_found";
    if (response.status >= 500) return "server";
    return "network";
  };

  const validateResponse = (response, item) => {
    if (response.status !== 200) throw Store.boundedError(statusForResponse(response));
    if ((response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase() !== "audio/mpeg") throw Store.boundedError("bad_type");
    if (response.headers.get("content-length") !== String(item.byte_size)) throw Store.boundedError("bad_length");
    if (response.headers.get("etag") !== etagFor(item.sha256)) throw Store.boundedError("bad_identity");
  };

  const readAndPut = async (request, response, onProgress) => {
    if (!response.body || !response.body.tee) {
      await mediaCache.put(request, response);
      onProgress?.(response.headers.get("content-length") ? Number(response.headers.get("content-length")) : 0);
      return;
    }
    const [cacheBody, progressBody] = response.body.tee();
    const cacheResponse = new Response(cacheBody, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
    let transferred = 0;
    const countBody = async () => {
      const reader = progressBody.getReader();
      try {
        while (true) {
          const next = await reader.read();
          if (next.done) break;
          transferred += next.value?.byteLength || 0;
          onProgress?.(transferred);
        }
      } finally {
        reader.releaseLock();
      }
    };
    await Promise.all([mediaCache.put(request, cacheResponse), countBody()]);
  };

  const waitForLease = async (item, signal) => {
    for (let attempt = 0; attempt < 160; attempt += 1) {
      if (signal?.aborted) throw Store.boundedError("cancelled");
      if (await cachedItem(item)) return true;
      const row = await Store.getBlob(handle, item.sha256);
      if (!row || !row.lease_owner || Number(row.lease_expires_at) <= Date.now()) return false;
      await wait(200);
    }
    return false;
  };

  const downloadOne = async (item, { signal, onProgress } = {}) => {
    if (await cachedItem(item)) return { sha256: item.sha256, byte_size: item.byte_size, reused: true };
    let lease = await Store.claimLease(handle, item.sha256, TAB_OWNER, LEASE_MS);
    if (!lease.claimed) {
      if (lease.ready && await cachedItem(item)) return { sha256: item.sha256, byte_size: item.byte_size, reused: true };
      if (await waitForLease(item, signal)) return { sha256: item.sha256, byte_size: item.byte_size, reused: true };
      lease = await Store.claimLease(handle, item.sha256, TAB_OWNER, LEASE_MS);
      if (!lease.claimed) throw Store.boundedError("lease_expired");
    }
    try {
      if (signal?.aborted) throw Store.boundedError("cancelled");
      const response = await window.fetch(item.media_url, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "audio/mpeg", "Cache-Control": "no-store" },
        signal,
      });
      validateResponse(response, item);
      await readAndPut(cacheRequest(item.sha256), response, onProgress);
      const stored = await mediaCache.match(cacheRequest(item.sha256));
      if (!responseMatches(stored, item)) throw Store.boundedError("missing");
      await Store.releaseLease(handle, item.sha256, TAB_OWNER, {
        state: "ready", last_error_code: null, last_used_at: nowIso(),
      });
      channel?.postMessage({ type: "blob-ready", sha256: item.sha256 });
      return { sha256: item.sha256, byte_size: item.byte_size, reused: false };
    } catch (error) {
      const code = errorCode(error, "network");
      await Store.releaseLease(handle, item.sha256, TAB_OWNER, {
        state: "failed", last_error_code: code,
      });
      throw error?.code ? error : Store.boundedError(code);
    }
  };

  const uniqueItems = (items) => {
    const unique = new Map();
    items.forEach((item) => { if (!unique.has(item.sha256)) unique.set(item.sha256, item); });
    return [...unique.values()];
  };

  const storageStatus = async () => {
    const estimate = navigator.storage?.estimate ? await navigator.storage.estimate() : {};
    let persistent = null;
    if (navigator.storage?.persisted) {
      try { persistent = await navigator.storage.persisted(); } catch (_) { persistent = null; }
    }
    lastStorageStatus = {
      usage: Number.isFinite(estimate.usage) ? estimate.usage : null,
      quota: Number.isFinite(estimate.quota) ? estimate.quota : null,
      persistent,
    };
    return lastStorageStatus;
  };

  const requestPersistence = async () => {
    const status = await storageStatus();
    if (status.persistent || !navigator.storage?.persist) return { ...status, requested: false, denied: false };
    try {
      const request = Promise.resolve().then(() => navigator.storage.persist()).catch(() => null);
      const granted = await Promise.race([request, wait(1500).then(() => null)]);
      if (granted === null) return { ...status, requested: true, denied: true };
      return { ...(await storageStatus()), requested: true, denied: !granted };
    } catch (_) {
      return { ...(await storageStatus()), requested: true, denied: true };
    }
  };

  const checkQuota = async (bytes) => {
    const status = await storageStatus();
    const free = status.quota !== null && status.usage !== null ? Math.max(0, status.quota - status.usage) : null;
    if (free !== null && free < bytes) throw Store.boundedError("quota_insufficient");
    return { ...status, free, requested: false, denied: false };
  };

  const report = (callback, value) => { try { callback?.(value); } catch (_) {} };

  const cachePlaylist = async (payload, options = {}) => {
    await openStorage();
    const valid = Store.validateQueuePayload(payload, scope);
    const unique = uniqueItems(valid.items);
    const missing = [];
    for (const item of unique) if (!(await cachedItem(item))) missing.push(item);
    const missingBytes = missing.reduce((sum, item) => sum + item.byte_size, 0);
    report(options.onEstimate, {
      tracks: missing.length,
      bytes: missingBytes,
      totalTracks: unique.length,
      totalBytes: unique.reduce((sum, item) => sum + item.byte_size, 0),
    });
    let quota;
    try {
      quota = await checkQuota(missingBytes);
      if (options.requestPersistence !== false) quota = await requestPersistence();
      if (quota.quota !== null && quota.usage !== null && quota.quota - quota.usage < missingBytes) throw Store.boundedError("quota_insufficient");
    } catch (error) {
      report(options.onError, { code: errorCode(error), message: message(errorCode(error)) });
      throw error;
    }
    await Store.replaceSnapshot(handle, valid, { intent: options.intent || "explicit", lastSyncedAt: nowIso() });
    const operation = { cancelled: false, completed: 0, bytes: 0, total: unique.length, totalBytes: unique.reduce((sum, item) => sum + item.byte_size, 0) };
    const signal = options.signal;
    let cursor = 0;
    let failure = null;
    const worker = async () => {
      while (true) {
        if (signal?.aborted) { operation.cancelled = true; return; }
        if (failure) return;
        const index = cursor;
        cursor += 1;
        if (index >= missing.length) return;
        const item = missing[index];
        try {
          await downloadOne(item, {
            signal,
            onProgress: (transferred) => report(options.onProgress, { ...operation, current: item.sha256, currentBytes: transferred, phase: "downloading" }),
          });
          operation.completed += 1;
          operation.bytes += item.byte_size;
          report(options.onProgress, { ...operation, current: item.sha256, currentBytes: item.byte_size, phase: "complete" });
        } catch (error) {
          const code = errorCode(error);
          if (code === "cancelled") operation.cancelled = true;
          else failure = error?.code ? error : Store.boundedError(code);
          return;
        }
      }
    };
    await Promise.all(Array.from({ length: DOWNLOAD_CONCURRENCY }, () => worker()));
    const ownerId = valid.context.type === "playlist" ? valid.context.playlist_id : "played-tracks";
    await Store.recomputeOwner(handle, ownerId);
    if (failure) await Store.setOwnerState(handle, ownerId, "failed");
    else if (operation.cancelled) await Store.setOwnerState(handle, ownerId, "partial");
    else await Store.setOwnerState(handle, ownerId, "ready");
    await reconcile();
    const owner = await Store.getOwner(handle, ownerId);
    if (failure) {
      report(options.onError, { code: errorCode(failure), message: message(errorCode(failure)) });
      throw failure;
    }
    report(options.onComplete, { owner, cancelled: operation.cancelled, quota });
    return { owner, cancelled: operation.cancelled, quota };
  };

  const startPlaylistDownload = (payload, options = {}) => {
    const controller = new AbortController();
    const promise = cachePlaylist(payload, { ...options, signal: controller.signal });
    return { promise, cancel: () => controller.abort() };
  };

  const queuePayloadFromOwner = (owner) => ({
    schema_version: 2,
    context: {
      type: owner.playlist.kind === "local-played" ? "library" : "playlist",
      playlist_id: owner.playlist.kind === "local-played" ? null : owner.playlist.id,
      playlist_title: owner.playlist.title,
      playlist_kind: owner.playlist.kind === "local-played" ? null : owner.playlist.kind,
      revision: owner.playlist.server_revision || "0".repeat(64),
    },
    items: owner.entries.map(itemFromEntry),
  });

  const retryPlaylist = async (playlistId, options = {}) => {
    await openStorage();
    const owner = await Store.getOwner(handle, playlistId);
    if (!owner.playlist) throw Store.boundedError("missing");
    return cachePlaylist(queuePayloadFromOwner(owner), options);
  };

  const refreshPlaylist = async (queueUrl, options = {}) => {
    await openStorage();
    let parsedUrl;
    try {
      parsedUrl = new URL(queueUrl, window.location.href);
      if (parsedUrl.origin !== window.location.origin || !parsedUrl.pathname.startsWith(scope)) throw Store.boundedError("bad_url");
    } catch (error) {
      throw error?.code ? error : Store.boundedError("bad_url");
    }
    let response;
    try {
      response = await window.fetch(parsedUrl.href, {
        credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json", "Cache-Control": "no-store" },
        signal: options.signal,
      });
    } catch (error) {
      if (error?.code === "cancelled" || error?.name === "AbortError") throw Store.boundedError("cancelled");
      throw Store.boundedError("network");
    }
    if (!response.ok) throw Store.boundedError(statusForResponse(response));
    let payload;
    try { payload = await response.json(); } catch (_) { throw Store.boundedError("bad_queue"); }
    Store.validateQueuePayload(payload, scope);
    return cachePlaylist(payload, options);
  };

  const startPlaylistRefresh = (queueUrl, options = {}) => {
    const controller = new AbortController();
    const promise = refreshPlaylist(queueUrl, { ...options, signal: controller.signal });
    return { promise, cancel: () => controller.abort() };
  };

  const removeUnusedMapping = async () => {
    const entries = await Store.listEntries(handle);
    const paths = new Set(entries.map((entry) => {
      try { return new URL(entry.media_url, window.location.href).pathname; } catch (_) { return ""; }
    }));
    for (const mapping of await Store.listMappings(handle)) if (!paths.has(mapping.pathname)) await Store.deleteMapping(handle, mapping.pathname);
  };

  const reconcile = async () => {
    if (!handle || !mediaCache) return;
    const refs = await Store.listRefs(handle);
    const referenced = new Set(refs.map((ref) => ref.sha256));
    const blobs = await Store.listBlobs(handle);
    for (const blob of blobs) {
      const item = { sha256: blob.sha256, byte_size: blob.byte_size, mime_type: blob.mime_type };
      const request = cacheRequest(blob.sha256);
      if (!referenced.has(blob.sha256) || blob.state === "deleting") {
        await mediaCache.delete(request);
        await Store.deleteBlob(handle, blob.sha256);
        continue;
      }
      if (blob.lease_expires_at && Number(blob.lease_expires_at) <= Date.now() && blob.state === "downloading") {
        await Store.updateBlob(handle, blob.sha256, { state: "failed", last_error_code: "lease_expired", lease_owner: null, lease_expires_at: 0 });
        continue;
      }
      if (blob.state === "ready" && !responseMatches(await mediaCache.match(request), item)) await Store.updateBlob(handle, blob.sha256, { state: "failed", last_error_code: "missing" });
    }
    const known = new Set((await Store.listBlobs(handle)).map((blob) => blob.sha256));
    for (const request of await mediaCache.keys()) {
      const match = request.url.match(/\/sha256\/([0-9a-f]{64})$/);
      if (!match || !known.has(match[1]) || !referenced.has(match[1])) await mediaCache.delete(request);
    }
    for (const owner of await Store.listOwners(handle)) await Store.recomputeOwner(handle, owner.id);
    await removeUnusedMapping();
  };

  const cacheTrack = async (item, context = {}) => {
    await openStorage();
    const playlist = context.type === "playlist";
    const validItem = Store.validateQueueItem({ ...item, queue_entry_id: playlist ? item.queue_entry_id : null }, scope, { playlist });
    if (playlist) {
      const current = await Store.getOwner(handle, context.playlist_id);
      const currentItems = current.entries?.map(itemFromEntry) || [];
      const merged = currentItems.filter((entry) => entry.queue_entry_id !== validItem.queue_entry_id);
      merged.push(validItem);
      await Store.replaceSnapshot(handle, {
        schema_version: 2,
        context: {
          type: "playlist",
          playlist_id: String(context.playlist_id),
          playlist_title: String(context.playlist_title || current.playlist?.title || "Playlist"),
          playlist_kind: context.playlist_kind || current.playlist?.kind || "custom",
          revision: context.server_revision || current.playlist?.server_revision || "0".repeat(64),
        },
        items: merged,
      }, { intent: "automatic" });
    } else {
      await Store.addPlayedTrack(handle, validItem);
    }
    try {
      return await downloadOne(validItem, { signal: context.signal });
    } finally {
      await Store.recomputeOwner(handle, playlist ? context.playlist_id : "played-tracks");
      await reconcile();
    }
  };

  const removeOwner = async (playlistId) => {
    await openStorage();
    const before = new Map((await Store.listBlobs(handle)).map((blob) => [blob.sha256, blob.byte_size]));
    const removed = await Store.clearOwner(handle, playlistId);
    const refs = new Set((await Store.listRefs(handle)).map((ref) => ref.sha256));
    let freedBytes = 0;
    let retainedBytes = 0;
    for (const hash of removed.hashes) {
      const bytes = before.get(hash) || 0;
      if (refs.has(hash)) retainedBytes += bytes;
      else {
        freedBytes += bytes;
        await mediaCache.delete(cacheRequest(hash));
        await Store.deleteBlob(handle, hash);
      }
    }
    await removeUnusedMapping();
    window.dispatchEvent(new CustomEvent("audioventura:offline-updated"));
    return { freedBytes, retainedBytes };
  };

  const formatBytes = (bytes) => {
    if (!Number.isFinite(bytes)) return "unknown";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
  };

  const setText = (selector, value) => { const node = document.querySelector(selector); if (node) node.textContent = value; };
  const informWorkerConnectivity = (enabled) => {
    try { navigator.serviceWorker?.controller?.postMessage({ type: "offline-mode", enabled }); } catch (_) {}
  };
  const ownerMatchesFilter = (owner, filter) => !filter || owner.title.toLowerCase().includes(filter.toLowerCase());
  const ownerQueueUrl = (playlistId) => new URL(`${scope}player/queue/playlist/${encodeURIComponent(playlistId)}`, window.location.origin).href;
  const ownerStatus = (owner) => {
    if (!owner) return "Not saved on this browser yet.";
    const synced = owner.last_synced_at ? ` · synced ${new Date(owner.last_synced_at).toLocaleString()}` : "";
    return `${owner.state} · ${owner.ready_track_count}/${owner.track_count} tracks · ${formatBytes(owner.ready_unique_bytes)} / ${formatBytes(owner.unique_bytes)}${synced}`;
  };

  const setToolStatus = (tool, value) => {
    const status = tool.querySelector("[data-offline-playlist-status]");
    if (status) status.textContent = value;
  };

  const syncPlaylistTools = async () => {
    if (!handle) return;
    for (const tool of document.querySelectorAll("[data-offline-playlist]")) {
      const id = tool.dataset.offlinePlaylist;
      if (!id) continue;
      const owner = await Store.getOwner(handle, id);
      const active = activeOperations.get(id);
      setToolStatus(tool, active?.status || tool.dataset.offlineError || ownerStatus(owner?.playlist));
      const progress = tool.querySelector("[data-offline-playlist-progress]");
      if (progress) {
        progress.hidden = !active;
        if (active?.totalBytes) {
          progress.max = String(active.totalBytes);
          progress.value = String(Math.min(active.totalBytes, active.bytes || 0));
        }
      }
      const keep = tool.querySelector("[data-offline-cache-playlist]");
      const refresh = tool.querySelector("[data-offline-refresh-playlist]");
      const cancel = tool.querySelector("[data-offline-cancel-playlist]");
      if (keep) keep.disabled = Boolean(active);
      if (refresh) refresh.disabled = Boolean(active);
      if (cancel) cancel.hidden = !active;
    }
  };

  const renderOwners = async () => {
    const list = document.querySelector("[data-offline-owner-list]");
    if (!list || !handle) return;
    const owners = (await Store.listOwners(handle)).filter((owner) => owner.kind === "local-played" || owner.intent === "explicit" || owner.server_revision);
    const filter = document.querySelector("[data-offline-filter]")?.value.trim() || "";
    list.replaceChildren();
    const visible = owners.filter((owner) => ownerMatchesFilter(owner, filter));
    if (!visible.length) {
      const empty = document.createElement("div"); empty.className = "empty";
      const paragraph = document.createElement("p"); paragraph.textContent = "No saved playlists yet."; empty.append(paragraph); list.append(empty); return;
    }
    for (const owner of visible) {
      const card = document.createElement("article"); card.className = "offline-owner-card"; card.dataset.offlineOwnerId = owner.id;
      const heading = document.createElement("h3"); heading.textContent = owner.title; card.append(heading);
      const status = document.createElement("p"); status.className = "muted"; status.textContent = `${owner.state} · ${owner.ready_track_count}/${owner.track_count} tracks · ${formatBytes(owner.ready_unique_bytes)} / ${formatBytes(owner.unique_bytes)}`; card.append(status);
      const progress = document.createElement("progress"); progress.max = Math.max(1, owner.unique_bytes); progress.value = owner.ready_unique_bytes; progress.setAttribute("aria-label", `${owner.title} offline progress`); card.append(progress);
      if (owner.last_synced_at) {
        const synced = document.createElement("p"); synced.className = "hint"; synced.textContent = `Last refreshed ${new Date(owner.last_synced_at).toLocaleString()}`; card.append(synced);
      }
      const actions = document.createElement("div"); actions.className = "offline-owner-actions";
      const play = document.createElement("button"); play.className = "button secondary"; play.type = "button"; play.textContent = "Play"; play.dataset.offlinePlay = owner.id; actions.append(play);
      if (owner.state !== "ready") { const retry = document.createElement("button"); retry.className = "button secondary"; retry.type = "button"; retry.textContent = "Retry"; retry.dataset.offlineRetry = owner.id; actions.append(retry); }
      if (owner.kind !== "local-played") { const refresh = document.createElement("button"); refresh.className = "button secondary"; refresh.type = "button"; refresh.textContent = "Refresh"; refresh.dataset.offlineRefreshOwner = owner.id; refresh.dataset.offlineQueueUrl = ownerQueueUrl(owner.id); actions.append(refresh); }
      const remove = document.createElement("button"); remove.className = "button danger"; remove.type = "button"; remove.textContent = "Remove download"; remove.dataset.offlineRemove = owner.id; actions.append(remove);
      card.append(actions); list.append(card);
    }
    await syncPlaylistTools();
  };

  const renderStorage = async () => {
    const status = await storageStatus();
    setText("[data-offline-persistence]", status.persistent === true ? "Persistent storage granted" : status.persistent === false ? "Best-effort storage" : "Unavailable");
    setText("[data-offline-quota]", status.quota === null ? "Unavailable" : `${formatBytes(status.usage || 0)} used / ${formatBytes(status.quota)} quota`);
    const blobs = await Store.listBlobs(handle);
    setText("[data-offline-bytes]", formatBytes(blobs.filter((blob) => blob.state === "ready").reduce((sum, blob) => sum + blob.byte_size, 0)));
  };

  const startPlaylistAction = (tool, queueUrl) => {
    const id = tool.dataset.offlinePlaylist;
    if (!id || activeOperations.has(id)) return;
    delete tool.dataset.offlineError;
    const operation = startPlaylistRefresh(queueUrl, {
      intent: "explicit",
      onEstimate: (estimate) => {
        operation.totalBytes = estimate.totalBytes;
        operation.bytes = 0;
        operation.status = estimate.tracks
          ? `${estimate.tracks} tracks / ${formatBytes(estimate.bytes)} to download`
          : "All tracks are already saved; refreshing snapshot";
        void syncPlaylistTools();
      },
      onProgress: (progress) => {
        operation.bytes = Math.max(operation.bytes || 0, (progress.bytes || 0) + (progress.currentBytes || 0));
        operation.totalBytes = progress.totalBytes || operation.totalBytes || 1;
        operation.status = progress.phase === "complete" ? `Saved ${progress.completed} of ${progress.total} tracks` : "Downloading playlist…";
        void syncPlaylistTools();
      },
      onComplete: (result) => {
        delete tool.dataset.offlineError;
        operation.status = result.cancelled ? "Download cancelled; completed tracks remain saved." : "Playlist is available offline.";
      },
      onError: (error) => { operation.status = message(error.code); },
    });
    activeOperations.set(id, operation);
    operation.promise.catch((error) => {
      operation.status = message(errorCode(error));
      tool.dataset.offlineError = operation.status;
      setToolStatus(tool, operation.status);
      setText("[data-offline-storage-message]", operation.status);
    }).finally(async () => {
      activeOperations.delete(id);
      await renderOwners();
      await syncPlaylistTools();
      await renderStorage();
    });
    void syncPlaylistTools();
  };

  const bindUi = () => {
    if (document.documentElement.dataset.offlineUiBound === "true") return;
    document.documentElement.dataset.offlineUiBound = "true";
    document.querySelector("[data-offline-filter]")?.addEventListener("input", () => renderOwners());
    document.addEventListener("click", async (event) => {
      const cache = event.target.closest("[data-offline-cache-playlist]");
      const refresh = event.target.closest("[data-offline-refresh-playlist]");
      const cancel = event.target.closest("[data-offline-cancel-playlist]");
      const refreshOwner = event.target.closest("[data-offline-refresh-owner]");
      const retry = event.target.closest("[data-offline-retry]");
      const remove = event.target.closest("[data-offline-remove]");
      const play = event.target.closest("[data-offline-play]");
      try {
        if (cache || refresh) {
          const tool = (cache || refresh).closest("[data-offline-playlist]");
          if (tool) startPlaylistAction(tool, tool.dataset.offlineQueueUrl);
          return;
        }
        if (cancel) {
          const tool = cancel.closest("[data-offline-playlist]");
          const active = tool && activeOperations.get(tool.dataset.offlinePlaylist);
          active?.cancel();
          return;
        }
        if (refreshOwner) {
          const card = refreshOwner.closest("[data-offline-owner-id]");
          const id = refreshOwner.dataset.offlineRefreshOwner;
          if (card && id) {
            card.dataset.offlinePlaylist = id;
            card.dataset.offlineQueueUrl = refreshOwner.dataset.offlineQueueUrl;
            startPlaylistAction(card, refreshOwner.dataset.offlineQueueUrl);
          }
          return;
        }
        if (retry) { retry.disabled = true; await retryPlaylist(retry.dataset.offlineRetry); }
        if (remove) {
          remove.disabled = true;
          const result = await removeOwner(remove.dataset.offlineRemove);
          setText("[data-offline-storage-message]", `Removed download: ${formatBytes(result.freedBytes)} freed; ${formatBytes(result.retainedBytes)} retained for another owner.`);
        }
        if (play) window.dispatchEvent(new CustomEvent("audioventura:offline-play", { detail: { playlistId: play.dataset.offlinePlay } }));
      } catch (error) {
        setText("[data-offline-storage-message]", message(errorCode(error)));
      } finally {
        await renderOwners(); await renderStorage(); await syncPlaylistTools();
      }
    });
  };

  const refreshUi = async () => {
    if (!handle) return;
    await renderOwners();
    await renderStorage();
    await syncPlaylistTools();
  };

  const init = async () => {
    try {
      await openStorage();
      bindUi();
      await refreshUi();
      setText("[data-offline-connectivity]", navigator.onLine ? "Online; saved snapshots can be refreshed." : "Offline; showing browser-local snapshots.");
      // The service worker marks a shell as offline when its navigation
      // fallback is used. Do not clear that state merely because Chromium's
      // navigator.onLine remains true while the context is emulated offline.
      if (navigator.onLine === false) informWorkerConnectivity(true);
      window.addEventListener("online", () => {
        if (!sawOfflineSignal || isOfflineShell) return;
        sawOfflineSignal = false;
        informWorkerConnectivity(false);
        setText("[data-offline-connectivity]", "Online; saved snapshots can be refreshed.");
      });
      window.addEventListener("offline", () => {
        sawOfflineSignal = true;
        informWorkerConnectivity(true);
        setText("[data-offline-connectivity]", "Offline; showing browser-local snapshots.");
      });
      window.dispatchEvent(new CustomEvent("audioventura:offline-ready"));
    } catch (error) {
      setText("[data-offline-storage-message]", message(errorCode(error, "storage_unavailable")));
      window.dispatchEvent(new CustomEvent("audioventura:offline-unavailable", { detail: { code: errorCode(error) } }));
    }
  };

  window.addEventListener("audioventura:navigation", () => { void refreshUi(); });
  window.addEventListener("audioventura:offline-updated", () => { void refreshUi(); });

  channel?.addEventListener("message", async (event) => {
    if (event.data?.type === "blob-ready" && handle) await renderOwners();
  });

  window.AudioventuraOffline = {
    scope,
    scopeKey,
    MEDIA_CACHE_NAME,
    SHELL_CACHE_PREFIX,
    init,
    openStorage,
    storageStatus,
    requestPersistence,
    cachePlaylist,
    startPlaylistDownload,
    retryPlaylist,
    refreshPlaylist,
    startPlaylistRefresh,
    cacheTrack,
    removeOwner,
    reconcile,
    getOfflineQueue: async (playlistId) => { await openStorage(); const owner = await Store.getOwner(handle, playlistId); return owner.entries?.map(itemFromEntry) || []; },
    getOwner: async (playlistId) => { await openStorage(); return Store.getOwner(handle, playlistId); },
    refreshUi,
    formatBytes,
    message,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();

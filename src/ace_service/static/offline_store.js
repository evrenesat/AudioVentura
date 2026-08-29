(() => {
  "use strict";

  if (window.AudioventuraOfflineStore) return;

  const SCHEMA_VERSION = 1;
  const MAX_PLAYLIST_ENTRIES = 2000;
  const MAX_LIBRARY_ENTRIES = 500;
  const MAX_TITLE_LENGTH = 300;
  const MAX_HASH_LENGTH = 64;
  const MAX_MEDIA_BYTES = 512 * 1024 * 1024;
  const STORE_NAMES = ["blobs", "media_urls", "playlists", "entries", "refs"];
  const STORE_STATES = new Set(["downloading", "ready", "deleting", "failed"]);
  const ERROR_CODES = new Set([
    "bad_metadata", "bad_scope", "bad_queue", "bad_hash", "bad_size", "bad_type",
    "bad_url", "network", "offline", "unauthorized", "not_found", "server",
    "bad_identity", "bad_length", "quota", "quota_insufficient", "missing",
    "lease_expired", "cancelled", "storage_unavailable", "unknown",
  ]);

  const boundedError = (code, message = code) => {
    const error = new Error(message);
    error.code = ERROR_CODES.has(code) ? code : "unknown";
    return error;
  };

  const requestResult = (request) => new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(boundedError("storage_unavailable"));
  });

  const transactionResult = (transaction, work) => new Promise((resolve, reject) => {
    let result;
    let failed = false;
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = () => {
      if (!failed) reject(boundedError("storage_unavailable"));
    };
    transaction.onabort = () => {
      if (!failed) reject(boundedError("storage_unavailable"));
    };
    Promise.resolve()
      .then(() => work(transaction))
      .then((value) => { result = value; })
      .catch((error) => {
        failed = true;
        try { transaction.abort(); } catch (_) {}
        reject(error && error.code ? error : boundedError("storage_unavailable"));
      });
  });

  const normalizeScope = (value) => {
    let pathname = value;
    try { pathname = new URL(value, window.location.origin).pathname; } catch (_) {}
    if (typeof pathname !== "string" || !pathname.startsWith("/")) {
      throw boundedError("bad_scope");
    }
    if (!pathname.endsWith("/")) pathname += "/";
    if (pathname.includes("//") || pathname.includes("\\") || pathname.includes("..")) {
      throw boundedError("bad_scope");
    }
    return pathname;
  };

  const scopeKey = (scope) => {
    const normalized = normalizeScope(scope);
    if (normalized === "/") return "root";
    const value = normalized.slice(1, -1).replace(/[^A-Za-z0-9_-]+/g, "_");
    if (!value || value.length > 80) throw boundedError("bad_scope");
    return value;
  };

  const scopeFromPage = () => {
    const meta = document.querySelector('meta[name="offline-worker"]');
    const workerUrl = meta?.content || "/notification-worker.js";
    try { return normalizeScope(new URL("./", new URL(workerUrl, window.location.href)).pathname); }
    catch (_) { return normalizeScope(window.location.pathname); }
  };

  const databaseName = (scope) => `audioventura-offline:${scopeKey(scope)}:v1`;
  const cacheUrl = (scope, sha256) => {
    const hash = validateHash(sha256);
    return new URL(`${normalizeScope(scope)}__offline/media/sha256/${hash}`, window.location.origin).href;
  };

  const validateHash = (value) => {
    if (typeof value !== "string" || value.length !== MAX_HASH_LENGTH || !/^[0-9a-f]{64}$/.test(value)) {
      throw boundedError("bad_hash");
    }
    return value;
  };

  const validateText = (value, max = MAX_TITLE_LENGTH) => {
    if (typeof value !== "string" || !value.trim() || value.length > max || /[\u0000-\u001f\u007f]/.test(value)) {
      throw boundedError("bad_metadata");
    }
    return value;
  };

  const normalizePath = (value, scope, kind = "media") => {
    let url;
    try { url = new URL(value, window.location.href); } catch (_) { throw boundedError("bad_url"); }
    const normalizedScope = normalizeScope(scope);
    if (url.origin !== window.location.origin || url.search || url.hash || !url.pathname.startsWith(normalizedScope)) {
      throw boundedError("bad_url");
    }
    const mediaPrefix = `${normalizedScope}media/library/`;
    const downloadPrefix = `${normalizedScope}files/library/`;
    if (kind === "media" && (!url.pathname.startsWith(mediaPrefix) || !/^\d+$/.test(url.pathname.slice(mediaPrefix.length)))) {
      throw boundedError("bad_url");
    }
    if (kind === "download" && (!url.pathname.startsWith(downloadPrefix) || !/^\d+\/download$/.test(url.pathname.slice(downloadPrefix.length)))) {
      throw boundedError("bad_url");
    }
    return url.pathname;
  };

  const validateQueueItem = (item, scope, { playlist = false } = {}) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw boundedError("bad_queue");
    const mediaItemId = validateText(String(item.media_item_id || item.id || ""), 80);
    const mediaFileId = item.media_file_id;
    if (typeof mediaFileId !== "number" || !Number.isSafeInteger(mediaFileId) || mediaFileId < 1) {
      throw boundedError("bad_metadata");
    }
    const mimeType = item.mime_type;
    if (mimeType !== "audio/mpeg") throw boundedError("bad_type");
    const byteSize = item.byte_size;
    if (!Number.isSafeInteger(byteSize) || byteSize < 1 || byteSize > MAX_MEDIA_BYTES) {
      throw boundedError("bad_size");
    }
    const hash = validateHash(item.sha256);
    const mediaPath = normalizePath(item.media_url, scope);
    if (Number(mediaPath.slice(mediaPath.lastIndexOf("/") + 1)) !== mediaFileId) throw boundedError("bad_url");
    const downloadPath = item.download_url ? normalizePath(item.download_url, scope, "download") : mediaPath;
    if (playlist && (typeof item.queue_entry_id !== "number" || !Number.isSafeInteger(item.queue_entry_id) || item.queue_entry_id < 1)) {
      throw boundedError("bad_queue");
    }
    if (!playlist && item.queue_entry_id !== null && item.queue_entry_id !== undefined) throw boundedError("bad_queue");
    if (item.position !== null && item.position !== undefined && (!Number.isSafeInteger(item.position) || item.position < 0)) {
      throw boundedError("bad_queue");
    }
    const duration = item.duration_seconds === null || item.duration_seconds === undefined
      ? null : Number(item.duration_seconds);
    if (duration !== null && (!Number.isFinite(duration) || duration < 0 || duration > 24 * 60 * 60)) {
      throw boundedError("bad_metadata");
    }
    return {
      id: mediaItemId,
      queue_entry_id: item.queue_entry_id ?? null,
      position: item.position ?? null,
      media_item_id: mediaItemId,
      media_file_id: mediaFileId,
      title: validateText(item.title, MAX_TITLE_LENGTH),
      project_id: validateText(String(item.project_id || ""), 80),
      project_title: validateText(item.project_title, MAX_TITLE_LENGTH),
      duration_seconds: duration,
      mime_type: mimeType,
      byte_size: byteSize,
      sha256: hash,
      updated_at: validateText(String(item.updated_at || item.media_updated_at || ""), 64),
      media_updated_at: validateText(String(item.media_updated_at || item.updated_at || ""), 64),
      media_url: new URL(item.media_url, window.location.href).href,
      download_url: item.download_url ? new URL(item.download_url, window.location.href).href : new URL(item.media_url, window.location.href).href,
      _media_pathname: mediaPath,
      _download_pathname: downloadPath,
    };
  };

  const validateQueuePayload = (payload, scope) => {
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || payload.schema_version !== 2) {
      throw boundedError("bad_queue");
    }
    const context = payload.context;
    if (!context || typeof context !== "object" || Array.isArray(context)) throw boundedError("bad_queue");
    const type = context.type;
    const playlist = type === "playlist";
    if (!playlist && type !== "library") throw boundedError("bad_queue");
    if (playlist) {
      validateText(String(context.playlist_id || ""), 80);
      validateText(String(context.playlist_title || ""), MAX_TITLE_LENGTH);
      if (!["project", "custom"].includes(context.playlist_kind)) throw boundedError("bad_queue");
    }
    if (typeof context.revision !== "string" || !/^[0-9a-f]{64}$/.test(context.revision)) throw boundedError("bad_queue");
    if (!Array.isArray(payload.items)) throw boundedError("bad_queue");
    const max = playlist ? MAX_PLAYLIST_ENTRIES : MAX_LIBRARY_ENTRIES;
    if (payload.items.length > max) throw boundedError("bad_queue");
    const entryIds = new Set();
    const items = payload.items.map((item) => {
      const result = validateQueueItem(item, scope, { playlist });
      if (playlist) {
        if (entryIds.has(result.queue_entry_id)) throw boundedError("bad_queue");
        entryIds.add(result.queue_entry_id);
      }
      return result;
    });
    return {
      schema_version: 2,
      context: {
        type,
        playlist_id: playlist ? String(context.playlist_id) : null,
        playlist_title: playlist ? String(context.playlist_title) : String(context.playlist_title || "Library"),
        playlist_kind: playlist ? context.playlist_kind : null,
        revision: context.revision,
      },
      items,
    };
  };

  const createDatabase = (scope) => {
    if (!window.indexedDB) return Promise.reject(boundedError("storage_unavailable"));
    const request = window.indexedDB.open(databaseName(scope), SCHEMA_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      const create = (name, options, indexes) => {
        const store = db.objectStoreNames.contains(name) ? request.transaction.objectStore(name) : db.createObjectStore(name, options);
        indexes.forEach(([indexName, keyPath, indexOptions]) => {
          if (!store.indexNames.contains(indexName)) store.createIndex(indexName, keyPath, indexOptions || {});
        });
      };
      create("blobs", { keyPath: "sha256" }, [
        ["state", "state"], ["lease_expires_at", "lease_expires_at"], ["last_used_at", "last_used_at"],
      ]);
      create("media_urls", { keyPath: "pathname" }, [["sha256", "sha256"], ["media_file_id", "media_file_id"]]);
      create("playlists", { keyPath: "id" }, [["state", "state"], ["kind", "kind"], ["updated_at", "updated_at"]]);
      create("entries", { keyPath: ["playlist_id", "entry_key"] }, [["playlist_id", "playlist_id"], ["sha256", "sha256"]]);
      create("refs", { keyPath: ["playlist_id", "sha256"] }, [["playlist_id", "playlist_id"], ["sha256", "sha256"]]);
    };
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(boundedError("storage_unavailable"));
      request.onblocked = () => reject(boundedError("storage_unavailable"));
    });
  };

  const open = async (scope = scopeFromPage()) => ({ scope: normalizeScope(scope), db: await createDatabase(scope) });

  const run = async (handle, names, mode, work) => {
    if (!handle || !handle.db) throw boundedError("storage_unavailable");
    const transaction = handle.db.transaction(names, mode);
    return transactionResult(transaction, (tx) => work(tx, Object.fromEntries(names.map((name) => [name, tx.objectStore(name)]))));
  };

  const get = (store, key) => requestResult(store.get(key));
  const getAll = (store) => requestResult(store.getAll());
  const getAllKeys = (store) => requestResult(store.getAllKeys());
  const getIndexAll = (store, name, key) => requestResult(store.index(name).getAll(IDBKeyRange.only(key)));

  const nowIso = () => new Date().toISOString();
  const baseBlob = (item, scope) => ({
    sha256: item.sha256,
    cache_url: cacheUrl(scope, item.sha256),
    byte_size: item.byte_size,
    mime_type: "audio/mpeg",
    state: "downloading",
    lease_owner: null,
    lease_expires_at: 0,
    created_at: nowIso(),
    updated_at: nowIso(),
    last_used_at: nowIso(),
    last_error_code: null,
  });

  const entryKey = (item, index) => item.queue_entry_id === null ? `local-${item.media_item_id}-${index}` : String(item.queue_entry_id);

  const snapshotRecords = (payload, handle, intent = "explicit") => {
    const playlistId = payload.context.type === "playlist" ? payload.context.playlist_id : "played-tracks";
    const entries = payload.items.map((item, index) => ({
      playlist_id: playlistId,
      entry_key: entryKey(item, index),
      queue_entry_id: item.queue_entry_id,
      position: item.position ?? (index + 1) * 1024,
      media_item_id: item.media_item_id,
      media_file_id: item.media_file_id,
      sha256: item.sha256,
      title: item.title,
      project_id: item.project_id,
      project_title: item.project_title,
      duration_seconds: item.duration_seconds,
      byte_size: item.byte_size,
      mime_type: item.mime_type,
      media_url: item.media_url,
      download_url: item.download_url,
      updated_at: item.updated_at,
    }));
    const unique = new Map();
    payload.items.forEach((item) => { if (!unique.has(item.sha256)) unique.set(item.sha256, item); });
    return { playlistId, entries, unique, intent };
  };

  const replaceSnapshot = async (handle, payload, { intent = "explicit", lastSyncedAt = nowIso() } = {}) => {
    const valid = validateQueuePayload(payload, handle.scope);
    const records = snapshotRecords(valid, handle, intent);
    const timestamp = nowIso();
    return run(handle, STORE_NAMES, "readwrite", async (_tx, stores) => {
      const existing = await get(stores.playlists, records.playlistId);
      const oldEntries = await getIndexAll(stores.entries, "playlist_id", records.playlistId);
      const oldRefs = await getIndexAll(stores.refs, "playlist_id", records.playlistId);
      for (const entry of oldEntries) stores.entries.delete([records.playlistId, entry.entry_key]);
      for (const ref of oldRefs) stores.refs.delete([records.playlistId, ref.sha256]);
      for (const [hash, item] of records.unique) {
        const current = await get(stores.blobs, hash);
        if (!current) stores.blobs.put(baseBlob(item, handle.scope));
        else {
          if (current.byte_size !== item.byte_size || current.mime_type !== "audio/mpeg") {
            if (current.state !== "ready") {
              current.state = "failed";
              current.last_error_code = "bad_metadata";
            }
          }
          current.updated_at = timestamp;
          stores.blobs.put(current);
        }
        stores.refs.put({ playlist_id: records.playlistId, sha256: hash, byte_size: item.byte_size, mime_type: "audio/mpeg", created_at: timestamp });
      }
      for (const entry of records.entries) stores.entries.put(entry);
      for (const item of valid.items) {
        stores.media_urls.put({
          pathname: item._media_pathname,
          media_file_id: item.media_file_id,
          media_item_id: item.media_item_id,
          sha256: item.sha256,
          byte_size: item.byte_size,
          mime_type: item.mime_type,
          server_updated_at: item.media_updated_at,
        });
      }
      const ready = [];
      for (const item of records.unique.values()) {
        const blob = await get(stores.blobs, item.sha256);
        if (blob && blob.state === "ready") ready.push(item);
      }
      const readyHashes = new Set(ready.map((item) => item.sha256));
      const readyEntryCount = records.entries.filter((entry) => readyHashes.has(entry.sha256)).length;
      stores.playlists.put({
        id: records.playlistId,
        title: valid.context.playlist_title,
        kind: valid.context.type === "playlist" ? valid.context.playlist_kind : "local-played",
        server_revision: valid.context.type === "playlist" ? valid.context.revision : null,
        intent,
        state: records.unique.size === 0 || ready.length === records.unique.size ? "ready" : "partial",
        track_count: records.entries.length,
        ready_track_count: readyEntryCount,
        unique_bytes: [...records.unique.values()].reduce((sum, item) => sum + item.byte_size, 0),
        ready_unique_bytes: ready.reduce((sum, item) => sum + item.byte_size, 0),
        created_at: existing?.created_at || timestamp,
        updated_at: timestamp,
        last_synced_at: valid.context.type === "playlist" ? lastSyncedAt : null,
      });
      return { playlist_id: records.playlistId, entries: records.entries, unique: [...records.unique.values()] };
    });
  };

  const listOwners = (handle) => run(handle, ["playlists"], "readonly", (_tx, stores) => getAll(stores.playlists));
  const getOwner = (handle, playlistId) => run(handle, ["playlists", "entries"], "readonly", async (_tx, stores) => ({
    playlist: await get(stores.playlists, playlistId),
    entries: await getIndexAll(stores.entries, "playlist_id", playlistId),
  }));
  const listBlobs = (handle) => run(handle, ["blobs"], "readonly", (_tx, stores) => getAll(stores.blobs));
  const listRefs = (handle) => run(handle, ["refs"], "readonly", (_tx, stores) => getAll(stores.refs));
  const listEntries = (handle) => run(handle, ["entries"], "readonly", (_tx, stores) => getAll(stores.entries));
  const listMappings = (handle) => run(handle, ["media_urls"], "readonly", (_tx, stores) => getAll(stores.media_urls));
  const getBlob = (handle, hash) => run(handle, ["blobs"], "readonly", (_tx, stores) => get(stores.blobs, validateHash(hash)));
  const getMapping = (handle, pathname) => run(handle, ["media_urls"], "readonly", (_tx, stores) => get(stores.media_urls, pathname));

  const updateBlob = async (handle, hash, changes) => run(handle, ["blobs"], "readwrite", async (_tx, stores) => {
    const blob = await get(stores.blobs, validateHash(hash));
    if (!blob) throw boundedError("missing");
    Object.assign(blob, changes, { updated_at: nowIso() });
    stores.blobs.put(blob);
    return blob;
  });

  const deleteBlob = (handle, hash) => run(handle, ["blobs"], "readwrite", (_tx, stores) => {
    stores.blobs.delete(validateHash(hash));
  });

  const deleteMapping = (handle, pathname) => run(handle, ["media_urls"], "readwrite", (_tx, stores) => {
    stores.media_urls.delete(pathname);
  });

  const claimLease = async (handle, hash, owner, ttlMs = 30000) => run(handle, ["blobs"], "readwrite", async (_tx, stores) => {
    const blob = await get(stores.blobs, validateHash(hash));
    if (!blob) throw boundedError("missing");
    if (blob.state === "ready") return { claimed: false, ready: true, blob };
    const now = Date.now();
    if (blob.lease_owner && blob.lease_owner !== owner && Number(blob.lease_expires_at) > now) {
      return { claimed: false, ready: false, blob };
    }
    blob.state = "downloading";
    blob.lease_owner = owner;
    blob.lease_expires_at = now + ttlMs;
    blob.last_error_code = null;
    blob.updated_at = nowIso();
    stores.blobs.put(blob);
    return { claimed: true, ready: false, blob };
  });

  const releaseLease = async (handle, hash, owner, changes = {}) => run(handle, ["blobs"], "readwrite", async (_tx, stores) => {
    const blob = await get(stores.blobs, validateHash(hash));
    if (!blob) return null;
    if (blob.lease_owner && blob.lease_owner !== owner) return blob;
    blob.lease_owner = null;
    blob.lease_expires_at = 0;
    Object.assign(blob, changes, { updated_at: nowIso() });
    stores.blobs.put(blob);
    return blob;
  });

  const recomputeOwner = async (handle, playlistId) => run(handle, ["playlists", "entries", "blobs"], "readwrite", async (_tx, stores) => {
    const playlist = await get(stores.playlists, playlistId);
    if (!playlist) return null;
    const entries = await getIndexAll(stores.entries, "playlist_id", playlistId);
    const hashes = [...new Set(entries.map((entry) => entry.sha256))];
    const blobs = [];
    for (const hash of hashes) blobs.push(await get(stores.blobs, hash));
    const ready = blobs.filter((blob) => blob && blob.state === "ready");
    playlist.track_count = entries.length;
    playlist.ready_track_count = new Set(entries.filter((entry) => ready.some((blob) => blob.sha256 === entry.sha256)).map((entry) => entry.entry_key)).size;
    playlist.unique_bytes = hashes.reduce((sum, hash) => sum + (entries.find((entry) => entry.sha256 === hash)?.byte_size || 0), 0);
    playlist.ready_unique_bytes = ready.reduce((sum, blob) => sum + blob.byte_size, 0);
    playlist.state = hashes.length === 0 || ready.length === hashes.length ? "ready" : "partial";
    playlist.updated_at = nowIso();
    stores.playlists.put(playlist);
    return playlist;
  });

  const setOwnerState = (handle, playlistId, state) => run(handle, ["playlists"], "readwrite", async (_tx, stores) => {
    const playlist = await get(stores.playlists, playlistId);
    if (!playlist) return null;
    if (!["partial", "ready", "stale", "failed"].includes(state)) throw boundedError("bad_metadata");
    playlist.state = state;
    playlist.updated_at = nowIso();
    stores.playlists.put(playlist);
    return playlist;
  });

  const clearOwner = async (handle, playlistId) => run(handle, ["playlists", "entries", "refs"], "readwrite", async (_tx, stores) => {
    const playlist = await get(stores.playlists, playlistId);
    if (!playlist) return { freedBytes: 0, retainedBytes: 0, hashes: [] };
    const refs = await getIndexAll(stores.refs, "playlist_id", playlistId);
    const affected = refs.map((ref) => ref.sha256);
    const allRefs = await getAll(stores.refs);
    for (const entry of await getIndexAll(stores.entries, "playlist_id", playlistId)) stores.entries.delete([playlistId, entry.entry_key]);
    for (const ref of refs) stores.refs.delete([playlistId, ref.sha256]);
    stores.playlists.delete(playlistId);
    const retained = new Set(allRefs.filter((ref) => ref.playlist_id !== playlistId).map((ref) => ref.sha256));
    return { freedBytes: affected.filter((hash) => !retained.has(hash)), retainedBytes: affected.filter((hash) => retained.has(hash)), hashes: affected };
  });

  const addPlayedTrack = async (handle, item) => {
    const validItem = validateQueueItem({ ...item, queue_entry_id: null, position: null }, handle.scope);
    const existing = await getOwner(handle, "played-tracks");
    const entries = (existing.entries || []).filter((entry) => entry.media_item_id !== validItem.media_item_id);
    const nextItems = entries.map((entry) => ({
      ...entry,
      id: entry.media_item_id,
      queue_entry_id: null,
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
    }));
    nextItems.push(validItem);
    const payload = {
      schema_version: 2,
      context: { type: "library", playlist_id: null, playlist_title: "Played tracks", playlist_kind: null, revision: "0".repeat(64) },
      items: nextItems.slice(-MAX_LIBRARY_ENTRIES),
    };
    const result = await replaceSnapshot(handle, payload, { intent: "automatic" });
    await run(handle, ["playlists"], "readwrite", (_tx, stores) => {
      const request = stores.playlists.get("played-tracks");
      request.onsuccess = () => {
        const playlist = request.result;
        if (playlist) {
          playlist.id = "played-tracks";
          playlist.title = "Played tracks";
          playlist.kind = "local-played";
          stores.playlists.put(playlist);
        }
      };
    });
    return result;
  };

  window.AudioventuraOfflineStore = {
    SCHEMA_VERSION,
    MAX_PLAYLIST_ENTRIES,
    MAX_LIBRARY_ENTRIES,
    STORE_STATES,
    boundedError,
    normalizeScope,
    scopeKey,
    scopeFromPage,
    databaseName,
    cacheUrl,
    validateHash,
    validateQueueItem,
    validateQueuePayload,
    open,
    run,
    replaceSnapshot,
    listOwners,
    getOwner,
    listBlobs,
    listRefs,
    listEntries,
    listMappings,
    getBlob,
    getMapping,
    updateBlob,
    deleteBlob,
    deleteMapping,
    claimLease,
    releaseLease,
    recomputeOwner,
    setOwnerState,
    clearOwner,
    addPlayedTrack,
  };
})();

/* AudioVentura unified offline and notification worker. */
"use strict";

const APP_SHELL_VERSION = "v2";
const MEDIA_CACHE_VERSION = "v1";
const OFFLINE_DB_VERSION = 1;
const MAX_OFFLINE_HASH = 64;

const normalizedScope = () => {
  const pathname = new URL(self.registration.scope).pathname;
  return pathname.endsWith("/") ? pathname : `${pathname}/`;
};

const scopePath = normalizedScope();
const scopeKey = scopePath === "/" ? "root" : scopePath.slice(1, -1).replace(/[^A-Za-z0-9_-]+/g, "_");
const shellCacheName = `audioventura:${scopeKey}:shell:${APP_SHELL_VERSION}`;
const mediaCacheName = `audioventura:${scopeKey}:media:${MEDIA_CACHE_VERSION}`;
const databaseName = `audioventura-offline:${scopeKey}:v${OFFLINE_DB_VERSION}`;
const mediaPathPrefix = `${scopePath}media/library/`;
const syntheticPathPrefix = `${scopePath}__offline/media/sha256/`;
let offlineMode = false;
let offlineModeReason = "initial";

const setOfflineMode = (enabled, reason) => {
  offlineMode = enabled;
  offlineModeReason = reason;
};

const inScope = (url) => {
  if (url.origin !== self.location.origin || !url.pathname.startsWith(scopePath)) return false;
  // A root worker must never answer beta requests before the beta worker has
  // taken the more-specific scope.
  return !(scopePath === "/" && url.pathname.startsWith("/beta/"));
};

const shellUrls = () => [
  `${scopePath}offline-shell`,
  `${scopePath}manifest.webmanifest`,
  `${scopePath}static/app.css`,
  `${scopePath}static/player.js`,
  `${scopePath}static/app_shell.js`,
  `${scopePath}static/offline_store.js`,
  `${scopePath}static/offline_cache.js`,
  `${scopePath}static/icon-192.svg`,
  `${scopePath}static/icon-512.svg`,
];

const openDatabase = () => new Promise((resolve, reject) => {
  if (!self.indexedDB) { reject(new Error("storage unavailable")); return; }
  const request = self.indexedDB.open(databaseName, OFFLINE_DB_VERSION);
  request.onupgradeneeded = () => {
    const db = request.result;
    const create = (name, options, indexes) => {
      const store = db.objectStoreNames.contains(name) ? request.transaction.objectStore(name) : db.createObjectStore(name, options);
      indexes.forEach(([indexName, keyPath]) => { if (!store.indexNames.contains(indexName)) store.createIndex(indexName, keyPath); });
    };
    create("blobs", { keyPath: "sha256" }, [["state", "state"], ["lease_expires_at", "lease_expires_at"]]);
    create("media_urls", { keyPath: "pathname" }, [["sha256", "sha256"]]);
    create("playlists", { keyPath: "id" }, [["state", "state"]]);
    create("entries", { keyPath: ["playlist_id", "entry_key"] }, [["playlist_id", "playlist_id"], ["sha256", "sha256"]]);
    create("refs", { keyPath: ["playlist_id", "sha256"] }, [["playlist_id", "playlist_id"], ["sha256", "sha256"]]);
  };
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(new Error("storage unavailable"));
});

const idbGet = (store, key) => new Promise((resolve, reject) => {
  const request = store.get(key);
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(new Error("storage unavailable"));
});

const markBlobFailed = async (hash, code) => {
  let db;
  try {
    db = await openDatabase();
    await new Promise((resolve, reject) => {
      const transaction = db.transaction(["blobs"], "readwrite");
      const request = transaction.objectStore("blobs").get(hash);
      request.onsuccess = () => {
        const blob = request.result;
        if (blob) {
          blob.state = "failed";
          blob.last_error_code = code;
          blob.lease_owner = null;
          blob.lease_expires_at = 0;
          blob.updated_at = new Date().toISOString();
          transaction.objectStore("blobs").put(blob);
        }
      };
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(new Error("storage unavailable"));
    });
  } catch (_) {
    if (db) db.close();
  }
  if (db) db.close();
};

const mappedBlob = async (pathname) => {
  let db;
  try {
    db = await openDatabase();
    const transaction = db.transaction(["media_urls", "blobs"], "readonly");
    const mapping = await idbGet(transaction.objectStore("media_urls"), pathname);
    if (!mapping || typeof mapping.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(mapping.sha256) || mapping.mime_type !== "audio/mpeg") return null;
    const blob = await idbGet(transaction.objectStore("blobs"), mapping.sha256);
    return { mapping, blob };
  } catch (_) {
    return null;
  } finally {
    if (db) db.close();
  }
};

const mediaResponseHeaders = (source, hash, size, range) => {
  const headers = new Headers();
  headers.set("Content-Type", "audio/mpeg");
  headers.set("Accept-Ranges", "bytes");
  headers.set("ETag", `"sha256-${hash}"`);
  headers.set("Cache-Control", "private, no-store");
  headers.set("Content-Length", String(size));
  if (range) headers.set("Content-Range", `bytes ${range.start}-${range.end}/${range.size}`);
  if (source?.headers?.get("Content-Disposition")) headers.set("Content-Disposition", source.headers.get("Content-Disposition"));
  return headers;
};

const unsatisfiable = (size) => new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });

const parseSingleRange = (value, size) => {
  if (typeof value !== "string" || !value.toLowerCase().startsWith("bytes=") || value.slice(6).includes(",")) return null;
  const expression = value.slice(6).trim();
  const separator = expression.indexOf("-");
  if (separator < 0) return null;
  const startText = expression.slice(0, separator).trim();
  const endText = expression.slice(separator + 1).trim();
  let start;
  let end;
  if (!startText) {
    if (!/^\d+$/.test(endText) || Number(endText) < 1) return null;
    const suffix = Number(endText);
    if (!Number.isSafeInteger(suffix)) return null;
    start = Math.max(size - suffix, 0);
    end = size - 1;
  } else {
    if (!/^\d+$/.test(startText)) return null;
    start = Number(startText);
    if (!Number.isSafeInteger(start) || start >= size) return null;
    if (!endText) end = size - 1;
    else {
      if (!/^\d+$/.test(endText)) return null;
      end = Number(endText);
      if (!Number.isSafeInteger(end) || end < start) return null;
      end = Math.min(end, size - 1);
    }
  }
  return { start, end, size };
};

const serveCachedMedia = async (request, mapping, blob) => {
  if (!blob || blob.state !== "ready" || blob.sha256 !== mapping.sha256 || blob.byte_size !== mapping.byte_size || blob.mime_type !== "audio/mpeg") return null;
  const cache = await caches.open(mediaCacheName);
  const cached = await cache.match(new Request(blob.cache_url || `${self.location.origin}${syntheticPathPrefix}${blob.sha256}`));
  if (!cached || cached.status !== 200 || cached.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "audio/mpeg" || cached.headers.get("Content-Length") !== String(mapping.byte_size) || cached.headers.get("ETag") !== `"sha256-${mapping.sha256}"`) {
    await markBlobFailed(mapping.sha256, "missing");
    return null;
  }
  if (!request.headers.has("Range")) return cached;
  const range = parseSingleRange(request.headers.get("Range"), mapping.byte_size);
  if (!range) return unsatisfiable(mapping.byte_size);
  const body = await cached.blob();
  if (body.size !== mapping.byte_size) {
    await markBlobFailed(mapping.sha256, "missing");
    return null;
  }
  const sliced = body.slice(range.start, range.end + 1, "audio/mpeg");
  return new Response(sliced, { status: 206, headers: mediaResponseHeaders(cached, mapping.sha256, sliced.size, range) });
};

const mediaFetch = (event) => {
  const request = event.request;
  if (request.method !== "GET" || !inScope(new URL(request.url))) return fetch(request);
  const url = new URL(request.url);
  if (!url.pathname.startsWith(mediaPathPrefix) || !/^\d+$/.test(url.pathname.slice(mediaPathPrefix.length))) return fetch(request);
  const cachedFallback = () => mappedBlob(url.pathname).then((found) => found
    ? serveCachedMedia(request, found.mapping, found.blob).then((cached) => cached || fetch(request))
    : fetch(request));
  if (offlineMode || self.navigator?.onLine === false) return cachedFallback();
  // Keep ordinary online playback on the server's native media/range path.
  // A network failure then consults the browser-local mapping, which also
  // avoids opening IndexedDB during the document's first online load.
  return fetch(request).catch(() => cachedFallback());
};

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(shellCacheName);
    // addAll rejects the install if even one required public asset is absent.
    await cache.addAll(shellUrls());
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key.startsWith(`audioventura:${scopeKey}:shell:`) && key !== shellCacheName).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (!inScope(url)) return;
  if (request.mode === "navigate") {
    // Keep the network promise directly chained to the navigation event. This
    // avoids Firefox cancelling a navigation while an async function is
    // awaiting the same request, while preserving real HTTP error responses.
    const offlineShell = () => {
      setOfflineMode(true, "navigation-fallback");
      return caches.open(shellCacheName).then((cache) => cache.match(`${scopePath}offline-shell`).then((cached) => cached || Response.error()));
    };
    const networkNavigation = () => fetch(request).catch(offlineShell);
    event.respondWith(offlineMode || self.navigator?.onLine === false ? offlineShell() : networkNavigation());
    return;
  }
  if (shellUrls().includes(url.pathname) && request.method === "GET") {
    event.respondWith(caches.open(shellCacheName).then((cache) => cache.match(request).then((cached) => cached || fetch(request))));
    return;
  }
  if (url.pathname.startsWith(mediaPathPrefix)) {
    // Firefox cancels a controlled media navigation when an online media
    // request is needlessly re-served through respondWith(fetch(request)).
    // Leave online media on the native authenticated path; offline mode still
    // takes the mapped-cache path below, with network failure as a fallback.
    if (offlineMode || self.navigator?.onLine === false) event.respondWith(mediaFetch(event));
  }
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  const reply = (payload) => event.ports?.[0]?.postMessage(payload);
  if (data.type === "activate") {
    // skipWaiting is only reachable through an explicit page/user message.
    event.waitUntil(self.skipWaiting());
    reply({ type: "activation-requested", version: APP_SHELL_VERSION });
    return;
  }
  if (data.type === "health") {
    reply({ type: "offline-health", ok: true, scope: scopePath, shell: shellCacheName, media: mediaCacheName, version: APP_SHELL_VERSION, offline: offlineMode, reason: offlineModeReason, online: self.navigator?.onLine ?? null });
    return;
  }
  if (data.type === "storage") {
    event.waitUntil((async () => {
      const estimate = self.navigator?.storage?.estimate ? await self.navigator.storage.estimate() : {};
      reply({ type: "offline-storage", usage: estimate.usage ?? null, quota: estimate.quota ?? null });
    })());
    return;
  }
  if (data.type === "offline-mode") {
    if (data.enabled === true) {
      setOfflineMode(true, "page-offline");
      reply({ type: "offline-mode", enabled: offlineMode });
    } else {
      // navigator.onLine can be true while a browser has no route to the
      // origin. Verify before allowing online media/navigation behavior back.
      event.waitUntil((async () => {
        try {
          await fetch(`${self.location.origin}${scopePath}__offline-connectivity-probe?${Date.now()}`, { cache: "no-store" });
          setOfflineMode(false, "page-online");
        } catch (_) {
          setOfflineMode(true, "connectivity-probe-failed");
        }
        reply({ type: "offline-mode", enabled: offlineMode });
      })());
    }
    return;
  }
  if (data.type === "reconcile") {
    event.waitUntil((async () => {
      const keys = await caches.keys();
      reply({ type: "offline-reconciled", shell: keys.includes(shellCacheName), media: keys.includes(mediaCacheName) });
    })());
  }
});

self.addEventListener("push", (event) => {
  event.waitUntil((async () => {
    let payload;
    try { payload = event.data ? event.data.json() : null; } catch (_) { return; }
    const kinds = new Set([
      "generation_completed", "managed_generation_started",
      "capacity_retained_reminder", "capacity_release_warning",
      "capacity_released", "capacity_release_overdue",
    ]);
    if (!payload || !kinds.has(payload.kind) || typeof payload.event_key !== "string" ||
        typeof payload.title !== "string" || typeof payload.body !== "string" ||
        typeof payload.path !== "string" || !payload.path.startsWith("/") ||
        payload.path.startsWith("//") || payload.path.includes("://") ||
        payload.title.length > 128 || payload.body.length > 512 ||
        payload.event_key.length > 256) return;
    const target = new URL(payload.path.replace(/^\/+/, ""), self.registration.scope);
    if (target.origin !== self.location.origin ||
        !target.pathname.startsWith(new URL(self.registration.scope).pathname)) return;
    await self.registration.showNotification(payload.title, {
      body: payload.body, tag: payload.event_key, data: { path: target.pathname + target.search },
    });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const path = event.notification.data && event.notification.data.path;
    if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") || path.includes("://")) return;
    const target = new URL(path, self.location.origin);
    if (target.origin !== self.location.origin ||
        !target.pathname.startsWith(new URL(self.registration.scope).pathname)) return;
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (client.url.startsWith(self.registration.scope) && "focus" in client) {
        await client.focus();
        if ("navigate" in client) await client.navigate(target.href);
        return;
      }
    }
    await clients.openWindow(target.href);
  })());
});

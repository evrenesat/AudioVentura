(() => {
  "use strict";
  if (window.__audioventuraShellBound) return;
  window.__audioventuraShellBound = true;
  const workerUrl = document.querySelector('meta[name="offline-worker"]')?.content || "/notification-worker.js";
  const isOfflineShell = document.querySelector('meta[name="offline-shell"]')?.content === "true";
  let sawOfflineSignal = navigator.onLine === false;
  let navigationSequence = 0;
  let activeNavigationController = null;
  const tellWorkerConnectivity = (registration, enabled) => {
    try { registration?.active?.postMessage({ type: "offline-mode", enabled }); } catch (_) {}
  };
  const showUpdateNotice = (registration) => {
    if (!registration?.waiting || document.querySelector("[data-worker-update]")) return;
    const notice = document.createElement("aside");
    notice.className = "worker-update-notice";
    notice.dataset.workerUpdate = "true";
    notice.setAttribute("role", "status");
    const text = document.createElement("span");
    text.textContent = "A new offline player update is ready.";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button small";
    button.textContent = "Use update";
    button.addEventListener("click", () => {
      registration.waiting?.postMessage({ type: "activate" });
      notice.remove();
    });
    notice.append(text, button);
    document.body.append(notice);
  };
  const registerWorker = async () => {
    if (!("serviceWorker" in navigator)) return null;
    const location = new URL(workerUrl, window.location.origin);
    const scope = new URL("./", location).pathname;
    let registration;
    try {
      registration = await navigator.serviceWorker.register(location.href, { scope });
    } catch (_) {
      return null;
    }
    const announceUpdate = () => window.dispatchEvent(new CustomEvent("audioventura:worker-update", { detail: { registration } }));
    if (registration.waiting) announceUpdate();
    // Only assert offline state during startup when the browser explicitly
    // reports it. A shell reached through the worker's network fallback can
    // still see navigator.onLine=true and must not clear that worker state.
    if (navigator.onLine === false) tellWorkerConnectivity(registration, true);
    registration.addEventListener?.("updatefound", () => {
      const installing = registration.installing;
      installing?.addEventListener?.("statechange", () => {
        if (installing.state === "installed" && navigator.serviceWorker.controller) announceUpdate();
      });
    });
    return registration;
  };
  window.AudioventuraServiceWorkerRegistration = window.AudioventuraServiceWorkerRegistration || registerWorker();
  window.AudioventuraRegisterWorker = () => window.AudioventuraServiceWorkerRegistration;
  window.addEventListener("online", () => {
    if (!sawOfflineSignal || isOfflineShell) return;
    sawOfflineSignal = false;
    void window.AudioventuraServiceWorkerRegistration?.then((registration) => tellWorkerConnectivity(registration, false));
  });
  window.addEventListener("offline", () => {
    sawOfflineSignal = true;
    void window.AudioventuraServiceWorkerRegistration?.then((registration) => tellWorkerConnectivity(registration, true));
  });
  window.addEventListener("audioventura:worker-update", (event) => showUpdateNotice(event.detail?.registration));
  const main = () => document.querySelector("#app-main");
  const announce = (message) => {
    let node = document.querySelector("#app-shell-announcement");
    if (!node) {
      node = document.createElement("div");
      node.id = "app-shell-announcement";
      node.className = "sr-only";
      node.setAttribute("aria-live", "polite");
      document.body.append(node);
    }
    node.textContent = message;
  };
  const markCurrent = (url) => document.querySelectorAll("[data-app-nav]").forEach((link) => {
    link.setAttribute(
      "aria-current",
      new URL(link.href, window.location.href).pathname === url.pathname ? "page" : "false",
    );
  });
  const loadPageScripts = async (root) => {
    const sources = [...root.querySelectorAll("script[src]")].map((script) => script.src).filter(Boolean);
    await Promise.all(sources.map((src) => new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = resolve;
      document.body.append(script);
    })));
  };
  const navigate = async (href, replace = false) => {
    const target = new URL(href, window.location.href);
    if (target.origin !== window.location.origin || target.hash) return false;
    const sequence = ++navigationSequence;
    activeNavigationController?.abort();
    const controller = new AbortController();
    activeNavigationController = controller;
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(target.href, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
        signal: controller.signal,
      });
      if (sequence !== navigationSequence) return false;
      if (!response.ok || !(response.headers.get("content-type") || "").includes("text/html")) throw new Error("navigation failed");
      const documentText = await response.text();
      if (sequence !== navigationSequence) return false;
      const parsed = new DOMParser().parseFromString(documentText, "text/html");
      const replacement = parsed.querySelector("#app-main");
      const current = main();
      if (!replacement || !current) throw new Error("navigation shell missing");
      if (sequence !== navigationSequence) return false;
      current.replaceWith(replacement);
      document.title = parsed.title || document.title;
      if (replace) window.history.replaceState({}, "", target.href);
      else window.history.pushState({}, "", target.href);
      markCurrent(target);
      await loadPageScripts(replacement);
      window.dispatchEvent(new CustomEvent("audioventura:navigation", { detail: { url: target.href } }));
      replacement.querySelector("h1, [autofocus]")?.focus({ preventScroll: true });
      announce(`Loaded ${document.title}`);
      return true;
    } catch (_) {
      if (sequence !== navigationSequence) return false;
      window.location.href = target.href;
      return false;
    } finally {
      window.clearTimeout(timeout);
      if (sequence === navigationSequence) activeNavigationController = null;
    }
  };
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target === "_blank" || link.hasAttribute("download") || link.pathname.startsWith("/static/") || link.pathname.includes("/files/") || link.pathname.includes("/media/")) return;
    if (new URL(link.href, window.location.href).origin !== window.location.origin) return;
    event.preventDefault();
    navigate(link.href);
  });
  window.addEventListener("popstate", () => navigate(window.location.href, true));
  markCurrent(new URL(window.location.href));
})();

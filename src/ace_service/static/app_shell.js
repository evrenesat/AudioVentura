(() => {
  "use strict";
  if (window.__audioventuraShellBound) return;
  window.__audioventuraShellBound = true;
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
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(target.href, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
        signal: controller.signal,
      });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("text/html")) throw new Error("navigation failed");
      const documentText = await response.text();
      const parsed = new DOMParser().parseFromString(documentText, "text/html");
      const replacement = parsed.querySelector("#app-main");
      const current = main();
      if (!replacement || !current) throw new Error("navigation shell missing");
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
      window.location.href = target.href;
      return false;
    } finally {
      window.clearTimeout(timeout);
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

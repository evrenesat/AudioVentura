(() => {
  "use strict";

  const select = document.querySelector("#backend");
  if (!select) return;

  let choices = [];
  try {
    choices = JSON.parse(select.dataset.backendCapabilities || "[]");
  } catch (_error) {
    return;
  }

  const note = document.createElement("p");
  note.className = "hint";
  note.id = "backend-compatibility-note";
  select.insertAdjacentElement("afterend", note);
  const pricingNote = document.querySelector("#backend-pricing-note");
  const gpuEstimate = document.querySelector("#gpu-cost-estimate");
  const durationMode = document.querySelector("#duration_mode");
  const durationSeconds = document.querySelector("#duration_seconds");
  const variationCount = document.querySelector("#variation_count");
  let pricingController = null;
  let pricingTimer = null;

  const rememberKey = `ace-service-backend:${window.location.pathname}`;
  const fieldAliases = {
    prompt: ["description", "target_style"],
    duration: ["duration_seconds"],
    seed: ["seed"],
    lyrics: ["lyrics"],
    source_style: ["source_style"],
    source_lyrics: ["source_lyrics"],
    strength: ["strength", "audio_cover_strength"],
    start_seconds: ["start_seconds"],
    end_seconds: ["end_seconds"],
    before_seconds: ["before_seconds"],
    after_seconds: ["after_seconds"],
  };
  const fieldDefaults = new WeakMap();
  for (const wrapper of document.querySelectorAll("[data-backend-field]")) {
    const input = wrapper.querySelector("input, select, textarea");
    if (!input) continue;
    fieldDefaults.set(input, {
      disabled: input.disabled,
      required: input.required,
      min: input.getAttribute("min"),
      max: input.getAttribute("max"),
    });
  }

  const restoreAttribute = (element, name, value) => {
    if (value == null) element.removeAttribute(name);
    else element.setAttribute(name, value);
  };

  const selectedChoice = () => choices.find((item) => item.backend_id === select.value);

  const renderPricing = (pricing) => {
    if (!pricingNote) return;
    if (!pricing || pricing.applicable === false) {
      pricingNote.hidden = true;
      pricingNote.textContent = "";
      return;
    }
    pricingNote.hidden = false;
    if (!pricing.available) {
      pricingNote.textContent = "Fal price unavailable; generation is not blocked.";
      return;
    }
    const stale = pricing.stale ? " (stale)" : "";
    const total = pricing.total
      ? ` Estimated request total: ~$${pricing.total}.`
      : pricing.reference_total
        ? ` Published output-price reference: up to ~$${pricing.reference_total}; account total depends on runtime usage.`
        : " Enter a supported duration to estimate this request.";
    const unitLabel = pricing.unit_label || pricing.unit.replaceAll("_", " ");
    pricingNote.textContent =
      `Fal account rate: ~$${pricing.unit_price} per ${unitLabel}; fetched ${pricing.fetched_at}${stale}.${total}`;
  };

  const refreshPricing = () => {
    const choice = selectedChoice();
    const isFal = choice && choice.provider === "fal.ai";
    window.clearTimeout(pricingTimer);
    pricingTimer = null;
    if (gpuEstimate) gpuEstimate.hidden = Boolean(isFal);
    if (!pricingNote) return;
    if (!isFal) {
      if (pricingController) pricingController.abort();
      renderPricing({ applicable: false });
      return;
    }
    const pricingUrl = pricingNote.dataset.pricingUrl;
    if (!pricingUrl) return;
    pricingTimer = window.setTimeout(async () => {
      if (pricingController) pricingController.abort();
      pricingController = new AbortController();
      const url = new URL(pricingUrl, window.location.origin);
      url.searchParams.set("backend", select.value);
      url.searchParams.set("duration_mode", durationMode ? durationMode.value : "auto");
      url.searchParams.set("duration_seconds", durationSeconds ? durationSeconds.value : "");
      url.searchParams.set("variation_count", variationCount ? variationCount.value : "1");
      try {
        const response = await window.fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: pricingController.signal,
        });
        if (!response.ok) throw new Error("pricing request failed");
        renderPricing(await response.json());
      } catch (error) {
        if (error.name !== "AbortError") renderPricing({ available: false });
      }
    }, 150);
  };

  const update = () => {
    const choice = selectedChoice();
    if (!choice) return;
    try {
      window.sessionStorage.setItem(rememberKey, select.value);
    } catch (_error) {
      // Storage is optional; the server remains authoritative.
    }

    const nativeFormats = Array.isArray(choice.native_formats) ? choice.native_formats : [];
    const output = document.querySelector("#output_format");
    if (output && nativeFormats.length) {
      for (const option of output.options) {
        option.disabled = !nativeFormats.includes(option.value);
        option.hidden = option.disabled;
      }
      if (!nativeFormats.includes(output.value)) output.value = nativeFormats[0];
    }

    const fields = choice.fields || {};
    for (const wrapper of document.querySelectorAll("[data-backend-field]")) {
      const name = wrapper.getAttribute("data-backend-field");
      const policy = name ? fields[name] : null;
      wrapper.hidden = !policy;
      const input = wrapper.querySelector("input, select, textarea");
      if (input) {
        const defaults = fieldDefaults.get(input);
        if (!defaults) continue;
        input.disabled = !policy || defaults.disabled;
        input.required = Boolean(policy && policy.required);
        restoreAttribute(
          input,
          "min",
          policy && policy.minimum != null ? String(policy.minimum) : defaults.min,
        );
        restoreAttribute(
          input,
          "max",
          policy && policy.maximum != null ? String(policy.maximum) : defaults.max,
        );
      }
    }
    for (const [catalogName, aliases] of Object.entries(fieldAliases)) {
      const policy = fields[catalogName];
      for (const alias of aliases) {
        const element = document.querySelector(`#${alias}`);
        if (!element || !policy) continue;
        if (policy.minimum != null) element.min = policy.minimum;
        if (policy.maximum != null) element.max = policy.maximum;
        element.required = Boolean(policy.required);
      }
    }
    note.textContent = nativeFormats.length
      ? `${choice.label} · ${choice.operation.replaceAll("_", " ")} · native output: ${nativeFormats.join(", ").toUpperCase()}`
      : choice.label;
    refreshPricing();
  };

  try {
    const remembered = window.sessionStorage.getItem(rememberKey);
    if (remembered && choices.some((item) => item.backend_id === remembered)) {
      select.value = remembered;
    }
  } catch (_error) {
    // Storage is optional.
  }
  select.addEventListener("change", update);
  if (durationMode) durationMode.addEventListener("change", refreshPricing);
  if (durationSeconds) durationSeconds.addEventListener("input", refreshPricing);
  if (variationCount) variationCount.addEventListener("change", refreshPricing);
  update();
})();

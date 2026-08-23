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
    const isFal = choice.provider === "fal.ai";
    for (const wrapper of document.querySelectorAll("[data-backend-field]")) {
      const name = wrapper.getAttribute("data-backend-field");
      const policy = name ? fields[name] : null;
      wrapper.hidden = isFal && !policy;
      const input = wrapper.querySelector("input, select, textarea");
      if (input) {
        const defaults = fieldDefaults.get(input);
        if (!defaults) continue;
        input.disabled = isFal ? !policy : defaults.disabled;
        input.required = isFal ? Boolean(policy && policy.required) : defaults.required;
        restoreAttribute(
          input,
          "min",
          isFal && policy && policy.minimum != null ? String(policy.minimum) : defaults.min,
        );
        restoreAttribute(
          input,
          "max",
          isFal && policy && policy.maximum != null ? String(policy.maximum) : defaults.max,
        );
      }
    }
    for (const [catalogName, aliases] of Object.entries(fieldAliases)) {
      const policy = fields[catalogName];
      for (const alias of aliases) {
        const element = document.querySelector(`#${alias}`);
        if (!isFal || !element || !policy) continue;
        if (policy.minimum != null) element.min = policy.minimum;
        if (policy.maximum != null) element.max = policy.maximum;
        element.required = Boolean(policy.required);
      }
    }
    note.textContent = nativeFormats.length
      ? `${choice.label} · ${choice.operation.replaceAll("_", " ")} · native output: ${nativeFormats.join(", ").toUpperCase()}`
      : choice.label;
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
  update();
})();

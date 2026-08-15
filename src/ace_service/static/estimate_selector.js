(() => {
  "use strict";
  // Binds the informational "This request" total to the variation selector.
  // Every option carries its server-computed request text; this script only
  // swaps in the selected option's label — no client-side money arithmetic.
  const select = document.querySelector("#variation_count");
  const textNode = document.querySelector("#estimate-request-text");
  if (!select || !textNode) return;
  const update = () => {
    const option = select.options[select.selectedIndex];
    const text = option ? option.getAttribute("data-request-text") : null;
    if (text) textNode.textContent = text;
  };
  select.addEventListener("change", update);
  update();
})();

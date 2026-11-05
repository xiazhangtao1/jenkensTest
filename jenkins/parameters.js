/* Native Jenkins parameters remain intact for buildWithParameters and saved defaults. */
(() => {
  "use strict";

  if (!location.pathname.replace(/\/$/, "").endsWith("/job/x-ran-cloud/build")) return;

  function parameter(name) {
    const key = Array.from(document.querySelectorAll('input[name="name"]'))
      .find(input => input.value === name);
    return key && (key.closest('.jenkins-form-item') || key.closest('[name="parameter"]'));
  }

  function update() {
    const kind = parameter("BUILD_KIND");
    const select = kind && kind.querySelector('select[name="value"]');
    if (!select || !["service", "image"].includes(select.value)) return;
    const visibility = {
      SERVICE_TARGET: select.value === "service",
      IMAGE_TARGET: select.value === "image",
      IMAGE_TAG: select.value === "image",
    };
    let complete = true;
    Object.entries(visibility).forEach(([name, visible]) => {
      const row = parameter(name);
      if (!row) { complete = false; return; }
      row.hidden = !visible;
      // Jenkins layouts can define display on the parameter row, overriding [hidden].
      if (visible) row.style.removeProperty("display");
      else row.style.setProperty("display", "none", "important");
    });
    return complete;
  }

  function start() {
    document.addEventListener("change", update);
    // Jenkins may finish rendering parameter controls after DOMContentLoaded.
    if (!update()) {
      const observer = new MutationObserver(() => { if (update()) observer.disconnect(); });
      observer.observe(document.body, {childList: true, subtree: true});
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();

/* Native Jenkins parameters remain intact for buildWithParameters and saved defaults. */
(() => {
  "use strict";

  const path = location.pathname.replace(/\/$/, "");
  const history = /\/job\/x-ran-cloud\/[^/]+\/parameters$/.test(path);
  if (!history && !path.endsWith("/job/x-ran-cloud/build")) return;

  function parameter(name) {
    const key = Array.from(document.querySelectorAll('input[name="name"]'))
      .find(input => input.value === name);
    if (key) return key.closest('.jenkins-form-item') || key.closest('[name="parameter"]');
    // Read-only build Parameters pages identify rows by label rather than inputs.
    return Array.from(document.querySelectorAll('#main-panel .jenkins-form-item'))
      .find(row => row.querySelector('.jenkins-form-label')?.textContent.trim() === name);
  }

  function update() {
    const kind = parameter("BUILD_KIND");
    if (history && kind) {
      let next = kind;
      ["COMPILER_IMAGE_PULL_POLICY", "COMPILER_IMAGE"].forEach(name => {
        const row = parameter(name);
        if (row && row.parentNode === kind.parentNode) {
          if (row.nextElementSibling !== next) next.before(row);
          next = row;
        }
      });
    }
    const select = kind && kind.querySelector('select[name="value"]');
    const value = select ? select.value : kind?.querySelector('.jenkins-quote')?.textContent.trim();
    if (!["service", "image"].includes(value)) return;
    const visibility = {
      SERVICE_TARGET: value === "service",
      IMAGE_TARGET: value === "image",
      IMAGE_TAG: value === "image",
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

// The jobs API filters Job/Read before exporting descriptions. Only administrators
// can receive the maintenance job and its notice; no public systemMessage is used.
(() => {
  "use strict";
  const root = document.head.dataset.rooturl || "";
  const path = location.pathname.slice(root.length).replace(/\/$/, "");
  if (path !== "" && !/^\/view\/[^/]+$/.test(path)) return;

  async function notice() {
    const panel = document.querySelector("#main-panel");
    if (!panel) return;
    try {
      const response = await fetch(root + "/api/json?tree=jobs[name,description]", {credentials: "same-origin"});
      if (!response.ok) return;
      const data = await response.json();
      const job = (data.jobs || []).find(item => item.name === "storage-maintenance");
      if (!job || !job.description) return;
      const element = document.createElement("div");
      element.id = "xran-admin-notice";
      element.className = "jenkins-alert jenkins-alert-info";
      element.style.whiteSpace = "pre-line";
      element.textContent = job.description;
      panel.prepend(element);
    } catch (_) {
      // An unavailable notice must not interfere with the Jenkins dashboard.
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", notice, {once: true});
  else notice();
})();

// Fill [data-t] elements from CTX and schedule [data-at] animations (seconds, may be a CTX key).
function setup() {
  const C = window.CTX || {};
  document.querySelectorAll("[data-t]").forEach(el => {
    const v = el.dataset.t.split(".").reduce((o, k) => (o || {})[k], C);
    if (v !== undefined && v !== null) el.textContent = v;
  });
  document.querySelectorAll("[data-at]").forEach(el => {
    let at = el.dataset.at;
    at = isNaN(+at) ? at.split(".").reduce((o, k) => (o || {})[k], C) : +at;
    el.style.animationDelay = (at || 0) + "s";
  });
}
document.addEventListener("DOMContentLoaded", setup);

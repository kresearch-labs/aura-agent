/* prepaint.js — puts the person's look on <html> BEFORE the page draws, so it never flashes the
 * wrong colours. Plain JS, no imports.
 */
(function () {
  var C = {
    look: ["system", "graphite", "soft", "ink"],
    accent: ["teal", "violet", "amber", "blue"],
    density: ["comfortable", "compact"],
    contrast: ["system", "normal", "more"],
    motion: ["system", "full", "reduced"]
  };
  var D = { look: "system", accent: "teal", density: "comfortable", contrast: "system", motion: "system" };
  function get(k) {
    var v = null;
    try { v = window.localStorage.getItem("aura.team." + k); } catch (e) { v = null; }
    return C[k].indexOf(v) >= 0 ? v : D[k];
  }
  var dark = false;
  try { dark = window.matchMedia("(prefers-color-scheme: dark)").matches; } catch (e) { dark = false; }
  var look = get("look");
  if (look === "system") look = dark ? "graphite" : "soft";
  var h = document.documentElement;
  h.setAttribute("data-look", look);
  h.setAttribute("data-accent", get("accent"));
  h.setAttribute("data-density", get("density"));
  h.setAttribute("data-theme", look === "soft" ? "light" : "dark");
  var c = get("contrast"), m = get("motion");
  if (c !== "system") h.setAttribute("data-contrast", c); else h.removeAttribute("data-contrast");
  if (m !== "system") h.setAttribute("data-motion", m); else h.removeAttribute("data-motion");
})();

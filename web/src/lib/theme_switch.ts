// A theme switch changes every colour at once. Parts that fade their
// background (buttons, cards) would otherwise show the new text colour over
// a half-faded old background for a moment, which can be unreadable. While
// `data-switching` is on the root, the theme turns those fades off (see
// parts.css); it comes off again after the new colours are painted.
export function switchWithoutFades(change: () => void): void {
  if (typeof document === "undefined") { change(); return; }
  const root = document.documentElement;
  root.setAttribute("data-switching", "");
  change();
  void root.offsetHeight;   // apply the new colours with fades off
  const raf = typeof requestAnimationFrame === "function"
    ? requestAnimationFrame : (f: () => void) => setTimeout(f, 16);
  raf(() => raf(() => root.removeAttribute("data-switching")));
}

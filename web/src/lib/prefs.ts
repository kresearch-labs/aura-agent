// prefs.ts — the look settings as Svelte stores. Import it once from web/src/team/entry.ts
// (`import "../lib/prefs";`) so it starts before the first screen. - Each setting is a writable
// store, saved in this browser. - Changing any of them re-applies the attributes on <html> at
// once. - "Match my computer" follows the computer live (it changes when the computer switches
// between light and dark). - The old `theme` store in team/frame/shell_state.ts (the "Theme —
// dark / light" button and its tests) stays and is kept in step both ways.
import { writable, get, type Writable } from "svelte/store";
import { DEFAULTS, KEYS, pick, resolve, lookForTheme, type Prefs } from "./prefs_logic";
import { theme } from "../team/frame/shell_state";
import { switchWithoutFades } from "./theme_switch";

function read<K extends keyof Prefs>(k: K): Prefs[K] {
  try { return pick(k, globalThis.localStorage?.getItem(KEYS[k])); }
  catch { return DEFAULTS[k]; }
}
function save<K extends keyof Prefs>(k: K, v: Prefs[K]) {
  try { globalThis.localStorage?.setItem(KEYS[k], String(v)); } catch { /* private window */ }
}

function store<K extends keyof Prefs>(k: K): Writable<Prefs[K]> {
  const s = writable<Prefs[K]>(read(k));
  s.subscribe((v) => { save(k, v); apply(); });
  return s;
}

const darkQuery = typeof matchMedia === "function"
  ? matchMedia("(prefers-color-scheme: dark)") : null;

let ready = false;            // apply() waits until every store exists
export const look = store("look");
export const accent = store("accent");
export const density = store("density");
export const contrast = store("contrast");
export const motion = store("motion");
export const enterKey = store("enter");
ready = true;

export function current(): Prefs {
  return { look: get(look), accent: get(accent), density: get(density),
           contrast: get(contrast), motion: get(motion), enter: get(enterKey) };
}

let syncing = false;
export function apply() {
  if (!ready || typeof document === "undefined") return;
  const p = current();
  const want = resolve(p, darkQuery?.matches ?? true);
  const h = document.documentElement;
  const names = ["data-look", "data-accent", "data-density", "data-theme",
                 "data-contrast", "data-motion"];
  const differs = names.some((n) => h.getAttribute(n) !== (n in want ? want[n] : null));
  const write = () => {
    for (const name of names) {
      if (name in want) h.setAttribute(name, want[name]);
      else h.removeAttribute(name);
    }
  };
  if (differs) switchWithoutFades(write); else write();
  // keep the old theme store in step (its subscriber writes data-theme too —
  // same value, so nothing flickers)
  const t = p.look === "system" ? "system" : want["data-theme"];
  if (!syncing && get(theme) !== t) { syncing = true; theme.set(t as "dark" | "light" | "system"); syncing = false; }
}
apply();

// the old "Theme — dark/light" button → a look
theme.subscribe((t) => {
  if (syncing || !ready) return;
  const next = lookForTheme(t, get(look));
  if (next !== get(look)) { syncing = true; look.set(next); syncing = false; apply(); }
});

// "Match my computer" follows the computer live
darkQuery?.addEventListener?.("change", () => apply());

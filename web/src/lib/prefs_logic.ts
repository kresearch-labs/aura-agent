// prefs_logic.ts — the look settings, as plain functions (no Svelte, no DOM), so they can be
// unit-tested. Change both together or neither.

export type Look = "system" | "graphite" | "soft" | "ink";
export type Accent = "teal" | "violet" | "amber" | "blue";
export type Density = "comfortable" | "compact";
export type Contrast = "system" | "normal" | "more";
export type Motion = "system" | "full" | "reduced";
export type EnterKey = "enter" | "ctrl-enter";

export type Prefs = {
  look: Look; accent: Accent; density: Density;
  contrast: Contrast; motion: Motion; enter: EnterKey;
};

export const DEFAULTS: Prefs = {
  look: "system", accent: "teal", density: "comfortable",
  contrast: "system", motion: "system", enter: "enter",
};

/** localStorage key for each setting (one key each, plain strings). */
export const KEYS: Record<keyof Prefs, string> = {
  look: "aura.team.look", accent: "aura.team.accent", density: "aura.team.density",
  contrast: "aura.team.contrast", motion: "aura.team.motion", enter: "aura.team.enter",
};

export const CHOICES: { [K in keyof Prefs]: readonly Prefs[K][] } = {
  look: ["system", "graphite", "soft", "ink"],
  accent: ["teal", "violet", "amber", "blue"],
  density: ["comfortable", "compact"],
  contrast: ["system", "normal", "more"],
  motion: ["system", "full", "reduced"],
  enter: ["enter", "ctrl-enter"],
};

/** The words shown in Account › Appearance. */
export const LABELS: { [K in keyof Prefs]: Record<Prefs[K], string> } = {
  look: { system: "Match my computer", graphite: "Graphite", soft: "Soft light", ink: "Ink + glass" },
  accent: { teal: "Teal", violet: "Violet", amber: "Amber", blue: "Blue" },
  density: { comfortable: "Comfortable", compact: "Compact" },
  contrast: { system: "Match my computer", normal: "Normal", more: "More contrast" },
  motion: { system: "Match my computer", full: "Full", reduced: "Less motion" },
  enter: { enter: "Enter sends · Shift+Enter new line", "ctrl-enter": "Ctrl+Enter sends · Enter new line" },
};

/** A stored value, or the default when it's missing or not a known choice. */
export function pick<K extends keyof Prefs>(key: K, raw: string | null | undefined): Prefs[K] {
  return (CHOICES[key] as readonly string[]).includes(raw ?? "")
    ? (raw as Prefs[K]) : DEFAULTS[key];
}

/** What goes on <html>. `systemDark` = prefers-color-scheme: dark. */
export function resolve(p: Prefs, systemDark: boolean): Record<string, string> {
  const look = p.look === "system" ? (systemDark ? "graphite" : "soft") : p.look;
  const attrs: Record<string, string> = {
    "data-look": look,
    "data-accent": p.accent,
    "data-density": p.density,
    "data-theme": look === "soft" ? "light" : "dark",
  };
  // "system" leaves the attribute off so the CSS @media rules decide
  if (p.contrast !== "system") attrs["data-contrast"] = p.contrast;
  if (p.motion !== "system") attrs["data-motion"] = p.motion;
  return attrs;
}

/** The old "Theme — dark/light" toggle maps onto a look. */
export function lookForTheme(theme: "dark" | "light" | "system", current: Look): Look {
  if (theme === "system") return "system";
  if (theme === "light") return "soft";
  return current === "ink" ? "ink" : "graphite";
}

/** Does this key press send the message? (chat composer) */
export function isSend(e: { key: string; shiftKey: boolean; ctrlKey: boolean; metaKey: boolean; isComposing?: boolean },
                       mode: EnterKey): boolean {
  if (e.key !== "Enter" || e.isComposing) return false;   // isComposing: typing in Chinese/Japanese
  if (mode === "enter") return !e.shiftKey && !e.ctrlKey && !e.metaKey;
  return e.ctrlKey || e.metaKey;
}

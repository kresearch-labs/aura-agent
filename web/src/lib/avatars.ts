// avatars.ts — each person's picture: a drawn avatar (Lorelei by default, or
// Avataaars) made in the browser from their username or chosen seed, or
// their uploaded photo. The hub stores only the choice; no name leaves it.
//
// The drawing library loads only when the first drawn avatar is needed (its
// own file), so the page itself stays small. Until it arrives an avatar is
// just its soft colour circle; photos never wait for it.
import { writable, get } from "svelte/store";

export type AvatarStyle = "lorelei" | "avataaars";
export type AvatarInfo = { id: number; style: AvatarStyle; seed: string; photo: number };

export type AvatarChoice = { style: AvatarStyle; seed: string };
const STYLES: AvatarStyle[] = ["lorelei", "avataaars"];
/** A page of drawings to choose from, mixed from both sets (people never see
 *  which set a drawing came from). Page 0 starts with the person's own first
 *  drawing; the rest come from short seeds made from the name and the page,
 *  so the same page shows the same choices. */
export function avatarChoices(name: string, page: number, count = 18): AvatarChoice[] {
  const base = (name.toLowerCase().replace(/[^a-z0-9-]/g, "") || "x").slice(0, 20);
  const out: AvatarChoice[] = [];
  if (page === 0) out.push({ style: "lorelei", seed: "" });
  for (let i = 0; out.length < count; i++) {
    out.push({ style: STYLES[i % 2], seed: `${base}-${page}-${i}`.slice(0, 32) });
  }
  return out;
}

export const avatars = writable<Map<string, AvatarInfo>>(new Map());
/** true once the drawing library has loaded (avatars re-render then) */
export const drawReady = writable(false);

type Draw = (style: AvatarStyle, seed: string) => string;
let draw: Draw | null = null;
let loadingLib: Promise<void> | null = null;
function loadLib(): void {
  if (loadingLib) return;
  loadingLib = Promise.all([
    import("@dicebear/core"), import("@dicebear/lorelei"), import("@dicebear/avataaars"),
  ]).then(([core, lorelei, avataaars]) => {
    // the drawings are transparent: the avatar's own colour circle sits
    // behind them, so they suit light and dark alike
    draw = (style, seed) => (style === "avataaars"
      ? core.createAvatar(avataaars, { seed })
      : core.createAvatar(lorelei, { seed })).toDataUri();
    drawReady.set(true);
  }).catch(() => { loadingLib = null; });     // try again on the next avatar
}

const drawn = new Map<string, string>();       // style|seed → data URI
/** A drawing as an isolated image (inline SVGs share ids and break each
 *  other); "" until the library has loaded. */
export function drawAvatar(style: AvatarStyle, seed: string): string {
  if (!draw) { loadLib(); return ""; }
  const key = `${style}|${seed}`;
  let uri = drawn.get(key);
  if (!uri) { uri = draw(style, seed); drawn.set(key, uri); }
  return uri;
}

/** The picture for a person: their photo, else their drawing. Before the
 *  choices load, everyone is drawn in Lorelei from their username. Pass
 *  `ready` ($drawReady) so a caller re-renders when drawings arrive. */
export function avatarSrc(name: string, map: Map<string, AvatarInfo> = get(avatars), ready = get(drawReady)): string {
  void ready;
  const info = map.get(name);
  if (info?.photo) return `/api/v1/people/${info.id}/avatar?v=${info.photo}`;
  return drawAvatar(info?.style ?? "lorelei", info?.seed || name);
}

let loading: Promise<void> | null = null;
export function loadAvatars(force = false): Promise<void> {
  if (loading && !force) return loading;
  loading = fetch("/api/v1/avatars", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : { avatars: {} }))
    .then((d: { avatars?: Record<string, AvatarInfo> }) => {
      avatars.set(new Map(Object.entries(d.avatars ?? {})));
    })
    .catch(() => { /* drawings from usernames still show */ });
  return loading;
}

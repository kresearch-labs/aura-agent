// shell_state.ts — the app's minimal runtime: me, the permissions rule
// (menus-build-from this), projects, the hash router,
// theme (localStorage,). One source; everything else hangs off.
import { writable, derived, type Writable } from "svelte/store";
import { call, connect, signOut } from "../../lib/api/client";
import { switchWithoutFades } from "../../lib/theme_switch";

export type Me = {
  id: number; name: string; display: string;
  team_role: string; global_role: string; must_change: boolean;
};
export type Project = {
  id: number; slug: string; name: string; main_branch?: string;
  modules?: Record<string, boolean>;
  settings?: Record<string, unknown> | string;
  archived_at?: number | null;
  members?: { person: number; name: string; role: string }[];
};
export type Module =
  | "now" | "board" | "tree" | "flow" | "history" | "chat"
  | "admin" | "palette" | "password" | "account" | "signin";

// -------- persistent conveniences (lives in the browser only —) ----
const THEME_KEY = "aura.team.theme";
const initialTheme = (localStorage.getItem(THEME_KEY) as "dark" | "light" | null)
  ?? null;
export const theme: Writable<"dark" | "light" | "system"> =
  writable(initialTheme ?? "system");
theme.subscribe((t) => {
  if (typeof document === "undefined") return;   // unit-lane (vitest node)
  const root = document.documentElement;
  const was = root.getAttribute("data-theme");
  const want = t === "system" ? null : t;
  if (was !== want) switchWithoutFades(() => {
    if (want === null) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", want);
  });
  try { typeof localStorage !== "undefined" &&
        localStorage.setItem(THEME_KEY, t); } catch { /* the stub keeps */ }
});

// -------- me / perms: the menu law -------------------------------------
type MeState = { value: Me | null; loaded: boolean; error: string };
type PermState = { map: Map<number, Set<string>> };   // project → ticks

class MeStore {
  state = writable<MeState>({ value: null, loaded: false, error: "" });
  value: Me | null = null;
  loadEpoch = 0;

  async load() {
    const epoch = ++this.loadEpoch;
    try {
      const data = await call<{ person: Me }>("/me");
      if (epoch !== this.loadEpoch) return;
      this.value = data.person;
      this.state.set({ value: this.value, loaded: true, error: "" });
    } catch (e) {
      if (epoch !== this.loadEpoch) return;
      this.value = null;
      this.state.set({
        value: null, loaded: true,
        error: e instanceof Error ? e.message : "unreachable",
      });
    }
  }
  get isSignedIn(): boolean { return this.value !== null; }
}
export const meStore = new MeStore();
export const me = meStore.state;      // (loaded fidelity happens via $me.value)
// The whole frame appears only after the first permission answer. Otherwise
// the rail rearranges under the pointer while a person is trying to click it.
export const booting = writable<boolean>(true);

class PermStore {
  state = writable<PermState>({ map: new Map() });
  map = new Map<number, Set<string>>();

  /** May this person open that area of the project? (unknown yet: yes) */
  areaOpen(area: string, pid: number | undefined): boolean {
    if (!pid) return true;
    const s = this.map.get(pid);
    if (!s || ![...s].some((t) => t.startsWith("area:"))) return true;
    return s.has(`area:${area}`);
  }

  allowed(tick: string, projects: Project[] | undefined,
          pid: number | undefined): boolean {
    if (!pid) {
      for (const s of this.map.values()) if (s.has(tick)) return true;
      return false;
    }
    return this.map.get(pid)?.has(tick) ?? false;
  }

  async loadFor(pid: number) {
    if (this.map.has(pid)) return;
    try {
      const d = await call<{ permissions: string[]; modules?: string[] }>(
        `/me/permissions?project=${pid}`);
      const next = new Map(this.map);
      next.set(pid, new Set([...d.permissions, ...(d.modules ?? []).map((m) => `area:${m}`)]));
      this.map = next;
      this.state.set({ map: next });
    } catch {
      const next = new Map(this.map);
      next.set(pid, new Set());
      this.map = next;
      this.state.set({ map: next });
    }
  }

  clear() {
    this.map = new Map();
    this.state.set({ map: this.map });
  }
}
const _p = new PermStore();
export const perms = Object.assign(_p, {
  subscribe: (run: (v: PermState) => void,
              invalidate?: () => void) => _p.state.subscribe(run, invalidate),
});

// -------- projects + current ------------------------------------------
export const projects: Writable<Project[] | undefined> = writable(undefined);
export const currentProjectId: Writable<number | undefined> = writable(undefined);

function projectPreference(): number | undefined {
  const person = meStore.value?.id;
  if (!person) return undefined;       // never wear one person's memory on another
  try {
    const v = Number(localStorage.getItem(`aura.team.project.${person}`));
    return Number.isInteger(v) && v > 0 ? v : undefined;
  } catch { return undefined; }
}

// The picker's own lane.
export function setCurrentProject(pid: number | undefined) {
  currentProjectId.set(pid);
  if (pid === undefined) return;
  void perms.loadFor(pid);
  const person = meStore.value?.id;
  if (person) {
    try { localStorage.setItem(`aura.team.project.${person}`, String(pid)); }
    catch { /* a denied storage lane loses convenience, never permission */ }
  }
}

// -------- the ONE live stream -----------------------------------
// every event the server publishes for this connection lands here —
// same text, same order; pages subscribe to their kinds and refetch the
// data they own (never trust the event payload to hand-build rows).
export type LiveEvent = { kind: string; data: any; id: number; at: number };
export const live: Writable<LiveEvent | null> = writable(null);
let _es: EventSource | null = null;
let _esBackoff = 0;
let _lastLiveId = 0;
let _streamEpoch = 0;

// Keep the real transport id within this connection's epoch. A restarted
// stream is a new epoch, so a fresh hub ID sequence is not mistaken for a
// duplicate of the previous hub. Old closed connections cannot deliver here.
async function _streamSync(kind: string, data: any, id: number) {
  const transportId = Number.isSafeInteger(id) && id > 0 ? id : 0;
  if (transportId > 0 && transportId <= _lastLiveId) return;
  if (transportId > 0) _lastLiveId = transportId;
  live.set({ kind, data, id: transportId, at: Date.now() });
}

// The thin connection bar — "Reconnecting…" while the stream is down, "Back online" for 2 s on
// its return.
export const liveLink = writable<"up" | "down" | "back">("up");

export type RoomLite = { id: number; kind: string; name: string;
                          project?: number | null; unread?: number; mentions?: number };
export const chatRoomsPrefetch = writable<RoomLite[] | null>(null);
let _backTimer: ReturnType<typeof setTimeout> | undefined;

export function openStream(): void {
  if (_es || typeof window === "undefined") return;
  const epoch = ++_streamEpoch;
  _lastLiveId = 0;
  _es = connect((kind, data, id) => {
    if (epoch !== _streamEpoch) return;
    if (get_store(liveLink) !== "up") {
      liveLink.set("back");
      if (_backTimer) clearTimeout(_backTimer);
      _backTimer = setTimeout(() => liveLink.set("up"), 2000);
    }
    _esBackoff = 0;
    // the server's `reset` answers its own answer: the page reloads clean
    // ('reset' never leaves the server beyond the ring's own answer)
    if (kind === "reset") { location.reload(); return; }
    void _streamSync(kind, data, id);
  });
  _es.onerror = () => {
    // A later hub can begin its event numbering again. This still-current
    // connection epoch may learn that sequence; a superseded one cannot.
    if (epoch === _streamEpoch) {
      if (get_store(liveLink) === "up") liveLink.set("down");
      _lastLiveId = 0;
    }
    // EventSource retries on its own (~3 s); the notifier lane stays
    // silent — the hub's death row (hub-off is normal) never blares.
  };
}

export function closeStream(): void {
  ++_streamEpoch;
  _lastLiveId = 0;
  _es?.close();
  _es = null;
}

// alias for readability in stream handlers
import { get as get_store } from "svelte/store";

// -------- hashing router (the old page's hashes still resolve) ---------
function parseHash(): { module: Module; sub?: string } {
  const h = location.hash.replace(/^#\/?/, "") || "now";
  const [module, sub] = h.split("/");
  return { module: ((module || "now") as Module), sub };
}
const _route = writable(parseHash());
export const route = {
  subscribe: _route.subscribe,
  go(module: Module | string, sub?: string) {
    const m = String(module);
    location.hash = sub ? `#/${m}/${sub}` : `#/${m}`;
    // Consumers must see the selected identity NOW, not a previous hash
    // while the browser's later hashchange event is still queued.
    _route.set({module: m as Module, sub});
  },
};
if (typeof window !== "undefined") {
  window.addEventListener("hashchange", () => _route.set(parseHash()));
}

// -------- boot: me → projects → streams (payloads are shape) ----
let booted = false;

export async function boot() {
  if (booted) return;
  booted = true;
  await bootHome();
}
// law: sign-in isn't a page flip — the shell's own "projects live
// now" lane re-runs and fills the project picker. Calling boot() again
// is cheap: its guard only owns the shell (once), never this re-entry.
export async function refreshProjects() {
  // A project/module or personal-role edit may have changed this answer.
  // Always invalidate BEFORE reselecting, not only during initial boot.
  perms.clear();
  const d = await call<{ projects: Project[] }>("/projects");
  // Archived work stays in its own server records, not in the active picker.
  const available = (d.projects ?? []).filter((p) => !p.archived_at);
  projects.set(available);
  if (!available.length) { setCurrentProject(undefined); return; }
  const current = available.find((p) => p.id === currentId());
  const choice = current
    ?? available.find((p) => p.id === projectPreference())
    ?? available.find((p) => p.slug !== "general")
    ?? available[0];
  setCurrentProject(choice.id);
  // This is a first-class readiness boundary: click targets are not shown
  // until the precise project/role ticks have arrived, not a vague splash delay.
  await perms.loadFor(choice.id);
}

function currentId(): number | undefined {
  let current: number | undefined;
  const unsub = currentProjectId.subscribe((v) => { current = v; });
  unsub();
  return current;
}

export async function bootHome() {
  await meStore.load();
  if (!meStore.isSignedIn) {
    closeStream();
    route.go("signin");
    booting.set(false);
    return;
  }
  // an account the admin made must choose its own password first: the hub
  // answers nothing else until then, so the page goes straight there
  if (meStore.value?.must_change) {
    closeStream();
    route.go("password");
    booting.set(false);
    return;
  }
  // Stream on every page-load that proves sign-in
  openStream();
  perms.clear();
  try {
    await refreshProjects();
  } catch {
    projects.set([]);
  } finally {
    booting.set(false);
  }
}

// The server-side session is already ended when this transition runs.
// Share it with normal sign-out and the account page's current-row action.
export function completeSignOut() {
  closeStream();
  route.go("signin");
  location.hash = "#/signin";
  location.reload();                        // discard all cached session content
}

export async function signOutAndGoto() {
  await signOut();
  completeSignOut();
}

// aura_state.ts — what each task's Aura is doing, worked out from data the hub ALREADY sends
// (no hub change). Sources: GET api/v1/projects/<pid>/activity → { events: ActivityEvent[] }
// newest first; the types we use: task.started, task.blocked, task.finished (Aura sends them
// from teams_run.py) GET api/v1/projects/<pid>/map → { collisions: Collision[] } files that two
// or more people's Auras are changing

export type ActivityEvent = {
  seq: number; type: string; who: string | null; ts: number;   // ts = seconds
  sentence?: string;
  data?: { task?: number | string; changed?: string[] | null; commit?: string | null; failed?: number | null };
  task_ref?: { number: number; title: string } | null;
};
export type Collision = { path: string; people: { person: string; task: number | string }[] };

export type AuraState =
  | { kind: "working"; who: string; since: number }
  | { kind: "waiting"; who: string; since: number }
  | { kind: "done"; who: string; at: number; files: number };

export type Clash = { others: string[]; paths: string[] };

const KINDS: Record<string, AuraState["kind"]> = {
  "task.started": "working", "task.blocked": "waiting", "task.finished": "done",
};

/** task number → its Aura's latest state. Events must be newest first (as
 *  the hub sends them). The newest of started/blocked/finished wins. */
export function auraStates(events: ActivityEvent[]): Map<number, AuraState> {
  const out = new Map<number, AuraState>();
  for (const e of events) {
    const kind = KINDS[e.type];
    const n = e.task_ref?.number;
    if (!kind || n == null || out.has(n)) continue;
    const who = e.who ?? "someone";
    if (kind === "done") out.set(n, { kind, who, at: e.ts, files: e.data?.changed?.length ?? 0 });
    else out.set(n, { kind, who, since: e.ts });
  }
  return out;
}

/** The clash warning for one task: the OTHER people changing the same files.
 *  The map names a task by its number or its id, so pass both. */
export function clashFor(task: { id: number; number: number; people: string[] },
                         collisions: Collision[]): Clash | null {
  const others = new Set<string>();
  const paths: string[] = [];
  for (const c of collisions) {
    const mine = c.people.some((p) => String(p.task) === String(task.number) || String(p.task) === String(task.id));
    if (!mine) continue;
    paths.push(c.path);
    for (const p of c.people)
      if (String(p.task) !== String(task.number) && String(p.task) !== String(task.id)) others.add(p.person);
  }
  return paths.length ? { others: [...others], paths } : null;
}

/** "4 min", "1 h 5 min", "2 days" — plain words, never "4m". */
export function elapsed(fromSeconds: number, nowSeconds: number): string {
  const m = Math.max(0, Math.floor((nowSeconds - fromSeconds) / 60));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return d === 1 ? "1 day" : `${d} days`;
}

/** "Asha's Aura" / "Your Aura" */
export function auraName(who: string, me: string, display: (u: string) => string): string {
  return who === me ? "Your Aura" : `${display(who)}'s Aura`;
}

/** The words on the card strip. Plain words; the ◆ mark is drawn by the
 *  component, not part of the text. */
export function stripText(s: AuraState, me: string, display: (u: string) => string, now: number): string {
  const name = auraName(s.who, me, display);
  if (s.kind === "working") return `${name} is working · ${elapsed(s.since, now)}`;
  if (s.kind === "waiting") return s.who === me ? `${name} is waiting for you` : `${name} is waiting for ${display(s.who)}`;
  const files = s.files === 1 ? "1 file changed" : `${s.files} files changed`;
  return `${name} finished · ${files}`;
}

/** The clash words. */
export function clashText(c: Clash, me: string, display: (u: string) => string): string {
  const names = c.others.map((o) => auraName(o, me, display));
  const who = names.length <= 1 ? (names[0] ?? "Another Aura")
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `${who} changed ${c.paths.length === 1 ? "this file" : "these files"} too — check before merging`;
}

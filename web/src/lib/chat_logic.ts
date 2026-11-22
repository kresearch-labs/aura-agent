// chat_logic.ts — chat helpers, plain functions. The hub stores ONE file per message. "Several
// pictures in one message" is therefore done in the page: picture-only messages sent by the
// same person one after another (within 2 minutes) are shown together as one grid.

export type FileInfo = { id: string; name?: string; type?: string; size?: number; width?: number; height?: number };
export type Msg = {
  id: number; person: { name: string } | string; body: string; ts: number;   // ts = seconds
  deleted?: boolean | null; reply_to?: number | null;
  file?: string | FileInfo | null;
};

export const personOf = (m: Msg) => (typeof m.person === "string" ? m.person : m.person.name);
export const fileOf = (m: Msg): FileInfo | null =>
  m.file == null ? null : typeof m.file === "string" ? { id: m.file } : m.file;
export const isImage = (f: FileInfo | null) =>
  !!f && (/^image\//.test(f.type ?? "") || /\.(png|jpe?g|gif|webp|avif|bmp)$/i.test(f.name ?? ""));

/** One row in the message list. `pictures` holds every picture of a run. */
export type Row = { msg: Msg; pictures: FileInfo[]; first: boolean };

const RUN_GAP = 120;     // seconds: pictures closer than this join one grid
const GROUP_GAP = 300;   // seconds: same person within 5 min → no new name line

/** Turn messages (oldest first) into rows: picture runs merged, and `first`
 *  = this row shows the avatar + name (false = a continuation row). */
export function rows(msgs: Msg[]): Row[] {
  const out: Row[] = [];
  for (const m of msgs) {
    const f = fileOf(m);
    const prev = out[out.length - 1];
    // a picture with its own words starts a new row (its words are the caption)
    const pictureOnly = isImage(f) && !m.deleted && !m.reply_to && !m.body.trim();
    if (prev && pictureOnly && prev.pictures.length
        && personOf(prev.msg) === personOf(m)
        && m.ts - lastTs(prev) <= RUN_GAP) {
      prev.pictures.push(f!);
      (prev as Row & { _last?: number })._last = m.ts;
      continue;
    }
    const first = !prev || personOf(prev.msg) !== personOf(m)
      || m.ts - lastTs(prev) > GROUP_GAP || dayOf(m.ts) !== dayOf(prev.msg.ts);
    out.push({ msg: m, pictures: isImage(f) && !m.deleted ? [f!] : [], first });
  }
  return out;
}
function lastTs(r: Row) { return (r as Row & { _last?: number })._last ?? r.msg.ts; }
const dayOf = (ts: number) => new Date(ts * 1000).toDateString();

/** The grid shape for n pictures. */
export function grid(n: number): { cls: "n1" | "n2" | "n3" | "n4"; shown: number; more: number } {
  if (n <= 1) return { cls: "n1", shown: 1, more: 0 };
  if (n === 2) return { cls: "n2", shown: 2, more: 0 };
  if (n === 3) return { cls: "n3", shown: 3, more: 0 };
  return { cls: "n4", shown: 4, more: n - 4 };
}

/** Task numbers mentioned as #12 in a message (each once, in order). Ignores
 *  #12 inside `code` and inside links. */
export function taskRefs(body: string): number[] {
  const clean = body.replace(/`[^`]*`/g, " ").replace(/https?:\/\/\S+/g, " ");
  const seen = new Set<number>();
  for (const m of clean.matchAll(/(^|[\s(])#(\d{1,6})\b/g)) seen.add(Number(m[2]));
  return [...seen];
}

/** "1.2 MB", "820 KB", "12 bytes" */
export function sizeText(bytes: number | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
}

/** What kind of file — picks the icon and the small tag on the file card. */
export function fileKind(f: FileInfo): { kind: "image" | "pdf" | "code" | "archive" | "doc" | "sheet" | "video" | "file"; tag: string } {
  const name = (f.name ?? "").toLowerCase(), type = f.type ?? "";
  const ext = name.includes(".") ? name.split(".").pop()! : "";
  const tag = (ext || type.split("/").pop() || "file").slice(0, 4).toUpperCase();
  if (isImage(f)) return { kind: "image", tag };
  if (type === "application/pdf" || ext === "pdf") return { kind: "pdf", tag: "PDF" };
  if (/^video\//.test(type) || ["mp4", "mov", "webm", "mkv"].includes(ext)) return { kind: "video", tag };
  if (["zip", "gz", "tgz", "7z", "rar", "tar"].includes(ext)) return { kind: "archive", tag };
  if (["csv", "xlsx", "xls", "ods"].includes(ext)) return { kind: "sheet", tag };
  if (["doc", "docx", "odt", "rtf", "txt", "md"].includes(ext)) return { kind: "doc", tag };
  if (["py", "ts", "js", "svelte", "rs", "go", "java", "c", "cpp", "json", "yaml", "yml", "toml", "sh", "html", "css"].includes(ext))
    return { kind: "code", tag };
  return { kind: "file", tag };
}

/** Half-written messages, one per room, kept in this browser. */
const DRAFT_KEY = (room: number) => `aura.team.draft.${room}`;
export const drafts = {
  get(room: number): string {
    try { return globalThis.localStorage?.getItem(DRAFT_KEY(room)) ?? ""; } catch { return ""; }
  },
  set(room: number, text: string) {
    try {
      if (text.trim()) globalThis.localStorage?.setItem(DRAFT_KEY(room), text);
      else globalThis.localStorage?.removeItem(DRAFT_KEY(room));
    } catch { /* private window: drafts just aren't kept */ }
  },
};

/** Room names: the hub may store "#general" — show one # only. */
export const roomName = (name: string) => name.replace(/^#+/, "");
/** A direct message's room is stored as "DM king–mira": on screen it is
 *  just the other person ("mira"); a DM with yourself stays your name. */
export function dmName(name: string, me: string): string {
  const people = name.replace(/^DM\s+/i, "").split(/\s*[–—-]\s*/).filter(Boolean);
  return people.find((p) => p !== me) ?? people[0] ?? name;
}
/** "1 member", "4 members" */
export const members = (n: number) => `${n} ${n === 1 ? "member" : "members"}`;

/** Avatar colour: the same person always gets the same of the 10 hues. */
const HUES = ["grey", "red", "orange", "amber", "green", "teal", "blue", "indigo", "violet", "pink"] as const;
export function hueFor(username: string): string {
  let h = 0;
  for (const ch of username.toLowerCase()) h = (h * 31 + ch.codePointAt(0)!) >>> 0;
  return `var(--hue-${HUES[h % HUES.length]})`;
}
/** "Ravi Kumar" → "RK", "asha" → "AS", "" → "?" */
export function initials(display: string): string {
  const words = display.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

// Due-date presentation — "Today"/"Tomorrow"/date, hot when today-or-overdue
export function dueState(due: number | undefined, now = Date.now() / 1000) {
  if (!due) return null;
  const d = new Date(due * 1000), t = new Date(now * 1000);
  const startOfToday = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime() / 1000;
  const hot = due < startOfToday + 86400;              // today or overdue
  const sameYear = d.getFullYear() === t.getFullYear();
  const text = due >= startOfToday && due < startOfToday + 86400 ? "Today"
    : due >= startOfToday + 86400 && due < startOfToday + 2 * 86400 ? "Tomorrow"
    : d.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" });
  return { hot, text };
}

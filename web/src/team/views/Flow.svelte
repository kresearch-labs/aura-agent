<!-- Flow.svelte — the project in time, and how its tasks depend on each other.
     Our own SVG, no chart library.
       timeline: a lane per person (or a row per task), names always shown on
                 the left, a two-level time axis that fits the work (minutes …
                 months), fixed-height bars, a "now" line, Aura's moments on the
                 bar (files, tests, commit/push, merged), clashes, blocked-by
                 arrows (red when the order is wrong). Real pixels: nothing is
                 stretched to the page; wide spans scroll sideways.
       diagram:  boxes sized to their text, layered left → right by blocked-by,
                 one barycenter pass, unlinked tasks in their own block, zoom
                 that never goes past 100 % on "fit". -->
<script lang="ts">
  import { onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { currentProjectId, route } from "../frame/shell_state";
  import { hueFor, initials } from "../../lib/chat_logic";

  type Ev = { t: number; kind: string; who?: string | null; path?: string;
              ok?: boolean; passed?: number; failed?: number;
              commit?: string; pushed?: boolean };
  type Row = { number: number; title: string; status_type: string; status?: string;
               start: number | null; end: number | null; scheduled: boolean;
               progress: number; blocked_by: number[]; people?: string[];
               merged_at?: number | null; events?: Ev[] };
  type Clash = { path: string; tasks: number[]; people: string[]; at: number };

  let rows = $state<Row[]>([]);
  let clashes = $state<Clash[]>([]);
  let hubNow = $state(0);
  let loading = $state(true);
  let error = $state("");
  let group = $state<"person" | "task">("person");

  const STATUS_COLORS: Record<string, string> = {
    backlog: "var(--muted)", todo: "var(--text-2)", in_progress: "var(--accent)",
    in_review: "var(--warn)", done: "var(--ok)", canceled: "var(--line-strong)",
  };
  const colorOf = (s: string) => STATUS_COLORS[s] ?? "var(--line-strong)";
  const STATUS_WORD: Record<string, string> = {
    backlog: "backlog", todo: "to do", in_progress: "in progress",
    in_review: "in review", done: "done", canceled: "canceled",
  };

  // ------------------------------------------------------------ timeline
  const ROW = 34, LANE = 30, AXIS = 44, BAR = 16, PAD = 24;
  const MIN = 60, HOUR = 3600, DAY = 86400;
  // px per second for each zoom step: one unit ≈ 100 px
  const ZOOMS: { key: string; label: string; pps: number }[] = [
    { key: "min", label: "Minutes", pps: 100 / MIN },
    { key: "hour", label: "Hours", pps: 100 / HOUR },
    { key: "day", label: "Days", pps: 100 / DAY },
    { key: "week", label: "Weeks", pps: 100 / (7 * DAY) },
    { key: "month", label: "Months", pps: 100 / (30 * DAY) },
  ];
  let zoomKey = $state("fit");
  let pps = $state(1);
  let scroller: HTMLDivElement | undefined = $state();
  let paneW = $state(800);

  const now = $derived(hubNow || Date.now() / 1000);
  const timed = $derived(rows.filter((r) => r.scheduled && r.start != null));
  const span = $derived.by(() => {
    if (!timed.length) return { a: now - HOUR, b: now };
    let a = Infinity, b = -Infinity;
    for (const r of timed) {
      a = Math.min(a, r.start!);
      b = Math.max(b, endOf(r), r.merged_at ?? 0);
    }
    b = Math.max(b, now);
    if (b - a < 2 * MIN) a = b - 2 * MIN;
    const m = (b - a) * 0.04;                       // a little air on both sides
    return { a: a - m, b: b + m };
  });
  function endOf(r: Row): number {
    if (r.status_type === "in_progress" || r.status_type === "in_review") return now;
    return r.end ?? r.start ?? now;
  }
  const fitPps = $derived(Math.max(1e-6, (paneW - PAD * 2) / Math.max(span.b - span.a, 1)));
  $effect(() => { pps = zoomKey === "fit" ? fitPps : (ZOOMS.find((z) => z.key === zoomKey)?.pps ?? fitPps); });
  const chartW = $derived(Math.max(paneW, Math.ceil((span.b - span.a) * pps) + PAD * 2));
  const X = (t: number) => PAD + (t - span.a) * pps;

  // rows of the chart, top to bottom: lane headers and task rows
  type Line = { kind: "lane"; name: string; y: number; count: number }
            | { kind: "task"; row: Row; y: number };
  const lines = $derived.by(() => {
    const out: Line[] = [];
    let y = 0;
    const byStart = (p: Row, q: Row) =>
      (p.start ?? Infinity) - (q.start ?? Infinity) || p.number - q.number;
    if (group === "task") {
      for (const r of [...rows].sort(byStart)) { out.push({ kind: "task", row: r, y }); y += ROW; }
      return out;
    }
    const lanes = new Map<string, Row[]>();
    for (const r of rows) {
      const who = r.people?.length ? r.people : ["(nobody yet)"];
      for (const p of who) (lanes.get(p) ?? lanes.set(p, []).get(p)!).push(r);
    }
    const names = [...lanes.keys()].sort((p, q) =>
      p === "(nobody yet)" ? 1 : q === "(nobody yet)" ? -1 : p.localeCompare(q));
    for (const name of names) {
      const list = lanes.get(name)!.sort(byStart);
      out.push({ kind: "lane", name, y, count: list.length }); y += LANE;
      for (const r of list) { out.push({ kind: "task", row: r, y }); y += ROW; }
    }
    return out;
  });
  const chartH = $derived(lines.length ? lines[lines.length - 1].y + ROW + 8 : 60);
  // where each task's row sits (the first one, in lane view a task with two
  // people appears in both lanes)
  const rowY = $derived.by(() => {
    const m = new Map<number, number>();
    for (const l of lines) if (l.kind === "task" && !m.has(l.row.number)) m.set(l.row.number, l.y);
    return m;
  });

  // the time axis: a nice step whose labels sit ≥ 80 px apart
  const STEPS = [MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR,
                 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY,
                 91 * DAY, 365 * DAY];
  const ticks = $derived.by(() => {
    const step = STEPS.find((s) => s * pps >= 80) ?? 365 * DAY;
    const minor: { x: number; label: string }[] = [];
    const major: { x: number; label: string }[] = [];
    const tz = new Date().getTimezoneOffset() * 60;
    const align = (t: number, s: number) =>
      s >= DAY ? Math.ceil((t - tz) / s) * s + tz : Math.ceil(t / s) * s;
    const nowX = X(now);
    for (let t = align(span.a, step); t <= span.b && minor.length < 400; t += step) {
      const d = new Date(t * 1000);
      if (Math.abs(X(t) - nowX) < 34 && Math.abs(X(t) - nowX) > 0.5) {
        minor.push({ x: X(t), label: "" });          // "now" is written there
        continue;
      }
      minor.push({ x: X(t), label: step < DAY
        ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : step < 28 * DAY ? d.toLocaleDateString([], { day: "numeric", month: "short" })
        : d.toLocaleDateString([], { month: "short", year: "numeric" }) });
    }
    // the bigger unit above: the day (for hour/minute steps) or the year
    const big = step < DAY ? DAY : 365 * DAY;
    const first = new Date(span.a * 1000);
    let d = step < DAY ? new Date(first.getFullYear(), first.getMonth(), first.getDate())
                       : new Date(first.getFullYear(), 0, 1);
    major.push({ x: Math.max(PAD, X(d.getTime() / 1000)), label: big === DAY
      ? d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
      : String(d.getFullYear()) });
    for (let i = 0; i < 400; i++) {
      d = big === DAY ? new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)
                      : new Date(d.getFullYear() + 1, 0, 1);
      const t = d.getTime() / 1000;
      if (t > span.b) break;
      major.push({ x: X(t), label: big === DAY
        ? d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
        : String(d.getFullYear()) });
    }
    return { minor, major };
  });

  // blocked-by arrows: from the end of the blocker's bar to the start of
  // the blocked one; red when the blocker ends after the blocked one starts
  const arrows = $derived.by(() => {
    const out: { d: string; late: boolean; title: string }[] = [];
    const byNum = new Map(rows.map((r) => [r.number, r]));
    for (const r of timed) {
      for (const b of r.blocked_by) {
        const blocker = byNum.get(b);
        if (!blocker || !blocker.scheduled || blocker.start == null) continue;
        const y1 = (rowY.get(b) ?? 0) + ROW / 2, y2 = (rowY.get(r.number) ?? 0) + ROW / 2;
        const x1 = X(endOf(blocker)), x2 = X(r.start!);
        let d: string;
        if (x2 - x1 >= 16) {
          const mx = (x1 + x2) / 2;
          d = `M${x1} ${y1} H${mx} V${y2} H${x2 - 2}`;
        } else {
          // the blocked task started first: out of the blocker's end, along
          // the gap between the rows, back to the blocked bar's start
          const gap = y2 > y1 ? y2 - ROW / 2 : y2 + ROW / 2;
          d = `M${x1} ${y1} h8 V${gap} H${x2 - 10} V${y2} H${x2 - 2}`;
        }
        out.push({ d,
                   late: endOf(blocker) > r.start!,
                   title: `#${b} blocks #${r.number}` });
      }
    }
    return out;
  });

  // the last thing that really happened (an event, a finish, a merge) —
  // an open task's bar runs on to "now", but the work is where the events are
  const lastWork = $derived.by(() => {
    let t = 0;
    for (const r of timed) {
      t = Math.max(t, r.start ?? 0, r.merged_at ?? 0,
                   r.status_type === "done" ? (r.end ?? 0) : 0);
      for (const e of r.events ?? []) t = Math.max(t, e.t);
    }
    return t || now;
  });
  function zoom(key: string) {
    // from "fit" the new view lands on the latest work; between steps it
    // keeps what was in the middle
    const center = zoomKey === "fit" || !scroller ? lastWork
      : (scroller.scrollLeft + scroller.clientWidth / 2 - PAD) / pps + span.a;
    const atRight = zoomKey === "fit";
    zoomKey = key;
    queueMicrotask(() => {
      if (!scroller) return;
      scroller.scrollLeft = Math.max(0, X(center) - scroller.clientWidth * (atRight ? 0.7 : 0.5));
    });
  }
  const dateRange = $derived.by(() => {
    const f = (t: number) => new Date(t * 1000).toLocaleDateString([], { day: "numeric", month: "short" });
    const a = f(span.a), b = f(span.b);
    return a === b ? new Date(span.a * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
                   : `${a} – ${b}`;
  });
  function toNow() {
    if (scroller) scroller.scrollLeft = Math.max(0, X(now) - scroller.clientWidth * 0.7);
  }
  function openTask(n: number) { route.go("board", String(n)); }
  function short(t: number): string {
    return new Date(t * 1000).toLocaleString([], { day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit" });
  }
  function evTitle(r: Row, e: Ev): string {
    const at = short(e.t);
    if (e.kind === "file") return `${e.who ?? ""} changed ${e.path} · ${at}`;
    if (e.kind === "ran") return `tests ${e.ok ? "passed" : "FAILED"}` +
      (e.passed || e.failed ? ` (${e.passed ?? 0} passed, ${e.failed ?? 0} failed)` : "") + ` · ${at}`;
    if (e.kind === "finished") return `finished #${r.number}` + (e.commit ? ` · commit ${e.commit}` : "") +
      (e.pushed ? " · pushed" : "") + ` · ${at}`;
    if (e.kind === "blocked") return `blocked · ${at}`;
    return at;
  }

  async function refresh(pid: number) {
    loading = rows.length === 0; error = "";
    try {
      const d = await call<{ tasks: Row[]; clashes?: Clash[]; now?: number }>(`/projects/${pid}/gantt`);
      rows = d.tasks; clashes = d.clashes ?? []; hubNow = d.now ?? 0;
    } catch (e) { error = e instanceof Error ? e.message : "no answer"; }
    finally { loading = false; }
  }
  let curPid: number | undefined;
  onMount(() => {
    const ro = new ResizeObserver(() => { if (scroller) paneW = scroller.clientWidth; });
    const un = currentProjectId.subscribe((v: number | undefined) => {
      if (v !== undefined && v !== curPid) { curPid = v; void refresh(v); void loadDiagram(); }
    });
    const tick = setInterval(() => { if (curPid !== undefined) void refresh(curPid); }, 30_000);
    queueMicrotask(() => { if (scroller) { paneW = scroller.clientWidth; ro.observe(scroller); } });
    return () => { un(); clearInterval(tick); ro.disconnect(); };
  });

  // -------------------------------------------------------------- diagram
  type GNode = { id: string; title: string; status_type?: string; people?: string[];
                 task?: number; x: number; y: number; w: number; h: number; lines: string[] };
  type GEdge = { from: string; to: string };
  let diagramKind = $state<"tasks" | "modules">("tasks");
  let diagramRaw = $state<{ mermaid: string; note?: string; nodes?: any[]; edges?: any[] } | null>(null);
  let diagramLoading = $state(false);
  let diagramError = $state("");
  let mermaidNote = $state("");
  let diagramEpoch = 0;
  let dScale = $state(1);
  let dPane: HTMLDivElement | undefined = $state();

  const BOX_W = 200, LINE_H = 16, COL_GAP = 72, ROW_GAP = 18, CHARS = 26;
  function wrap(text: string, n = CHARS, max = 3): string[] {
    const out: string[] = [];
    let cur = "";
    for (const w of text.split(/\s+/)) {
      if (!w) continue;
      if ((cur + " " + w).trim().length <= n) cur = (cur + " " + w).trim();
      else { if (cur) out.push(cur); cur = w.length > n ? w.slice(0, n - 1) + "…" : w; }
      if (out.length === max) break;
    }
    if (out.length < max && cur) out.push(cur);
    if (out.length === max && text.length > out.join(" ").length + 1)
      out[max - 1] = out[max - 1].slice(0, n - 1).replace(/\s*\S*$/, "") + "…";
    return out;
  }
  // the server's JSON when it sends it (tasks); else the Mermaid text, parsed
  function parsed(): { nodes: { id: string; title: string; status_type?: string;
                               people?: string[]; task?: number }[]; edges: GEdge[] } {
    if (!diagramRaw) return { nodes: [], edges: [] };
    if (Array.isArray(diagramRaw.nodes)) {
      return {
        nodes: diagramRaw.nodes.map((n: any) => ({ id: String(n.id), title: `#${n.id} ${n.title}`,
          status_type: n.status_type, people: n.people, task: Number(n.id) })),
        edges: (diagramRaw.edges ?? []).map((e: any) => ({ from: String(e.from), to: String(e.to) })),
      };
    }
    const labels = new Map<string, string>();
    const edges: GEdge[] = [];
    const ARROW = /\s*-+\.+->|\s*-->|\s*---\s*/;
    const strip = (id: string) => {
      const m = id.match(/^([\w.-]+)/);
      const bare = m ? m[1] : id;
      const lbl = id.match(/\[(.*)\]/);
      if (lbl) labels.set(bare, lbl[1].replace(/^"|"$/g, ""));
      else if (!labels.has(bare)) labels.set(bare, bare);
      return bare;
    };
    for (const raw of diagramRaw.mermaid.split(/\n|\\n/)) {
      const line = raw.trim();
      if (!line || line.startsWith("graph ") || line.startsWith("%%")) continue;
      const parts = line.split(ARROW);
      if (parts.length >= 2) {
        const a = strip(parts[0].trim()), b = strip(parts[parts.length - 1].trim());
        // tasks: "t5 -.-> t3" = 5 is blocked by 3 → drawn 3 → 5
        if (a && b && a !== b) edges.push(/-\.+->/.test(line) ? { from: b, to: a } : { from: a, to: b });
        continue;
      }
      strip(line);
    }
    return { nodes: [...labels].map(([id, title]) => ({ id, title,
      task: /^t\d+$/.test(id) ? Number(id.slice(1)) : undefined })), edges };
  }
  const graph = $derived.by(() => {
    const { nodes, edges } = parsed();
    const ids = new Set(nodes.map((n) => n.id));
    const E = edges.filter((e) => ids.has(e.from) && ids.has(e.to));
    const linked = new Set(E.flatMap((e) => [e.from, e.to]));
    // layer = longest path from a node with nothing before it (cycles cut)
    const ins = new Map<string, string[]>();
    for (const e of E) (ins.get(e.to) ?? ins.set(e.to, []).get(e.to)!).push(e.from);
    const layer = new Map<string, number>();
    const seen = new Set<string>();
    const L = (id: string): number => {
      if (layer.has(id)) return layer.get(id)!;
      if (seen.has(id)) return 0;
      seen.add(id);
      const v = Math.max(-1, ...(ins.get(id) ?? []).map(L)) + 1;
      layer.set(id, v);
      return v;
    };
    const cols: string[][] = [];
    for (const n of nodes) if (linked.has(n.id)) (cols[L(n.id)] ??= []).push(n.id);
    // one barycenter pass: each column ordered by where its parents sit
    const order = new Map<string, number>();
    cols.forEach((c, ci) => {
      if (ci > 0) c.sort((p, q) => bary(p) - bary(q));
      c.forEach((id, i) => order.set(id, i));
    });
    function bary(id: string): number {
      const ps = (ins.get(id) ?? []).map((p) => order.get(p) ?? 0);
      return ps.length ? ps.reduce((s, v) => s + v, 0) / ps.length : 0;
    }
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const out: GNode[] = [];
    let maxY = 0, x = 8;
    for (const c of cols) {
      if (!c) continue;
      let y = 8;
      for (const id of c) {
        const n = byId.get(id)!;
        const ls = wrap(n.title);
        const h = 26 + ls.length * LINE_H;
        out.push({ ...n, x, y, w: BOX_W, h, lines: ls });
        y += h + ROW_GAP;
      }
      maxY = Math.max(maxY, y);
      x += BOX_W + COL_GAP;
    }
    // tasks with no links: their own block below, in a grid
    const loose = nodes.filter((n) => !linked.has(n.id));
    let looseTop = out.length ? maxY + 28 : 8;
    const perRow = Math.max(1, Math.floor(((dPane?.clientWidth ?? 900) / Math.max(dScale, 0.3)) / (BOX_W + 16)));
    let rowH = 0;
    loose.forEach((n, i) => {
      const ls = wrap(n.title);
      const h = 26 + ls.length * LINE_H;
      if (i > 0 && i % perRow === 0) { looseTop += rowH + 12; rowH = 0; }
      out.push({ ...n, x: 8 + (i % perRow) * (BOX_W + 16), y: looseTop, w: BOX_W, h, lines: ls });
      rowH = Math.max(rowH, h);
    });
    const w = Math.max(...out.map((n) => n.x + n.w), 100) + 8;
    const h = Math.max(...out.map((n) => n.y + n.h), 40) + 8;
    return { nodes: out, edges: E, w, h, looseLabelY: loose.length && out.length > loose.length ? maxY + 18 : -1 };
  });
  const nodeById = $derived(new Map(graph.nodes.map((n) => [n.id, n])));
  function edgePath(e: GEdge): string {
    const a = nodeById.get(e.from), b = nodeById.get(e.to);
    if (!a || !b) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x - 3, y2 = b.y + b.h / 2;
    const mx = x1 + Math.max(16, (x2 - x1) / 2);
    return `M${x1} ${y1} H${mx} V${y2} H${x2}`;
  }
  function fitDiagram() {
    const avail = (dPane?.clientWidth ?? graph.w) - 4;
    // never past 100 %, and never so small the words can't be read (a
    // phone scrolls the rest)
    dScale = Math.max(0.7, Math.min(1, avail / graph.w));
  }

  async function loadDiagram(): Promise<void> {
    if (curPid === undefined) return;
    const owner = ++diagramEpoch;
    diagramLoading = true; diagramError = ""; mermaidNote = "";
    try {
      const g = await call<{ mermaid: string; note?: string; nodes?: any[]; edges?: any[] }>(
        `/projects/${curPid}/diagram?kind=${diagramKind}`);
      if (owner !== diagramEpoch) return;
      diagramRaw = g;
      queueMicrotask(fitDiagram);
    } catch (e) {
      if (owner === diagramEpoch) {
        diagramRaw = null;
        diagramError = e instanceof Error ? e.message : "no answer";
      }
    } finally {
      if (owner === diagramEpoch) diagramLoading = false;
    }
  }
  async function copyMermaid(): Promise<void> {
    const text = diagramRaw?.mermaid ?? "";
    if (!text) return;
    mermaidNote = "";
    try {
      if (!window.isSecureContext || !navigator.clipboard ||
          typeof navigator.clipboard.writeText !== "function")
        throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      mermaidNote = "Mermaid text copied.";
    } catch {
      mermaidNote = "Clipboard write did not complete — select the text below.";
    }
  }
  const nobody = (n: string) => n === "(nobody yet)";
</script>

<svelte:head><title>Flow · Aura Teams</title></svelte:head>

<div class="wrap" data-kit="flow">
  <header class="head">
    <h2>Flow</h2>
    <span class="muted">{rows.length} tasks · {timed.length} started</span>
    <span class="spacer"></span>
    <label class="muted small" for="flow-group">Show</label>
    <select id="flow-group" aria-label="group the timeline" bind:value={group}>
      <option value="person">by person</option>
      <option value="task">by task</option>
    </select>
    <div class="seg" role="group" aria-label="zoom">
      {#each ZOOMS as z}
        <button type="button" class:on={zoomKey === z.key} aria-pressed={zoomKey === z.key}
                onclick={() => zoom(z.key)}>{z.label}</button>
      {/each}
      <button type="button" class:on={zoomKey === "fit"} aria-pressed={zoomKey === "fit"}
              onclick={() => zoom("fit")}>Fit</button>
    </div>
    <button class="btn tiny" type="button" onclick={toNow}>Now</button>
  </header>

  {#if loading}
    <p class="muted">Loading…</p>
  {:else if error}
    <p class="err card">{error}</p>
  {:else}
    <section class="tl card" aria-label="timeline">
      <div class="names" style:height={`${AXIS + chartH}px`}>
        <div class="axis-pad" style:height={`${AXIS}px`}><span class="small muted">{dateRange}</span></div>
        {#each lines as l, i (i)}
          {#if l.kind === "lane"}
            <div class="lane" style:height={`${LANE}px`}>
              {#if !nobody(l.name)}
                <span class="av" style:background={`color-mix(in oklch, ${hueFor(l.name)} 35%, var(--raised))`}
                      aria-hidden="true">{initials(l.name)}</span>
              {/if}
              <b>{nobody(l.name) ? "Nobody yet" : `${l.name}'s Aura`}</b>
              <span class="muted small count">{l.count} task{l.count === 1 ? "" : "s"}</span>
            </div>
          {:else}
            <button type="button" class="name" style:height={`${ROW}px`}
                    title={`#${l.row.number} ${l.row.title}`} onclick={() => openTask(l.row.number)}>
              <span class="num">#{l.row.number}</span>
              <span class="ttl">{l.row.title}</span>
              {#if group === "task" && l.row.people?.length}
                <span class="muted small who">{l.row.people.join(", ")}</span>
              {/if}
            </button>
          {/if}
        {/each}
      </div>
      <div class="chart" bind:this={scroller}>
        <svg width={chartW} height={AXIS + chartH} role="group" style:min-width="100%"
             aria-label="the project's tasks in time" class="gantt">
          <defs>
            <pattern id="flow-live" width="8" height="8" patternUnits="userSpaceOnUse"
                     patternTransform="rotate(45)">
              <rect width="8" height="8" fill="var(--accent)" />
              <rect width="3" height="8" fill="color-mix(in oklab, var(--accent), white 25%)" />
            </pattern>
            <marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
                    markerHeight="7" orient="auto-start-reverse">
              <path d="M1 1L9 5L1 9" fill="none" stroke="context-stroke" stroke-width="1.6" />
            </marker>
          </defs>
          <!-- the axis: the bigger unit above, the step below -->
          {#each ticks.major as m}
            <text x={m.x + 4} y="16" class="ax-major">{m.label}</text>
            <line x1={m.x} y1="4" x2={m.x} y2={AXIS + chartH} class="grid-major" />
          {/each}
          {#each ticks.minor as m}
            <text x={m.x + 4} y="36" class="ax-minor">{m.label}</text>
            <line x1={m.x} y1="24" x2={m.x} y2={AXIS + chartH} class="grid" />
          {/each}
          <line x1="0" y1={AXIS} x2={chartW} y2={AXIS} class="grid-major" />
          <g transform={`translate(0 ${AXIS})`}>
            {#each lines as l, i (i)}
              {#if l.kind === "lane"}
                <rect x="0" y={l.y} width="100%" height={LANE} class="lane-bg" />
              {:else}
                {@const r = l.row}
                {@const by = l.y + (ROW - BAR) / 2}
                {#if r.scheduled && r.start != null}
                  {@const x1 = X(r.start)}
                  {@const x2 = Math.max(x1 + 6, X(endOf(r)))}
                  {@const live = r.status_type === "in_progress" || r.status_type === "in_review"}
                  <g class="bar" role="button" tabindex="0"
                     aria-label={`#${r.number} ${r.title}, ${STATUS_WORD[r.status_type] ?? r.status_type}`}
                     onclick={() => openTask(r.number)}
                     onkeydown={(e) => { if (e.key === "Enter") openTask(r.number); }}>
                    <title>#{r.number} {r.title} · {STATUS_WORD[r.status_type] ?? r.status_type} · {short(r.start)} → {live ? "now" : short(endOf(r))}</title>
                    <rect x={x1} y={by} width={x2 - x1} height={BAR} rx="3"
                          fill={live ? "url(#flow-live)" : colorOf(r.status_type)}
                          opacity={r.status_type === "canceled" ? 0.5 : 1} />
                  </g>
                  {#each r.events ?? [] as e}
                    {#if e.kind === "file"}
                      <circle cx={X(e.t)} cy={by + BAR / 2} r="1.8" class="ev-file"><title>{evTitle(r, e)}</title></circle>
                    {:else if e.kind === "ran"}
                      <circle cx={X(e.t)} cy={by - 4} r="3.2" class={e.ok ? "ev-ok" : "ev-bad"}><title>{evTitle(r, e)}</title></circle>
                    {:else if e.kind === "finished"}
                      <line x1={X(e.t)} y1={by - 3} x2={X(e.t)} y2={by + BAR + 3} class="ev-done"><title>{evTitle(r, e)}</title></line>
                    {:else if e.kind === "blocked"}
                      <circle cx={X(e.t)} cy={by + BAR / 2} r="3.5" class="ev-warn"><title>{evTitle(r, e)}</title></circle>
                    {/if}
                  {/each}
                  {#if r.merged_at}
                    <g transform={`translate(${X(r.merged_at)} ${by + BAR / 2})`} class="ev-merged">
                      <title>merged into main · {short(r.merged_at)}</title>
                      <path d="M0 -5 L5 0 L0 5 L-5 0 Z" />
                    </g>
                  {/if}
                {:else}
                  <text x={X(span.a) + 4} y={l.y + ROW / 2 + 4} class="unsched">not started yet</text>
                {/if}
              {/if}
            {/each}
            {#each arrows as a}
              <path d={a.d} class={a.late ? "arrow late" : "arrow"} marker-end="url(#flow-arrow)"><title>{a.title}{a.late ? " — it ends after this one starts" : ""}</title></path>
            {/each}
            {#each clashes as c}
              {@const ya = rowY.get(c.tasks[0])}
              {@const yb = rowY.get(c.tasks[1])}
              {#if ya != null && yb != null}
                {@const cx = X(c.at)}
                <g class="clash">
                  <title>clash: {c.people.join(" and ")} changed {c.path} at the same time (#{c.tasks[0]}, #{c.tasks[1]})</title>
                  <line x1={cx} y1={ya + ROW / 2} x2={cx} y2={yb + ROW / 2} />
                  <circle cx={cx} cy={ya + ROW / 2} r="6" />
                  <circle cx={cx} cy={yb + ROW / 2} r="6" />
                </g>
              {/if}
            {/each}
            <line x1={X(now)} y1={-AXIS + 22} x2={X(now)} y2={chartH} class="now" />
          </g>
          <text x={X(now) - 4} y="36" class="now-label" text-anchor="end">now</text>
        </svg>
      </div>
    </section>
    <div class="legend muted small" aria-label="legend">
      <span><i class="sw" style:background="var(--ok)"></i>done</span>
      <span><i class="sw live"></i>in progress</span>
      <span><i class="sw" style:background="var(--text-2)"></i>to do</span>
      <span><i class="dot ok"></i>tests passed</span>
      <span><i class="dot bad"></i>tests failed</span>
      <span><i class="tick"></i>finished (commit, push)</span>
      <span><i class="dia"></i>merged into main</span>
      <span><i class="ring"></i>clash: same file, same time</span>
    </div>
    {#if clashes.length}
      <ul class="clashes small" aria-label="clashes">
        {#each clashes as c}
          <li><b>{c.path}</b> — {c.people.join(" and ")} (#{c.tasks[0]}, #{c.tasks[1]}) · {short(c.at)}</li>
        {/each}
      </ul>
    {/if}
  {/if}

  <section class="card strip" aria-label="diagrams">
    <div class="diag-actions">
      <h3>How the tasks depend on each other</h3>
      <span class="spacer"></span>
      <select aria-label="diagram kind" bind:value={diagramKind} onchange={() => void loadDiagram()}>
        <option value="tasks">tasks (blocked by)</option>
        <option value="modules">modules (files)</option>
      </select>
      <div class="seg" role="group" aria-label="diagram zoom">
        <button type="button" onclick={() => (dScale = Math.max(0.3, dScale - 0.15))} aria-label="zoom out">−</button>
        <button type="button" onclick={fitDiagram}>Fit</button>
        <button type="button" onclick={() => (dScale = Math.min(1.5, dScale + 0.15))} aria-label="zoom in">+</button>
      </div>
    </div>
    {#if diagramError}
      <p class="err" role="alert">{diagramError}</p>
    {:else if diagramLoading && !diagramRaw}
      <p class="muted" role="status">Laying the graph out…</p>
    {:else if diagramRaw === null}
      <p class="muted">No graph yet.</p>
    {:else}
      {#if graph.nodes.length === 0}
        <p class="muted">{diagramRaw.note ?? "nothing to lay out yet"}</p>
      {:else}
        {#if !graph.edges.length && diagramKind === "tasks"}
          <p class="muted small">No task is marked as blocked by another yet — set "blocked by" on a task to draw its arrows.</p>
        {/if}
        <div class="dpane" bind:this={dPane}>
          <svg width={graph.w * dScale} height={graph.h * dScale}
               viewBox={`0 0 ${graph.w} ${graph.h}`} class="diagram" role="group"
               aria-label={`${diagramKind} diagram`}>
            <defs>
              <marker id="diag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7"
                      markerHeight="7" orient="auto-start-reverse">
                <path d="M1 1L9 5L1 9" fill="none" stroke="context-stroke" stroke-width="1.6" />
              </marker>
            </defs>
            {#each graph.edges as e}
              <path d={edgePath(e)} class="diag-edge" marker-end="url(#diag-arrow)" />
            {/each}
            {#if graph.looseLabelY > 0}
              <text x="8" y={graph.looseLabelY} class="diag-sub">no links</text>
            {/if}
            {#each graph.nodes as n (n.id)}
              {@const done = n.status_type === "done" || n.status_type === "canceled"}
              <g transform={`translate(${n.x} ${n.y})`} class:faded={done}
                 class:clickable={n.task != null} role="button"
                 tabindex={n.task != null ? 0 : -1} aria-disabled={n.task == null}
                 onclick={() => { if (n.task != null) openTask(n.task); }}
                 onkeydown={(ev) => { if (ev.key === "Enter" && n.task != null) openTask(n.task); }}>
                <title>{n.title}</title>
                <rect width={n.w} height={n.h} rx="6" class="diag-node"
                      style:stroke={n.status_type ? colorOf(n.status_type) : undefined} />
                {#if n.status_type}
                  <rect width="4" height={n.h} rx="2" style:fill={colorOf(n.status_type)} />
                  <text x="12" y="17" class="diag-sub">{STATUS_WORD[n.status_type] ?? n.status_type}</text>
                {/if}
                {#each n.lines as ln, li}
                  <text x="12" y={(n.status_type ? 34 : 20) + li * LINE_H} class="diag-label">{ln}</text>
                {/each}
                {#each (n.people ?? []).slice(0, 2) as p, pi}
                  <g transform={`translate(${n.w - 14 - pi * 18} 13)`}>
                    <circle r="8" style:fill={`color-mix(in oklch, ${hueFor(p)} 35%, var(--raised))`} />
                    <text text-anchor="middle" y="3" class="diag-av">{initials(p)}</text>
                    <title>{p}</title>
                  </g>
                {/each}
              </g>
            {/each}
          </svg>
        </div>
      {/if}
      <details class="diag-raw">
        <summary class="muted">the diagram's own text</summary>
        <textarea class="diag-text" readonly rows="4" aria-label="the diagram as text"
                  spellcheck="false">{diagramRaw.mermaid}</textarea>
      </details>
      <div class="chain-actions">
        <button class="btn tiny" type="button" onclick={() => void copyMermaid()}>Copy as Mermaid text</button>
        {#if mermaidNote}<span class="muted">{mermaidNote}</span>{/if}
      </div>
    {/if}
  </section>
</div>

<style>
  .wrap { padding: var(--sp-16); display: flex; flex-direction: column; gap: var(--sp-12); min-width: 0; }
  .head { display: flex; gap: var(--sp-8); align-items: center; flex-wrap: wrap; }
  .head h2 { margin: 0 var(--sp-8) 0 0; }
  .spacer { flex: 1; }
  .small { font-size: 12px; }
  .seg { display: inline-flex; border: 1px solid var(--line); border-radius: var(--radius-control); overflow: hidden; }
  .seg button { background: none; border: none; color: var(--text-2); font: inherit; font-size: 12px;
                padding: 4px 9px; cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--line); }
  .seg button.on { background: color-mix(in oklab, var(--bg), var(--text) 10%); color: var(--text); }
  .tl { display: flex; padding: 0; overflow: hidden; min-width: 0; }
  .names { flex: 0 0 var(--names-w, 232px); width: var(--names-w, 232px); border-right: 1px solid var(--line);
           background: var(--bg); position: relative; z-index: 1; }
  .lane { display: flex; align-items: center; gap: var(--sp-8); padding: 0 var(--sp-8);
          background: color-mix(in oklab, var(--bg), var(--text) 5%); font-size: 13px; }
  .lane b { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .count { white-space: nowrap; }
  .axis-pad { display: flex; align-items: flex-end; padding: 0 var(--sp-8) 8px; box-sizing: border-box;
              border-bottom: 1px solid var(--line-strong); }
  .av { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center;
        justify-content: center; font-size: 10px; color: var(--text); flex: none; }
  .name { all: unset; box-sizing: border-box; display: flex; align-items: center; gap: var(--sp-8);
          width: 100%; padding: 0 var(--sp-8) 0 var(--sp-16); cursor: pointer; font-size: 13px; }
  .name:hover, .name:focus-visible { background: color-mix(in oklab, var(--bg), var(--text) 6%); }
  .num { font-family: var(--font-mono); font-size: 12px; color: var(--muted); flex: none; }
  .ttl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); min-width: 0; }
  .who { flex: none; margin-left: auto; }
  .chart { flex: 1; overflow-x: auto; overflow-y: hidden; min-width: 0; }
  .gantt { display: block; }
  .gantt :global(.ax-major) { font-size: 12px; font-weight: 600; fill: var(--text); }
  .gantt :global(.ax-minor) { font-size: 11px; fill: var(--muted); }
  .gantt :global(.grid) { stroke: var(--line); stroke-width: 1; opacity: .6; }
  .gantt :global(.grid-major) { stroke: var(--line-strong); stroke-width: 1; }
  .gantt :global(.lane-bg) { fill: color-mix(in oklab, var(--bg), var(--text) 5%); }
  .gantt :global(.bar) { cursor: pointer; outline: none; }
  .gantt :global(.bar:hover rect), .gantt :global(.bar:focus-visible rect) { stroke: var(--text); stroke-width: 1.5; }
  .gantt :global(.unsched) { font-size: 12px; fill: var(--muted); font-style: italic; }
  .gantt :global(.ev-file) { fill: var(--bg); opacity: .85; }
  .gantt :global(.ev-ok) { fill: var(--ok); stroke: var(--bg); stroke-width: 1; }
  .gantt :global(.ev-bad) { fill: var(--err); stroke: var(--bg); stroke-width: 1; }
  .gantt :global(.ev-done) { stroke: var(--text); stroke-width: 2; }
  .gantt :global(.ev-warn) { fill: var(--warn); }
  .gantt :global(.ev-merged path) { fill: var(--text); stroke: var(--bg); stroke-width: 1; }
  .gantt :global(.arrow) { fill: none; stroke: var(--text-2); stroke-width: 1.3; }
  .gantt :global(.arrow.late) { stroke: var(--err); }
  .gantt :global(.clash line) { stroke: var(--err); stroke-width: 1.5; stroke-dasharray: 3 2; }
  .gantt :global(.clash circle) { fill: none; stroke: var(--err); stroke-width: 2; }
  .gantt :global(.now) { stroke: var(--warn); stroke-width: 2; }
  .gantt :global(.now-label) { font-size: 11px; fill: var(--warn); font-weight: 600; }
  .legend { display: flex; flex-wrap: wrap; gap: var(--sp-12); align-items: center; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .sw { display: inline-block; width: 16px; height: 9px; border-radius: 2px; }
  .sw.live { background: repeating-linear-gradient(45deg, var(--accent) 0 5px,
             color-mix(in oklab, var(--accent), white 25%) 5px 8px); }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.ok { background: var(--ok); } .dot.bad { background: var(--err); }
  .tick { width: 2px; height: 12px; background: var(--text); display: inline-block; }
  .dia { width: 8px; height: 8px; background: var(--text); transform: rotate(45deg); display: inline-block; }
  .ring { width: 9px; height: 9px; border: 2px solid var(--err); border-radius: 50%; display: inline-block; }
  .clashes { margin: 0; padding-left: var(--sp-16); color: var(--text-2); }
  .strip { display: flex; flex-direction: column; gap: var(--sp-8); padding: var(--sp-16); }
  .diag-actions { display: flex; align-items: center; gap: var(--sp-8); flex-wrap: wrap; }
  .diag-actions h3 { margin: 0; font-size: 14px; }
  .dpane { overflow: auto; max-height: 70vh; }
  .diagram { display: block; }
  .diagram :global(.diag-node) { fill: var(--raised); stroke: var(--line-strong); stroke-width: 1.2; }
  .diagram :global(.diag-label) { font-size: 13px; fill: var(--text); }
  .diagram :global(.diag-sub) { font-size: 11px; fill: var(--muted); }
  .diagram :global(.diag-av) { font-size: 8px; fill: var(--text); }
  .diagram :global(.diag-edge) { fill: none; stroke: var(--text-2); stroke-width: 1.3; stroke-dasharray: 5 3; }
  .diagram :global(.faded) { opacity: .55; }
  .diagram :global(.clickable) { cursor: pointer; }
  .diag-raw { font-size: 12px; }
  .diag-text { width: 100%; box-sizing: border-box; resize: vertical; font-family: var(--font-mono);
               font-size: 12px; background: var(--raised); color: var(--text); border: 1px solid var(--line); }
  @media (max-width: 640px) {
    .tl { --names-w: 150px; }
    .name { padding-left: var(--sp-8); }
    .count { display: none; }
  }
</style>

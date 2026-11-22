<!-- Now.svelte — the project's work-structure first, people woven
     in. The fold matches the old page: the summary strip → who-does-what
     cards → shared-work flags. The doc's aura-first law: an Aura's OWN
     files never render as a presence lane. -->
<script lang="ts">
  import { TriangleAlert } from "@lucide/svelte";
  import { onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { currentProjectId, live, me, projects } from "../frame/shell_state";
  import { isLinkedApp } from "../../lib/admin_users";
  import { auraStates, type ActivityEvent } from "../../lib/aura_state";

  // someone whose Aura isn't connected to this team yet gets the next step
  let connectHint = $state<{ address: string; name: string } | null>(null);
  const hideKey = () => `aura.team.connect.hidden.${$me.value?.name ?? ""}`;
  function hideConnect() {
    connectHint = null;
    try { localStorage.setItem(hideKey(), "1"); } catch { /* this visit only */ }
  }
  onMount(() => {
    void (async () => {
      try {
        if (localStorage.getItem(hideKey())) return;
      } catch { /* show it */ }
      try {
        const d = await call<{ devices: { revoked_at?: number | null }[] }>("/devices");
        if ((d.devices ?? []).some((x) => !x.revoked_at)) return;
        let address = location.origin;
        try {
          const i = await call<{ address?: string }>("/hub/lan");
          if (i.address && /^(127\.|localhost|\[::1\])/.test(location.hostname)) address = i.address;
        } catch { /* the page's own address */ }
        connectHint = { address, name: $me.value?.name ?? "" };
      } catch { /* no hint */ }
    })();
  });
  import Avatar from "../../lib/parts/Avatar.svelte";
  import StatusIcon from "../../lib/parts/StatusIcon.svelte";

  type Task = {
    id: number; number: number; title: string; status: string;
    status_type?: string; assignees?: { name: string }[];
    due?: number; blocked_by?: number[]; planned_files?: string[];
  };
  type PresencePerson = { name: string; display?: string; state: "online" | "away" | "offline" };

  type OverlayPeople = { person: string; task: number | string }[];
  type Collision = { path: string; people: OverlayPeople };
  let collisions = $state<Collision[]>([]);
  let lastReport = $state<Record<string, string>>({});
  let lastTs = $state<Record<string, number>>({});
  let openName = $state<string | null>(null);
  let loading = $state(true);
  let error = $state("");
  let late = $state(false);
  $effect(() => {
    if (!loading) { late = false; return; }
    const t = setTimeout(() => (late = true), 300);
    return () => clearTimeout(t);
  });
  let tasks = $state<Task[]>([]);
  let events = $state<ActivityEvent[]>([]);
  let presence = $state<PresencePerson[]>([]);
  let byName = $derived.by(() => {
    const map: Record<string, Task[]> = {};
    for (const t of tasks) {
      for (const a of t.assignees ?? []) {
        (map[a.name] ??= []).push(t);
      }
    }
    return map;
  });
  let counts = $derived.by(() => {
    const nr = (t: Task) => t.status_type ?? t.status ?? "todo";
    const open = tasks.filter((t) => !["done", "canceled"].includes(nr(t)));
    return {
      open: open.length,
      in_progress: open.filter((t) => nr(t) === "in_progress").length,
      in_review: open.filter((t) => nr(t) === "in_review").length,
      blocked: open.filter((t) => (t.blocked_by ?? []).length).length,
    };
  });

  async function refresh(pid: number) {
    loading = true; error = "";
    try {
      const [t, p, m] = await Promise.all([
        call<{ tasks: Task[] }>(`/tasks?project=${pid}`),
        call<{ people: PresencePerson[] }>("/presence"),
        call<{ collisions?: Collision[] }>(`/projects/${pid}/map`)
          .catch(() => ({ collisions: [] })),       // the map may be empty — the card hides then
      ]);
      tasks = t.tasks; presence = p.people.filter((x) => !isLinkedApp(x));
      collisions = m.collisions ?? [];
      try {
        const a = await call<{ events?: { who: string | null; sentence: string; ts?: number }[] }>(
          `/projects/${pid}/activity`);
        const map: Record<string, string> = {};
        const at: Record<string, number> = {};
        for (const it of a.events ?? []) {
          if (it.who && !(it.who in map)) {                          // newest first
            map[it.who] = it.sentence;
            if (it.ts) at[it.who] = it.ts;
          }
        }
        lastReport = map; lastTs = at;
        events = (a.events ?? []) as ActivityEvent[];
      } catch { lastReport = {}; }
    } catch (e) {
      error = e instanceof Error ? e.message : "the hub didn't answer";
    } finally { loading = false; }
  }

  // the subscribe owns re-runs: project chosen at boot, switched via the
  // picker — never the snapshot-and-die read
  let lastPid: number | undefined;
  onMount(() => {
    // Project picked or switched → the page's own answers land here
    const sub = currentProjectId.subscribe((v: number | undefined) => {
      if (v !== undefined && v !== lastPid) {
        lastPid = v;
        void refresh(v);
      }
    });
    return sub;
  });

  // Law: a task or map answer rides the page's own truth over the wire
  onMount(() => live.subscribe((ev) => {
    if (!ev) return;
    const dp = (ev.data as { project?: number } | null | undefined)?.project;
    if (dp !== undefined && lastPid !== undefined && dp !== lastPid) return;
    if (["task", "map", "comment", "log"].includes(ev.kind) &&
        lastPid !== undefined) {
      void refresh(lastPid);
    }
  }));

  // due dates in words: "due today", "due in 2
  // days", "overdue 1 day"
  function auraBoxes(): { line: string }[] {
    const out: { line: string }[] = [];
    // "Your Aura" is the signed-in person's own; teammates' Auras are in the
    // list beside it
    const mine = $me.value?.name;
    if (!mine) return out;
    const states = auraStates(events);
    for (const [n, s] of states) {
      if (s.kind === "done" || s.who !== mine) continue;
      const title = tasks.find((t) => t.number === n)?.title ?? "";
      out.push({ line: `#${n}${title ? " " + title : ""} — ${s.kind}` });
    }
    return out;
  }
  function dueToday(): { n: number; title: string; hot: boolean; due: number }[] {
    const nowTs = Date.now() / 1000;
    return (tasks.filter((t) => t.due) as (Task & { due: number })[])
      .sort((a, b) => a.due - b.due)
      .map((t) => ({ n: t.number, title: t.title, due: t.due,
        hot: Math.round((t.due - nowTs) / 86400) <= 0 }));
  }

  function relDay(ts: number): string {
    const days = Math.round((ts - Date.now() / 1000) / 86400);
    if (days < 0) return `overdue ${-days} day${-days === 1 ? "" : "s"}`;
    if (days === 0) return "due today";
    if (days === 1) return "due tomorrow";
    return `due in ${days} days`;
  }

  // "2m", "1h", "Yesterday", or the date.
  function ago(ts: number | undefined): string {
    if (!ts) return "";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "now";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    if (s < 2 * 86400) return "Yesterday";
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  const openOf = (name: string) => (byName[name] ?? [])
    .filter((t) => !["done", "canceled"].includes(t.status_type ?? t.status ?? "todo"));

  const presenceOf = $derived.by(() => {
    const map = new Map<string, PresencePerson["state"]>();
    for (const p of presence) map.set(p.name, p.state);
    return map;
  });
  const displayOf = $derived.by(() => {
    const map = new Map<string, string>();
    for (const p of presence) if (p.display) map.set(p.name, p.display);
    return map;
  });
</script>

<svelte:head><title>Now · Aura Teams</title></svelte:head>

{#if $projects && $projects.length === 0}
  <!-- a new teammate before anyone adds them to a project -->
  <div class="wrap noproj">
    <div class="box">
      <h3>You're not in a project yet</h3>
      <p class="mini">Ask your team admin to add you to one. Chat works already — see the sidebar.</p>
    </div>
    {#if connectHint}
      <div class="box connect" data-kit="now.connect">
        <h3>Connect your Aura</h3>
        <ol class="mini steps">
          <li>Open Aura and say: <i>connect me to my team at {connectHint.address}</i></li>
          <li>It asks your name (<code>{connectHint.name}</code>) and your password.</li>
          <li>Then name a board task in your request, e.g. <i>#3 fix the login</i>.</li>
        </ol>
        <button class="btn tiny" type="button" onclick={hideConnect}>Got it</button>
      </div>
    {/if}
  </div>
{:else if loading}
  <div class="wrap">{#if late}<span class="muted">the pages load…</span>{/if}</div>
{:else if error}
  <div class="wrap"><div class="err card" role="alert">
    <p>{error}</p>
    <button class="btn" onclick={() => lastPid !== undefined && void refresh(lastPid)}>Try again</button>
  </div></div>
{:else if !tasks.length}
  <div class="wrap card">You're all caught up.</div>
{:else}
  <div class="page">
    <header class="rowgap">
      <h2>Now</h2>
      <div class="chips">
        <span class="chip"><b>{counts.open}</b> open</span>
        <span class="chip"><b>{counts.in_progress}</b> in progress</span>
        <span class="chip"><b>{counts.in_review}</b> in review</span>
        {#if counts.blocked}
          <span class="chip warn"><b>{counts.blocked}</b> blocked</span>
        {/if}
      </div>
    </header>
    {#if collisions.length}
      <div class="card collisions" role="alert">
        <strong>The same files are moving twice:</strong>
        {#each collisions as c}
          <div class="rowwarn"><TriangleAlert size={13} strokeWidth={1.75}/> <code>{c.path}</code> — {c.people.map((p2) => p2.person).join(" and ")} on {c.people.map((p2) => "#" + p2.task).join(", ")}</div>
        {/each}
        <a class="accent" href="#/tree">See the tree ›</a>
      </div>
    {/if}

    <div class="now">
      <div class="inbox">
        {#each [...new Set(Object.keys(byName).concat(presence.map((p) => p.name)))] as name, pi (name)}
          {@const open = openOf(name)}
          {@const doing = open.find((t) => (t.status_type ?? t.status) === "in_progress")}
          <div class="item" data-person={name} data-kit={pi === 0 ? "now.item" : undefined}
               class:read={!lastReport[name]}
               role="button" tabindex="0" aria-expanded={openName === name}
               onclick={() => (openName = openName === name ? null : name)}
               onkeydown={(e: KeyboardEvent) => {
                 if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openName = openName === name ? null : name; }
               }}>
            <Avatar {name} display={displayOf.get(name)} size="md" presence={presenceOf.get(name) ?? "offline"} />
            <div class="tx">
              <b>{displayOf.get(name) ?? name}</b>
              {#if doing}is working on <b>#{doing.number} {doing.title}</b> ·{/if}
              {open.length} open task{open.length === 1 ? "" : "s"}
              <div class="q">{lastReport[name] ?? ((presenceOf.get(name) ?? "offline") === "offline" ? "Not here right now" : "Here, no report yet")}</div>
              {#if openName === name}
                <div class="tasks">
                  {#each open as t (t.id)}
                    <a class="mini" href={`#/board/${t.number}`} onclick={(e: Event) => e.stopPropagation()}>
                      <StatusIcon type={t.status_type ?? t.status} />
                      <span class="num">#{t.number}</span>
                      <span class="ttl">{t.title}</span>
                      {#if (t.blocked_by ?? []).length}
                        <span class="warnline"><TriangleAlert size={12} strokeWidth={1.75}/> blocked by {(t.blocked_by ?? []).map((n) => "#" + n).join(", ")}</span>
                      {/if}
                      {#if t.due}<span class={Math.round((t.due - Date.now() / 1000) / 86400) < 0 ? "err" : "muted"}>· {relDay(t.due)}</span>{/if}
                    </a>
                  {:else}
                    <div class="mini muted">Nothing open</div>
                  {/each}
                </div>
              {/if}
            </div>
            <span class="tm">{ago(lastTs[name])}</span>
          </div>
        {/each}
      </div>
      <aside class="rail2" data-kit="now.rail">
        {#if connectHint}
          <div class="box connect" data-kit="now.connect">
            <h3>Connect your Aura</h3>
            <p class="mini">Your Aura isn't connected to this team yet. On your computer:</p>
            <ol class="mini steps">
              <li>Open Aura and say: <i>connect me to my team at {connectHint.address}</i></li>
              <li>It asks your name (<code>{connectHint.name}</code>) and your password.</li>
              <li>Then name a board task in your request, e.g. <i>#3 fix the login</i>.</li>
            </ol>
            <button class="btn tiny" type="button" onclick={hideConnect}>Got it</button>
          </div>
        {/if}
        <div class="box" data-kit="now.box">
          <h3><span class="pulse" aria-hidden="true"></span> Your Aura</h3>
          {#each auraBoxes() as ab, bi (bi + ab.line)}
            <div class="mini">{ab.line}</div>
          {:else}
            <div class="mini muted">Not working on a task</div>
          {/each}
        </div>
        <div class="box" data-kit="now.due">
          <h3>Due today</h3>
          {#each dueToday() as d, di (di + d.title)}
            <div class="mini {!d.hot ? 'muted' : ''}">{d.hot ? '! ' : ''}#{d.n} {d.title} <span class="when">· {relDay(d.due)}</span></div>
          {:else}
            <div class="mini muted">Nothing dated today</div>
          {/each}
        </div>
      </aside>
    </div>
  </div>
{/if}

<style>
  .connect { border: 1px solid color-mix(in oklch, var(--acc) 40%, transparent); }
  .noproj { display: flex; flex-direction: column; gap: 12px; max-width: 520px; padding: 24px; }
  .connect .steps { margin: 4px 0 8px; padding-left: 18px; display: flex; flex-direction: column; gap: 4px;
                    text-align: left; }
  .connect, .noproj .box { text-align: left; }
  .connect code { font-size: 11.5px; }
  .wrap { padding: var(--sp-16); display: flex; flex-direction: column; gap: var(--sp-16); }
  .page { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  .rowgap { display: flex; align-items: baseline; gap: var(--sp-16); padding: var(--sp-16) 20px var(--sp-12); }
  .rowgap h2 { margin: 0; }
  .chips { margin-left: auto; display: flex; gap: var(--sp-8); flex-wrap: wrap; }
  .chip {
    font-size: 12.5px; padding: 2px var(--sp-8); border: 1px solid var(--line);
    border-radius: var(--radius-control); color: var(--text-2);
  }
  .chip b { color: var(--text); font-variant-numeric: tabular-nums; }
  .chip.warn { border-color: var(--warn); color: var(--warn); }
  .tasks { margin-top: 6px; }
  .when { color: var(--muted); white-space: nowrap; }
  .tasks .mini { color: var(--text-2); text-decoration: none; }
  .tasks .mini:hover .ttl { color: var(--text); }
  .num { font-family: var(--font-mono); font-size: 12px; color: var(--muted); }
  .warnline { color: var(--warn); font-size: 12.5px; display: inline-flex; align-items: center; gap: 4px; }
  .collisions { border-color: var(--err); background: var(--err-bg); color: var(--text);
                display: flex; flex-direction: column; gap: var(--sp-8); font-size: 13px;
                margin: 0 20px var(--sp-12); }
  .collisions code { color: var(--err); }
  .rowwarn { line-height: 1.45; }
  @media (max-width: 900px) { .now { grid-template-columns: minmax(0, 1fr); } }
</style>

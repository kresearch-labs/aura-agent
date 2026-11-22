<!-- Palette.svelte — quick search. Ctrl+K/⌘K (or the sidebar's search button) opens.pal; the
     input (palette.input) drives grouped rows — Actions, Tasks (#N or words), Rooms, People
     (opens a DM). ↑↓ move, Enter opens, Esc closes and returns focus. No "Ask Aura" row (RULES
     9). The FB's g-prefixes stay.
-->
<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { get } from "svelte/store";
  import { untrack } from "svelte";
  import { me, perms, currentProjectId, route, projects } from "./shell_state";
  import { call } from "../../lib/api/client";
  import { canOpenAdministration } from "../../lib/admin_users";

  let open = $state(false);
  let query = $state("");
  let index = $state(0);
  type Row = { label: string; kind: string; act: () => void };
  let groups = $state<{ lab: string; rows: Row[] }[]>([]);
  let flat = $state<Row[]>([]);
  let tasksPool = $state<{ number: number; title: string }[]>([]);
  let roomsPool = $state<{ id: number; name: string }[]>([]);
  let peoplePool = $state<string[]>([]);

  const KIND = [
    ["n", "now"], ["b", "board"], ["t", "tree"], ["f", "flow"],
    ["h", "history"], ["c", "chat"],
  ] as const;

  function filter(q: string) {
    // the pools ride in via loadPools's explicit re-filter — never tracked,
    // or the pools' own assignment re-runs this effect (depth-exceeded loop)
    untrack(() => filterInner(q));
  }
  let __calls = 0;
  function filterInner(q: string) {
    console.log("FLT", (++__calls), JSON.stringify(q).slice(0,40));
    const low = q.trim().toLowerCase();
    const mev = get(me).value;
    const pid = get(currentProjectId);
    const chosen = pid === undefined ? undefined
      : get(projects)?.find((row) => row.id === pid);
    function tickOf(k: string): boolean {
      if (pid !== undefined && chosen?.modules?.[k] === false) return false;
      const tick = { now: "view_activity", board: "view_work_packages",
        tree: "browse_repository", flow: "view_work_packages",
        history: "view_activity", chat: "view_messages" }[k] ?? "";
      if (pid !== undefined && k !== "chat" && !perms.areaOpen(k, pid)) return false;
      return perms.allowed(tick, undefined, pid);
    }
    const G: { lab: string; rows: Row[] }[] = [];

    // Actions — always first
    const acts: Row[] = [];
    if (pid !== undefined && perms.allowed("edit_project", undefined, pid))
      acts.push({ label: "New task", kind: "C", act: () => route.go("board", "new") });
    for (const mod of ["board", "chat", "now"] as const)
      if (tickOf(mod))
        acts.push({ label: `Go to ${mod[0].toUpperCase()}${mod.slice(1)}`,
                    kind: "G", act: () => route.go(mod) });
    acts.push({ label: "Change look…", kind: "A", act: () => route.go("account") });
    const words = low.split(/\s+/).filter(Boolean);
    G.push({ lab: "Actions", rows: acts.filter((r) =>
      !words.length || words.every((wd) => r.label.toLowerCase().includes(wd))
      || r.label.toLowerCase().startsWith(low)) });

    // Tasks / Rooms / People — only while a query exists
    if (low) {
      const tks = tasksPool
        .filter((t) => `#${t.number}`.includes(low) || t.title.toLowerCase().includes(low))
        .slice(0, 6);
      if (tks.length) G.push({ lab: "Tasks", rows: tks.map((t) => ({
        label: `#${t.number} ${t.title}`, kind: "T",
        act: () => route.go("board", String(t.number)) })) });

      const rms = roomsPool
        .filter((r) => r.name.toLowerCase().includes(low)).slice(0, 4);
      if (rms.length) G.push({ lab: "Rooms", rows: rms.map((r) => ({
        label: r.name, kind: "R", act: () => route.go("chat") })) });

      const ppls = peoplePool
        .filter((nam) => nam.toLowerCase().includes(low)).slice(0, 4);
      if (ppls.length) G.push({ lab: "People", rows: ppls.map((nam) => ({
        label: nam, kind: "DM", act: () => route.go("chat") })) });
    }

    // Modules (the legacy lane — the person may always jump)
    const mods: Row[] = [];
    for (const [key, label] of KIND) {
      if (!tickOf(label)) continue;
      if (!low || label.startsWith(low) || q.toLowerCase() === key)
        mods.push({ label: label[0].toUpperCase() + label.slice(1), kind: key.toUpperCase(),
                    act: () => route.go(label) });
    }
    if (canOpenAdministration(mev) && (!low || "admin".startsWith(low)))
      mods.push({ label: "Admin", kind: "⚙", act: () => route.go("admin") });
    if (mods.length) G.push({ lab: "Modules", rows: mods });

    if (low && low.startsWith("#")) {
      G.push({ lab: "Jump", rows: [{
        label: `open ${low}`, kind: "#",
        act: () => { location.hash = `#/board/${low.slice(1)}`; } }] });
    }

    groups = G.filter((g) => g.rows.length);
    flat = groups.flatMap((g) => g.rows);
    index = 0;
  }

  function close() { open = false; query = ""; }
  function toggle() { open = !open; if (open) { query = ""; void loadPools(); } }

  function keyHandler(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement | null)?.tagName;
    const inField = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if ((e.key === "k" && (e.metaKey || e.ctrlKey)) ||
        (e.key === "/" && !inField)) {
      toggle(); e.preventDefault(); return;
    }
    if (!open) {
      if (!inField && e.key.length === 1) {
        // 'g <x>' meant first-then — FB's go-pair: only the first letter lands
        if (e.key === "g") {
          const once = (e2: KeyboardEvent) => {
            const key = e2.key.toLowerCase();
            const row = KIND.find(([k]) => k === key);
            if (row) { route.go(row[1]); e2.preventDefault(); }
            window.removeEventListener("keydown", once, true);
          };
          window.addEventListener("keydown", once, { capture: true });
          setTimeout(() => window.removeEventListener("keydown", once, true), 1400);
        } else if (e.key === "?") {
          query = ""; open = true;
        }
      }
      return;
    }
    if (e.key === "Escape") { close(); e.preventDefault(); return; }
    if (e.key === "ArrowDown") { index = Math.min(index + 1, flat.length - 1); }
    if (e.key === "ArrowUp") { index = Math.max(index - 1, 0); }
    if (e.key === "Enter") {
      e.preventDefault();
      if (flat[index]) { flat[index].act(); close(); }
    }
    e.stopPropagation();
  }

  async function loadPools() {
    const pidv = get(currentProjectId);
    try {
      if (pidv !== undefined) {
        const d = await call<{ tasks: { number: number; title: string }[] }>(`/tasks?project=${pidv}`);
        tasksPool = d.tasks ?? [];
      }
      const r = await call<{ rooms: { id: number; name: string }[] }>("/chat/rooms");
      roomsPool = (r.rooms ?? []).map((x) => ({ id: x.id, name: x.name }));
      const pp = await call<{ people: { name: string }[] }>("/presence");
      peoplePool = (pp.people ?? []).map((x) => x.name);
    } catch { /* pools stay empty — the groups hide */ }
    filter(query);                         // re-run: the pools just landed
  }

  onMount(() => {
    window.addEventListener("keydown", keyHandler);
    window.addEventListener("aura-palette", toggle);
    void loadPools();
    return () => window.removeEventListener("keydown", keyHandler);
  });
  onDestroy(() => window.removeEventListener("aura-palette", toggle));

  $effect(() => { if (open) filter(query); });
  // the page changed under the palette (a link, the back button): it closes
  let lastRoute = "";
  $effect(() => {
    const now = `${$route.module}/${$route.sub ?? ""}`;
    if (lastRoute && now !== lastRoute && open) close();
    lastRoute = now;
  });
  // drawn on the page's top layer: inside the sheet (which has its own
  // stacking box) the shade never reached the sidebar
  function toBody(node: HTMLElement) {
    document.body.appendChild(node);
    return { destroy() { node.remove(); } };
  }

  $effect(() => { if (open) setTimeout(() => document.getElementById("plq")?.focus(), 20); });
</script>

{#if open}
  <div class="shade" role="presentation" onclick={close} use:toBody></div>
  <div class="pal" data-kit="palette.box" role="dialog" aria-label="the palette" use:toBody>
    <input id="plq" data-kit="palette.input" aria-label="what to do" bind:value={query}
           placeholder="Search tasks, rooms and people…" />
    {#each groups as g (g.lab)}
      <div class="lab">{g.lab}</div>
      {#each g.rows as r (g.lab + r.label)}
        <button class="mi" data-kit={flat[index] === r ? "palette.item" : ""}
                role="option" aria-selected={flat[index] === r} aria-label={r.label}
                onclick={() => { r.act(); close(); }}>
          <span class="lbl">{r.label}</span><span class="k">{r.kind}</span>
        </button>
      {/each}
    {/each}
    {#if flat.length === 0}<div class="mi muted">Nothing matches “{query}”.</div>{/if}
  </div>
{/if}

<style>
  .shade { position: fixed; inset: 0; background: rgb(0 0 0 / 0.45); z-index: 40; }
  .pal { position: fixed; top: 18vh; left: 50%; transform: translateX(-50%);
         width: 620px; max-width: 90vw; z-index: 50;
         padding: var(--sp-8); }
  .pal input { width: 100%; height: 52px; border: none; background: transparent;
               color: var(--text); border-radius: 0;
               padding: 0 18px; font-size: 15px; box-sizing: border-box;
               border-bottom: 1px solid var(--line); }
  .lab { font-size: 11px; color: var(--muted); padding: var(--sp-8) var(--sp-8) 2px; }
  .pal .mi { all: unset; display: flex; gap: var(--sp-8); align-items: center;
             height: 36px; padding: 0 10px; cursor: pointer; width: 100%;
             box-sizing: border-box; font: inherit; font-size: 13px; color: var(--text-2); }
  .pal .mi:hover, .pal .mi[data-kit="palette.item"] {
    background: color-mix(in oklab, var(--bg), var(--text-2) 8%); color: var(--text); }
  .mi .lbl { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mi .k { color: var(--muted); font-size: 11px; font-family: var(--font-mono); }
  .mi.muted { cursor: default; }
</style>

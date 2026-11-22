<!-- Workspace row (team name from info), the search button (Ctrl K, opens the palette), nav
     rows with unread counts, Projects section (colour dots via hueFor), the me-menu kept
     verbatim (summary toggle + Theme button — tests use them), and the Your Aura card. The
     aside keeps class `rail` and each module row `rail-item` with its visible label (helpers +
     p8's rail grammar). Phone (<1024px): the sidebar is a drawer opened by the sheet's menu
     button; <480px everything is one column.
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  import { canOpenAdministration } from "../../lib/admin_users";
  import { loadAvatars } from "../../lib/avatars";
  import { get as _get } from "svelte/store";
  import Palette from "./Palette.svelte";
import { Menu, X, Command, Clock, SquareKanban, ListTree, ChartGantt, History, MessageCircle, UsersRound, Search, PanelLeftClose, PanelLeftOpen } from "@lucide/svelte";
  import Avatar from "../../lib/parts/Avatar.svelte";
  import type { Component } from "svelte";
  import { me, perms, projects, currentProjectId, route, theme,
           live, setCurrentProject,
           signOutAndGoto, type Module as Mod } from "./shell_state";
  import { call } from "../../lib/api/client";
  import { liveLink, chatRoomsPrefetch } from "./shell_state";
  import { hueFor } from "../../lib/chat_logic";
  import { auraStates, elapsed, type AuraState } from "../../lib/aura_state";

  let { children }: { children: Snippet } = $props();

  // the state's OWN reactive surface for the perms map: plain $state
  // (Svelte's rune-era reliable); the store's subscribe refreshes it
  let permTicks = $state(new Map<number, Set<string>>());
  perms.state.subscribe((s) => { permTicks = s.map; });

  // "only what the person may see" — the module needs its view tick glyph = a Lucide component
  // (@lucide/svelte
  const MODULES: { key: string; label: string; glyph: Component<{size?: number; strokeWidth?: number}> }[] = [
    { key: "now",     label: "Now",     glyph: Clock },
    { key: "board",   label: "Board",   glyph: SquareKanban },
    { key: "tree",    label: "Tree",    glyph: ListTree },
    { key: "flow",    label: "Flow",    glyph: ChartGantt },
    { key: "history", label: "History", glyph: History },
    { key: "chat",    label: "Chat",    glyph: MessageCircle },
    { key: "admin",   label: "Admin",   glyph: UsersRound },
  ];

  const SEE: Record<string, string> = {
    now:     "view_activity",
    board:   "view_work_packages",
    tree:    "browse_repository",
    flow:    "view_work_packages",
    history: "view_activity",
    chat:    "view_messages",
    admin:   "",             // admin screens hide the tick check —
                             // the gate is own the route's own words
  };

  // reactive: the mirror above carries the perms store's updates


  function permitted(m: { key: string }): boolean {
    void permTicks;                    // repaint when the perms load
    const mev = $me.value;
    if (m.key === "admin") return canOpenAdministration(mev);
    const pid = _get(currentProjectId);
    if (pid !== undefined) {
      const p = _get(projects)?.find((row) => row.id === pid);
      if (p?.modules?.[m.key] === false) return false;
      // their own switches in this project (and their role) per area
      if (m.key !== "chat" && !perms.areaOpen(m.key, pid)) return false;
    }
    if (m.key === "chat") return true;               // DM/none: people always
    const tick = SEE[m.key];
    if (!tick) return true;
    // CURRENT PROJECT — the lane's own truth, never the person's id; the
    // map lookup squints against permTicks (a plain $state mirror — Svelte
    // tracks its re-assignment, and the rail repaints on each load)
    if (pid === undefined) {
      for (const ticks of permTicks.values()) if (ticks.has(tick)) return true;
      return false;
    }
    return permTicks.get(pid)?.has(tick) ?? false;
  }

  function navigate(key: string) {
    route.go(key);
    drawer = false;                    // phone: a pick closes the drawer
  }
  let accountMenu: HTMLDetailsElement;
  function navigateFromAccountMenu(key: Mod) {
    route.go(key);
    accountMenu.open = false;
    drawer = false;
  }
  function pickProject(pid: number) {
    setCurrentProject(pid || undefined);       // perms load with the pick
    drawer = false;
  }

  // ---- Sidebar data ----
  let teamName = $state("");
  let chatUnread = $state(0);
  let drawer = $state(false);
  // desktop: the sidebar folds to its icons (remembered in this browser)
  let collapsed = $state((() => {
    try { return localStorage.getItem("aura.team.rail") === "collapsed"; } catch { return false; }
  })());
  function toggleRail() {
    collapsed = !collapsed;
    try { localStorage.setItem("aura.team.rail", collapsed ? "collapsed" : "open"); } catch { /* per-browser only */ }
  }
  let beat = $state(0);                        // 1 Hz heart-beat for elapsed
  let myAura = $state<{ n: number; title: string; kind: "working" | "waiting"; since: number } | null>(null);

  function pickMyAura(pid: number | undefined) {
    if (pid === undefined) { myAura = null; return; }
    void call<{ events: { seq: number; type: string; who: string | null; ts: number;
                          task_ref?: { number: number; title: string } | null }[] }>(
      `/projects/${pid}/activity`)
      .then((a) => {
        const states: Map<number, AuraState> = auraStates(a.events as any);
        const mine = _get(me).value;
        let found: { n: number; title: string; kind: "working" | "waiting"; since: number } | null = null;
        for (const [n, s] of states) {         // newest task wins (map order)
          if (s.kind === "done") continue;
          if (mine && s.who !== mine.name && s.who !== mine.display) continue;
          const ev = a.events.find((e) => e.task_ref?.number === n);
          found = { n, title: ev?.task_ref?.title ?? "", kind: s.kind, since: s.since };
        }
        myAura = found;
      })
      .catch(() => { myAura = null; });
  }

  let lastPid: number | undefined;
  currentProjectId.subscribe((pid) => {
    if (pid !== lastPid) { lastPid = pid; pickMyAura(pid); }
  });

  function refreshChatUnread() {
    void call<{ rooms: any[] }>("/chat/rooms")
      .then((d) => {
        chatUnread = (d.rooms ?? []).reduce((a, r) => a + (r.unread ?? 0), 0);
        chatRoomsPrefetch.set(d.rooms ?? []);   // Chat hydrates from this
        // Warm every room's history cache at boot (background).
        (async () => {
          for (const room of (d.rooms ?? []).slice(0, 6)) {
            const key = `aura.hist.${room.id}`;
            if (localStorage.getItem(key)) continue;
            try {
              const hr = await call<{ msgs: any[] }>(`/chat/history?room=${room.id}&limit=80`);
              localStorage.setItem(key, JSON.stringify(hr.msgs ?? []).slice(0, 200_000));
            } catch { /* one room's failure never blocks boot */ }
          }
        })();
      })
      .catch(() => {});
  }

  void call<{ team?: string }>("/info")
    .then((i) => { teamName = i.team ?? ""; })
    .catch(() => {});

  $effect(() => {
    refreshChatUnread();
    const iv = setInterval(() => { beat++; }, 1000);
    return () => clearInterval(iv);
  });

  // live: a chat/log landing refreshes the sidebar's own counters
  $effect(() => {
    void loadAvatars();                       // everyone's picture choice, once
    const unsub = live.subscribe((ev) => {
      if (!ev) return;
      // someone changed their picture: every avatar on the page follows
      if (ev.kind === "people" && (ev.data as { avatar?: boolean } | null)?.avatar) void loadAvatars(true);
      if (["chat", "comment", "log"].includes(ev.kind)) {
        refreshChatUnread();
        pickMyAura(_get(currentProjectId));
      }
    });
    return () => unsub();
  });
</script>

<!-- the phone drawer closes like any overlay: Escape, or a tap outside it -->
<svelte:window onkeydown={(e: KeyboardEvent) => {
  if (e.key === "Escape" && drawer) drawer = false;
  // Ctrl+\ (⌘\ on a Mac) folds or opens the sidebar
  if (e.key === "\\" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); toggleRail(); }
}} />

<div class="app" data-kit="frame.app" class:collapsed>
  {#if drawer}<div class="scrim" aria-hidden="true" onclick={() => (drawer = false)}></div>{/if}
  <aside class="side rail" class:open={drawer} aria-label="modules">
    <div class="ws" data-kit="frame.ws">
      <img class="ws-logo" src="./aura-logo-64.png" alt="" aria-hidden="true" />
      <span class="ws-names">
        <span class="ws-name">Aura Teams</span>
        {#if teamName}<span class="ws-team">{teamName}</span>{/if}
      </span>
      <button class="rail-toggle" type="button" onclick={toggleRail}
              aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
              title={`${collapsed ? "Expand" : "Collapse"} the sidebar (Ctrl \\)`}>
        {#if collapsed}<PanelLeftOpen size={16} strokeWidth={1.75} />{:else}<PanelLeftClose size={16} strokeWidth={1.75} />{/if}
      </button>
    </div>

    <button class="side-search" data-kit="frame.search" title="Search (Ctrl K)"
            onclick={() => window.dispatchEvent(new CustomEvent("aura-palette"))}>
      <Search size={14} strokeWidth={1.75} />
      <span class="fold">Search</span>
      <span class="k fold">Ctrl K</span>
    </button>

    {#each MODULES.filter(permitted) as m (m.key)}
      <button
        class="nav rail-item"
        data-kit={$route.module === m.key ? "frame.nav-on" : "frame.nav"}
        class:on={$route.module === m.key}
        aria-current={$route.module === m.key}
        title={collapsed ? m.label : undefined}
        onclick={() => navigate(m.key)}>
        <m.glyph size={16} strokeWidth={1.75}/>
        <span class="label">{m.label}</span>
        {#if m.key === "chat" && chatUnread > 0}
          <span class="cnt">{chatUnread}</span>
        {/if}
      </button>
    {/each}

    {#if ($projects?.length ?? 0) > 0}
      <div class="sec fold">Projects</div>
      {#each $projects as p (p.id)}
        <button class="nav proj-row" data-project-id={p.id}
                class:on={p.id === $currentProjectId}
                title={collapsed ? p.name : undefined}
                onclick={() => pickProject(p.id)}>
          <span class="proj-dot" style:background={hueFor(p.slug ?? p.name)}></span>
          <span class="label">{p.name}</span>
        </button>
      {/each}
    {/if}

    <details class="me-menu" bind:this={accountMenu}>
      <summary title={collapsed ? ($me.value?.display || $me.value?.name || "") : undefined}>
        {#if $me.value?.name}<Avatar name={$me.value.name} display={$me.value.display} />{/if}
        <span class="me-name">{$me.value?.display || $me.value?.name || "me"}</span>
      </summary>
      <div class="me-menu-card card">
        <button class="ghost" type="button" onclick={() => navigateFromAccountMenu("account")}>My account</button>
        <button class="ghost" type="button" onclick={() => navigateFromAccountMenu("password")}>Change password</button>
        <button class="ghost" onclick={() =>
          theme.set($theme === "dark" ? "light" : "dark")}>
          Theme — {$theme === "dark" ? "light" : "dark"}
        </button>
        <button class="ghost" onclick={async () => await signOutAndGoto()}>
          Sign out
        </button>
      </div>
    </details>

    <div class="my-aura" data-kit="frame.your-aura">
      {#if myAura}
        <div class="row">
          <span class="pulse" aria-hidden="true"></span>
          <b>Your Aura</b>
          <span class="muted aura-el">{elapsed(myAura.since, Date.now() / 1000 + beat * 0)}</span>
        </div>
        <div class="t2">#{myAura.n} · {myAura.title}</div>
      {:else}
        <div class="t2">Your Aura isn't working on a task</div>
      {/if}
    </div>
  </aside>

  <section class="sheet" data-kit="frame.sheet">
    {#if $liveLink === "down"}
      <div class="linkbar" role="status">Reconnecting…</div>
    {:else if $liveLink === "back"}
      <div class="linkbar okay" role="status">Back online</div>
    {/if}
    <button class="burger" aria-label="menu"
            onclick={() => { drawer = !drawer; }}>
      {#if drawer}<X size={18} strokeWidth={1.75}/>{:else}<Menu size={18} strokeWidth={1.75}/>{/if}
    </button>
    <main id="main" class="main" tabindex="-1">
      {@render children()}
    </main>
    <Palette />                                   <!-- M-5: actually mounted
                                                      (the ⌘K + g-chords lane
                                                      was imported but
                                                      never in the tree) -->
  </section>
</div>

<style>
  /* Layout deltas only — every visual value comes from theme/parts.css. Scoped here: app
   * height, drawer mechanics, burger, me-menu placement, my-aura micro-layout, phone media.
   */
  .app {
    display: grid; grid-template-columns: 232px minmax(0, 1fr);
    height: 100dvh; background: var(--bg); color: var(--text);
  }
  .side { overflow-y: auto; }
  /* pages that are a plain stack (Account, Admin, Flow, Now…) scroll here;
     Board and Chat fill it exactly and scroll inside their own columns */
  .main { overflow-y: auto; overflow-x: hidden; }
  /* a page or project name in the sidebar: one line, cut off neatly (the
     theme's .label is a tag pill; these aren't) */
  .side .nav .label { display: block; height: auto; padding: 0; border-radius: 0; background: none;
                      font: inherit; color: inherit; min-width: 0; text-align: left;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rail-toggle { margin-left: auto; width: 26px; height: 26px; border-radius: 6px; flex: none;
                 display: grid; place-items: center; color: var(--muted); }
  .rail-toggle:hover { background: var(--hover); color: var(--text); }
  .me-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  /* folded (desktop): icons only; names show as tooltips */
  .app.collapsed { grid-template-columns: 60px minmax(0, 1fr); }
  .app.collapsed .side :global(.label), .app.collapsed .ws-names, .app.collapsed .fold,
  .app.collapsed .me-name, .app.collapsed .my-aura { display: none; }
  .app.collapsed .ws { flex-direction: column; gap: 6px; padding: 6px 0; }
  .app.collapsed .rail-toggle { margin-left: 0; }
  .app.collapsed .side :global(.nav), .app.collapsed .side-search,
  .app.collapsed .me-menu summary { justify-content: center; padding: 0; gap: 0; }
  .app.collapsed .side :global(.nav .cnt) { position: absolute; margin: 0; transform: translate(10px, -8px);
                                            font-size: 10.5px; color: var(--acc); font-weight: 600; }
  .app.collapsed .side :global(.nav) { position: relative; }
  .app.collapsed .me-menu-card { position: fixed; left: 64px; bottom: 12px; width: 200px; z-index: 50;
                                 background: var(--raised); box-shadow: var(--pop); }
  .ws-names { display: flex; flex-direction: column; min-width: 0; line-height: 1.2; }
  .ws-team { font-size: 11.5px; font-weight: 400; color: var(--muted);
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .scrim { display: none; }
  /* rows keep their height and the rail scrolls, however many projects */
  .side > :global(*) { flex: none; }
  .me-menu summary {
    display: flex; align-items: center; gap: 10px; height: 30px;
    padding: 0 8px; border-radius: var(--r-ctl);
    color: var(--text-2); font-size: 13.5px; cursor: pointer; list-style: none;
  }
  .me-menu summary::-webkit-details-marker { display: none; }
  .me-menu-card {
    display: flex; flex-direction: column; gap: 2px; padding: 6px;
    border-radius: var(--r-ctl); margin: 4px 0;
  }
  .me-menu-card .ghost {
    background: transparent; color: var(--text-2); border: none;
    height: 28px; padding: 0 8px; font: inherit; cursor: pointer;
    text-align: left;
  }
  .me-menu-card .ghost:hover { color: var(--text); }
  .my-aura .row { display: flex; align-items: center; gap: 8px; }
  .my-aura .aura-el { margin-left: auto; font-variant-numeric: tabular-nums; }

  .burger {
    display: none; position: absolute; top: 10px; left: 10px; z-index: 30;
    width: 34px; height: 34px; border-radius: var(--r-ctl);
    border: 1px solid var(--line-2); background: var(--raised);
    color: var(--text); place-items: center; cursor: pointer;
  }

  /* phone: the sidebar becomes a drawer */
  @media (max-width: 1023px) {
    .app { grid-template-columns: minmax(0, 1fr); }
    .burger { display: grid; }
    .side {
      position: fixed; inset: 0 auto 0 0; width: 260px; z-index: 40;
      transform: translateX(-110%);
      transition: transform var(--t-panel, 200ms) var(--ease, ease);
      box-shadow: var(--pop, 0 18px 50px rgba(0, 0, 0, .25));
      background: var(--raised);
    }
    .side.open { transform: none; }
    .app.collapsed { grid-template-columns: minmax(0, 1fr); }
    .app.collapsed .side :global(.label), .app.collapsed .ws-names, .app.collapsed .fold,
    .app.collapsed .me-name, .app.collapsed .my-aura { display: revert; }
    .rail-toggle { display: none; }
    .scrim { display: block; position: fixed; inset: 0; z-index: 39; background: rgba(0, 0, 0, .3); }
    /* the menu button has its own strip; page titles never sit under it */
    .main { padding-top: 48px; box-sizing: border-box; }
  }
  @media (max-width: 479px) {
    .sheet { display: block; }
    /* bounded by the sheet, so long pages scroll inside it (unbounded, the
       sheet clipped everything below the first screen) */
    .main { display: block; height: 100%; }
  }
  .linkbar {
    position: absolute; top: 0; left: 0; right: 0; z-index: 35;
    text-align: center; font-size: 12px; padding: 3px 0;
    background: var(--warn); color: var(--bg);
  }
  .linkbar.okay { background: var(--ok); }
</style>
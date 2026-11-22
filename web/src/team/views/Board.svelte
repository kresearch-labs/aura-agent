<!-- Board.svelte — the kanban (list toggle), the New task button only
     with the permission; the side panel edits with version (the
     "keep theirs / apply mine" conflict lines ride under it). -->
<script lang="ts">
  import { onMount } from "svelte";
  import { get } from "svelte/store";
  import { ApiError, call } from "../../lib/api/client";
  import { currentProjectId, me, perms, live, route, setCurrentProject, projects } from "../frame/shell_state";
  import { auraStates, clashFor, stripText, clashText, type ActivityEvent, type Collision } from "../../lib/aura_state";
  import { TriangleAlert, Hand, Plus, Calendar, X } from "@lucide/svelte";
  import StatusIcon from "../../lib/parts/StatusIcon.svelte";
  import PriorityIcon from "../../lib/parts/PriorityIcon.svelte";
  import Avatar from "../../lib/parts/Avatar.svelte";
  import Label from "../../lib/parts/Label.svelte";
  import NewTask from "./NewTask.svelte";
  import Chooser from "../../lib/parts/Chooser.svelte";
  import { hueFor, dueState } from "../../lib/chat_logic";
  import TaskPanel from "./TaskPanel.svelte";

  type Task = {
    id: number; number: number; title: string; status: string;
    status_type?: string; assignees?: { name: string }[]; labels?: string[];
    priority?: number; version: number; due?: number; start?: number;
    description?: string; project?: number;
    links?: { kind: string; other: number }[];
  };
  type Status = { key: string; name: string; type: string;
                  position: number; colour: string };


  let tasks = $state<Task[]>([]);
  let statuses = $state<Status[]>([]);
  // The board's columns.
  const columns = $derived([
    ...statuses.filter((s) => s.type !== "backlog" && s.type !== "canceled"),
    ...statuses.filter((s) => s.type === "backlog"),
    ...statuses.filter((s) => s.type === "canceled"),
  ]);
  let loading = $state(true);
  let error = $state("");
  let mode = $state<"board" | "list" | "timeline">("board");
  let pid = $state<number | undefined>(undefined);
  let panelTask = $state<Task | null>(null);
  let panelOpen = $state(false);
  let deepLoading = $state(false);
  let deepError = $state("");
  let deepEpoch = 0;
  let refreshEpoch = 0;
  let events = $state<ActivityEvent[]>([]);
  let collisions = $state<Collision[]>([]);
  let beat = $state(0);                          // 30 s strip-time re-render
  $effect(() => {                                 // Placeholders after 300 ms
    if (!loading) { late = false; return; }
    const t = setTimeout(() => (late = true), 300);
    return () => clearTimeout(t);
  });
  let late = $state(false);
  let panelEpoch = 0;
  let selectionEpoch = 0;
  let discardEpoch = $state(0);
  let conflictBusy = $state(false);
  let conflict = $state<{
    theirs: Task;
    mine: { id: number; version: number } & Record<string, unknown>;
  } | null>(null);
  let toast = $state("");

  const myName = $derived(($me.value?.name) ?? "");

  function mayWrite(): boolean {
    return pid !== undefined && perms.allowed("edit_work_packages", undefined, pid);
  }

  let _beat = 0;
  $effect(() => { _beat = beat; });   // read so 30 s re-renders reach the strips

  const focusNow = (el: HTMLInputElement) => { setTimeout(() => el.focus(), 30); };
  let addingIn: string | null = $state(null);
  let addTitle = $state("");
  let dropTarget: string | null = $state(null);
  let selected = $state<Set<number>>(new Set());
  let menuFor: Task | null = $state(null);
  let menuPane: string = $state("");

  function firstOpenStatus() {
    return statuses.find((s) => s.type !== "done" && s.type !== "canceled") ?? statuses[0];
  }
  // the new-task form; the column's "+" opens it with that column chosen
  let newOpen = $state(false);
  let newIn = $state("");
  function newTaskInColumn(key?: string) {
    newIn = key ?? (statuses.find((s) => s.type === "todo") ?? firstOpenStatus())?.key ?? "";
    newOpen = true;
  }
  function cardClick(e: MouseEvent, t: Task) {
    if (e.shiftKey || e.ctrlKey || e.metaKey) {
      const nx = new Set(selected);
      nx.has(t.id) ? nx.delete(t.id) : nx.add(t.id);
      selected = nx;
    } else { openTask(t); }
  }
  async function moveSideways(t: Task, dir: 1 | -1) {
    const idx = statuses.findIndex((s) => s.key === (t.status_type ?? t.status));
    const nxt = statuses[idx + dir];
    if (!nxt) return;
    try {
      await call(`/tasks/${t.id}`, { status: nxt.key, version: t.version }, "PATCH");
      moveAnnounce = `#${t.number} moved to ${nxt.name}`;
      void refresh();
    } catch { /* the toast path already speaks for conflicts */ }
  }
  function openMenu(t: Task, x: number, y: number) {
    if (!mayWrite()) return;
    menuFor = t; menuPane = ""; menuXY = { x, y };
  }
  function closeMenu() { menuFor = null; menuPane = ""; }
  async function patchBulk(fields: Record<string, unknown>) {
    const ids = menuFor ? [menuFor.id] : [...selected];   // right-click acts on its card
    for (const id of ids) {
      const t = tasks.find((x) => x.id === id);
      if (!t) continue;
      try { await call(`/tasks/${t.id}`, { ...fields, version: t.version }, "PATCH"); }
      catch { /* per-task conflicts keep their own toast path */ }
    }
    selected = new Set();
    void refresh();
  }
  async function deleteTaskWithUndo(t: Task) {
    const snapshot = t;
    try {
      await call(`/tasks/${t.id}`, undefined, "DELETE");
      toast = `Task #${t.number} deleted`;
      undoTask = snapshot;
      selected.delete(t.id); selected = new Set(selected);
      void refresh();
    } catch { /* conflict path speaks via toast */ }
  }
  async function undoDelete() {
    const snap = undoTask;
    if (!snap) return;
    try {
      const made = await call<{ task: Task }>("/tasks",
        { title: snap.title, description: snap.description, project: pid }, "POST");
      if (made?.task) {
        await call(`/tasks/${made.task.id}`,
          { status: snap.status_type ?? snap.status, priority: snap.priority,
            labels: snap.labels ?? [], assignees: (snap.assignees ?? []).map((a) => a.name),
            due: snap.due, version: made.task.version }, "PATCH");
      }
      undoTask = null; toast = "";
      void refresh();
    } catch { /* the deletion stands; the hub has no restore */ }
  }
  let menuXY = $state({ x: 0, y: 0 });
  let bulkStatusOpen = $state(false);
  let undoTask: Task | null = $state(null);
  function stripKit(cls: string): string | undefined {   // Finished strips take no id
    return cls === "c-aura" ? "board.aura-strip"
         : cls === "c-ask" ? "board.wait-strip"
         : cls === "c-warn" ? "board.clash-strip" : undefined;
  }
  function stripsFor(t: Task): { cls: string; text: string }[] {
    const st = auraStates(events).get(t.number);
    const done = (t.status_type ?? t.status) === "done";
    const out: { cls: string; text: string }[] = [];
    if (st) {
      if (st.kind === "working") out.push({ cls: "c-aura", text: stripText(st, myName, (u) => u, Date.now() / 1000) });
      else if (st.kind === "waiting") out.push({ cls: "c-ask", text: stripText(st, myName, (u) => u, Date.now() / 1000) });
      else if (st.kind === "done" && !done)
        out.push({ cls: "c-aura done-strip", text: stripText(st, myName, (u) => u, Date.now() / 1000) });
      const cf = clashFor({ id: t.id, number: t.number,
                            people: (t.assignees ?? []).map((a) => a.name) }, collisions);
      if (cf) out.push({ cls: "c-warn", text: clashText(cf, myName, (u) => u) });
    }
    return out;
  }

  $effect(() => {
    const iv = setInterval(() => { beat++; }, 30_000);
    return () => clearInterval(iv);
  });


  async function refresh(pid_?: number) {
    const p = pid_ ?? get(currentProjectId) ?? pid;
    if (p === undefined) return;
    const seq = ++refreshEpoch;
    const panelSeq = ++panelEpoch;
    const openId = panelOpen ? panelTask?.id : undefined;
    loading = true; error = "";
    try {
      const openRead = openId !== undefined
        ? readCurrentTask(openId).catch((err: unknown) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;                       // network/perms are not a deletion
        }) : Promise.resolve(null);
      const [t, s, open, act, mp] = await Promise.all([
        call<{ tasks: Task[] }>(`/tasks?project=${p}`),
        call<{ statuses: Status[] }>(`/projects/${p}/statuses`),
        openRead,
        call<{ events: ActivityEvent[] }>(`/projects/${p}/activity`)
          .catch(() => ({ events: [] as ActivityEvent[] })),   // a Reviewer (no view_activity) still gets the board; the Aura strip renders empty
        call<{ collisions?: Collision[] }>(`/projects/${p}/map`).catch(() => ({ collisions: [] as Collision[] })),
      ]);
      if (seq !== refreshEpoch) return;
      events = act.events ?? [];
      collisions = mp.collisions ?? [];
      tasks = t.tasks;
      statuses = (s.statuses ?? []).sort(
        (a, b) => a.position - b.position || a.name.localeCompare(b.name));
      pid = p;
      // A panel opened/closed during this read cannot cancel the valid board
      // update; only its captured panel mutation has become obsolete.
      if (panelSeq !== panelEpoch) return;
      // A live event is a reason to read the open task, not to throw away its
      // editor. Never replace it with a closed task or another project's row.
      const active = panelTask;
      if (open && panelOpen && active && active.id === open.id &&
          open.project === p && open.version >= active.version) {
        panelTask = open;
      }
      if (open === null && openId !== undefined &&
          panelOpen && panelTask?.id === openId) {
        closePanel();                    // replace the whole stale task state
        deepError = "The open task was deleted or is no longer available to you.";
      }
    } catch (e) {
      if (seq === refreshEpoch)
        error = e instanceof Error ? e.message : "the hub didn't answer";
    } finally {
      if (seq === refreshEpoch) loading = false;
    }
  }

  // a phone shows one column at a time: open on the first one with cards,
  // not on an empty Backlog
  function startAtWork(node: HTMLElement) {
    let done = false;
    const go = () => {
      if (done || node.clientWidth > 640) return;
      const col = [...node.querySelectorAll<HTMLElement>(".col")]
        .find((c) => c.querySelector("[data-kit='board.card'], .card"));
      if (!col) return;
      done = true;
      node.scrollLeft = col.offsetLeft - node.offsetLeft - 12;
    };
    const mo = new MutationObserver(go);
    mo.observe(node, { childList: true, subtree: true });
    queueMicrotask(go);
    return { destroy() { mo.disconnect(); } };
  }
  onMount(() => {
    const sub = currentProjectId.subscribe((v: number | undefined) => {
      if (v !== undefined && v !== pid) {
        // A deliberate project change closes the old task AND its deep link.
        // On an initial mount there is no previous project/panel to abandon.
        if (pid !== undefined) closePanel();
        void refresh(v);
      }
    });
    return sub;
  });

  // An event for MY project refreshes the board (never swallow other projects' answers into
  // mine; data-room answers re-read the server)
  onMount(() => live.subscribe((ev) => {
    if (!ev || ev.kind !== "task") return;
    const dp = (ev.data as { project?: number } | null | undefined)?.project;
    const selected = get(currentProjectId) ?? pid;
    if (dp !== undefined && selected !== undefined && dp !== selected) return;
    void refresh();
  }));

  async function readCurrentTask(tid: number): Promise<Task> {
    const answer = await call<{task: Task}>(`/tasks/${tid}`);
    return answer.task;
  }

  // ---------- drag & drop (fix cares about a 409 rollback toast) --
  function dropInto(status: Status) {
    return {
      // handlers wired at the kanban column level
      // DOM event names from a prop spread are case-sensitive.
      ondragover: (e: DragEvent) => {
        if (status.type === "backlog") return;
        e.preventDefault();
        e.dataTransfer!.dropEffect = "move";
      },
      ondrop: async (e: DragEvent) => {
        e.preventDefault();
        const raw = e.dataTransfer?.getData("text/plain");
        if (!raw) return;
        let t: Task;
        try { t = JSON.parse(raw); } catch { return; }
        if ((t.status_type ?? t.status) === status.key) return;
        const iCan = mayWrite();
        if (!iCan) { toast = "your role can't move cards"; return; }
        // optimistic first, confirm the commit; on the conflict the toast
        // names it and the newer task shows with "keep theirs / apply mine"
        const ownerEpoch = selectionEpoch;
        const ownerProject = pid;
        const ownsDrop = () => selectionEpoch === ownerEpoch && pid === ownerProject;
        try {
          await call(`/tasks/${t.id}`, { status: status.key, version: t.version },
                     "PATCH");
          moveAnnounce = `#${t.number} moved to ${status.name}`;
          void refresh();
        } catch (err) {
          const any_ = err as { code?: string; message?: string };
          if (any_.code === "conflict") {
            if (!ownsDrop()) { void refresh(); return; }
            const theirs = await readCurrentTask(t.id);
            if (!ownsDrop()) { void refresh(); return; }
            // A drag's requested change is its destination, not every field of
            // its old card (whose assignees are read-model objects, not input).
            conflictBusy = false;
            conflict = { theirs, mine: {
              id: t.id, version: t.version, status: status.key,
            } };
            void refresh();
          } else {
            if (!ownsDrop()) { void refresh(); return; }
            toast = any_.message ?? "the move didn't land — refresh and try again";
            void refresh();
          }
        }
      },
    };
  }

  async function loadLinkedTask(tid: number) {
    const epoch = ++deepEpoch;
    ++panelEpoch;
    ++selectionEpoch;
    deepLoading = true; deepError = "";
    conflict = null; conflictBusy = false;
    panelOpen = false; panelTask = null;
    try {
      const answer = await call<{task: Task}>(`/tasks/${tid}`);
      if (epoch !== deepEpoch) return;
      const current = get(route);
      if (current.module !== "board" || current.sub !== String(tid)) return;
      const project = answer.task.project;
      if (project !== undefined && project !== pid) {
        // A validated deep link adopts its own project FIRST. The picker
        // then sees this as the same project, not as an explicit manual
        // switch which should close the task being opened. Refresh its
        // board/statuses explicitly so this project change reloads them too.
        pid = project;
        void refresh(project);
        setCurrentProject(project);
        await perms.loadFor(project);
      }
      if (epoch !== deepEpoch) return;
      panelTask = answer.task;
      panelOpen = true;
    } catch (err) {
      if (epoch === deepEpoch)
        deepError = err instanceof Error ? err.message : "The task could not be opened";
    } finally {
      if (epoch === deepEpoch) deepLoading = false;
    }
  }

  // "#/board/new" (the palette's New task) opens the form
  $effect(() => {
    if ($route.sub === "new" && mayWrite()) {
      newTaskInColumn();
      route.go("board");
    }
  });
  $effect(() => {
    const raw = $route.sub;
    const tid = raw && /^\d{1,10}$/.test(raw) ? Number(raw) : undefined;
    const project = pid;
    if (tid === undefined || project === undefined || deepLoading || deepError) return;
    // Card clicks also write the canonical link. Do not refetch the already
    // fetched full list record in that case; a fresh hash load must GET it.
    if (panelOpen && panelTask?.id === tid && panelTask.project === project) return;
    void loadLinkedTask(tid);
  });

  function openTask(t: Task | null) {
    ++deepEpoch;
    ++panelEpoch;
    ++selectionEpoch;
    deepLoading = false; deepError = "";
    conflict = null; conflictBusy = false;
    panelTask = t; panelOpen = true;
    route.go("board", t ? String(t.id) : undefined);
  }
  function closePanel() {
    ++deepEpoch;
    ++panelEpoch;
    ++selectionEpoch;
    deepLoading = false; deepError = "";
    panelOpen = false; panelTask = null; conflict = null; conflictBusy = false;
    if (get(route).sub) route.go("board");
  }
  async function finishAssign(t: Task, patch_: Record<string, unknown>) {
    const expected = typeof patch_.version === "number" ? patch_.version : t.version;
    const ownerEpoch = selectionEpoch;
    const ownerProject = pid;
    const ownsSave = () => selectionEpoch === ownerEpoch && panelOpen &&
      panelTask?.id === t.id && (t.project === undefined || pid === ownerProject);
    try {
      await call(`/tasks/${t.id}`, { ...patch_, version: expected }, "PATCH");
      // The panel saves one field at a time and stays open; the refresh brings the saved task
      // (and its new version) back into it.
      await refresh();
    } catch (err) {
      const any_ = err as { code?: string; message?: string };
      if (any_.code === "conflict" && ownsSave()) {
        const theirs = await readCurrentTask(t.id);
        // A task/selection change while reading must revoke this stale
        // conflict, not install it over the task now being edited.
        if (ownsSave()) {
          conflictBusy = false;
          // Retry only fields this edit requested, not unrelated read-model
          // fields such as an unchanged status or server-shaped assignees.
          conflict = { theirs, mine: { ...patch_, id: t.id, version: expected } };
        }
      }
      void refresh();
      throw err;
    }
  }
  async function taskChanged() {
    // refresh owns the single open-task read, including 404 and panel epochs;
    // do not issue an older, unguarded duplicate read before refreshing.
    await refresh();
  }

  async function createTask(patch_: Record<string, unknown>) {
    closePanel();
    await call("/tasks", { ...patch_, project: pid }, "POST");
    void refresh();
  }

  function tlNow(): { pct: number; on: boolean } {
    const nowTs = Date.now() / 1000;
    const withDue = (tasks.filter((t) => t.due) as (Task & { due: number })[])
      .sort((a, b2) => a.due - b2.due);
    const lo = withDue.length ? withDue[0].due : nowTs;
    const hi = withDue.length ? Math.max(...withDue.map((t) => t.due), nowTs) : nowTs + 86400;
    const span = Math.max(86400, hi - lo);
    return { pct: Math.round(((nowTs - lo) / span) * 100), on: lo <= nowTs && nowTs <= hi };
  }

  function tlRows(): { n: number; pct: number; due: number }[] {
    const nowTs = Date.now() / 1000;
    const withDue = (tasks.filter((t) => t.due) as (Task & { due: number })[])
      .sort((a, b2) => a.due - b2.due);
    const lo = withDue.length ? withDue[0].due : nowTs;
    const hi = withDue.length ? Math.max(...withDue.map((t) => t.due), nowTs) : nowTs + 86400;
    const span = Math.max(86400, hi - lo);
    return withDue.map((t) => ({ n: t.number, due: t.due,
      pct: Math.round(((t.due - lo) / span) * 100) }));
  }

  function relDayBoard(tsNum: number): string {
    const days = Math.round((tsNum - Date.now() / 1000) / 86400);
    if (days < 0) return `overdue ${-days}d`;
    if (days === 0) return "due today";
    return `in ${days}d`;
  }

  async function addInto(st: Status, title: string) {
    const made = await call<{ task: Task }>("/tasks",
      { title, project: pid }, "POST");
    if (st.key !== "todo") {                // KNOWN-MISTAKES 19: create-then-place
      try {
        await call(`/tasks/${made.task.id}`,
          { status: st.key, version: made.task.version }, "PATCH");
      } catch { toast = "created — placing it here didn't land; refresh and drag it"; }
    }
    void refresh();
  }

  // presentational only: the dragged card lifts (accent ring + shadow)
  let dragId = $state<number | null>(null);
  let moveAnnounce = $state("");
</script>

<svelte:head><title>Board · Aura Teams</title></svelte:head>

<svelte:window onpointerdown={(e) => { if (menuFor && !(e.target as HTMLElement).closest?.(".cardmenu")) closeMenu(); bulkStatusOpen = false; }} />

<div class="wrap">
  <header class="ph" data-kit="frame.header">
    <h1 data-kit="frame.title">{$projects?.find((row) => row.id === $currentProjectId)?.name ?? "board"}</h1>
    <div class="seg" role="group" aria-label="view mode" data-kit="ui.segmented">
      <button class="segb" class:on={mode === "board"} aria-pressed={mode === "board"}
              data-kit={mode === "board" ? "ui.segmented-on" : "ui.segmented"}
              onclick={() => (mode = "board")}>board</button>
      <button class="segb" class:on={mode === "list"} aria-pressed={mode === "list"}
              data-kit={mode === "list" ? "ui.segmented-on" : "ui.segmented"}
              onclick={() => (mode = "list")}>list</button>
      <!-- the project's timeline is Flow's (one timeline, not two copies) -->
      <button class="segb" data-kit="ui.segmented"
              onclick={() => route.go("flow")}>timeline</button>
    </div>
    <div class="right">
      {#if mayWrite()}
              {/if}
    </div>
    {#if mayWrite()}
      <div class="right">
        <button class="btn pri" data-kit="ui.button-main" onclick={() => newTaskInColumn()}>
          <Plus size={14} strokeWidth={1.75} />New task
        </button>
      </div>
    {/if}
  </header>

  {#if toast}<div class="toast warn" role="status">{toast}
      {#if undoTask}<button class="btn" onclick={() => void undoDelete()}>Undo</button>{/if}</div>{/if}
  {#if loading && late}
    <div class="ph-sk" aria-hidden="true"><p class="muted">the board loads…</p></div>
  {:else if error}
    <div class="err card" role="alert">
      <p>{error}</p>
      <button class="btn" onclick={() => void refresh()}>Try again</button>
    </div>
  {:else if mode === "board"}
    <div class="board" use:startAtWork>
      {#if tasks.length === 0}
        <div class="boardempty card">
          <span>No tasks yet.</span>
          {#if mayWrite()}<button class="btn primary" onclick={() => newTaskInColumn()}>Add a task</button>{/if}
        </div>
      {/if}
      {#each columns as st}
        {@const inCol = tasks.filter((t) => (t.status_type ?? t.status) === st.key)}
        <section class="col" data-kit="board.column" {...dropInto(st)} aria-label={st.name}
                 style:border-top-color={st.colour || "transparent"}
                 class:over={dropTarget === st.key}>
          <div class="col-h" data-kit="board.column-head">
            <StatusIcon type={st.type} size={14} />{st.name}
            <span class="n num">{inCol.length}</span>
            {#if mayWrite()}
              <button class="add" aria-label={`Add a task to ${st.name}`}
                      onclick={() => newTaskInColumn(st.key)}>
                <Plus size={14} strokeWidth={1.75} />
              </button>
            {/if}
          </div>
          <div class="cards" class:over={dropTarget === st.key}>
            {#each inCol as t (t.id)}
              {@const due = dueState(t.due)}
              <button class="card" type="button" data-kit="board.card"
                      class:lift={dragId === t.id} class:sel={selected.has(t.id)}
                      class:done={(t.status_type ?? t.status) === "done"
                                  || (t.status_type ?? t.status) === "canceled"}
                      draggable={mayWrite()}
                      ondragstart={(e: DragEvent) => {
                        if (mayWrite()) {
                          dragId = t.id;
                          e.dataTransfer!.setData("text/plain", JSON.stringify(t));
                        }
                      }}
                      ondragend={() => { dragId = null; dropTarget = null; }}
                      onclick={(e: MouseEvent) => cardClick(e, t)}
                      onkeydown={(e: KeyboardEvent) => {
                        if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
                          e.preventDefault();
                          void moveSideways(t, e.key === "ArrowRight" ? 1 : -1);
                        }
                      }}
                      oncontextmenu={(e: MouseEvent) => { e.preventDefault(); openMenu(t, e.clientX, e.clientY); }}
                      aria-label={`#${t.number} ${t.title}`}>
                <div class="c-top">
                  <span class="c-id" data-kit="board.card-id">#{t.number}</span>
                  <span class="c-pr" style:margin-left="auto">
                    <PriorityIcon priority={t.priority} kit={(t.priority ?? 0) >= 4 ? "board.urgent" : undefined} />
                  </span>
                </div>
                <div class="c-title" data-kit="board.card-title">{t.title}</div>
                <div class="c-meta">
                  {#each t.labels ?? [] as lab, li (lab)}
                    <Label text={lab} colour={hueFor(lab)} kit={li === 0 ? "board.label" : undefined} />
                  {/each}
                  {#if due}
                    <span class="c-due" class:hot={due.hot} data-kit={due.hot ? "board.due-today" : undefined}>
                      <Calendar size={12} strokeWidth={1.75} />{due.text}
                    </span>
                  {/if}
                  <span class="sp avs">
                    {#each (t.assignees ?? []) as a, ai (a.name)}
                      <Avatar name={a.name} kit={ai === 0 ? "board.avatar" : undefined} />
                    {/each}
                  </span>
                </div>
                {#each stripsFor(t) as s, si (si + s.cls + s.text)}
                  <span class={`${s.cls} cardstrip`} data-kit={stripKit(s.cls)}>
                    {#if s.cls === "c-aura" && !s.cls.includes("done")}<span class="pulse"></span><img class="strip-mark" src="./aura-logo-64.png" alt="" />{:else if s.cls === "c-warn"}<TriangleAlert size={12} strokeWidth={1.75}/>{:else if s.cls === "c-ask"}<Hand size={12} strokeWidth={1.75}/>{/if}
                    {s.text}
                  </span>
                {/each}
              </button>
            {/each}
          </div>
        </section>
      {/each}    </div>
  {:else if mode === "timeline"}
    <div class="tl" role="img" aria-label="the timeline">
      {#each tlRows() as row (row.n)}
        <div class="tlrow">
          <span class="mono">#{row.n}</span>
          <span class="tlbar"><i style:left={`${row.pct}%`} /></span>
          <span class="tldue">{relDayBoard(row.due)}</span>
        </div>
      {:else}
        <p class="muted">no dated tasks yet</p>
      {/each}
      <div class="nowline" style:left={`${tlNow().pct}%`} hidden={!tlNow().on}><i>now</i></div>
    </div>
  {:else}
    <table class="list table">
      <thead><tr><th></th><th>#</th><th>task</th><th>labels</th><th>due</th><th>who</th><th></th></tr></thead>
      <tbody>
        {#each tasks as t}
          {@const st = statuses.find((s) => s.key === (t.status_type ?? t.status))}
          <tr onclick={() => openTask(t)} tabindex="0">
            <td>{#if st}<StatusIcon type={st.key} />{/if}</td>
            <td class="mono">#{t.number}</td>
            <td>{t.title}</td>
            <td>{#each t.labels ?? [] as lab}<span class="chip lbl">{lab}</span>{/each}</td>
            <td>{#if t.due}{relDayBoard(t.due)}{/if}</td>
            <td>{#each t.assignees ?? [] as a}<Avatar name={a.name} /> {a.name}{/each}</td>
            <td>{#if (t.priority ?? 0) >= 4}<PriorityIcon priority={t.priority} />{/if}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

{#if selected.size > 0}
  <div class="bulk on" data-kit="board.bulk" onpointerdown={(e) => e.stopPropagation()}>
    <span>{selected.size} selected</span>
    <button class="btn ghost" onclick={(e) => { bulkStatusOpen = !bulkStatusOpen; e.stopPropagation(); }}>Status</button>
    {#if bulkStatusOpen}
      <div class="menu bulks">
        {#each statuses as st (st.key)}
          <button class="mi" onclick={() => { void patchBulk({ status: st.key }); bulkStatusOpen = false; }}>
            <StatusIcon type={st.type} size={12} />{st.name}
          </button>
        {/each}
      </div>
    {/if}
    <button class="btn danger" onclick={() => { for (const id of selected) { const t = tasks.find((x) => x.id === id); if (t) void deleteTaskWithUndo(t); } selected = new Set(); }}>Delete</button>
    <button class="ib" aria-label="clear selection" onclick={() => (selected = new Set())}><X size={14} strokeWidth={1.75} /></button>
  </div>
{/if}

{#if menuFor}
  <div class="menu cardmenu" style:top={`${menuXY.y + 8}px`} style:left={`${menuXY.x - 8}px`}>
    {#if menuPane === ""}
      <button class="mi" onclick={() => (menuPane = "status")}>Status ▸</button>
      <button class="mi" onclick={() => (menuPane = "priority")}>Priority ▸</button>
      <button class="mi" onclick={() => (menuPane = "labels")}>Labels ▸</button>
      <button class="mi" onclick={() => (menuPane = "assign")}>Assign ▸</button>
      <button class="mi" onclick={() => (menuPane = "due")}>Due ▸</button>
      <button class="mi" style:color="var(--err)" onclick={() => { const t = menuFor; closeMenu(); if (t) void deleteTaskWithUndo(t); }}>Delete</button>
    {:else if menuPane === "status"}
      {#each statuses as st (st.key)}
        <button class="mi" onclick={() => { void patchBulk({ status: st.key }); closeMenu(); }}>
          <StatusIcon type={st.type} size={12} />{st.name}
        </button>
      {/each}
    {:else if menuPane === "priority"}
      {#each [0, 1, 2, 3, 4] as pv (pv)}
        <button class="mi" onclick={() => { void patchBulk({ priority: pv }); closeMenu(); }}>
          <PriorityIcon priority={pv} />{pv >= 4 ? "Urgent" : ["No priority", "Low", "Medium", "High", "Urgent"][pv]}
        </button>
      {/each}
    {/if}
    {#if menuPane === ""}<button class="ib closemenu" aria-label="close menu" onclick={closeMenu}><X size={14} strokeWidth={1.75} /></button>{/if}
  </div>
{/if}


{#if deepLoading}
  <p class="card muted" role="status">Opening the requested task…</p>
{:else if deepError}
  <div class="card err" role="alert">
    <p>{deepError}</p>
    {#if get(route).sub}
      <button class="btn" onclick={() => void loadLinkedTask(Number(get(route).sub))}>
        Retry task
      </button>
    {/if}
  </div>
{/if}

{#if newOpen && pid !== undefined}
  <NewTask pid={pid} statuses={statuses} tasks={tasks} startIn={newIn}
           onclose={() => (newOpen = false)} onmade={() => void refresh()} />
{/if}
{#if panelOpen}
  <TaskPanel task={panelTask} events={events} collisions={collisions} onclose={closePanel}
             onsave={async (t: Task | null, patch_: Record<string, unknown>) => {
               if (t) await finishAssign(t, patch_);
               else await createTask(patch_);
             }}
             statuses={statuses} mayWrite={mayWrite()}
            canComment={pid !== undefined && perms.allowed("add_work_package_comments", undefined, pid)}
            canEditOwn={pid !== undefined && perms.allowed("edit_own_messages", undefined, pid)}
            canDeleteOwn={pid !== undefined && perms.allowed("edit_own_messages", undefined, pid)}
            canRelate={mayWrite() && pid !== undefined &&
                      perms.allowed("manage_work_package_relations", undefined, pid)}
             candidates={tasks} onchanged={taskChanged}
             resetToken={discardEpoch}
            canDeleteTask={mayWrite() && pid !== undefined &&
                          perms.allowed("delete_work_packages", undefined, pid)}
            ondeleted={async (n?: number) => { closePanel(); undoTask = null; if (n) toast = `Task #${n} deleted`; await refresh(); }} />
{/if}
{#if conflict}
  <div class="conflict card" role="alertdialog" aria-label="the conflict">
    <p><b>Someone else just edited that task.</b></p>
    <p class="muted">Server version #{conflict.theirs.version}; your save was based on
      #{conflict.mine.version}. Keep theirs discards your draft. Apply yours writes
      your shown draft onto that server version.</p>
    <div class="row">
      <button class="btn" disabled={conflictBusy} onclick={async () => {
        const attempt = conflict!; conflictBusy = true;
        try {
          const theirs = await readCurrentTask(attempt.theirs.id);
          if (conflict !== attempt) return;
          // An explicit keep-theirs choice is the ONLY forced draft reset.
          if (panelOpen && panelTask?.id === theirs.id) {
            panelTask = theirs; ++discardEpoch;
          }
          // Card/drop conflicts have no open panel; that choice still resolves
          // the visible conflict and reloads the authoritative card.
          if (conflict === attempt) {
            conflict = null;
            void refresh();
          }
        } catch (err) {
          if (conflict === attempt)
            toast = err instanceof Error ? err.message : "the latest task couldn't be read";
        } finally {
          if (conflict === attempt) conflictBusy = false;
        }
      }}>keep theirs</button>
      <button class="btn primary" disabled={conflictBusy} onclick={async () => {
        const attempt = conflict!;
        const ownerEpoch = selectionEpoch;
        const ownerProject = pid;
        const ownsChoice = () => conflict === attempt &&
          selectionEpoch === ownerEpoch && pid === ownerProject;
        if (!ownsChoice()) return;
        conflictBusy = true;
        try {
          // Use the server version the person just reviewed, never the stale
          // local version from before the conflict. A newer write still gets
          // a fresh 409 and a new visible conflict below.
          await call(`/tasks/${attempt.mine.id}`,
            { ...attempt.mine, version: attempt.theirs.version }, "PATCH");
          if (!ownsChoice()) return;
          const answer = await call<{task: Task}>(`/tasks/${attempt.mine.id}`);
          if (!ownsChoice()) return;
          if (panelOpen && panelTask?.id === answer.task.id) {
            panelTask = answer.task; ++discardEpoch;
          }
          conflictBusy = false;
          conflict = null;
          void refresh();
        } catch (err) {
          // A later task owns its own conflict and messages. An old completion
          // cannot clear those controls or expose an unrelated failure there.
          if (!ownsChoice()) return;
          const e = err as { code?: string; message?: string };
          if (e.code === "conflict") {
            const fresh = await readCurrentTask(attempt.mine.id);
            if (ownsChoice()) {
              conflictBusy = false;
              conflict = { theirs: fresh, mine: attempt.mine };
            }
          } else {
            toast = e.message ?? "your draft couldn't be applied — it is still here";
          }
        } finally {
          if (ownsChoice()) conflictBusy = false;
        }
      }}>apply mine</button>
    </div>
  </div>
{/if}

<style>
  /* The canon for board/columns/cards/chips lives in theme/parts.css */

  .wrap { display: flex; flex-direction: column; gap: var(--sp-8); min-height: 100%; }
  .board .col-h { flex: none; }
  /* a phone shows one column at a time; it snaps, and the next one peeks */
  @media (max-width: 640px) {
    .board { scroll-snap-type: x mandatory; padding-left: 12px; padding-right: 12px; }
    .board .col { scroll-snap-align: start; }
  }
  .addin { width: 100%; box-sizing: border-box; height: 36px; padding: 0 10px; margin: 2px 0 6px;
           border: 1px solid var(--line-2); border-radius: var(--r-ctl); background: var(--raised);
           color: var(--text); font: inherit; font-size: 13px; }
  .addin:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: transparent; }
  .rowgap { display: flex; gap: var(--sp-8); align-items: flex-start; flex-wrap: wrap; }
  .toast { position: sticky; top: var(--sp-8); padding: var(--sp-8);
           display: flex; gap: var(--sp-8); align-items: center;
           z-index: 5; justify-content: space-between; }
  .num { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .title { font-size: 13.5px; color: var(--text); }
  .taskcard.done .title {
    text-decoration: line-through;
    text-decoration-thickness: 2px;
    text-decoration-color: color-mix(in oklab, var(--text) 45%, transparent);
    color: var(--text-2);
  }
  .nothing { padding: var(--sp-8); }
  .table { width: 100%; border-collapse: collapse; }
  .table th { text-align: left; font-size: 11.5px; color: var(--muted); padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .table td { padding: 6px 8px; border-bottom: 1px solid var(--line); font-size: 13px; }
  .table tr:hover td { background: var(--hover); }
  /* floats above the open task panel (z 25): on a narrow screen the panel
     covers the page, and the choice must stay reachable */
  .conflict { position: fixed; right: 16px; bottom: 16px; z-index: 30;
              width: min(340px, calc(100vw - 32px)); background: var(--raised);
              box-shadow: var(--pop); border: 1px solid var(--err);
              border-radius: var(--radius-card); padding: var(--sp-8);
              display: flex; flex-direction: column; gap: var(--sp-8); }
  .conflict .row { display: flex; gap: var(--sp-8); justify-content: flex-end; }
  .wrap { position: relative; }
  .cardmenu { position: fixed; display: flex; flex-direction: column; min-width: 160px; }
  .bulks { position: absolute; bottom: calc(100% + 4px); left: 0; display: flex; flex-direction: column; min-width: 160px; }
  .closemenu { margin-left: auto; }
  .strip-mark { width: 12px; height: 12px; object-fit: contain; vertical-align: -2px; }
</style>

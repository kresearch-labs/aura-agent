<!-- NewTask.svelte — the form for a new task: title and description, then the
     task's properties as the task panel shows them (column, people, priority,
     labels, start, due, what it waits for). Ctrl+Enter makes it, Esc closes,
     "make another" keeps the form open for the next one. -->
<script lang="ts">
  import { call } from "../../lib/api/client";
  import Chooser from "../../lib/parts/Chooser.svelte";
  import DateChooser from "../../lib/parts/DateChooser.svelte";
  import StatusIcon from "../../lib/parts/StatusIcon.svelte";
  import PriorityIcon from "../../lib/parts/PriorityIcon.svelte";
  import { Calendar, X } from "@lucide/svelte";

  type Status = { key: string; name: string; type: string };
  type TaskLite = { id: number; number: number; title: string; labels?: string[] };
  let { pid, statuses, tasks, startIn = "", onclose, onmade }:
    { pid: number; statuses: Status[]; tasks: TaskLite[]; startIn?: string;
      onclose: () => void; onmade: () => void } = $props();

  const PRIORITIES = [{ v: "0", label: "No priority" }, { v: "1", label: "Low" },
                      { v: "2", label: "Medium" }, { v: "3", label: "High" }, { v: "4", label: "Urgent" }];

  let title = $state("");
  let description = $state("");
  // svelte-ignore state_referenced_locally
  let status = $state(startIn || (statuses.find((s) => s.type === "todo") ?? statuses[0])?.key || "");
  let assignees = $state<string[]>([]);
  let priority = $state("0");
  let labels = $state<string[]>([]);
  let start = $state<number | null>(null);
  let due = $state<number | null>(null);
  let blockedBy = $state<string[]>([]);
  let another = $state(false);
  let busy = $state(false);
  let error = $state("");
  let note = $state("");
  let members = $state<string[]>([]);
  let titleBox: HTMLInputElement | undefined = $state();

  $effect(() => {
    void call<{ projects: { id: number; members?: { name: string }[] }[] }>("/projects")
      .then((d) => { members = (d.projects.find((x) => x.id === pid)?.members ?? []).map((m) => m.name); })
      .catch(() => { /* no list: the task can still be made unassigned */ });
    queueMicrotask(() => titleBox?.focus());
  });
  const knownLabels = $derived([...new Set([...tasks.flatMap((t) => t.labels ?? []), ...labels])].sort());
  const statusOf = $derived(statuses.find((s) => s.key === status));
  const day = (ts: number) => new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" });

  async function make() {
    error = ""; note = "";
    if (!title.trim()) { error = "Give the task a title."; titleBox?.focus(); return; }
    if (start && due && due < start) { error = "The due date is before the start."; return; }
    busy = true;
    try {
      const made = await call<{ task: { number: number } }>("/tasks", {
        project: pid, title: title.trim(), description, status,
        assignees, priority: Number(priority), labels,
        start, due, blocked_by: blockedBy.map(Number),
      }, "POST");
      onmade();
      if (another) {
        note = `#${made.task.number} made.`;
        title = ""; description = ""; blockedBy = [];
        queueMicrotask(() => titleBox?.focus());
      } else {
        onclose();
      }
    } catch (e) {
      error = e instanceof Error ? e.message : "The task wasn't made.";
    } finally {
      busy = false;
    }
  }
  function keys(e: KeyboardEvent) {
    if (e.key === "Escape") { e.preventDefault(); onclose(); }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void make(); }
  }
</script>

<div class="nt-shade" role="presentation" onclick={onclose}></div>
<div class="nt card" role="dialog" aria-modal="true" aria-labelledby="nt-h" tabindex="-1" onkeydown={keys}>
  <header class="nt-head">
    <h2 id="nt-h">New task</h2>
    <button class="ib" type="button" aria-label="Close" onclick={onclose}><X size={16} strokeWidth={1.75} /></button>
  </header>

  <input class="nt-title" bind:this={titleBox} bind:value={title} placeholder="Task title"
         aria-label="Title" maxlength="300" />
  <textarea class="nt-desc" bind:value={description} placeholder="Add a description…"
            aria-label="Description" rows="4"></textarea>

  <div class="nt-props" aria-label="task properties">
    {#snippet statusTrigger()}
      <button class="pill" type="button" aria-label="Column">
        <StatusIcon type={statusOf?.type ?? "todo"} label={false} /> {statusOf?.name ?? "Column"}</button>
    {/snippet}
    <Chooser options={statuses.map((s) => ({ v: s.key, label: s.name }))} value={status}
             onchange={(v) => (status = v as string)} trigger={statusTrigger} />

    {#snippet peopleTrigger()}
      <button class="pill" type="button" aria-label="Assignees">
        {assignees.length ? assignees.join(", ") : "Assignee"}</button>
    {/snippet}
    <Chooser search multiple options={members.map((m) => ({ v: m, label: m }))} value={assignees}
             onchange={(v) => (assignees = v as string[])} trigger={peopleTrigger} />

    {#snippet prioTrigger()}
      <button class="pill" type="button" aria-label="Priority">
        <PriorityIcon priority={Number(priority)} /> {PRIORITIES[Number(priority)].label}</button>
    {/snippet}
    <Chooser options={PRIORITIES} value={priority} onchange={(v) => (priority = v as string)}
             trigger={prioTrigger} />

    {#snippet labelTrigger()}
      <button class="pill" type="button" aria-label="Labels">
        {labels.length ? labels.join(", ") : "Labels"}</button>
    {/snippet}
    <Chooser search multiple create options={knownLabels.map((l) => ({ v: l, label: l }))} value={labels}
             onchange={(v) => (labels = v as string[])} trigger={labelTrigger} />

    {#snippet startTrigger()}
      <button class="pill" type="button" aria-label="Start date">
        <Calendar size={13} strokeWidth={1.75} /> {start ? `Starts ${day(start)}` : "Start"}</button>
    {/snippet}
    <DateChooser value={start} onchange={(ts) => (start = ts)} trigger={startTrigger} />

    {#snippet dueTrigger()}
      <button class="pill" type="button" aria-label="Due date">
        <Calendar size={13} strokeWidth={1.75} /> {due ? `Due ${day(due)}` : "Due"}</button>
    {/snippet}
    <DateChooser value={due} onchange={(ts) => (due = ts)} trigger={dueTrigger} />

    {#snippet blockTrigger()}
      <button class="pill" type="button" aria-label="Blocked by">
        {blockedBy.length ? `Waits for ${blockedBy.map((id) => "#" + (tasks.find((t) => String(t.id) === id)?.number ?? "?")).join(", ")}` : "Waits for…"}</button>
    {/snippet}
    <Chooser search multiple options={tasks.map((t) => ({ v: String(t.id), label: `#${t.number} ${t.title}` }))}
             value={blockedBy} onchange={(v) => (blockedBy = v as string[])} trigger={blockTrigger} />
  </div>

  {#if error}<p class="nt-err" role="alert">{error}</p>{/if}
  {#if note}<p class="muted nt-note" role="status">{note}</p>{/if}

  <footer class="nt-foot">
    <label class="nt-another"><input type="checkbox" bind:checked={another} /> Make another</label>
    <span class="spacer"></span>
    <button class="btn" type="button" onclick={onclose}>Cancel</button>
    <button class="btn primary" type="button" disabled={busy} onclick={() => void make()}
            title="Ctrl+Enter">{busy ? "Making…" : "Create task"}</button>
  </footer>
</div>

<style>
  .nt-shade { position: fixed; inset: 0; background: rgba(0, 0, 0, .35); z-index: 60; }
  .nt { position: fixed; z-index: 61; top: 12vh; left: 50%; transform: translateX(-50%);
        width: min(640px, calc(100vw - 24px)); max-height: 80vh; overflow: auto; box-sizing: border-box;
        display: flex; flex-direction: column; gap: 12px; padding: 18px 20px; }
  .nt-head { display: flex; align-items: center; justify-content: space-between; }
  .nt-head h2 { margin: 0; font-size: 14px; color: var(--muted); font-weight: 500; }
  .nt-title { width: 100%; box-sizing: border-box; border: 0; outline: none; background: transparent;
              color: var(--text); font: inherit; font-size: 20px; font-weight: 600; padding: 4px 0; }
  .nt-desc { width: 100%; box-sizing: border-box; border: 0; outline: none; background: transparent; resize: vertical;
             color: var(--text); font: inherit; font-size: 14px; line-height: 1.55; padding: 4px 0;
             min-height: 72px; field-sizing: content; max-height: 40vh; }
  .nt-props { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 10px; border-top: 1px solid var(--line); }
  .pill { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px;
          border: 1px solid var(--line-2); border-radius: 999px; background: var(--raised);
          color: var(--text-2); font: inherit; font-size: 12.5px; cursor: pointer; max-width: 260px;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill:hover { color: var(--text); border-color: var(--line-strong); }
  .nt-err { margin: 0; color: var(--err); font-size: 13px; }
  .nt-note { margin: 0; font-size: 13px; }
  .nt-foot { display: flex; align-items: center; gap: 8px; padding-top: 10px; border-top: 1px solid var(--line); }
  .nt-another { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-2); }
  .spacer { flex: 1; }
  @media (max-width: 640px) { .nt { top: 4vh; max-height: 92vh; } }
</style>

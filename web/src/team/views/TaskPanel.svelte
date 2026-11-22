<!-- The must-change law (version) still rides every save.
-->
<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { editedTaskFields, isCurrentCommentRead, mergeTaskDraft, snapshotLiveTask, type TaskDraft } from "../../lib/task_live";
  import Md from "../../lib/Md.svelte";
  import Chooser from "../../lib/parts/Chooser.svelte";
  import StatusIcon from "../../lib/parts/StatusIcon.svelte";
  import DateChooser from "../../lib/parts/DateChooser.svelte";
  import { dueState } from "../../lib/chat_logic";
  import { Ellipsis, X, Calendar, Plus } from "@lucide/svelte";
  import { me, live } from "../frame/shell_state";

  type Comment = { id: number; kind?: string; body: string;
                   person: string; created: number; edited?: number | null };
  type TaskLink = { kind: string; other: number };
  type Task = {
    id: number; number: number; title: string; description?: string;
    status: string; assignees?: { name: string }[]; labels?: string[];
    priority?: number; version: number; start?: number; due?: number;
    project?: number; links?: TaskLink[];
  };
  // runes-take: $props() gives anys; we narow at the first use —
  // $derived keeps the derivation LIVE (a re-open with a different task
  // would otherwise ride the very first draft)
  type PatchSh = { title?: string; description?: string;
                   assignees?: string[]; labels?: string[];
                   priority?: number; start?: number | null; due?: number | null }
                 & Record<string, unknown>;
  const props = $props();
  const task = $derived(props.task as Task | null);
  // Watch the primitive id, not the object's identity: a live replacement of
  // the same task must not reset a local comment or editing session.
  const taskId = $derived(task?.id);
  const statuses = $derived((props.statuses ?? []) as { key: string; name: string }[]);
  const mayWrite = $derived(props.mayWrite as boolean);
  const onclose = $derived(props.onclose as () => void);
  const evtIn = $derived((props.events ?? []) as { seq: number; type: string; who: string | null; ts: number; task_ref?: { number: number; title: string } | null }[]);
  const colIn = $derived((props.collisions ?? []) as Collision[]);
  import { auraStates as _aS, stripText as _sT, type AuraState as _AS, type Collision } from "../../lib/aura_state";
  function stripsFor(t: Task): { cls: string; text: string }[] {
    if (!t) return [];
    const st = _aS(evtIn).get(t.number);
    const done = ((t as Task & { status_type?: string }).status_type ?? t.status) === "done";
    const out: { cls: string; text: string }[] = [];
    if (st) {
      if (st.kind === "working") out.push({ cls: "c-aura", text: _sT(st, t.assignees?.[0]?.name ?? "", (u) => u, Date.now() / 1000) });
      else if (st.kind === "waiting") out.push({ cls: "c-ask", text: _sT(st, t.assignees?.[0]?.name ?? "", (u) => u, Date.now() / 1000) });
      else if (st.kind === "done" && !done) out.push({ cls: "c-aura", text: _sT(st, t.assignees?.[0]?.name ?? "", (u) => u, Date.now() / 1000) });
    }
    return out;
  }
  const onsave = $derived(props.onsave as (t: Task | null, patch_: PatchSh) => Promise<void>);
  const canComment = $derived(!!props.canComment);
  const canEditOwn = $derived(!!props.canEditOwn);
  const canDeleteOwn = $derived(!!props.canDeleteOwn);
  const canRelate = $derived(!!props.canRelate);
  const canDeleteTask = $derived(!!props.canDeleteTask);
  const ondeleted = $derived(props.ondeleted as (n?: number) => Promise<void>);
  const onchanged = $derived(props.onchanged as () => Promise<void>);
  const resetToken = $derived(Number(props.resetToken ?? 0));
  const candidates = $derived((props.candidates ?? []) as Task[]);
  const choices = $derived(candidates.filter((t) => t.id !== taskId));
  let otherId = $state<number>(0);
  let relation = $state("relates_to");
  let linkBusy = $state(false);
  let linkError = $state("");
  const myName = $derived($me.value?.name ?? "");
  let comments = $state<Comment[]>([]);
  let commentsBusy = $state(true);
  let commentError = $state("");
  let commentBusy = $state(false);
  let draft = $state("");
  let editingId = $state<number | null>(null);
  let editText = $state("");
  let editingOriginal = $state<Comment | null>(null);
  let commentCollision = $state("");
  let commentEpoch = 0;
  let commentMutationEpoch = 0;
  let commentDraftEpoch = 0;
  let commentEditEpoch = 0;
  // Entity ownership is shared; operation generations are independent. A
  // save cannot cancel an unrelated relation/delete completion on this task.
  let taskEntityEpoch = 0;
  let saveEpoch = 0;
  let relationEpoch = 0;
  let deleteEpoch = 0;
  let lastTaskMutationIdentity: {id: number | undefined; reset: number} | null = null;
  $effect(() => {
    const identity = {id: taskId, reset: resetToken};
    if (lastTaskMutationIdentity?.id === identity.id &&
        lastTaskMutationIdentity?.reset === identity.reset) return;
    lastTaskMutationIdentity = identity;
    ++taskEntityEpoch;
  });
  const ownsCommentMutation = (tid: number, requestEpoch: number) =>
    isCurrentCommentRead(tid, taskId, requestEpoch, commentMutationEpoch);

  async function loadComments(tid: number, epoch?: number) {
    // A mutation may resolve after a different task is already open. Do not
    // let that old request invalidate the current task's own comment read.
    if (!isCurrentCommentRead(tid, taskId, commentEpoch, commentEpoch)) return;
    const generation = epoch ?? ++commentEpoch;
    commentsBusy = true;
    try {
      const d = await call<{comments: Comment[]}>(`/tasks/${tid}/comments`);
      if (!isCurrentCommentRead(tid, taskId, generation, commentEpoch)) return;
      if (!Array.isArray(d.comments)) {
        commentError = "the server returned no comments";
        return;
      }
      if (editingId !== null && editingOriginal) {
        const fresh = d.comments.find((c) => c.id === editingId);
        if (!fresh) {
          commentCollision = "This comment was deleted elsewhere. Your draft is preserved below.";
        } else if (fresh.body !== editingOriginal.body) {
          commentCollision = "This comment changed elsewhere. Your draft is preserved; review it before saving.";
        } else {
          commentCollision = "";
        }
      } else if (editingId === null) {
        commentCollision = "";
      }
      comments = d.comments;
      commentError = "";
    } catch (e) {
      if (isCurrentCommentRead(tid, taskId, generation, commentEpoch))
        commentError = e instanceof Error ? e.message : "comments could not load";
    } finally {
      if (isCurrentCommentRead(tid, taskId, generation, commentEpoch)) commentsBusy = false;
    }
  }

  $effect(() => {
    const id = taskId;
    ++commentEpoch;                 // invalidate a previous task's read
    ++commentMutationEpoch;         // and a previous task's pending mutation
    ++commentDraftEpoch;
    ++commentEditEpoch;
    comments = []; editingId = null; editText = ""; draft = "";
    editingOriginal = null; commentCollision = "";
    commentError = ""; commentBusy = false;
    linkError = ""; linkBusy = false; otherId = 0; relation = "relates_to";
    if (id !== undefined) void loadComments(id, ++commentEpoch);
    else commentsBusy = false;
  });

  // the assignee choices: this task's project members (GET /projects lists them)
  $effect(() => {
    const pidv = task?.project;
    if (pidv === undefined) return;
    void call<{ projects: { id: number; members?: { name: string }[] }[] }>("/projects")
      .then((d) => {
        if (task?.project !== pidv) return;
        members = (d.projects.find((x) => x.id === pidv)?.members ?? []).map((m) => m.name);
      })
      .catch(() => { /* a read-only panel still shows the current names */ });
  });
  onDestroy(() => {
    // An unmounted branch may retain its old task id. Make destruction an
    // ownership change so its settled requests cannot read or update UI.
    ++commentEpoch;
    ++commentMutationEpoch;
    ++commentDraftEpoch;
    ++commentEditEpoch;
    ++taskEntityEpoch;
  });

  onMount(() => live.subscribe((ev) => {
    if (!ev || ev.kind !== "comment") return;
    const tid = (ev.data as { task?: number } | null | undefined)?.task;
    if (tid !== undefined && tid === taskId) void loadComments(tid);
  }));

  async function addComment(e: Event) {
    e.preventDefault();
    const t = task;
    if (!t || !canComment || commentBusy || !draft.trim()) return;
    const requestEpoch = ++commentMutationEpoch;
    const inputEpoch = commentDraftEpoch;
    const submitted = draft;
    commentBusy = true; commentError = "";
    try {
      await call(`/tasks/${t.id}/comments`, {body: submitted.trim()}, "POST");
      if (ownsCommentMutation(t.id, requestEpoch)) {
        if (inputEpoch === commentDraftEpoch) draft = "";
        await loadComments(t.id);
      }
    } catch (e) {
      if (ownsCommentMutation(t.id, requestEpoch))
        commentError = e instanceof Error ? e.message : "the comment was not saved";
    } finally {
      if (ownsCommentMutation(t.id, requestEpoch)) commentBusy = false;
    }
  }

  function beginCommentEdit(c: Comment) {
    if (commentBusy || !canEditOwn) return;
    if (editingId !== null && editingOriginal && editText !== editingOriginal.body) {
      commentError = "Save or cancel your current comment edit first.";
      return;
    }
    editingId = c.id; editText = c.body; editingOriginal = {...c};
    commentCollision = ""; commentError = "";
  }

  async function saveComment(cid: number) {
    const tid = taskId;
    if (!tid || !canEditOwn || commentBusy || commentCollision || !editText.trim()) return;
    const requestEpoch = ++commentMutationEpoch;
    const inputEpoch = commentEditEpoch;
    const submitted = editText;
    commentBusy = true; commentError = "";
    try {
      await call(`/comments/${cid}`, {body: submitted.trim()}, "PATCH");
      if (taskId === tid && editingId === cid && ownsCommentMutation(tid, requestEpoch) &&
          inputEpoch === commentEditEpoch) {
        // Clear BEFORE refresh: an acknowledged save by this editor is not a
        // conflict with another person's in-tab editor.
        editingId = null; editText = ""; editingOriginal = null;
        commentCollision = "";
      }
      if (ownsCommentMutation(tid, requestEpoch)) await loadComments(tid);
    } catch (e) {
      if (ownsCommentMutation(tid, requestEpoch))
        commentError = e instanceof Error ? e.message : "the edit was not saved";
    } finally {
      if (ownsCommentMutation(tid, requestEpoch)) commentBusy = false;
    }
  }

  async function deleteComment(cid: number) {
    const tid = taskId;
    if (!tid || !canDeleteOwn || commentBusy) return;
    if (!confirm("Delete your comment?")) return;
    const requestEpoch = ++commentMutationEpoch;
    commentBusy = true; commentError = "";
    try {
      await call(`/comments/${cid}`, {}, "DELETE");
      if (taskId === tid && editingId === cid && ownsCommentMutation(tid, requestEpoch)) {
        editingId = null; editText = ""; editingOriginal = null;
        commentCollision = "";
      }
      if (ownsCommentMutation(tid, requestEpoch)) await loadComments(tid);
    } catch (e) {
      if (ownsCommentMutation(tid, requestEpoch))
        commentError = e instanceof Error ? e.message : "the comment was not deleted";
    } finally {
      if (ownsCommentMutation(tid, requestEpoch)) commentBusy = false;
    }
  }

  // the draft rebinds when the panel reopens with another task
  let title = $state("");
  let description = $state("");
  let assignees = $state("");
  let tab = $state<"activity" | "files">("activity");
  let openProp = $state("");
  let members = $state<string[]>([]);
  let addedLabels = $state<string[]>([]);
  // every label this project already uses, plus the ones made here
  const knownLabels = $derived([...new Set([
    ...candidates.flatMap((t) => t.labels ?? []), ...(task?.labels ?? []), ...addedLabels,
  ])].sort());
  // A pick is one field, saved at once. Picks run one after another, each on
  // the server's current version (a pick is a deliberate change of that one
  // field), and a failure shows in the panel instead of vanishing.
  let pickQueue: Promise<void> = Promise.resolve();
  function pickSave(patch_: PatchSh) {
    const subject = task;
    if (!subject || !mayWrite) return;
    const subjectId = subject.id;
    pickQueue = pickQueue.then(async () => {
      if (taskId !== subjectId) return;
      error = "";
      try {
        const cur = await call<{ task: Task }>(`/tasks/${subjectId}`);
        await onsave(cur.task, { ...patch_, version: cur.task.version });
      } catch (err) {
        if (taskId === subjectId) error = err instanceof Error ? err.message : "Not saved";
      }
    });
  }
  function saveAssignees(list: string[]) { assignees = list.join(","); pickSave({ assignees: list }); }
  function savePriority(nv: number) { priority = String(nv); pickSave({ priority: nv }); }
  function saveLabels(list: string[]) {
    labels = list.join(",");
    addedLabels = [...new Set([...addedLabels, ...list])];
    pickSave({ labels: list });
  }
  function saveStatus(key: string) { pickSave({ status: key }); }
  function statusWord(): string {
    const keyv = (task as (Task & { status_type?: string }) | null)?.status_type ?? task?.status;
    return (statuses.find((s) => s.key === keyv)?.name ?? keyv ?? "—").replaceAll("_", " ");
  }
  function priorityWord(): string {
    const pn = Math.max(0, Math.min(4, Number(priority) || 0));
    return ["No priority", "Low", "Medium", "High", "Urgent"][pn];
  }
  let labels = $state("");
  let priority = $state("0");
  let start = $state("");
  let due = $state("");
  let busy = $state(false); let error = $state("");
  let knownServerTask: Task | null = null;
  let lastResetToken = -1;
  let lastIncomingTask: Task | null | undefined;
  let baseVersion = $state(0);
  let liveTaskNote = $state("");

  $effect(() => {
    const incoming = task;
    const forced = resetToken !== lastResetToken;
    // Read editable state only for a changed server object or an explicit
    // reset. Otherwise every local edit (and even a blank new-task form)
    // would re-enter this effect and merge against itself.
    if (!forced && lastIncomingTask === incoming) return;
    lastIncomingTask = incoming;
    const current: TaskDraft = {
      title, description, assignees, labels, priority: String(priority), start, due,
    };
    const changedTask = incoming?.id !== knownServerTask?.id;
    const merged = mergeTaskDraft(knownServerTask, current, incoming, forced);
    if (merged.outdated) return;
    // Keep the newest authoritative FIELD values, but anchor their remembered
    // version to this draft. Re-fed server snapshots cannot advance past a
    // local edit; only an explicit resolution or a clean draft may advance.
    knownServerTask = snapshotLiveTask(incoming, merged.baseVersion);
    lastResetToken = resetToken;
    baseVersion = merged.baseVersion;
    liveTaskNote = merged.remoteChange
      ? `Another update arrived while you were editing. Your draft is based on #${merged.baseVersion}; the server is now #${incoming?.version}.`
      : "";
    title = merged.form.title; description = merged.form.description;
    assignees = merged.form.assignees; labels = merged.form.labels;
    priority = merged.form.priority; start = merged.form.start; due = merged.form.due;
    // Background updates do NOT erase a failed save's actual error. Only an
    // explicit conflict resolution or a different task starts a new state.
    if (forced || changedTask) {
      busy = false; error = "";
      // A reset has new entity ownership; do not leave its visible relation
      // controls disabled by the operation the reset deliberately revoked.
      linkBusy = false; linkError = "";
    }
  });

  function relationName(kind: string): string {
    const names: Record<string, string> = {
      blocked_by: "blocked by", relates_to: "relates to", parent: "parent",
    };
    return names[kind] ?? kind;
  }

  function taskName(id: number): string {
    return choices.find((t) => t.id === id)?.title ?? `task ${id}`;
  }

  async function changeRelation(remove?: TaskLink) {
    const t = task;
    if (!t || !canRelate || linkBusy) return;
    if (!remove && !otherId) return;
    if (remove && !t.links?.some((l) => l.kind === remove.kind && l.other === remove.other))
      return;
    const subjectId = t.id;
    const requestEpoch = ++relationEpoch;
    const entityEpoch = taskEntityEpoch;
    const ownsRelation = () => taskId === subjectId &&
      taskEntityEpoch === entityEpoch && relationEpoch === requestEpoch;
    linkBusy = true; linkError = "";
    try {
      const kind = remove?.kind ?? relation;
      const other = remove?.other ?? otherId;
      await call(`/tasks/${t.id}/links/${kind}/${other}`, {},
                 remove ? "DELETE" : "PUT");
      // The acknowledged write can refresh the current visible project. Only
      // its originating editor may apply local errors or clear pending state.
      await onchanged();
    } catch (e) {
      if (ownsRelation())
        linkError = e instanceof Error ? e.message : "the relationship was not changed";
    } finally { if (ownsRelation()) linkBusy = false; }
  }

  async function deleteThisTask() {
    const t = task;
    if (!t || !canDeleteTask || busy) return;
    confirmDelete = false;
    const subjectId = t.id;
    const requestEpoch = ++deleteEpoch;
    const entityEpoch = taskEntityEpoch;
    const ownsDelete = () => taskId === subjectId &&
      taskEntityEpoch === entityEpoch && deleteEpoch === requestEpoch;
    busy = true; error = "";
    try {
      await call(`/tasks/${t.id}`, {}, "DELETE");
      if (ownsDelete()) await ondeleted(t.number);
    } catch (e) {
      if (ownsDelete())
        error = e instanceof Error ? e.message : "The task was not deleted";
    } finally { if (ownsDelete()) busy = false; }
  }

  // one field at a time, through the panel's own clash path (onsave → the
  // board's conflict card on 409); the version is this draft's base version
  async function saveFields(patch_: PatchSh) {
    const subject = task;
    if (!subject || !mayWrite || busy) return;
    if (!knownServerTask) { error = "The task is still loading; try again"; return; }
    patch_.version = baseVersion;
    busy = true; error = "";
    const subjectId = subject.id;
    const requestEpoch = ++saveEpoch;
    const entityEpoch = taskEntityEpoch;
    const ownsSave = () => taskId === subjectId &&
      taskEntityEpoch === entityEpoch && saveEpoch === requestEpoch;
    await onsave(subject, patch_).then(() => {}).catch((err) => {
      if (ownsSave()) error = err instanceof Error ? err.message : "Not saved";
    }).finally(() => {
      if (ownsSave()) busy = false;
    });
  }

  // title, edited in place
  let editingTitle = $state(false);
  let titleInput: HTMLInputElement | undefined = $state();
  function beginTitle() {
    if (!mayWrite || !task) return;
    title = task.title; editingTitle = true;
    queueMicrotask(() => { titleInput?.focus(); titleInput?.select(); });
  }
  function endTitle(keep: boolean) {
    if (!editingTitle) return;
    editingTitle = false;
    const next = title.trim();
    if (keep && task && next && next !== task.title) void saveFields({ title: next });
    else if (task) title = task.title;
  }

  // description: edited in place; Save and Cancel show once it changed
  function saveDesc() {
    if (task && description !== (task.description ?? "")) void saveFields({ description });
  }

  // dates: the chooser gives seconds or null (Clear)
  function saveDate(field: "due" | "start", v: number | null) {
    void saveFields({ [field]: v });
  }

  // relations: kind first, then the task
  let addingKind = $state<string | null>(null);
  const RELATION_KINDS = [
    { v: "blocked_by", label: "Blocked by" },
    { v: "relates_to", label: "Relates to" },
    { v: "parent", label: "Parent (one level)" },
    { v: "duplicates", label: "Duplicates" },
  ];
  async function addRelation(other: number) {
    if (!addingKind) return;
    relation = addingKind; otherId = other; addingKind = null;
    await changeRelation();
    otherId = 0;
  }

  // ⋯ menu and the delete question
  let moreOpen = $state(false);
  let confirmDelete = $state(false);
</script>

{#if true}
  <div class="panel-scrim" role="presentation" onclick={onclose}></div>
  <div class="card sidepanel" role="dialog" aria-label="the task card"
       data-kit="task.panel"
       data-task-id={taskId ?? ""}>
    <header class="task-h" data-kit="task.head">
      <span class="mono">{#if task}#{task.number}{:else}Task{/if}</span>
      <span class="spacer"></span>
      {#if task && canDeleteTask}
        <span class="more">
          <button class="ib" type="button" aria-label="More actions" aria-haspopup="menu"
                  aria-expanded={moreOpen} onclick={() => (moreOpen = !moreOpen)}>
            <Ellipsis size={14} strokeWidth={1.75} /></button>
          {#if moreOpen}
            <div class="menu" role="menu" tabindex="-1"
                 onkeydown={(e: KeyboardEvent) => { if (e.key === "Escape") moreOpen = false; }}>
              <button class="mi danger" role="menuitem" type="button" disabled={busy}
                      onclick={() => { moreOpen = false; confirmDelete = true; }}>Delete task</button>
            </div>
          {/if}
        </span>
      {/if}
      <button class="ib x" type="button" onclick={onclose} aria-label="Close"><X size={14} strokeWidth={1.75} /></button>
    </header>
    <div class="task" data-kit="task.box">
    {#if task}
      {#if editingTitle}
        <input class="task-title title-edit" data-kit="task.title" aria-label="Title"
               bind:this={titleInput} bind:value={title}
               onkeydown={(e: KeyboardEvent) => {
                 if (e.key === "Enter") { e.preventDefault(); endTitle(true); }
                 else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endTitle(false); }
               }}
               onblur={() => endTitle(true)} />
      {:else}
        <button type="button" class="task-title title-btn" data-kit="task.title"
                title={mayWrite ? "Click to rename" : undefined} disabled={!mayWrite}
                onclick={beginTitle}>{task.title}</button>
      {/if}
    {/if}
    {#each stripsFor(task as Task) as s, spi (spi + s.cls + s.text)}
      <span class={`${s.cls} cardstrip`}>{s.text}</span>
    {/each}
    {#if task}
    <div class="props" data-kit="task.props">
      <span class="k" data-kit="task.prop-label">Status</span>
      {#snippet statusTrigger()}
        <button class="v" data-kit="task.status-value">
          <StatusIcon type={(task as (Task & { status_type?: string }) | null)?.status_type ?? task?.status ?? "todo"} />
          {statusWord()}
        </button>
      {/snippet}
      <Chooser options={(statuses ?? []).map((s) => ({ v: s.key, label: s.name,
                 icon: undefined })) }
               value={(task as (Task & { status_type?: string }) | null)?.status_type ?? task?.status ?? ""}
               onchange={(v: string | string[]) => void saveStatus(v as string)}
               trigger={statusTrigger} />
      <span class="k" data-kit="task.prop-label">Assignees</span>
      {#snippet assignTrigger()}
        <button class="v" data-kit="task.prop-value">{#if assignees}{assignees.split(",").join(", ").slice(0, 40)}{:else}<span class="muted">Add</span>{/if}</button>
      {/snippet}
      <Chooser search multiple trigger={assignTrigger}
               options={(members ?? []).map((mn) => ({ v: mn, label: mn,
                 icon: undefined as any })) }
               value={(assignees || "").split(",").map((s) => s.trim()).filter(Boolean)}
               onchange={(v) => void saveAssignees(v as string[])} />
      <span class="k" data-kit="task.prop-label">Priority</span>
      {#snippet prioTrigger()}
        <button class="v" data-kit="task.prop-value">{priorityWord()}</button>
      {/snippet}
      <Chooser options={[{v:"0",label:"No priority"},{v:"1",label:"Low"},
                         {v:"2",label:"Medium"},{v:"3",label:"High"},{v:"4",label:"Urgent"}]}
               value={String(priority ?? 0)}
               onchange={(v: string | string[]) => void savePriority(Number(v))}
               trigger={prioTrigger} />
      <span class="k" data-kit="task.prop-label">Labels</span>
      {#snippet labelTrigger()}
        <button class="v" data-kit="task.prop-value">{#if labels}{labels.split(",").join(", ").slice(0, 40)}{:else}<span class="muted">Add</span>{/if}</button>
      {/snippet}
      <Chooser search multiple create trigger={labelTrigger}
               options={(knownLabels ?? []).map((ln) => ({ v: ln, label: ln }))}
               value={(labels || "").split(",").map((s) => s.trim()).filter(Boolean)}
               onchange={(v) => void saveLabels(v as string[])} />
      <span class="k" data-kit="task.prop-label">Start</span>
      {#snippet startTrigger()}
        <button class="v" data-kit="task.prop-value" aria-label="Start date">
          <Calendar size={14} strokeWidth={1.75} class="muted" />
          {#if task?.start}{new Date(task.start * 1000).toLocaleDateString()}{:else}<span class="muted">Set date</span>{/if}
        </button>
      {/snippet}
      <DateChooser value={task.start ?? null} trigger={startTrigger}
                   onchange={(v) => saveDate("start", v)} />
      <span class="k" data-kit="task.prop-label">Due</span>
      {#snippet dueTrigger()}
        <button class="v" data-kit="task.prop-value" aria-label="Due date">
          <Calendar size={14} strokeWidth={1.75} class="muted" />
          {#if task?.due}<span class:hot={dueState(task.due)?.hot}>{dueState(task.due)?.text}</span>{:else}<span class="muted">Set date</span>{/if}
        </button>
      {/snippet}
      <DateChooser value={task.due ?? null} trigger={dueTrigger}
                   onchange={(v) => saveDate("due", v)} />
    </div>

    <div class="desc">
      <textarea class="desc-text" aria-label="Description" rows="3" bind:value={description}
                placeholder={mayWrite ? "Add a description…" : "No description."} readonly={!mayWrite}
                oninput={(e) => { const t = e.currentTarget; t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; }}></textarea>
      {#if mayWrite && description !== (task.description ?? "")}
        <div class="row">
          <button class="btn pri" type="button" disabled={busy} onclick={saveDesc}>Save</button>
          <button class="btn ghost" type="button" onclick={() => (description = task?.description ?? "")}>Cancel</button>
        </div>
      {/if}
    </div>
    {/if}
    {#if liveTaskNote}<p class="card warn" role="status">{liveTaskNote}</p>{/if}
    {#if error}<p class="err" role="alert">{error}</p>{/if}

    {#if task}
    <div class="tabs2" data-kit="task.tabs" role="tablist">
      <button role="tab" data-kit={tab === "activity" ? "task.tab-on" : undefined} class:tab-on={tab === "activity"} aria-selected={tab === "activity"}
              onclick={() => (tab = "activity")}>Activity</button>
      <button role="tab" data-kit={tab === "files" ? "task.tab-on" : undefined} class:tab-on={tab === "files"} aria-selected={tab === "files"}
              onclick={() => (tab = "files")}>Files</button>
    </div>
    {#if tab === "activity"}
    <section class="relations" aria-label="task relationships">
      <h3>Relations</h3>
      {#if linkError}<p class="err" role="alert">{linkError}</p>{/if}
      <div class="linkchips">
        {#each task.links ?? [] as link (`${link.kind}:${link.other}`)}
          <span class="chip task-link">
            <span class="muted">{relationName(link.kind)}</span>
            <a href={`#/board/${link.other}`} aria-label={`open task ${link.other}`}>{taskName(link.other)}</a>
            {#if canRelate}
              <button class="chipx" type="button"
                      aria-label={`remove relationship ${link.kind} ${link.other}`}
                      disabled={linkBusy}
                      onclick={() => void changeRelation(link)}><X size={12} strokeWidth={1.75} /></button>
            {/if}
          </span>
        {:else}
          <span class="muted">No relations.</span>
        {/each}
      </div>
      {#if canRelate}
        {#if addingKind === null}
          {#snippet kindTrigger()}
            <button class="btn ghost" type="button" disabled={linkBusy}><Plus size={14} strokeWidth={1.75} />Add relation</button>
          {/snippet}
          <Chooser options={RELATION_KINDS} value="" trigger={kindTrigger}
                   onchange={(v) => (addingKind = v as string)} />
        {:else}
          {#snippet otherTrigger()}
            <button class="btn ghost" type="button" disabled={linkBusy}>{RELATION_KINDS.find((k) => k.v === addingKind)?.label}: choose a task</button>
          {/snippet}
          <Chooser search options={choices.map((o) => ({ v: String(o.id), label: `#${o.number} ${o.title}` }))}
                   value="" trigger={otherTrigger}
                   onchange={(v) => void addRelation(Number(v))} />
          <button class="btn ghost" type="button" onclick={() => (addingKind = null)}>Cancel</button>
        {/if}
      {/if}
    </section>

    <section class="comments" aria-label="task comments">
      <h3>Comments</h3>
      {#if commentsBusy}<p class="muted">asking for the comments…</p>{/if}
      {#if commentError}<p class="err" role="alert">{commentError}</p>{/if}
      {#if !commentsBusy && !comments.length}
        <p class="muted">No comments yet.</p>
      {/if}
      <ul class="commentlist">
        {#each comments as c (c.id)}
          <li class="task-comment" data-comment-id={c.id}>
            <header class="commentwho"><b>{c.person}</b>
              <span class="muted">{c.edited ? "edited" : c.kind || "text"}</span>
            </header>
            {#if editingId === c.id}
              {#if commentCollision}
                <div class="card warn" role="alert">
                  <p>{commentCollision}</p>
                  <details><summary>server version</summary><Md text={c.body} /></details>
                  <button class="btn tiny" type="button" disabled={commentBusy}
                    onclick={() => { editText = c.body; editingOriginal = {...c};
                      commentCollision = ""; }}>Use latest server text</button>
                </div>
              {/if}
              <textarea rows="3" bind:value={editText} required
                        oninput={() => ++commentEditEpoch}
                        aria-label="edit comment text"></textarea>
              <div class="row">
                <button class="btn primary" type="submit"
                        onclick={() => void saveComment(c.id)} disabled={commentBusy || !!commentCollision}>
                  Save comment
                </button>
                <button class="btn" type="button" disabled={commentBusy}
                        onclick={() => { editingId = null; editText = "";
                          editingOriginal = null; commentCollision = ""; }}>Cancel</button>
              </div>
            {:else}
              <div class="task-comment-body"><Md text={c.body} /></div>
              {#if c.person === myName}
                <div class="row">
                  {#if canEditOwn}
                    <button class="btn tiny" type="button"
                            aria-label={`edit comment ${c.id}`}
                            disabled={commentBusy}
                            onclick={() => beginCommentEdit(c)}>Edit</button>
                  {/if}
                  {#if canDeleteOwn}
                    <button class="btn tiny" type="button"
                            aria-label={`delete comment ${c.id}`}
                            disabled={commentBusy}
                            onclick={() => void deleteComment(c.id)}>Delete</button>
                  {/if}
                </div>
              {/if}
            {/if}
          </li>
        {/each}
      </ul>
      {#if editingId !== null && editingOriginal &&
          !comments.some((c) => c.id === editingId)}
        <section class="card warn" aria-label="preserved deleted comment draft">
          <p role="alert">{commentCollision}</p>
          <label for="preserved-comment">preserved draft (not posted)</label>
          <textarea id="preserved-comment" rows="3"
                    aria-label="preserved comment draft" bind:value={editText}></textarea>
          <button class="btn" type="button"
            onclick={() => { editingId = null; editText = ""; editingOriginal = null;
              commentCollision = ""; }}>Discard preserved draft</button>
        </section>
      {/if}
      {#if canComment}
        <form onsubmit={addComment}>
          <label for="task-new-comment">New comment</label>
          <textarea id="task-new-comment" rows="3" bind:value={draft} required
                    oninput={() => ++commentDraftEpoch}
                    aria-label="new task comment"></textarea>
          <button class="btn primary" type="submit"
                  aria-label="add task comment"
                  disabled={commentBusy || !draft.trim()}>Add comment</button>
        </form>
      {:else}
        <p class="muted">Your role may read these comments but not add one.</p>
      {/if}
    </section>
    {/if}
    {#if tab === "files"}
      <section class="files-tab" aria-label="files">
        <p class="muted">No files here yet.</p>
      </section>
    {/if}
    {/if}
  </div>
    </div>
{/if}

{#if confirmDelete && task}
  <div class="backdrop" role="presentation" onclick={() => (confirmDelete = false)}></div>
  <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="del-q" tabindex="-1"
       onkeydown={(e: KeyboardEvent) => { if (e.key === "Escape") confirmDelete = false; }}>
    <p id="del-q"><b>Delete #{task.number}?</b> This can't be undone.</p>
    <div class="row">
      <button class="btn danger" type="button" disabled={busy} onclick={() => void deleteThisTask()}>Delete</button>
      <button class="btn sec2" type="button" onclick={() => (confirmDelete = false)}>Cancel</button>
    </div>
  </div>
{/if}

<style>
  .sidepanel {
    position: fixed; top: 0; right: 0; bottom: 0;
    width: min(560px, 92vw); z-index: 25;
    border-radius: 0; border-left: 1px solid var(--line-strong);
    display: flex; flex-direction: column; gap: var(--sp-12);
    padding: var(--sp-16); overflow: auto;
  }
  .sidepanel .rowgap { justify-content: space-between; }
  .sidepanel .task { height: auto; overflow: visible; gap: var(--sp-12); padding: 18px 20px; }
  /* on a phone the page behind the panel is dimmed and closes it on a tap */
  .panel-scrim { display: none; }
  @media (max-width: 700px) {
    .panel-scrim { display: block; position: fixed; inset: 0; z-index: 24; background: rgba(0, 0, 0, .3); }
    .sidepanel { width: 100vw; }
    /* the page's menu button stays on top; the panel's head leaves it room */
    .sidepanel :global(.task-h) { padding-left: 44px; }
  }
  .comments, .relations { border-top: 1px solid var(--line); padding-top: var(--sp-12); }
  .linkchips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .task-link a { color: var(--text); }
  .chipx { display: grid; place-items: center; width: 18px; height: 18px; border-radius: 5px; color: var(--muted); }
  .chipx:hover { background: var(--hover); color: var(--text); }
  .spacer { flex: 1; }
  .more { position: relative; }
  .more .menu { right: 0; top: calc(100% + 4px); }
  .mi.danger { color: var(--err); }
  /* the title's font comes from parts.css .task-title; only the button chrome goes */
  .title-btn { display: block; width: 100%; text-align: left; background: transparent; border: 0;
               padding: 0; color: inherit; cursor: text; border-radius: 6px; }
  .title-btn:hover:not(:disabled) { background: var(--hover); }
  .title-btn:focus-visible { outline: 2px solid var(--acc); }
  .title-edit { width: 100%; border: 0; outline: none; background: var(--raised-2); border-radius: 6px; color: var(--text); }
  .desc { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
  .desc-text { width: 100%; resize: none; border: 0; outline: none; background: transparent;
               font: inherit; color: inherit; line-height: inherit; padding: 4px 8px; margin: -4px -8px;
               border-radius: 6px; min-height: 48px;
               /* the whole description, growing with its text (no cut-off) */
               field-sizing: content; max-height: 50vh; overflow: auto; }
  .desc-text:hover:not([readonly]) { background: var(--hover); }
  .desc-text:focus { background: var(--raised-2); }
  .hot { color: var(--err); }
  .dialog { position: fixed; z-index: 80; left: 50%; top: 30%; transform: translateX(-50%);
            width: min(380px, 92vw); padding: 18px; background: var(--raised);
            border-radius: var(--r-panel); box-shadow: var(--pop); }
  .backdrop { position: fixed; inset: 0; z-index: 79; background: rgba(0, 0, 0, .3); }

  .commentlist { list-style: none; padding: 0; display: grid; gap: var(--sp-12); }
  .task-comment { border-top: 1px solid var(--line); padding-top: var(--sp-8); }
  .commentwho { display: flex; gap: var(--sp-8); align-items: baseline; }
  .task-comment-body { overflow-wrap: anywhere; }
  .comments textarea { width: 100%; }

  .row { display: flex; gap: var(--sp-8); }
  .tabs2 { display: flex; gap: 2px; margin: 12px 0 4px; border-bottom: 1px solid var(--line); }
  .tabs2 button { all: unset; padding: 8px 14px; font: inherit; font-size: 13px;
                  color: var(--muted); cursor: pointer; }
  .tabs2 button.tab-on { color: var(--text); border-bottom: 2px solid var(--accent); }
</style>

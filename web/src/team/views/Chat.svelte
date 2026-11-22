<!-- Chat.svelte — fully: rooms, DMs, unread, reply, edit/delete,
     reactions, upload/download, search, typing, mentions. The composer only
     answers with add_messages; read with view_messages. -->
<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { get } from "svelte/store";
  import { call } from "../../lib/api/client";
  import { currentProjectId, perms, live, route, me as meStore } from "../frame/shell_state";
  import { isLinkedApp } from "../../lib/admin_users";
  import { roomName, dmName, members, taskRefs, hueFor, drafts } from "../../lib/chat_logic";
  import { isSend } from "../../lib/prefs_logic";
  import { enterKey } from "../../lib/prefs";
  import { LABELS as PLABELS } from "../../lib/prefs_logic";
  import StatusIcon from "../../lib/parts/StatusIcon.svelte";
  import Md from "../../lib/Md.svelte";
  import AttachmentSlot from "./AttachmentSlot.svelte";
  import Avatar from "../../lib/parts/Avatar.svelte";
  import { Paperclip, Bold, Code, AtSign, Smile, Send } from "@lucide/svelte";

    type TaskShape = {
    id: number; number: number; title: string; status: string;
    status_type?: string; assignees?: { name: string }[]; due?: number; version: number;
  };
type Room = { id: number; name: string; kind: "project" | "room" | "dm";
                project?: number; unread?: number; mentions?: number;
                members?: string[] };
  type Msg = {
    id: number; person: { name: string } | string; body: string; ts: number;
    edited?: number | null; deleted?: boolean | null;
    reply_to?: number | null; room: number;
    file?: string | { id: string; name?: string; type?: string; size?: number } | null;
    reactions?: { emoji: string; n: number; by?: number[] }[];
    seen_by?: string[] };
  type Person = { id: number; name: string; display?: string; state: string };

  // -------- state -------------------------------------------------------
  let rooms = $state<Room[]>((() => {
    try { return JSON.parse(localStorage.getItem("aura.chat.rooms") ?? "[]"); }
    catch { return []; }
  })());
  let people = $state<Person[]>([]);
  let openRoom = $state<Room | null>(null);
  let history = $state<Msg[]>([]);
  // Synchronous first paint.
  if (typeof localStorage !== "undefined") {
    try {
      const lid = JSON.parse(localStorage.getItem("aura.chat.lastRoom") ?? "null");
      if (lid) {
        const cached = JSON.parse(localStorage.getItem(`aura.hist.${lid}`) ?? "null");
        if (cached?.length) history = cached;
      }
    } catch { /* corrupt cache just means empty */ }
  }
  let draft = $state("");
  let reply = $state<Msg | null>(null);
  let editing = $state<Msg | null>(null);
  let searchQ = $state("");
  let searchHits = $state<{ id: number; room: number; room_name: string;
                            person: string; body: string; ts: number }[]>([]);
  let loading = $state(true);
  let error = $state("");
  let late = $state(false);
  $effect(() => {                                  // 300 ms placeholder law
    if (!loading) { late = false; return; }
    const t = setTimeout(() => (late = true), 300);
    return () => clearTimeout(t);
  });
  let typingNames = $state<string[]>([]);
  let curPid = $state<number | undefined>(undefined);
  let curMeName = $state("");
  let typingTimer: ReturnType<typeof setTimeout> | null = null;
  let attachList: File[] = $state([]);
  let progBytes = $state<Record<string, number>>({});   // name → bytes sent
  let xhrs = $state<Record<string, XMLHttpRequest>>({});// name → live upload
  function acceptFiles(fs: FileList | File[] | null | undefined) {
    if (!fs) return;
    const merged = [...attachList];
    for (const f of Array.from(fs)) {
      if (f.size > 200 * 1024 * 1024) { uploadErr = `${f.name} is over 200 MB`; continue; }
      if (!merged.some((x) => x.name === f.name)) merged.push(f);
    }
    attachList = merged;
  }
  let uploadErr = $state("");
  function removeAttach(name: string) {
    xhrs[name]?.abort();               // mid-upload remove = abort; not posted
    delete xhrs[name]; delete progBytes[name];
    attachList = attachList.filter((f) => f.name !== name);
  }
  function uploadOne(f: File): Promise<string | null> {
    return new Promise((resolve) => {
      const attemptUpload = (triesLeft: number) => {
        const xhr = new XMLHttpRequest();
        xhrs = { ...xhrs, [f.name]: xhr };
        xhr.upload.onprogress = (e) => { progBytes = { ...progBytes, [f.name]: e.loaded }; };
        xhr.onload = () => {
          delete xhrs[f.name];
          if (xhr.status === 201 || xhr.status === 200) {
            resolve(JSON.parse(xhr.responseText).file.id);
          } else if (xhr.status === 429 && triesLeft > 0) {
            // rate window: obey and retry — a silent drop would post the
            // message WITHOUT its files (data loss disguised as success)
            const ra = Number(xhr.getResponseHeader("Retry-After"));
            setTimeout(() => attemptUpload(triesLeft - 1),
              Math.max(Number.isFinite(ra) ? ra * 1000 : 0, 15000));
          } else {
            uploadErr = `${f.name}: the upload didn't land (${xhr.status})`;
            resolve(null);
          }
        };
        xhr.onerror = () => {
          delete xhrs[f.name];
          if (triesLeft > 0) setTimeout(() => attemptUpload(triesLeft - 1), 3000);
          else resolve(null);
        };
        xhr.open("POST", "/api/v1/chat/upload");
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-File-Name", f.name);
        xhr.send(f);
      };
      attemptUpload(3);
    });
  }
  let mentionHint = $state<string[]>([]);

  const fType = (f: Msg["file"]) => (f && typeof f !== "string" ? f.type ?? "" : "");
  const fName = (f: Msg["file"]) => (f && typeof f !== "string" ? f.name ?? "the file" : "the file");
  const fId = (f: Msg["file"]) => (f && typeof f !== "string" ? f.id : typeof f === "string" ? f : "");

  // ---- The full-size picture viewer ----
  let lbRun = $state<Msg[]>([]);
  let lbIdx = $state(0);
  let lbZoom = $state(false);
  let lbOpen = $state(false);
  let lastTile: HTMLElement | undefined;

  function openLb(run: Msg[], idx: number, tile: HTMLElement) {
    lbRun = run; lbIdx = idx; lbZoom = false; lastTile = tile; lbOpen = true;
  }
  function closeLb() { lbOpen = false; lastTile?.focus(); }
  function lbMove(delta: number) { lbIdx = (lbIdx + delta + lbRun.length) % lbRun.length; }

  // ---- #N in a message renders that task's live card ----
  let taskIndex = $state<Map<number, TaskShape>>(new Map());

  async function refreshTaskIndex() {
    const pidv = get(currentProjectId);
    if (pidv === undefined) { taskIndex = new Map(); return; }
    try {
      const d = await call<{ tasks: TaskShape[] }>(`/tasks?project=${pidv}`);
      const m = new Map<number, TaskShape>();
      for (const t of d.tasks ?? []) m.set(t.number, t);
      taskIndex = m;
    } catch { /* the cards hide when the index can't load */ }
  }
  let showPeople = $state(false);
  let newRoomOpen = $state(false);
  let newRoomName = $state("");
  let newRoomInvite = $state("");
  let _debounce: ReturnType<typeof setTimeout> | null = null;
  let _ta: HTMLTextAreaElement | null = $state(null);
  let _msgEl: HTMLUListElement | null = $state(null);

  // the unread spine: last-seen boundary for this visit (visual only)
  let unreadSpineAt = $state<number | null>(null);

  const REACT_CHOICES = ["👍", "✓", "🎉", "❤️", "👀", "🤔"];

  // The perms' own Svelte store is the only thing Svelte can track — a shared field never
  // updates the component
  let permTicks = $state<Map<number, Set<string>>>(new Map());
  perms.state.subscribe((s) => { permTicks = s.map; });
  function timeStr(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString(undefined,
      { hour: "2-digit", minute: "2-digit" });
  }

  const canPost = $derived.by(() => {
    if (!openRoom) return false;
    if (openRoom.kind !== "project") return true;
    return curPid !== undefined && !!permTicks.get(curPid)?.has("add_messages");
  });
  const canMakeRoom = $derived.by(() => {
    if (curPid === undefined) return false;
    return !!permTicks.get(curPid)?.has("edit_project");
  });
  // Refinement: an open-but-backgrounded room defers its watermark until the tab is visible
  // again (mark-on-visible)
  let markPending = false;
  async function markReadWhenVisible() {
    if (document.visibilityState !== "visible") { markPending = true; return; }
    await markRead();
  }
  function unreadOf(r: Room): number { return r.unread ?? 0; }
  function mentionsOf(r: Room): number { return r.mentions ?? 0; }

  // The grid shows on the run's FIRST message; the others fold into it.
  const isPicMsg = (m: Msg) => !m.deleted && !!m.file &&
    (typeof m.file === "string" || fType(m.file).startsWith("image/"));
  const picRuns = $derived.by(() => {
    const runOf = new Map<number, Msg[]>();
    const folded = new Set<number>();
    let cur: Msg[] | null = null;
    for (const m of history) {
      const bare = isPicMsg(m) && !m.reply_to && !(m.body ?? "").trim();
      const last = cur?.[cur.length - 1];
      if (cur && last && bare && pName(last) === pName(m) && m.ts - last.ts <= 60 && cur.length < 12) {
        cur.push(m); folded.add(m.id); continue;
      }
      cur = isPicMsg(m) ? [m] : null;
      if (cur) runOf.set(m.id, cur);
    }
    return { runOf, folded };
  });
  function pName(m: Msg): string {
    return typeof m.person === "string" ? m.person : m.person.name;
  }
  // The composer's markdown toolbar.
  function fmt(kind: string) {
    if (!_ta) return;
    const s = _ta.selectionStart ?? 0, e = _ta.selectionEnd ?? 0;
    const sel = draft.slice(s, e);
    let ins = "";
    if (kind === "b") ins = `**${sel || "bold"}**`;
    else if (kind === "i") ins = `*${sel || "italic"}*`;
    else if (kind === "s") ins = `~~${sel || "strike"}~~`;
    else if (kind === "code") ins = "`" + (sel || "code") + "`";
    else if (kind === "block") ins = "```\n" + sel + "\n```";
    else if (kind === "link") ins = `[${sel || "text"}](url)`;
    else if (kind === "list") ins = sel ? sel.split("\n").map((l) => "- " + l).join("\n") : "- ";
    else if (kind === "emoji") ins = "👍";
    draft = draft.slice(0, s) + ins + draft.slice(e);
    queueMicrotask(() => {
      _ta?.focus();
      const p = s + ins.length;
      _ta?.setSelectionRange(p, p);
    });
  }
  // "@" at the cursor, then the mention list opens (the same path as typing it)
  function startMention() {
    if (!_ta) return;
    const s = _ta.selectionStart ?? draft.length, e = _ta.selectionEnd ?? s;
    const before = draft.slice(0, s);
    const ins = before && !/\s$/.test(before) ? " @" : "@";
    draft = before + ins + draft.slice(e);
    queueMicrotask(() => {
      if (!_ta) return;
      _ta.focus();
      const p = s + ins.length;
      _ta.setSelectionRange(p, p);
      onDraftInput();
    });
  }
  // Today / Yesterday / the date, for the day divider
  function dayLabel(ts: number): string {
    const d = new Date(ts * 1000), now = new Date();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === now.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  }
  const firstMentionId = $derived(history.find((m) => !m.deleted && isMention(m.body))?.id);
  function mineOf(m: Msg): boolean { return pName(m) === curMeName; }
  function isMention(body: string): boolean {
    if (!curMeName) return false;
    const word = curMeName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp("@" + word + "(?!\\w)", "i").test(body);
  }
  function lastRead(): number {
    return history.length ? history[history.length - 1].id : 0;
  }

  async function markRead() {
    if (!openRoom) return;
    const lid = lastRead();
    if (!lid) return;
    try { await call("/chat/read", { room: openRoom.id, last: lid }, "POST");
    } catch { /* the answer-landing is quiet on failure */ }
    rooms = rooms.map((r) => r.id === openRoom!.id
      ? { ...r, unread: 0, mentions: 0 } : r);
  }

  let historyEpoch = 0;

  function stashDraft() {
    if (openRoom) drafts.set(openRoom.id, draft);
  }
  async function pickRoom(r: Room, mark = true) {
    stashDraft();
    draft = drafts.get(r.id) ?? "";
    if (!r.name) return;
    const epoch = ++historyEpoch;
    openRoom = r;
    localStorage.setItem("aura.chat.lastRoom", String(r.id));
    loading = true;
    try {
      const d = await call<{ msgs: Msg[] }>(
        `/chat/history?room=${r.id}&limit=80`);
      if (epoch !== historyEpoch || openRoom?.id !== r.id) return;
      history = d.msgs;
      localStorage.setItem(`aura.hist.${r.id}`, JSON.stringify(d.msgs).slice(0, 200_000));
      loading = false;
      // the spine sits where the room's own unread count points — visual only
      const u = unreadOf(r);
      unreadSpineAt = u > 0 && history.length >= u
        ? history[history.length - u]?.id ?? null : null;
    } catch (e) {
      if (epoch !== historyEpoch || openRoom?.id !== r.id) return;
      error = e instanceof Error ? e.message : "no history";
    } finally {
      if (epoch === historyEpoch && openRoom?.id === r.id) loading = false;
    }
    if (epoch !== historyEpoch || openRoom?.id !== r.id) return;
    if (mark) {
      void markReadWhenVisible();
      setTimeout(() => _msgEl?.scrollTo({ top: _msgEl.scrollHeight }), 20);
    }
  }

  async function loadRooms(autoPick = true) {
    try {
      const d = await call<{ rooms: Room[] }>("/chat/rooms");
      rooms = d.rooms;
      if (autoPick && !openRoom && rooms.length) {
        const proj = rooms.find((r) => r.kind === "project"
                                     && r.project === curPid) ?? rooms[0];
        if (proj) await pickRoom(proj);
      }
    } catch (e) {
      error = e instanceof Error ? e.message : "the hub didn't answer";
      loading = false;
    }
  }

  async function loadPeople() {
    try {
      const d = await call<{ people: Person[] }>("/presence");
      people = (d.people ?? []).filter((p) => !isLinkedApp(p));
    } catch { people = []; }
  }

  // -------- actions ------------------------------------------------------
  async function post(e?: Event) {
    e?.preventDefault();
    if (!canPost || !openRoom) return;
    const text = draft.trim();
    if (!text && attachList.length === 0) return;
    const files = [...attachList];        // the lane and the box empty up-front
    attachList = []; uploadErr = "";
    draft = ""; drafts.set(openRoom.id, "");
    let captionUsed = false;
    for (const f of files) {
      const id = await uploadOne(f);      // ordered: one at a time
      if (!id) continue;
      const body: Record<string, unknown> = { room: openRoom.id, file: id };
      if (text && !captionUsed) { body.body = text; captionUsed = true; }
      if (reply) { body.reply_to = reply.id; }
      const sent = await call<{ msg: Msg }>("/chat/post", body, "POST");
      if (sent?.msg && sent.msg.room === openRoom.id
          && !history.some((x) => x.id === sent.msg.id))  // the live echo dupes the optimistic append
        history = [...history, sent.msg];
    }
    if (!captionUsed && text) {           // text-only messages still post
      const body: Record<string, unknown> = { room: openRoom.id, body: text };
      if (reply) body.reply_to = reply.id;
      const sent = await call<{ msg: Msg }>("/chat/post", body, "POST");
      if (sent?.msg && sent.msg.room === openRoom.id
          && !history.some((x) => x.id === sent.msg.id))
        history = [...history, sent.msg];
    }
    reply = null;
    if (_ta) _ta.style.height = "auto";
    void pickRoom(openRoom);
  }

  function onDraftInput(_ev?: Event) {
    // typing candle — the server's own window holds the 3s throttle too
    if (!typingTimer && openRoom) {
      typingTimer = setTimeout(() => { typingTimer = null; }, 2400);
      void call("/chat/typing", { room: openRoom.id }, "POST")
        .catch(() => { /* typing never breaks the page */ });
    }
    // composer auto-grow 44 → 192 px, then internal scroll (research spec)
    if (_ta) {
      _ta.style.height = "auto";
      _ta.style.height = Math.min(_ta.scrollHeight, 192) + "px";
    }
    // @mention answers after the caret's own last '@…' tail
    const ta = _ta!;
    const val = ta.value ?? "";
    const tail = val.slice(0, ta.selectionStart ?? val.length);
    const m = tail.match(/@([a-z0-9_ -]{0,38})$/i);
    mentionHint = m
      ? (openRoom?.members ?? []).filter((n) =>
          n.toLowerCase().startsWith(m[1].toLowerCase()))
      : [];
  }
  function acceptMention(name: string) {
    if (!_ta) return;
    const pos = _ta.selectionStart ?? _ta.value.length;
    const val = _ta.value;
    const before = val.slice(0, pos);
    const m = before.match(/@([a-z0-9_ -]{0,38})$/i)!;
    draft = before.slice(0, m.index) + "@" + name + " "
          + val.slice(pos);
    mentionHint = [];
    _ta.focus();
  }
  function acceptFirst() {
    if (mentionHint.length) acceptMention(mentionHint[0]!);
  }

  async function startEdit(m: Msg) {
    editing = m; reply = null; draft = m.body;
    queueMicrotask(() => _ta?.focus());
  }
  async function saveEdit() {
    if (!editing) return;
    const text = draft.trim();
    if (!text) { editing = null; return; }
    const mid = editing.id;
    editing = null; draft = "";
    await call("/chat/edit", { msg: mid, body: text }, "POST");
    void pickRoom(openRoom!);
  }
  async function trash(m: Msg) {
    if (!confirm("delete this message? (it answers \"this message was deleted\")"))
      return;
    await call("/chat/delete", { msg: m.id }, "POST");
    void pickRoom(openRoom!);
  }
  async function react(m: Msg, emoji: string) {
    const mine = ((m.reactions ?? []) as { emoji: string; by?: number[] }[])
      .some((r) => r.emoji === emoji
        && (r.by ?? []).includes($meStore.value?.id ?? -1));
    await call("/chat/react", { msg: m.id, emoji, on: !mine }, "POST");
    void pickRoom(openRoom!);
  }

  async function dm(name: string) {
    const r = await call<{ room: { id: number; kind: string; name: string } }>(
      "/chat/dm", { with: name }, "POST").catch(() => null);
    if (!r) return;
    await loadRooms();
    const mine = rooms.find((x) => x.id === r.room.id);
    if (mine) await pickRoom(mine);
  }

  async function newRoomSave() {
    if (!newRoomName.trim()) return;
    const invite = newRoomInvite.split(/[\s,]+/).filter(Boolean);
    const r = await call<{ room: { id: number } }>("/chat/room",
      { name: newRoomName.trim(), invite }, "POST");
    newRoomOpen = false; newRoomName = ""; newRoomInvite = "";
    await loadRooms();
    const mine = rooms.find((x) => x.id === r.room.id);
    if (mine) await pickRoom(mine);
  }

  // ---- Search ----
  async function search() {
    const q = searchQ.trim();
    if (!q) { searchHits = []; return; }
    try {
      const d = await call<{ hits: typeof searchHits }>(
        `/chat/search?q=${encodeURIComponent(q)}`);
      searchHits = d.hits;
    } catch { searchHits = []; }
  }
  function jump(h: { room: number }) {
    const r = rooms.find((x) => x.id === h.room);
    if (r) void pickRoom(r);
    searchHits = []; searchQ = "";
  }

  // -------- mounts ---------------------------------------------------------
  onMount(() => {
    // The mark-on-visible half of a deferred watermark fires when the tab returns; each
    // visible-return also refreshes the open room's stamp
    const onVis = () => {
      if (document.visibilityState !== "visible" || !openRoom) return;
      markPending = false;
      void markRead();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  });
  onMount(() => currentProjectId.subscribe((v: number | undefined) => {
    if (v !== undefined && v !== curPid) {
      curPid = v; void loadRooms(); void loadPeople(); void refreshTaskIndex();
    }
  }));
  onMount(() => {
    void loadPeople();
    void call<{ person: { name: string } }>("/me").then((d) => {
      curMeName = d.person?.name ?? "";
    }).catch(() => {});
  });

  onMount(() => live.subscribe((ev) => {
    if (!ev) return;
    const d = (ev.data ?? {}) as Record<string, unknown>;
    if (ev.kind === "presence" || ev.kind === "people") {
      if (d["state"] && d["name"]) loadPeople();
      return;
    }
    if (ev.kind === "task" || ev.kind === "log") { void refreshTaskIndex(); }
    if (ev.kind === "typing") {
      // typing rides its own event kind (NOT "chat"),
      const roomId = d["room"] as number | null | undefined;
      const who = d["person"] as string | undefined;
      if (roomId === openRoom?.id && who) {
        if (!typingNames.includes(who)) typingNames = [...typingNames, who];
        // typing answers die on their own (the answer's cloud keeps its own window)
        setTimeout(() => { typingNames = typingNames.filter((n) => n !== who); },
                   4000);
      }
      return;
    }
    if (ev.kind === "read") {
      // Seen-by is read-side state: refetch messages/rooms, but never answer
      // this event with another watermark write or force another auto-open.
      void loadRooms(false);
      if (d["room"] === openRoom?.id) void pickRoom(openRoom!, false);
      return;
    }
    if (ev.kind !== "chat") return;
    if ((d["room"] ?? null) !== openRoom?.id) {
      // the answer's the room itself (its own unread answers land next
      // reload of the rooms list too — keep the badge updated early)
      void loadRooms();
      return;
    }
    if (d["kind"] === "typing") {
      const who = d["person"] as string | undefined;
      if (who) typingNames = [...new Set([...typingNames, who])];
      return;
    }
    void pickRoom(openRoom!);      // answer: the room's own answers back
    void loadRooms();
  }));

  onDestroy(() => {
    if (_debounce) clearTimeout(_debounce);
    if (typingTimer) clearTimeout(typingTimer);
  });
</script>

<svelte:head><title>Chat · Aura Teams</title></svelte:head>

<div class="wrap">
  <aside class="rooms" data-kit="chat.rooms" aria-label="the rooms">
    <div class="hd">
      <h4>rooms</h4>
      {#if canMakeRoom}
        <button class="ghost" aria-label="new room" title="make a room"
                onclick={() => newRoomOpen = true}>＋</button>
      {/if}
    </div>
    {#each rooms as r}
                    <!-- accessible name = visible text ("#web") — no synthetic label -->
      <button class="room" class:unread={!!unreadOf(r)} class:on={openRoom?.id === r.id}
              data-kit={unreadOf(r) ? "chat.room-unread" : openRoom?.id === r.id ? "chat.room-on" : "chat.room"}
              onclick={() => void pickRoom(r)}
              aria-label={`${r.kind === "project" ? "#" : r.kind === "dm" ? "@" : "▸ "}${r.name}`}>
        {#if r.kind === "project"}#{roomName(r.name)}{:else if r.kind === "dm"}@{dmName(r.name, curMeName)}{:else}▸ {roomName(r.name)}{/if}
        {#if unreadOf(r)}<span class="badge unread" data-kit="chat.badge">{unreadOf(r)}</span>{/if}
        {#if mentionsOf(r)}<span class="badge warn">{mentionsOf(r)}@</span>{/if}
      </button>
    {:else}
      <span class="muted">{loading ? "…" : "no rooms yet"}</span>
    {/each}
    <div class="people-lane">
      <h4>people
        <button class="ghost" aria-label="toggle people list"
                onclick={() => (showPeople = !showPeople)}>{showPeople ? "▾" : "▸"}</button>
      </h4>
      {#if showPeople}
        {#each people as p}
          <button class="personbtn" onclick={() => { if (p.name !== curMeName) void dm(p.name); }}
                  disabled={p.name === curMeName}
                  title={p.name === curMeName ? `${p.name} (you)` : `message ${p.name}`}>
            <span class="dot" class:on={p.state === "online"}
                  class:away={p.state === "away"}></span>
            {p.name}
          </button>
        {/each}
      {/if}
    </div>
  </aside>

  <div class="main conv">
    {#if newRoomOpen}
      <div class="card newroom" role="dialog" aria-modal="true" aria-label="new room">
        <h4>new room</h4>
        <label>name<input bind:value={newRoomName} /></label>
        <label>invite (names, commas or spaces; leave blank for you alone)
          <input bind:value={newRoomInvite} /></label>
        <div class="rowgap">
          <button class="btn primary" onclick={() => void newRoomSave()}>make the room</button>
          <button class="btn" onclick={() => newRoomOpen = false}>never mind</button>
        </div>
      </div>
    {/if}

    {#if loading && !openRoom}
      <p class="muted">loading…</p>
    {:else if error}
      <div class="err card" role="alert">
        <p>{error}</p>
        <button class="btn" onclick={() => openRoom ? void pickRoom(openRoom) : void loadRooms()}>
          Try again</button>
      </div>
    {:else if !openRoom}
      <p class="muted">pick a room, a person, or make one.</p>
    {:else}
      <div class="roomhead conv-h" data-kit="chat.head">
        <h3 class="roomname nm" data-kit="chat.room-name">
          {openRoom.kind === "dm" ? "@" + dmName(openRoom.name, curMeName) : "#" + roomName(openRoom.name)}
        </h3>
        {#if openRoom.members?.length}
          <span class="rcount">{members(openRoom.members.length)}</span>
        {/if}
        <div class="search">
          <input bind:value={searchQ} placeholder="Search this chat"
                 oninput={() => {
                   if (_debounce) clearTimeout(_debounce);
                   _debounce = setTimeout(() => void search(), 180);
                 }} />
          {#if searchHits.length}
            <ul class="hits card" role="listbox" aria-label="search results">
              {#each searchHits as h}
                <li><button class="hit" onclick={() => jump(h)}>
                  <b>{h.person}</b> in #{roomName(h.room_name)} — {h.body}
                  <time class="muted">{timeStr(h.ts)}</time>
                </button></li>
              {/each}
            </ul>
          {/if}
        </div>
      </div>
      {#if typingNames.length}
        <div class="busy" aria-live="polite">
          <span class="tdots" aria-hidden="true"><i></i><i></i><i></i></span>
          {typingNames.join(", ")} typing…
        </div>
      {/if}
      <ul class="msgs" role="log" aria-live="polite" bind:this={_msgEl}>
        {#each history as m, i (m.id)}
          {#if !picRuns.folded.has(m.id)}
          {@const dayCross = i === 0 ||
            (new Date(m.ts * 1000).toDateString() !==
             new Date(history[i - 1].ts * 1000).toDateString())}
          {@const firstUnread = unreadSpineAt === m.id}
          {#if dayCross}<li class="day" data-kit="chat.day">{dayLabel(m.ts)}</li>{/if}
          {#if firstUnread}<li class="newline" data-kit="chat.new-line">NEW</li>{/if}
          <li class="msg" class:deleted={m.deleted} class:edited={m.edited}
                 class:me-mentioned={isMention(m.body)}
                 data-kit={m.id === firstMentionId ? "chat.mentioned" : undefined}
              class:same={i > 0 && pName(history[i - 1]) === pName(m) && !m.deleted && !dayCross && !firstUnread}>
            <span class="avslot" aria-hidden="true"><Avatar name={pName(m)} size="md" /></span>
            <div class="block">
              <div class="meta">
                <span class="who"><b data-kit="chat.who">{pName(m)}</b></span>
                <time class="muted" data-kit="chat.time">{timeStr(m.ts)}</time>
                {#if m.edited}<span class="muted edited">(edited)</span>{/if}
                                {#if reply?.id === m.id}<span class="muted">you're answering this</span>{/if}
                {#if mineOf(m) && m.seen_by?.length}
                  <span class="muted seen" title={`read by ${m.seen_by.join(", ")}`}>
                    seen by {m.seen_by.join(", ")}</span>
                {/if}
              </div>
              {#if m.reply_to}
                {#each history.filter((x) => x.id === m.reply_to) as orig}
                  <div class="replyquote muted">“{orig.body.slice(0, 80)}” — {pName(orig)}</div>
                {/each}
              {/if}
              <div class="body">
                {#if m.deleted}
                  <span class="gone">this message was deleted</span>
                {:else}
                  <p data-kit="chat.text"><Md text={m.body} /></p>
                {/if}
              </div>
              {#if !m.deleted}
                {#each taskRefs(m.body).slice(0, 3) as ref}
                  {@const t = taskIndex.get(ref)}
                  {#if t}
                    <div class="acard" data-kit="chat.task-card" data-task-number={t.number}>
                      <div class="top">
                        <StatusIcon type={t.status_type ?? t.status} />
                        <span>#{t.number} · {t.title}</span>
                        {#if (taskIndex.get(t.number)?.status_type ?? t.status) === "in_progress"}
                          <span class="pill work">working</span>
                        {:else if (t.status ?? "") === "blocked"}
                          <span class="pill wait">waiting</span>
                        {/if}
                      </div>
                      <div class="rows">
                        <div><span class="rk">assignees</span> {(t.assignees ?? []).map((a) => a.name).join(", ") || "—"}</div>
                        {#if t.due}<div><span class="rk">due</span> {new Date(t.due * 1000).toLocaleDateString()}</div>{/if}
                      </div>
                      <div class="foot">
                        <button class="btn" onclick={() => route.go("board", String(t.number))}>Open task</button>
                      </div>
                    </div>
                  {/if}
                {/each}
              {/if}
              {#if m.file && !m.deleted}
                {#if picRuns.runOf.get(m.id)}
                  {@const run = picRuns.runOf.get(m.id) ?? []}
                  <div class={`imgs n${Math.min(run.length, 4)}`} data-kit="chat.images">
                    {#each run.slice(0, 4) as pic, pi (pic.id)}
                      <button class="btn-none tilebtn" onclick={(e: Event) => {
                      const idx = run.findIndex((x) => x.id === pic.id);
                      openLb(run, idx, e.currentTarget as HTMLElement);
                    }}>
                      <img class="tile" data-kit="chat.image" src={`/api/v1/chat/file/${fId(pic.file)}`} alt={`Picture ${run.indexOf(pic) + 1} of ${run.length} — ${fName(pic.file)}`} loading="lazy" />
                    </button>
                    {#if pi === 3 && run.length > 4}
                      <button class="btn-none tilebtn moretile" onclick={(e: Event) => {
                      const idx = 4;
                      openLb(run, idx, e.currentTarget as HTMLElement);
                    }}>
                        <span class="more">+{run.length - 4}</span>
                      </button>
                    {/if}
                    {/each}
                  </div>
                {:else if m.file}
                  <AttachmentSlot fileId={m.file} />
                {/if}
              {/if}
              {#if m.reactions?.length}
                <div class="reacts" data-kit="chat.reaction-list">
                  {#each m.reactions as rec}
                    <button class="react" class:me={(rec.by ?? []).includes(($meStore.value?.id ?? -1))}
                            data-kit={((rec.by ?? []).includes(($meStore.value?.id ?? -1))) ? "chat.reaction-mine" : "chat.reaction"}
                            aria-label={`${rec.emoji} ${rec.n}`}
                            onclick={() => void react(m, rec.emoji)}>
                      {rec.emoji} {rec.n}</button>
                  {/each}
                </div>
              {/if}
              {#if !m.deleted}
                <span class="acts" role="group" aria-label="message actions">
                  <button class="ghost" onclick={() => { reply = m; editing = null; }}
                          title="reply">↩</button>
                  {#each REACT_CHOICES as e}
                    <button class="ghost" onclick={() => void react(m, e)}
                            title={`react ${e}`}>{e}</button>
                  {/each}
                  {#if mineOf(m)}
                    <button class="ghost" onclick={() => void startEdit(m)}
                            title="edit">✎</button>
                    <button class="ghost" onclick={() => void trash(m)}
                            title="delete">🗑</button>
                  {/if}
                </span>
              {/if}
            </div>
          </li>
          {/if}
        {/each}
        {#if loading}
          {#if late}<li class="muted">loading…</li>{/if}
        {:else if history.length === 0}
          <li class="empty muted" data-kit="chat.empty">No messages yet — say hello.</li>
        {/if}
      </ul>

      {#if canPost}
        <form class="composer" data-kit="chat.composer" onsubmit={(e) => {
                e.preventDefault();
                if (editing) void saveEdit();
                else { acceptFirst(); void post(); } }}>
          {#if reply}<div class="replybar">answering <b>{pName(reply)}</b>
            <button class="ghost" onclick={() => { reply = null; }}>×</button></div>{/if}
          {#if editing}<div class="replybar">editing your own message
            <button class="ghost" onclick={() => { editing = null; draft = ""; }}>×</button></div>{/if}
          {#if uploadErr}<div class="replybar errline">{uploadErr}</div>{/if}
          <div class="lane" data-kit="chat.lane">
            {#each attachList as f (f.name)}
              <div class="att">
                {#if f.type.startsWith("image/")}
                  <img class="att-thumb" src={URL.createObjectURL(f)} alt="" />
                {:else}
                  <span class="att-ico">▤</span>
                {/if}
                <span class="att-nm">{f.name}<i class="att-size">{Math.round(f.size / 1024)} KB</i></span>
                <button class="x2" aria-label={`remove ${f.name}`} type="button"
                        onclick={() => removeAttach(f.name)}>×</button>
                {#if progBytes[f.name] !== undefined && xhrs[f.name]}
                  <span class="prog"><i style:width={`${Math.round(((progBytes[f.name] || 0) / f.size) * 100)}%`}></i></span>
                {/if}
              </div>
            {/each}
          </div>
          <div class="cwrap">
            <textarea data-kit="chat.composer-text" bind:value={draft} bind:this={_ta} rows="1"
                      oninput={() => void onDraftInput()}
                      onkeydown={(e: KeyboardEvent) => {
                        if (!isSend(e, $enterKey)) return;
                        if (mentionHint.length) { e.preventDefault(); acceptMention(mentionHint[0]); return; }
                        e.preventDefault();
                        if (editing) void saveEdit();
                        else { acceptFirst(); void post(); }
                      }}
                      ondragover={(e: DragEvent) => e.preventDefault()}
                      ondrop={(e: DragEvent) => {
                        e.preventDefault();
                        acceptFiles(e.dataTransfer?.files ?? null);   // every dropped file
                      }}
                      placeholder={`Message ${openRoom.kind === "dm" ? dmName(openRoom.name, curMeName) : "#" + roomName(openRoom.name)} — @ to mention`}></textarea>
            {#if mentionHint.length}
              <div class="menu mentions" role="listbox" aria-label="people to mention">
                {#each mentionHint as n}
                  <button type="button" class="mi" role="option" aria-selected={false}
                          onclick={() => acceptMention(n)}>
                    <Avatar name={n} />{n}
                  </button>
                {/each}
              </div>
            {/if}
          </div>
          <div class="cbar">
            <label class="ib" aria-label="Attach files" title="Attach files (up to 200 MB each)">
              <Paperclip size={16} strokeWidth={1.75} />
              <input type="file" multiple class="visually-hidden" oninput={(e: Event) => {
                const inp = e.currentTarget as HTMLInputElement;
                acceptFiles(inp.files);
                inp.value = "";
              }} />
            </label>
            <button type="button" class="ib" aria-label="Bold" onclick={() => fmt("b")}><Bold size={16} strokeWidth={1.75} /></button>
            <button type="button" class="ib" aria-label="Code" onclick={() => fmt("code")}><Code size={16} strokeWidth={1.75} /></button>
            <button type="button" class="ib" aria-label="Mention" onclick={startMention}><AtSign size={16} strokeWidth={1.75} /></button>
            <button type="button" class="ib" aria-label="Emoji" onclick={() => fmt("emoji")}><Smile size={16} strokeWidth={1.75} /></button>
            <span class="hint" data-kit="chat.hint">{PLABELS.enter[$enterKey]}</span>
            <button class="send" type="submit" data-kit="chat.send" aria-label={editing ? "Save edit" : "Send"}
                    disabled={!draft.trim() && attachList.length === 0}><Send size={14} strokeWidth={1.75} /></button>
          </div>
        </form>
      {:else}
        <p class="muted">you watch here — the room's role names who writes.</p>
      {/if}
    {/if}
  </div>
</div>

<svelte:window onkeydown={(e: KeyboardEvent) => {
  if (!lbOpen) return;
  if (e.key === "Escape") closeLb();
  else if (e.key === "ArrowRight") lbMove(1);
  else if (e.key === "ArrowLeft") lbMove(-1);
}} />

{#if lbOpen && lbRun[lbIdx]?.file && typeof lbRun[lbIdx].file !== "string"}
  <div class="lb open" data-kit="lightbox.backdrop"
       role="dialog" aria-label="the picture viewer">
    <div class="lb-top">
      <b>{pName(lbRun[lbIdx])}</b>
      <span class="muted">#{roomName(openRoom?.name ?? "chat")} · {timeStr(lbRun[lbIdx].ts)} · picture {lbIdx + 1} of {lbRun.length}</span>
      <span class="lb-acts">
        <button class="ghost" onclick={() => (lbZoom = !lbZoom)} title="zoom">zoom</button>
        <a class="ghost" href={`/api/v1/chat/file/${fId(lbRun[lbIdx].file)}`} download>download</a>
        <a class="ghost" href={`/api/v1/chat/file/${fId(lbRun[lbIdx].file)}`} target="_blank" rel="noreferrer">open original</a>
        <button class="ghost" onclick={closeLb} aria-label="close the viewer">×</button>
      </span>
    </div>
    <div class="lb-stage">
      {#if lbRun.length > 1}
        <button class="lb-nav l" aria-label="previous" onclick={() => lbMove(-1)}>‹</button>
      {/if}
      <img data-kit="lightbox.image" class={{ big: lbZoom } as Record<string, boolean>}
           src={`/api/v1/chat/file/${fId(lbRun[lbIdx].file)}`}
           alt={`picture ${lbIdx + 1} of ${lbRun.length}`} />
              <button class="lb-nav r" data-kit="lightbox.nav" aria-label="next" onclick={() => lbMove(1)}>›</button>
    </div>
    <div class="lb-strip">
      {#each lbRun as pic, ix (pic.id)}
        <button class="stripthumb" class:on={ix === lbIdx}
                data-kit={ix === 0 ? "lightbox.thumb" : ""}
                onclick={() => (lbIdx = ix)}>
          <img data-kit={ix === 0 ? "lightbox.thumb" : undefined} src={`/api/v1/chat/file/${fId(pic.file)}`} alt="" />
        </button>
      {/each}
    </div>
  </div>
{/if}

<style>
  /* ===== Chat skin. Full-bleed two-pane; messages scroll in their own lane; the composer is
   * pinned at the bottom; actions reveal on hover. No behavior. =====
   */

  .wrap {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    height: 100%;
    min-height: 0;
  }

  /* ---- rooms rail: surface-1 step, hairline divider, quiet rows ---- */
  .rooms {
    border-radius: 0;
    border-top: none; border-bottom: none; border-left: none;
    border-right: 1px solid var(--line);
    background: color-mix(in oklab, var(--bg), var(--raised) 55%);
    display: flex; flex-direction: column; gap: 2px;
    padding: var(--sp-12) var(--sp-8);
    overflow-y: auto;
  }
  .hd { display: flex; align-items: center; justify-content: space-between; }
  /* rows keep their height and the list scrolls, however many rooms */
  .rooms > * { flex: none; }
  .hd h4, .people-lane h4 {
    margin: 0; font-size: 10.5px; font-weight: 650;
    letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted);
  }
  .hd .ghost { height: 22px; min-width: 22px; padding: 0 4px; }

  .lb { position: fixed; inset: 0; z-index: 60; display: flex; flex-direction: column;
        background: rgb(5 6 8 / 0.88); }
  .lb-top { display: flex; align-items: center; gap: 10px; height: 52px; padding: 0 16px;
            color: var(--text); }
  .lb-acts { margin-left: auto; display: flex; gap: 6px; }
  .lb-stage { flex: 1; display: grid; place-items: center; position: relative; min-height: 0; }
  .lb-stage img { max-width: 88vw; max-height: 72vh; border-radius: 10px; }
  .lb-stage img.big { transform: scale(1.6); }
  .lb-nav { position: absolute; top: 50%; transform: translateY(-50%); width: 44px; height: 44px;
            border-radius: 50%; border: none; background: rgb(255 255 255 / 0.12);
            color: white; font-size: 20px; cursor: pointer; }
  .lb-nav.l { left: 24px; } .lb-nav.r { right: 24px; }
  .lb-strip { display: flex; justify-content: center; gap: 8px; padding: 14px; }
  .stripthumb { width: 64px; height: 48px; padding: 0; border: none; background: none;
                opacity: 0.5; cursor: pointer; }
  .stripthumb.on { opacity: 1; outline: 2px solid white; }
  .stripthumb img { width: 64px; height: 48px; object-fit: cover; border-radius: 6px; }
  .room .badge.warn { background: var(--warn); color: var(--bg); }

  .people-lane { margin-top: var(--sp-12); border-top: 1px solid var(--line); padding-top: var(--sp-8); }
  .people-lane h4 { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2px; }
  .personbtn {
    display: flex; align-items: center; gap: 7px;
    width: 100%; padding: 4px 8px; margin: 1px 0;
    background: transparent; border: none; border-radius: 6px;
    color: var(--text-2); font: inherit; font-size: 13px;
    text-align: left; cursor: pointer; min-height: 26px;
  }
  .personbtn:hover:not(:disabled) { background: var(--accent-dim); color: var(--text); }
  .personbtn:disabled { opacity: 0.55; cursor: default; }
  .dot {
    width: 8px; height: 8px; border-radius: 50%; flex: none;
    background: var(--line-strong);
  }
  .dot.on { background: var(--ok); }
  .dot.away { background: var(--warn); }

  /* ---- main pane: header / scrolling log / pinned composer ---- */
  .main {
    display: flex; flex-direction: column;
    min-width: 0; min-height: 0;
    padding: 0;
    overflow: hidden;
  }
  .roomhead {
    flex: none;
    display: flex; align-items: center; gap: var(--sp-16);
    padding: var(--sp-8) var(--sp-16);
    border-bottom: 1px solid var(--line);
  }
  .roomname {
    margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.01em;
    color: var(--text);
  }
  .busy {
    padding: 2px var(--sp-16); font-size: 12px; flex: none;
    color: var(--accent-text); font-style: italic;
    display: flex; align-items: center; gap: 8px;
  }
  .tdots { display: inline-flex; gap: 3px; }
  .tdots i {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--accent-text);
    animation: tdance 1.2s infinite;
  }
  .tdots i:nth-child(2) { animation-delay: 0.15s; }
  .tdots i:nth-child(3) { animation-delay: 0.3s; }
  @keyframes tdance {
    0%, 60%, 100% { opacity: 0.25; transform: none; }
    30% { opacity: 1; transform: translateY(-2px); }
  }
  .rcount { font-size: 12px; color: var(--muted); }

  /* Composer toolbar + attach pill + kbd chips -- */

  .msgs {
    flex: 1; min-height: 0; overflow-y: auto;
    list-style: none; margin: 0;
    display: flex; flex-direction: column;
  }
  .msg {
    position: relative;
    display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 10px;
    padding: 3px 8px 3px 10px; border-radius: 8px;
    margin-top: 10px;
    transition: background var(--motion);
  }
  .msg.same { margin-top: 0; }
  .msg:hover, .msg:focus-within { background: color-mix(in oklab, var(--bg), var(--raised-2) 40%); }
  .avslot { align-self: start; margin-top: 2px; display: flex; }
  .msg.same .avslot { visibility: hidden; height: 1px; }
  .msg.same .meta { display: none; }
  .block { min-width: 0; }
  .meta {
    display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
    margin-bottom: 1px;
  }
  .meta b { font-size: 13px; font-weight: 600; }
  .meta time { font-size: 11.5px; color: var(--muted); }
  .edited { font-size: 10.5px; }
  .seen { font-size: 10.5px; margin-left: auto; }
  .replyquote {
    font-size: 12px; padding: 2px 8px; margin: 2px 0 3px;
    border-left: 2px solid var(--line-strong);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .body { font-size: 13.5px; line-height: 1.55; overflow-wrap: anywhere; }
  .body :global(p) { margin: 0; }
  .gone { color: var(--muted); font-style: italic; }
  .msg.deleted .body { opacity: 0.6; }



  /* hover reveals the action cluster — 'hover whispers', nothing permanent */
  .acts {
    position: absolute; right: 10px; bottom: -11px;
    display: flex; gap: 2px;
    background: var(--raised-2); border: 1px solid var(--line-strong);
    border-radius: 7px; padding: 2px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    opacity: 0; pointer-events: none;
    transition: opacity var(--motion);
    z-index: 2;
  }
  .msg:hover .acts, .msg:focus-within .acts { opacity: 1; pointer-events: auto; }
  .acts .ghost { height: 24px; min-width: 24px; padding: 0 5px; border: none; font-size: 12px; }

  /* ---- composer: pinned bottom card, focus-within teal halo ---- */
  .cwrap { position: relative; }
  .mentions { top: auto; bottom: calc(100% + 4px); left: 8px; }

  .visually-hidden {
    position: absolute; width: 1px; height: 1px;
    clip-path: inset(50%); overflow: hidden; white-space: nowrap;
  }

  .replybar {
    flex: none;
    display: flex; align-items: center; gap: 6px;
    margin: 8px 12px 0;
    padding: 4px 10px; font-size: 12px;
    background: var(--raised); border: 1px solid var(--line);
    border-radius: 6px; color: var(--text-2);
  }
  .replybar.errline { color: var(--err); border-color: var(--err); }
  .replybar .ghost { margin-left: auto; height: 20px; min-width: 20px; padding: 0 4px; border: none; }
  .rowgap { display: flex; gap: var(--sp-8); }

  /* ---- search: lives in the room header (top), results drop downward ---- */
  .search {
    position: relative;
    margin: 0 0 0 auto; width: 300px; flex: none;
  }
  .search input {
    width: 100%; height: 30px; padding: 0 var(--sp-8);
    background: var(--raised); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px;
    font: inherit; font-size: 12.5px;
  }
  .search input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--ring); }
  .hits {
    position: absolute; bottom: calc(100% + 4px); left: 0; right: 0;
    max-height: 240px; overflow-y: auto;
    list-style: none; margin: 0; padding: 4px;
    background: var(--raised-2);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }
  .hit {
    display: block; width: 100%; text-align: left;
    background: none; border: none; border-radius: 5px;
    padding: 6px 8px; font: inherit; font-size: 12.5px;
    color: var(--text-2); cursor: pointer;
  }
  .hit:hover { background: var(--accent-dim); color: var(--text); }
  .hit b { color: var(--accent-text); }
  .hit time { float: right; }

  /* ---- misc ---- */
  .newroom { max-width: 420px; margin: var(--sp-24) auto; }
  .newroom label { display: flex; flex-direction: column; gap: 4px; margin-bottom: var(--sp-8); font-size: 13px; color: var(--text-2); }
  .newroom input { height: 30px; padding: 0 var(--sp-8); background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: var(--radius-control); font: inherit; }
  .muted { color: var(--muted); }
  .err.card { border-color: var(--err); }

  @media (max-width: 720px) {
    .wrap { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
    .rooms { flex-direction: row; flex-wrap: wrap; max-height: 130px; overflow-y: auto; border-right: none; border-bottom: 1px solid var(--line); }
    .people-lane { margin: 0; padding: 0; border: none; flex-basis: 100%; }
    /* the room's name on one line, its search on the next, full width */
    .roomhead { flex-wrap: wrap; row-gap: var(--sp-8); }
    .roomhead .search { flex: 1 1 100%; margin-left: 0; }
    .roomhead .search input { width: 100%; box-sizing: border-box; }
  }

  @media (prefers-reduced-motion: reduce) {
    .msg, .acts { transition: none; }
  }
</style>

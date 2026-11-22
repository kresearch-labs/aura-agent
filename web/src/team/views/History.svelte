<!-- History.svelte — activity + decisions + the digest. The 
     fixes: "1 file" not "1 files", ✓/✗ per real status, filters list
     only people in the project, plain-word kinds, verify line. -->
<script lang="ts">
  import { onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { currentProjectId, perms, live } from "../frame/shell_state";
  import { Check, X, CircleDot } from "@lucide/svelte";

  type EventRow = { seq: number; type: string; who: string; sentence: string;
                    ts: number; detail?: { code?: string; text?: string } };
  type DecisionRow = EventRow;

  let events = $state<EventRow[]>([]);
  let decisions = $state<DecisionRow[]>([]);
  let digest = $state<string | null>(null);
  let loading = $state(true);
  let error = $state("");
  let pid = $state<number | undefined>(undefined);
  let q = $state("");
  let person = $state("");
  let type_ = $state("");
  let detail = $state(false);
  // a person's name only: rows the hub wrote itself (an overlap) have none
  let peopleList = $derived(Array.from(new Set(events.map((e) => e.who).filter(Boolean))).sort() as string[]);
  // row: export + verify only with the tick the pages call
  let canExport = $derived(pid !== undefined &&
    perms.allowed("export_activity", undefined, pid));

  // The chain check — log/verify with its own permission (manage_activity), honest result line,
  // own epoch.
  type Verify = { ok: boolean; detail: string };
  let verifyBusy = $state(false);
  let verifyResult = $state<Verify | null>(null);
  let verifyError = $state("");
  let canVerify = $derived(pid !== undefined &&
    perms.allowed("manage_activity", undefined, pid));
  let verifySeq = 0;

  async function checkChain(): Promise<void> {
    if (verifyBusy || pid === undefined) return;
    const seq = ++verifySeq;
    verifyBusy = true; verifyError = ""; verifyResult = null;
    try {
      const answer = await call<{ ok: boolean; checked?: number;
                                  first_bad_seq?: number;
                                  bad?: { seq: number; type: string;
                                          subject?: string };
                                  chain?: string }>(
        `/log/verify?project=${pid}`);
      if (seq !== verifySeq) return;
      verifyResult = answer.ok
        ? { ok: true,
            detail: `the chain verifies — ${answer.checked ?? 0} events checked` }
        : { ok: false,
            detail: `first broken row: #${answer.bad?.seq ?? answer.first_bad_seq ?? "?"}` +
              (answer.bad?.type ? ` (${answer.bad.type})` : "") };
    } catch (e) {
      if (seq === verifySeq)
        verifyError = e instanceof Error ? e.message : "no answer";
    } finally {
      if (seq === verifySeq) verifyBusy = false;
    }
  }

  function tone(t: string): "ok" | "warn" | "err" | "" {
    if (t.endsWith(".ran") || t.endsWith(".test") || t === "task.ran") {
      return t.includes(":fail") || t.includes("failed") ? "err" : "ok";
    }
    if (t === "task.finished") return "ok";
    if (t === "task.blocked") return "warn";
    return "";
  }

  function prettyWord(t: string): string {                     // plain words,
    return t.replaceAll(".", " ").replaceAll("_", " ");        // never raw codes
  }

  export function when_(ts: number): string {
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  const filtered = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    return events.filter((e) =>
      (!person || e.who === person) &&
      (!type_ || e.type === type_) &&
      (!needle || (e.sentence + " " + e.who).toLowerCase().includes(needle)));
  });

  let refreshSequence = 0;
  async function refresh(p: number) {
    const seq = ++refreshSequence;
    pid = p; loading = true; error = "";
    try {
      const qs = `?detail=${detail ? 1 : 0}` +
        (q ? `&q=${encodeURIComponent(q)}` : "");
      const [a, d] = await Promise.all([
        call<{ events: EventRow[]; total?: number; first_ts?: number }>(
          `/projects/${p}/activity${qs}`),
        call<{ events: DecisionRow[] }>(
          `/projects/${p}/activity?type=decision.made&detail=1`),
      ]);
      if (seq !== refreshSequence) return;
      events = a.events ?? [];
      decisions = d.events ?? [];
    } catch (e) {
      if (seq === refreshSequence)
        error = e instanceof Error ? e.message : "no answer";
    } finally {
      if (seq === refreshSequence) loading = false;
    }
  }

  let curPid: number | undefined;
  onMount(() => currentProjectId.subscribe((v: number | undefined) => {
    if (v !== undefined && v !== curPid) { curPid = v; void refresh(v); }
  }));

  // A log-answer for this project rewinds through the answers
  onMount(() => live.subscribe((ev) => {
    if (!ev || ev.kind !== "log") return;
    const dp = (ev.data as { project?: number } | null | undefined)?.project;
    if (dp !== undefined && curPid !== undefined && dp !== curPid) return;
    if (curPid !== undefined) void refresh(curPid);
  }));

  // The digest follows the shown project — never stale from the first mount the same
  // subscription that drives the activity load also answers for the digest line
  onMount(() => currentProjectId.subscribe((v: number | undefined) => {
    if (v === undefined) return;
    void (async () => {
      try {
        const d = await call<{ text?: string }>(`/projects/${v}/digest`);
        digest = d.text ?? null;
      } catch { digest = null; }
    })();
  }));
</script>

<svelte:head><title>History · Aura Teams</title></svelte:head>

<div class="wrap">
  <header class="rowgap"><h2>History</h2>
    <span class="muted">everything that happened, in order — signed, never
      rewritten</span>
    {#if canExport}
      <a class="btn" href={`/api/v1/projects/${pid}/export?format=json`}
         target="_blank" rel="noopener">export this project's chain</a>
    {/if}
    {#if canVerify}
      <button class="btn" type="button" onclick={() => void checkChain()}
              disabled={verifyBusy}>
        {verifyBusy ? "checking the chain…" : "check the chain"}</button>
    {/if}
  </header>
  {#if canVerify && (verifyResult || verifyError)}
    {#if verifyError}
      <p class="err card" role="alert">{verifyError}</p>
    {:else if verifyResult}
      <p class={verifyResult.ok ? "muted" : "err card"} role="status">
        {#if verifyResult.ok}<Check size={13} strokeWidth={1.75}/>{:else}<X size={13} strokeWidth={1.75}/>{/if} {verifyResult.detail}</p>
    {/if}
  {/if}

  <div class="filters">
    <input bind:value={q} placeholder="search" class="q" />
    <div class="chips" role="group" aria-label="by person">
      <button class="chip" class:on={!person} onclick={() => (person = "")}>everyone</button>
      {#each peopleList as p (p)}
        <button class="chip" class:on={person === p} onclick={() => (person = p)}>{p}</button>
      {/each}
    </div>
    <!-- "kinds in plain words": chips, only types the feed answered -->
    <div class="chips" role="group" aria-label="by kind">
      <button class="chip" class:on={!type_} onclick={() => (type_ = "")}>any kind</button>
      {#each Array.from(new Set(events.map((e) => e.type))).sort() as t (t)}
        <button class="chip" class:on={type_ === t} onclick={() => (type_ = t)}>{prettyWord(t)}</button>
      {/each}
    </div>
    <label class="detail">
      <input type="checkbox" bind:checked={detail}
             onchange={() => { if (pid !== undefined) void refresh(pid); }} /> show detail
    </label>
  </div>

  {#if loading}
    <p class="muted">the journal reads…</p>
  {:else if error}
    <div class="err card" role="alert">
      <p>{error}</p>
      <button class="btn" onclick={() => pid !== undefined && void refresh(pid)}>Try again</button>
    </div>
  {:else if filtered.length === 0}
    <p class="muted">Nothing has happened in this project yet.</p>
  {:else}
    <div class="events" role="list" aria-live="polite">
      {#each filtered as e (e.seq)}
        {@const po = tone(e.type)}
        <div class="evrow" role="listitem">
          <span class={`evtag ${po}`}>
            {#if po === "err"}<X size={12} strokeWidth={1.75}/>{:else if po === "ok"}<Check size={12} strokeWidth={1.75}/>{:else}<CircleDot size={12} strokeWidth={1.75}/>{/if}
            {prettyWord(e.type)}
          </span>
          <b>{e.who}</b>
          <span class="msg">{e.sentence}</span>
          <time class="muted">{when_(e.ts)}</time>
        </div>
      {/each}
    </div>
  {/if}

  {#if decisions.length}
    <h3 class="sectionhead">decisions</h3>
    <div class="cards">
      {#each decisions as d (d.seq)}
        <div class="card">
          <b>{d.who}</b>
          <span>{d.sentence}</span>
          <time class="muted">{when_(d.ts)}</time>
        </div>
      {/each}
    </div>
  {/if}

  {#if digest}
    <h3 class="sectionhead">today in the small</h3>
    <pre class="card digest mono">{digest}</pre>
  {/if}
</div>

<style>
  .wrap { padding: var(--sp-16); display: flex; flex-direction: column; gap: var(--sp-12); }
  .filters { display: flex; gap: var(--sp-8); align-items: baseline; flex-wrap: wrap; }
  .q { height: 28px; padding: 0 var(--sp-8); background: var(--bg); color: var(--text);
       border: 1px solid var(--line); border-radius: var(--radius-control); }
  select { height: 28px; background: var(--bg); color: var(--text);
           border: 1px solid var(--line); border-radius: var(--radius-control); }
  label.detail { display: inline-flex; align-items: center; gap: var(--sp-4);
                 color: var(--muted); font-size: 13px; }
  .events { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
  .evrow { display: flex; gap: var(--sp-8); align-items: baseline;
           padding: var(--sp-4) 0; }
  .evtag { font-family: var(--font-mono); font-size: 11px; padding: 0 var(--sp-8);
           border-radius: var(--radius-control); border: 1px solid var(--line); }
  .evtag.ok { color: var(--ok); }
  .evtag.err { color: var(--err); }
  .evtag.warn { color: var(--warn); }
  .msg { color: var(--text-2); flex: 1; min-width: 0; overflow:hidden; text-overflow: ellipsis; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
           gap: var(--sp-12); }
  .card { display: flex; flex-direction: column; gap: var(--sp-8); }
  .sectionhead { color: var(--muted); font-size: 13px; text-transform: none; }
  .digest { max-height: 32ex; overflow: auto; font-size: 12.5px; }
</style>

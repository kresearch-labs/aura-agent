<!-- Closes.
-->
<script lang="ts">
  const fileChanges: { path: string }[] = [];
  import { onMount } from "svelte";
  import { call } from "../../lib/api/client";

  let { path, project, onclose } = $props();
  let busy = $state(true); let error = $state("");
  let whoMoves = $state<{ person: string; task: number | string }[]>([]);
  let touchedLately = $state<{ when: string; task: number | string }[]>([]);
  let plannedBy = $state<number[]>([]);
  let fileName = $state("");

  onMount(async () => {
    fileName = path.split("/").pop() ?? path;
    try {
      const m = await call<{ overlays?: { path: string; people: { person: string; task: number | string }[] }[] }>(
        `/projects/${project}/map`);
      whoMoves = (m.overlays ?? []).find((o) => o.path === path)?.people ?? [];
      const t = await call<{ tasks: { id: number; number: number; planned_files?: string[]; status_type?: string; status: string }[] }>(
        `/tasks?project=${project}`);
      plannedBy = (t.tasks ?? [])
        .filter((x) => (x.planned_files ?? []).includes(path))
        .map((x) => x.number ?? x.id);
    } catch (e) { error = e instanceof Error ? e.message : "no answer"; }
    finally { busy = false; }
  });
</script>

<div class="card filepanel" role="dialog" aria-label={`the file ${fileName}`}>
  <header class="rowgap"><b class="mono">{fileName}</b><span class="spacer"></span>
    <button class="btn" onclick={onclose} aria-label="close">×</button></header>
  {#if busy}
    <p class="muted">asking the hub…</p>
  {:else if error}
    <p class="err">{error}</p>
  {:else}
    <div class="field"><span class="tag-label">who is changing it now</span>
      {#if whoMoves.length === 0}
        <span class="muted">nobody right now</span>
      {:else}
        <div class="chipline">
          {#each whoMoves as p}<span class="chip">{p.person} · #{p.task}</span>{/each}
        </div>
      {/if}
    </div>
    <div class="field"><span class="tag-label">tasks that plan it</span>
      {#if plannedBy.length === 0}
        <span class="muted">none named</span>
      {:else}
        <div class="chipline">
          {#each plannedBy as n}<a href={`#/board/${n}`}>#{n}</a>{/each}
        </div>
      {/if}
    </div>
    {#if fileChanges.length}
      <div class="field"><span class="tag-label">last changes</span>
        {#each fileChanges as fc, fci (fci + fc.path)}
          <span class="mono">{fc.path}</span>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .filepanel { position: fixed; right: var(--sp-24); top: 56px; width: 320px;
               z-index: 30; padding: var(--sp-16); display: flex;
               flex-direction: column; gap: var(--sp-12); }
  .rowgap { display: flex; gap: var(--sp-8); align-items: center; }
  .spacer { flex-grow: 1 }
  .chipline { display: flex; gap: var(--sp-8); flex-wrap: wrap; }
  .chip { font-size: 11px; padding: 0 var(--sp-8); }
  .tag-label { color: var(--text-2); font-size: 12.5px; }
</style>

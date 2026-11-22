<!-- Native popup like Chooser (the bits-ui popups throw on first mount here).
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  let { value, onchange, trigger, kit = "menu.box" }:
    { value?: number | null; onchange: (ts: number | null) => void; trigger: Snippet; kit?: string } = $props();
  let open = $state(false);
  let view = $state(new Date());
  let back: HTMLElement | undefined = $state();
  const y = $derived(view.getFullYear());
  const m = $derived(view.getMonth());
  const lead = $derived(new Date(y, m, 1).getDay());
  const days = $derived(new Date(y, m + 1, 0).getDate());
  const today = new Date().toDateString();
  // noon local time: a date never slips a day across time zones
  const ts = (day: number) => Math.round(new Date(y, m, day, 12).getTime() / 1000);
  const isSel = (day: number) =>
    value != null && new Date(y, m, day).toDateString() === new Date(value * 1000).toDateString();
  function pick(v: number | null) { onchange(v); close(); }
  function shift(delta: number) {
    const d = new Date(); d.setDate(d.getDate() + delta); d.setHours(12, 0, 0, 0);
    pick(Math.round(d.getTime() / 1000));
  }
  function toggle() {
    open = !open;
    if (open) view = value ? new Date(value * 1000) : new Date();
  }
  // a click anywhere else closes it (focus stays where the person clicked)
  let root: HTMLElement | undefined = $state();
  $effect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (root && !root.contains(e.target as Node)) { open = false; } };
    document.addEventListener("pointerdown", away, true);
    return () => document.removeEventListener("pointerdown", away, true);
  });
  function close() {
    open = false;
    back?.querySelector<HTMLElement>("button")?.focus();
  }
</script>

<span class="chooser" bind:this={root}>
  <span bind:this={back} role="presentation"
        onclick={toggle}
        onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}>
    {@render trigger()}
  </span>
  {#if open}
    <div class="menu datechooser" role="dialog" aria-label="Choose a date" data-kit={kit} tabindex="-1"
         onkeydown={(e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } }}>
      <button type="button" class="mi" onclick={() => shift(0)}>Today</button>
      <button type="button" class="mi" onclick={() => shift(1)}>Tomorrow</button>
      <button type="button" class="mi" onclick={() => shift(7)}>Next week</button>
      {#if value != null}<button type="button" class="mi" onclick={() => pick(null)}>Clear</button>{/if}
      <div class="hd">
        <button type="button" class="ib" aria-label="Previous month" onclick={() => (view = new Date(y, m - 1, 1))}>‹</button>
        <b>{view.toLocaleDateString(undefined, { month: "long", year: "numeric" })}</b>
        <button type="button" class="ib" aria-label="Next month" onclick={() => (view = new Date(y, m + 1, 1))}>›</button>
      </div>
      <div class="grid">
        {#each ["S", "M", "T", "W", "T", "F", "S"] as w, wi (wi)}<span class="wd">{w}</span>{/each}
        {#each Array(lead) as _, li (li)}<span></span>{/each}
        {#each Array(days) as _, d (d)}
          <button type="button" class="day" class:sel={isSel(d + 1)}
                  class:today={new Date(y, m, d + 1).toDateString() === today}
                  aria-label={new Date(y, m, d + 1).toLocaleDateString()}
                  onclick={() => pick(ts(d + 1))}>{d + 1}</button>
        {/each}
      </div>
    </div>
  {/if}
</span>

<style>
  .datechooser { width: 252px; }
  .hd { display: flex; align-items: center; justify-content: space-between; padding: 6px 4px 4px;
        border-top: 1px solid var(--line); margin-top: 4px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; padding: 2px; }
  .wd { font-size: 11px; color: var(--muted); text-align: center; padding: 2px 0; }
  .day { height: 30px; border-radius: 6px; font-size: 12.5px; color: var(--text-2); }
  .day:hover { background: var(--hover); color: var(--text); }
  .day.today { color: var(--acc); font-weight: 600; }
  .day.sel { background: var(--acc); color: var(--on-acc); }
</style>

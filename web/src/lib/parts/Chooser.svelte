<!-- Dropdown with optional search; Enter picks, Esc closes, focus returns. Same keyboard
     contract as Segmented.
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  let { search = false, multiple = false, create = false, options, value, onchange, trigger, kit = "menu.box" }:
    { search?: boolean; multiple?: boolean; create?: boolean; options: { v: string; label: string; icon?: Snippet }[];
      value: string | string[]; onchange: (v: string | string[]) => void;
      trigger: Snippet; kit?: string } = $props();
  let open = $state(false);
  let query = $state("");
  let box: HTMLElement | undefined = $state();
  let back: HTMLElement | undefined = $state();
  const shown = $derived(options.filter((o) => !query || o.label.toLowerCase().includes(query.toLowerCase())));
  // create: the typed words become a new choice (labels)
  const canCreate = $derived(create && query.trim() !== "" &&
    !options.some((o) => o.label.toLowerCase() === query.trim().toLowerCase()));
  const isChecked = (o: { v: string }) => Array.isArray(value) ? value.includes(o.v) : value === o.v;
  function choose(o: { v: string }) {
    if (multiple) {
      const cur = Array.isArray(value) ? [...value] : [value];
      onchange(cur.includes(o.v) ? cur.filter((x) => x !== o.v) : [...cur, o.v]); // stays open
    } else {
      onchange(o.v);
      close();
    }
  }
  // a click anywhere else closes it (focus stays where the person clicked)
  let root: HTMLElement | undefined = $state();
  $effect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (root && !root.contains(e.target as Node)) { open = false; query = ""; } };
    document.addEventListener("pointerdown", away, true);
    return () => document.removeEventListener("pointerdown", away, true);
  });
  function close() {
    open = false;
    query = "";
    // focus returns to the trigger's interactive child (the snippet's own control)
    back?.querySelector<HTMLElement>("[tabindex]")?.focus();
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
    else if (e.key === "Enter") {
      const q = box?.querySelector<HTMLInputElement>("input.q");
      if (q && document.activeElement === q) {
        e.preventDefault();
        box?.querySelector<HTMLElement>(".mi:not(.muted)")?.click();
      }
    }
  }
</script>

<span class="chooser" bind:this={root}>
  <span bind:this={back} aria-haspopup="listbox" aria-expanded={open}
        onclick={() => (open = !open)}
        onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open = !open; } }}>
    {@render trigger()}
  </span>
  {#if open}
    <div class="menu" role="listbox" data-kit={kit} bind:this={box} onkeydown={onKey}>
      {#if search}<input class="q" placeholder="Search…" bind:value={query} aria-label="Search" />{/if}
      {#each shown as o, i (o.v)}
        <button type="button" class="mi" role="option" data-kit={i === 0 ? "menu.item" : undefined}
                aria-selected={isChecked(o)}
                onclick={() => choose(o)}>
          {@render o.icon?.()}
          <span class="ck">{isChecked(o) ? "✓" : ""}</span>{o.label}
        </button>
      {/each}
      {#if canCreate}
        <button type="button" class="mi" onclick={() => { const v = query.trim(); query = ""; choose({ v }); }}>
          Create “{query.trim()}”</button>
      {:else if shown.length === 0}<div class="mi muted">Nothing matches.</div>{/if}
    </div>
  {/if}
</span>

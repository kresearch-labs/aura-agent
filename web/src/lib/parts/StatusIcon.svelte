<!-- StatusIcon.svelte — the six status shapes (shape AND colour, never colour alone). `type` is
     the hub's status type: backlog | todo | in_progress | in_review | done | canceled. Colour
     is set with style:color (a style="" attribute is blocked by the hub).
-->
<script lang="ts">
  let { type, size = 14, label = true }: { type: string; size?: 12 | 14 | 16; label?: boolean } = $props();
  const COLOURS: Record<string, string> = {
    backlog: "var(--muted)", todo: "var(--text-2)", in_progress: "var(--warn)",
    in_review: "var(--wait)", done: "var(--ok)", canceled: "var(--muted)",
  };
  const WORDS: Record<string, string> = {
    backlog: "Backlog", todo: "To do", in_progress: "In progress",
    in_review: "In review", done: "Done", canceled: "Canceled",
  };
  const colour = $derived(COLOURS[type] ?? "var(--muted)");
  const cls = $derived(size === 16 ? "i" : `i s${size}`);
</script>

<svg class={cls} viewBox="0 0 24 24" style:color={colour}
     role={label ? "img" : undefined} aria-label={label ? WORDS[type] ?? type : undefined}
     aria-hidden={label ? undefined : "true"}>
  {#if type === "backlog"}
    <path d="M10.1 2.18a10 10 0 0 1 3.8 0M17.6 3.71a10 10 0 0 1 2.69 2.7M21.82 10.1a10 10 0 0 1 0 3.8M20.29 17.6a10 10 0 0 1-2.7 2.69M13.9 21.82a10 10 0 0 1-3.8 0M6.4 20.29a10 10 0 0 1-2.69-2.7M2.18 13.9a10 10 0 0 1 0-3.8M3.71 6.4a10 10 0 0 1 2.7-2.69"/>
  {:else if type === "in_progress"}
    <circle cx="12" cy="12" r="10"/><path d="M12 6a6 6 0 0 1 0 12z" fill="currentColor" stroke="none"/>
  {:else if type === "in_review"}
    <circle cx="12" cy="12" r="10"/><path d="M12 6a6 6 0 1 1-6 6h6z" fill="currentColor" stroke="none"/>
  {:else if type === "done"}
    <circle cx="12" cy="12" r="10" fill="currentColor" stroke="none"/><path d="m8 12 3 3 5-6" stroke-width="2.4" style:stroke="var(--sheet)"/>
  {:else if type === "canceled"}
    <circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/>
  {:else}
    <circle cx="12" cy="12" r="10"/>
  {/if}
</svg>

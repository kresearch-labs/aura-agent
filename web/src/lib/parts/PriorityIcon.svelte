<!-- The hub stores priority as a number ("0 = normal"). The page offers only: 0 No priority · 1
     Low · 2 Medium · 3 High · 4 Urgent (anything above 4 shows as Urgent).
     Classes.prio/.p1-3/.urgent come from parts.css.
-->
<script lang="ts" module>
  export const PRIORITY_WORDS = ["No priority", "Low", "Medium", "High", "Urgent"] as const;
  export const priorityWord = (p: number | undefined) => PRIORITY_WORDS[Math.max(0, Math.min(4, p ?? 0))];
</script>

<script lang="ts">
  let { priority = 0, kit }: { priority?: number; kit?: string } = $props();
  const p = $derived(Math.max(0, Math.min(4, priority ?? 0)));
</script>

{#if p >= 4}
  <span class="urgent" role="img" aria-label="Urgent" data-kit={kit}>!</span>
{:else}
  <span class="prio p{p}" role="img" aria-label={PRIORITY_WORDS[p]} data-kit={kit}><b></b><b></b><b></b></span>
{/if}

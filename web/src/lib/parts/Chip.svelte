<!-- A small filter/action chip -->
<script lang="ts">
  import type { Snippet } from "svelte";
  let { on = false, onclick, children }: { on?: boolean; onclick?: () => void; children?: Snippet } = $props();
</script>
<button class="chip" class:on {onclick}>{@render children?.()}</button>

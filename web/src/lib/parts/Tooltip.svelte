<!-- Small menu-look tooltip -->
<script lang="ts">
  import { Tooltip } from "bits-ui";
  import type { Snippet } from "svelte";
  let { text, children }: { text: string; children?: Snippet } = $props();
</script>
<Tooltip.Provider>
  <Tooltip.Root>
    <Tooltip.Trigger>{@render children?.()}</Tooltip.Trigger>
    <Tooltip.Portal><Tooltip.Content class="menu tip">{text}</Tooltip.Content></Tooltip.Portal>
  </Tooltip.Root>
</Tooltip.Provider>

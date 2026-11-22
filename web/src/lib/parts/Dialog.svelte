<!-- Modal dialog on bits-ui; focus trap, Esc, focus return -->
<script lang="ts">
  import { Dialog } from "bits-ui";
  import type { Snippet } from "svelte";
  let { open = false, onopenchange, title, children }:
    { open?: boolean; onopenchange: (o: boolean) => void; title: string; children?: Snippet } = $props();
</script>
<Dialog.Root {open} onOpenChange={onopenchange}>
  <Dialog.Portal>
    <Dialog.Overlay class="backdrop" />
    <Dialog.Content class="task-wrap dialog">
      <Dialog.Title class="task-h">{title}</Dialog.Title>
      {@render children?.()}
      <Dialog.Close class="x">×</Dialog.Close>
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

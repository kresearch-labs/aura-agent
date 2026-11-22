<!-- The sliding side panel (not modal); parts.css.task-wrap.panel drives -->
<script lang="ts">
  import { Dialog } from "bits-ui";
  import type { Snippet } from "svelte";
  let { open = false, onopenchange, title, children }:
    { open?: boolean; onopenchange: (o: boolean) => void; title: string; children?: Snippet } = $props();
</script>
<Dialog.Root {open} onOpenChange={onopenchange}>
  <Dialog.Portal>
    <Dialog.Content class={`task-wrap panel${open ? " open" : ""}`}>
      <Dialog.Title class="visually-hidden">{title}</Dialog.Title>
      {@render children?.()}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

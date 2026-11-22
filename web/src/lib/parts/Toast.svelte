<!-- Polite status toast, bottom-centre, for Undo-after-delete -->
<script lang="ts">
  import type { Snippet } from "svelte";
  let { children }: { children?: Snippet } = $props();
</script>
<div class="toast-line" role="status" aria-live="polite">
  {@render children?.()}
</div>

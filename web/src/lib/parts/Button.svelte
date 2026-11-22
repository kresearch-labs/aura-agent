<script lang="ts">
  import type { Snippet } from "svelte";
  let { kind = "sec2", icon = false, label, onclick, children, disabled = false }: {
    kind?: "pri" | "sec2" | "ghost" | "danger";
    icon?: boolean;
    label?: string;                        // icon-only needs it (screen readers)
    onclick?: () => void;
    children?: Snippet;
    disabled?: boolean;
  } = $props();
</script>
<button class={`btn ${kind}${icon ? " ib" : ""}`} {onclick} {disabled}
        aria-label={icon ? label : undefined} title={icon ? label : undefined}>
  {@render children?.()}
</button>

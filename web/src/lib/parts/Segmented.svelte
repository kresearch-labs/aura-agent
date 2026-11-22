<!-- Radiogroup of buttons; arrow keys move the selection -->
<script lang="ts">
  let { options, value, onchange }: {
    options: { v: string; label: string }[];
    value: string;
    onchange: (v: string) => void;
  } = $props();
  function key(e: KeyboardEvent) {
    const i = options.findIndex((o) => o.v === value);
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      onchange(options[(i + 1) % options.length].v);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      onchange(options[(i - 1 + options.length) % options.length].v);
    }
  }
</script>
<div class="seg" role="radiogroup" onkeydown={key}>
  {#each options as o (o.v)}
    <button role="radio" aria-checked={value === o.v} class:on={value === o.v}
            tabindex={value === o.v ? 0 : -1}
            onclick={() => onchange(o.v)}>
      {o.label}
    </button>
  {/each}
</div>

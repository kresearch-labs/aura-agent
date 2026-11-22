<script lang="ts">
  let { module }: { module: string } = $props();
</script>

<section class="wrap card">
  <h3>{module}</h3>
  <p>There is no page here.</p>
</section>

<style>
  .wrap { margin: var(--sp-16); }
</style>

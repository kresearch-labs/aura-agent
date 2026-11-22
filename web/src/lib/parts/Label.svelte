<!-- A coloured label — dot + text; colour rides --lc -->
<script lang="ts">
  let { text, colour, kit }: { text: string; colour: string; kit?: string } = $props();
</script>
<span class="label" style:--lc={colour} data-kit={kit}><i></i>{text}</span>

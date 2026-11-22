<!-- #/gallery — DEV only. -->
<script lang="ts">
  import Button from "./Button.svelte";
  import Segmented from "./Segmented.svelte";
  import Chip from "./Chip.svelte";
  import Label from "./Label.svelte";
  import Avatar from "./Avatar.svelte";
  import Chooser from "./Chooser.svelte";
  let seg = $state("a");
  let picked = $state("");
  let pickedMulti = $state<string[]>([]);
</script>
<div class="g-wrap">
  <h2>the gallery</h2>
  <section><h3>buttons</h3>
    <Button kind="pri" onclick={() => {}}>Primary</Button>
    <Button kind="sec2" onclick={() => {}}>Secondary</Button>
    <Button kind="ghost" onclick={() => {}}>Ghost</Button>
    <Button kind="danger" onclick={() => {}}>Danger</Button>
    <Button icon label="close" onclick={() => {}}>×</Button>
    <Button kind="pri" disabled onclick={() => {}}>Disabled</Button>
  </section>
  <section><h3>segmented</h3>
    <Segmented options={[{v:"a",label:"One"},{v:"b",label:"Two"},{v:"c",label:"Three"}]}
               value={seg} onchange={(v) => (seg = v)}  />
  </section>
  <section><h3>chips + labels</h3>
    <Chip>off</Chip><Chip on>on</Chip>
    <Label text="security" colour="var(--hue-blue)" />
    <Label text="urgent" colour="var(--hue-pink)" />
  </section>
  <section><h3>avatars</h3>
    <Avatar name="asha" /><Avatar name="ravi" size="md" presence="online" />
    <Avatar name="mei" size="lg" presence="away" /><Avatar name="aura" aura />
  </section>
  <section><h3>chooser — single, searchable</h3>
    {#snippet trigSingle()}<span tabindex="0" role="button">status: {picked || "—"}</span>{/snippet}
    <Chooser search trigger={trigSingle}
             options={[{v:"todo",label:"To do"},{v:"review",label:"In review"},{v:"done",label:"Done"}]}
             value={picked} onchange={(v) => (picked = v as string)} />
  </section>
  <section><h3>chooser — multiple, stays open</h3>
    {#snippet trigMulti()}<span tabindex="0" role="button">labels: {pickedMulti.length}</span>{/snippet}
    <Chooser search multiple trigger={trigMulti}
             options={[{v:"security",label:"security"},{v:"perf",label:"performance"},{v:"ux",label:"ux"}]}
             value={pickedMulti} onchange={(v) => (pickedMulti = v as string[])} />
  </section>
</div>
<style>
  .g-wrap { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  section { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  h3 { width: 100%; margin: 0; font-size: 12px; color: var(--muted); }
</style>

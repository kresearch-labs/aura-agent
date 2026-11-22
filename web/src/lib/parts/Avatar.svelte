<!-- A person's avatar — their photo, or the drawing they picked (see lib/avatars.ts) — with the
     presence dot;.md/.lg sizes;.aura = the Aura logo tile.
-->
<script lang="ts">
  import { avatars, avatarSrc, drawReady } from "../avatars";
  import { hueFor } from "../chat_logic";
  let { name, display, size, presence, aura = false, kit }:
    { name: string; display?: string; size?: "md" | "lg"; presence?: "online" | "away" | "offline"; aura?: boolean; kit?: string } = $props();
  const src = $derived(aura ? "" : avatarSrc(name, $avatars, $drawReady));
</script>
<span class={`av${size === "md" ? " md" : size === "lg" ? " lg" : ""}${aura ? " aura" : ""}`}
      data-kit={kit} title={aura ? undefined : (display ?? name)}
      style:background={aura ? undefined : `color-mix(in oklch, ${hueFor(name)} 30%, var(--raised))`}>
  {#if aura}<img class="aura-mark" src="./aura-logo-64.png" alt="" />
  {:else if src}<img class="av-img" {src} alt="" loading="lazy" />{/if}
  {#if presence && presence !== "offline"}<span class="pr" class:away={presence === "away"}></span>{/if}
</span>

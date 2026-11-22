<!-- Reuse the server's upload magic-byte answer; never require a HEAD verb just to render.
-->
<script lang="ts">
  interface Props {
    fileId: string | { id: string; name?: string; type?: string; size?: number }
  }
  let { fileId }: Props = $props();
  const id = $derived(typeof fileId === "string" ? fileId : fileId?.id);
  const type = $derived(typeof fileId === "string" ? "" : fileId?.type ?? "");
  const name = $derived(typeof fileId === "string" ? "the file" : fileId?.name ?? "the file");
  const size = $derived(typeof fileId === "string" ? 0 : fileId?.size ?? 0);
  const ext = $derived((name.split(".").pop() ?? "").toUpperCase().slice(0, 4));
  const kindWord = $derived(type.startsWith("image/") ? "image"
    : type.includes("pdf") ? "PDF"
    : /zip|compressed/.test(type) ? "archive" : "file");
  function sizeText(n: number): string {
    if (!n) return "";
    if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(n / 1024))} KB`;
  }
</script>

{#if id === undefined}
  <span class="muted">…</span>
{:else if type.startsWith("image/")}
  <img src={`/api/v1/chat/file/${id}`} alt={name} class="chatimg" loading="lazy" />
{:else}
  <a class="fcard" data-kit="chat.file-card" href={`/api/v1/chat/file/${id}`} target="_blank" rel="noreferrer" download>
    <span class="fthumb"><span class="ext">{ext}</span></span>
    <span class="fin">
      <span class="nm">{name}</span>
      <span class="meta">{kindWord}{size ? ` · ${sizeText(size)}` : ""}</span>
    </span>
  </a>
{/if}

<style>
  .chatimg { max-width: 280px; display: block; margin-top: 4px;
             border-radius: var(--radius-control); }
  .fcard { display: flex; align-items: center; gap: 10px; margin-top: 4px;
           padding: 10px 10px; border-radius: 12px;
           background: var(--raised); border: 1px solid var(--line);
           color: var(--text-2); text-decoration: none; max-width: 420px; }
  .fcard:hover { border-color: var(--line-strong); color: var(--text); }
  .fthumb { width: 34px; height: 34px; border-radius: 6px; flex: none;
            background: var(--raised-2); display: grid; place-items: center; }
  .ext { font-size: 8px; font-weight: 700; color: var(--text-2); }
  .fin { display: flex; flex-direction: column; min-width: 0; }
  .nm { font-size: 13px; color: var(--text); overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; }
  .meta { font-size: 11.5px; color: var(--muted); }
</style>

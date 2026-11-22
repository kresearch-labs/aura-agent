<!-- Md.svelte — render the neutralised markdown tree (web/src/lib/markdown.ts)
     with formatting + links and nothing else (the neutralise already
     demoted the dangerous shapes to text before we see them). -->
<script lang="ts">
  import { parse } from "./markdown";

  let { text }: { text: string } = $props();
  const doc = $derived(parse(text ?? ""));
  $effect(() => { void doc; mentionSeen = 0; });  // reruns (cheap) per doc change

  function flattenToText(n: any): string {
    if (n.value) return String(n.value);
    let out = "";
    for (const c of n.children ?? []) out += flattenToText(c);
    return out;
  }

  // A plain text node may carry #N task references — #12 splits as a link to the board's own
  // panel (#/board/12). Runs BEFORE task-ref splitting so the two nest cleanly. @ inside code
  // never reaches this path (code renders in its own branch).
  function mentionsOf(value: string): { kind: "text" | "mention"; v: string }[] {
    const out: { kind: "text" | "mention"; v: string }[] = [];
    let rest = value;
    const re = /@[a-zA-Z][\w-]{0,30}/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(rest)) !== null) {
      if (m.index) out.push({ kind: "text", v: rest.slice(0, m.index) });
      out.push({ kind: "mention", v: m[0] });
      rest = rest.slice(m.index + m[0].length);
    }
    if (rest) out.push({ kind: "text", v: rest });
    return out;
  }
  let mentionSeen = 0;   // instance counter, reset by the $effect below
  function mentionKit(isMention: boolean): string | undefined {
    if (!isMention) return undefined;
    return ++mentionSeen === 1 ? "chat.mention" : undefined;
  }

  
    
  function splitTaskRefs(value: string): { kind: "text" | "task"; v: string }[] {
    const out: { kind: "text" | "task"; v: string }[] = [];
    let rest = value;
    const re = /#(\d{1,10})\b/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(rest)) !== null) {
      if (m.index) out.push({ kind: "text", v: rest.slice(0, m.index) });
      out.push({ kind: "task", v: m[0] });
      rest = rest.slice(m.index + m[0].length);
    }
    if (rest) out.push({ kind: "text", v: rest });
    return out;
  }
</script>

<span class="md">
  {#each doc.children as block (block.position?.start?.offset ?? block)}
    {#if block.type === "paragraph"}
      <span class="block">
        {#each block.children ?? [] as n (n.position?.start?.offset ?? n)}
          {#if n.type === "text"}
            <span>
              <!-- @name mentions wrap first, then task refs -->
              {#each mentionsOf(n.value) as ment (ment.v + mentionSeen)}
                {#if ment.kind === "mention"}
                  <span class="mention" data-kit={mentionKit(true)}>{ment.v}</span>
                {:else}
                  {#each splitTaskRefs(ment.v) as part (part.v)}
                    {#if part.kind === "task"}
                      <a class="taskref"
                         href={`#/board/${part.v.slice(1)}`}>{part.v}</a>
                    {:else}
                      {part.v}
                    {/if}
                  {/each}
                {/if}
              {/each}
            </span>
          {:else if n.type === "strong" || n.type === "delete"}
            <strong>{flattenToText(n)}</strong>
          {:else if n.type === "emphasis"}
            <em>{flattenToText(n)}</em>
          {:else if n.type === "inlineCode"}
            <code>{n.value}</code>
          {:else if n.type === "link"}
            {@const url = String(n.url ?? "")}
            <a href={url} target="_blank" rel="noreferrer">{flattenToText(n)}</a>
          {:else}
            <span>{splitTaskRefs(flattenToText(n)).map((p) => p.v).join("")}</span>
          {/if}
        {/each}
      </span>
    {:else if block.type === "heading"}
      <strong class="h">{flattenToText(block)}</strong>
    {:else if block.type === "code"}
      <code class="code">{String((block as { value?: string }).value ?? "")}</code>
    {:else if block.type === "list"}
      <span class="block list">
        {#each (block.children ?? []) as item (item.position?.start?.offset ?? item)}
          <span class="li">· {flattenToText(item)}</span>
        {/each}
      </span>
    {:else}
      <span class="block">{flattenToText(block)}</span>
    {/if}
  {/each}
</span>

<style>
  .md .block { display: inline; white-space: pre-wrap; }
  .md .h { display: block; font-weight: 700; margin: 2px 0; }
  .md .code { display: block; background: var(--bg);
    border: 1px solid var(--line); padding: 4px 8px; border-radius: 6px;
    white-space: pre-wrap; font-family: ui-monospace, monospace; }
  .md .li { display: block; }
  .md strong { font-weight: 600; }
  .md em { font-style: italic; }
  .md a { color: var(--accent); text-decoration: none;
    border-bottom: 1px dotted var(--line-strong); }
  .md a:focus, .md a:hover { border-bottom-color: var(--accent); }
  .md a.taskref { color: var(--accent); font-weight: 500;
                  border-bottom: 1px solid var(--line-strong); }
</style>

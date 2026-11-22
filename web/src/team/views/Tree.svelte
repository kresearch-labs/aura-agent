<!-- Tree.svelte — the shared work tree from origin/<main>, folded,
     per-file size/name, the overlays (marks people full names, not one
     letter), collision chip per path. Click opens the file panel. -->
<script lang="ts">
  import { TriangleAlert, ChevronRight, Folder, FolderOpen, File, FileCode, FileText,
           FileImage, FileCog, FileLock, FileTerminal, Braces, Palette } from "@lucide/svelte";
  import { clashFor, clashText } from "../../lib/aura_state";
  import { onMount } from "svelte";
  import { call } from "../../lib/api/client";
  import { buildFileTree, type TreeNode } from "../../lib/file_tree";
  import { currentProjectId, live } from "../frame/shell_state";
  import FilePanel from "./FilePanel.svelte";

  type FileRow = { path: string; size: number; lang: string; rank: number;
                   updated?: number; who?: string };
  type Overlay = { path: string; people: { person: string; task: number | string }[] };
  type Collision = Overlay;

  let files = $state<FileRow[]>([]);
  let overlays = $state<Overlay[]>([]);
  let collisions = $state<Collision[]>([]);
  let searchQ = $state("");
  let loading = $state(true);
  let error = $state("");
  let pid = $state<number | undefined>(undefined);
  let fileSelected = $state<string | null>(null);
  let folds = $state<Record<string, boolean>>({});

  const byOverlay = $derived.by(() => {
    const map = new Map<string, Overlay["people"]>();
    for (const o of overlays) map.set(o.path, o.people);
    return map;
  });
  const collidesWith = $derived.by(() => new Set(collisions.map((c) => c.path)));

  type Node = TreeNode<FileRow>;
  const derivedTree = $derived.by(() => buildFileTree(files));

  // toggles from what is on screen now (a top folder starts open, so the
  // first click closes it)
  function openIt(path: string) {
    folds = { ...folds, [path]: !isOpen(path, depthOf(path)) };
  }
  function allFolders(nodes: Node[], out: string[] = []): string[] {
    for (const n of nodes) if ("children" in n) { out.push(n.path); allFolders(n.children, out); }
    return out;
  }
  const folderCount = $derived(allFolders(derivedTree.nodes).length);
  function setAll(open: boolean) {
    folds = Object.fromEntries(allFolders(derivedTree.nodes).map((f) => [f, open]));
  }
  // a file's kind, by its name: the icon and its colour
  const CODE = /\.(m?[jt]sx?|cjs|py|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|svelte|vue|sql)$/i;
  function kindOf(name: string): { icon: typeof File; hue: string } {
    const n = name.toLowerCase();
    if (/^\.?env(\..*)?$|secret|\.(pem|key)$/.test(n)) return { icon: FileLock, hue: "var(--hue-red)" };
    if (/^\.(gitignore|gitattributes|editorconfig|npmrc)$|\.(toml|ya?ml|ini|cfg|lock)$|^dockerfile$|^makefile$|config\.[a-z]+$/.test(n))
      return { icon: FileCog, hue: "var(--hue-grey)" };
    if (/\.(json|jsonc|jsonl)$/.test(n)) return { icon: Braces, hue: "var(--hue-green)" };
    if (/\.(md|mdx|txt|rst|adoc)$|^(readme|license|notice|changelog)/.test(n)) return { icon: FileText, hue: "var(--hue-blue)" };
    if (/\.(png|jpe?g|gif|webp|svg|ico|bmp|avif)$/.test(n)) return { icon: FileImage, hue: "var(--hue-pink)" };
    if (/\.(css|scss|sass|less)$/.test(n)) return { icon: Palette, hue: "var(--hue-violet)" };
    if (/\.(html?|xml)$/.test(n)) return { icon: FileCode, hue: "var(--hue-orange)" };
    if (/\.(sh|bash|zsh|ps1|bat|cmd)$/.test(n)) return { icon: FileTerminal, hue: "var(--hue-teal)" };
    if (/\.py$/.test(n)) return { icon: FileCode, hue: "var(--hue-blue)" };
    if (CODE.test(n)) return { icon: FileCode, hue: "var(--hue-amber)" };
    return { icon: File, hue: "var(--muted)" };
  }
  function isOpen(path: string, depth: number): boolean {
    const folded = folds[path] === true ? false : folds[path] === false ? true
      : depth < 2 ? true : false;
    return folds[path] ?? depth < 2;
  }

  function depthOf(p: string): number { return p.split("/").length - 1; }
  function humanSize(n?: number): string {
    if (n == null) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1_048_576) return `${(n / 1024).toFixed(1)} kB`;
    return `${(n / 1_048_576).toFixed(1)} MB`;
  }
  function pickedColor(name: string): string {
    const hue = (name.split("").reduce((a, c) => a + c.charCodeAt(0), 0) * 67) % 360;
    return `hsl(${hue} 45% 62%)`;
  }

  type Row = { el: unknown; path: string; depth: number; isFolder: boolean };
  const foldPersist: Record<string, boolean> = {};

  let refreshSequence = 0;
  async function refresh(_pid: number) {
    const seq = ++refreshSequence;
    pid = _pid; loading = true; error = "";
    try {
      const d = await call<{
        files: FileRow[]; overlays?: Overlay[]; collisions?: Collision[];
      }>(`/projects/${pid}/map`);
      if (seq !== refreshSequence) return;
      files = d.files ?? [];
      overlays = d.overlays ?? [];
      collisions = d.collisions ?? [];
    } catch (e) {
      if (seq === refreshSequence)
        error = e instanceof Error ? e.message : "the hub didn't answer";
    } finally {
      if (seq === refreshSequence) loading = false;
    }
  }
  let curPid: number | undefined;
  onMount(() => {
    const sub = currentProjectId.subscribe((v: number | undefined) => {
      if (v !== undefined && v !== curPid) { curPid = v; void refresh(v); }
    });
    return sub;
  });

  // The map answers (overlay/collision the second they land)
  onMount(() => live.subscribe((ev) => {
    if (!ev || ev.kind !== "map") return;
    const dp = (ev.data as { project?: number } | null | undefined)?.project;
    if (dp !== undefined && curPid !== undefined && dp !== curPid) return;
    if (curPid !== undefined) void refresh(curPid);
  }));

  function visible(nodeRows: Node[], depth: number, out: Node[]): Node[] {
    for (const n of nodeRows) {
      if ("children" in n) {
        out.push(n);
        if (isOpen(n.path, depth)) visible(n.children, depth + 1, out);
      } else { out.push(n); }
    }
    return out;
  }

  function pathHits(row: FileRow, q: string): boolean {
    if (!q) return true;
    return row.path.toLowerCase().includes(q.toLowerCase());
  }
  const rowsShown = $derived.by(() => {
    const { nodes } = derivedTree;
    const q = searchQ.trim();
    if (!q) return visible(nodes, 0, []);
    const m = files.filter((f) => pathHits(f, q));
    return visible(nodes, 0, []).filter((n) => {
      if ("children" in n) {
        return m.some((f) => f.path.startsWith(n.path + "/")) ||
               n.path.toLowerCase().includes(q.toLowerCase());
      }
      return pathHits(n as FileRow, q);
    });
  });
</script>

<svelte:head><title>Tree · Aura Teams</title></svelte:head>

<div class="wrap">
  <header class="rowgap">
    <h2>Tree</h2>
    <input bind:value={searchQ} class="search" placeholder="Filter by path (press /)"
           aria-label="filter the tree" />
    {#if pid !== undefined}
      <span class="muted">the files on main, marked with who is working on them</span>
    {/if}
    <span class="spacer"></span>
    {#if files.length}
      <span class="muted stats">{files.length} file{files.length === 1 ? "" : "s"} · {folderCount} folder{folderCount === 1 ? "" : "s"}</span>
      <button class="btn tiny" type="button" onclick={() => setAll(true)}>Expand all</button>
      <button class="btn tiny" type="button" onclick={() => setAll(false)}>Collapse all</button>
    {/if}
  </header>
  {#if loading}
    <p class="muted">the tree builds…</p>
  {:else if error}
    <p class="err card">{error}</p>
  {:else if rowsShown.length === 0}
    <p class="muted">{searchQ
      ? "nothing matches — parent folders stay with hits"
      : "no files seen yet — the map fills this in."}</p>
  {:else}
    <ul class="tree" role="tree" aria-label="project files">
      {#each rowsShown as n}
        {@const isFolder = "children" in n}
        {@const depth = depthOf(n.path)}
        {@const ov = byOverlay.get(n.path)}
        {@const collEntry = collisions.find((c) => c.path === n.path)}
        {@const coll = Boolean(collEntry)}
        {@const clashTxt = collEntry
          ? clashText(clashFor({ id: 0, number: 0,
                                  people: collEntry.people.map((pp) => pp.person) }, collisions)
                      ?? undefined as never, "", (u: string) => u)
          : ""}
        {@const rowFile = isFolder ? undefined : n as FileRow}
        <li
          class="treerow"
          class:folder={isFolder}
          class:collision={coll}
          role="treeitem" aria-selected="false"
          aria-expanded={isOpen(n.path, depth)}
          aria-level={depth + 1}
          class:picked={!isFolder && fileSelected === n.path}
        >
          {#each Array(depth) as _, gi (gi)}<span class="guide" aria-hidden="true"></span>{/each}
          {#if isFolder}
            {@const open = isOpen(n.path, depth)}
            {@const kids = (n as { children: Node[] }).children.length}
            <button class="foldrow" type="button" onclick={() => openIt(n.path)}
                    aria-label={`${open ? "collapse" : "expand"} ${n.path}`}>
              <span class="chev" class:open aria-hidden="true"><ChevronRight size={14} strokeWidth={2} /></span>
              <span class="ico folder-ico" aria-hidden="true">
                {#if open}<FolderOpen size={16} strokeWidth={1.75} />{:else}<Folder size={16} strokeWidth={1.75} />{/if}
              </span>
              <span class="trname dir">{n.path.split("/").pop()}</span>
            </button>
            <span class="muted count">{kids} item{kids === 1 ? "" : "s"}</span>
            {#if ov}
              <span class="chip overlay">{ov.map((o) => o.person).join(", ")}</span>
            {/if}
            {#if coll}<span class="chip warn" title="both tasks plan here"><TriangleAlert size={11} strokeWidth={1.75}/> {clashTxt}</span>{/if}
          {:else}
            {@const f = rowFile!}
            {@const fname = f.path.split("/").pop() ?? f.path}
            {@const kind = kindOf(fname)}
            <button class="btn-none name" onclick={() => { fileSelected = f.path; }}>
              <span class="chev-pad" aria-hidden="true"></span>
              <span class="ico" style:color={kind.hue} aria-hidden="true"><kind.icon size={16} strokeWidth={1.75} /></span>
              <span class="trname">{fname}</span>
            </button>
            {#if ov}
              {#each ov as p2}
                <span class="chip overlay"
                      style:background={`${pickedColor(p2.person)}22`}
                      style:color={pickedColor(p2.person)}
                      title={`${p2.person} moves it here`}>{p2.person}</span>
              {/each}
            {/if}
            {#if coll}<span class="chip warn" title="both tasks name this file"><TriangleAlert size={11} strokeWidth={1.75}/> {clashTxt}</span>{/if}
            <span class="muted trsize">{humanSize(f.size)}</span>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

{#if fileSelected}
  <FilePanel path={fileSelected} project={pid} onclose={() => (fileSelected = null)} />
{/if}

<style>
  .wrap { padding: var(--sp-16); display: flex; flex-direction: column; gap: var(--sp-12); }
  .rowgap { display: flex; gap: var(--sp-12); align-items: center; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .stats { font-size: 12px; }
  .search { flex: 0 1 240px; height: 30px; padding: 0 var(--sp-8);
            background: var(--bg); color: var(--text); border: 1px solid var(--line);
            border-radius: var(--radius-control); }
  .tree { list-style: none; margin: 0; padding: 6px 0; font-size: 13px;
          border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--raised); }
  .treerow { display: flex; align-items: center; gap: var(--sp-8); padding: 0 12px 0 8px;
             height: 30px; white-space: nowrap; overflow: hidden; }
  .treerow.collision { background: var(--err-bg); }
  .treerow:hover { background: color-mix(in oklab, var(--raised), var(--text) 6%); }
  .treerow.picked { background: color-mix(in oklab, var(--raised), var(--accent) 14%); }
  /* one guide line per level, so the nesting reads at a glance */
  .guide { flex: none; width: 18px; align-self: stretch; margin-right: -8px;
           background: linear-gradient(var(--line), var(--line)) no-repeat 8px 0 / 1px 100%; }
  .foldrow, .name { all: unset; display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
                    min-width: 0; height: 100%; }
  .foldrow:focus-visible, .name:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: 4px; }
  .chev { display: inline-flex; width: 16px; color: var(--muted); transition: transform 120ms ease; }
  .chev.open { transform: rotate(90deg); }
  .chev-pad { display: inline-block; width: 16px; }
  .ico { display: inline-flex; flex: none; }
  .folder-ico { color: var(--accent); }
  .trname { color: var(--text); overflow: hidden; text-overflow: ellipsis; }
  .trname.dir { font-weight: 600; }
  .count { font-size: 12px; }
  .chip { font-size: 11px; padding: 0 var(--sp-8); border-radius: var(--radius-control);
          border: 1px solid var(--line); }
  .chip.warn { border-color: var(--warn); color: var(--warn); }
  .chip.overlay { border: none; }
  .trsize { margin-left: auto; padding-left: 12px; font-size: 12px; font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
</style>

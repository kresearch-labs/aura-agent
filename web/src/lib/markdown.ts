// markdown.ts — E.11's shape verbatim: the GFM parse, then neutralise:
// html nodes → text, link href + image src checked, image src limited to
// /api/v1/files/<hash> (the hub's own file-lane) — Markdown.svelte walks
// the tree with a {#snippet} per node type; text nodes are plain text.
import { fromMarkdown } from "mdast-util-from-markdown";
import { gfm } from "micromark-extension-gfm";
import { gfmFromMarkdown } from "mdast-util-gfm";
import type { Root, RootContent } from "mdast";

const FILES_ONLY = /^\/api\/v1\/files\/[A-Za-z0-9-]+$/;
const SAFE_LINK = /^(https?:|mailto:|\/(?!\/))/i;

export function parse(src: string): Root {
  const tree = fromMarkdown(src, {
    extensions: [gfm()],
    mdastExtensions: [gfmFromMarkdown()],
  });
  nearTheDefinitions(tree);
  return neutralise(tree);
}

function nearTheDefinitions(root: Root): void {
  const walkD = (node: Root | RootContent): void => {
    if ("children" in node && Array.isArray(node.children)) {
      const kept: RootContent[] = [];
      for (const child of node.children) {
        if (child.type === "definition") {
          const url = (child as { url?: string }).url ?? "";
          if (!SAFE_LINK.test(url)) continue;      // never speaks
        }
        walkD(child);
        kept.push(child);
      }
      node.children = kept;
    }
  };
  walkD(root);
}

function neutralise(root: Root): Root {
  const walk = (node: Root | RootContent): void => {
    if ("children" in node && Array.isArray(node.children)) {
      const kept: RootContent[] = [];
      for (const child of node.children) {
        // raw HTML becomes TEXT — the doc: "html → text"
        if (child.type === "html") {
          kept.push({ type: "text", value: (child as { value?: string }).value ?? "" });
          continue;
        }
        // link & image URL policy
        if (child.type === "link") {
          const url = (child as { url?: string }).url ?? "";
          if (!SAFE_LINK.test(url)) {
            // Demote to the children — THROUGH the policy, not around it
            const inner = { children:
              [...((child as { children?: RootContent[] }).children ?? [])] };
            walk(inner as Root);
            for (const t of inner.children) kept.push(t);
            continue;
          }
          walk(child);
          kept.push(child);
          continue;
        }
        if (child.type === "image") {
          const img = child as { url?: string; alt?: string };
          if (!img.url || !FILES_ONLY.test(img.url)) {
            kept.push({ type: "text", value: img.alt ?? "image" });
            continue;                // out-of-lane sources become text
          }
          walk(child);
          kept.push(child);
          continue;
        }
        walk(child);
        kept.push(child);
      }
      node.children = kept;
    }
  };
  walk(root);
  return root;
}

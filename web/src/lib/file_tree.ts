export type TreeFolder<T extends {path: string}> = {
  path: string;
  children: TreeNode<T>[];
  isFolder: true;
};

export type TreeNode<T extends {path: string}> = T | TreeFolder<T>;

export function buildFileTree<T extends {path: string}>(rows: T[]) {
  const byPath = new Map<string, TreeFolder<T>>();
  const allParts = new Set<string>();

  function folderAt(path: string): TreeFolder<T> {
    const existing = byPath.get(path);
    if (existing) return existing;
    const folder: TreeFolder<T> = {path, children: [], isFolder: true};
    // Insert BEFORE descending so siblings share this exact node, and return
    // without a parent for "". The old unconditional folderAt("") recursion
    // overflowed the stack and left every Tree refresh stuck in loading.
    byPath.set(path, folder);
    if (path) {
      const parent = folderAt(path.includes("/")
        ? path.slice(0, path.lastIndexOf("/")) : "");
      parent.children.push(folder);
    }
    return folder;
  }

  const root = folderAt("");
  const sorted = [...rows].sort((a, b) => a.path.localeCompare(b.path));
  for (const row of sorted) {
    const parts = row.path.split("/");
    for (let i = 1; i < parts.length; i++) {
      const cur = parts.slice(0, i).join("/");
      allParts.add(cur);
      folderAt(cur);
    }
    const parent = parts.length > 1
      ? folderAt(parts.slice(0, -1).join("/")) : root;
    parent.children.push(row);
  }

  const orderChildren = (folder: TreeFolder<T>) => {
    folder.children.sort((a, b) =>
      ("isFolder" in a ? 0 : 1) - ("isFolder" in b ? 0 : 1)
      || a.path.localeCompare(b.path));
    for (const child of folder.children) {
      if ("isFolder" in child) orderChildren(child);
    }
  };
  orderChildren(root);
  return {nodes: root.children, allParts};
}

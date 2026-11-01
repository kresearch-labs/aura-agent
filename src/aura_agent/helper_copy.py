"""helper_copy.py — a writing helper works on a copy of the project; its changes
come back through the parent's gates.

Like a pull request: the work happens on a copy, comes back as a list of
changes, and is applied by the parent — through its own write tool, so
approvals, plan mode, checkpoints and skill-pack limits apply — and a file the
parent changed since the copy is a conflict, reported, never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

MAX_FILES = 20_000
MAX_BYTES = 300_000_000


def _skip() -> set:
    from .tools.search import SKIP_DIRS
    return set(SKIP_DIRS) | {".git", ".hg", ".svn"}


def _sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def make_copy(src: Path, dest: Path) -> dict:
    """Copy the project (no dependency/build/VCS folders) and remember each
    file's hash — the base the helper starts from. Returns the base map, or
    raises ValueError when the project is too big to copy."""
    src, dest = Path(src).resolve(), Path(dest)
    skip = _skip()
    base, n, size = {}, 0, 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                st = p.stat()
            except OSError:
                continue
            n += 1
            size += st.st_size
            if n > MAX_FILES or size > MAX_BYTES:
                raise ValueError(f"the project is too big to copy for a writing helper "
                                 f"(over {MAX_FILES:,} files or {MAX_BYTES // 1_000_000} MB)")
            rel = p.relative_to(src).as_posix()
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
            base[rel] = _sha(p)
    (dest.parent / "base.json").write_text(json.dumps(base), encoding="utf-8")
    return base


def changes(copy: Path) -> list[dict]:
    """What the helper changed in its copy: changed, added, deleted files."""
    copy = Path(copy)
    base = json.loads((copy.parent / "base.json").read_text(encoding="utf-8"))
    skip = _skip()
    seen, out = set(), []
    for dirpath, dirnames, filenames in os.walk(copy):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            p = Path(dirpath) / f
            rel = p.relative_to(copy).as_posix()
            seen.add(rel)
            h = _sha(p)
            if rel not in base:
                out.append({"path": rel, "kind": "added", "new": h})
            elif base[rel] != h:
                out.append({"path": rel, "kind": "changed", "base": base[rel], "new": h})
    for rel, h in base.items():
        if rel not in seen:
            out.append({"path": rel, "kind": "deleted", "base": h})
    (copy.parent / "changes.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def describe(chs: list[dict]) -> str:
    if not chs:
        return "no files changed"
    by = {"changed": [], "added": [], "deleted": []}
    for c in chs:
        by[c["kind"]].append(c["path"])
    return "; ".join(f"{k}: {', '.join(v[:8])}{' …' if len(v) > 8 else ''}"
                     for k, v in by.items() if v)


def apply(helper_root: Path, workspace: Path, write) -> str:
    """Apply a helper's changes to the project. write(path, text) is the
    PARENT's own write tool (its gates apply); returns a report."""
    helper_root, workspace = Path(helper_root), Path(workspace)
    f = helper_root / "changes.json"
    if not f.is_file():
        return "this helper has no changes to apply"
    chs = json.loads(f.read_text(encoding="utf-8"))
    copy = helper_root / "copy"
    done, conflicts, skipped = [], [], []
    for c in chs:
        rel = c["path"]
        target = workspace / rel
        if c["kind"] == "deleted":
            skipped.append(f"{rel} (the helper deleted it — delete it yourself if you agree)")
            continue
        now = _sha(target) if target.is_file() else None
        if c["kind"] == "changed" and now != c["base"]:
            conflicts.append(f"{rel} (changed here since the helper's copy)")
            continue
        if c["kind"] == "added" and now is not None and now != c["new"]:
            conflicts.append(f"{rel} (now exists here with other content)")
            continue
        try:
            text = (copy / rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(f"{rel} (not text — copy it yourself)")
            continue
        res = write(rel, text)
        if isinstance(res, dict) and res.get("status") == "error":
            skipped.append(f"{rel} ({str(res.get('error'))[:80]})")
        else:
            done.append(rel)
    out = [f"applied {len(done)} file(s)" + (f": {', '.join(done[:10])}" if done else "")]
    if conflicts:
        out.append("conflicts, not applied: " + "; ".join(conflicts))
    if skipped:
        out.append("not applied: " + "; ".join(skipped))
    return "\n".join(out)

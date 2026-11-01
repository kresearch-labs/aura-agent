"""checkpoints.py — BUILD 2: workspace checkpoint / one-command rollback.

The Replit post-mortem pattern (July 2025): after their agent deleted a
production database, the fixes were structural — automatic snapshots, a
one-click restore, and doc-grounding so the agent KNOWS recovery exists (their
agent lied "rollback impossible"; ours must never have to).

Design: - git-FIRST when the engagement is a repo (git_head recorded in
history), but the snapshot itself is a TAR of the engagement tree — so restore
works even after `.git` is deleted (the append-only sidecar survives). -
history.jsonl is APPEND-ONLY: written once per checkpoint, never edited. -
checkpoints live in `.aura/checkpoints/` INSIDE the engagement; tar
excludes.git/.shadow/venvs/caches to stay small (size guard: skip
auto-snapshots when the tree is huge — checkpointing must never be the thing
that fills the disk). - in a project, the tree snapshotted is the WORKSPACE and
the tars live in the notebook (`store`), so nothing of Aura's lands in the
project. - restore extracts the tar over the engagement root (operator-invoked
via restore, or agent-invoked only after a VERIFY-FAIL / destructive-op failure
— restore is recovery, never a routine tool).

Test-safety: all checkpoint tests run in synthetic sandbox roots.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

CKPT_DIR = ".aura/checkpoints"
HISTORY = "history.jsonl"          # Append-only sidecar
KEEP_LAST = 20                     # Tar pruned to last N; history never pruned
MAX_TREE_BYTES = 200_000_000      # Auto-snapshot skip guard (~200MB)
EXCLUDE_PARTS = {".git", ".shadow", "__pycache__", "node_modules",
                 ".venv", "venv", ".aura/checkpoints"}


def _ckpt_root(root: Path) -> Path:
    return Path(root) / CKPT_DIR


def _git_head(root: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _tree_bytes(root: Path) -> int:
    total = 0
    for p in Path(root).rglob("*"):
        try:
            parts = set(p.relative_to(root).parts)
            if parts & EXCLUDE_PARTS:
                continue
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
        if total > MAX_TREE_BYTES:
            return total
    return total


def create(root: Path, label: str = "", auto: bool = False,
           store: Path | None = None) -> dict:
    """Snapshot the tree at `root` (the workspace) into `store` (the
    notebook; default root). Returns the history record."""
    root = Path(root)
    croot = _ckpt_root(store or root)
    croot.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    cid = f"{ts}-{label}" if label else ts
    tar_path = croot / f"{cid}.tar.gz"

    size = _tree_bytes(root)
    if auto and size > MAX_TREE_BYTES:
        rec = {"id": cid, "ts": time.time(), "label": label, "auto": True,
               "skipped": f"tree too large for auto-snapshot ({size:,}B)",
               "git_head": _git_head(root)}
        _append_history(croot, rec)
        return rec

    with tarfile.open(tar_path, "w:gz") as tf:
        for p in sorted(root.rglob("*")):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if set(rel.parts) & EXCLUDE_PARTS or rel.parts[:2] == ("",):
                continue
            if ".aura" in rel.parts and "checkpoints" in rel.parts:
                continue
            try:
                tf.add(p, arcname=str(rel), recursive=False)
            except OSError:
                continue  # Unreadable file must not kill the checkpoint

    # prune old tars (history.jsonl is NEVER pruned — append-only sidecar)
    tars = sorted(croot.glob("*.tar.gz"))
    for old in tars[:-KEEP_LAST]:
        try:
            old.unlink()
        except OSError:
            pass

    rec = {"id": cid, "ts": time.time(), "label": label, "auto": auto,
           "tar": tar_path.name, "bytes": tar_path.stat().st_size,
           "tree_bytes": size, "git_head": _git_head(root)}
    _append_history(croot, rec)
    return rec


def _append_history(croot: Path, rec: dict) -> None:
    with open(croot / HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def list_checkpoints(root: Path, store: Path | None = None) -> list[dict]:
    h = _ckpt_root(Path(store or root)) / HISTORY
    if not h.exists():
        return []
    out = []
    for ln in h.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def restore(root: Path, cid: str | None = None,
            store: Path | None = None) -> str:
    """Restore the latest (or a given) checkpoint over the engagement root.
    The tar is extracted to a STAGING dir first, then merged in (mv-first:
    files are moved into place, staging removed after — never a partial
    extract straight over live files)."""
    root = Path(root)
    croot = _ckpt_root(store or root)
    recs = [r for r in list_checkpoints(root, store) if r.get("tar")]
    if cid:
        recs = [r for r in recs if r["id"] == cid]
    if not recs:
        return "no checkpoints to restore"
    rec = recs[-1]
    tar_path = croot / rec["tar"]
    if not tar_path.exists():
        return f"checkpoint tar '{rec['tar']}' missing (pruned or deleted)"
    staging = croot / f".staging-{rec['id']}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(staging)  # Trusted: we created this tar ourselves

    moved = 0
    for p in sorted(staging.rglob("*")):
        rel = p.relative_to(staging)
        dest = root / rel
        if p.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))  # Mv-first: move, not delete+write
        moved += 1
    shutil.rmtree(staging, ignore_errors=True)
    return f"restored checkpoint {rec['id']} ({moved} files into {root})"

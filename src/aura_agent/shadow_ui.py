"""shadow_ui.py — diff and undo wired to shadow-git snapshots.

The engine lives in tools/fs.py (_snapshot + restore). This module adds the
operator surface: list snapshots as a timeline, show per-file content diff
(shadow vs live) in unified style, restore with confirmation string.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path


def _shadow_dir(eng: Path) -> Path:
    return Path(eng) / ".shadow"


def list_snapshots(eng: Path, limit: int = 12) -> list[dict]:
    """Newest-first snapshot timeline: {sha, file, ts}."""
    shadow = _shadow_dir(eng)
    out = []
    for d in shadow.glob("*"):
        meta = d / "meta.json"
        if not d.is_dir() or not meta.exists():
            continue
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            out.append({"sha": d.name, "file": m.get("file", "?"),
                        "ts": m.get("ts", 0)})
        except Exception:
            continue
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]


def diff_snapshot(eng: Path, sha: str) -> str:
    """Unified diff: shadowed content (before) vs current file (after)."""
    shadow = _shadow_dir(eng) / sha
    meta = shadow / "meta.json"
    if not sha or not meta.exists():
        return f"no such snapshot: {sha}"
    m = json.loads(meta.read_text(encoding="utf-8"))
    from .paths import workspace_for
    target = workspace_for(eng) / m["file"]
    before = next((f for f in shadow.iterdir() if f.name != "meta.json"), None)
    if before is None:
        return f"snapshot {sha} is empty"
    old = before.read_text(encoding="utf-8", errors="replace").splitlines()
    new = (target.read_text(encoding="utf-8", errors="replace").splitlines()
           if target.exists() else [])
    diff = difflib.unified_diff(old, new,
                                fromfile=f"{m['file']} (snapshot {sha})",
                                tofile=f"{m['file']} (current)", lineterm="")
    return "\n".join(diff) if diff else f"no changes vs snapshot {sha}"


def undo(eng: Path, sha: str | None = None) -> str:
    """Restore latest (or given) snapshot per file."""
    from .tools.fs import restore
    from .paths import workspace_for
    return restore(_shadow_dir(eng), workspace_for(eng), sha)

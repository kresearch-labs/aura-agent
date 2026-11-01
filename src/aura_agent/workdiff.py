"""workdiff.py — "what changed in the project during this task?" without git.

So a file a shell command or a script changed is missed by all of them outside
git.

Aura: at the start of a task, a fingerprint of the project (each file's size
and modification time) plus a copy of the small text files; at the end, the
files that differ — whoever changed them, Aura's tools or a command — with a
real unified diff where the "before" was kept, else a plain line with the
sizes. Bounded: big trees, big files and binary files are fingerprinted or
skipped, never copied; the limits are said in the result. Read-only on the
project; standard library only.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", ".shadow", ".aura", "__pycache__", "node_modules",
             ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             ".next", ".cache", "target", "dist", "build"}
MAX_FILES = 20_000            # Bigger trees: no fingerprint (said, never guessed)
KEEP_FILE = 256 * 1024        # Text files up to this are kept for a real diff
KEEP_TOTAL = 16 * 1024 * 1024
SHOW_LINES = 400              # Longest diff shown per file


def _walk(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_DIRS:
                            stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        yield Path(e.path), e.stat(follow_symlinks=False)
        except OSError:
            continue


def _text(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass
class Baseline:
    root: Path
    files: dict = field(default_factory=dict)      # Rel -> (size, mtime_ns)
    kept: dict = field(default_factory=dict)       # Rel -> text
    too_big: bool = False

    @classmethod
    def take(cls, root: Path) -> "Baseline":
        root = Path(root).resolve()
        b = cls(root)
        total = 0
        for n, (p, st) in enumerate(_walk(root)):
            if n >= MAX_FILES:
                b.too_big, b.files, b.kept = True, {}, {}
                return b
            rel = p.relative_to(root).as_posix()
            b.files[rel] = (st.st_size, st.st_mtime_ns)
            if st.st_size <= KEEP_FILE and total + st.st_size <= KEEP_TOTAL:
                try:
                    t = _text(p.read_bytes())
                except OSError:
                    t = None
                if t is not None:
                    b.kept[rel] = t
                    total += st.st_size
        return b


def changes(b: Baseline) -> tuple[str, list[str]]:
    """(unified diff text, one line per changed file). Empty both = nothing
    changed. A file whose size and time are the same is taken as the same."""
    if b.too_big:
        return "", []
    now = {p.relative_to(b.root).as_posix(): st for p, st in _walk(b.root)}
    parts: list[str] = []
    listed: list[str] = []
    for rel in sorted(set(b.files) | set(now)):
        before = b.files.get(rel)
        st = now.get(rel)
        if before and st and (st.st_size, st.st_mtime_ns) == before:
            continue
        old = b.kept.get(rel) if before else ""
        new: str | None = ""
        if st:
            try:
                new = _text((b.root / rel).read_bytes()) if st.st_size <= 4 * KEEP_FILE else None
            except OSError:
                new = None
        if before and st and old is not None and new is not None and old == new:
            continue                              # Touched, not changed
        what = "added" if not before else "deleted" if not st else "changed"
        if old is not None and new is not None:
            diff = list(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="/dev/null" if not before else f"a/{rel}",
                tofile="/dev/null" if not st else f"b/{rel}"))
            if len(diff) > SHOW_LINES:
                diff = diff[:SHOW_LINES] + [f"... ({len(diff) - SHOW_LINES} more diff lines)\n"]
            parts.append("".join(d if d.endswith("\n") else d + "\n" for d in diff))
            listed.append(f"{what}: {rel}")
        else:
            sizes = f"{before[0] if before else 0} -> {st.st_size if st else 0} bytes"
            parts.append(f"Binary or large file {what}: {rel} ({sizes})\n")
            listed.append(f"{what}: {rel} ({sizes}, no line diff)")
    return "".join(parts), listed

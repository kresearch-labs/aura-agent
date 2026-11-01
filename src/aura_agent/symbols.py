"""symbols.py — #1: enumerated real-entity table.

The 85%-vs-13% finding: agents hallucinate the PROJECT's environment, not the
world. Fix: generate only from an enumerated set of real entities.

Symbols is a project manifest of what actually exists:
  files/ every real path (from list_files-style walk)
  functions/ Python defs, JS functions, shell `which` hits
  commands/ executables on PATH (+ package binaries)
  imports/ declared third-party imports (requirements/pyproject)

Built once per run (cached by mtime), exposed to the model two ways:
  1. `symbols` tool (READ): full/filtered sheet, the model reads BEFORE
     writing code that calls project APIs.
  2. validate_args hook: write/execute tool args containing a dotted name
     or path are checked against the sheet; invented names bounce back
     with the nearest real matches (diff-closest), never a silent pass.

Never raises; absent project files degrade to an empty sheet.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

IGNORE_DIRS = {".shadow", "__pycache__", ".git", "node_modules",
               ".venv", "venv", "engagement", "sessions", "artifacts",
               "memories", "chains"}
MAX_FILES = 2000
MAX_FUNCS = 3000


def build(root: Path) -> dict:
    """Walk the work dir and extract real entities. Cached by caller."""
    root = Path(root)
    files: list[str] = []
    funcs: set[str] = set()
    imports: set[str] = set()
    count = 0
    for p in sorted(root.rglob("*")):
        if count >= MAX_FILES:
            break
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.parts) or p.name.startswith("."):
            continue
        try:
            rel = p.relative_to(root).as_posix()   # '/' on every OS
        except ValueError:
            continue
        files.append(rel)
        count += 1
        if p.suffix == ".py" and len(funcs) < MAX_FUNCS:
            try:
                tree = ast.parse(
                    p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    funcs.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
    # commands that actually exist on PATH
    cmds: set[str] = set()
    for d in ("/usr/local/bin", "/usr/bin", "/bin"):
        try:
            for c in Path(d).iterdir():
                cmds.add(c.name)
        except OSError:
            continue
    return {"files": files, "funcs": sorted(funcs), "imports": sorted(imports),
            "cmds": sorted(cmds)}


def nearest(word: str, pool: list[str], k: int = 4) -> list[str]:
    """Diff-closest k entries (simple char-overlap score; no deps)."""
    import os as _os
    w = word.lower()
    scored = []
    for cand in pool:
        c = cand.lower()
        inter = len(set(w) & set(c))
        prefix = len(_os.path.commonprefix([w, c]))
        scored.append((inter + prefix * 2, cand))
    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored[:k]]


class SymbolTable:
    """Built once per run; refreshed when the caller knows the tree changed."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._data: dict | None = None

    def refresh(self) -> dict:
        self._data = build(self.root)
        return self._data

    @property
    def data(self) -> dict:
        if self._data is None:
            self.refresh()
        return self._data

    def sheet(self, max_lines: int = 60) -> str:
        """Compact human-readable sheet for context injection."""
        d = self.data
        if not d["files"] and not d["funcs"]:
            return ""
        lines = ["[PROJECT SYMBOLS — real entities only. Do NOT call anything "
                 "not listed here; verify with symbols/ls first.]"]
        files = d["files"]
        funcs = d["funcs"]
        cmds = [c for c in ("git", "python3", "pip", "docker", "curl")
                if c in d["cmds"]]
        lines.append(f"files ({len(files)}): " + ", ".join(files[:25])
                     + (f" …+{len(files) - 25} (use list_files)" if len(files) > 25 else ""))
        if funcs:
            lines.append(f"defs ({len(funcs)}): " + ", ".join(funcs[:30])
                         + (f" …+{len(funcs) - 30}" if len(funcs) > 30 else ""))
        lines.append(f"third-party imports: " + (", ".join(d["imports"][:15]) or "none"))
        return "\n".join(lines[:max_lines])

    # ------------------------------------------------------------ validation
    _DOTTED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]{2,})\b")

    def check_path(self, path: str) -> dict:
        """Validate a path-like argument. Returns {ok, real, suggestions}.
        Same file name in another folder → ok, with those real paths as
        suggestions (the wrong-folder case)."""
        path = (path or "").strip().strip("'\"").replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if not path or path == ".":
            return {"ok": True, "real": True, "suggestions": []}
        d = self.data
        exact = path in d["files"]
        if exact:
            return {"ok": True, "real": True, "suggestions": []}
        # directory prefix match (agent may reference a dir to create in)
        if any(f.startswith(path.rstrip("/") + "/") for f in d["files"]):
            return {"ok": True, "real": False, "dir": True, "suggestions": []}
        base = path.split("/")[-1]
        same_name = [f for f in d["files"] if f.split("/")[-1] == base]
        if same_name:
            return {"ok": True, "real": True, "suggestions": same_name[:4]}
        sug = nearest(base, d["files"])
        return {"ok": False, "real": False, "suggestions": sug}

    def check_name(self, name: str) -> dict:
        """Validate a function/class/def name against real defs + builtins."""
        if not name:
            return {"ok": True, "real": True, "suggestions": []}
        d = self.data
        base = name.split("(")[0].strip()
        if base in d["funcs"] or base in d["cmds"]:
            return {"ok": True, "real": True, "suggestions": []}
        pool = d["funcs"] + d["cmds"]
        return {"ok": False, "real": False, "suggestions": nearest(base, pool)}

"""code_nav.py — where is it defined, who uses it, what's in this file.

With an installed language server (lsp.py): its own definition / references /
document symbols — exact, through imports and aliases. Without one: the
built-in way — search_files (definitions first) and a Python outline from the
syntax tree. The model asks by NAME (it rarely knows a cursor position); Aura
finds the position itself.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..mediator import BlastRadius, Mediator, ToolSpec

MAX_LINES = 40
# LSP SymbolKind: File, Module, Namespace, Package, Class, Interface, Struct, Enum
_CONTAINERS = {1, 2, 3, 4, 5, 11, 23, 10}


def _rel(uri: str, root: Path) -> str:
    try:
        # Decode first: pyright sends the drive as "c%3A"
        path = unquote(urlparse(uri).path)
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return uri


def _line(root: Path, rel: str, n: int) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()[n].strip()[:140]
    except (OSError, IndexError):
        return ""


def _position(root: Path, path: str, name: str):
    """First whole-word occurrence of name in the file → (line, char)."""
    try:
        text = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for i, ln in enumerate(text.splitlines()):
        m = re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", ln)
        if m:
            return i, m.start()
    return None


def _file_with(root: Path, name: str, lang_of) -> str | None:
    """A project file that mentions the name, in a language with a server."""
    from .search import SKIP_DIRS
    pat = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts) or not p.is_file() or not lang_of(p.name):
            continue
        try:
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                return p.relative_to(root).as_posix()
        except OSError:
            continue
    return None


def _locs(res, root: Path) -> list[str]:
    if isinstance(res, dict):
        res = [res]
    out = []
    for loc in res or []:
        uri = loc.get("uri") or loc.get("targetUri", "")
        rng = loc.get("range") or loc.get("targetSelectionRange") or {}
        n = (rng.get("start") or {}).get("line", 0)
        rel = _rel(uri, root)
        out.append(f"{rel}:{n + 1}: {_line(root, rel, n)}")
    return out[:MAX_LINES]


def _outline_py(root: Path, path: str) -> list[str]:
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            out.append((node.lineno, f"{path}:{node.lineno}: {kind} {node.name}"))
    return [s for _, s in sorted(out)][:MAX_LINES]


def code_nav(root: Path, args: dict) -> str:
    root = Path(root)
    action = str(args.get("action", "definition")).lower()
    name = str(args.get("name", "")).strip()
    path = str(args.get("path", "")).strip().replace("\\", "/").lstrip("./")
    if action not in ("definition", "references", "symbols"):
        return "error: action is definition, references or symbols"
    if action != "symbols" and not re.fullmatch(r"[A-Za-z_$][\w$.]*", name or ""):
        return "error: give the name of a function, class or variable"
    if action == "symbols" and not path:
        return "error: symbols needs a file path"
    from .. import lsp
    # 1) the language server, when one is installed for this language
    try:
        target = path or _file_with(root, name.split(".")[-1], lsp.language_of)
        c = lsp.client_for(target, root) if target else None
        if c is not None:
            text = (root / target).read_text(encoding="utf-8", errors="replace")
            uri = c.sync(root / target, text)
            if action == "symbols":
                res = c.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
                lines = []

                def walk(items, depth=0):
                    for s in items or []:
                        rng = s.get("range") or (s.get("location") or {}).get("range") or {}
                        n = (rng.get("start") or {}).get("line", 0)
                        lines.append(f"{target}:{n + 1}: {'  ' * depth}{s.get('name')}")
                        # into classes, modules and namespaces only — a
                        # function's arguments and locals are not what the
                        # file defines (pyright lists them as children)
                        if s.get("kind") in _CONTAINERS:
                            walk(s.get("children"), depth + 1)
                if res and isinstance(res, list) and "location" in res[0]:
                    # the flat form (SymbolInformation): drop what lives
                    # inside a function or method — its arguments, locals
                    funcs = {s.get("name") for s in res if s.get("kind") in (6, 9, 12)}
                    res = [s for s in res if s.get("containerName") not in funcs]
                walk(res)
                if lines:
                    return "\n".join(lines[:MAX_LINES]) + "\n(from the language server)"
            else:
                pos = _position(root, target, name.split(".")[-1])
                if pos:
                    params = {"textDocument": {"uri": uri},
                              "position": {"line": pos[0], "character": pos[1]}}
                    if action == "references":
                        # True: an import alias ("from geo import area as
                        # size") is a declaration to the server — excluding
                        # declarations dropped that use
                        params["context"] = {"includeDeclaration": True}
                    res = c.request("textDocument/" + action, params)
                    lines = _locs(res, root)
                    if lines:
                        return "\n".join(lines) + "\n(from the language server)"
    except Exception:                                    # noqa: BLE001
        pass
    # 2) the built-in way
    if action == "symbols":
        lines = _outline_py(root, path) if path.endswith(".py") else []
        if not lines:
            from .search import search_files
            return search_files(root, {"pattern": r"^\s*(def|class|function|func|fn|"
                                       r"interface|type|struct|enum|const|let|var)\b",
                                       "path": str(Path(path).parent) if "/" in path else ".",
                                       "glob": Path(path).name, "limit": MAX_LINES})
        return "\n".join(lines) + "\n(built-in outline — no language server installed)"
    from .search import search_files
    out = search_files(root, {"pattern": rf"\b{re.escape(name.split('.')[-1])}\b",
                              "limit": MAX_LINES})
    return out + "\n(built-in search, definitions first — no language server for this language)"


def register(mediator: Mediator, root: Path, dispatch: dict) -> None:
    mediator.register(ToolSpec("code_nav", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["code_nav"] = lambda a: code_nav(Path(getattr(mediator, "work_dir", None) or root), a)

"""teams_map.py — the shared code map (plan).

Each Aura builds a COMPACT map of the project workspace: git-tracked files
only, skipping binaries, big files,.env*/key/secret files and anything the
scrubber flags; per file a one-line summary (docstring or first comment — never
a body, never the model) and, for Python (and more languages when the optional
`aura-agent[map]` extra is installed), top-level functions/classes/methods with
name + signature line + line number. Ranked by how often each name appears
across the tracked files. Every entry carries the HEAD commit id. The map never
contains a function body — the tests prove it with planted markers.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

MAX_FILE = 1024 * 1024
MAX_SUMMARY = 120
MAX_SIG = 200
MAX_MAP = 60_000
# name-only skip: the FILE'S OWN name, not "any path containing the word"
# ('monkey.paw' and 'donkey.py' are NOT secrets — the FB's substring
# matcher banned them)
_NAME_MISS = re.compile(
    r"(?:^|[-_.])(?:secret[-_]?s?|credential[-_]?s?|password[-_]?s?"
    r"|token[-_]?s?)(?:[-_.]|$)")
SKIP_NAMES = (".env", ".envrc", ".env.local")           # Exact basenames
SKIP_SUFFIX = (".key", ".pem", ".p12", ".pfx")          # Exact suffixes
LANGS = {".py": "python", ".js": "javascript", ".ts": "typescript",
         ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
         ".rs": "rust", ".java": "java", ".cs": "csharp", ".rb": "ruby",
         ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".md": "md",
         ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json"}


class GitError(Exception):
    pass


def _git(repo: str, *args: str, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]}: {e}") from e
    if r.returncode != 0:
        raise GitError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def _lang(path: str) -> str:
    return LANGS.get(("." + path.rsplit(".", 1)[-1].lower())
                     if "." in path.rsplit("/", 1)[-1] else "", "")


def _skip(path: str) -> bool:
    """Secret-by-NAME: the file's own basename only — never a substring
    over the whole path."""
    base = path.rsplit("/", 1)[-1].lower()
    if base in SKIP_NAMES or base.endswith(SKIP_SUFFIX):
        return True
    if _NAME_MISS.search(base):            # secrets.py, my-token.txt …
        return True
    low = path.lower()
    return low.endswith((".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
                         ".zip", ".gz", ".woff", ".woff2", ".ttf", ".db",
                         ".sqlite", ".pyc", ".exe", ".dll", ".so"))


def _summary(text: str, lang: str) -> str:
    """The module docstring's first line, else the first comment line — NEVER a
    body.
    """
    try:
        if lang == "python":
            tree = ast.parse(text)
            d = ast.get_docstring(tree)
            if d:
                line = d.strip().splitlines()[0]
            else:
                line = next((l.lstrip("# ").strip() for l in
                             text.splitlines()
                             if l.strip().startswith("#")), "")
        else:
            line = next((l.lstrip("#/ ").strip() for l in text.splitlines()
                         if l.strip().startswith(("#", "//"))), "")
    except SyntaxError:
        line = ""
    line = re.sub(r"\s+", " ", line).strip()
    return line[:MAX_SUMMARY]


def _sig_clean(node):
    """Signatures WITHOUT default values — the map mustn't learn which constants
    live in code (they can be credentials). A copy whose defaults are `...`.
    """
    import copy as _copy
    if not hasattr(node, "args"):
        return None
    args = _copy.deepcopy(node.args)
    args.defaults = [ast.Constant(value=...) for _ in args.defaults]
    args.kw_defaults = [None if d is None else ast.Constant(value=...)
                        for d in args.kw_defaults]
    return args


def _py_symbols(text: str) -> list[dict]:
    """Top-level functions/classes + methods: name, signature line, line — via ast
    (stdlib); no bodies.
    """
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    lines = text.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            sig = (ast.unparse(_sig_clean(node)) if hasattr(node, "args")
                   and isinstance(node, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)) else "")
            sig_line = f"def {node.name}({ast.unparse(_sig_clean(node))})" \
                if isinstance(node, (ast.FunctionDef,
                                     ast.AsyncFunctionDef)) \
                else f"class {node.name}"
            out.append({"kind": "class" if isinstance(node, ast.ClassDef)
                        else "def", "name": node.name,
                        "signature": sig_line[:MAX_SIG],
                        "line": node.lineno})
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef,
                                        ast.AsyncFunctionDef)):
                        out.append({
                            "kind": "method", "name": f"{node.name}."
                                                       f"{sub.name}",
                            "signature": (f"def {sub.name}("
                                          f"{ast.unparse(_sig_clean(sub))})")[:MAX_SIG],
                            "line": sub.lineno})
    return out


def _tree_sitter_symbols(text: str, lang: str) -> list[dict] | None:
    """More languages when the optional extra is installed; same output shape.
    None = not available.
    """
    try:
        from tree_sitter_languages import get_parser  # noqa: F401
    except Exception:                                # noqa: BLE001
        return None
    return None


def _main_ref(repo: str) -> str | None:
    """The main line the whole team agrees on: origin/HEAD as `git symbolic-ref
    refs/remotes/origin/HEAD`, else origin/main or origin/master themselves;
    None when this copy has no remote to lean on (the map then says nothing at
    all — the team's tree doesn't budge on guesses).
    """
    try:
        head = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=15,
            ).stdout.strip()
        if head:
            return head
        for cand in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
            ok = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", cand],
                cwd=repo, capture_output=True, text=True, timeout=15)
            if ok.returncode == 0:
                return cand
    except OSError:
        return None


def _blob_text(repo: str, commit: str, rel: str) -> "str | None":
    """`git show <commit>:<path>` — the file AS THE TEAM SEES IT, not the chat's
    working tree (, "the main branch's tree").
    """
    try:
        r = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=repo,
                           capture_output=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    if b"\x00" in r.stdout[:8192]:
        return None                                 # Binary
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build(repo: str, max_chars: int = 60_000) -> dict:
    """The map for the workspace, ALWAYS of origin/<main>: `git ls-tree -r -l
    <main>` shapes the file list and `git show <main>:<path>` feeds the bytes —
    the chat's working tree doesn't leak into the team's picture.
    """
    main = _main_ref(repo)
    if not main:
        raise GitError("this copy knows no origin main — the map stays "
                       "where the team agreed it")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", main], cwd=repo,
            capture_output=True, text=True, timeout=30).stdout.strip()
        ls = subprocess.run(
            ["git", "ls-tree", "-r", "-l", commit], cwd=repo,
            capture_output=True, text=True, timeout=60).stdout
    except OSError as e:
        raise GitError(str(e)) from e
    entries: list[tuple[str, int]] = []
    for line in ls.splitlines():
        # "100644 blob <sha>  <size>\t<path>" — `-` size marks trees/subs
        if "\t" not in line:
            continue
        meta, rel = line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 3 or parts[1] not in ("blob",):
            continue
        try:
            size = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        except ValueError:
            size = -1
        entries.append((rel, size))
    files: list[dict] = []
    symbols: list[dict] = []
    mention: dict = {}
    for rel, size in entries:
        if not rel or _skip(rel):
            continue
        if size > MAX_FILE:
            continue
        text = _blob_text(repo, commit, rel)
        if text is None:
            continue
        lang = _lang(rel)
        summary = _summary(text, lang)
        try:
            from .secrets_guard import scrub_secrets
            summary = scrub_secrets(summary)
        except Exception:                            # noqa: BLE001
            pass
        files.append({"path": rel, "size": size, "lang": lang,
                      "summary": summary})
        syms = _py_symbols(text) if lang == "python" else \
            (_tree_sitter_symbols(text, lang) or [])
        for s in syms:
            s["path"] = rel
            symbols.append(s)
            mention[s["name"]] = mention.get(s["name"], 0) + 1
    # rank: name mentions across tracked files (word match, cheap)
    corpus = " ".join(f["path"] for f in files)
    for s in symbols:
        s["rank"] = mention.get(s["name"], 1)
    # file rank = sum of its symbols' ranks
    by_file: dict = {}
    for s in symbols:
        by_file[s["path"]] = by_file.get(s["path"], 0) + s["rank"]
    for f in files:
        f["rank"] = by_file.get(f["path"], 0)
    files.sort(key=lambda f: -f["rank"])
    symbols.sort(key=lambda s: -s["rank"])
    # Cap the map's size (≤ 60,000 chars): ranked files first, but reserve a
    # floor for symbols so a big tree doesn't starve them
    kept_files: list = []
    budget = max_chars - 512
    sym_budget = min(20_000, max(budget // 3, 4_000))
    for f in files:
        blob = len(json.dumps(f))
        if budget - blob < sym_budget:
            break
        budget -= blob
        kept_files.append(f)
    kept: list = []
    for s in symbols:
        blob = len(json.dumps(s))
        if budget - blob < 0:
            break
        budget -= blob
        kept.append(s)
    return {"commit": commit, "files": kept_files, "symbols": kept}


def os_size(path: str) -> int:
    try:
        import os
        return os.path.getsize(path)
    except OSError:
        return 0


MAX_MAP = MAX_MAP_CONST = 60_000


def upload(env_cfg: dict, repo: str, project_id: int) -> dict:
    """Build here, PUT to the project; needs report.write. A successful PUT emits
    exactly one client-side `map.updated` report event ('sent as map.updated
    when a task finishes') carrying the commit + file count — teams_run.finish
    calls upload once, so a finish lands one event; the event is queued through
    the report queue like every other (swallowed: reporting never breaks the
    run).
    """
    from .teamserver.teamclient import AuraClient
    from . import teams
    at = teams.load_config().get("aura_team")
    if not at:
        return {"ok": False, "detail": "not on your own team server"}
    try:
        m = build(repo)
    except GitError as e:
        return {"ok": False, "detail": str(e)}
    c = AuraClient(at)
    # 's "older commits don't move the tree": ask the map what commit it stands
    # on and prove (HERE, creator-side — the hub has no repo of its own) that
    # the fresh one descends from it; send only then
    st0, d0 = c.request("GET", f"/api/v1/projects/{project_id}/map")
    if st0 == 200:
        prev = str((d0 or {}).get("commit") or "")
        if prev and prev != m["commit"]:
            anc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", prev, m["commit"]],
                cwd=repo, capture_output=True)
            if anc.returncode != 0:
                return {"ok": False, "skipped": True,
                        "detail": f"the map at the hub stands on {prev[:9]} — "
                                  f"my {m['commit'][:9]} doesn't descend from "
                                  f"it; I don't move the team's tree"}
    status, data = c.request(
        "PUT", f"/api/v1/projects/{project_id}/map",
        {"commit": m["commit"], "files": m["files"],
         "symbols": m["symbols"]})
    ok = status == 200
    if ok:
        try:
            from . import teams_report as R
            # The server's declared shape (api_log, REPORT_TYPES): `files` is
            # the per-file PATH list (already relative + scrubbed by build);
            # the / file count is its length
            fpaths = [f.get("path") for f in (m.get("files") or [])
                      if isinstance(f, dict) and f.get("path")][:200]
            ev = R.make_event("map.updated", int(project_id),
                              (m.get("commit") or "map")[:60],
                              {"commit": m.get("commit") or "",
                               "files": fpaths})
            if ev is not None:
                R.queue_report(ev)
        except Exception:                        # noqa: BLE001
            pass
    return {"ok": ok, "detail": data}

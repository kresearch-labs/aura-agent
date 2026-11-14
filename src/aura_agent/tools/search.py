"""search.py — find things in the project: text inside files, or files by name.

What Aura's search does about it:

  - ripgrep when it's installed (fast, .gitignore-aware), plain Python
    otherwise — with the same skips: .gitignore, dependency and build
    folders, binary and huge files;
  - results grouped by file, the file where the name is DEFINED first;
  - every line comes with its tag (`12:a3|`) — usable directly by
    edit_file(lines=...);
  - a size budget: beyond it, the remaining files as counts and a hint to
    narrow — one broad search never floods the conversation;
  - files read_file would refuse (secrets) are never shown;
  - nothing found → says what was searched and close names that do exist.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from ..tool_names import SEARCH_FILES

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
             "build", "target", ".idea", ".vscode", ".next", ".nuxt", "coverage",
             ".gradle", ".shadow", ".cache", "site-packages", ".eggs"}
MAX_FILE_BYTES = 2_000_000
BUDGET_CHARS = 6_000
PER_FILE_SHOWN = 8

# a line that DEFINES a name, in most languages
_DEF = (r"^\s*(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:static\s+)?"
        r"(?:def|class|function|fn|func|struct|enum|trait|interface|type|impl|module|"
        r"const|let|var|val|object|record|protocol|macro|sub)\s+")


def _rg() -> str | None:
    return shutil.which("rg")


# ---------------------------------------------------------------- ignore --
def _gitignore(root: Path) -> list[tuple[str, bool]]:
    """(pattern, dir_only) from the project's top .gitignore (no negation)."""
    out = []
    try:
        for ln in (root / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s or s.startswith(("#", "!")):
                continue
            out.append((s.rstrip("/").lstrip("/"), s.endswith("/")))
    except OSError:
        pass
    return out


def _ignored(rel: str, is_dir: bool, pats: list[tuple[str, bool]]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pat, dir_only in pats:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) \
                or fnmatch.fnmatch(rel, pat + "/*"):
            return True
    return False


def _walk(root: Path, sub: Path):
    """Project files under sub (relative posix paths), skipping what ripgrep
    would skip."""
    pats = _gitignore(root)
    for dp, dns, fns in os.walk(sub):
        d = Path(dp)
        rel_d = d.relative_to(root).as_posix()
        rel_d = "" if rel_d == "." else rel_d
        dns[:] = [n for n in dns if n not in SKIP_DIRS and not n.startswith(".")
                  and not _ignored(f"{rel_d}/{n}".lstrip("/"), True, pats)]
        for f in fns:
            rel = f"{rel_d}/{f}".lstrip("/")
            if not _ignored(rel, False, pats):
                yield rel


def _secret(rel: str) -> bool:
    try:
        from ..secrets_guard import is_never_read
        return is_never_read(rel)
    except Exception:                                # noqa: BLE001
        return True                                  # Fail closed


def _globs_ok(rel: str, globs: list[str]) -> bool:
    if not globs:
        return True
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in globs)


# --------------------------------------------------------------- content --
def _content_rg(root: Path, sub: str, pattern: str, literal: bool, case: str,
                globs: list[str], context: int, cap: int) -> list[dict]:
    # No-require-git: honour.gitignore in any folder, not only inside a git
    # repository --hidden + "!.*/": look at hidden FILES (.env …) and skip
    # hidden FOLDERS, like the built-in search — ripgrep skips dot-files by
    # default, so a secret file that matched was never reported as hidden;
    # _secret below keeps it out of the results
    cmd = [_rg(), "--json", "--max-columns", "400", "--max-filesize", "2M",
           "-m", "200", "--no-require-git", "--hidden", "--glob", "!.*/"]
    cmd += ["-F"] if literal else []
    cmd += {"insensitive": ["-i"], "sensitive": ["-s"]}.get(case, ["-S"])
    for g in globs:
        cmd += ["--glob", g]
    for d in sorted(SKIP_DIRS):
        cmd += ["--glob", f"!{d}/"]
    if context:
        cmd += ["-C", str(context)]
    cmd += ["-e", pattern, "--", sub or "."]
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise Violation("search took over 30 s — narrow it with path= or glob=")
    if r.returncode == 2 and not r.stdout:
        raise ValueError((r.stderr or "ripgrep error").strip().splitlines()[-1][:300])
    hits: list[dict] = []
    for ln in r.stdout.splitlines():
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if ev.get("type") not in ("match", "context"):
            continue
        d = ev["data"]
        path = (d.get("path") or {}).get("text", "")
        text = (d.get("lines") or {}).get("text", "").rstrip("\r\n")
        rel = Path(path).as_posix().removeprefix("./")
        hits.append({"file": rel, "line": d.get("line_number") or 0, "text": text,
                     "match": ev["type"] == "match"})
        if sum(1 for h in hits if h["match"]) >= cap:
            break
    return hits


def _content_py(root: Path, sub: Path, pattern: str, literal: bool, case: str,
                globs: list[str], context: int, cap: int) -> list[dict]:
    flags = 0
    if case == "insensitive" or (case == "smart" and pattern == pattern.lower()):
        flags |= re.IGNORECASE
    rx = re.compile(re.escape(pattern) if literal else pattern, flags)
    hits: list[dict] = []
    matches = 0
    for rel in _walk(root, sub):
        if not _globs_ok(rel, globs):
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue                                  # Binary
        lines = data.decode("utf-8", errors="replace").splitlines()
        want: dict[int, bool] = {}
        for i, line in enumerate(lines):
            if rx.search(line):
                want[i] = True
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    want.setdefault(j, False)
                matches += 1
                if matches >= cap:
                    break
        for i in sorted(want):
            hits.append({"file": rel, "line": i + 1, "text": lines[i][:400], "match": want[i]})
        if matches >= cap:
            break
    return hits


def _def_rx(pattern: str):
    """A line defining the searched name: def/class/fn/… NAME, or NAME = …"""
    m = re.fullmatch(r"\s*(?:def\s+|class\s+)?([A-Za-z_][\w.]*)\s*\(?\s*", pattern)
    if not m:
        return None
    name = re.escape(m.group(1).split(".")[-1])
    return re.compile(rf"(?:{_DEF}\W*{name}\b)|(?:^\s*(?:self\.)?{name}\s*(?::[^=\n]+)?=(?!=))")


def _render_content(pattern: str, hits: list[dict], engine: str, budget: int,
                    capped: bool) -> str:
    from .fs import line_tag, line_tags_on
    tags = line_tags_on()
    by: dict[str, list[dict]] = {}
    for h in hits:
        by.setdefault(h["file"], []).append(h)
    defrx = _def_rx(pattern)

    def is_def(h):
        return bool(h["match"] and defrx and defrx.search(h["text"]))

    def fscore(item):
        f, hs = item
        n = sum(1 for h in hs if h["match"])
        return (-(any(is_def(h) for h in hs)), -n, f)

    files = sorted(by.items(), key=fscore)
    total = sum(1 for h in hits if h["match"])
    head = (f"search {pattern!r} — {total}{'+' if capped else ''} match"
            f"{'es' if total != 1 else ''} in {len(files)} file{'s' if len(files) != 1 else ''}"
            f" ({engine})")
    out = [head]
    used = len(head)
    rest = []
    for f, hs in files:
        n = sum(1 for h in hs if h["match"])
        block = [f"{f} ({n})"]
        shown = 0
        defs_first = sorted(hs, key=lambda h: (not is_def(h), h["line"])) \
            if not any(not h["match"] for h in hs) else hs
        for h in defs_first:
            if h["match"]:
                shown += 1
                if shown > PER_FILE_SHOWN:
                    continue
            pre = f"{h['line']}:{line_tag(h['text'])}" if tags else str(h["line"])
            mark = "|" if h["match"] else "-"
            block.append(f"  {pre}{mark} {h['text'].strip()[:200]}"
                         + ("   ← defined here" if is_def(h) else ""))
        if shown > PER_FILE_SHOWN:
            block.append(f"  … +{shown - PER_FILE_SHOWN} more in this file")
        text = "\n".join(block)
        if used + len(text) > budget and len(out) > 1:
            rest.append(f"{f} ({n})")
            continue
        out.append(text)
        used += len(text)
    if rest:
        out.append(f"also matching ({len(rest)} files, not shown): " + ", ".join(rest[:40])
                   + (" …" if len(rest) > 40 else ""))
        out.append("narrow it: path='folder', glob='*.py', or a more exact pattern")
    elif capped:
        out.append("stopped at the match limit — narrow it or raise limit=")
    return "\n".join(out)


# ----------------------------------------------------------------- names --
def _score_name(rel: str, q: str) -> float:
    r, base = rel.lower(), rel.lower().rsplit("/", 1)[-1]
    q = q.lower()
    if base == q:
        return 100
    if base.startswith(q):
        return 80
    if q in base:
        return 60
    if q in r:
        return 40
    it = iter(r)                                     # Letters in order (fuzzy)
    if all(c in it for c in q):
        return 10 + 10 * len(q) / max(1, len(r))
    return 0


def _names(root: Path, sub: Path, query: str, globs: list[str], limit: int) -> str:
    rels = [r for r in _walk(root, sub) if _globs_ok(r, globs) and not _secret(r)]
    scored = []
    for r in rels:
        s = _score_name(r, query) if query not in ("", "*") else 1
        if s:
            try:
                mt = (root / r).stat().st_mtime
            except OSError:
                mt = 0
            scored.append((s, mt, r))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    if not scored:
        return f"no file name matches {query!r} (searched {len(rels)} files)"
    # no "changed N min ago": the proof step replays reads and compares
    # the output, which must not change just because time passed
    lines = [f"files matching {query!r} — {len(scored)} found, best match first "
             "(ties: newest first)" + (f", {limit} shown" if len(scored) > limit else "")]
    for _s, _mt, r in scored[:limit]:
        lines.append(f"  {r}")
    return "\n".join(lines)


# ------------------------------------------------------------------ tool --
def matches_of(hits: list[dict]) -> int:
    return sum(1 for h in hits if h["match"])


def search_files(root: Path, args: dict) -> str:
    from .fs import _base, _resolve_in_root
    pattern = str(args.get("pattern") or args.get("query") or "")
    mode = str(args.get("in") or args.get("target") or "content").lower()
    sub = _resolve_in_root(root, str(args.get("path") or "."), read=True)
    if not sub.exists():
        raise FileNotFoundError(f"{args.get('path')} doesn't exist in the project")
    root = _base(root, sub)          # An allowed folder outside the project
    g = args.get("glob") or []
    globs = [g] if isinstance(g, str) else [str(x) for x in g]
    try:
        limit = max(1, min(500, int(args.get("limit") or (40 if mode != "names" else 30))))
    except (TypeError, ValueError):
        limit = 40
    if mode in ("names", "files", "name", "file"):
        return _names(root, sub, pattern, globs, limit)
    if not pattern:
        raise ValueError("give pattern= (text or a regular expression)")
    literal = bool(args.get("literal"))
    note = ""
    if not literal:
        try:
            re.compile(pattern)
        except re.error as e:
            literal, note = True, f" (not a valid regular expression — {e}; searched as plain text)"
    case = str(args.get("case") or "smart").lower()
    try:
        context = max(0, min(5, int(args.get("context") or 0)))
    except (TypeError, ValueError):
        context = 0
    rel_sub = sub.relative_to(root.resolve()).as_posix() if sub != root.resolve() else "."
    engine = "ripgrep" if _rg() else "built-in search"
    try:
        hits = (_content_rg(root, rel_sub, pattern, literal, case, globs, context, limit)
                if _rg() else
                _content_py(root, sub, pattern, literal, case, globs, context, limit))
    except ValueError:
        if literal:
            raise
        literal, note = True, " (searched as plain text — the pattern didn't work as a regular expression)"
        hits = (_content_rg(root, rel_sub, pattern, literal, case, globs, context, limit)
                if _rg() else
                _content_py(root, sub, pattern, literal, case, globs, context, limit))
    # for a plain name: find where it is defined too, even past the cap
    drx = _def_rx(pattern) if not literal or re.fullmatch(r"[\w.]+", pattern) else None
    if drx is not None and matches_of(hits) >= limit:
        try:
            defs = _content_py(root, sub, drx.pattern, False, "sensitive", globs, 0, 10)
            have = {(h["file"], h["line"]) for h in hits}
            hits = [d for d in defs if (d["file"], d["line"]) not in have] + hits
        except (re.error, OSError):
            pass
    hidden = {h["file"] for h in hits if _secret(h["file"])}
    hits = [h for h in hits if h["file"] not in hidden]
    matches = sum(1 for h in hits if h["match"])
    if not matches:
        try:
            n_files = sum(1 for p in sub.rglob("*") if p.is_file()
                          and not any(x in SKIP_DIRS for x in p.relative_to(sub).parts))
        except OSError:
            n_files = 0
        msg = (f"no match for {pattern!r}{note} in {rel_sub if rel_sub != '.' else 'the project'}"
               f"{' (' + ', '.join(globs) + ')' if globs else ''} ({n_files} files there) — "
               ".gitignore and dependency/build folders were skipped. A mistyped pattern "
               "looks the same as a missing thing: check the pattern before saying it "
               "isn't there")
        try:
            from ..symbols import SymbolTable, nearest
            word = re.sub(r"[^\w]", "", pattern)
            if word:
                import difflib
                near = [n for n in nearest(word, SymbolTable(root).data["funcs"])
                        if difflib.SequenceMatcher(None, n.lower(), word.lower()).ratio() >= 0.7]
                if near:
                    msg += "; names that do exist: " + ", ".join(near)
        except Exception:                            # noqa: BLE001
            pass
        return msg
    out = _render_content(pattern, hits, engine, BUDGET_CHARS, matches >= limit)
    if hidden:
        out += f"\n({len(hidden)} secret file(s) matched and are not shown)"
    return out + note


def register(mediator: Mediator, root: Path, dispatch: dict | None = None) -> None:
    """search_files through the mediator (READ, plan-safe), jailed to root."""
    root = Path(root)
    mediator.register(ToolSpec(SEARCH_FILES, BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    (dispatch if dispatch is not None else mediator._fs)[SEARCH_FILES] = \
        lambda a: search_files(root, a)

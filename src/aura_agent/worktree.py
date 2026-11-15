"""worktree.py — a separate git copy of the project for one chat's work.

`.worktrees/` is ignored through git's local exclude list (.git/info/exclude) —
none of the user's files change. Gitignored settings files (.env …, or
what.worktreeinclude lists) are copied in so the project runs there. `/worktree
done`: a worktree with nothing in it is removed; one with work gets one choice
— merge into your branch, keep it, or remove it.
"""
from __future__ import annotations

import fnmatch
import re
import secrets
import shutil
import subprocess
from pathlib import Path

DIR = ".worktrees"
PREFIX = "aura/"
DEFAULT_INCLUDE = (".env", ".env.local", ".env.*.local", ".env.development")


def _git(cwd, *args: str, check: bool = False) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        if check:
            raise RuntimeError(f"git couldn't run: {e}") from e
        return 1, str(e)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "git failed")
    return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout))


def repo_root(ws) -> Path | None:
    code, out = _git(ws, "rev-parse", "--show-toplevel")
    return Path(out.strip()).resolve() if code == 0 and out.strip() else None


def main_root(ws) -> Path | None:
    """The main checkout, also when `ws` is one of its worktrees."""
    code, out = _git(ws, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not out.strip():
        return repo_root(ws)
    common = Path(out.strip()).resolve()
    return common.parent if common.name == ".git" else repo_root(ws)


def is_aura_worktree(ws) -> bool:
    p = Path(ws).resolve()
    return p.parent.name == DIR and repo_root(p) == p and main_root(p) != p


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").lower()).strip("-.")
    return s[:40] or "work-" + secrets.token_hex(3)


def _exclude(root: Path) -> None:
    """`.worktrees/` in git's local ignore list (never the user's .gitignore)."""
    code, out = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if code != 0:
        return
    f = Path(out.strip())
    try:
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        if f"/{DIR}/" not in text.split():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text + ("" if text.endswith("\n") or not text else "\n")
                         + f"/{DIR}/\n", encoding="utf-8")
    except OSError:
        pass


def _includes(root: Path) -> list[str]:
    f = root / ".worktreeinclude"
    try:
        pats = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        return pats or list(DEFAULT_INCLUDE)
    except OSError:
        return list(DEFAULT_INCLUDE)


def _copy_settings(root: Path, dest: Path) -> list[str]:
    """Gitignored files the list names (settings like .env) — tracked files are
    already in the new checkout and are never copied."""
    pats, copied = _includes(root), []
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or rel.startswith((DIR + "/", ".git/")) or "/node_modules/" in "/" + rel:
            continue
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat) for pat in pats):
            continue
        if _git(root, "check-ignore", "-q", rel)[0] != 0:
            continue                               # Tracked or not ignored: not ours to copy
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        copied.append(rel)
        if len(copied) >= 50:
            break
    return copied


def new(ws, name: str = "") -> dict:
    """{'path', 'branch', 'copied'} — raises RuntimeError with plain words."""
    root = main_root(ws)
    if root is None:
        raise RuntimeError("this project isn't a git repository — a separate copy needs git")
    if _git(root, "rev-parse", "--verify", "HEAD")[0] != 0:
        raise RuntimeError("the repository has no commit yet — a separate copy starts from one")
    slug = _slug(name)
    path = root / DIR / slug
    branch = PREFIX + slug
    if path.exists() and repo_root(path) == path.resolve():
        return {"path": path, "branch": branch, "copied": [], "reopened": True}
    _exclude(root)
    if _git(root, "rev-parse", "--verify", branch)[0] == 0:
        _git(root, "worktree", "add", str(path), branch, check=True)
    else:
        _git(root, "worktree", "add", "-b", branch, str(path), "HEAD", check=True)
    base = _git(path, "rev-parse", "HEAD")[1].strip()
    code, gp = _git(path, "rev-parse", "--path-format=absolute", "--git-path", "aura-base")
    if code == 0:
        try:
            Path(gp.strip()).write_text(base, encoding="utf-8")
        except OSError:
            pass
    return {"path": path, "branch": branch, "copied": _copy_settings(root, path), "reopened": False}


def _base(path: Path) -> str:
    code, gp = _git(path, "rev-parse", "--path-format=absolute", "--git-path", "aura-base")
    try:
        return Path(gp.strip()).read_text(encoding="utf-8").strip() if code == 0 else ""
    except OSError:
        return ""


def state(path) -> dict:
    """{'dirty': [files], 'commits': n, 'branch'} — what removing would lose."""
    path = Path(path)
    dirty = [ln[3:] for ln in _git(path, "status", "--porcelain")[1].splitlines() if ln.strip()]
    base = _base(path)
    n = 0
    if base:
        code, out = _git(path, "rev-list", "--count", f"{base}..HEAD")
        n = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    return {"dirty": dirty, "commits": n, "branch": branch}


def listing(ws) -> list[dict]:
    root = main_root(ws)
    if root is None:
        return []
    out, cur = [], {}
    for ln in _git(root, "worktree", "list", "--porcelain")[1].splitlines() + [""]:
        if not ln.strip():
            if cur.get("worktree") and Path(cur["worktree"]).parent.name == DIR:
                out.append({"path": Path(cur["worktree"]),
                            "branch": cur.get("branch", "").replace("refs/heads/", "")})
            cur = {}
            continue
        k, _, v = ln.partition(" ")
        cur[k] = v
    return out


def remove(path) -> str:
    path = Path(path).resolve()
    root = main_root(path)
    branch = state(path)["branch"]
    _git(root, "worktree", "remove", "--force", str(path), check=True)
    if branch.startswith(PREFIX):
        _git(root, "branch", "-D", branch)
    return f"removed {path.name} (branch {branch})"


def merge(path) -> str:
    """Commit what's left in the worktree, merge its branch into the main
    checkout's current branch, then remove the worktree."""
    path = Path(path).resolve()
    root = main_root(path)
    st = state(path)
    if _git(root, "status", "--porcelain", "--untracked-files=no")[1].strip():
        raise RuntimeError("your main folder has uncommitted changes — commit or put them "
                           "aside first, then merge")
    if st["dirty"]:
        _git(path, "add", "-A", check=True)
        _git(path, "commit", "-m", f"work from {st['branch']}", check=True)
    code, out = _git(root, "merge", "--no-ff", "--no-edit", st["branch"])
    if code != 0:
        _git(root, "merge", "--abort")
        raise RuntimeError("the merge has conflicts — nothing was changed; the separate "
                           f"copy is kept ({path})")
    into = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    remove(path)
    return f"merged {st['branch']} into {into} and removed the separate copy"

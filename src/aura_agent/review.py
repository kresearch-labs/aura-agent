"""review.py — review: a second look at changes, by a helper with its own context.

Aura's review:

  what what this chat changed (works without git) · uncommitted changes
            · a commit · a branch against its base · a file or folder · and
            any words after it say what to look at most
  how a helper with its own context (the chat isn't filled) and only
            reading tools: 1. find possible bugs in the change and the code
            around it, 2. check each one against the code — what can't be
            shown in the code is dropped, 3. rank what's left
  result a list: where, what's wrong, when it breaks — nothing is changed;
            fixing is the user's yes
"""
from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path

MAX_DIFF = 60_000              # Characters of diff given whole; more → the files are named

REVIEWER = (
    "You review code changes for bugs, like a careful senior engineer. Read the "
    "change and the code around it (open the files — the change alone is not enough).\n"
    "1. FIND: what in this change is wrong or will break — logic errors, wrong edge "
    "cases, crashes, data loss, security holes, broken callers, missing error handling "
    "that matters. Not style, not formatting, not taste.\n"
    "2. CHECK each one against the code before you report it: read the lines, follow "
    "the call. Drop anything you can't point to in the code, and anything already "
    "handled elsewhere.\n"
    "3. RANK what is left: high (breaks real use), medium (breaks in some cases), "
    "low (worth fixing).\n"
    "Change nothing. Answer with the list only, most serious first, at most 12, each:\n"
    "- [high|medium|low] path:line — what is wrong — when it breaks (a concrete case)\n"
    "If you found nothing that holds up, say exactly: No problems found.")


def _git(ws: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _is_git(ws: Path) -> bool:
    return (_git(ws, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def chat_diff(ws: Path, changed: dict) -> str:
    """What this chat changed: each file as it was before vs now."""
    out = []
    for rel, before in sorted((changed or {}).items()):
        p = Path(rel) if Path(rel).is_absolute() else Path(ws) / rel
        try:
            now = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            now = ""
        if before == now:
            continue
        out.append("".join(difflib.unified_diff(
            (before or "").splitlines(True), now.splitlines(True),
            f"a/{rel}", f"b/{rel}", n=3)))
    return "\n".join(x for x in out if x)


def target(ws: Path, arg: str, changed: dict | None = None) -> tuple[str, str, str]:
    """(what is reviewed, the diff or files, the user's own focus words)."""
    ws = Path(ws)
    words = str(arg or "").split()
    git = _is_git(ws)
    if words and words[0].lower() == "commit" and len(words) > 1 and git:
        d = _git(ws, "show", "--format=%H %s", words[1]) or ""
        return f"commit {words[1]}", d, " ".join(words[2:])
    if words and words[0].lower() == "branch" and git:
        base = words[1] if len(words) > 1 else next(
            (b for b in ("main", "master") if _git(ws, "rev-parse", "--verify", b)), "HEAD~1")
        d = _git(ws, "diff", f"{base}...HEAD") or ""
        return f"this branch against {base}", d, " ".join(words[2:])
    if words and (ws / words[0]).exists():
        p = ws / words[0]
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()
                                               and not any(part.startswith(".") for part in x.parts))[:40]
        listing = "\n".join(str(f.relative_to(ws)) for f in files)
        return (f"{words[0]}", "Review these files (read them):\n" + listing,
                " ".join(words[1:]))
    focus = " ".join(words)
    d = chat_diff(ws, changed or {})
    if d:
        return "what this chat changed", d, focus
    if git:
        d = (_git(ws, "diff", "HEAD") or "")
        if d.strip():
            return "the uncommitted changes", d, focus
    return "", "", focus


def task_text(label: str, diff: str, focus: str) -> str:
    if len(diff) > MAX_DIFF:
        files = sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)))
        body = ("The change is too big to show whole. These files changed — read each "
                "and review what changed:\n" + "\n".join(files))
    else:
        body = "The change:\n```diff\n" + diff + "\n```"
    return (f"Review {label}.\n" + (f"Look most at: {focus}\n" if focus else "") + "\n" + body)


def run(loop, m, eng: Path, ws: Path, arg: str = "", changed: dict | None = None) -> dict:
    """{'label', 'report', 'empty'} — the review, done by a reading-only helper."""
    label, diff, focus = target(ws, arg, changed)
    if not diff.strip():
        return {"label": "", "report": "nothing to review — no changes in this chat, and no "
                                       "uncommitted changes (name a file, a folder, `commit <id>` "
                                       "or `branch`)", "empty": True}
    from .delegate import run_delegate
    res = run_delegate(Path(eng), loop.llm.p, m.scope, Path(eng) / "audit.jsonl",
                       task_text(label, diff, focus), name="review",
                       role={"role": "reviewer", "system": REVIEWER}, workspace=Path(ws))
    report = str(res.get("summary") or "").strip() or "No problems found."
    return {"label": label, "report": report, "empty": False}


BIG_FILES, BIG_LINES = 3, 100   # What counts as a big change (the review.auto default)


def size(diff: str) -> tuple[int, int]:
    """(files, changed lines) of a unified diff."""
    files = len(re.findall(r"^\+\+\+ ", diff or "", re.M))
    lines = sum(1 for ln in (diff or "").splitlines()
                if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    return files, lines


def due(mode: str, diff: str) -> bool:
    """Should the change just made be reviewed on its own (review.auto)?"""
    files, lines = size(diff)
    if not files or mode == "off":
        return False
    return mode == "always" or files >= BIG_FILES or lines >= BIG_LINES


def found_problems(report: str) -> bool:
    # with or without a list mark (a live model wrote "[high] …" bare)
    return bool(re.search(r"^\s*(?:[-*\d.]+\s*)?\[(high|medium|low)\]", report or "", re.M | re.I))

"""teams_collide.py — Aura Teams: the collision check + git.

The check runs inside each person's Aura BEFORE every task starts. `git fetch`
runs first, automatically (it only reads). It compares the work about to start
against the tasks in the same project that are `in progress`, or done but NOT
merged. With git, a done task's commit id (from the done comment) is checked
with `git merge-base --is-ancestor <commit> <main>`; a commit Aura can't see
counts as not merged. Without git, the `merged` label set by people decides.
The four hits: same task, same files, shared pieces, a newer decision. On a
hit: TELL the user — the user decides. Hub unreachable: say the check can't run
now; the user decides whether to start.

"The same task" (left for the build): someone else already started THIS task (a
start comment on another branch with no done after it), or another open /
unmerged task whose title says nearly the same thing (most of the words of 4+
letters shared).

Aura works with the repository wherever the user has it. A branch per person
(the username). Commits are local (the repository's own hooks run); push only
on an explicit yes; if the remote can't be reached, push later. Merging is the
user's business — Aura never merges.

Every comment read here was written with the fixed layout, so parsing is exact:
`[AURA] started` (branch, planned files) and `[AURA] done` (changed, shared,
decision, commit, proof).
"""
from __future__ import annotations

import re
import subprocess

from . import teams_board as B
from .teams_board import BoardError

MERGED_LABEL = "merged"
IN_PROGRESS = B.IN_PROGRESS
SAME_TITLE = 0.6              # Share of title words that makes "the same task"


# -------------------------------------------------------------- parsing ---
def _files(val: str) -> list[str]:
    return [v.strip() for v in val.split(",")
            if v.strip() and v.strip().lower() not in ("none", "none listed",
                                                         "none listed yet")]


def parse_done_comment(text: str) -> dict:
    """The done layout, parsed exactly. Returns {} when this is not one."""
    if not text.startswith("[AURA] done"):
        return {}
    out: dict = {}
    for line in text.splitlines():
        for key in ("changed", "shared", "decision", "commit", "proof"):
            if line.lower().startswith(key + ":"):
                val = line.split(":", 1)[1].strip()
                if key in ("changed", "shared"):
                    out[key] = _files(val)
                elif key == "commit":
                    m = re.match(r"([0-9a-f]{4,40})\b", val)
                    out[key] = m.group(1) if m else None
                    m2 = re.search(r"pushed:\s*(yes|no)", val, re.I)
                    out["pushed"] = bool(m2 and m2.group(1).lower() == "yes")
                else:
                    out[key] = val
    return out


def parse_start_comment(text: str) -> dict:
    """The start layout: branch and planned files. {} when not one."""
    if not text.startswith("[AURA] started"):
        return {}
    out: dict = {"planned": []}
    for line in text.splitlines():
        if line.lower().startswith("branch:"):
            out["branch"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("planned files"):
            out["planned"] = _files(line.split(":", 1)[1])
    return out


def parse_decision_comment(text: str) -> str | None:
    if text.startswith("[AURA] decision"):
        return text.split("—", 1)[-1].strip() if "—" in text else text
    return None


def _labels(task: dict) -> list[str]:
    return B.marks(task)                 # Labels, or a label-less hub's comment marks


def _comments(task_id: int) -> list[str]:
    return B.comments(task_id)


def task_facts(task_id: int) -> dict:
    """What the comments on a task say, in order: the latest start (branch,
    planned files), what was changed and shared, the commit, decisions."""
    f: dict = {"branch": None, "planned": [], "changed": [], "shared": [],
               "commit": None, "decision": None, "started_after_done": False}
    for c in _comments(task_id):
        s = parse_start_comment(c)
        if s:
            f["branch"] = s.get("branch")
            f["planned"] = s.get("planned", [])
            f["started_after_done"] = True
        d = parse_done_comment(c)
        if d:
            f["changed"] += d.get("changed", [])
            f["shared"] += d.get("shared", [])
            f["commit"] = d.get("commit") or f["commit"]
            if d.get("decision") and d["decision"].lower() != "none":
                f["decision"] = d["decision"]
            f["started_after_done"] = False
        dec = parse_decision_comment(c)
        if dec:
            f["decision"] = dec
    return f


# ------------------------------------------------------------------ git ---
class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]}: {e}") from e
    if check and r.returncode != 0:
        raise GitError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def is_repo(path: str | None) -> bool:
    if not path:
        return False
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree",
                    check=False, timeout=15) == "true"
    except GitError:
        return False


def main_ref(repo: str) -> str | None:
    """The team's main line: the remote's default branch when known, else
    origin/main, origin/master, main, master. None = can't tell."""
    head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD",
                check=False)
    if head:
        return head
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/heads/main", "refs/heads/master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False):
            return ref
    return None


def commit_merged(commit: str, repo: str, ref: str | None = None) -> bool | None:
    """True = in the main line; False = not; None = the commit (or the main line)
    is unknown here — counts as NOT merged, per
    """
    ref = ref or main_ref(repo)
    if not ref:
        return None
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", commit, ref],
                           cwd=repo, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(r.returncode)


def git_fetch(repo: str) -> bool:
    """Automatic and read-only. False when the remote can't be reached — the check
    still runs, on what is here.
    """
    try:
        return subprocess.run(["git", "fetch", "--all", "--quiet"], cwd=repo,
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_branch(repo: str, username: str) -> str:
    """A branch per person. Switches to it, makes it if missing. Never merges,
    never touches other branches.
    """
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == username:
        return username
    exists = _git(repo, "branch", "--list", username)
    try:
        _git(repo, "checkout", *([username] if exists.strip() else ["-b", username]))
    except GitError as e:
        raise GitError(f"could not switch to your branch '{username}' ({e}) — "
                       "commit or put aside the changes here first") from e
    return username


def commit_local(repo: str, changed: list[str], message: str) -> str | None:
    """Commit locally after a task. The repository's own hooks run. Returns the
    short commit id, None when there was nothing to commit.
    """
    if not changed:
        return None
    _git(repo, "add", "-A", "--", *changed)
    r = subprocess.run(["git", "commit", "-m", message],
                       cwd=repo, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        if "nothing to commit" in (r.stdout + r.stderr):
            return None
        raise GitError((r.stderr or r.stdout).strip()[-300:])
    return _git(repo, "rev-parse", "--short", "HEAD")


def push_branch(repo: str, username: str) -> dict:
    """Push ONLY when the user said yes (the caller asks). A remote that can't be
    reached is not an error: the push happens later.
    """
    try:
        r = subprocess.run(["git", "push", "-u", "origin", username],
                           cwd=repo, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": True, "pushed": False, "detail": f"push later: {e}"}
    if r.returncode == 0:
        return {"ok": True, "pushed": True, "detail": r.stderr.strip()[-100:]}
    return {"ok": True, "pushed": False,
            "detail": "not pushed — it can be pushed later: "
                      + (r.stderr.strip()[-150:] or "unknown")}


# -------------------------------------------------------- the check itself ---
def _same_dir(a: str, b: str) -> bool:
    sa = a.rsplit("/", 1)[0] if "/" in a else ""
    sb = b.rsplit("/", 1)[0] if "/" in b else ""
    return bool(sa) and sa == sb


def _touches(piece: str, f: str) -> bool:
    """A shared piece (a file or a folder) covers this file."""
    piece = piece.replace("\\", "/")
    f = f.replace("\\", "/")
    return piece == f or f.startswith(piece.rstrip("/") + "/") or _same_dir(f, piece)


def same_title(a: str, b: str) -> bool:
    ta, tb = B._tokens(a), B._tokens(b)
    if len(ta & tb) < 2:
        return False
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= SAME_TITLE


def collision_check(task_id: int, planned_files: list[str],
                    repo: str | None = None, me: str | None = None) -> dict:
    """Runs before a task starts. Never blocks silently: on any hit the result
    says so and the user decides; if the hub is unreachable the result says the
    check can't run and the user decides whether to start.

    Returns hits/lines for the user, and `others`: {file: what} for every file
    another open or unmerged task planned or changed — the run checks each file
    it is about to change against it.
    """
    result: dict = {"hits": [], "lines": [], "can_check": True, "others": {},
                    "pieces": {}}
    try:
        me = me if me is not None else B._auth()[1]
        my = B.get_task(task_id)
        pid = my.get("project_id")
        both = B.project_tasks(pid, done=False) + B.project_tasks(pid, done=True)
        mine = task_facts(task_id)
        facts = {t.get("id"): task_facts(t["id"]) for t in both
                 if t.get("id") != my.get("id") and (
                     (IN_PROGRESS in _labels(t) and not t.get("done"))
                     or (t.get("done") and MERGED_LABEL not in _labels(t)))}
    except BoardError as e:
        result["can_check"] = False
        result["lines"].append(
            f"the check can't run now ({e}) — you decide whether to start")
        return result
    git = bool(repo) and is_repo(repo)
    ref = None
    if git:
        if not git_fetch(repo):                       # Automatic, read-only
            result["lines"].append("git fetch could not reach the remote — "
                                   "checked with what this copy has")
        ref = main_ref(repo)
    # someone else already started THIS task
    if mine["started_after_done"] and mine["branch"] and mine["branch"] != me:
        result["hits"].append("same task")
        result["lines"].append(
            f"#{task_id} — already started by {mine['branch']} "
            f"(planned: {', '.join(mine['planned'][:4]) or 'not listed'})")
    seen: set = set()
    for t in both:
        tid = t.get("id")
        if tid == my.get("id") or tid in seen:
            continue
        seen.add(tid)
        t_labels = _labels(t)
        is_wip = IN_PROGRESS in t_labels and not t.get("done")
        is_done_unmerged = bool(t.get("done")) and MERGED_LABEL not in t_labels
        if not (is_wip or is_done_unmerged):
            continue
        f = facts[tid]
        if is_done_unmerged and git and f["commit"]:
            if commit_merged(f["commit"], repo, ref):
                continue                              # Merged → no collision
            # Unseen commit or no main line: counts as NOT merged
        state = "in progress" if is_wip else "done, not merged"
        who = f["branch"] or "someone"
        what = f"#{tid} {t.get('title', '')} ({who}, {state})"
        their_files = sorted(set(f["planned"] if is_wip else []) | set(f["changed"]))
        for x in their_files:
            result["others"].setdefault(x, what)
        for x in f["shared"]:
            result["pieces"].setdefault(x, what)
        # The four hits --
        if same_title(my.get("title", ""), t.get("title", "")):
            result["hits"].append("same task")
            result["lines"].append(f"{what} — looks like the same task")
        same_files = sorted(set(planned_files) & set(their_files))
        if same_files:
            result["hits"].append("same files")
            result["lines"].append(
                f"{what} — the same files ({', '.join(same_files[:4])})")
        shared_pieces = sorted({s for s in f["shared"]
                                if any(_touches(s, x) for x in planned_files)})
        if shared_pieces:
            result["hits"].append("shared pieces")
            result["lines"].append(
                f"{what} — shared pieces changed there: "
                f"{', '.join(shared_pieces[:4])}")
        if f["decision"] and (same_files or shared_pieces):
            result["hits"].append("newer decision")
            result["lines"].append(f"{what} — a decision made there: {f['decision']}")
    if not result["hits"]:
        result["lines"].append("no collisions — clear to start")
    return result


def file_hit(path: str, check: dict) -> str | None:
    """During the run: is this file in someone else's open or unmerged
    work? (The planned files at the start are only an estimate.)"""
    p = path.replace("\\", "/")
    if p in check.get("others", {}):
        return f"{p} is in {check['others'][p]}"
    for piece, what in check.get("pieces", {}).items():
        if _touches(piece, p):
            return f"{p} is a shared piece changed in {what}"
    return None

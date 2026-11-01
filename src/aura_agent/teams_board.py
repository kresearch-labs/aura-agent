"""teams_board.py — Aura Teams: the work loop.

Linking: match the request to the user's assigned tasks and ASK ("This looks
like #12 … — is it that one?"); a task number in the request matches by itself;
otherwise token match (words of 4+ letters) over titles and descriptions, up to
5 candidates. No match → offer to create a task, assigned to the user; a "no"
means work off-board (and the collision check can't cover it — said plainly).

Writes: fixed comment layout, label `in progress` at start, done ONLY with
proof, `decision` label on decisions, every comment through Aura's secret
scrubber, and NEVER any of the user's fields (priority, dates, reminders,
assignees, percent_done …).

Vikunja's task update REPLACES the task: a field left out is cleared
(pkg/models/tasks.go, updateSingleTask: "mergo does ignore nil values" —
description, due date, priority, percent done … are reset when empty). So
marking done reads the whole task and sends it back with only `done` changed —
what Vikunja's own web page does.

Reads: the task's details, labels, comments, links; blocked-by an unfinished
task is told; due soon / overdue is told; everything read from the hub goes
back to the model as OUTSIDE TEXT.

API routes (pkg/routes/routes.go, v2.7.1):
  GET /tasks?filter=… (all pages: x-pagination-total-pages)
  GET /tasks/{id} · POST /tasks/{id} (the whole task)
  PUT /tasks/{id}/comments {comment} · GET same
  GET /labels · PUT /labels {title}
  GET|PUT /tasks/{id}/labels · DELETE /tasks/{id}/labels/{label}
  GET /projects · PUT /projects/{id}/tasks · PUT /tasks/{id}/assignees
  relations: kind `blocked` on the blocked task (the other side `blocking`)
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import keystore, teams
from .secrets_guard import scrub_secrets

IN_PROGRESS = "in progress"
DECISION = "decision"
TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
DUE_SOON_DAYS = 3
PER_PAGE = 50
MAX_PAGES = 200                    # A runaway answer stops somewhere


# ------------------------------------------------------------- client ---
class BoardError(RuntimeError):
    """Said to the user in plain words, never a traceback."""


class HubOff(BoardError):
    """The hub can't be reached — normal; updates wait in the queue."""


class TokenRefused(BoardError):
    """The hub said no to the token — not queued, the user makes a new one."""


def _auth() -> tuple[str, str, str]:
    """(base, username, token) or raise with what to do about it."""
    cfg = teams.load_config()
    if not cfg.get("hub_url"):
        raise BoardError("not connected to a hub — `aura teams connect "
                         "<hub-address>` first")
    token = keystore.get(teams.TOKEN_KEY)
    if not token:
        raise BoardError("no hub token in the key store — `aura teams "
                         "connect` again with a fresh token")
    return cfg["hub_url"], cfg.get("username", ""), token


def _request(method: str, path: str, body: dict | None = None):
    """(parsed body, response headers)."""
    base, _, token = _auth()
    url = base.rstrip("/") + "/api/v1" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}",
                                          "User-Agent": "aura-teams"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return (json.loads(raw) if raw.strip() else {}), r.headers
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TokenRefused(
                "the hub refused the token (it may have expired, or lacks a "
                "permission) — make a new one in Vikunja and `aura teams "
                "connect` again") from e
        detail = e.read().decode("utf-8", "replace")[:150]
        raise BoardError(f"the hub said {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        why = getattr(e, "reason", e)
        raise HubOff(f"the hub is not reachable ({why}) — the collision "
                     f"check and the board are off until it is back") from e


def _call(method: str, path: str, body: dict | None = None) -> dict | list:
    return _request(method, path, body)[0]


def _call_all(path: str) -> list:
    """Every page of a list. Vikunja pages its lists (per_page, capped by
    service.maxitemsperpage) and says how many pages in a header."""
    sep = "&" if "?" in path else "?"
    out: list = []
    page = 1
    while page <= MAX_PAGES:
        body, headers = _request("GET", f"{path}{sep}page={page}&per_page={PER_PAGE}")
        items = body if isinstance(body, list) else []
        out += [i for i in items if isinstance(i, dict)]
        try:
            pages = int(headers.get("x-pagination-total-pages") or 0)
        except (TypeError, ValueError):
            pages = 0
        if (pages and page >= pages) or (not pages and len(items) < PER_PAGE) \
                or not items:
            break
        page += 1
    return out


def _filter(f: str) -> str:
    """A task filter. Include-nulls off: a filter on assignees must not
    also return tasks with nobody assigned (the web page's default is on)."""
    return "filter=" + urllib.parse.quote(f, safe="") + "&filter_include_nulls=false"


# ------------------------------------------------------------ the hub ---
def hub():
    """The hub this machine works with (teams_hub.current): Vikunja, or any
    app through the wiring Aura wrote and the user approved."""
    from .teams_hub import current
    return current()


def open_tasks() -> list[dict]:
    """Everyone's open tasks the user can see."""
    return hub().list_tasks(done=False)


def comments(task_id: int) -> list[str]:
    return hub().comments(task_id)


# --------------------------------------------- the chat's sign-in ---
# No password, no token leaves.
SIGNIN_TITLE = "Aura chat sign-ins — keep open"


def signin_task(create: bool = True) -> int | None:
    h = hub()
    for t in h.list_tasks(done=False):
        if str(t.get("title", "")).startswith("Aura chat sign-ins"):
            return t["id"]
    if not create or not h.has("create_task"):
        return None
    pid = None
    if h.has("projects"):
        ps = h.projects()
        pid = ps[0]["id"] if ps else None
    t = h.create_task(pid, SIGNIN_TITLE, "Aura posts one-time codes here when someone opens the "
                                         "team chat. Keep it open; the comments can be ignored.")
    return t.get("id") if isinstance(t, dict) else None


def comment_authors(task_id: int) -> list[tuple[str, str]]:
    h = hub()
    if hasattr(h, "comment_authors"):
        return h.comment_authors(task_id)
    return [("", c) for c in h.comments(task_id)]


def team_people() -> list[str]:
    h = hub()
    try:
        return h.people() if hasattr(h, "people") else []
    except BoardError:
        return []


# ------------------------------------------------------------ task lists ---
def my_tasks(done: bool | None = False) -> list[dict]:
    """The user's assigned tasks. done=None → everything."""
    _, user, _ = _auth()
    if not user:
        raise BoardError("no username on record — `aura setup you`, or "
                         "`aura teams connect <hub-address> --user <your-name>`")
    return hub().list_tasks(assignee=user, done=done)


def project_tasks(project_id: int, done: bool) -> list[dict]:
    return hub().list_tasks(project=project_id, done=done)


def get_task(task_id: int) -> dict:
    t = hub().get_task(task_id)
    if not isinstance(t, dict) or not t.get("id"):
        raise BoardError(f"task {task_id} not found on the hub")
    return t


def my_projects() -> list[dict]:
    h = hub()
    if not h.has("projects"):
        return []
    return [p for p in h.projects()
            if not p.get("is_archived") and int(p.get("id", 0) or 0) > 0]


def create_task(project_id: int, title: str, description: str = "") -> dict:
    """The work isn't on the board and the user said yes — the task is made in the
    project the user picked and assigned to the user.
    """
    h = hub()
    if not h.has("create_task"):
        raise BoardError("this hub's wiring can't create tasks — link the work to an existing one")
    t = h.create_task(project_id, scrub_secrets(title.strip())[:250], scrub_secrets(description))
    if not isinstance(t, dict) or not t.get("id"):
        raise BoardError("the hub did not make the task")
    return get_task(t["id"])


# ------------------------------------------------------------- matching ---
def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))


def match_request(request: str, tasks: list[dict] | None = None) -> dict:
    """Never silent — the caller asks when there are candidates and offers to
    create when there are none.
    """
    m = re.search(r"(?<![\w&])#(\d+)\b", request)
    if m:
        try:
            t = get_task(int(m.group(1)))
        except HubOff:
            raise
        except BoardError:
            return {"match": None, "candidates": [], "by_id": int(m.group(1))}
        return {"match": t, "candidates": [t], "by_id": t["id"]}
    tasks = tasks if tasks is not None else my_tasks(done=False)
    want = _tokens(request)
    scored = []
    for t in tasks:
        hay = _tokens(t.get("title", "") + " " + (t.get("description") or ""))
        hit = want & hay
        if hit:
            scored.append((len(hit), t))
    scored.sort(key=lambda x: -x[0])
    cands = [t for _, t in scored[:5]]
    return {"match": cands[0] if len(cands) == 1 else None,
            "candidates": cands, "by_id": None}


# -------------------------------------------------------------- comments ---
def start_comment(summary: str, branch: str, planned_files: list[str]) -> str:
    """."""
    files = ", ".join(planned_files) if planned_files else "none listed yet"
    return scrub_secrets(
        f"[AURA] started — {summary}\n"
        f"branch: {branch or 'none (no git)'}\n"
        f"planned files (estimate): {files}")


def done_comment(changed: list[str], shared: list[str] | str,
                 decision: str, commit: str, pushed: bool, proof: str) -> str:
    shared_s = ", ".join(shared) if isinstance(shared, list) else (shared or "")
    return scrub_secrets(
        f"[AURA] done\n"
        f"changed: {', '.join(changed) if changed else 'none listed'}\n"
        f"shared: {shared_s or 'none'}\n"
        f"decision: {decision or 'none'}\n"
        f"commit: {commit or 'none'} (pushed: {'yes' if pushed else 'no'})\n"
        f"proof: {proof or 'not proven'}")


def decision_comment(text: str) -> str:
    return scrub_secrets(f"[AURA] decision — {text}")


def add_comment(task_id: int, text: str) -> dict:
    c = hub().add_comment(task_id, text)
    return c if isinstance(c, dict) else {}


# ---------------------------------------------------------------- labels ---
MARK_LINE = "[AURA] mark: "              # A hub without labels: the mark is a comment line


def attach_label(task_id: int, name: str) -> None:
    h = hub()
    if h.has("mark"):
        h.mark(task_id, name)
    else:
        add_comment(task_id, MARK_LINE + name)


def detach_label(task_id: int, name: str) -> None:
    h = hub()
    if h.has("unmark"):
        h.unmark(task_id, name)
    else:
        add_comment(task_id, MARK_LINE + "no longer " + name)


def marks(task: dict) -> list[str]:
    """A task's labels — or, on a hub without labels, the marks its comments
    left (the newest line about each wins)."""
    labs = [lab.get("title") for lab in (task.get("labels") or []) if isinstance(lab, dict)]
    if labs or hub().has("labels"):
        return labs
    on: dict[str, bool] = {}
    for c in comments(task["id"]):
        if c.startswith(MARK_LINE):
            m = c[len(MARK_LINE):].strip()
            if m.startswith("no longer "):
                on[m[len("no longer "):]] = False
            else:
                on[m] = True
    return [k for k, v in on.items() if v]


PEOPLES_FIELDS = ("title", "description", "done", "due_date", "priority",
                  "percent_done", "start_date", "end_date", "repeat_after",
                  "repeat_mode", "hex_color", "project_id")


def fingerprint(t: dict) -> str:
    """What people can change on a task, as one short hash. Vikunja's `updated` is
    in whole seconds — a change in the same second as the read kept it equal —
    so a change is told by the fields themselves. Labels are left out: Aura's
    own `in progress` label must not count as someone else's change.
    """
    keep = {k: t.get(k) for k in PEOPLES_FIELDS}
    keep["assignees"] = sorted(a.get("id") for a in (t.get("assignees") or [])
                               if isinstance(a, dict))
    raw = json.dumps(keep, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def set_done(task_id: int, base: str | None = None) -> dict:
    """Only `done` changes. The whole task is read and sent back (see the top).
    base.
    """
    cur = get_task(task_id)
    if cur.get("done"):
        return {"ok": True, "already": True, "task": cur}
    if base and fingerprint(cur) != base:
        return {"ok": False, "changed_since": True, "task": cur}
    upd = hub().set_done(task_id, cur)
    return {"ok": bool(isinstance(upd, dict) and upd.get("done")), "task": upd}


# ------------------------------------------------------------ the writes ---
def task_start(task_id: int, summary: str, branch: str,
               planned_files: list[str]) -> dict:
    """Start: label `in progress` + the start comment. Only the label and the
    comment are touched — never the user's fields.
    """
    attach_label(task_id, IN_PROGRESS)
    c = add_comment(task_id, start_comment(summary, branch, planned_files))
    return {"ok": bool(c), "comment": c.get("comment", "")}


def task_done(task_id: int, changed: list[str], shared, decision: str,
              commit: str, pushed: bool, proof: str,
              base: str | None = None) -> dict:
    """Done: marked done ONLY with proof (refuse without, plainly)."""
    if not (proof and proof.strip()):
        return {"ok": False,
                "detail": "no proof given — a task is marked done only with "
                          "proof (checks run and their result)"}
    # the done mark first: our own comment must not count as "someone
    # changed it meanwhile"; the comment goes on either way (only added)
    r = set_done(task_id, base)
    c = add_comment(task_id, done_comment(changed, shared, decision, commit,
                                          pushed, proof))
    if r.get("changed_since"):
        return {"ok": False, "comment": c.get("comment", ""),
                "detail": f"#{task_id} was changed by someone else meanwhile — "
                          "the done comment is on it, the done mark was not "
                          "written; you decide"}
    detach_label(task_id, IN_PROGRESS)
    return {"ok": r["ok"], "comment": c.get("comment", ""),
            "detail": "already marked done by someone" if r.get("already") else ""}


def task_decision(task_id: int, text: str) -> dict:
    """Decision: comment + `decision` label."""
    attach_label(task_id, DECISION)
    c = add_comment(task_id, decision_comment(text))
    return {"ok": bool(c), "comment": c.get("comment", "")}


# ------------------------------------------------------------- the reads ---
def _blocked_by(t: dict) -> list[str]:
    out = []
    for other in (t.get("related_tasks") or {}).get("blocked") or []:
        if isinstance(other, dict) and not other.get("done"):
            out.append(f"#{other.get('id')} {other.get('title', '')}".strip())
    return out


def read_task(task_id: int) -> dict:
    """Read everything and return what the user must be TOLD, plus the raw task
    marked as outside text (the agent treats hub text as outside).
    """
    t = get_task(task_id)
    lines: list[str] = []
    blocked = _blocked_by(t)
    if blocked:
        lines.append("blocked by unfinished: " + "; ".join(blocked))
    due = _fmt_due(t)
    if due:
        lines.append(due)
    labels = [lab.get("title") for lab in (t.get("labels") or [])
              if isinstance(lab, dict)]
    cmts = comments(task_id)
    return {"task": {"outside_text": True, "data": t},
            "tells": lines, "labels": labels, "comments": cmts}


def _fmt_due(t: dict) -> str | None:
    due = t.get("due_date")
    if not due or str(due).startswith("0001-01-01"):
        return None
    try:
        d = datetime.date.fromisoformat(str(due)[:10])
    except ValueError:
        return None
    days = (d - datetime.date.today()).days
    if days < 0:
        return f"overdue: was due {d.isoformat()} ({-days} day(s) ago)"
    if days <= DUE_SOON_DAYS:
        return f"due soon: {d.isoformat()} (in {days} day(s))"
    return None


def team_summary() -> str:
    """`aura teams board --all`: everyone's open work the user can see — who, in
    progress or not, blocked, due. For the lead (or whoever asks their Aura to
    watch the team); never run on its own.
    """
    tasks = open_tasks()
    if not tasks:
        return "no open tasks on the board."
    lines = [f"{len(tasks)} open task(s) on the board"]
    for t in tasks:
        who = ", ".join(a.get("username", "?") for a in (t.get("assignees") or [])) or "nobody"
        labs = marks(t)
        tells = (["in progress"] if IN_PROGRESS in labs else []) + \
            [f"blocked by {b.split(' ', 1)[0]}" for b in _blocked_by(t)]
        due = _fmt_due(t)
        if due:
            tells.append(due)
        tail = f"  ({'; '.join(tells)})" if tells else ""
        lines.append(f"  #{t['id']} {t.get('title', '')} — {who}{tail}")
    return "\n".join(lines)


def due_tells() -> list[str]:
    """The user's own open tasks that are due soon or overdue."""
    out = []
    for t in my_tasks(done=False):
        due = _fmt_due(t)
        if due:
            out.append(f"#{t['id']} {t.get('title', '')}: {due}")
    return out


def board_summary() -> str:
    """What `aura teams board` prints: my open tasks, blocked, due."""
    _, user, _ = _auth()
    tasks = my_tasks(done=False)
    if not tasks:
        return f"{user}: no open tasks on the board."
    lines = [f"{user}: {len(tasks)} open task(s)"]
    for t in tasks:
        marks = [f"blocked by {b.split(' ', 1)[0]}" for b in _blocked_by(t)]
        due = _fmt_due(t)
        if due:
            marks.append(due)
        tail = f"  ({'; '.join(marks)})" if marks else ""
        lines.append(f"  #{t['id']} {t.get('title', '')}{tail}")
    return "\n".join(lines)

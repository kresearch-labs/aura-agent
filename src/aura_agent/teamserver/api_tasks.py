"""teamserver/api_tasks.py — tasks, comments, links (plan).

Stale writes: a PATCH whose `version` differs from the task's current one gets
409 conflict WITH the current task. version += 1 and updated = now on every
success. Every change writes its log entry and an SSE event. The trash: DELETE
sets deleted_at; nothing is auto-cleaned.
"""
from __future__ import annotations

import json
import re

from . import log as logmod
from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .api_projects import _member_role, _require, _visible
from .perms import Forbidden, can, require

PAGE = 100
TASK_FIELDS = ("title", "description", "status", "priority", "start", "due")



def _num(v, default=0):
    """hostile-shape-safe int; garbage → default (never a 500)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _task(conn, tid: int):
    return conn.execute("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
                        (tid,)).fetchone()


def _full_task(conn, row) -> dict:
    """Task + assignees + labels + links + version (GET tasks/<id>)."""
    d = dict(row)
    d["assignees"] = [dict(a) for a in conn.execute(
        "SELECT p.id, p.name FROM task_assignees t JOIN people p"
        " ON p.id = t.person WHERE t.task = ?", (row["id"],)).fetchall()]
    d["labels"] = [r["label"] for r in conn.execute(
        "SELECT label FROM task_labels WHERE task = ?", (row["id"],))]
    d["links"] = [dict(l) for l in conn.execute(
        "SELECT kind, other FROM task_links WHERE task = ?", (row["id"],))]
    return d


def _can_read(conn, actor, tid: int, project: int) -> bool:
    return can(conn, actor, "view_work_packages", project)


def _require_write(handler, conn, actor, project, action: str = "edit_work_packages"):
    try:
        require(conn, actor, action, project)
        return True
    except Forbidden as e:
        handler._error(403, str(e))
        return False


def list_tasks(handler, match, body) -> None:
    """GET api/v1/tasks?project=&assignee=me&status_type=&open=1&page= 100 per
    page with a `next` cursor.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    where, args = ["t.deleted_at IS NULL"], []
    if q.get("project"):
        where.append("t.project = ?")
        args.append(int(q["project"][0]))
    if q.get("assignee"):
        who = q["assignee"][0]
        if actor["role"] in ("owner", "admin"):
            where.append("t.id IN (SELECT task FROM task_assignees a JOIN"
                         " people p ON p.id = a.person WHERE p.name = ?)")
            args.append(who)
        else:
            where.append("t.id IN (SELECT task FROM task_assignees WHERE"
                         " person = ?) AND t.project IN (SELECT project FROM"
                         " project_members WHERE person = ?)")
            args += [actor["person_id"], actor["person_id"]]
    if q.get("status_type"):
        where.append("t.status IN (SELECT key FROM statuses WHERE project ="
                     " t.project AND type = ?)")
        args.append(q["status_type"][0])
    if q.get("open"):
        where.append("t.status NOT IN (SELECT key FROM statuses WHERE"
                     " project = t.project AND type IN ('done','canceled'))")
    page = int((q.get("page") or ["1"])[0])
    if actor["role"] not in ("owner", "admin"):
        # only the projects whose board this person may see
        from . import perms
        mine = [r["project"] for r in conn.execute(
            "SELECT project FROM project_members WHERE person = ?",
            (actor["person_id"],)).fetchall()]
        seen = [p for p in mine
                if perms.allowed(conn, actor, "view_work_packages", p, module="board")]
        where.append("t.project IN (%s)" % (",".join("?" * len(seen)) or "NULL"))
        args += seen
    rows = conn.execute(
        "SELECT t.* FROM tasks t WHERE " + " AND ".join(where) +
        " ORDER BY t.id LIMIT ? OFFSET ?",
        (*args, PAGE + 1, (page - 1) * PAGE)).fetchall()
    out = {"tasks": [_full_task(conn, r) for r in rows[:PAGE]]}
    if len(rows) > PAGE:
        out["next"] = page + 1
    handler._json(200, out)


def _mirror_guard(conn, handler, pid: int) -> bool:
    """409 when the project's tasks come from an outside hub: the mirror is
    read-only, never written back.
    """
    from .api_mirror import tasks_from
    if tasks_from(conn, pid):
        handler._error(409, "this project's tasks come from an outside "
                            "hub — edit them there (read-only mirror)")
        return True
    return False

def task_create(handler, match, body) -> None:
    """POST /api/v1/tasks — task.write; number = next in the project;
    task.created logged; SSE task event."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    pid = _num(req.get("project"))
    title = str(req.get("title") or "").strip()[:300]
    try:
        require(conn, actor, "edit_work_packages", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
        (pid,)).fetchone()
    if project is None:
        handler._error(404, "no such project")
        return
    if project["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    if _mirror_guard(conn, handler, pid):
        return
    if not title:
        handler._error(400, "give the task a title")
        return
    # Validate person/task references BEFORE store.write — an error answer
    # inside the write block commits what INSERTed so far
    want_assignees = (req.get("assignees") or [])[:20]
    member_ids = []
    for who in want_assignees:
        pr = conn.execute(
            "SELECT p.id FROM people p JOIN project_members m"
            " ON m.person = p.id AND m.project = ? WHERE p.name = ?",
            (pid, who)).fetchone()
        if not pr:
            handler._error(400, f"'{who}' isn't a member of this project — "
                                "they go on its page's members list first")
            return
        member_ids.append(pr["id"])
    want_blocked = []
    for other in (req.get("blocked_by") or [])[:20]:
        oid = _num(other, default=-1)
        ot = conn.execute(
            "SELECT id FROM tasks WHERE id = ? AND project = ?",
            (oid, pid)).fetchone()
        if ot is None:
            handler._error(400, f"#~{other} isn't a task of this project — "
                                "blocks link only tasks of one project")
            return
        want_blocked.append(oid)
    # the column it starts in (the new-task form's choice); an unknown one is
    # refused, none given is To do
    status0 = "todo"
    if req.get("status"):
        row = conn.execute("SELECT key FROM statuses WHERE project = ? AND key = ?",
                           (pid, str(req["status"])[:40])).fetchone()
        if row is None:
            handler._error(400, "no such column in this project")
            return
        status0 = row["key"]
    now = store.now()
    with store.write(conn):
        n = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM tasks WHERE project = ?",
            (pid,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO tasks (project, number, title, description, status,"
            " priority, start, due, created_by, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, n, title, str(req.get("description") or "")[:20000],
             status0, int(req.get("priority") or 0), req.get("start"),
             req.get("due"), actor["person_id"], now, now))
        tid = cur.lastrowid
        for mid in member_ids:                # Proven above, every name
            conn.execute("INSERT OR IGNORE INTO task_assignees (task,"
                         " person) VALUES (?,?)", (tid, mid))
        for lab in (req.get("labels") or [])[:20]:
            conn.execute("INSERT OR IGNORE INTO task_labels (task, label)"
                         " VALUES (?,?)", (tid, str(lab)[:60]))
        for oid in want_blocked:              # Proven above, same project
            conn.execute("INSERT OR IGNORE INTO task_links (task, kind,"
                         " other) VALUES (?, 'blocked_by', ?)",
                         (tid, oid))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "task.created", f"#{n}", None,
                      {"id": tid, "title": title, "by": actor["name"]})
        _sse(conn, pid, "task", {"id": tid, "kind": "created"})
    handler._json(200, {"task": _full_task(conn, _task(conn, tid))})


# what a person must be allowed in the project to receive a live event
LIVE_NEEDS = {
    "task": (("view_work_packages", "board"),),
    "comment": (("view_work_packages", "board"),),
    "map": (("browse_repository", "tree"),),
    "log": (("view_activity", "history"), ("view_activity", "now")),
}


def _task_may_see(conn, pid: int, kind: str = "task"):
    """may_see for task/comment/overlap/map/log events."""
    from .perms import members_who_may
    allowed = members_who_may(conn, pid, LIVE_NEEDS.get(kind, LIVE_NEEDS["task"]))
    allowed |= {r["id"] for r in conn.execute(
        "SELECT id FROM people WHERE role IN ('owner', 'admin')"
        " AND disabled_at IS NULL")}

    def may_see(person_id, project=None, kind=None) -> bool:
        return person_id in allowed
    return may_see


def _sse(conn, project, kind, data):
    """Publish after the owning write commits through the ONE broadcaster.

    may_see rides the ring entry, so Last-Event-ID replay is filtered exactly
    like live delivery: a non-member never receives the event. The callback
    preserves that contract without waking a reader before COMMIT.
    """
    def deliver():
        from . import teamserver_db
        try:
            teamserver_db.broadcaster().publish(
                project, kind, data, may_see=_task_may_see(conn, project, kind))
        except Exception:                            # noqa: BLE001
            pass                       # Streaming must never break a write
    store.after_commit(conn, deliver)


def task_get(handler, match, body) -> None:
    """GET api/v1/tasks/<id> — task.read; outsiders get 404 (don't reveal it
    exists).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid = int(match.group(1))
    task = _task(conn, tid)
    if task is None or not _can_read(conn, actor, tid, task["project"]):
        handler._error(404, "no such task")
        return
    handler._json(200, {"task": _full_task(conn, task)})


def task_patch(handler, match, body) -> None:
    """PATCH api/v1/tasks/<id> — task.write; STALE WRITES rejected: a version
    other than the current → 409 + the current task; success = version += 1,
    updated = now; task.updated / task.status logged, SSE out.

    The person's fields (priority, dates, assignees, labels) change only when
    the request carries them; the work loop sends status only.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid = int(match.group(1))
    task = _task(conn, tid)
    if task is None:
        handler._error(404, "no task %d on this team" % tid)   # Never silent
        return
    if not _can_read(conn, actor, tid, task["project"]) or \
            not _require_write(handler, conn, actor, task["project"]):
        return
    if _archived(handler, conn, task["project"]):
        return
    if _mirror_guard(conn, handler, task["project"]):
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    version = _num(req.get("version"))
    if version != task["version"]:
        changed_by = conn.execute(
            "SELECT p.name FROM log l JOIN people p ON p.id = l.actor"
            " WHERE l.project = ? AND l.type LIKE 'task.%'"
            " ORDER BY l.seq DESC LIMIT 1", (task["project"],)).fetchone()
        handler._json(409, {
            "error": {"code": "conflict",
                      "message": ("this task changed by %s since you read it"
                                  % (changed_by["name"] if changed_by
                                     else "someone"))},
            "current": _full_task(conn, task)})
        return
    # the status must be one of the project's keys; its TYPE decides the log
    fields, args, kinds = [], [], []
    status_from = task["status"]
    status_to = None
    for f in ("title", "description"):
        if f in req:
            fields.append(f"{f} = ?")
            args.append(str(req[f])[:20000])
    if "status" in req:
        key = str(req["status"])
        stype = conn.execute(
            "SELECT type FROM statuses WHERE project = ? AND key = ?",
            (task["project"], key)).fetchone()
        if stype is None:
            handler._error(400, "no such status in this project")
            return
        fields.append("status = ?")
        args.append(key)
        status_to = key
    for f in ("priority", "start", "due"):
        if f in req:
            fields.append(f"{f} = ?")
            args.append(req[f])
    if req.get("merged") is not None:
        fields.append("merged_at = ?")
        fields.append("merged_by = ?")
        args += ([store.now(), actor["person_id"]] if req["merged"]
                 else [None, None])
    has_list_changes = isinstance(req.get("assignees"), list) or \
        isinstance(req.get("labels"), list)
    if not fields and not has_list_changes:
        handler._error(400, "nothing to change")
        return
    # Pre-prove every assignee name BEFORE the write: a 400 inside store.write
    # would otherwise COMMIT the wiped assignee table
    member_ids = None
    if isinstance(req.get("assignees"), list):
        member_ids = []
        for who in req["assignees"][:20]:
            pr = conn.execute(
                "SELECT p.id FROM people p JOIN project_members m"
                " ON m.person = p.id AND m.project = ? WHERE p.name = ?",
                (task["project"], who)).fetchone()
            if not pr:
                handler._error(400, f"'{who}' isn't a member of this "
                                    "project — they go on its page's "
                                    "members list first")
                return
            member_ids.append(pr["id"])
    fields += ["version = version + 1", "updated = ?"]
    args.append(store.now())
    with store.write(conn):
        args.append(tid)
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
                     args)
        # assignees / labels replacements (the caller's list wins)
        new_assignees: list = []
        if member_ids is not None:
            before = {r["person"] for r in conn.execute(
                "SELECT person FROM task_assignees WHERE task = ?", (tid,))}
            conn.execute("DELETE FROM task_assignees WHERE task = ?", (tid,))
            for mid in member_ids:            # Proven above
                conn.execute("INSERT OR IGNORE INTO task_assignees (task,"
                             " person) VALUES (?,?)", (tid, mid))
                if mid not in before:
                    new_assignees.append(mid)
        if isinstance(req.get("labels"), list):
            conn.execute("DELETE FROM task_labels WHERE task = ?", (tid,))
            for lab in req["labels"][:20]:
                conn.execute("INSERT OR IGNORE INTO task_labels (task, label)"
                             " VALUES (?,?)", (tid, str(lab)[:60]))
        # ONE log id per event — two different rows never share the dedup id (a
        # resend passes because each row keeps its own, and the repeat-guards
        # below keep the started/finished stories straight)
        changed = {k: req[k] for k in ("title", "description", "priority",
                                       "start", "due") if k in req}
        if changed_by_fields := changed:
            src, ev = logmod.server_event()
            logmod.append(conn, task["project"], src, ev, "task.updated",
                          f"#{task['number']}", actor["person_id"],
                          {"changed": list(changed.keys()),
                           "by": actor["name"]})
        if status_to is not None:
            src, ev = logmod.server_event()
            from_type = conn.execute(
                "SELECT type FROM statuses WHERE project = ? AND key = ?",
                (task["project"], task["status"])).fetchone()
            to_type = conn.execute(
                "SELECT type FROM statuses WHERE project = ? AND key = ?",
                (task["project"], status_to)).fetchone()
            logmod.append(conn, task["project"], src, ev, "task.status_changed",
                          f"#{task['number']}", actor["person_id"],
                          {"from": from_type["type"] if from_type else None,
                           "to": to_type["type"] if to_type else None})
            # #: the work events the timeline consumes (charts read real #
            # starts/finishes, never fabricated now-ish bars). One #
            # task.started rises the first time a task lands in-progress; #
            # task.finished per arrival in done/canceled. (honesty)
            to_t = to_type["type"] if to_type else None
            if to_t == "in_progress":
                seen_started = conn.execute(
                    "SELECT 1 FROM log WHERE project = ? AND type ="
                    " 'task.started' AND json_extract(data, '$.task') = ?"
                    " LIMIT 1", (task["project"], tid)).fetchone()
                if seen_started is None:
                    logmod.append(conn, task["project"], src,
                                  ev + "-started", "task.started",
                                  f"#{task['number']}", actor["person_id"],
                                  {"task": tid, "by": actor["name"]})
            if to_t in ("done", "canceled"):
                logmod.append(conn, task["project"], src,
                              ev + "-finished", "task.finished",
                              f"#{task['number']}", actor["person_id"],
                              {"task": tid, "state": to_t,
                               "by": actor["name"]})
        _sse(conn, task["project"], "task", {"id": tid, "kind": "updated"})
        # 4.26: a NEWLY assigned person is told once — one log row per
        # (task, person) (the ev_id dedups), an SSE event for the page dot
        # and the browser notification
        for person_id in (new_assignees or []):
            ev2 = "as-" + __import__("hashlib").sha1(
                f"{tid}:{person_id}".encode()).hexdigest()[:16]
            # a change that is ONLY a new assignee has no other row's source
            logmod.append(conn, task["project"], "server", ev2, "task.assigned",
                          f"#{task['number']}", actor["person_id"],
                          {"person": person_id, "by": actor["name"]})
            _sse(conn, task["project"], "task",
                 {"id": tid, "kind": "assigned", "person": person_id,
                  "number": task["number"], "title": task["title"]})
    handler._json(200, {"task": _full_task(conn, _task(conn, tid))})


def _archived(handler, conn, pid) -> bool:
    row = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if row and row["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return True
    return False


def task_delete(handler, match, body) -> None:
    """DELETE api/v1/tasks/<id> — the trash: deleted_at set. An
    existing-but-forbidden id 404s like a missing one (: never an existence
    oracle).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid = int(match.group(1))
    task = _task(conn, tid)
    if task is None or not can(conn, actor, "edit_work_packages", task["project"]):
        handler._error(404, "no such task")
        return
    if _archived(handler, conn, task["project"]):
        return
    if _mirror_guard(conn, handler, task["project"]):
        return
    with store.write(conn):
        conn.execute("UPDATE tasks SET deleted_at = ?, updated = ?"
                     " WHERE id = ?", (store.now(), store.now(), tid))
        src, ev = logmod.server_event()
        logmod.append(conn, task["project"], src, ev, "task.deleted",
                      f"#{task['number']}", actor["person_id"],
                      {"title": task["title"]})
        _sse(conn, task["project"], "task", {"id": tid, "kind": "deleted"})
    handler._json(200, {"ok": True})


def link_put(handler, match, body) -> None:
    """PUT api/v1/tasks/<id>/links/<kind>/<other>: the link's other end must exist
    in the SAME project — a cross-project (or missing) id 404s, never 403s (no
    existence oracle).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid, kind, other = (int(match.group(1)), match.group(2),
                        int(match.group(3)))
    task = _task(conn, tid)
    if task is None:
        handler._error(404, "no such task")
        return
    if kind not in ("blocked_by", "relates_to", "parent", "duplicates"):
        handler._error(400, "kind is blocked_by, relates_to, parent or duplicates")
        return
    if not can(conn, actor, "edit_work_packages", task["project"]):
        handler._error(404, "no such task")
        return
    other_row = _task(conn, other)
    if other_row is None or other_row["project"] != task["project"]:
        handler._error(404, "no such task in this project")
        return
    if _archived(handler, conn, task["project"]):
        return
    with store.write(conn):
        conn.execute("INSERT OR IGNORE INTO task_links (task, kind, other)"
                     " VALUES (?,?,?)", (tid, kind, other))
        src, ev = logmod.server_event()
        logmod.append(conn, task["project"], src, ev, "task.updated",
                      f"#{task['number']}", actor["person_id"],
                      {"link": {kind: other}})
    handler._json(200, {"ok": True})


def link_delete(handler, match, body) -> None:
    """DELETE api/v1/tasks/<id>/links/<kind>/<other> — 404 on
    existing-but-forbidden ids, like task_delete.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid, kind, other = (int(match.group(1)), match.group(2),
                        int(match.group(3)))
    task = _task(conn, tid)
    if task is None or not can(conn, actor, "edit_work_packages", task["project"]):
        handler._error(404, "no such task")
        return
    with store.write(conn):
        conn.execute("DELETE FROM task_links WHERE task = ? AND kind = ?"
                     " AND other = ?", (tid, kind, other))
        src, ev = logmod.server_event()
        logmod.append(conn, task["project"], src, ev, "task.updated",
                      f"#{task['number']}", actor["person_id"],
                      {"unlinked": {kind: other}})
    handler._json(200, {"ok": True})


def comments_list(handler, match, body) -> None:
    """GET /api/v1/tasks/<id>/comments — kind, person, body, data, created."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid = int(match.group(1))
    task = _task(conn, tid)
    if task is None or not _can_read(conn, actor, tid, task["project"]):
        handler._error(404, "no such task")
        return
    rows = conn.execute(
        "SELECT c.id, c.kind, c.body, c.data, c.created, c.edited,"
        " c.deleted_at, p.name AS person FROM comments c"
        " JOIN people p ON p.id = c.person WHERE c.task = ?"
        " AND c.deleted_at IS NULL ORDER BY c.id", (tid,)).fetchall()
    handler._json(200, {"comments": [dict(r) for r in rows]})


def comment_add(handler, match, body) -> None:
    """POST api/v1/tasks/<id>/comments — comment.write; kind from the first line
    when the caller didn't set it (: [AURA] started → start …). comment.added
    logged, SSE comment event.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    tid = int(match.group(1))
    task = _task(conn, tid)
    if task is None or not _can_read(conn, actor, tid, task["project"]):
        handler._error(404, "no such task")
        return
    if not _require_write(handler, conn, actor, task["project"],
                          "add_work_package_comments"):
        return
    if _archived(handler, conn, task["project"]):
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    body_text = str(req.get("body") or "").strip()
    if not body_text:
        handler._error(400, "the comment is empty")
        return
    # Comment text goes only scrubbed — a key in it would land in the chain, in
    # the export bundle, in the chat's search preview
    from .api_chat import _scrub
    body_text = _scrub(body_text)
    kind = str(req.get("kind") or "") or _kind_from_text(body_text)
    if kind not in ("text", "start", "done", "decision", "system"):
        kind = "text"
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO comments (task, person, kind, body, data, created)"
            " VALUES (?,?,?,?,?,?)",
            (tid, actor["person_id"], kind, body_text,
             json.dumps(req.get("data") or {}), store.now()))
        src, ev = logmod.server_event()
        logmod.append(conn, task["project"], src, ev, "comment.added",
                      f"#{task['number']}", actor["person_id"],
                      {"kind": kind})
        _sse(conn, task["project"], "comment",
             {"task": tid, "kind": kind})
    handler._json(200, {"comment": dict(conn.execute(
        "SELECT id, kind, body, created FROM comments WHERE id = ?",
        (cur.lastrowid,)).fetchone())})


def _kind_from_text(text: str) -> str:
    low = text.lower()
    if low.startswith("[aura] started"):
        return "start"
    if low.startswith("[aura] done"):
        return "done"
    if low.startswith("decision") or low.startswith("[aura] decision"):
        return "decision"
    return "text"


def comment_patch(handler, match, body) -> None:
    """PATCH api/v1/comments/<id> — the AUTHOR, or team.manage; the old and new
    text both logged. A comment you may not touch 404s (no existence oracle).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    cid = int(match.group(1))
    row = conn.execute(
        "SELECT c.*, t.project, t.number FROM comments c JOIN tasks t"
        " ON t.id = c.task WHERE c.id = ? AND c.deleted_at IS NULL",
        (cid,)).fetchone()
    own = row is not None and row["person"] == actor["person_id"]
    if row is None or \
            (not own and actor["role"] not in ("owner", "admin")):
        handler._error(404, "no such comment")
        return
    if _archived(handler, conn, row["project"]):
        return                           # Archived = read-only, comments too
    _req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    new_body = str(_req.get("body") or "").strip()
    if not new_body_ok(new_body):
        handler._error(400, "the comment is empty")
        return
    from .api_chat import _scrub          # The same law as post
    new_body = _scrub(new_body)
    with store.write(conn):
        conn.execute("UPDATE comments SET body = ?, edited = ? WHERE id = ?",
                     (new_body, store.now(), cid))
        src, ev = logmod.server_event()
        logmod.append(conn, row["project"], src, ev, "comment.edited",
                      f"#{row['number']}", actor["person_id"],
                      {"old": row["body"], "new": new_body})
        _sse(conn, row["project"], "comment", {"task": row["task"],
                                               "kind": "edited"})
    handler._json(200, {"ok": True})


def comment_delete(handler, match, body) -> None:
    """DELETE api/v1/comments/<id> — soft delete, logged; 404 on
    existing-but-forbidden ids.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    cid = int(match.group(1))
    row = conn.execute(
        "SELECT c.*, t.project, t.number FROM comments c JOIN tasks t"
        " ON t.id = c.task WHERE c.id = ? AND c.deleted_at IS NULL",
        (cid,)).fetchone()
    if row is None or \
            (row["person"] != actor["person_id"] and
             actor["role"] not in ("owner", "admin")):
        handler._error(404, "no such comment")
        return
    with store.write(conn):
        conn.execute("UPDATE comments SET deleted_at = ? WHERE id = ?",
                     (store.now(), cid))
        src, ev = logmod.server_event()
        logmod.append(conn, row["project"], src, ev, "comment.deleted",
                      f"#{row['number']}", actor["person_id"], {})
        _sse(conn, row["project"], "comment",
             {"task": row["task"], "kind": "deleted"})
    handler._json(200, {"ok": True})


def new_body_ok(x) -> bool:
    return bool(x and x.strip())


task_get_alias = task_get

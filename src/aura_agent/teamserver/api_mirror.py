"""teamserver/api_mirror.py — the read-only outside-hub mirror.

PUT projects/<id>/mirror — the admin's Aura pushes the wired hub's tasks
(read-only); GET returns them with source:"mirror". When the project's
`tasks_from` setting names a hub, the task WRITE routes on that project refuse
with 409 read_only_mirror — the outside hub stays the source of truth, nothing
is ever written back.
"""
from __future__ import annotations

import json

from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .perms import Forbidden, require, is_owner_admin

MAX_MIRROR = 2_000


def tasks_from(conn, pid: int) -> str | None:
    """The project's tasks_from setting, or None (our own tasks)."""
    try:
        row = conn.execute(
            "SELECT settings FROM projects WHERE id = ?", (pid,)).fetchone()
        if not row or not row["settings"]:
            return None
        s = json.loads(row["settings"])
        v = s.get("tasks_from")
        return str(v) if v else None
    except Exception:                                # noqa: BLE001
        return None


def mirror_put(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body)
    if actor is None:
        return
    pid = int(match.group(1))
    _ar = conn.execute(
        "SELECT archived_at FROM projects WHERE id = ? AND deleted_at IS NULL",
        (pid,)).fetchone()
    if _ar is None:
        handler._error(404, "no such project")
        return
    ok = conn.execute(
        "SELECT 1 FROM project_members m WHERE m.project = ? AND m.person = ?"
        " AND (m.role = 'manager' OR m.role_id ="
        " (SELECT r.id FROM roles r WHERE r.name = 'Project admin'))",
        (pid, actor.get("person_id"))).fetchone() or \
        is_owner_admin(actor)
    if not ok:
        handler._error(403, "the mirror is pushed by a manager (or admin)")
        return
    if _ar["archived_at"]:          # Archived = read-only
        handler._error(409, "this project is archived — unarchive it first")
        return
    try:
        req, _je = _parse_json(body or b"{}")
        if _je is not None:
            handler._error(400, _je)
            return
    except Exception:                                # noqa: BLE001
        handler._error(400, "bad json")
        return
    tasks = req.get("tasks") or []
    if not isinstance(tasks, list) or len(tasks) > MAX_MIRROR:
        handler._error(400, f"a mirror carries at most {MAX_MIRROR} tasks")
        return
    source = str(req.get("source") or "outside-hub")[:60]
    clean = []
    for t in tasks[:MAX_MIRROR]:
        if not isinstance(t, dict):
            continue
        clean.append({
            "key": str(t.get("key") or "")[:60],
            "title": str(t.get("title") or "")[:200],
            "status": str(t.get("status") or "")[:40],
            "assignee": str(t.get("assignee") or "")[:80],
            "updated": t.get("updated") if
            isinstance(t.get("updated"), (int, float)) else store.now()})
    with store.write(conn):
        conn.execute("DELETE FROM mirror_tasks WHERE project = ?", (pid,))
        for t in clean:
            conn.execute(
                "INSERT INTO mirror_tasks (project, key, title, status,"
                " assignee, source, updated) VALUES (?,?,?,?,?,?,?)",
                (pid, t["key"], t["title"], t["status"], t["assignee"],
                 source, store.now()))
    handler._json(200, {"ok": True, "tasks": len(clean), "source": source})


def mirror_get(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL",
            (pid,)).fetchone():
        handler._error(404, "no such project")
        return
    if not is_owner_admin(actor) and not conn.execute(
            "SELECT 1 FROM project_members WHERE project = ? AND person = ?",
            (pid, actor.get("person_id"))).fetchone():
        handler._error(404, "no such project")
        return
    rows = [dict(r) for r in conn.execute(
        "SELECT key, title, status, assignee, source, updated FROM"
        " mirror_tasks WHERE project = ? ORDER BY key", (pid,)).fetchall()]
    handler._json(200, {"tasks": rows, "source":
                        tasks_from(conn, pid) or "own"})

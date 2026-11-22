"""teamserver/api_map.py — the shared code map (plan –8.2).

PUT api/v1/projects/<id>/map — replaces the project's map rows in one write
(needs report.write); every path (relative, no..) and size is re-checked before
storing; entries carry the commit id. GET api/v1/projects/<id>/map — the ranked
map back within the token budget (`team.map_budget`, default 16,000 chars),
plus who is changing which file now and "changed since you last looked"
(`since=<seq>`). 'every entry carries its commit and date': each file row
returns its stored `updated`; symbols hold no per-row date in the schema
(untouched), so the answer carries the map-level `asof` (the newest file
`updated`).
"""
from __future__ import annotations

import json

from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .perms import Forbidden, require

MAX_FILES = 5_000
MAX_SYMBOLS = 20_000
MAX_PATH = 300


def _project_member(conn, actor, pid) -> bool:
    """."""
    from .perms import is_owner_admin
    if is_owner_admin(actor):
        return conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL",
            (pid,)).fetchone() is not None
    return bool(conn.execute(
        "SELECT 1 FROM project_members WHERE project = ? AND person = ?",
        (pid, actor.get("person_id"))).fetchone())


def _clean_path(p) -> str | None:
    if not isinstance(p, str) or not p:
        return None
    p = p.replace("\\", "/").lstrip("/")
    if p.startswith("/") or ".." in p.split("/") or len(p) > MAX_PATH:
        return None
    return p


def map_put(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body)
    if actor is None:
        return
    pid = int(match.group(1))
    if not _project_member(conn, actor, pid):
        handler._error(404, "no such project")
        return
    try:
        require(conn, actor, "aura_work", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:          # Archived = read-only
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
    commit = str(req.get("commit") or "")[:64]
    files = req.get("files") or []
    symbols = req.get("symbols") or []
    if not isinstance(files, list) or len(files) > MAX_FILES or \
            not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS:
        handler._error(400, "the map is too large (5,000 files, "
                            "20,000 symbols)")
        return
    clean_files = []
    for f in files[:MAX_FILES]:
        if not isinstance(f, dict):
            continue
        path = _clean_path(f.get("path"))
        if path is None:
            continue
        try:
            size = max(0, min(int(f.get("size") or 0), 1 << 30))
            rank = max(0.0, min(float(f.get("rank") or 0), 1e12))
        except (TypeError, ValueError):
            size, rank = 0, 0.0
        clean_files.append((path, size, str(f.get("lang") or "")[:24],
                            str(f.get("summary") or "")[:200], rank))
    clean_syms = []
    for s in symbols[:MAX_SYMBOLS]:
        if not isinstance(s, dict):
            continue
        path = _clean_path(s.get("path"))
        if path is None:
            continue
        try:
            line = max(0, min(int(s.get("line") or 0), 1 << 20))
            rank = max(0.0, min(float(s.get("rank") or 0), 1e12))
        except (TypeError, ValueError):
            line, rank = 0, 0.0
        clean_syms.append((path, str(s.get("kind") or "")[:16],
                           str(s.get("name") or "")[:120],
                           str(s.get("signature") or "")[:200], line, rank))
    with store.write(conn):
        conn.execute("DELETE FROM map_files WHERE project = ?", (pid,))
        conn.execute("DELETE FROM map_symbols WHERE project = ?", (pid,))
        for path, size, lang, summary, rank in clean_files:
            conn.execute(
                "INSERT INTO map_files (project, path, size, lang, summary,"
                " rank, commit_id, by_person, updated) VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (pid, path, size, lang, summary, rank, commit,
                 actor.get("person_id"), store.now()))
        for path, kind, name, sig, line, rank in clean_syms:
            conn.execute(
                "INSERT INTO map_symbols (project, path, kind, name,"
                " signature, line, rank) VALUES (?,?,?,?,?,?,?)",
                (pid, path, kind, name, sig, line, rank))
        # A hint only; every Tree refetches its own authorised map.
        from .api_tasks import _sse
        _sse(conn, pid, "map", {"project": pid})
    handler._json(200, {"ok": True, "files": len(clean_files),
                        "symbols": len(clean_syms), "commit": commit})


def _budget(conn) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'team.map_budget'"
        ).fetchone()
        return min(max(int(row[0]), 2_000), 60_000) if row else 16_000
    except Exception:                                # noqa: BLE001
        return 16_000


def map_get(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not _project_member(conn, actor, pid):
        handler._error(404, "no such project")
        return
    try:
        require(conn, actor, "view_work_packages", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    text = (q.get("query") or [""])[0][:120].lower()
    since = int((q.get("since") or [0])[0] or 0)
    budget = _budget(conn)
    files = []
    for r in conn.execute(
            "SELECT path, size, lang, summary, rank, commit_id, updated FROM"
            " map_files WHERE project = ? ORDER BY rank DESC", (pid,)):
        d = dict(r)
        if text and text not in r["path"].lower() and \
                text not in (r["summary"] or "").lower():
            continue
        files.append(d)
    symbols = []
    for r in conn.execute(
            "SELECT path, kind, name, signature, line, rank FROM"
            " map_symbols WHERE project = ? ORDER BY rank DESC", (pid,)):
        d = dict(r)
        if text and text not in r["name"].lower() and \
                text not in r["path"].lower():
            continue
        symbols.append(d)
    # "Now editing" = someone changing the file on a task still open; a change
    # with no task counts for two hours. A finished task's files are free.
    now_editing: dict = {}
    _fresh = store.now() - 2 * 3600
    overlays: dict = {}      # Path → who is where, from the open
                             # tasks' reports (a file.changed's data may
                             # hold `task`; the person lands from actor)
    try:
        for r in conn.execute(
                "SELECT l.subject AS path, l.data AS data,"
                " l.time AS time, p.name AS who FROM log l"
                " LEFT JOIN people p ON p.id = l.actor"
                " WHERE l.project = ? AND l.type = 'file.changed'"
                " ORDER BY l.seq DESC LIMIT 400", (pid,)):
            task_no = None
            try:
                payload = json.loads(r["data"] or "{}")
                task_no = payload.get("task") or payload.get("task_id")
            except ValueError:
                task_no = None
            # an open task only — done ones stop overlaying
            if task_no is None:
                if r["who"] and (r["time"] or 0) > _fresh:
                    now_editing.setdefault(r["path"], r["who"])
                continue
            _strow = conn.execute(
                    "SELECT s.type AS t FROM tasks t JOIN statuses s"
                    " ON s.project = t.project AND s.key = t.status"
                    " WHERE t.project = ? AND t.deleted_at IS NULL"
                    " AND (t.number = ? OR t.id = ?)"
                    " AND s.type IN ('in_progress','in_review','todo')",
                    (pid, task_no, task_no)).fetchone()
            if not _strow:
                continue
            state = {"in_progress": "working", "in_review": "reviewing",
                     "todo": "planned"}.get(_strow["t"], "working")
            if state != "planned" and r["who"]:
                now_editing.setdefault(r["path"], r["who"])
            lst = overlays.setdefault(r["path"], [])
            person = (r["who"] if r["who"] else "?")
            if not any(x.get("person") == person for x in lst):
                lst.append({"person": person, "task": task_no,
                            "state": state})
    except Exception:                                # noqa: BLE001
        overlays = overlays or {}
    collisions = [{"path": pth, "people": lst}
                  for pth, lst in overlays.items() if len(lst) > 1]

    changed_since = []
    if since:
        for r in conn.execute(
                "SELECT type, subject, time FROM log WHERE project = ? AND"
                " seq > ? AND type IN ('file.changed', 'task.started',"
                " 'task.finished') ORDER BY seq DESC LIMIT 50", (pid, since)):
            changed_since.append(dict(r))
    # pack within the budget: ranked files first, then symbols
    asof = conn.execute(
        "SELECT MAX(updated) FROM map_files WHERE project = ?",
        (pid,)).fetchone()[0]
    kept_files, kept_syms = [], []
    used = 512
    for f in files:
        blob = len(json.dumps(f, default=str))
        if used + blob > budget:
            break
        used += blob
        f["now_editing"] = now_editing.get(f["path"], "")
        kept_files.append(f)
    for s in symbols:
        blob = len(json.dumps(s, default=str))
        if used + blob > budget:
            break
        used += blob
        kept_syms.append(s)
    handler._json(200, {
        "commit": (files[0].get("commit_id") or "") if files else "",
        "files": kept_files, "symbols": kept_syms,
        "changed_since": changed_since,
        "overlays": [{"path": pth, "people": lst}
                     for pth, lst in overlays.items()][:200],
        "collisions": collisions[:50],
        "budget": budget, "used": used, "asof": asof,
        "note": "open the real file before changing it"})

"""teamserver/api_views.py — the Project tab's live views (plan).

GET projects/<id>/activity — the log as sentences; detail=0 shows KEY MOMENTS
only (task.started/finished, FAILING test.ran, task.blocked, decision.made,
overlap); detail=1 needs `activity.detail` (the server's perms decide; the
response always says `detail_allowed` so the page never guesses from a role).
Filters: type, person, q, before. Every row carries `task_ref` (number+title)
when its data names a task. GET projects/<id>/who — per person: open tasks,
branch, last files (each dated), overlaps (dated); quiet 2 h → faint, 24 h →
"Not active". GET projects/<id>/tree — the project MAP (map_files) joined with
the latest file.changed per path (who/when), plus log-only paths; open tasks'
planned files are flagged `active`.
"""
from __future__ import annotations

import hashlib
import json

from . import store
from .api_people import _actor, _db
from .perms import Forbidden, can, detail_settings_of, require

KEY_TYPES = ("task.started", "task.finished", "test.ran", "task.blocked",
             "decision.made", "overlap")
QUIET_AFTER = 2 * 3600
INACTIVE_AFTER = 24 * 3600
PAGE = 100



def _num(v, default=0):
    """Hostile-shape-safe int: garbage in → default out."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

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


def _detail_settings(conn, pid: int) -> dict:
    row = conn.execute("SELECT settings FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    try:
        settings = json.loads(row["settings"]) if row and row["settings"] \
            else {}
    except Exception:                                # noqa: BLE001
        settings = {}
    return detail_settings_of(settings, pid)


def _sentence(etype: str, data: dict, subject: str) -> str:
    if etype == "task.started":
        return (f"started #{data.get('task')} — {subject}" +
                (f" (branch {data['branch']})" if data.get("branch") else ""))
    if etype == "task.finished":
        changed = data.get("changed") or []
        return (f"finished #{data.get('task')} {subject} — "
                f"{len(changed)} file{'' if len(changed) == 1 else 's'}" +
                (f", commit {str(data.get('commit'))[:8]}" if data.get("commit")
                 else "") + (" (pushed)" if data.get("pushed") else ""))
    if etype == "test.ran":
        return (f"ran {subject[:50]} — {data.get('passed', 0)} passed" +
                (f", {data['failed']} FAILED" if data.get("failed")
                 else ", all green"))
    if etype == "task.blocked":
        return f"#{data.get('task')} blocked by {data.get('by', 'something')}"
    if etype == "decision.made":
        return f"decided: {subject[:70]}"
    if etype == "overlap":
        return (f"overlap on {data.get('path')} — " +
                ", ".join(data.get("people") or []))
    # a sentence for every event type the journal can show — evidence
    # completeness is the log's job; a bare subject is the flagged last resort,
    # never the norm
    if etype == "task.assigned":
        return f"assigned #{data.get('task')} to {data.get('name', 'them')}"
    if etype == "export.made":
        return (f"exported the log — {data.get('rows', '?')} events, "
                f"head {str(data.get('head', ''))[:8]}…")
    if etype == "log.verified":
        return (f"checked the chain — {data.get('checked', '?')} events, "
                f"{'intact' if data.get('ok') else 'BROKEN'}")
    if etype == "hub.backup":
        return f"hub backed up — head {str(data.get('head', ''))[:8]}…"
    if etype in ("settings.changed",):
        return f"changed setting {data.get('key', '…')} → {data.get('value', '…')}"
    if etype == "person.joined":
        return f"{data.get('name', 'someone')} joined the team"
    if etype == "person.renamed":
        return (f"{data.get('from', '?')} is now called "
                f"{data.get('to', '?')}")
    if etype == "invite.made":
        return "made a sign-in invite"
    if etype.endswith(".created") and etype.startswith("task."):
        return f"created #{data.get('task')} {subject[:50]}"
    return subject[:80]


def activity(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not _project_member(conn, actor, pid):
        handler._error(404, "no such project")
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    detail = q.get("detail", ["0"])[0] == "1"
    from .perms import detail_ok, allowed
    if not allowed(conn, actor, "view_activity", pid):
        handler._error(403, "this project's activity isn't yours to see")
        return
    detail_allowed = detail_ok(conn, actor, pid)
    if detail and not detail_allowed:
        handler._error(403, "the detail level isn't yours on this project")
        return
    type_f = (q.get("type") or [None])[0]
    person = (q.get("person") or [None])[0]
    text = (q.get("q") or [""])[0][:100]
    before = _num((q.get("before") or [0])[0] or 0)
    sql = ("SELECT l.seq, l.type, l.subject, l.data, l.time, l.source, l.actor,"
           " p.name AS who"
           " FROM log l LEFT JOIN people p ON p.id = l.actor"
           " WHERE l.project = ? AND (l.source = 'server' OR"
           " l.source LIKE 'device:%')")
    args: list = [pid]
    if not detail:
        marks = ",".join("?" * len(KEY_TYPES))
        sql += f" AND l.type IN ({marks})"
        args.extend(KEY_TYPES)
    if type_f:
        sql += " AND l.type = ?"
        args.append(type_f)
    if person:
        sql += " AND p.name = ?"
        args.append(person)
    if before:
        sql += " AND l.seq < ?"
        args.append(before)
    sql += " ORDER BY l.seq DESC LIMIT ?"
    args.append(PAGE)
    out = []
    trefs: dict = {}

    def task_ref(tid):
        """Resolve data.task (a task id) to its #{number} and title once per
        request — the decisions view renders these as chips (: a decision
        carries its task).
        """
        try:
            tid = _num(tid)
        except (TypeError, ValueError):
            return None
        if tid not in trefs:
            row = conn.execute(
                "SELECT number, title FROM tasks WHERE id = ? AND"
                " project = ? AND deleted_at IS NULL",
                (tid, pid)).fetchone()
            trefs[tid] = ({"number": row["number"], "title": row["title"]}
                          if row else None)
        return trefs[tid]

    for r in conn.execute(sql, args):
        try:
            data = json.loads(r["data"] or "{}")
        except Exception:                            # noqa: BLE001
            data = {}
        if r["type"] == "test.ran" and not detail and not data.get("failed"):
            continue                                 # Green runs are detail
        # A card moved by an Aura: the hub's own start/finish row (kept for the
        # charts) and the Aura's report of the same moment are one thing — the
        # feed shows the Aura's, which carries the branch, files and commit
        if (r["source"] == "server" and r["type"] in ("task.started", "task.finished")
                and data.get("task") is not None and conn.execute(
                    "SELECT 1 FROM log WHERE project = ? AND type = ? AND actor = ?"
                    " AND source LIKE 'device:%' AND json_extract(data, '$.task') = ?"
                    " AND ABS(time - ?) < 600 LIMIT 1",
                    (pid, r["type"], r["actor"], data.get("task"), r["time"])).fetchone()):
            continue
        # Search never looks inside what the person's level hides — the data
        # json is searched only when detail is allowed; the subject is always
        # the row's face
        if text:
            needle = text.lower()
            in_subject = needle in (r["subject"] or "").lower()
            in_data = detail_allowed and needle in json.dumps(data).lower()
            if not (in_subject or in_data):
                continue
        ev = {"seq": r["seq"], "type": r["type"], "who": r["who"],
              "sentence": _sentence(r["type"], data,
                                    r["subject"] or ""),
              "data": (data if detail else
                       {k: data.get(k) for k in
                        ("task", "changed", "commit", "failed")}),
              "ts": r["time"]}
        ref = task_ref(data.get("task"))
        if ref:
            ev["task_ref"] = ref
            # an Aura's start names the task by the request it was given (a
            # long instruction); the line names the board's own title
            if r["type"] in ("task.started", "task.finished"):
                verb = "started" if r["type"] == "task.started" else "finished"
                ev["sentence"] = _sentence(r["type"], {**data, "task": ref["number"]},
                                           ref["title"]).replace(
                    f"{verb} #{ref['number']} — ", f"{verb} #{ref['number']} ", 1)
        out.append(ev)
    handler._json(200, {"events": out, "detail": detail,
                        "detail_allowed": detail_allowed,
                        "total": len(out),
                        "total_all": conn.execute(
                            "SELECT COUNT(*) AS n FROM log WHERE project = ?"
                            " AND (source = 'server' OR source LIKE 'device:%')",
                            (pid,)).fetchone()["n"],
                        "hidden_by_level": 0 if detail else conn.execute(
                            "SELECT COUNT(*) AS n FROM log WHERE project = ?"
                            " AND (source = 'server' OR source LIKE 'device:%')"
                            f" AND type NOT IN ({','.join('?' * len(KEY_TYPES))})",
                            (pid, *KEY_TYPES)).fetchone()["n"],
                        "first_ts": conn.execute(
                            "SELECT MIN(time) AS t FROM log WHERE project = ? AND"
                            " (source = 'server' OR source LIKE 'device:%')",
                            (pid,)).fetchone()["t"],
                        "last_verify": (lambda r: None if r is None else {
                            "time": r["t"],
                            **__import__("json").loads(r["d"] or "{}")
                          })(conn.execute(
                            "SELECT l.time AS t, l.data AS d FROM log l"
                            " WHERE l.project = ? AND l.type = 'log.verified'"
                            " ORDER BY l.seq DESC LIMIT 1",
                            (pid,)).fetchone())})


def _open_tasks(conn, pid: int):
    """Open tasks (in_progress/in_review) with their assignees; the status
    TYPE is the truth — tasks.status is the per-project status KEY."""
    rows = conn.execute(
        "SELECT t.id, t.number, t.title, t.status, s.type AS stype,"
        " s.name AS sname FROM tasks t JOIN statuses s ON s.project ="
        " t.project AND s.key = t.status"
        " WHERE t.project = ? AND t.deleted_at IS NULL"
        " AND s.type IN ('in_progress', 'in_review')", (pid,)).fetchall()
    out = []
    for r in rows:
        assignees = list(conn.execute(
            "SELECT person FROM task_assignees WHERE task = ?",
            (r["id"],)).fetchall())
        if not assignees:
            d = dict(r)
            d["assigned_to"] = None
            out.append(d)
            continue
        for a in assignees:
            d = dict(r)
            d["assigned_to"] = a["person"]
            out.append(d)
    return out


def _task_paths(conn, pid: int, task_id: int) -> set:
    paths = set()
    for r in conn.execute(
            "SELECT subject, data, type FROM log WHERE project = ? AND"
            " type IN ('file.changed', 'task.started')"
            " ORDER BY seq DESC LIMIT 400", (pid,)):
        try:
            d = json.loads(r["data"] or "{}")
        except Exception:                            # noqa: BLE001
            continue
        if d.get("task") != task_id:
            continue
        if r["subject"] and r["type"] == "file.changed":
            paths.add(r["subject"])
        paths.update(d.get("planned_files") or [])
    return paths


def who(handler, match, body) -> None:
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
    now = store.now()
    cards: dict = {}
    for r in _open_tasks(conn, pid):
        key = r["assigned_to"] or 0
        if key not in cards:
            prow = conn.execute("SELECT name FROM people WHERE id = ?",
                                (key,)).fetchone() if key else None
            cards[key] = {
                "person": (prow["name"] if prow else None) or "unassigned",
                "tasks": [], "files": [], "branch": "", "last_ts": None}
        card = cards[key]
        card["tasks"].append({"number": r["number"], "status": r["sname"],
                              "status_type": r["stype"], "_id": r["id"]})
        started = conn.execute(
            "SELECT data FROM log WHERE project = ? AND type ="
            " 'task.started' AND json_extract(data, '$.task') = ?"
            " ORDER BY seq DESC LIMIT 1", (pid, r["id"])).fetchone()
        if started:
            try:
                card["branch"] = json.loads(started["data"]).get("branch") \
                    or card["branch"]
            except Exception:                        # noqa: BLE001
                pass
    for key, card in cards.items():
        last = None
        # Per person: last 10 file.changed, each chip dated (: "every entry
        # dated")
        if key:
            for fr in conn.execute(
                    "SELECT subject, time FROM log WHERE project = ? AND"
                    " type = 'file.changed' AND actor = ?"
                    " ORDER BY seq DESC LIMIT 40", (pid, key)):
                if (len(card["files"]) >= 10
                        or any(f["path"] == fr["subject"] for f in card["files"])):
                    continue                         # A file once, at its latest change
                card["files"].append({"path": fr["subject"],
                                      "ts": fr["time"]})
                last = max(last or 0, fr["time"])
        card["last_ts"] = last
        age = now - last if last else None
        # quiet = no event 2 h; Not active = 24 h. A person on an OPEN
        # task with no events yet is on it NOW (they just started).
        card["quiet"] = age is not None and QUIET_AFTER < age
        card["inactive"] = age is not None and age > INACTIVE_AFTER
    overlaps = []
    for r in conn.execute(
            "SELECT data, time FROM log WHERE project = ? AND type ="
            " 'overlap' ORDER BY seq DESC LIMIT 20", (pid,)):
        try:
            entry = json.loads(r["data"])
            entry["ts"] = r["time"]                   # Overlaps are dated
            overlaps.append(entry)
        except Exception:                            # noqa: BLE001
            pass
    active = [c for c in cards.values() if not c["inactive"]]
    handler._json(200, {"active": active,
                        "not_active": [c for c in cards.values()
                                       if c["inactive"]],
                        "overlaps": overlaps})


def tid_of(card, task) -> int:
    return task["_id"] if "_id" in task else 0


def task_number_to_id(conn, pid: int, number: int) -> int | None:
    row = conn.execute("SELECT id FROM tasks WHERE project = ? AND number = ?",
                       (pid, number)).fetchone()
    return row["id"] if row else None


TREE_LIMIT = 1000
TREE_DEPTH = 12                            # Deepest path shown expanded
TREE_LINES = 60                            # The digest tree's line cap


def _tree_text(root: dict, prefix: str = "", depth: int = 0,
               out: list | None = None) -> list[str]:
    """Indented text tree for the digest; a directory appears before its
    children (depth order through recursion) and carries its rolled-up
    size in human units."""
    if out is None:
        out = []
    if depth > TREE_DEPTH:
        return out
    for name in sorted(root["dirs"]):
        d = root["dirs"][name]
        out.append(f"{prefix}{name}/ ({_human(d['size'])})")
        _tree_text(d, prefix + "  ", depth + 1, out)
    for f in sorted(root["files"], key=lambda x: x["path"]):
        base = f["path"].rsplit("/", 1)[-1]
        out.append(f"{prefix}{base} ({_human(f['size'])})"
                   if f.get("size") is not None else f"{prefix}{base}")
    return out


def _human(size) -> str:
    if size is None:
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1 << 20:
        return f"{size / 1024:.1f} kB"
    return f"{size / (1 << 20):.1f} MB"


def _tree_rows(conn, pid: int) -> list[dict]:
    """The folder tree's rowset: map_files IS the project map — every mapped file
    is listed, touched or not; the latest file.changed per path supplies WHO
    touched it last and WHEN (else the map's own `by_person`/`updated` count);
    open tasks' planned/touched files are flagged `active` (who touches it
    NOW). Paths only seen in the log (a file the map has not indexed yet) are
    listed too, size-less.
    """
    rows: dict = {}
    for r in conn.execute(
            "SELECT f.path, f.size, f.updated, p.name AS mapper"
            " FROM map_files f LEFT JOIN people p ON p.id = f.by_person"
            " WHERE f.project = ?", (pid,)):
        rows[r["path"]] = {"path": r["path"], "size": r["size"],
                           "who": r["mapper"], "when": r["updated"],
                           "active": 0}
    for r in conn.execute(
            "SELECT l.subject AS path, l.time, p.name AS who FROM log l"
            " LEFT JOIN people p ON p.id = l.actor"
            " WHERE l.project = ? AND l.type = 'file.changed' AND"
            " l.seq IN (SELECT MAX(seq) FROM log WHERE project = ? AND"
            " type = 'file.changed' GROUP BY subject)", (pid, pid)):
        row = rows.get(r["path"])
        if row is None:
            row = {"path": r["path"], "size": None, "who": None,
                   "when": None, "active": 0}
            rows[r["path"]] = row
        row["who"] = r["who"] or row["who"]
        if r["time"]:
            row["when"] = max(row["when"] or 0, r["time"])
    active: set = set()
    for t in _open_tasks(conn, pid):
        active |= _task_paths(conn, pid, t["id"])
    for path in active:
        if path in rows:
            rows[path]["active"] = 1
    # depth-sorted: shallow paths first, alphabetical within a depth
    return sorted(rows.values(),
                  key=lambda f: (f["path"].count("/"), f["path"]))


def _tree_nested(flat: list[dict]) -> dict:
    """flat rows → nested dirs/files, each directory's size the ROLLUP
    of everything under it."""
    root = {"size": 0, "files": [], "dirs": {}}
    for f in flat:
        parts = f["path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(
                part, {"size": 0, "files": [], "dirs": {}})
        node["files"].append(f)
        node = root
        node["size"] += f.get("size") or 0
        for part in parts[:-1]:
            node = node["dirs"][part]
            node["size"] += f.get("size") or 0
    return root


def tree(handler, match, body) -> None:
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
    flat = _tree_rows(conn, pid)[:TREE_LIMIT]
    handler._json(200, {"files": flat, "tree": _tree_nested(flat)})


# ---- gantt, diagram, digest ----

def _gantt_rows(conn, pid: int) -> list[dict]:
    rows = []
    for t in conn.execute(
            "SELECT t.id, t.number, t.title, t.start, t.due, s.type AS"
            " stype, s.name AS sname, t.created, t.merged_at FROM tasks t"
            " JOIN statuses s ON s.project = t.project AND s.key = t.status"
            " WHERE t.project = ? AND t.deleted_at IS NULL", (pid,)):
        started = conn.execute(
            "SELECT time FROM log WHERE project = ? AND type ="
            " 'task.started' AND json_extract(data, '$.task') = ?"
            " ORDER BY seq ASC LIMIT 1", (pid, t["id"])).fetchone()
        finished = conn.execute(
            "SELECT time FROM log WHERE project = ? AND type ="
            " 'task.finished' AND json_extract(data, '$.task') = ?"
            " ORDER BY seq DESC LIMIT 1", (pid, t["id"])).fetchone()
        start = t["start"] or (started["time"] if started else None)
        end = t["due"] or (finished["time"] if finished else (
            max(start, store.now()) if start and t["stype"] in
            ("in_progress", "in_review") else start))
        # A date is scheduled ONLY when it has a real basis — an explicit
        # start/due or a task.started event. Never-started, undated work stays
        # NULL: the page puts it in the honest "unscheduled" strip instead of
        # fabricating a 1970/now bar (part-4 research law).
        scheduled = bool(start)
        progress = 0
        if t["stype"] in ("in_progress", "in_review"):
            progress = 50
        elif t["stype"] in ("done", "canceled"):
            progress = 100
        blocked_by = [x["number"] for x in conn.execute(
            "SELECT t2.number AS number FROM task_links l"
            " JOIN tasks t2 ON t2.id = l.other WHERE l.task = ?"
            " AND l.kind = 'blocked_by'", (t["id"],)).fetchall()]
        people = [r["name"] for r in conn.execute(
            "SELECT p.name FROM task_assignees a JOIN people p ON p.id = a.person"
            " WHERE a.task = ? ORDER BY p.name", (t["id"],))]
        rows.append({"number": t["number"], "title": t["title"],
                     "status_type": t["stype"], "status": t["sname"],
                     "start": start, "end": end,
                     "scheduled": scheduled, "progress": progress,
                     "blocked_by": blocked_by, "people": people,
                     "merged_at": t["merged_at"],
                     "events": _task_events(conn, pid, t["id"])})
    return rows


def _task_events(conn, pid: int, tid: int) -> list[dict]:
    """The moments the Flow timeline marks on a task's bar, from the log:
    file changes (who, which file), test runs (passed or not), the finish
    (commit, pushed). Times are the hub's own clock."""
    out = []
    for r in conn.execute(
            "SELECT l.type, l.time, l.data, p.name AS who FROM log l"
            " LEFT JOIN people p ON p.id = l.actor WHERE l.project = ?"
            " AND l.source LIKE 'device:%' AND json_extract(l.data, '$.task') = ?"
            " AND l.type IN ('file.changed', 'test.ran', 'task.finished',"
            " 'task.blocked') ORDER BY l.seq LIMIT 400", (pid, tid)):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            d = {}
        kind = "file" if r["type"] == "file.changed" else r["type"].split(".", 1)[1]
        ev = {"t": r["time"], "kind": kind, "who": r["who"]}
        if r["type"] == "file.changed":
            ev["path"] = d.get("path")
        elif r["type"] == "test.ran":
            ev["ok"] = bool(d.get("ok", not d.get("failed")))
            ev["passed"], ev["failed"] = d.get("passed", 0), d.get("failed", 0)
        elif r["type"] == "task.finished":
            ev["commit"] = str(d.get("commit") or "")[:12]
            ev["pushed"] = bool(d.get("pushed"))
        out.append(ev)
    return out


def _clashes(rows: list[dict]) -> list[dict]:
    """Two people's tasks that changed the same file while both were open —
    the moment Teams exists to show. One entry per (path, pair of tasks)."""
    spans = []
    for r in rows:
        if not r["start"]:
            continue
        end = r["end"] or r["start"]
        for ev in r["events"]:
            if ev["kind"] == "file" and ev.get("path"):
                spans.append((ev["path"], r["number"], ev.get("who"), ev["t"],
                              r["start"], end))
    seen: dict = {}
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if a[0] != b[0] or a[1] == b[1] or a[2] == b[2]:
                continue
            # one change happened while the other task was open
            if not (b[4] <= a[3] <= b[5] or a[4] <= b[3] <= a[5]):
                continue
            key = (a[0], min(a[1], b[1]), max(a[1], b[1]))
            at = max(min(a[3], b[3]), max(a[4], b[4]))
            if key not in seen or at < seen[key]["at"]:
                seen[key] = {"path": a[0], "tasks": [key[1], key[2]],
                             "people": sorted({a[2], b[2]} - {None}), "at": at}
    return sorted(seen.values(), key=lambda c: c["at"])


def gantt(handler, match, body) -> None:
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
    rows = _gantt_rows(conn, pid)
    handler._json(200, {"tasks": rows, "clashes": _clashes(rows),
                        "now": store.now()})


def _mq(text: str) -> str:
    """Escape a name for a Mermaid double-quoted label.: a label coming in with
    newlines makes one node look like several — they leave, the same word
    stays.
    """
    return (str(text or "?").replace("\\", "\\\\")
            .replace("\n", " ").replace("\r", " ")
            .replace('"', "#quot;").replace("[", "&#91;")
            .replace("]", "&#93;").replace("(", "&#40;")
            .replace(")", "&#41;"))[:80]


def _slug(text: str) -> str:
    import hashlib as _h
    return "n" + _h.sha1(str(text).encode()).hexdigest()[:10]


def _dir_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _cochange_edges(conn, pid: int, allowed: set | None = None,
                    limit: int = 40) -> list:
    """REAL structure edges, derived inside api_views (no schema change): two
    files touched within the SAME task — file.changed carries its task ref in
    data — are co-change neighbours, weighted by how often. The map stores no
    import graph, so the work history supplies the edges while the map supplies
    the nodes.
    """
    by_task: dict = {}
    for r in conn.execute(
            "SELECT subject, json_extract(data, '$.task') AS task"
            " FROM log WHERE project = ? AND type = 'file.changed'",
            (pid,)):
        if r["task"] is None or not r["subject"]:
            continue
        if allowed is not None and r["subject"] not in allowed:
            continue
        by_task.setdefault(int(r["task"]), set()).add(r["subject"])
    counts: dict = {}
    for files in by_task.values():
        fs = sorted(files)
        for i, a in enumerate(fs):
            for b in fs[i + 1:]:
                counts[(a, b)] = counts.get((a, b), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def diagram(handler, match, body) -> None:
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
    kind = (q.get("kind") or ["tasks"])[0]
    symbol = (q.get("symbol") or [""])[0][:120]
    lines: list[str] = []
    note = ""
    if kind == "modules":
        lines.append("graph LR")
        files = [r["path"] for r in conn.execute(
            "SELECT path FROM map_files WHERE project = ?"
            " ORDER BY rank DESC, path LIMIT 40", (pid,))]
        for p in files:
            lines.append(f'  {file_id(p)}["{_mq(p)}"]')
        if not files:
            lines.append("  %% no map yet — Aura sends it after a scan")
        linked: set = set()
        for (a, b), _n in _cochange_edges(conn, pid, set(files), 40):
            lines.append(f"  {file_id(a)} --> {file_id(b)}")
            linked |= {a, b}
        # a file no one co-touched yet links ONCE inside its own
        # directory, so young logs still show the directory structure
        by_dir: dict = {}
        for p in files:
            by_dir.setdefault(_dir_of(p), []).append(p)
        n_edges = len(linked) // 2

        for same_dir in by_dir.values():
            if len(same_dir) < 2 or n_edges >= 40:
                continue
            for p in same_dir[1:]:
                if p in linked or same_dir[0] == p or n_edges >= 40:
                    continue
                lines.append(
                    f"  {file_id(same_dir[0])} --> {file_id(p)}")
                linked.add(p)
                n_edges += 1
        note = ("nodes: top map files by rank; edges: co-change from the"
                " log (files touched in the same task), plus one"
                " same-directory link for files no one touched yet")
    elif kind == "flow":
        lines.append("graph TD")
        # a feature's flow, honestly derived: the files that moved in
        # the SAME tasks as the file holding the symbol — the log's task
        # refs are the only flow signal the server stores
        root_path = None
        if symbol:
            row = conn.execute(
                "SELECT path FROM map_symbols WHERE project = ?"
                " AND name = ? ORDER BY rank DESC LIMIT 1",
                (pid, symbol)).fetchone()
            root_path = row["path"] if row else None
        if root_path is None:
            row = conn.execute(
                "SELECT path FROM map_files WHERE project = ?"
                " ORDER BY rank DESC, path LIMIT 1", (pid,)).fetchone()
            root_path = row["path"] if row else None
        label = symbol or root_path or "?"
        lines.append(f'  root["{_mq(label)}"]')
        partners: dict = {}
        for (a, b), n in _cochange_edges(conn, pid, None, 10_000):
            if a == root_path:
                partners[b] = partners.get(b, 0) + n
            elif b == root_path:
                partners[a] = partners.get(a, 0) + n
        for p, _n in sorted(partners.items(),
                            key=lambda kv: (-kv[1], kv[0]))[:25]:
            lines.append(f'  {file_id(p)}["{_mq(p)}"]')
            lines.append(f"  {file_id(p)} --> root")
        if not partners and root_path is None:
            lines.append("  %% no map and no log yet")
        note = (f"files that moved in the same task as {_brief(label)} "
                f"(its file {root_path or '—'}), by co-change count")
    else:                                            # Tasks (default)
        lines.append("graph LR")
        nodes, edges = [], []
        for t in _gantt_rows(conn, pid):
            if t["status_type"] == "canceled":
                continue
            # every task — done ones too (the page fades them); a task with
            # no links is still part of the project
            nodes.append({"id": t["number"], "title": t["title"],
                          "status_type": t["status_type"], "people": t["people"]})
            lines.append(f'  t{t["number"]}["#{_mq(t["number"])} '
                         f'{_mq(t["title"])}"]')
            for b in t["blocked_by"]:
                lines.append(f"  t{t['number']} -.-> t{b}")
                edges.append({"from": b, "to": t["number"]})   # Blocker -> blocked
        note = "every task; dashed arrow = blocked by"
        handler._json(200, {"kind": kind, "mermaid": "\n".join(lines),
                            "note": note, "nodes": nodes, "edges": edges})
        return
    handler._json(200, {"kind": kind, "mermaid": "\n".join(lines),
                        "note": note})


def _brief(text: str) -> str:
    s = str(text or "?")
    return f"'{s[:40]}'"


def files_list(conn, pid: int):
    return conn.execute(
        "SELECT path, rank FROM map_files WHERE project = ?"
        " ORDER BY rank DESC LIMIT 40", (pid,)).fetchall()


def file_id(path: str) -> str:
    import hashlib as _h
    return "f" + _h.sha1(path.encode()).hexdigest()[:10]


def digest(handler, match, body) -> None:
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
    pro = conn.execute("SELECT name, slug FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    counts = {}
    for r in conn.execute(
            "SELECT s.type AS t, COUNT(*) AS n FROM tasks t"
            " JOIN statuses s ON s.project = t.project AND s.key = t.status"
            " WHERE t.project = ? AND t.deleted_at IS NULL GROUP BY s.type",
            (pid,)):
        counts[r["t"]] = r["n"]
    open_tasks = []
    for r in conn.execute(
            "SELECT t.number, t.title, s.name AS sname FROM tasks t"
            " JOIN statuses s ON s.project = t.project AND s.key = t.status"
            " WHERE t.project = ? AND t.deleted_at IS NULL AND s.type IN"
            " ('todo', 'in_progress', 'in_review', 'blocked')", (pid,)):
        open_tasks.append(f"- #{r['number']} {r['title']} ({r['sname']})")
    files = [(r["path"], r["summary"]) for r in conn.execute(
        "SELECT path, summary FROM map_files WHERE project = ?"
        " ORDER BY rank DESC LIMIT 15", (pid,))]
    map_rows = conn.execute(
        "SELECT path, size FROM map_files WHERE project = ?"
        " ORDER BY path", (pid,)).fetchall()
    decisions = []
    for r in conn.execute(
            "SELECT subject, data, time, p.name AS who FROM log l"
            " LEFT JOIN people p ON p.id = l.actor WHERE l.project = ? AND"
            " l.type = 'decision.made' ORDER BY l.seq DESC LIMIT 10",
            (pid,)):
        try:
            d = json.loads(r["data"] or "{}")
        except Exception:                            # noqa: BLE001
            d = {}
        parts = [r["subject"][:80]]
        if d.get("text"):
            parts.append(str(d["text"])[:120])
        # one leading dash per line — the dash is added at join time
        decisions.append(" — ".join(parts) + f" ({r['who'] or '?'})")
    # (part 7): the digest STAMPS first — project, date + period, the chain
    # head's short fingerprint, and the size as a budget. A trust artifact that
    # can't say WHICH day it describes isn't one.
    from datetime import datetime, timezone
    proj_name = pro["name"] if pro else str(pid)
    first_log = conn.execute(
        "SELECT MIN(time) AS t FROM log WHERE project = ?", (pid,)).fetchone()
    head = conn.execute(
        "SELECT seq, hash FROM log_heads WHERE chain = ?",
        (f"p{pid}",)).fetchone()
    head_fp = f"{head['seq']}·{head['hash'][:4]}" if head else "0"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    period = (datetime.fromtimestamp(first_log["t"], timezone.utc)
              .strftime("%Y-%m-%d") if first_log and first_log["t"]
              else today)
    blocked_n = sum(1 for line in open_tasks)
    done_n = counts.get("done", 0)
    open_n = sum(v for k, v in counts.items() if k not in ("done", "canceled"))
    situation = (f"{open_n} open task{'s' if open_n != 1 else ''} "
                 f"({counts.get('in_progress', 0)} in progress), "
                 f"{done_n} done so far; decisions and every file change "
                 f"are on the append-only chain, head {head_fp}.")
    stamp = [f"# {proj_name} — digest", "",
             f"project: {proj_name} · date: {today} · covers: {period} → "
             f"today",
             f"chain head: {head_fp} · the full history is the export, "
             f"this is the human digest",
             ""]
    md = stamp + [
          "## Situation",
          situation,
          "",
          "## Tasks",
          ", ".join(f"{k}: {v}" for k, v in counts.items()) or "none",
          ""]
    if open_tasks:
        md.append("## Open")
        md += open_tasks
        md.append("")
    if map_rows:
        # The digest carries the folder tree (map paths only, directories
        # before their children, sizes rolled up); bounded so a 5k-file map
        # can't blow the token line
        flat = [{"path": r["path"], "size": r["size"]} for r in map_rows]
        tree_lines = _tree_text(_tree_nested(flat))
        md.append("## Tree")
        md += tree_lines[:TREE_LINES]
        if len(tree_lines) > TREE_LINES:
            md.append(f"… ({len(tree_lines) - TREE_LINES} more)")
        md.append("")
    if files:
        md.append("## Key files (map)")
        md += [f"- `{p}` — {s}" for p, s in files]
        md.append("")
    if decisions:
        md.append("## Recent decisions")
        md += [f"- {x}" for x in decisions]
        md.append("")
    text = "\n".join(md)
    handler._json(200, {"markdown": text,
                        "tokens_approx": len(text) // 4,
                        "note": "log- and task-derived only; never chat"})


def check_overlap(conn, pid: int, task_id: int, path: str, who: str) -> None:
    """A file.changed / task.started arrival: another person's OPEN task on the
    same path → ONE `overlap` log entry (ev_id = ov-…, UNIQUE (source, ev_id)
    makes a resend a no-op) + an SSE event.
    """
    others = []
    for r in _open_tasks(conn, pid):
        if r["id"] == task_id:
            continue
        if r["assigned_to"]:
            prow = conn.execute("SELECT name FROM people WHERE id = ?",
                                (r["assigned_to"],)).fetchone()
            person = prow["name"] if prow else "unassigned"
        else:
            person = "unassigned"
        if person == who:
            continue
        if path in _task_paths(conn, pid, r["id"]):
            others.append((r["id"], person))
    if not others:
        return
    from . import log as logmod
    people = sorted({name for _, name in others} | {who})
    tasks = sorted({task_id} | {tid for tid, _ in others})
    ev_id = "ov-" + hashlib.sha1(
        f"{','.join(map(str, tasks))}:{path}".encode()).hexdigest()[:16]
    if logmod.append(conn, pid, "server", ev_id, "overlap", path, None,
                     {"path": path, "people": people, "tasks": tasks},
                     store.now()) is None:
        return
    # Until now this published inside reports_in's transaction: an observer
    # could read pre-commit overlap rows or see an event for a rolled-back
    # log insert. Preserve the existing task filter through the deferred seam.
    from .api_tasks import _sse
    _sse(conn, pid, "task", {"kind": "overlap", "path": path,
                            "people": people})

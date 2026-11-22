"""teamserver/api_log.py — the log's verify/head and the reports in (plan –5.2).

Also THE post-commit publish seam: dispatch_pending scans the log and chat for
rows newer than the last run and serves all three / push paths — team notices,
linked-room relays, signed webhooks — from ONE place, OUTSIDE any write
transaction. Being log-driven instead of call-site-driven is what lets the
notice hook see events appended by code that must not call into the notice
layer: api_views.check_overlap appends its `overlap` row here and the
dispatcher picks it up after commit, like any other event. Callers: reports_in
(after its write commits), the __main__ route wrapper (after every non-GET),
and the __main__ background ticker (catch-up after a restart).
"""
from __future__ import annotations

import json
import re
import secrets
import threading

from . import log as logmod
from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .perms import Forbidden, can, require

# The allow-list: type -> (required data fields, allowed fields — everything
# else is dropped at the door, not filtered afterwards)
REPORT_TYPES = {
    "task.started": (["task"], {"task", "branch", "planned_files"}),
    "file.changed": (["path"], {"task", "path", "added", "removed"}),
    # Aura sends the runner's name, never the command line (its words stay on
    # the person's machine); "command" is still taken from older Auras
    "test.ran": (["ok"], {"task", "runner", "command", "passed", "failed", "ok"}),
    "command.changed": (["command"], {"task", "command"}),
    "decision.made": (["text"], {"task", "text", "why"}),
    "task.blocked": (["task", "by"], {"task", "by"}),
    "task.finished": (["task", "changed", "proof"],
                      {"task", "changed", "proof", "commit", "pushed",
                       "usage"}),
    "map.updated": (["commit", "files"], {"commit", "files"}),
}
MAX_BATCH = 200
MAX_PATHS = 200


def _db():
    from . import teamserver_db
    return teamserver_db.open_team()


def _clean_path(p) -> str | None:
    """Repository-relative, no `..`, no absolute — else None."""
    if not isinstance(p, str) or not p:
        return None
    p = p.replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", p) or p.startswith("/"):
        return None
    if ".." in p.split("/"):
        return None
    return p


def _scrub(text: str) -> str:
    try:
        from ..secrets_guard import scrub_secrets
        return scrub_secrets(text)
    except Exception:                            # noqa: BLE001
        # fail-CLOSED, never open: the chain is append-only and nobody can
        # take a secret back out of it — a scrubber fault costs one entry's
        # prose, not a credential
        return "[removed — the scrubber failed]"


def _clean_files(v) -> list | None:
    """The map's file list: entries are paths, or {path,size,lang} rows — anything
    else is dropped, never kept raw.
    """
    if not isinstance(v, list):
        return None
    out = []
    for item in v[:MAX_PATHS]:
        p = _clean_path(item.get("path") if isinstance(item, dict) else item)
        if not p:
            continue
        if isinstance(item, dict):
            try:
                size = max(0, int(item.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            out.append({"path": p, "size": size,
                        "lang": str(item.get("lang") or "")[:40]})
        else:
            out.append(p)
    return out


def _clean_event(device_id: str, raw: dict) -> dict | None:
    etype = raw.get("type")
    if etype not in REPORT_TYPES:
        return None
    required, allowed = REPORT_TYPES[etype]
    data = raw.get("data") or {}
    clean = {}
    for field in required:
        if field not in data:
            return None                          # A required field missing
    for field in allowed:
        if field not in data:
            continue
        v = data[field]
        if field in ("path", "changed"):
            if isinstance(v, list):
                v = [p2 for p2 in (_clean_path(x) for x in v[:MAX_PATHS])
                     if p2]
            else:
                v = _clean_path(v)
            if v is None or (field == "changed" and not isinstance(v, list)):
                return None
        elif field == "planned_files":
            if not isinstance(v, list):
                return None
            v = [p2 for p2 in (_clean_path(x) for x in v[:200]) if p2]
        elif field == "files":                   # map.updated's file list
            v = _clean_files(v)
            if v is None:
                return None
        elif field in ("command", "text", "proof", "by", "why"):
            v = _scrub(str(v))
            if field == "command":
                v = v[:200]
                if etype == "command.changed":
                    # The command's NAME only — its arguments (paths, -e VAR=…,
                    # urls) never leave the person's machine
                    v = (v.split() or [""])[0][:80]
                    if not v:
                        return None
            elif field == "by":
                v = v[:300]
            else:
                v = v[:2000] if field in ("text", "why") else v[:500]
        elif field == "commit":
            v = str(v)[:80]
        elif field == "runner":
            v = re.sub(r"[^\w.-]", "", str(v))[:40]
        elif field in ("passed", "failed"):
            try:
                v = max(0, int(v))
            except (TypeError, ValueError):
                v = 0
        elif field == "ok":
            v = bool(v)
        clean[field] = v
    return {"id": str(raw.get("id") or "")[:80] or
            f"{device_id}-{secrets.token_hex(4)}",
            "type": etype, "project": raw.get("project"),
            "subject": _scrub(str(raw.get("subject") or ""))[:200],
            "time": raw.get("time"), "data": clean}


def _qs_project(handler) -> int | None:
    """The ?project= query id, or None (the team chain)."""
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    if not q.get("project"):
        return None
    try:
        return int(q["project"][0])
    except (TypeError, ValueError):
        return None


def log_verify(handler, match, body) -> None:
    """GET api/v1/log/verify?project= — log.verify; needs log.verify ON THAT
    PROJECT (: own project for a manager). ✓ or the first bad seq.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = _qs_project(handler)
    try:
        require(conn, actor, "manage_activity", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    chain = "team" if pid is None else f"p{pid}"
    res = logmod.verify(conn, chain)
    # The check is itself evidence: a successful verify appends one
    # log.verified row so "last checked" is a dated fact, not a green badge
    # that never decays
    if res["ok"]:
        from . import store as _store
        with _store.write(conn):
            head = conn.execute(
                "SELECT seq, hash FROM log_heads WHERE chain = ?",
                (chain,)).fetchone()
            logmod.append(conn, None if chain == "team" else int(chain[1:]),
                          *logmod.server_event(), "log.verified",
                          f"chain {chain} verified", actor["person_id"],
                          {"checked": res["checked"], "ok": True,
                           "head": (head["hash"] if head else "")[:12]})
    if not res["ok"]:
        bad = conn.execute(
            "SELECT l.seq, l.type, l.subject, l.time, p.name AS who "
            "FROM log l LEFT JOIN people p ON p.id = l.actor "
            "WHERE l.seq = ?",
            (res["first_bad_seq"],)).fetchone()
        if bad:
            res["bad"] = {"seq": bad["seq"], "type": bad["type"],
                          "subject": bad["subject"], "who": bad["who"],
                          "time": bad["time"]}
    handler._json(200, dict(res, chain=chain))


def log_head(handler, match, body) -> None:
    """GET api/v1/log/head?project= — the chain head (needs export on that
    project); the admin copies it elsewhere: that is what makes a full rewrite
    detectable.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = _qs_project(handler)
    try:
        require(conn, actor, "export_activity", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    chain = "team" if pid is None else f"p{pid}"
    row = conn.execute("SELECT seq, hash FROM log_heads WHERE chain = ?",
                       (chain,)).fetchone()
    handler._json(200, {"chain": chain, "seq": row["seq"] if row else 0,
                        "hash": row["hash"] if row else logmod.GENESIS})


_DISPATCH_LOCK = threading.Lock()


def _mark(conn, key: str) -> int:
    try:
        row = conn.execute("SELECT val FROM dispatcher_state WHERE key = ?",
                           (key,)).fetchone()
    except Exception:                                # noqa: BLE001
        return 0                                     # Pre- store
    return int(row["val"]) if row else 0


def dispatch_pending(conn, limit: int = 500) -> dict:
    """Push everything the log and chat gained since the last run: notices
    (outbox-guarded), webhooks (journaled per sub), linked-room relays
    (origin-stamped, no echo). One in-process lock serializes dispatchers;
    every delivery's own dedup (outbox / wid journal / chatlinks_seen) makes
    a re-run safe."""
    if conn is None:
        return {"skipped": True}
    if not _DISPATCH_LOCK.acquire(blocking=False):
        return {"skipped": True}
    try:
        return _dispatch(conn, limit)
    finally:
        _DISPATCH_LOCK.release()


def _dispatch(conn, limit: int) -> dict:
    from .. import team_notices as _TN
    from . import api_monitor as _AM
    from . import chatlinks as _CL
    out = {"log": 0, "msg": 0}
    since = _mark(conn, "log_seq")
    rows = conn.execute(
        "SELECT seq, project, source, ev_id, type, subject, actor, time,"
        " data FROM log WHERE seq > ? ORDER BY seq LIMIT ?",
        (since, limit)).fetchall()
    last_seq, last_msg = since, _mark(conn, "msg_id")
    for r in rows:
        last_seq = r["seq"]
        out["log"] += 1
        try:
            data = json.loads(r["data"] or "{}")
        except Exception:                            # noqa: BLE001
            data = {}
        if r["project"] is not None and _TN.notable(r["type"], data):
            pro = conn.execute("SELECT name FROM projects WHERE id = ?",
                               (r["project"],)).fetchone()
            try:
                _TN.notice_once(conn, r["project"], r["ev_id"], r["type"],
                                data, r["subject"],
                                pro["name"] if pro else "?")
            except Exception:                        # noqa: BLE001
                pass
        try:
            _AM.webhook_deliver(conn, None, {
                "id": r["ev_id"], "type": r["type"],
                "project": r["project"], "source": r["source"],
                "subject": r["subject"], "actor": r["actor"],
                "data": data, "time": r["time"], "seq": r["seq"]})
        except Exception:                            # noqa: BLE001
            pass
    try:
        n, last_msg = _CL.relay_outbound(conn, last_msg, limit)
        out["msg"] += n
    except Exception:                                # noqa: BLE001
        pass
    with store.write(conn):                          # The marks advance only
        conn.execute(                                # Once everything above
            "INSERT INTO dispatcher_state (key, val) VALUES ('log_seq', ?)"  # Is done — a crash re-does
            " ON CONFLICT(key) DO UPDATE SET val = excluded.val",
            (last_seq,))                             # The tail next time
    with store.write(conn):
        conn.execute(
            "INSERT INTO dispatcher_state (key, val) VALUES ('msg_id', ?)"
            " ON CONFLICT(key) DO UPDATE SET val = excluded.val",
            (last_msg,))
    return out


def reports_in(handler, match, body) -> None:
    """POST api/v1/reports — signed, ≤ 200 events; each lands in the log under its
    DEVICE source with its device-local id (the same report twice = one row).:
    a member OF a project may report INTO it — an event for a project the
    reporter is not in is dropped and counted (Part C.6: dropped is a LIST,
    duplicates are counted).
    """
    conn = _db()
    actor = _actor(conn, handler, body)
    if actor is None:
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    if not isinstance(req, dict):
        # hostile or wrong-shaped body ([] carries .get to AttributeError —
        # a 500 that lied about the caller; doc shape is an object)
        handler._error(400, "a report is a JSON object with an events list")
        return
    events = req.get("events") or []
    if not isinstance(events, list) or len(events) > MAX_BATCH:
        handler._error(400, "a report carries at most 200 events")
        return
    accepted, duplicates, dropped = 0, 0, []
    # Source IS the device — it also keys the (source, ev_id) dedup
    source = f"device:{actor['device_id']}"
    with store.write(conn):
        for raw in events[:MAX_BATCH]:
            ev = _clean_event(actor["device_id"], raw)
            if ev is None:
                dropped.append({"id": (raw.get("id") if isinstance(raw, dict)
                                       else None), "reason": "invalid"})
                continue
            if not can(conn, actor, "aura_work", ev.get("project")):
                dropped.append({"id": ev.get("id"),
                                "reason": "not a member of the project"})
                continue
            ts = store.now()             # The hub's clock, never the
                                         # reporter's (a skewed Aura can't write
                                         # yesterday into the chain)
            if logmod.append(conn, ev.get("project"), source, ev["id"],
                             ev["type"], ev["subject"], actor["person_id"],
                             ev["data"], ts) is not None:
                accepted += 1
                # Overlap is computed at arrival — another person's open task
                # on the same path
                if ev["type"] in ("file.changed", "task.started"):
                    try:
                        from .api_views import check_overlap
                        path = ev["data"].get("path") or \
                            ev["subject"]
                        me = conn.execute(
                            "SELECT name FROM people WHERE id = ?",
                            (actor["person_id"],)).fetchone()
                        tid = ev["data"].get("task")
                        if path and tid:
                            check_overlap(conn, ev["project"], int(tid),
                                          path,
                                          me["name"] if me else "?")
                    except Exception:                # noqa: BLE001
                        pass
                # Key moments ("started, finished, failed tests, blocked,
                # decisions, overlaps") do NOT fire from here any more — the
                # post-commit dispatcher below reads them back from the
                # committed log (which is also how api_views' overlap append
                # reaches them); the old in-transaction notice_once nested
                # BEGIN IMMEDIATE inside this write and marked failed sends as
                # sent
            else:
                duplicates += 1                     # A resend: already in
    # Post-commit push: notices / webhooks / link relays NEVER ride a client's
    # answer: a background lane sends; the answer never waits
    import threading as _th
    def _push():
        # its own connection — the handler's conn is thread-bound; sqlite
        # answers a cross-thread conn with a ProgrammingError, and the push
        # never deserves to know the client's answer anyway
        try:
            from .teamserver_db import open_team as _open
            pushed_conn = _open()
            if pushed_conn is not None:
                try:
                    dispatch_pending(pushed_conn)
                finally:
                    pushed_conn.close()
        except Exception:                            # noqa: BLE001
            pass
    _th.Thread(target=_push, name="aura-report-push", daemon=True).start()
    handler._json(200, {"stored": accepted, "duplicates": duplicates,
                        "dropped": dropped, "accepted": accepted})

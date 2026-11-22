"""teamserver/log.py — the append-only, hash-chained log (plan).

One chain per project (or `team`); each row's hash covers the previous hash
plus a canonical-JSON body, so the chain is re-computable by anyone; UNIQUE
(source, ev_id) makes a resend a no-op instead of a fork. UPDATE and DELETE are
refused by schema triggers — the log is append-only.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import store

GENESIS = "0" * 64


# What is logged — the reference list; every phase logs its own and none may be
# skipped. Chat message TEXT is never logged (chat stays in chat); only
# chat.room_created appears here.
EVENTS = [
    "team.created",
    "person.joined", "person.renamed", "person.role_changed", "person.disabled",
    "device.added", "device.revoked",
    "invite.made", "invite.revoked",
    "signin.approved", "session.revoked",
    "settings.changed",
    "project.created", "project.archived", "project.member_changed",
    "hub.backup", "hub.moved", "hub.handover",
    "task.created", "task.updated", "task.status_changed", "task.deleted",
    "comment.added", "comment.edited", "comment.deleted",
    "report.received", "report.refused",
    "overlap",
    "chat.room_created",
    "export.made",
    "task.assigned", "log.verified",
]


def chain_name(project) -> str:
    """`p<project>` per project; `team` when project is None."""
    return "team" if project is None else f"p{project}"


def canonical(project, source, ev_id, type, subject, actor, time, data) -> str:
    """The exact string the hash covers (step 2) — sort_keys, tight separators, so
    every verifier rebuilds it identically.
    """
    # Float first: an INTEGER time serializes "1700000050" but reads back from
    # the REAL column as 1700000050.0 — the chain must hash ONE form (reports
    # carry device-local integer times)
    return json.dumps(
        {"p": project, "s": source, "i": ev_id, "t": type, "sub": subject,
         "a": actor, "time": round(float(time or 0), 3), "d": data},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append(conn, project, source, ev_id, type, subject, actor, data,
           time=None) -> int | None:
    """One link in the chain, INSIDE the caller's store.write(). A resend
    (same source + ev_id) does nothing and returns None."""
    ts = store.now() if time is None else time
    chain = chain_name(project)
    row = conn.execute("SELECT hash FROM log_heads WHERE chain = ?",
                       (chain,)).fetchone()
    prev_hash = row["hash"] if row else GENESIS
    dup = conn.execute("SELECT seq FROM log WHERE source = ? AND ev_id = ?",
                       (source, ev_id)).fetchone()
    if dup:
        return None
    h = hashlib.sha256(
        (prev_hash + "\n" + canonical(project, source, ev_id, type, subject,
                                      actor, ts, data)).encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO log (project, source, ev_id, type, subject, actor, time,"
        " data, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, source, ev_id, type, subject, actor, ts,
         json.dumps(data, ensure_ascii=False, sort_keys=True), prev_hash, h))
    conn.execute(
        "INSERT INTO log_heads (chain, seq, hash) VALUES (?,?,?) "
        "ON CONFLICT(chain) DO UPDATE SET seq = excluded.seq,"
        " hash = excluded.hash",
        (chain, cur.lastrowid, h))
    if project is not None:
        # project History is invalidation-only: the recipient re-reads the
        # chain, never a subject/body copied from this event. A duplicate
        # already returned above, and after_commit prevents rollback events.
        from .api_tasks import _sse
        _sse(conn, project, "log", {"project": project, "seq": cur.lastrowid})
    return cur.lastrowid


def server_event() -> tuple[str, str]:
    """(source, ev_id) for server-made events."""
    return "server", uuid.uuid4().hex


def _rows(conn, chain: str):
    where = ("project IS NULL" if chain == "team" else "project = ?")
    args = () if chain == "team" else (int(chain[1:]),)
    return conn.execute(
        f"SELECT * FROM log WHERE {where} ORDER BY seq", args).fetchall()


def verify(conn, chain: str) -> dict:
    """Recompute the chain from its first row. ok False names the first bad seq —
    a changed row breaks the chain there.
    """
    rows = _rows(conn, chain)
    prev = GENESIS
    for r in rows:
        body = json.loads(r["data"]) if r["data"] else {}
        c = canonical(r["project"], r["source"], r["ev_id"], r["type"],
                      r["subject"], r["actor"], r["time"], body)
        h = hashlib.sha256((r["prev_hash"] + "\n" + c).encode()).hexdigest()
        if r["prev_hash"] != prev or h != r["hash"]:
            return {"ok": False, "checked": len(rows),
                    "first_bad_seq": r["seq"]}
        prev = r["hash"]
    return {"ok": True, "checked": len(rows), "first_bad_seq": None}

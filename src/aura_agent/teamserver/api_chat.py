"""teamserver/api_chat.py — chat on the new accounts (plan).

Same shapes as the old teams_chat.py, under api/v1/chat/*, but person ids
instead of names. PEOPLE ONLY: the POSTs accept a browser session, never an
Aura device signature — Aura doesn't post. The team setting `team.chat_enabled`
(default on) 404s the routes when off.

Search texts are scrubbed (keys → [REDACTED]) before they go anywhere.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time

from . import store
from .api_people import _actor
from .http import parse_json as _parse_json
from .api_people import _db  # noqa: F401  (parity with the other modules)

MAX_FILE = 200 * 1024 * 1024

# Typing pings ride SSE but never flood it: at most one event per person per
# room per window, enforced server-side
TYPING_WINDOW = 3.0
_TYPING_LAST: dict[tuple[int, int], float] = {}

# @name in a message body (md.js renders the same token as .mention)
MENTION_RX = re.compile(r"@(\w+)")

# Magic bytes decide what is an image — the client's claimed type never decides
# (: an.exe claiming image/png is an attachment)
IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    # webp: RIFF....WEBP
]
SCRUBS = [
    (re.compile(r"(?i)sk-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\W{0,3}[=:\s]\W{0,3})\S{6,}"),
     r"\1[REDACTED]"),
    (re.compile(r"(?i)ghp_[A-Za-z0-9]{20,}"), "[REDACTED]"),
]



def _num(v, default=0):
    """Hostile-shape-safe int: garbage in → default out."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _scrub(text: str) -> str:
    for rx, sub in SCRUBS:
        text = rx.sub(sub, text)
    return text


def _chat_on(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'team.chat_enabled'"
        ).fetchone()
    except Exception:                            # noqa: BLE001
        return True
    if row is None:
        return True                              # Default on
    try:
        return json.loads(row[0]) is not False
    except Exception:                            # noqa: BLE001
        return True


def _me(conn, handler):
    """The browser-session person — a device signature alone is refused here
    (people only, /). Normalized to {"id", "name"}.
    """
    me = _actor(conn, handler, b"", need="session")
    if me is None:
        return None
    pid = me.get("person_id", me.get("id"))
    return {"id": pid, "name": me.get("name", "?")}


def _rooms_of(conn, person_id: int) -> list[dict]:
    out = []
    from .perms import allowed, person_actor
    actor = person_actor(conn, person_id)
    for r in conn.execute("SELECT * FROM rooms ORDER BY id").fetchall():
        if r["kind"] == "project":
            # a member of the project AND allowed to read its chat there
            # (role, the project's Chat switch, their own switch)
            ok = conn.execute(
                "SELECT 1 FROM project_members WHERE project = ? AND"
                " person = ?", (r["project"], person_id)).fetchone()
            ok = ok and actor is not None and allowed(
                conn, actor, "view_messages", r["project"], module="chat")
        else:
            ok = conn.execute(
                "SELECT 1 FROM chat_members WHERE room = ? AND person = ?",
                (r["id"], person_id)).fetchone()
        if ok:
            out.append({"id": r["id"], "kind": r["kind"], "name": r["name"],
                        "project": r["project"]})
    return out


def _can_read(conn, room_id: int, person_id: int) -> bool:
    return any(r["id"] == room_id for r in _rooms_of(conn, person_id))


def _require_room_member(conn, handler, me, room_id):
    if not _can_read(conn, room_id, me["id"]):
        handler._error(403, "not your room")
        return None
    return conn.execute("SELECT * FROM rooms WHERE id = ?",
                        (room_id,)).fetchone()


def _room_write_ok(conn, me, room_id) -> bool:
    """The post permission applies in EVERY room kind — a project room needs the
    add_messages tick in that project (with the module on); the other kinds
    (group rooms, DMs, linked) need add_messages in at least one project the
    person belongs to.
    """
    row = conn.execute("SELECT kind, project FROM rooms WHERE id = ?",
                       (room_id,)).fetchone()
    from .perms import allowed, allowed_any
    r = conn.execute("SELECT role FROM people WHERE id = ?",
                     (me["id"],)).fetchone()
    actor = {"person_id": me["id"], "name": me["name"],
             "role": r["role"] if r else None}
    if row is not None and row["kind"] == "project":
        return allowed(conn, actor, "add_messages", row["project"],
                       module="chat")
    return allowed_any(conn, actor, "add_messages", module="chat")


def _room_people(conn, room_id: int) -> list[tuple[int, str]]:
    """(person id, name) of everyone who may read this room — project
    members for a project room, chat members for a plain room or a DM."""
    room = conn.execute("SELECT kind, project FROM rooms WHERE id = ?",
                        (room_id,)).fetchone()
    if room is None:
        return []
    if room["kind"] == "project":
        from .perms import members_who_may
        may = members_who_may(conn, room["project"], (("view_messages", "chat"),))
        rows = [r for r in conn.execute(
            "SELECT pm.person AS id, p.name AS name FROM project_members pm"
            " JOIN people p ON p.id = pm.person WHERE pm.project = ? AND"
            " p.disabled_at IS NULL", (room["project"],)).fetchall()
            if r["id"] in may]
    else:
        rows = conn.execute(
            "SELECT cm.person AS id, p.name AS name FROM chat_members cm"
            " JOIN people p ON p.id = cm.person WHERE cm.room = ? AND"
            " p.disabled_at IS NULL", (room_id,)).fetchall()
    return [(r["id"], r["name"]) for r in rows]


def _mention_targets(conn, room_id: int, text: str, me: dict):
    """@name → the room's people (schema.py is forward-only, so mention
    state is derived from msg bodies at query time against the reads
    watermark — no mentions table). Whole-token match, so @sam doesn't
    ping samantha; the author never pings themself."""
    names = {n.lower() for n in MENTION_RX.findall(text or "")}
    if not names:
        return []
    return [(pid, pname) for pid, pname in _room_people(conn, room_id)
            if pid != me["id"] and pname.lower() in names]


def _one_person(person_id: int):
    """may_see for a single person (a mention reaches only them)."""
    def may_see(pid, project=None, kind=None) -> bool:
        return pid == person_id
    return may_see


# --------------------------------------------------------------- reads


def room_may_see(conn, room_id: int):
    """may_see for the broadcaster: members of the room (or of the room's project)
    only.
    """
    room = conn.execute("SELECT kind, project FROM rooms WHERE id = ?",
                        (room_id,)).fetchone()
    if room is None:
        return None
    if room["kind"] == "project":
        # the same rule as reading the room: allowed to read chat there
        from .perms import members_who_may
        allowed = members_who_may(conn, room["project"],
                                  (("view_messages", "chat"),))
    else:
        rows = conn.execute(
            "SELECT person FROM chat_members WHERE room = ?",
            (room_id,)).fetchall()
        allowed = {r["person"] for r in rows}

    def may_see(person_id, project=None, kind=None) -> bool:
        return person_id in allowed
    return may_see


def rooms(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    out = _rooms_of(conn, me["id"])
    my = re.compile("@" + re.escape(me["name"]) + r"(?!\w)", re.I)
    for room in out:
        last = conn.execute(
            "SELECT last FROM reads WHERE room = ? AND person = ?",
            (room["id"], me["id"])).fetchone()
        rows = conn.execute(
            "SELECT body FROM msgs WHERE room = ? AND id > ? AND"
            " deleted IS NULL AND person != ?",
            (room["id"], last["last"] if last else 0, me["id"])).fetchall()
        room["unread"] = len(rows)
        # mentions are msgs-adjacent (derived per query, no schema change):
        # messages past MY read watermark that @ me by name
        room["mentions"] = sum(1 for m in rows
                               if my.search(m["body"] or ""))
        room["members"] = [name for _pid, name in
                           _room_people(conn, room["id"])]
    handler._json(200, {"rooms": out})


def history(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    try:
        room = _num((q.get("room") or [0])[0])
    except ValueError:
        room = 0
    if not _can_read(conn, room, me["id"]):
        handler._error(403, "not your room")
        return
    before = _num((q.get("before") or [0])[0] or 0)
    if before:
        rows = conn.execute(
            "SELECT * FROM msgs WHERE room = ? AND id < ? ORDER BY id DESC"
            " LIMIT 80", (room, before)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM msgs WHERE room = ? ORDER BY id DESC LIMIT 80",
            (room,)).fetchall()
    msgs = [_msg_view(conn, m) for m in rows]
    handler._json(200, {"msgs": list(reversed(msgs))})


def _msg_view(conn, m) -> dict:
    person = conn.execute("SELECT name FROM people WHERE id = ?",
                          (m["person"],)).fetchone()
    reactions = [{"emoji": x["emoji"], "n": x["n"], "by": x["by"]}
                 for x in conn.execute(
        "SELECT emoji, COUNT(*) AS n, GROUP_CONCAT(DISTINCT person) AS by"
        " FROM reactions WHERE msg = ? GROUP BY emoji", (m["id"],)).fetchall()]
    # Read-watermarks are an answer from a person, not from the poster themself.
    # The page uses this only for the author's own rows to render "seen".
    seen_by = [x["name"] for x in conn.execute(
        "SELECT p.name FROM reads r JOIN people p ON p.id = r.person"
        " WHERE r.room = ? AND r.last >= ? AND r.person != ?"
        " ORDER BY p.name", (m["room"], m["id"], m["person"])).fetchall()]
    f = None
    deleted = m["deleted"] is not None
    if m["file"] and not deleted:
        fr = conn.execute("SELECT * FROM chat_files WHERE id = ?",
                          (m["file"],)).fetchone()
        if fr:
            f = {"id": fr["id"], "name": fr["name"], "type": fr["type"],
                 "size": fr["size"]}
    # A trashed message is a tombstone: the body and the file never leave the
    # server again — the legacy "(deleted)" marker is all that a reader can see
    return {"id": m["id"], "room": m["room"],
            "person": person["name"] if person else "?",
            "person_id": m["person"],
            "body": "(deleted)" if deleted else m["body"],
            "reply_to": m["reply_to"], "file": f, "ts": m["ts"],
            "edited": m["edited"], "deleted": deleted,
            "reactions": reactions, "seen_by": seen_by}


def search(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    text = (q.get("q") or [""])[0].strip()[:200]
    hits = []
    if text:
        mine = [r["id"] for r in _rooms_of(conn, me["id"])]
        if mine:
            marks = ",".join("?" * len(mine))
            for m in conn.execute(
                    f"SELECT * FROM msgs WHERE room IN ({marks}) AND"
                    " deleted IS NULL AND body LIKE ? ORDER BY id DESC"
                    " LIMIT 50", (*mine, f"%{text}%")):
                room = conn.execute("SELECT name FROM rooms WHERE id = ?",
                                    (m["room"],)).fetchone()
                person = conn.execute(
                    "SELECT name FROM people WHERE id = ?",
                    (m["person"],)).fetchone()
                hits.append({
                    "id": m["id"], "room": m["room"],
                    "room_name": room["name"] if room else "?",
                    "person": person["name"] if person else "?",
                    "body": _scrub(m["body"])[:160], "ts": m["ts"]})
    handler._json(200, {"hits": hits})


# ---------------------------------------------------------------- posts
def _chat_payload(handler, body) -> dict | None:
    try:
        req, _je = _parse_json(body or b"{}")
        if _je is not None:
            handler._error(400, _je)
            return None
        return req if isinstance(req, dict) else None
    except Exception:                            # noqa: BLE001
        handler._error(400, "bad json")
        return None


def post(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — Aura doesn't post")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    room = req.get("room")
    text = str(req.get("body") or "").strip()[:8000]
    if not text and not req.get("file"):
        handler._error(400, "empty message")
        return
    if not _can_read(conn, _num(room or 0), me["id"]):
        handler._error(403, "not your room")
        return
    if not _room_write_ok(conn, me, _num(room or 0)):
        handler._error(403, "this project room is read-only for your role")
        return
    reply_to = req.get("reply_to") or None
    if reply_to is not None:
        ok = conn.execute("SELECT 1 FROM msgs WHERE id = ? AND room = ?",
                          (reply_to, room)).fetchone()
        if not ok:
            handler._error(400, "reply_to is not in this room")
            return
    # Only the uploader attaches a file — the id belongs to whoever uploaded
    # it, not to whoever posts the card
    if req.get("file"):
        up = conn.execute("SELECT person FROM chat_files WHERE id = ?",
                          (str(req["file"]),)).fetchone()
        if up is None or up["person"] != me["id"]:
            handler._error(403, "that file isn't yours to attach")
            return
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO msgs (room, person, body, reply_to, file, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (room, me["id"], _scrub(text), reply_to, req.get("file"),
             store.now()))
        mid = cur.lastrowid
        conn.execute(
            "INSERT INTO reads (room, person, last) VALUES (?, ?, ?)"
            " ON CONFLICT(room, person) DO UPDATE SET last = ?",
            (room, me["id"], mid, mid))
    # A message posted in room A reaches ONLY members of A — the broadcaster's
    # may_see does the filtering, live (SSE)
    from . import teamserver_db
    bc = teamserver_db.broadcaster()
    bc.publish(None, "chat",
               {"kind": "msg", "room": room, "id": mid,
                "person": me["name"], "person_id": me["id"],
                "reply_to": reply_to,
                # Live events carry the full text (the callers that want a
                # preview can shorten; receivers need the real one)
                "body": _scrub(text), "ts": store.now()},
               may_see=room_may_see(conn, room))
    # @name pings THAT person (and only them), wherever they're looking
    room_row = conn.execute("SELECT name FROM rooms WHERE id = ?",
                            (room,)).fetchone()
    for target_id, _tname in _mention_targets(conn, room, text, me):
        bc.publish(None, "mention",
                   {"room": room,
                    "room_name": room_row["name"] if room_row else "?",
                    "id": mid, "person": me["name"],
                    "body": _scrub(text)[:200], "ts": store.now()},
                   may_see=_one_person(target_id))
    handler._json(200, {"msg": {"id": mid, "room": room, "person": me["name"],
                                "body": text}})


def edit(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    mid = _num(req.get("msg") or 0)
    text = str(req.get("body") or "").strip()[:8000]
    if not text:
        handler._error(400, "empty message")
        return
    row = conn.execute("SELECT * FROM msgs WHERE id = ?", (mid,)).fetchone()
    if row is None or row["deleted"] is not None:
        handler._error(404, "no such message")
        return
    if row["person"] != me["id"]:
        handler._error(403, "only your own messages")
        return
    with store.write(conn):
        conn.execute("UPDATE msgs SET body = ?, edited = ? WHERE id = ?",
                     (_scrub(text), store.now(), mid))
    _mutated(conn, row["room"], {"kind": "edited", "room": row["room"],
                                 "id": mid, "body": _scrub(text)[:8000],
                                 "person": me["name"], "ts": store.now()})
    handler._json(200, {"ok": True})


def delete(handler, match, body) -> None:
    """Trash, not erasure."""
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    mid = _num(req.get("msg") or 0)
    row = conn.execute("SELECT * FROM msgs WHERE id = ?", (mid,)).fetchone()
    if row is None:
        handler._error(404, "no such message")
        return
    if row["person"] != me["id"]:
        handler._error(403, "only your own messages")
        return
    with store.write(conn):
        conn.execute("UPDATE msgs SET deleted = ? WHERE id = ?",
                     (store.now(), mid))
    _mutated(conn, row["room"], {"kind": "deleted", "room": row["room"],
                                 "id": mid, "person": me["name"],
                                 "ts": store.now()})
    handler._json(200, {"ok": True})


def react(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    mid = _num(req.get("msg") or 0)
    emoji = str(req.get("emoji") or "")[:16]
    row = conn.execute("SELECT room FROM msgs WHERE id = ?",
                       (mid,)).fetchone()
    if row is None or not _can_read(conn, row["room"], me["id"]):
        handler._error(403, "not your room")
        return
    if not _room_write_ok(conn, me, row["room"]):
        handler._error(403, "this project room is read-only for your role")
        return
    with store.write(conn):
        conn.execute("DELETE FROM reactions WHERE msg = ? AND person = ?"
                     " AND emoji = ?", (mid, me["id"], emoji))
        if req.get("on", True):
            conn.execute("INSERT INTO reactions (msg, person, emoji)"
                         " VALUES (?, ?, ?)", (mid, me["id"], emoji))
    # the aggregate rides the event, so a viewer never has to guess the
    # toggle's before-state — it replaces its chips with these
    reactions = [{"emoji": x["emoji"], "n": x["n"]} for x in conn.execute(
        "SELECT emoji, COUNT(*) AS n FROM reactions WHERE msg = ?"
        " GROUP BY emoji", (mid,)).fetchall()]
    _mutated(conn, row["room"], {"kind": "reacted", "room": row["room"],
                                 "id": mid, "emoji": emoji,
                                 "person": me["name"],
                                 "reactions": reactions, "ts": store.now()})
    handler._json(200, {"ok": True})


def _mutated(conn, room_id: int, data: dict) -> None:
    """Live edit/delete/react to every OTHER viewer of the room: same chat kind, a
    `kind` discriminator in the payload; may_see is the room's members so
    replay stays filtered exactly like live delivery.
    """
    def deliver():
        from . import teamserver_db
        try:
            teamserver_db.broadcaster().publish(
                None, "chat", data, may_see=room_may_see(conn, room_id))
        except Exception:                            # noqa: BLE001
            pass                       # Streaming must never break a write
    store.after_commit(conn, deliver)

def read(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    room = _num(req.get("room") or 0)
    claimed_last = _num(req.get("last") or 0)
    if not _can_read(conn, room, me["id"]):
        handler._error(403, "not your room")
        return
    with store.write(conn):
        previous = conn.execute(
            "SELECT last FROM reads WHERE room = ? AND person = ?",
            (room, me["id"])).fetchone()
        conn.execute(
            "INSERT INTO reads (room, person, last) VALUES (?, ?, ?)"
            " ON CONFLICT(room, person) DO UPDATE SET last ="
            " MAX(last, excluded.last)", (room, me["id"], claimed_last))
        stored = conn.execute(
            "SELECT last FROM reads WHERE room = ? AND person = ?",
            (room, me["id"])).fetchone()
        last = int(stored["last"])              # Server's monotonic truth

        def notify_read():
            from . import teamserver_db
            try:
                teamserver_db.broadcaster().publish(
                    None, "read",
                    {"room": room, "person": me["name"], "last": last},
                    may_see=room_may_see(conn, room))
            except Exception:                        # noqa: BLE001
                pass                   # Stream weather never breaks reading
        # No event for a backward/no-op watermark. In particular, a peer
        # cannot make this handler claim phantom new seen-by information.
        if previous is None or previous["last"] != last:
            store.after_commit(conn, notify_read)
    handler._json(200, {"ok": True, "last": last})


def typing(handler, match, body) -> None:
    """POST api/v1/chat/typing — publish a `typing` event to the room's members
    over the ONE stream. Throttled server-side to ~3 s per person per room so a
    held key never floods the stream.
    """
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    room = _num(req.get("room") or 0)
    if not _can_read(conn, room, me["id"]):
        handler._error(403, "not your room")
        return
    now = time.time()
    key = (me["id"], room)
    if now - _TYPING_LAST.get(key, 0.0) >= TYPING_WINDOW:
        _TYPING_LAST[key] = now
        from . import teamserver_db
        try:
            teamserver_db.broadcaster().publish(
                None, "typing", {"room": room, "person": me["name"]},
                may_see=room_may_see(conn, room))
        except Exception:                        # noqa: BLE001
            pass                   # A typing ping must never break the page
    handler._json(200, {"ok": True})


def make_room(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    name = str(req.get("name") or "").strip()[:80]
    if not name:
        handler._error(400, "a room needs a name")
        return
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO rooms (kind, name, project, made_by, ts)"
            " VALUES ('room', ?, NULL, ?, ?)", (name, me["id"], store.now()))
        rid = cur.lastrowid
        conn.execute("INSERT INTO chat_members (room, person) VALUES (?, ?)",
                     (rid, me["id"]))
        for who in (req.get("invite") or [])[:20]:
            p = conn.execute("SELECT id FROM people WHERE name = ?",
                             (str(who),)).fetchone()
            if p:
                conn.execute("INSERT OR IGNORE INTO chat_members (room,"
                             " person) VALUES (?, ?)", (rid, p["id"]))
    handler._json(200, {"room": {"id": rid, "kind": "room", "name": name}})


def make_dm(handler, match, body) -> None:
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    req = _chat_payload(handler, body)
    if req is None:
        return
    who = str(req.get("with") or "")
    other = conn.execute("SELECT * FROM people WHERE name = ? AND"
                         " disabled_at IS NULL", (who,)).fetchone()
    if other is None:
        handler._error(404, "no such person")
        return
    if other["id"] == me["id"]:
        handler._error(400, "a DM is between two people")
        return
    # DMs only between the two people: find or make the pair room
    row = conn.execute(
        "SELECT r.id FROM rooms r JOIN chat_members a ON a.room = r.id"
        " JOIN chat_members b ON b.room = r.id WHERE r.kind = 'dm' AND"
        " a.person = ? AND b.person = ? AND r.kind = 'dm'",
        (me["id"], other["id"])).fetchone()
    if row:
        handler._json(200, {"room": {"id": row["id"], "kind": "dm",
                                     "name": who}})
        return
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO rooms (kind, name, project, made_by, ts)"
            " VALUES ('dm', ?, NULL, ?, ?)",
            (f"DM {me['name']}–{who}", me["id"], store.now()))
        rid = cur.lastrowid
        conn.execute("INSERT INTO chat_members (room, person) VALUES (?, ?)",
                     (rid, me["id"]))
        conn.execute("INSERT INTO chat_members (room, person) VALUES (?, ?)",
                     (rid, other["id"]))
    handler._json(200, {"room": {"id": rid, "kind": "dm", "name": who}})


def _sniff(head: bytes) -> str | None:
    for magic, typ in IMAGE_MAGIC:
        if head.startswith(magic):
            return typ
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def file_dl(handler, match, body) -> None:
    """GET api/v1/chat/file/<id> — an uploaded file. Images render inline;
    everything else is `Content-Disposition: attachment` + nosniff.
    """
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    fid = match.group(1)
    row = conn.execute("SELECT * FROM chat_files WHERE id = ?",
                       (fid,)).fetchone()
    if row is None:
        handler._error(404, "no such file")
        return
    # A file belongs to the room it was posted in: members of that room only —
    # everyone else doesn't even learn the id exists (404): a file referenced
    # only by a DELETED message is refused — same 404 bleed as if it never
    # existed (a re-post re-opens it; a wipe stays gone)
    ref = conn.execute("SELECT room FROM msgs WHERE file = ? AND"
                       " deleted IS NULL ORDER BY id LIMIT 1",
                       (fid,)).fetchone()
    if ref is None:
        if row["person"] != me["id"]:            # Never posted: uploader only
            handler._error(404, "no such file")
            return
    elif not _can_read(conn, ref["room"], me["id"]):
        handler._error(404, "no such file")
        return
    from .teamserver_db import team_dir
    f = team_dir() / "files" / fid
    if not f.exists():
        handler._error(404, "the file is gone")
        return
    data = f.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", row["type"] or
                        "application/octet-stream")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    if not (row["type"] or "").startswith("image/"):
        handler.send_header("Content-Disposition",
                            'attachment; filename="' + str(row["name"]) + '"')
    handler.send_header("Aura-Protocol", "1")
    handler.end_headers()
    handler.wfile.write(data)


def presence(handler, match, body) -> None:
    """GET api/v1/presence — who's online (an open stream ≤ 60 s), away (5 min
    idle), offline — from the broadcaster's connections and people.last_seen.
    """
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    from . import teamserver_db
    now = store.now()
    out = []
    for row in conn.execute(
            "SELECT p.id AS id, p.name AS name, p.role AS role,"
            " MAX(d.last_seen) AS last_seen FROM people p"
            " LEFT JOIN devices d ON d.person = p.id"
            " WHERE p.disabled_at IS NULL"
            " AND p.role != 'linked-app'"         # 's bridge is never a person
            " AND p.browser_only != 1"
            " GROUP BY p.id").fetchall():
        online = teamserver_db.is_online(row["id"])
        away = not online and row["last_seen"] and \
            now - row["last_seen"] <= 300
        out.append({"id": row["id"], "name": row["name"], "role": row["role"],
                    "state": "online" if online else
                             "away" if away else "offline"})
    handler._json(200, {"people": out})


def upload(handler, match, body) -> None:
    """."""
    conn = _db()
    if not _chat_on(conn):
        handler._error(404, "chat is off on this team")
        return
    me = _me(conn, handler)
    if me is None:
        handler._error(403, "people only — sign in")
        return
    ctype = handler.headers.get("Content-Type", "")
    if not ctype.startswith("application/octet-stream"):
        handler._error(415, "send the file as application/octet-stream")
        return
    length = len(body) if body else int(
        handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_FILE:
        handler._error(413, "The file is over 200 MB." if length > 0
                       else "the file is empty")
        return
    name = str(handler.headers.get("X-File-Name") or "file")[:120]
    name = re.sub(r"[^\w .()-]", "_", name)
    fid = secrets.token_hex(12)
    from .teamserver_db import team_dir
    files_dir = team_dir() / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    part = files_dir / (fid + ".part")
    # Written in 1 MB pieces to a.part file, never whole in memory; moved into
    # place only when every byte arrived
    head = b""
    got = 0
    try:
        with open(part, "wb") as out:
            if body:
                out.write(body)
                head, got = body[:16], len(body)
            else:
                while got < length:
                    chunk = handler.rfile.read(min(1 << 20, length - got))
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:16]
                    out.write(chunk)
                    got += len(chunk)
                    handler._stream_left = length - got
        if got != length:
            raise OSError("the upload stopped before the end")
        os.replace(part, files_dir / fid)
    except OSError:
        try:
            part.unlink()
        except OSError:
            pass
        handler.close_connection = True
        handler._error(400, "The upload stopped before the end — try again.")
        return
    image_type = _sniff(head)
    if image_type is None and name.lower().endswith(".svg"):
        image_type = None                      # SVG never renders inline
    with store.write(conn):
        conn.execute(
            "INSERT INTO chat_files (id, name, type, size, person, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fid, name, image_type or "application/octet-stream",
             got, me["id"], store.now()))
    handler._json(200, {"file": {"id": fid, "name": name,
                                 "type": image_type or
                                 "application/octet-stream",
                                 "size": got}})

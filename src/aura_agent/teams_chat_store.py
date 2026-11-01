"""teams_chat_store.py — the people chat's data.

One SQLite file in Aura's folder on the machine that runs the chat. A person is
their name on the hub. Rooms: one per hub project, and rooms people make; a DM
is a room of two. Typing is not stored (it's an event only).
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from .paths import aura_home

MAX_BODY = 4000
MAX_FILE = 10 * 1024 * 1024
EMOJI = ("👍", "❤️", "😂", "🎉", "👀", "✅")
MENTION = re.compile(r"(?<![\w@])@([\w.-]{1,64})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, last_seen REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rooms (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    name TEXT NOT NULL, project TEXT DEFAULT '', made_by TEXT DEFAULT '', ts REAL);
CREATE TABLE IF NOT EXISTS members (room INTEGER, user TEXT, PRIMARY KEY (room, user));
CREATE TABLE IF NOT EXISTS msgs (id INTEGER PRIMARY KEY AUTOINCREMENT, room INTEGER NOT NULL,
    user TEXT NOT NULL, body TEXT NOT NULL, reply_to INTEGER, file TEXT, ts REAL,
    edited REAL, deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS msgs_room ON msgs (room, id);
CREATE TABLE IF NOT EXISTS reactions (msg INTEGER, user TEXT, emoji TEXT,
    PRIMARY KEY (msg, user, emoji));
CREATE TABLE IF NOT EXISTS reads (room INTEGER, user TEXT, last INTEGER DEFAULT 0,
    PRIMARY KEY (room, user));
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, name TEXT, type TEXT, size INTEGER,
    user TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, room INTEGER,
    kind TEXT, data TEXT, ts REAL);
"""


def home() -> Path:
    d = aura_home() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d


def files_dir() -> Path:
    d = home() / "chat-files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(home() / "chat.sqlite3", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_v1(conn)
    return conn


def _migrate_v1(conn) -> None:
    """The first chat page kept one room in `messages(username, body, ts)`:
    its messages become the 'general' room's (once)."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "messages" not in have:
        return
    rid = general(conn)
    for r in conn.execute("SELECT username, body, ts FROM messages ORDER BY id").fetchall():
        conn.execute("INSERT INTO msgs(room,user,body,ts) VALUES(?,?,?,?)",
                     (rid, r["username"], r["body"], time.time()))
    conn.execute("ALTER TABLE messages RENAME TO messages_v1_moved")
    conn.commit()


# -------------------------------------------------------------- rooms ---
def general(conn) -> int:
    r = conn.execute("SELECT id FROM rooms WHERE kind='room' AND name='general'").fetchone()
    if r:
        return r["id"]
    cur = conn.execute("INSERT INTO rooms(kind,name,ts) VALUES('room','general',?)", (time.time(),))
    conn.commit()
    return cur.lastrowid


def ensure_project_rooms(conn, projects: list[str]) -> None:
    """One room per hub project."""
    have = {r["project"] for r in conn.execute("SELECT project FROM rooms WHERE kind='room'")}
    for p in projects:
        if p and p not in have:
            conn.execute("INSERT INTO rooms(kind,name,project,ts) VALUES('room',?,?,?)",
                         (p, p, time.time()))
    conn.commit()


def make_room(conn, name: str, by: str) -> int:
    name = re.sub(r"[^\w .-]+", "", name).strip()[:40]
    if not name:
        raise ValueError("a room needs a name")
    r = conn.execute("SELECT id FROM rooms WHERE kind='room' AND lower(name)=lower(?)", (name,)).fetchone()
    if r:
        return r["id"]
    cur = conn.execute("INSERT INTO rooms(kind,name,made_by,ts) VALUES('room',?,?,?)",
                       (name, by, time.time()))
    conn.commit()
    event(conn, cur.lastrowid, "room", {"id": cur.lastrowid, "name": name, "by": by})
    return cur.lastrowid


def dm(conn, a: str, b: str) -> int:
    """The DM room of two people (made once)."""
    if a == b:
        raise ValueError("a DM is with someone else")
    pair = sorted([a, b])
    r = conn.execute("""SELECT r.id FROM rooms r WHERE r.kind='dm' AND
        (SELECT count(*) FROM members m WHERE m.room=r.id AND m.user IN (?,?))=2""", pair).fetchone()
    if r:
        return r["id"]
    cur = conn.execute("INSERT INTO rooms(kind,name,ts) VALUES('dm',?,?)",
                       (" & ".join(pair), time.time()))
    for u in pair:
        conn.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (cur.lastrowid, u))
    conn.commit()
    return cur.lastrowid


def can_see(conn, room: int, user: str) -> bool:
    r = conn.execute("SELECT kind FROM rooms WHERE id=?", (room,)).fetchone()
    if not r:
        return False
    if r["kind"] == "room":
        return True                                  # Team rooms: everyone on the team
    return bool(conn.execute("SELECT 1 FROM members WHERE room=? AND user=?", (room, user)).fetchone())


def rooms_for(conn, user: str) -> list[dict]:
    """Every room this person sees, with unread and mention counts."""
    out = []
    for r in conn.execute("SELECT * FROM rooms ORDER BY kind DESC, name").fetchall():
        if not can_see(conn, r["id"], user):
            continue
        last = (conn.execute("SELECT last FROM reads WHERE room=? AND user=?",
                             (r["id"], user)).fetchone() or {"last": 0})["last"]
        rows = conn.execute("SELECT body FROM msgs WHERE room=? AND id>? AND user<>? AND deleted=0",
                            (r["id"], last, user)).fetchall()
        mentions = sum(1 for m in rows if user.lower() in {x.lower() for x in MENTION.findall(m["body"])})
        name = r["name"]
        if r["kind"] == "dm":
            other = [m["user"] for m in conn.execute("SELECT user FROM members WHERE room=?", (r["id"],))
                     if m["user"] != user]
            name = other[0] if other else name
        out.append({"id": r["id"], "kind": r["kind"], "name": name, "project": r["project"],
                    "unread": len(rows) + (0 if r["kind"] == "room" else 0), "mentions": mentions})
    return out


# ----------------------------------------------------------- messages ---
def _row(conn, m) -> dict:
    d = dict(m)
    d["reactions"] = {}
    for r in conn.execute("SELECT emoji, user FROM reactions WHERE msg=?", (m["id"],)):
        d["reactions"].setdefault(r["emoji"], []).append(r["user"])
    if d.get("deleted"):
        d["body"], d["file"] = "", None
    if d.get("file"):
        f = conn.execute("SELECT id,name,type,size FROM files WHERE id=?", (d["file"],)).fetchone()
        d["file"] = dict(f) if f else None
    if d.get("reply_to"):
        p = conn.execute("SELECT id,user,body,deleted FROM msgs WHERE id=?", (d["reply_to"],)).fetchone()
        d["reply"] = ({"id": p["id"], "user": p["user"], "body": "" if p["deleted"] else p["body"][:140]}
                      if p else None)
    return d


def post(conn, room: int, user: str, body: str, reply_to: int | None = None,
         file: str | None = None) -> dict:
    body = (body or "").strip()[:MAX_BODY]
    if not body and not file:
        raise ValueError("nothing to send")
    conn.execute("INSERT OR IGNORE INTO users(name,last_seen) VALUES(?,0)", (user,))
    cur = conn.execute("INSERT INTO msgs(room,user,body,reply_to,file,ts) VALUES(?,?,?,?,?,?)",
                       (room, user, body, reply_to, file, time.time()))
    conn.execute("INSERT INTO reads(room,user,last) VALUES(?,?,?) "
                 "ON CONFLICT(room,user) DO UPDATE SET last=excluded.last",
                 (room, user, cur.lastrowid))          # Your own message is read
    conn.commit()
    m = _row(conn, conn.execute("SELECT * FROM msgs WHERE id=?", (cur.lastrowid,)).fetchone())
    event(conn, room, "msg", m)
    return m


def _own(conn, msg: int, user: str):
    m = conn.execute("SELECT * FROM msgs WHERE id=?", (msg,)).fetchone()
    if not m or m["user"] != user:
        raise PermissionError("only your own messages")
    return m


def edit(conn, msg: int, user: str, body: str) -> dict:
    m = _own(conn, msg, user)
    body = (body or "").strip()[:MAX_BODY]
    if not body:
        raise ValueError("nothing left — delete it instead")
    conn.execute("UPDATE msgs SET body=?, edited=? WHERE id=?", (body, time.time(), msg))
    conn.commit()
    d = _row(conn, conn.execute("SELECT * FROM msgs WHERE id=?", (msg,)).fetchone())
    event(conn, m["room"], "edit", d)
    return d


def delete(conn, msg: int, user: str) -> None:
    m = _own(conn, msg, user)
    conn.execute("UPDATE msgs SET deleted=1, edited=? WHERE id=?", (time.time(), msg))
    conn.commit()
    event(conn, m["room"], "delete", {"id": msg})


def react(conn, msg: int, user: str, emoji: str) -> dict:
    if emoji not in EMOJI:
        raise ValueError("not one of the reactions")
    m = conn.execute("SELECT room FROM msgs WHERE id=?", (msg,)).fetchone()
    if not m:
        raise ValueError("no such message")
    if conn.execute("SELECT 1 FROM reactions WHERE msg=? AND user=? AND emoji=?", (msg, user, emoji)).fetchone():
        conn.execute("DELETE FROM reactions WHERE msg=? AND user=? AND emoji=?", (msg, user, emoji))
    else:
        conn.execute("INSERT INTO reactions VALUES(?,?,?)", (msg, user, emoji))
    conn.commit()
    d = _row(conn, conn.execute("SELECT * FROM msgs WHERE id=?", (msg,)).fetchone())
    event(conn, m["room"], "react", {"id": msg, "reactions": d["reactions"]})
    return d


def history(conn, room: int, before: int | None = None, limit: int = 60) -> list[dict]:
    q = "SELECT * FROM msgs WHERE room=?" + (" AND id<?" if before else "") + " ORDER BY id DESC LIMIT ?"
    args = (room, before, limit) if before else (room, limit)
    return [_row(conn, m) for m in reversed(conn.execute(q, args).fetchall())]


def mark_read(conn, room: int, user: str, last: int) -> None:
    conn.execute("INSERT INTO reads(room,user,last) VALUES(?,?,?) ON CONFLICT(room,user) "
                 "DO UPDATE SET last=max(last, excluded.last)", (room, user, last))
    conn.commit()
    event(conn, room, "seen", {"user": user, "last": last})


def seen_by(conn, room: int) -> dict:
    return {r["user"]: r["last"] for r in conn.execute("SELECT user,last FROM reads WHERE room=?", (room,))}


def search(conn, user: str, q: str, limit: int = 50) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    like = "%" + q.replace("%", "").replace("_", "") + "%"
    rows = conn.execute("SELECT * FROM msgs WHERE deleted=0 AND body LIKE ? ORDER BY id DESC LIMIT 300",
                        (like,)).fetchall()
    return [_row(conn, m) for m in rows if can_see(conn, m["room"], user)][:limit]


# -------------------------------------------------------------- files ---
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def image_type(data: bytes) -> str | None:
    """The image type its own bytes prove (never the name or what the browser said)."""
    for sig, t in _MAGIC:
        if data.startswith(sig):
            return t
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_file(conn, user: str, name: str, data: bytes) -> dict:
    if len(data) > MAX_FILE:
        raise ValueError(f"files up to {MAX_FILE // (1024 * 1024)} MB")
    fid = secrets.token_hex(12)                      # A random name, never the uploaded one
    (files_dir() / fid).write_bytes(data)
    t = image_type(data) or "application/octet-stream"
    clean = re.sub(r"[^\w .()-]+", "_", Path(name or "file").name)[:120] or "file"
    conn.execute("INSERT INTO files VALUES(?,?,?,?,?,?)", (fid, clean, t, len(data), user, time.time()))
    conn.commit()
    return {"id": fid, "name": clean, "type": t, "size": len(data)}


def get_file(conn, fid: str):
    if not re.fullmatch(r"[0-9a-f]{24}", fid or ""):
        return None, None
    f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    p = files_dir() / fid
    return (dict(f), p.read_bytes()) if f and p.exists() else (None, None)


# ------------------------------------------------- people and presence ---
def seen_now(conn, user: str) -> None:
    conn.execute("INSERT INTO users(name,last_seen) VALUES(?,?) ON CONFLICT(name) "
                 "DO UPDATE SET last_seen=excluded.last_seen", (user, time.time()))
    conn.commit()


def people(conn, team: list[str], online: set[str]) -> list[dict]:
    names = sorted(set(team) | {r["name"] for r in conn.execute("SELECT name FROM users")}, key=str.lower)
    seen = {r["name"]: r["last_seen"] for r in conn.execute("SELECT name,last_seen FROM users")}
    now = time.time()
    return [{"name": n, "online": n in online or now - (seen.get(n) or 0) < 90,
             "last_seen": seen.get(n) or 0} for n in names]


# -------------------------------------------------------------- events ---
def event(conn, room: int | None, kind: str, data: dict) -> int:
    cur = conn.execute("INSERT INTO events(room,kind,data,ts) VALUES(?,?,?,?)",
                       (room, kind, json.dumps(data), time.time()))
    conn.commit()
    return cur.lastrowid


def events_after(conn, after: int, user: str, limit: int = 200) -> list[dict]:
    out = []
    for e in conn.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)):
        if e["room"] is not None and not can_see(conn, e["room"], user):
            continue
        out.append({"id": e["id"], "room": e["room"], "kind": e["kind"], "data": json.loads(e["data"])})
    return out


def last_event(conn) -> int:
    r = conn.execute("SELECT max(id) AS m FROM events").fetchone()
    return int(r["m"] or 0)

"""teamserver/migrate_chat.py — the old teams_chat store moves in.

If an old chat database exists, its rooms/messages/reactions/reads are copied
into the team server's V2 tables, mapping names to people; a name that never
joined becomes a DISABLED person, so history keeps its author but nobody can
sign in as them. Runs once (guarded by a settings row).
"""
from __future__ import annotations

import json
import time


def migrate_chat(conn, old_db_path) -> dict:
    """Copy the old chat in. Idempotent: a second run is a no-op."""
    done = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'chat.migrated'").fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name ="
                        " 'settings'").fetchone() else None
    if done:
        return {"copied": 0, "already": True}
    import sqlite3
    old = sqlite3.connect(old_db_path)
    old.row_factory = sqlite3.Row
    counts = {"rooms": 0, "msgs": 0, "reactions": 0, "reads": 0}
    from . import store

    def person_id(name: str, disabled_ok=True) -> int:
        row = conn.execute("SELECT id FROM people WHERE name = ?",
                           (name,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO people (name, role, created, disabled_at)"
            " VALUES (?, 'member', ?, ?)",
            (name, store.now(), store.now() if disabled_ok else None))
        return cur.lastrowid

    with store.write(conn):
        for r in old.execute("SELECT * FROM rooms").fetchall():
            proj = None
            if r["project"]:
                prow = conn.execute(
                    "SELECT id FROM projects WHERE name = ? OR slug = ?",
                    (r["project"], r["project"])).fetchone()
                proj = prow["id"] if prow else None
            made_by = person_id(r["made_by"]) if r["made_by"] else None
            cur = conn.execute(
                "INSERT INTO rooms (kind, name, project, made_by, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (r["kind"] if r["kind"] in ("project", "room", "dm")
                 else "room", r["name"], proj, made_by,
                 r["ts"] or store.now()))
            old_room, new_room = r["id"], cur.lastrowid
            counts["rooms"] += 1
            for m in old.execute(
                    "SELECT DISTINCT user FROM members WHERE room = ?",
                    (old_room,)).fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO chat_members (room, person)"
                    " VALUES (?, ?)", (new_room, person_id(m["user"])))
            for m in old.execute(
                    "SELECT * FROM msgs WHERE room = ? ORDER BY id",
                    (old_room,)).fetchall():
                mcur = conn.execute(
                    "INSERT INTO msgs (room, person, body, reply_to, file,"
                    " ts, edited, deleted) VALUES (?, ?, ?, NULL, ?, ?,"
                    " ?, ?)",
                    (new_room, person_id(m["user"]), m["body"], m["file"],
                     m["ts"], m["edited"],
                     store.now() if m["deleted"] else None))
                mapping = counts.setdefault("msg_ids", {})
                mapping[m["id"]] = mcur.lastrowid
                counts["msgs"] += 1
        # replies after all msgs exist (old ids -> new ids)
        mapping = counts.get("msg_ids", {})
        for old_id, new_id in mapping.items():
            m = old.execute("SELECT reply_to FROM msgs WHERE id = ?",
                            (old_id,)).fetchone()
            if m and m["reply_to"] and m["reply_to"] in mapping:
                conn.execute("UPDATE msgs SET reply_to = ? WHERE id = ?",
                             (mapping[m["reply_to"]], new_id))
        for x in old.execute("SELECT * FROM reactions").fetchall():
            if x["msg"] in mapping:
                conn.execute(
                    "INSERT OR IGNORE INTO reactions (msg, person, emoji)"
                    " VALUES (?, ?, ?)",
                    (mapping[x["msg"]], person_id(x["user"]), x["emoji"]))
                counts["reactions"] += 1
        for x in old.execute("SELECT * FROM reads").fetchall():
            room_row = old.execute("SELECT name FROM rooms WHERE id = ?",
                                   (x["room"],)).fetchone()
            if room_row:
                nrow = conn.execute(
                    "SELECT id FROM rooms WHERE name = ?",
                    (room_row["name"],)).fetchone()
                if nrow:
                    conn.execute(
                        "INSERT OR IGNORE INTO reads (room, person, last)"
                        " VALUES (?, ?, ?)",
                        (nrow["id"], person_id(x["user"]), x["last"]))
                    counts["reads"] += 1
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('chat.migrated', ?)",
            (json.dumps({"at": store.now(), "counts": counts}),))
    old.close()
    return {k: v for k, v in counts.items() if k != "msg_ids"}

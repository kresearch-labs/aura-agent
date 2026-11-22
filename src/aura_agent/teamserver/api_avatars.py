"""Avatars — each person's picture on the team page.

By default a person's avatar is drawn on the page from their username, in
one of two styles (Lorelei, the default, or Avataaars). A person may pick
the style, shuffle to another drawing of it (a short seed), or upload a
photo that replaces the drawing everywhere. The hub stores only the choice
and the photo; the drawing itself is made in the browser, so no name ever
leaves the hub.

Routes (all for signed-in people):
  GET    /api/v1/avatars                  everyone's choice, by name
  PATCH  /api/v1/people/me/avatar         {style?, seed?} — your own
  PUT    /api/v1/people/me/avatar/photo   raw image bytes (≤ 2 MB)
  DELETE /api/v1/people/me/avatar/photo   back to the drawn avatar
  GET    /api/v1/people/<id>/avatar       that person's photo
"""
from __future__ import annotations

import re
import time

from . import store
from .api_chat import _sniff
from .api_people import _actor, _db, _parse_json

STYLES = ("lorelei", "avataaars")
SEED_OK = re.compile(r"[a-z0-9-]{1,32}")
MAX_PHOTO = 2 * 1024 * 1024
PHOTO_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _photo_dir():
    from .teamserver_db import team_dir
    d = team_dir() / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _image_type(data: bytes) -> str | None:
    """Magic bytes decide, never the client's claim (as chat uploads)."""
    t = _sniff(data[:16])
    if t is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        t = "image/webp"
    return t if t in PHOTO_TYPES else None


def _announce(conn, person_id: int) -> None:
    """Pages refresh the avatars they show (the live "people" event)."""
    try:
        from . import teamserver_db
        row = conn.execute("SELECT name FROM people WHERE id = ?",
                           (person_id,)).fetchone()
        teamserver_db.broadcaster().publish(
            None, "people", {"person_id": person_id,
                             "name": row["name"] if row else "?",
                             "avatar": True})
    except Exception:                            # noqa: BLE001
        pass                     # A missed refresh never breaks the change


def avatars_list(handler, match, body) -> None:
    """GET /api/v1/avatars — {avatars: {name: {style, seed, photo}}};
    photo is 0 for none, else a version number for the picture's URL."""
    conn = _db()
    if _actor(conn, handler, body, need="any") is None:
        return
    out = {}
    for r in conn.execute(
            "SELECT id, name, avatar_style, avatar_seed, avatar_photo,"
            " avatar_ver FROM people").fetchall():
        out[r["name"]] = {
            "id": r["id"],
            "style": r["avatar_style"] if r["avatar_style"] in STYLES else "lorelei",
            "seed": r["avatar_seed"] or "",
            "photo": int(r["avatar_ver"] or 0) if r["avatar_photo"] else 0,
        }
    handler._json(200, {"avatars": out})


def avatar_me(handler, match, body) -> None:
    """PATCH /api/v1/people/me/avatar — {style?: lorelei|avataaars,
    seed?: a-z 0-9 - (≤ 32), or "" for the username's own drawing}."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    changes, err = _parse_json(body or b"{}")
    if err is not None or not isinstance(changes, dict):
        handler._error(400, err or "the body isn't an avatar choice")
        return
    sets, args = [], []
    if "style" in changes:
        if changes["style"] not in STYLES:
            handler._error(400, "the style is lorelei or avataaars")
            return
        sets.append("avatar_style = ?"); args.append(changes["style"])
    if "seed" in changes:
        seed = str(changes["seed"] or "")
        if seed and not SEED_OK.fullmatch(seed):
            handler._error(400, "the seed is up to 32 lowercase letters, digits or dashes")
            return
        sets.append("avatar_seed = ?"); args.append(seed)
    if not sets:
        handler._error(400, "nothing to change: send style or seed")
        return
    pid = actor["person_id"]
    with store.write(conn):
        conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?",
                     (*args, pid))
    _announce(conn, pid)
    handler._json(200, {"ok": True})


def avatar_photo_put(handler, match, body) -> None:
    """PUT /api/v1/people/me/avatar/photo — the picture's raw bytes
    (PNG, JPEG or WebP, ≤ 2 MB); it replaces the drawing everywhere."""
    conn = _db()
    actor = _actor(conn, handler, b"", need="session")
    if actor is None:
        return
    data = body or b""
    if not data:
        handler._error(400, "the picture is empty")
        return
    if len(data) > MAX_PHOTO:
        handler._error(413, "The picture is over 2 MB.")
        return
    typ = _image_type(data)
    if typ is None:
        handler._error(415, "use a PNG, JPEG or WebP picture")
        return
    pid = actor.get("person_id", actor.get("id"))
    (_photo_dir() / str(int(pid))).write_bytes(data)
    with store.write(conn):
        conn.execute("UPDATE people SET avatar_photo = ?, avatar_ver = ?"
                     " WHERE id = ?", (typ, time.time(), pid))
    _announce(conn, pid)
    handler._json(200, {"ok": True})


def avatar_photo_delete(handler, match, body) -> None:
    """DELETE /api/v1/people/me/avatar/photo — back to the drawn avatar."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = actor["person_id"]
    f = _photo_dir() / str(int(pid))
    if f.exists():
        f.unlink()
    with store.write(conn):
        conn.execute("UPDATE people SET avatar_photo = '', avatar_ver = ?"
                     " WHERE id = ?", (time.time(), pid))
    _announce(conn, pid)
    handler._json(200, {"ok": True})


def avatar_photo_get(handler, match, body) -> None:
    """GET /api/v1/people/<id>/avatar — that person's photo (signed-in
    people only; 404 when they have none)."""
    conn = _db()
    if _actor(conn, handler, body, need="any") is None:
        return
    pid = int(match.group(1))
    row = conn.execute("SELECT avatar_photo FROM people WHERE id = ?",
                       (pid,)).fetchone()
    f = _photo_dir() / str(pid)
    if row is None or not row["avatar_photo"] or not f.exists():
        handler._error(404, "no photo")
        return
    data = f.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", row["avatar_photo"])
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.send_header("Aura-Protocol", "1")
    handler.end_headers()
    handler.wfile.write(data)

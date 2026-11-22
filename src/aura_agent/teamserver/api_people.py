"""teamserver/api_people.py — people, devices, the account sign-in. The first
build's inviting ways in (codes/invites) are gone — the admin makes accounts.

Signed-request routes verify through auth.verify; session routes need a
same-origin Origin header on every state change (step 5). Every action writes
its log entry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets

from . import auth
from . import log as logmod
from . import store
from .http import parse_json as _parse_json

NAME_OK = re.compile(r"[a-z0-9][a-z0-9-]{0,38}")


def _db():
    from . import teamserver_db
    return teamserver_db.open_team()


def _is_owner(person: dict) -> bool:
    return person.get("team_role") == "owner" or person.get("role") == "owner"


def _origin_ok(handler) -> bool:
    """A cookie-authenticated POST/PATCH/DELETE needs the server's own Origin
    (step 5).
    """
    origin = handler.headers.get("Origin") or ""
    host = handler.headers.get("Host") or ""
    if not origin:
        return False
    return hmac.compare_digest(origin.split("://", 1)[-1].lower(),
                               host.lower())


def _actor(conn, handler, body: bytes, need: str = "signed") -> dict | None:
    """Need: signed | session | any. Returns None after sending the error. A
    request with Aura-* headers is verified signed; one with the session cookie
    goes through the session path.

    's one-resolve: when the dispatcher already proved sign-in for THIS request
    (same method+path+body), the cached actor is the answer — re-verifying a
    request twice reads as a replay on the nonce store. The route's declared
    surface still holds for the cached answer.
    """
    import hashlib as _hl
    tagged = getattr(handler, "_actor_for_request", None)
    body_hash = _hl.sha256(body).hexdigest()
    if tagged and tagged[:3] == (handler.command, handler.path, body_hash):
        cached = tagged[3]
        if cached is None:
            handler._error(401, "sign in first (join the team, or sign in on "
                                "the page)")
            return None            # The gate already answered; do not repeat
        via_device = bool(cached.get("_via_device"))
        if need == "session" and via_device:
            handler._error(401, "this route takes a browser session, not a "
                                "device signature")
            return None
        if need == "signed" and not via_device:
            handler._error(401, "sign in first (this route needs your Aura's "
                                "signed request)")
            return None
        if (handler.command in ("POST", "PUT", "PATCH", "DELETE")
                and not via_device and not _origin_ok(handler)):
            handler._error(403, "this change must come from the team's own "
                                "page (same-origin)")
            return None
        return cached
    if handler.headers.get("Aura-Device"):
        if need == "session":
            handler._error(401, "this route takes a browser session, not a "
                                "device signature")
            return None
        person = auth.verify(conn, handler, body)
        if person is not None:
            person["_via_device"] = True       # For the cached-actor rule above
        return person
    if need == "signed":
        handler._error(401, "sign in first (this route needs your Aura's "
                            "signed request)")
        return None
    from . import sessions
    token = _cookie(handler, sessions.COOKIE)
    person = auth.session_person(conn, token)
    if person is None:
        handler._error(401, "sign in first (join the team, or sign in on "
                            "the page)")
        return None
    if person.get("must_change"):
        # With the must-change flag, only the password page works; every other
        # path answers once with the same refusal
        allowed = {("POST", "/api/v1/me/password"),
                   ("POST", "/api/v1/signout"),
                   ("GET", "/api/v1/me"),
                   ("GET", "/api/v1/health"),
                   ("GET", "/api/v1/info")}
        if (handler.command, handler.path.split("?", 1)[0]) not in allowed:
            handler._error(403, "change your password first — that's the only "
                                "thing this sign-in may do")
            return None
    if handler.command in ("POST", "PATCH", "DELETE"):
        # Every cookie-fed state change (not only need=="session") — the
        # dispatcher resolves with need="any", so the old tag let "any"
        # sessions skip same-origin entirely
        if not _origin_ok(handler):
            handler._error(403, "this change must come from the team's own "
                                "page (same-origin)")
            return None
    person["_via_device"] = False          # For the cached-actor rule above
    return person


def _cookie(handler, name: str) -> str | None:
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def me_permissions(handler, match, body) -> None:
    """GET api/v1/me/permissions?project= — the person's permissions in that
    project and the modules they may see; the screens build their menus from
    exactly this (one route, one answered shape).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from urllib.parse import parse_qs
    qs = parse_qs(handler.path.split("?", 1)[1] if "?" in handler.path else "")
    try:
        # Project= is REQUIRED — without it "the views you may see" is a
        # question with no answer
        pid = int(qs["project"][0])
    except (KeyError, ValueError, IndexError):
        handler._error(400, "project= the project's id")
        return
    row = conn.execute("SELECT modules FROM projects WHERE id = ? AND"
                       " deleted_at IS NULL", (pid,)).fetchone()
    if row is None:
        handler._error(404, "no such project")
        return
    from . import perms as _perms
    import json as _j
    try:
        mrow = _j.loads(row["modules"] or "{}")
    except ValueError:
        mrow = {}
    modules_on = [mod for mod in _perms.ALL_MODULES
                  if mrow.get(mod, True) is not False]
    # the modules the viewer may see = those on + whose view tick they hold
    view_ticks = {"board": "view_work_packages", "now": "view_activity",
                  "tree": "browse_repository", "flow": "view_work_packages",
                  "history": "view_activity", "chat": "view_messages",
                  "aura_work": "aura_work", "members": "view_members",
                  "settings": "edit_project"}
    mods = [m for m in modules_on
            if _perms.allowed(conn, actor, view_ticks[m], pid, module=m)]
    perms = sorted(k for k in _perms.MODULE_OF
                   if _perms.allowed(conn, actor, k, pid,
                                     module=_perms.MODULE_OF[k]))
    handler._json(200, {"project": pid, "me": actor["name"],
                        "permissions": perms, "modules": mods})


def health(handler, match, body) -> None:
    """GET api/v1/health — no auth: ok, protocol, team name, this process's
    run_id.
    """
    team = ""
    try:
        conn = _db()
        if conn is not None:
            row = conn.execute("SELECT name FROM team WHERE id = 1").fetchone()
            team = row["name"] if row else ""
    except Exception:                            # noqa: BLE001
        team = ""
    handler._json(200, {"ok": True, "protocol": 1, "team": team,
                        "run_id": getattr(handler, "run_id", "")})


def info(handler, match, body) -> None:
    """GET api/v1/info — no auth (rate-limited like health): the protocol and the
    server's certificate fingerprint, so the sign-in page can show it for
    comparison with the person's Aura. Only what health already says, plus the
    fingerprint.
    """
    team = ""
    moved_at = ""
    try:
        conn = _db()
        if conn is not None:
            row = conn.execute("SELECT name FROM team WHERE id = 1").fetchone()
            team = row["name"] if row else ""
            mv = conn.execute(
                "SELECT value FROM settings WHERE key = 'hub.moved_at'"
                ).fetchone()
            if mv:
                import datetime as _dt
                moved_at = _dt.datetime.fromtimestamp(
                    float(mv["value"]), _dt.timezone.utc).isoformat(
                        timespec="seconds")
    except Exception:                            # noqa: BLE001
        team = ""
    fp = getattr(handler.server, "server_fp", "") or ""
    handler._json(200, {"ok": True, "protocol": 1, "team": team,
                        "fingerprint": fp,
                        **({"moved at": moved_at} if moved_at else {})})


def hub_lan(handler, match, body) -> None:
    """GET api/v1/hub/lan — signed in: the address teammates on the network
    type (the page may be open as 127.0.0.1 on the hub's own machine). Never
    on the open /info."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    lan = ""
    try:
        from ..teams import lan_address
        ip = lan_address()
        port = handler.server.server_address[1]
        if ip and not ip.startswith("127."):
            lan = f"https://{ip}:{port}"
    except Exception:                            # noqa: BLE001
        lan = ""
    handler._json(200, {"address": lan})


def _presence_publish(conn, person_id: "int | None", state: str) -> None:
    """Live presence: every signed-in person may see who's on, so the event
    carries no may_see restriction. Fired on the session/device activity
    transitions (sign-in, sign-out, stream open/close) so the page's people
    list lights up.
    """
    if person_id is None:
        return
    try:
        from . import teamserver_db
        row = conn.execute("SELECT name FROM people WHERE id = ?",
                           (person_id,)).fetchone()
        teamserver_db.broadcaster().publish(
            None, "presence",
            {"person_id": person_id,
             "name": row["name"] if row else "?",
             "state": state})
    except Exception:                            # noqa: BLE001
        pass                     # Presence must never break sign-in paths


def me(handler, match, body) -> None:
    """GET api/v1/me — signed or session."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    p = conn.execute("SELECT * FROM people WHERE id = ?",
                     (actor["person_id"],)).fetchone()
    proles = auth.projects_of(conn, p["id"])
    projects = []
    for pid_s, role in proles.items():
        prow = conn.execute("SELECT slug, name, archived_at FROM projects"
                            " WHERE id = ?", (int(pid_s),)).fetchone()
        if prow is None:
            continue
        projects.append({"id": int(pid_s), "slug": prow["slug"],
                         "name": prow["name"],
                         "role": role if isinstance(role, str) else "member",
                         "archived": bool(prow["archived_at"])})
    handler._json(200, {
        "person": {"id": p["id"], "name": p["name"], "display": p["display"],
                   "role": p["role"],                       # Legacy field stays
                   "team_role": p["team_role"],
                   "global_role": p["global_role"],
                   "password_set": bool(p["pw_hash"]),
                   "must_change": bool(p["must_change"]),
                   "browser_only": bool(p["browser_only"])},
        "role": p["role"], "projects": projects,
        "device": actor.get("device_id")})


def people_list(handler, match, body) -> None:
    """GET api/v1/people — anyone in the team; no secrets."""
    conn = _db()
    if _actor(conn, handler, body, need="any") is None:
        return
    rows = conn.execute(
        "SELECT id, name, display, role, team_role, global_role,"
        " pw_hash, must_change, locked_until, browser_only, created,"
        " disabled_at FROM people ORDER BY id").fetchall()
    seen = {r["person"]: r["seen"] for r in conn.execute(
        "SELECT person, MAX(last_seen) AS seen FROM devices GROUP BY person")}
    def _row(r):
        row = {k: v for k, v in dict(r).items() if k != "pw_hash"}
        row.update(web=bool(r["browser_only"]), last_seen=seen.get(r["id"]),
                   password_set=bool(r["pw_hash"]),        # Admin › Users face:
                   must_change=bool(r["must_change"]))     # The doc's words
        return row
    handler._json(200, {"people": [_row(r) for r in rows]})


def people_patch(handler, match, body) -> None:
    """Logged, person-first."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    target = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if target is None:
        handler._error(404, "no such person")
        return
    from .perms import can, is_owner_admin
    is_admin = is_owner_admin(actor)
    has_global = actor.get("global_role") == "staff and projects manager"
    if not (can(conn, actor, "team.manage") or has_global):
        handler._error(403, "only the owner, an admin, or the staff-and-"
                            "projects-manager role manages people")
        return
    if _is_owner(dict(target)) and not _is_owner(actor):
        handler._error(403, "an admin cannot change an owner")
        return
    if has_global and not is_admin and is_owner_admin(dict(target)):
        handler._error(403, "the staff-and-projects-manager role never acts "
                            "over admins")      # The doc: 'never over admins'
        return
    changes, _je = _parse_json(body)
    if _je is not None or not isinstance(changes, dict):
        handler._error(400, "the body isn't valid JSON")
        return
    from . import passwords
    now = store.now()
    fields, args = [], []
    kinds = set()
    if "display" in changes:
        fields.append("display = ?")
        args.append(str(changes["display"])[:80])
        kinds.add("person.renamed")
    if "team_role" in changes or "global_role" in changes:
        if not is_admin:
            handler._error(403, "team roles and the global role are the "
                                "owner's or an admin's")
            return
    if "team_role" in changes:
        tr = str(changes["team_role"])
        if _is_owner(dict(target)):
            handler._error(403, "the owner's team role is fixed — hand "
                                "over instead")
            return
        if tr not in ("user", "admin"):
            handler._error(400, "team_role is 'user' or 'admin'")
            return
        fields.append("team_role = ?");   args.append(tr)
        fields.append("role = ?");        args.append("member" if tr == "user"
                                                     else "admin")
        kinds.add("person.role_changed")
    if "global_role" in changes:
        gr = str(changes["global_role"] or "")
        if gr not in ("", "staff and projects manager"):
            handler._error(400, "the only global role is 'staff and "
                                "projects manager'")
            return
        fields.append("global_role = ?"); args.append(gr)
        kinds.add("person.role_changed")
    if "password" in changes:
        new_pw = str(changes["password"] or "")
        bad = passwords.validate(new_pw)
        if bad:
            handler._error(400, bad)
            return
        fields.append("pw_hash = ?")
        args.append(passwords.hash_password(new_pw))
        fields.append("must_change = ?")
        args.append(1 if changes.get("must_change", True) else 0)
        kinds.add("person.password_set")
    elif "must_change" in changes:
        fields.append("must_change = ?")
        args.append(1 if changes["must_change"] else 0)
    if "locked" in changes:
        fields.append("locked_until = ?")
        if changes["locked"]:
            args.append(now + 30 * 365 * 86400)     # Admin lock: until unlocked
            kinds.add("person.locked")
        else:
            args.append(None)
            fields.append("failed = 0")
            kinds.add("person.unlocked")
            clear_fails(handler.server, pid)    # Every address may try again
    if "disabled" in changes:
        fields.append("disabled_at = ?")
        args.append(now if changes["disabled"] else None)
        kinds.add("person.disabled" if changes["disabled"] else "person.enabled")
    if not fields:
        handler._error(400, "nothing to change (display, team_role, "
                            "global_role, password, must_change, locked, "
                            "disabled)")
        return
    with store.write(conn):
        args.append(pid)
        conn.execute(f"UPDATE people SET {', '.join(fields)} WHERE id = ?",
                     args)
        for kind_ in sorted(kinds):
            src_, ev = logmod.server_event()
            logmod.append(conn, None, src_, ev, kind_, target["name"], None,
                          {"by": actor["name"], "changes":
                           [k for k in changes if k != "password"]})
        if "disabled" in changes and changes["disabled"]:
            # Disabling ends the person's sessions and devices
            conn.execute("UPDATE sessions SET ended = ? WHERE person = ?"
                         " AND ended IS NULL", (now, pid))
            conn.execute("UPDATE devices SET revoked_at = ? WHERE person = ?"
                         " AND revoked_at IS NULL", (now, pid))
    handler._json(200, {"ok": True})



def people_me(handler, match, body) -> None:
    """PATCH api/v1/people/me — your own name/display; logged."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    changes, _je = _parse_json(body or b"{}")
    if _je is not None or not isinstance(changes, dict):
        handler._error(400, _je or "the body isn't a settings object")
        return
    old = conn.execute("SELECT name, display FROM people WHERE id = ?",
                       (actor["person_id"],)).fetchone()
    has_name = "name" in changes
    name = (str(changes.get("name") or "").strip()
            if has_name else old["name"])
    if has_name:
        if not NAME_OK.fullmatch(name):
            handler._error(400, "names are lowercase letters, digits and dashes")
            return
        dup = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE"
                           " AND id != ?",
                           (name, actor["person_id"])).fetchone()
        if dup:
            handler._error(409, "that name is taken")
            return
    elif "display" not in changes:
        handler._error(400, "names are lowercase letters, digits and dashes")
        return
    display = (str(changes["display"])[:80]
               if "display" in changes else old["display"])
    with store.write(conn):
        conn.execute("UPDATE people SET name = ?, display = ? WHERE id = ?",
                     (name, display, actor["person_id"]))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "person.renamed", name,
                      actor["person_id"], {"old": old["name"], "new": name})
    handler._json(200, {"ok": True, "name": name, "display": display})


def devices_list(handler, match, body) -> None:
    """GET /api/v1/devices — your own, or all with team.manage."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from .perms import allowed as _allowed
    if _allowed(conn, actor, "team.manage"):
        rows = conn.execute(
            "SELECT d.id, d.name, d.person, p.name AS person_name,"
            " d.created, d.last_seen, d.revoked_at FROM devices d"
            " JOIN people p ON p.id = d.person ORDER BY d.created").fetchall()
    else:
        rows = conn.execute(
            "SELECT d.id, d.name, d.person, d.created, d.last_seen,"
            " d.revoked_at FROM devices d WHERE d.person = ?"
            " ORDER BY d.created", (actor["person_id"],)).fetchall()
    handler._json(200, {"devices": [dict(r) for r in rows]})


def device_delete(handler, match, body) -> None:
    """DELETE api/v1/devices/<id> — own, or people.revoke."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    dev = match.group(1)
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (dev,)).fetchone()
    if row is None:
        handler._error(404, "no such device")
        return
    own = row["person"] == actor["person_id"]
    if not own:
        from .perms import allowed as _allowed
        # Sqlite3.Row has no.get — read it straight
        _trow = conn.execute("SELECT team_role, role FROM people"
                             " WHERE id = ?", (row["person"],)).fetchone()
        target_owner = bool(_trow) and _is_owner(dict(_trow))
        if not _allowed(conn, actor, "team.manage"):
            handler._error(403, "you can revoke your own devices; other "
                                "people's need the owner or an admin")
            return
        if target_owner and not _is_owner(actor):
            handler._error(403, "the owner's things are the owner's")
            return
    with store.write(conn):
        conn.execute("UPDATE devices SET revoked_at = ? WHERE id = ?",
                     (store.now(), dev))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "device.revoked", row["name"],
                      None, {"by": actor["name"]})
    handler._json(200, {"ok": True})


# ------- the removed inviting ways: gone in -------
def signout(handler, match, body) -> None:
    """POST api/v1/signout — session revoked (step 6); the Origin rule applies (it
    is a session-authenticated state change).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="session")
    if actor is None:
        return
    from . import sessions
    token = _cookie(handler, sessions.COOKIE)
    with store.write(conn):
        conn.execute(
            "UPDATE sessions SET ended = ? WHERE token_hash = ?",
            (store.now(), hashlib.sha256(token.encode()).digest()))
    # Presence: offline unless another stream of theirs is open
    from . import teamserver_db
    pid = actor.get("person_id")
    _presence_publish(conn, pid,
                      "online" if (pid is not None and
                                   teamserver_db.is_online(pid))
                      else "offline")
    handler._json(200, {"ok": True}, extra=sessions.clear_cookie())


_DUMMY_HASH = ("scrypt$17$8$1$"
               "AAAAAAAAAAAAAAAAAAAAAA$"
               "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")


# 20 wrong passwords lock the name FOR THE ADDRESS THAT GUESSED, for 30
# minutes: a teammate guessing can't lock someone out. The people row's
# failed/locked_until still show the Admin page what happened; an Admin lock
# (far future) stays account-wide.
import threading as _threading
_FAILS_LOCK = _threading.Lock()
ADMIN_LOCK_AFTER = 365 * 86400


def _fails(server) -> dict:
    """(person id, ip) -> [count, until], kept on the running hub."""
    with _FAILS_LOCK:
        d = getattr(server, "sign_fails", None)
        if d is None:
            d = {}
            try:
                server.sign_fails = d
            except AttributeError:
                pass
        return d


def clear_fails(server, person_id: int) -> None:
    d = _fails(server)
    with _FAILS_LOCK:
        for k in [k for k in d if k[0] == person_id]:
            del d[k]


def _guess_key(handler, person_id: int):
    return (person_id, (handler.client_address or ("?",))[0])


def guess_locked(handler, row, now: float) -> bool:
    """Locked for THIS caller: an Admin lock (account-wide), or 20 wrong
    from this address in the last 30 minutes."""
    if row["locked_until"] and row["locked_until"] > now + ADMIN_LOCK_AFTER:
        return True
    fails = _fails(handler.server)
    with _FAILS_LOCK:
        mine = fails.get(_guess_key(handler, row["id"]))
        return bool(mine and mine[1] and mine[1] > now)


def guess_wrong(handler, conn, person_id: int, now: float) -> None:
    """Count one wrong password from this address; the people row keeps the
    count and the last lock only so the Admin page can show them."""
    fails = _fails(handler.server)
    with _FAILS_LOCK:
        mine = fails.setdefault(_guess_key(handler, person_id), [0, 0.0])
        mine[0] += 1
        lock = mine[0] >= 20
        if lock:
            mine[0], mine[1] = 0, now + 1800
    with store.write(conn):
        if lock:
            conn.execute("UPDATE people SET failed = 0, locked_until = ?"
                         " WHERE id = ? AND (locked_until IS NULL"
                         " OR locked_until < ?)",
                         (now + 1800, person_id, now + ADMIN_LOCK_AFTER))
        else:
            conn.execute("UPDATE people SET failed = COALESCE(failed, 0) + 1"
                         " WHERE id = ?", (person_id,))


def guess_right(handler, person_id: int) -> None:
    fails = _fails(handler.server)
    with _FAILS_LOCK:
        fails.pop(_guess_key(handler, person_id), None)


def signin(handler, match, body) -> None:
    """POST api/v1/signin"""
    conn = _db()
    from . import passwords, sessions
    req, _je = _parse_json(body)
    if not req or not isinstance(req, dict):
        handler._error(400, "the body isn't valid JSON")
        return
    name = str(req.get("name") or "").strip()[:80]
    pw = str(req.get("password") or "")
    SAME = "the name or password isn't right"
    now = store.now()
    row = conn.execute(
        "SELECT id, name, display, role, team_role, global_role, pw_hash,"
        " must_change, failed, locked_until, disabled_at, browser_only FROM people"
        " WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is None or row["disabled_at"] or row["browser_only"]:
        # unknown/disabled/never-a-person rows get the same words AND the same
        # cost: the scrypt must run even when no row ever answers, so a caller
        # can't time "does such a person exist" by whether the answer was slow
        passwords.verify(_DUMMY_HASH, pw)            # Throwaway work, equal lane
        handler._error(401, SAME)
        return
    if guess_locked(handler, row, now):
        # The same status and words as a wrong password: a lock only ever
        # exists for real names, so it must not show
        passwords.verify(_DUMMY_HASH, pw)
        handler._error(401, SAME)
        return
    if not passwords.verify(row["pw_hash"], pw):
        guess_wrong(handler, conn, row["id"], now)
        handler._error(401, SAME)
        return
    if not row["pw_hash"]:
        handler._error(401, SAME)
        return                             # Never a differentiating note
    token = auth.new_session(conn, row["id"], "signin")
    guess_right(handler, row["id"])
    with store.write(conn):
        conn.execute("UPDATE people SET failed = 0, locked_until = NULL"
                     " WHERE id = ?", (row["id"],))
    from . import teamserver_db
    _presence_publish(conn, row["id"], "online")
    handler._json(200, {"ok": True,
                        "me": {"id": row["id"], "name": row["name"],
                               "display": row["display"],
                               "team_role": row["team_role"],
                               "global_role": row["global_role"],
                               "must_change": bool(row["must_change"])}},
                  extra=sessions.set_cookie(token))
    # (`_presence_publish` already sent; nothing else here)


def me_password(handler, match, body) -> None:
    """POST api/v1/me/password — your own password change: current proves you; a
    wrong current counts toward the same lock as sign-in.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="session")
    if actor is None:
        return
    from . import passwords, sessions
    req, _je = _parse_json(body)
    if not req or not isinstance(req, dict):
        handler._error(400, "the body isn't valid JSON")
        return
    cur = str(req.get("current") or "")
    new = str(req.get("new") or "")
    bad = passwords.validate(new)
    if bad:
        handler._error(400, bad)
        return
    row = conn.execute(
        "SELECT id, pw_hash, failed, locked_until FROM people WHERE id = ?",
        (actor["person_id"],)).fetchone()
    now = store.now()
    if guess_locked(handler, row, now):
        handler._error(401, "the current password isn't right")
        return
    if not passwords.verify(row["pw_hash"], cur):
        guess_wrong(handler, conn, actor["person_id"], now)
        handler._error(401, "the current password isn't right")
        return
    guess_right(handler, actor["person_id"])
    new_hash = passwords.hash_password(new)
    from . import auth as _auth2
    import hashlib as _hl2, secrets as _sc
    with store.write(conn):
        conn.execute("UPDATE people SET pw_hash = ?, must_change = 0,"
                     " failed = 0, locked_until = NULL WHERE id = ?",
                     (new_hash, actor["person_id"]))
        # end the person's open sessions, all of them (this one included...)
        conn.execute("UPDATE sessions SET ended = ?"
                     " WHERE person = ? AND ended IS NULL",
                     (now, actor["person_id"]))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "person.password_changed",
                      actor["name"], actor["person_id"], {})
        # ... then write the fresh one the page now carries, in the same
        # transaction — never the nested-lane CompoundWriter error
        import time as _t3
        cur_cookie_token = _sc.token_urlsafe(32)
        ts_now = _t3.time()
        conn.execute(
            "INSERT INTO sessions (token_hash, person, approved_by, created,"
            " expires, last_used) VALUES (?,?,?,?,?,?)",
            (_hl2.sha256(cur_cookie_token.encode()).digest(),
             actor["person_id"], "password-change", ts_now,
             ts_now + 30 * 86400, ts_now))
    from . import sessions as _ses
    handler._json(200, {"ok": True},
                  extra=_ses.set_cookie(cur_cookie_token)
                  if cur_cookie_token else None)


def roles_route_list(handler, match, body) -> None:
    """GET api/v1/roles — the roles the team knows: the three built-ins plus the
    admin-made customs; a member sees the set, only an admin makes or removes
    one.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from . import perms as _perms
    handler._json(200, {"roles": _perms.roles_list(conn)})


def roles_route_create(handler, match, body) -> None:
    """POST api/v1/roles — make a custom role (: name + ticks per module,
    copied-from a role).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from . import perms as _perms
    if not _perms.can(conn, actor, "team.manage"):
        handler._error(403, "only an owner or team admin makes custom roles")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None or not isinstance(req, dict):
        handler._error(400, _je or "the body isn't a settings object")
        return
    try:
        rid = _perms.role_make(
            conn, str(req.get("name") or ""), req.get("copy_from") or None,
            req.get("permissions") or {}, by=actor)
    except ValueError as e:
        handler._error(400, str(e))
        return
    src_, ev = logmod.server_event()
    with store.write(conn):
        logmod.append(conn, None, src_, ev, "role.added",
                      str(req.get("name") or "")[:60], actor["name"],
                      {"by": actor["name"], "copy_from": req.get("copy_from")})
    handler._json(201, {"ok": True, "role": _perms.role_get(conn, rid)})


def roles_route_patch(handler, match, body) -> None:
    """PATCH api/v1/roles/<id> — change a role for everyone who has it:
    {"permissions": {module: [ticks]}}, {"name": …} (custom roles), or
    {"reset": true} (a built-in back to its default)."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from . import perms as _perms
    if not _perms.can(conn, actor, "team.manage"):
        handler._error(403, "only an owner or team admin changes roles")
        return
    req, _je = _parse_json(body or b"{}")
    if _je is not None or not isinstance(req, dict):
        handler._error(400, _je or "the body isn't an object")
        return
    rid = int(match.group(1))
    try:
        with store.write(conn):
            role = _perms.role_update(conn, rid, req.get("name"),
                                      req.get("permissions"), bool(req.get("reset")))
            src_, ev = logmod.server_event()
            logmod.append(conn, None, src_, ev, "role.changed", role["name"],
                          actor["person_id"],
                          {"by": actor["name"], "reset": bool(req.get("reset")),
                           "permissions": role["permissions"]})
    except LookupError as e:
        handler._error(404, str(e))
        return
    except ValueError as e:
        handler._error(400, str(e))
        return
    handler._json(200, {"ok": True, "role": role})


def roles_route_delete(handler, match, body) -> None:
    """DELETE api/v1/roles/<id> — a role in use can't be deleted."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from . import perms as _perms
    if not _perms.can(conn, actor, "team.manage"):
        handler._error(403, "only an owner or team admin removes custom roles")
        return
    rid = int(match.group(1))
    row = _perms.role_get(conn, rid)
    if row is None:
        handler._error(404, "no such role")
        return
    try:
        _perms.role_delete(conn, rid)
    except ValueError as e:
        handler._error(409, str(e))
        return
    src_, ev = logmod.server_event()
    with store.write(conn):
        logmod.append(conn, None, src_, ev, "role.removed", row["name"], None,
                      {"by": actor["name"]})
    handler._json(200, {"ok": True})


def people_create(handler, match, body) -> None:
    """POST /api/v1/people"""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from . import passwords
    from .perms import is_owner_admin
    is_admin = is_owner_admin(actor)
    delegated = actor.get("global_role") == "staff and projects manager"
    if not (is_admin or delegated):
        handler._error(403, "only the owner, an admin, or the staff-and-"
                            "projects-manager role creates accounts")
        return
    req, _je = _parse_json(body)
    if not req or not isinstance(req, dict):
        handler._error(400, "the body isn't valid JSON")
        return
    name = str(req.get("name") or "").strip()
    if not NAME_OK.fullmatch(name):
        handler._error(400, "names are lowercase letters, digits and dashes")
        return
    pw = str(req.get("password") or "")
    bad = passwords.validate(pw)
    if bad:
        handler._error(400, bad)
        return
    team_role = str(req.get("team_role") or "user")
    global_role = str(req.get("global_role") or "")
    must = bool(req.get("must_change", True))
    if team_role not in ("user", "admin"):
        handler._error(400, "team_role is 'user' or 'admin' here (the owner "
                            "is made at setup, not from a card)")
        return
    if team_role == "admin" or global_role:
        if not is_admin:
            handler._error(403, "team roles and the global role are the "
                                "owner's or an admin's cards to hand out")
            return
    if global_role and global_role != "staff and projects manager":
        handler._error(400, "the only global role here is 'staff and projects "
                            "manager'")
        return
    if conn.execute("SELECT 1 FROM people WHERE name = ? COLLATE NOCASE",
                    (name,)).fetchone():
        handler._error(409, "that name is taken")
        return
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO people (name, display, role, team_role, global_role,"
            " pw_hash, must_change, created) VALUES (?,?,?,?,?,?,?,?)",
            (name, str(req.get("display") or name)[:80],
             "member" if team_role == "user" else "admin",
             team_role, global_role, passwords.hash_password(pw),
             1 if must else 0, store.now()))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "person.added", name,
                      actor["person_id"],
                      {"by": actor["name"], "team_role": team_role,
                       "global_role": global_role, "must_change": must})
    handler._json(201, {"ok": True, "person": {"id": cur.lastrowid,
                                               "name": name,
                                               "team_role": team_role}})


def aura_device(handler, match, body) -> None:
    """POST api/v1/aura/device — the ONE way a machine joins (, plan): the person
    signs in with THEIR name+password; the hub gets this machine's new public
    key and nothing else. No password is stored, nothing flows through a
    message. Devices aren't people: the row here stays a device (the signing
    cert of the AURA on that machine).
    """
    conn = _db()
    # The dispatcher's "open" gate already charged this request once.
    req, _je = _parse_json(body)
    if _je is not None or not isinstance(req, dict):
        handler._error(400, "the body isn't valid JSON")
        return
    name = str(req.get("name") or "").strip()[:80]
    pw = str(req.get("password") or "")
    pub = str(req.get("pubkey") or "")
    device_name = str(req.get("device_name") or "aura")[:80]
    same_message = "the name or password isn't right"
    import base64 as _b64
    try:
        pub_bytes = _b64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
        if len(pub_bytes) != 32:
            raise ValueError
    except Exception:                                # noqa: BLE001
        handler._error(400, "pubkey: base64 of a 32-byte ed25519 public key")
        return
    row = conn.execute(
        "SELECT id, name, display, role, team_role, global_role, pw_hash,"
        " must_change, disabled_at, browser_only, failed, locked_until"
        " FROM people"
        " WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    from . import passwords as _PW
    if row is None or row["disabled_at"] or row["browser_only"]:
        _PW.verify(_DUMMY_HASH, pw)                  # The same cost
        handler._error(401, same_message)
        return
    now = store.now()
    if guess_locked(handler, row, now):
        _PW.verify(_DUMMY_HASH, pw)
        handler._error(401, same_message)
        return
    if not _PW.verify(row["pw_hash"], pw):
        guess_wrong(handler, conn, row["id"], now)
        handler._error(401, same_message)
        return
    guess_right(handler, row["id"])
    import uuid
    dev = uuid.uuid4().hex
    with store.write(conn):
        conn.execute(
            "INSERT INTO devices (id, person, name, pubkey, created,"
            " last_seen) VALUES (?,?,?,?,?,?)",
            (dev, row["id"], device_name, pub_bytes, now, now))
        conn.execute("UPDATE people SET failed = 0, locked_until = NULL"
                     " WHERE id = ?", (row["id"],))
        src_, ev = logmod.server_event()
        logmod.append(conn, None, src_, ev, "device.registered",
                      device_name, None, {"person": row["name"]})
    me = {"id": row["id"], "name": row["name"], "display": row["display"],
          "team_role": row["team_role"], "global_role": row["global_role"],
          "must_change": bool(row["must_change"])}
    handler._json(200, {"ok": True, "device_id": dev, "me": me})


def sessions_list(handler, match, body) -> None:
    """GET /api/v1/sessions — own, or all with team.manage."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from .perms import allowed as _allowed
    if _allowed(conn, actor, "team.manage"):
        rows = conn.execute(
            "SELECT s.token_hash, s.person, p.name AS person_name,"
            " s.approved_by, s.created, s.expires, s.last_used, s.ended,"
            " s.agent, s.revoked_at"
            " FROM sessions s JOIN people p ON p.id = s.person"
            " ORDER BY s.created DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT token_hash, person, approved_by, created, expires,"
            " last_used, ended, agent, revoked_at FROM sessions WHERE person = ?"
            " ORDER BY created DESC", (actor["person_id"],)).fetchall()
    from . import sessions
    token = (None if actor.get("_via_device")
             else _cookie(handler, sessions.COOKIE))
    request_hash = (hashlib.sha256(token.encode()).digest()
                    if token is not None else None)
    out = []
    for r in rows:
        d = dict(r)
        th = d.pop("token_hash")
        out.append({**{k: v for k, v in d.items()},  # No bytes here
                    "token": hashlib.sha256(th).hexdigest()[:12],
                    "current": bool(request_hash is not None and
                                    hmac.compare_digest(th, request_hash))})
    handler._json(200, {"sessions": out})


def session_delete(handler, match, body) -> None:
    """DELETE /api/v1/sessions/<hash12> — own, or team.manage."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    prefix = match.group(1)
    rows = conn.execute(
        "SELECT token_hash, person FROM sessions WHERE revoked_at IS NULL"
        ).fetchall()
    hit = [r for r in rows
           if hmac.compare_digest(
               hashlib.sha256(r["token_hash"]).hexdigest()[:12], prefix)]
    if not hit:
        handler._error(404, "no such open session")
        return
    row = hit[0]
    own = row["person"] == actor["person_id"]
    from .perms import allowed as _allowed
    _trow = conn.execute("SELECT team_role, role FROM people WHERE id = ?",
                          (row["person"],)).fetchone()
    target_owner = bool(_trow) and _is_owner(dict(_trow))
    if not own and not _allowed(conn, actor, "team.manage"):
        handler._error(403, "you can revoke your own sessions; others need "
                            "the owner or an admin")
        return
    if not own and target_owner and not _is_owner(actor):
        handler._error(403, "the owner's sessions are the owner's")
        return
    with store.write(conn):
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
            (store.now(), row["token_hash"]))
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "session.revoked", prefix, None,
                      {"by": actor["name"]})
    # Presence: offline unless another stream of theirs is open
    from . import teamserver_db
    _presence_publish(conn, row["person"],
                      "online" if teamserver_db.is_online(row["person"])
                      else "offline")
    handler._json(200, {"ok": True})


# --------------------------------------- browser-only people
def _port_of(host: str) -> str:
    return host.rpartition(":")[2] or "7443"


def _sock_hostname() -> str:
    import socket
    try:
        return socket.gethostname().lower()
    except Exception:                            # noqa: BLE001
        return ""

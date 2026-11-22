"""teamserver/chatlinks.py — linked rooms.

One of our rooms ↔ one outside channel. Endpoints: POST
api/v1/projects/<pid>/links (project.settings — create) GET
api/v1/projects/<pid>/links (project member — list) DELETE
api/v1/projects/<pid>/links/<id> (project.settings — remove) POST
api/v1/links/<id>/ingest (open, shared secret + Standard-Webhooks signature,
5-minute timestamp window) Secrets: the endpoint URL, the token and the ingest
secret live in the server_secrets table; the chat_links row keeps only pointer
keys and nothing shows more than a fingerprint.

Outbound: a post in a linked room is relayed by the post-commit dispatcher
(api_log.dispatch_pending — the seam chosen because api_chat/sse are not the
link layer to edit; one dispatcher serves notices + links + webhooks). No echo:
each relay stamps its origin id ("aura-<msg id>") into chatlinks_seen so an
echo of it dedups like any inbound, and every message arriving through a link
is authored by the clearly non-human 'linked-app' sentinel — the relay skips
sentinel-authored rows, so nothing ever goes back where it came from. Ghost
accounts: none — one sentinel for all links, the body keeps the origin label
("Ravi (Slack): …"); the trade is that per-app per-person attribution lives in
the TEXT, not the people table (spec's own reading of the mautrix lesson).

Inbound: POST links/<id>/ingest (generic webhook shape) or the poll loop
(poll_links reads ChatLink.read_since where a real app's data wiring exists).
No DMs are ever linked: creation refuses a dm room, and ingest targets only the
link's non-dm room or the project room.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import log as logmod
from . import store

SENTINEL = "linked-app"
APPS = ("slack", "discord", "telegram", "teams", "webhook")


# ------------------------------------------------------------- the sentinel
def link_person_id(conn) -> int:
    """Id of the 'linked-app' pseudo-person (guest, browser-only, clearly
    non-human;: linked messages are never authored by person id 1).
    """
    row = conn.execute("SELECT id FROM people WHERE name = ?",
                       (SENTINEL,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO people (name, display, role, browser_only, created)"
        " VALUES (?, ?, 'guest', 1, ?)",
        (SENTINEL, "linked app (not a person) — authors every message a"
                   " room link brings in", store.now()))
    return cur.lastrowid


def _link_secret(conn, link: dict, field: str) -> str:
    return store.secret_get(conn, str(link.get(field) or ""))


def link_public(link: dict) -> dict:
    """The admin-facing shape: fingerprints only, never a secret value."""
    return {
        "id": link["id"], "project": link["project"], "room": link["room"],
        "name": link["name"], "app": link["app"],
        "endpoint": bool(link["endpoint_ref"]),
        "has_token": bool(link["token_ref"]),
        "ingest_secret_set": bool(link["secret_ref"]),
        "out_cursor": link["out_cursor"], "in_cursor": link["in_cursor"],
        "created": link["created"],
        "last_error": link["last_error"], "last_error_at": link["last_error_at"]}


def _row_public(conn, link: dict) -> dict:
    out = link_public(link)
    for ref, key in (("endpoint_ref", "endpoint_fp"),
                     ("token_ref", "token_fp")):
        val = _link_secret(conn, link, ref)
        if val:
            out[key] = store.secret_fp(val)
    return out


# ------------------------------------------------------------- the contract
class ChatLink:
    """`post/read_since(cursor)/channels/whoami` — data wiring. Outbound goes
    through the same per-app senders + health/back-off the notices use
    (team_notices.send_notice_target's channel rule).
    """

    def __init__(self, link: dict, endpoint: str = "", token: str = "",
                 secret: str = ""):
        self.link = link
        self.id = link.get("id")
        self.project = link.get("project")
        self.name = str(link.get("name") or "linked")
        self.app = str(link.get("app") or "webhook")
        self.endpoint = endpoint
        self.token = token
        self.secret = secret
        self.cursor: str | None = link.get("in_cursor") or None

    def post(self, text: str, origin: str = "") -> dict:
        """Anti-echo delivery: the payload carries our origin id; a real
        outside app relays it back, and chatlinks_seen then dedups it."""
        text = text[:4000]
        headers: dict = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        gw = _gw()
        if self.app in ("slack", "discord", "telegram") and gw is not None:
            # The gateway's own senders: health + back-off included
            if self.app == "telegram":
                if not (self.token and self.name):
                    return {"ok": False,
                            "detail": "telegram needs bot token + chat_id"}
                return gw.deliver_message(text, "telegram",
                                          token=self.token,
                                          chat_id=self.name)
            if not self.endpoint:
                return {"ok": False,
                        "detail": f"{self.app} needs a webhook URL"}
            return gw.deliver_message(text, self.app, url=self.endpoint)
        if self.app == "teams":
            payload = {"@type": "MessageCard",
                       "@context": "http://schema.org/extensions",
                       "summary": self.name, "sections": [{"text": text}]}
        else:
            payload = {"text": text, "channel": self.name, "id": origin}
        body = json.dumps(payload, ensure_ascii=False).encode()
        if self.secret and self.app == "webhook":
            from .api_monitor import sign_webhook
            headers.update(sign_webhook(None, {"secret": self.secret}, body))
        res = _post_json(self.endpoint, body, headers)
        _health_record(self.app, self.endpoint, res)
        return res

    def read_since(self, cursor: str | None):
        """(messages, next_cursor): the generic webhook app has no history
        API — real apps are wired here as DATA (teams_wiring-style)."""
        return [], cursor

    def channels(self) -> list[str]:
        return [self.name]

    def whoami(self) -> str:
        return f"{self.app}:{self.name}"


def _gw():
    try:
        from .. import gateway
        return gateway
    except Exception:                                # noqa: BLE001
        return None


def _health_record(app: str, url: str, res: dict) -> None:
    """Reuse the gateway's per-target back-off for the custom payloads;
    best-effort — gateway internals are inherited privately and may not exist
    in another tree.
    """
    gw = _gw()
    if gw is None or not url:
        return
    try:
        key = gw._target_key(app, {"url": url})
        gw._record(key, res, store.now(), res.pop("retry_after", None))
    except Exception:                                # noqa: BLE001
        pass


def unsafe_target(url: str) -> str:
    """'' when the hub may post to this URL, else why not. Never this computer
    (loopback: the hub itself, a local model server), link-local (cloud
    metadata), unspecified, multicast or reserved addresses. A server on the
    team's own network (10.x, 192.168.x) stays allowed: self-hosted chat
    lives there."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return "the address can't be read"
    if not host:
        return "the address has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ""                    # Not resolvable now: the send just fails
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_multicast or ip.is_reserved):
            return "that address is this computer or a special network address"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: it could point the hub somewhere it may not go."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_json(url: str, body: bytes, headers: dict | None = None,
               timeout: float = 10.0) -> dict:
    """The other side's reply text is never kept or shown: only its status."""
    if not url.startswith(("http://", "https://")):
        return dict(ok=False, status=0, detail="the link has no endpoint URL")
    why = unsafe_target(url)
    if why:
        return dict(ok=False, status=0, detail=why)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return dict(ok=200 <= r.status < 300, status=r.status,
                        detail="" if r.status < 300 else f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        return dict(ok=False, status=e.code,
                    retry_after=float(ra) if ra and ra.isdigit() else None,
                    detail=f"HTTP {e.code}")
    except Exception:                                # noqa: BLE001
        return dict(ok=False, status=0, detail="the other side didn't answer")


def chatlink_from_row(conn, link: dict) -> ChatLink:
    return ChatLink(link, endpoint=_link_secret(conn, link, "endpoint_ref"),
                    token=_link_secret(conn, link, "token_ref"),
                    secret=_link_secret(conn, link, "secret_ref"))


# ------------------------------------------------------------------ inbound
def ingest(conn, pid: int, link_name: str, message: dict,
           link: dict | None = None) -> bool:
    """A linked message arrives: posted into the link's room (the project
    room when unnamed) as 'Name (App): …', authored by the linked-app
    sentinel. NEVER echoed back (the relay skips sentinel-authored rows and
    the origin id lands in chatlinks_seen); no DMs; a removed person's
    messages stop (membership checked on ingest)."""
    origin = str(message.get("id") or "")
    if not origin:
        return False                       # No origin id, no dedup → refuse
    person = str(message.get("person") or "?")[:120]
    text = str(message.get("text") or "")[:2000]
    pid_inserted = None
    with store.write(conn):
        seen = conn.execute(
            "SELECT 1 FROM chatlinks_seen WHERE project = ? AND origin = ?",
            (pid, origin)).fetchone()
        if seen:
            return False                                  # Already ingested
        member = conn.execute(
            "SELECT 1 FROM people p JOIN project_members pm ON pm.person ="
            " p.id WHERE p.name = ? AND pm.project = ? AND p.disabled_at IS"
            " NULL", (person, pid)).fetchone()
        if not member:
            return False                                  # removed/disabled
        room_id = None
        if link is not None:
            room = conn.execute(
                "SELECT id, kind FROM rooms WHERE id = ?", (link["room"],)
            ).fetchone()
            if room and room["kind"] != "dm":
                room_id = room["id"]
        if room_id is None:
            room = conn.execute(
                "SELECT id FROM rooms WHERE project = ? AND kind = 'project'",
                (pid,)).fetchone()
            if room is None:
                return False
            room_id = room["id"]
        body = f"{person} ({link_name}): {text}"
        from ..secrets_guard import scrub_secrets
        cur = conn.execute(
            "INSERT INTO msgs (room, person, body, ts) VALUES (?, ?, ?, ?)",
            (room_id, link_person_id(conn), scrub_secrets(body),
             store.now()))
        pid_inserted = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO chatlinks_seen (project, origin, ingested)"
            " VALUES (?, ?, ?)", (pid, origin, store.now()))
        if link is not None:
            conn.execute("UPDATE chat_links SET in_cursor = ? WHERE id = ?",
                         (origin, link["id"]))
    try:
        from . import teamserver_db
        from .api_chat import room_may_see
        teamserver_db.broadcaster().publish(None, "chat", {
            "kind": "msg", "room": room_id, "id": pid_inserted,
            "person": SENTINEL, "person_id": link_person_id(conn),
            "reply_to": None, "body": body[:200], "ts": store.now()},
            may_see=room_may_see(conn, room_id))
    except Exception:                                # noqa: BLE001
        pass
    return True


def poll_links(conn, limit: int = 20, only_link: int | None = None) -> int:
    """The poll loop entry: for links whose wiring knows read_since, pull new
    messages in. The generic webhook app has nothing to poll — its inbound is
    POST api/v1/links/<id>/ingest instead.
    """
    pulled = 0
    rows = (conn.execute("SELECT * FROM chat_links WHERE id = ?", (only_link,))
            if only_link is not None
            else conn.execute("SELECT * FROM chat_links ORDER BY id")).fetchall()
    for r in rows:
        link = dict(r)
        try:
            msgs, cur = chatlink_from_row(conn, link).read_since(
                link["in_cursor"] or None)
        except Exception as e:                       # noqa: BLE001
            _link_error(conn, link, e)
            continue
        for m in (msgs or [])[:limit]:
            if ingest(conn, link["project"], link["name"], m, link=link):
                pulled += 1
        if cur and cur != link["in_cursor"]:
            with store.write(conn):
                conn.execute(
                    "UPDATE chat_links SET in_cursor = ? WHERE id = ?",
                    (str(cur), link["id"]))
    return pulled


def _link_error(conn, link: dict, e) -> None:
    try:
        with store.write(conn):
            conn.execute(
                "UPDATE chat_links SET last_error = ?, last_error_at = ?"
                " WHERE id = ?",
                (str(e)[:160], store.now(), link["id"]))
    except Exception:                                # noqa: BLE001
        pass


# ----------------------------------------------------------------- outbound
def relay_outbound(conn, since_id: int, limit: int = 200) -> tuple[int, int]:
    """Relay msgs with id > since_id that belong to a linked room.
    Returns (relayed, new_high_water). NOT called inside a transaction —
    each send is network I/O and every dedup/error write is its own."""
    rows = conn.execute(
        "SELECT id, room, person, body, ts, deleted FROM msgs WHERE id > ?"
        " ORDER BY id LIMIT ?", (since_id, limit)).fetchall()
    if not rows:
        return 0, since_id
    links_by_room: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT * FROM chat_links").fetchall():
        links_by_room.setdefault(r["room"], []).append(dict(r))
    sentinel = link_person_id(conn)
    people = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM people")}
    relayed, last = 0, since_id
    for m in rows:
        last = m["id"]
        links = links_by_room.get(m["room"]) or []
        if not links or m["deleted"] is not None:
            continue
        if m["person"] == sentinel:
            continue                       # Came FROM a link — never goes back
        sender = people.get(m["person"], "?")
        origin = f"aura-{m['id']}"
        for lrow in links:
            if m["ts"] < lrow["created"]:
                continue                   # History from before the link
                                           # existed is OURS, not the app's
            link = chatlink_from_row(conn, lrow)
            res = link.post(f"{sender}: {m['body'][:3800]}", origin=origin)
            if res.get("ok"):
                relayed += 1
                with store.write(conn):
                    # The stamped origin id: an echo of this message dedups
                    # exactly like an inbound one ("no echo")
                    conn.execute(
                        "INSERT OR IGNORE INTO chatlinks_seen (project,"
                        " origin, ingested) VALUES (?, ?, ?)",
                        (lrow["project"], origin, store.now()))
                    conn.execute(
                        "UPDATE chat_links SET out_cursor = ?,"
                        " last_error = '', last_error_at = NULL WHERE id = ?",
                        (origin, lrow["id"]))
            else:
                _link_error(conn, lrow,
                            res.get("detail") or
                            f"HTTP {res.get('status') or '?'}")
    return relayed, last


# ------------------------------------------------------------------ the API
def _db():
    from . import teamserver_db
    return teamserver_db.open_team()


def _visible(conn, actor, pid) -> bool:
    from .api_projects import _visible as _v
    return _v(conn, actor, pid)


def links_list(handler, match, body) -> None:
    """GET /api/v1/projects/<id>/links — any project member; fingerprints
    and cursors only, never a secret value."""
    from .api_people import _actor
    conn = _db()
    if conn is None:
        handler._error(404, "no team yet")
        return
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if not _visible(conn, actor, pid):
        handler._error(404, "no such project")
        return
    rows = [_row_public(conn, dict(r)) for r in conn.execute(
        "SELECT * FROM chat_links WHERE project = ? ORDER BY id", (pid,))]
    handler._json(200, {"links": rows})


def link_create(handler, match, body) -> None:
    """POST /api/v1/projects/<id>/links {name, app, endpoint, token?, room?}.
    project.settings (manager and up). The endpoint and token go straight to
    the secrets table; the ingest secret is generated and shown ONCE."""
    from .api_people import _actor
    from .perms import Forbidden, require
    conn = _db()
    if conn is None:
        handler._error(404, "no team yet")
        return
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    try:
        require(conn, actor, "edit_project", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    # Archived = read-only — links are write lanes too
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    from .http import parse_json as _parse_json
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    name = str(req.get("name") or "").strip()[:80]
    app = str(req.get("app") or "webhook").strip().lower()
    endpoint = str(req.get("endpoint") or "").strip()[:300]
    token = str(req.get("token") or "").strip()[:300]
    if not name:
        handler._error(400, "name the outside channel")
        return
    if app not in APPS:
        handler._error(400, "app is one of " + ", ".join(APPS))
        return
    if endpoint and not endpoint.startswith(("http://", "https://")):
        handler._error(400, "the endpoint is an http(s) URL")
        return
    why = unsafe_target(endpoint) if endpoint else ""
    if why:
        handler._error(400, why)
        return
    room = conn.execute(
        "SELECT id FROM rooms WHERE project = ? AND kind = 'project'",
        (pid,)).fetchone()
    if req.get("room"):
        room = conn.execute(
            "SELECT id, kind FROM rooms WHERE id = ? AND project = ?",
            (int(req["room"]), pid)).fetchone()
        if room is None:
            handler._error(404, "no such room in this project")
            return
    if room is None:
        handler._error(404, "this project has no linkable room")
        return
    room_row = conn.execute("SELECT kind FROM rooms WHERE id = ?",
                            (room["id"],)).fetchone()
    if room_row and room_row["kind"] == "dm":
        handler._error(400, "no DMs are linked")
        return
    ingest_secret = secrets.token_urlsafe(24)
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO chat_links (project, room, name, app, created_by,"
            " created) VALUES (?,?,?,?,?,?)",
            (pid, room["id"], name, app, actor["person_id"], store.now()))
        lid = cur.lastrowid
        refs = {}
        if endpoint:
            fp = store.secret_put(conn, f"link:{lid}:endpoint", endpoint)
            refs["endpoint_ref"] = f"link:{lid}:endpoint"
        if token:
            store.secret_put(conn, f"link:{lid}:token", token)
            refs["token_ref"] = f"link:{lid}:token"
        store.secret_put(conn, f"link:{lid}:secret", ingest_secret)
        refs["secret_ref"] = f"link:{lid}:secret"
        conn.execute(
            "UPDATE chat_links SET endpoint_ref = ?, token_ref = ?,"
            " secret_ref = ? WHERE id = ?",
            (refs.get("endpoint_ref", ""), refs.get("token_ref", ""),
             refs["secret_ref"], lid))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "settings.changed", "linked-rooms",
                      actor["person_id"],
                      {"old": {}, "new": {"link": {
                          "name": name, "app": app, "room": room["id"],
                          "endpoint": "[stored]" if endpoint else ""}}})
    link = dict(conn.execute("SELECT * FROM chat_links WHERE id = ?",
                             (lid,)).fetchone())
    handler._json(200, {"ok": True, "link": _row_public(conn, link),
                        "ingest_secret": ingest_secret,
                        "note": "the ingest secret is shown once — store it"})


def pull_link(handler, match, body) -> None:
    """POST /projects/<pid>/links/<lid>/pull — an authorised person asks the
    hub itself to poll one stored wiring. It never returns connection secrets.
    Generic webhooks legitimately have nothing to poll; inbound POST /ingest
    remains their signed delivery lane (and is not silently emulated)."""
    from .api_people import _actor
    from .perms import Forbidden, require
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, lid = int(match.group(1)), int(match.group(2))
    try:
        require(conn, actor, "edit_project", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:
        handler._error(409, "this project is archived — unarchive it first")
        return
    row = conn.execute("SELECT * FROM chat_links WHERE id = ? AND project = ?",
                       (lid, pid)).fetchone()
    if row is None:
        handler._error(404, "no such linked room")
        return
    landed = poll_links(conn, only_link=lid)
    fresh = conn.execute("SELECT * FROM chat_links WHERE id = ?", (lid,)).fetchone()
    handler._json(200, {"ok": True, "landed": landed,
                        "link": _row_public(conn, dict(fresh))})


def link_delete(handler, match, body) -> None:
    """DELETE /api/v1/projects/<id>/links/<lid> — project.settings."""
    from .api_people import _actor
    from .perms import Forbidden, require
    conn = _db()
    if conn is None:
        handler._error(404, "no team yet")
        return
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid, lid = int(match.group(1)), int(match.group(2))
    try:
        require(conn, actor, "edit_project", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    _ar = conn.execute("SELECT archived_at FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    if _ar and _ar["archived_at"]:          # Archived = read-only
        handler._error(409, "this project is archived — unarchive it first")
        return
    link = conn.execute("SELECT * FROM chat_links WHERE id = ?"
                        " AND project = ?", (lid, pid)).fetchone()
    if link is None:
        handler._error(404, "no such link")
        return
    with store.write(conn):
        for ref in ("endpoint_ref", "token_ref", "secret_ref"):
            store.secret_drop(conn, str(link[ref] or ""))
        conn.execute("DELETE FROM chat_links WHERE id = ?", (lid,))
        src, ev = logmod.server_event()
        logmod.append(conn, pid, src, ev, "settings.changed", "linked-rooms",
                      actor["person_id"],
                      {"old": {"link": {"name": link["name"],
                                        "app": link["app"]}}, "new": {}})
    handler._json(200, {"ok": True})


def link_ingest(handler, match, body) -> None:
    """POST /api/v1/links/<id>/ingest — the generic webhook inbound: shared
    secret + Standard-Webhooks signature over the raw body, 5-minute
    timestamp window (api_monitor._verify)."""
    from . import api_monitor
    conn = _db()
    if conn is None:
        handler._error(404, "no team yet")
        return
    link = conn.execute("SELECT * FROM chat_links WHERE id = ?",
                        (int(match.group(1)),)).fetchone()
    if link is None:
        handler._error(404, "no such link")
        return
    secret = _link_secret(conn, dict(link), "secret_ref")
    wid = handler.headers.get("webhook-id") or ""
    ts = handler.headers.get("webhook-timestamp") or ""
    sig = handler.headers.get("webhook-signature") or ""
    if not secret or not api_monitor._verify(secret, wid, ts, body or b"",
                                             sig):
        handler._error(403, "bad signature — the ingest is signed with the"
                            " link's secret (shown once at link creation)")
        return
    from .http import parse_json as _parse_json
    req, _je = _parse_json(body or b"{}")
    if _je is not None:
        handler._error(400, _je)
        return
    landed = ingest(conn, link["project"], link["name"],
                    {"id": req.get("id"), "person": req.get("person"),
                     "text": req.get("text")}, link=dict(link))
    handler._json(200, {"ok": True, "landed": landed})

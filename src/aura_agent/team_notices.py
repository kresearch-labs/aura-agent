"""Team notices out + the secret split.

A project's key moments (the same Activity view moments: started, finished,
FAILED tests, blocked, decisions, overlaps — a green test.ran is NOT a moment)
go to the `notices` targets the admin puts in the project's settings. storage
split: a target's webhook URL and the signing secret never live in
projects.settings — settings keep only "[stored]" + a server_secrets pointer +
a sha256 fingerprint; the real value is read from the keystore table at send
time. GET api/v1/projects and the settings.changed log entry go through
redact_settings and so never carry a URL (not even a legacy plaintext one).

Delivery: slack/discord/telegram go through gateway.py's own senders (its
per-target health + back-off + flood limit included). The generic webhook and
Teams Workflows keep that same health layer (gateway._blocked/_record) but post
their own payloads: the generic one MUST carry the Standard-Webhooks signature
(a per-notice secret, auto-provisioned into the secrets table on first use for
legacy configs) and Teams needs a MessageCard — gateway's senders do neither.
Failed sends are recorded per target in notice_health and the event is NOT
marked in notice_outbox — the sweep retries it later (a notice on a failed send
is never marked sent).
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request

from .teamserver import store
from .secrets_guard import scrub_secrets  # noqa


def notices_for(conn, pid: int) -> list[dict]:
    try:
        row = conn.execute("SELECT settings FROM projects WHERE id = ?",
                           (pid,)).fetchone()
        if not row or not row["settings"]:
            return []
        s = json.loads(row["settings"])
        out = s.get("notices") or []
        return out if isinstance(out, list) else []
    except Exception:                                # noqa: BLE001
        return []


NOTICE_TYPES = ("task.started", "task.finished", "test.ran", "task.blocked",
                "decision.made", "overlap")
APP_KIND = {"slack": "slack", "discord": "discord", "telegram": "telegram",
            "webhook": "webhook", "teams": "teams"}
STORED = "[stored]"


def notable(etype: str, data: dict) -> bool:
    """Is this log event one of the moments? Activity detail=0 reads exactly the
    same way — above all, a GREEN test.ran is detail, not a moment.
    """
    if etype not in NOTICE_TYPES:
        return False
    if etype == "test.ran" and not (data or {}).get("failed"):
        return False                               # Only FAILED tests
    return True


def _line(etype: str, data: dict, subject: str) -> str:
    """One short line, log-derived (the Activity sentence)."""
    from .teamserver.api_views import _sentence
    return _sentence(etype, data, subject)[:240]


# ------------------------------------------------ redaction
def redact_settings(settings: dict) -> dict:
    """The notices config with every real secret stripped: `where`/`secret`
    become "[stored]" (a sha256 fingerprint stays so the admin can tell
    targets apart). Legacy entries written before the split still redact."""
    if not isinstance(settings, dict) or not isinstance(
            settings.get("notices"), list):
        return settings
    import copy
    out = copy.deepcopy(settings)
    clean = []
    for n in out["notices"]:
        if not isinstance(n, dict):
            continue
        entry = {k: v for k, v in n.items()
                 if k not in ("where", "secret", "token")}
        where = str(n.get("where") or "")
        if where and where != STORED and not n.get("where_fp"):
            entry["where_fp"] = store.secret_fp(where)
        if n.get("secret") and not n.get("secret_fp"):
            entry["secret_fp"] = store.secret_fp(str(n.get("secret")))
        if n.get("token") and not n.get("token_fp"):
            entry["token_fp"] = store.secret_fp(str(n.get("token")))
        if where or n.get("where_ref"):
            entry["where"] = STORED
        if n.get("secret") or n.get("secret_ref"):
            entry["secret"] = STORED
        if n.get("token") or n.get("token_ref"):
            entry["token"] = STORED
        clean.append(entry)
    out["notices"] = clean
    return out


def _resolve(conn, pid: int, idx: int, n: dict) -> dict:
    """The effective send view of one notice entry: real URL/token/secret
    read from the secrets table; a legacy plaintext `where` still works."""
    app = APP_KIND.get(str(n.get("app") or "webhook"), "webhook")
    where = str(n.get("where") or "")
    url = store.secret_get(conn, str(n.get("where_ref") or ""))
    if not url and where and where != STORED:
        url = where                                 # Pre-split legacy row
    token = store.secret_get(conn, str(n.get("token_ref") or ""))
    if not token and str(n.get("token") or "") != STORED:
        token = str(n.get("token") or "")
    secret = store.secret_get(conn, str(n.get("secret_ref") or ""))
    if not secret and str(n.get("secret") or "") != STORED:
        secret = str(n.get("secret") or "")
    fp = str(n.get("where_fp") or "")
    if not fp:
        fp = store.secret_fp(url) if url else f"{app}:{pid}:{idx}"
    return {"app": app, "url": url, "token": token, "secret": secret,
            "fp": fp, "types": n.get("types")}


def _gateway():
    """The personal gateway's senders ARE importable from the server process
    (aura_agent.gateway — the team server is the same package and the health
    file then lives under the server's own profile). Verified: gateway.py
    imports only .paths/.keystore, neither imports teamserver."""
    try:
        from . import gateway
        return gateway
    except Exception:                                # noqa: BLE001
        return None


def _post_json(url: str, body: bytes, headers: dict | None = None,
               timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(ok=200 <= r.status < 300, status=r.status,
                        detail=r.read().decode("utf-8",
                                               errors="replace")[:200])
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        return dict(ok=False, status=e.code,
                    retry_after=float(ra) if ra and ra.isdigit() else None,
                    detail=(e.read() or b"").decode("utf-8",
                                                    errors="replace")[:200])
    except Exception as e:                           # noqa: BLE001
        return dict(ok=False, status=0, detail=str(e)[:200])


def send_notice_target(conn, target: dict, line: str, project: str) -> dict:
    """One line to one target — {ok, detail, ...}, never raises.

    Channel routing: slack/discord/telegram go through gateway's
    deliver_message (health, back-off, flood limit, outbox on hold). teams
    and the signed generic webhook reuse gateway's health LAYER (same
    per-target key) but post their own payload — the gateway's generic
    sender can neither attach Standard-Webhooks signature headers nor shape
    a Teams MessageCard."""
    line = scrub_secrets(line)
    app, url = target["app"], target["url"]
    text = f"[{project}] {line}"
    gw = _gateway()
    if app in ("slack", "discord", "telegram") and gw is not None:
        if app in ("slack", "discord"):
            if not url:
                return {"ok": False, "detail": f"{app} needs a webhook URL"}
            return gw.deliver_message(text, app, url=url)
        # telegram: url field carries the chat_id; token the bot token
        if not (url and target["token"]):
            return {"ok": False,
                    "detail": "telegram needs bot token + chat_id"}
        return gw.deliver_message(text, "telegram",
                                  token=target["token"], chat_id=url)
    # custom payloads, gateway health layer when importable
    key = None
    now = store.now()
    if gw is not None and url:
        key = gw._target_key(app, {"url": url})
        why = gw._blocked(key, now)
        if why:
            return {"ok": False, "held": True,
                    "detail": f"not sent to {app}: {why}"}
    headers: dict = {}
    if app == "teams":
        body = json.dumps({
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "0076D7",
            "summary": f"[{project}] key moment",
            "sections": [{"text": f"**[{project}]** {line}"}],
        }, ensure_ascii=False).encode()
    else:
        # The generic webhook is SIGNED: a per-notice secret lives in the
        # secrets table; no secret → unsigned is a config error, notice_once
        # auto-provisions one before we get here
        payload = {"text": line, "project": project}
        body = json.dumps(payload, ensure_ascii=False).encode()
        secret = target.get("secret") or ""
        if secret:
            from .teamserver.api_monitor import sign_webhook
            headers.update(sign_webhook({"secret": secret}, body))
    if not url:
        return {"ok": False, "detail": f"{app} needs a URL"}
    res = _post_json(url, body, headers)
    res.setdefault("where", f"{app}:{target['fp']}")
    if gw is not None and key:
        gw._record(key, res, now, res.pop("retry_after", None))
    return res


def _record_health(conn, pid: int, fp: str, app: str, res: dict) -> None:
    """The last send result per (project, target) — the Admin page's
    observability for notice targets."""
    err = "" if res.get("ok") else str(
        res.get("detail") or f"HTTP {res.get('status') or '?'}")[:160]
    try:
        with store.write(conn):
            conn.execute(
                "INSERT INTO notice_health (project, target, app, ok, error,"
                " at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(project, target) DO UPDATE SET"
                " app = excluded.app, ok = excluded.ok, error = excluded.error,"
                " at = excluded.at",
                (pid, fp, app, 1 if res.get("ok") else 0, err, store.now()))
    except Exception:                                # noqa: BLE001
        pass


def _ensure_sign_secret(conn, pid: int, idx: int, t: dict) -> str:
    """For: EVERY generic-webhook notice post is signed. Configs written before
    the split (or by hand) get a secret auto-provisioned into the keystore
    table; the pointer is mirrored into the settings entry so the admin can see
    its fingerprint.
    """
    key = f"noticesign:{pid}:{idx}"
    secret = store.secret_get(conn, key)
    if secret:
        return secret
    secret = secrets.token_urlsafe(24)
    try:
        with store.write(conn):
            store.secret_put(conn, key, secret)
            row = conn.execute("SELECT settings FROM projects WHERE id = ?",
                               (pid,)).fetchone()
            s = json.loads(row["settings"]) if row and row["settings"] else {}
            ns = s.get("notices") or []
            if isinstance(ns, list) and idx < len(ns) \
                    and isinstance(ns[idx], dict):
                ns[idx]["secret_ref"] = key
                ns[idx]["secret_fp"] = store.secret_fp(secret)
                conn.execute("UPDATE projects SET settings = ? WHERE id = ?",
                             (json.dumps(s, ensure_ascii=False), pid))
    except Exception:                                # noqa: BLE001
        pass
    return secret


def notice_once(conn, pid: int, ev_id: str, etype: str, data: dict,
                subject: str, project_name: str) -> int:
    """Post the key moment to every notice target of the project — once per event.
    OUTBOX GUARD: the outbox row is written in its own store.write AFTER the
    posts (never nested inside the caller's transaction — the old nesting made
    BEGIN IMMEDIATE raise inside reports_in and the swallowed OperationalError
    marked events sent that never went out), and ONLY when something was
    actually sent: an event whose sends all failed stays unmarked so
    sweep_unsent retries it.
    """
    if pid is None:
        return 0
    if conn.execute("SELECT 1 FROM notice_outbox WHERE project = ?"
                    " AND ev_id = ?", (pid, ev_id)).fetchone():
        return 0                                      # Already sent
    line = _line(etype, data, subject)
    eligible, sent = 0, 0
    for idx, n in enumerate(notices_for(conn, pid)):
        types = n.get("types") or list(NOTICE_TYPES)
        if etype not in types:
            continue
        t = _resolve(conn, pid, idx, n)
        if not t["url"] and not (t["app"] == "telegram" and t["token"]):
            continue
        eligible += 1
        if t["app"] == "webhook" and not t["secret"]:
            t["secret"] = _ensure_sign_secret(conn, pid, idx, t)
        res = send_notice_target(conn, t, line, project_name)
        _record_health(conn, pid, t["fp"], t["app"], res)
        if res.get("ok"):
            sent += 1
    if sent or not eligible:
        with store.write(conn):
            conn.execute(
                "INSERT OR IGNORE INTO notice_outbox (project, ev_id,"
                " sent_at) VALUES (?, ?, ?)", (pid, ev_id, store.now()))
    return sent


def sweep_unsent(conn, limit: int = 25) -> int:
    """Retry key moments that never made it out (all targets failed, or the
    process died mid-post) — the outbox is the journal: a NOTICE_TYPES row
    without an outbox entry is re-posted here (the dispatcher's ticker calls
    this; each call is bounded)."""
    marks = ",".join("?" * len(NOTICE_TYPES))
    rows = conn.execute(
        f"SELECT seq, project, ev_id, type, subject, data FROM log WHERE"
        f" project IS NOT NULL AND type IN ({marks}) AND NOT EXISTS"
        f" (SELECT 1 FROM notice_outbox o WHERE o.project = log.project"
        f" AND o.ev_id = log.ev_id) ORDER BY seq LIMIT ?",
        (*NOTICE_TYPES, limit)).fetchall()
    n = 0
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except Exception:                            # noqa: BLE001
            data = {}
        if not notable(r["type"], data):
            with store.write(conn):                  # A green test.ran: mark
                conn.execute(                        # It done, never retry
                    "INSERT OR IGNORE INTO notice_outbox (project, ev_id,"
                    " sent_at) VALUES (?, ?, ?)",
                    (r["project"], r["ev_id"], store.now()))
            continue
        pro = conn.execute("SELECT name FROM projects WHERE id = ?",
                           (r["project"],)).fetchone()
        n += notice_once(conn, r["project"], r["ev_id"], r["type"], data,
                         r["subject"], pro["name"] if pro else "?")
    return n


def notices_status(handler, match, body) -> None:
    """Fingerprints, never URLs."""
    from .teamserver.api_people import _actor, _db
    from .teamserver.perms import Forbidden, require
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(actor, "team.manage")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    pronames = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM projects WHERE deleted_at IS NULL")}
    rows = [{
        "project": pronames.get(r["project"], str(r["project"])),
        "target": r["target"], "app": r["app"], "ok": bool(r["ok"]),
        "error": r["error"], "at": r["at"]}
        for r in conn.execute(
            "SELECT * FROM notice_health ORDER BY at DESC LIMIT 100")]
    handler._json(200, {"targets": rows})


# ------------------------------------------------ the chat contract
class ChatLink:
    """A linked room: data wiring + a poll cursor (/25.3).

    The full link layer lives in teamserver/chatlinks.py; this class stays here
    for import compatibility.
    """

    def __init__(self, link: dict):
        self.name = str(link.get("name") or "linked")
        self.app = str(link.get("app") or "webhook")
        self.endpoint = str(link.get("endpoint") or "")
        self.token = str(link.get("token") or "")
        self.cursor: str | None = None

    def post(self, text: str) -> bool:
        body = json.dumps({"text": text, "channel": self.name}).encode()
        req = urllib.request.Request(self.endpoint, data=body,
                                     headers={"Content-Type":
                                              "application/json",
                                              "Authorization":
                                              f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= r.status < 300
        except Exception:                             # noqa: BLE001
            return False

    def read_since(self, cursor: str | None):
        """(messages, next_cursor) — the fake chat app implements this;
        a real one is wired as data (teams_wiring-style)."""
        return [], cursor

    def channels(self) -> list[str]:
        return [self.name]

    def whoami(self) -> str:
        return f"{self.app}:{self.name}"


def ingest(conn, pid: int, link_name: str, message: dict) -> bool:
    """Compatibility shim — the real ingest lives in teamserver.chatlinks
    (sentinel-authored, deduped by origin id, membership-checked)."""
    from .teamserver import chatlinks
    return chatlinks.ingest(conn, pid, link_name, message)

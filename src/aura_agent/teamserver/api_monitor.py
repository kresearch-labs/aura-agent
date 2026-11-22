"""teamserver/api_monitor.py — monitoring export (plan).

GET metrics — Prometheus text behind an admin-made key (`Authorization: Bearer
***`, only the sha256 stored in settings); counts only, off until the admin
turns it on. Signed webhooks (Standard Webhooks): subscriptions {url, types[],
projects[], secret} — key moments posted with webhook-id/timestamp/ signature
(`v1,<base64 HMAC-SHA256 of "id.timestamp.body">`), retries 1 m/5 m/30 m/2 h/6
h, failures shown for the Admin page. GET projects/<id>/export?format=jsonl|csv
— the log with the chain head (needs `export`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import store
from .api_people import _actor, _db
from .http import parse_json as _parse_json
from .perms import Forbidden, require


def _db():
    from . import teamserver_db
    return teamserver_db.open_team()


def _setting(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:                                # noqa: BLE001
        return default


def _auth_key(conn, handler) -> bool:
    """Bearer key vs the stored sha256; the metrics key is admin-made."""
    h = handler.headers.get("Authorization") or ""
    if not h.startswith("Bearer "):
        return False
    token = h[7:].strip()
    stored = _setting(conn, "metrics_key_sha256")
    if not stored:
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),
                               stored)


def metrics(handler, match, body) -> None:
    """GET /metrics — Prometheus text; 404 until the admin makes a key."""
    conn = _db()
    if not _auth_key(conn, handler):
        handler._error(404, "metrics are off (ask the admin for the key)")
        return
    use_names = bool(_setting(conn, "metrics_names"))
    pro_names = {r["id"]: (r["name"] if use_names else str(r["id"]))
                 for r in conn.execute(
                     "SELECT id, name FROM projects").fetchall()}
    lines: list[str] = []
    for r in conn.execute(
            "SELECT t.project AS p, s.type AS t2, COUNT(*) AS n FROM tasks t"
            " JOIN statuses s ON s.project = t.project AND s.key = t.status"
            " WHERE t.deleted_at IS NULL GROUP BY t.project, s.type"):
        lines.append('aura_tasks{project="%s",status="%s"} %d' %
                     (pro_names.get(r["p"], "?"), r["t2"], r["n"]))
    for r in conn.execute(
            "SELECT type, COUNT(*) AS n FROM log WHERE"
            " source LIKE 'device:%' GROUP BY type"):
        lines.append('aura_reports_total{type="%s"} %d' %
                     (r["type"], r["n"]))
    failed = conn.execute(
        "SELECT COUNT(*) FROM log WHERE type = 'test.ran' AND"
        " json_extract(data, '$.failed') > 0").fetchone()[0]
    lines.append(f"aura_tests_failed_total {failed}")
    coll = conn.execute(
        "SELECT COUNT(*) FROM log WHERE type = 'overlap'").fetchone()[0]
    lines.append(f"aura_collisions_total {coll}")
    active = conn.execute(
        "SELECT COUNT(*) FROM people WHERE disabled_at IS NULL").fetchone()[0]
    lines.append(f"aura_people_active {active}")
    has_queue = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'queue'").fetchone()
    waiting = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] \
        if has_queue else 0
    lines.append(f"aura_queue_waiting {waiting}")
    has_codes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'http_codes'").fetchone()
    if has_codes:
        for r in conn.execute(
                "SELECT code, n FROM http_codes ORDER BY code"):
            lines.append('aura_http_requests_total{code="%s"} %d' %
                         (r["code"], r["n"]))
    try:
        from .teamserver_db import db_path
        import os
        lines.append(f"aura_db_bytes {os.path.getsize(db_path())}")
    except OSError:
        lines.append("aura_db_bytes 0")
    # backup age — the DIRECTORY is the truth (there is no backups table;
    # backup.py's rotation is off-limits): newest team-*.db file's mtime
    lines.append(f"aura_backup_age_seconds {backup_age_seconds()}")
    body_text = "\n".join(lines) + "\n"
    handler.send_response(200)
    handler.send_header("Content-Type",
                        "text/plain; version=0.0.4; charset=utf-8")
    handler.send_header("Content-Length", str(len(body_text)))
    handler.send_header("Aura-Protocol", "1")
    handler.end_headers()
    handler.wfile.write(body_text.encode())


def backup_age_seconds() -> int:
    """'s backup age: the newest team-*.db file's mtime in the backups directory
    (there is no backups TABLE; backup.py writes files). -1 when no backup
    exists.
    """
    from .teamserver_db import backups_dir
    import os
    newest = 0.0
    try:
        for p in backups_dir().glob("team-*.db"):
            if p.name.endswith(".json"):
                continue
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                continue
    except OSError:
        return -1
    return int(time.time() - newest) if newest else -1


def metrics_key(handler, match, body) -> None:
    """POST api/v1/metrics/key — admin makes (or replaces) the key; the plain key
    is shown ONCE, only its sha256 is stored.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(conn, actor, "team.manage")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    token = secrets.token_urlsafe(24)
    with store.write(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES"
            " ('metrics_key_sha256', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(hashlib.sha256(token.encode()).hexdigest()),))
    handler._json(200, {"key": token,
                        "note": "shown once — store it now"})


# ------------------------------------------------------- signed webhooks
def webhook_subscribe(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(conn, actor, "team.manage")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    try:
        req, _je = _parse_json(body or b"{}")
        if _je is not None:
            handler._error(400, _je)
            return
    except Exception:                                # noqa: BLE001
        handler._error(400, "bad json")
        return
    url = str(req.get("url") or "")[:300]
    types = [str(t)[:40] for t in (req.get("types") or [])][:20]
    if not url.startswith(("http://", "https://")):
        handler._error(400, "give the webhook URL")
        return
    projects = []
    for p in (req.get("projects") or [])[:50]:       # The projects
        try:                                         # Named on the card
            projects.append(int(p))                  # None = not made
        except (TypeError, ValueError):
            continue
    if not projects:
        handler._error(400, "whom does this hook listen to? name the project "
                            "ids — an all-projects hook isn't one of ours")
        return
    secret = secrets.token_urlsafe(24)
    with store.write(conn):
        cur = conn.execute(
            "INSERT INTO webhook_subs (url, types, secret, created,"
            " failures, projects) VALUES (?,?,?,?,0,?)",
            (url, json.dumps(types), "", store.now(), json.dumps(projects)))
        # The signing secret lives in the secrets table only; the subs row
        # carries its KEY, the value is revealed once below
        store.secret_put(conn, f"webhook:{cur.lastrowid}", secret)
        conn.execute("UPDATE webhook_subs SET secret = ? WHERE id = ?",
                     (f"webhook:{cur.lastrowid}", cur.lastrowid))
    handler._json(200, {"ok": True, "id": cur.lastrowid, "secret": secret,
                        "note": "verify signatures with this secret; "
                                "shown once"})


def webhook_list(handler, match, body) -> None:
    """URLs as fingerprints, never the value; the recent deliveries with their
    failures are what the Admin page shows.
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(conn, actor, "team.manage")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    subs = [{"id": r["id"],
             "url_fp": _fp(r["url"]),
             "types": json.loads(r["types"] or "[]"),
             "projects": json.loads(r["projects"] or "[]"),
             "created": r["created"],
             "failures": r["failures"]}
            for r in conn.execute(
                "SELECT * FROM webhook_subs ORDER BY id")]
    deliveries = [{
        "sub": r["url"], "url_fp": _fp(r["url"]), "type": r["type"],
        "ok": bool(r["ok"]), "at": r["at"], "wid": r["wid"] or "",
        "attempts": r["attempts"],
        "next_retry": r["next_retry"], "last_error": r["last_error"]}
        for r in conn.execute(
            "SELECT * FROM webhook_log ORDER BY at DESC LIMIT 50")]
    handler._json(200, {"subscriptions": subs, "deliveries": deliveries})


def webhook_delete(handler, match, body) -> None:
    """DELETE api/v1/webhooks/<id> — remove a subscription (team.manage, the same
    rule as subscribing). ONE write: the row, its sealed secret, and the
    pending retries of that exact URL — the "(check delete)" note; the
    append-only delivery history stays (removal never rewrites evidence).
    """
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    try:
        require(conn, actor, "team.manage")
    except Forbidden as e:
        handler._error(403, str(e))
        return
    sub_id = int(match.group(1))
    with store.write(conn):
        row = conn.execute(
            "SELECT id, url, secret FROM webhook_subs WHERE id = ?",
            (sub_id,)).fetchone()
        if row is None:
            handler._error(404, "no such webhook")
            return
        conn.execute("DELETE FROM webhook_subs WHERE id = ?", (sub_id,))
        # only the row's own marker key may drop; a legacy raw value is not
        # a key into the secret store
        if str(row["secret"]).startswith("webhook:"):
            store.secret_drop(conn, row["secret"])
        # the removed target's pending payloads never fire again — a surviving
        # same-URL subscription delivers NEW events with its own secret but
        # must not inherit the removed one's retries
        conn.execute(
            "UPDATE webhook_log SET next_retry = NULL WHERE url = ?"
            " AND next_retry IS NOT NULL", (row["url"],))
    handler._json(200, {"ok": True, "id": sub_id})


def _fp(url: str) -> str:
    return "sha256:" + hashlib.sha256(url.encode()).hexdigest()[:16]


def sign_webhook(conn, sub: dict, body: bytes,
                 wid: str | None = None) -> dict:
    """Standard Webhooks headers: v1,<base64 HMAC-SHA256 of "id.timestamp.body">.
    `wid` pins the id — a RETRY reuse it so the receiver's idempotency key
    holds (: the id makes repeats safe).
    """
    wid = wid or "msg_" + secrets.token_hex(8)
    ts = str(int(time.time()))
    # The subs row carries the KEY; the value rides server_secrets. conn=None
    # callers (the linked-rooms path) carry the key itself.
    from . import store
    secret_val = sub["secret"]
    if str(secret_val).startswith("webhook:"):
        if conn is None:
            raise ValueError("the subscription's secret needs the hub's "
                             "conn — the hub lane always has one")
        secret_val = store.secret_get(conn, secret_val)
    key = base64.b64encode(secret_val.encode())
    to_sign = f"{wid}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256)
                           .digest()).decode()
    return {"webhook-id": wid, "webhook-timestamp": ts,
            "webhook-signature": f"v1,{sig}"}


TIMESTAMP_WINDOW = 300            # Receivers allow a 5-minute window


def _verify(sub_secret: str, wid: str, ts: str, body: bytes, sig: str) -> bool:
    """Verify a Standard-Webhooks signature. Fixed: the key is
    base64(secret).encode BYTES — the old code called.digest on the base64
    bytes and crashed; and the timestamp window is now ENFORCED.
    """
    try:
        when = int(str(ts))
    except (TypeError, ValueError):
        return False
    if abs(time.time() - when) > TIMESTAMP_WINDOW:
        return False                                # A replay, too old
    if not sig or not str(sig).startswith("v1,"):
        return False
    key = base64.b64encode(str(sub_secret).encode())
    to_sign = f"{wid}.{ts}.".encode() + (body or b"")
    want = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256)
                            .digest()).decode()
    return hmac.compare_digest(f"v1,{want}", sig)


RETRY_DELAYS = [60, 300, 1800, 7200, 21600]


def _attempt(conn, sub: dict, body: bytes, wid: str | None = None):
    """One POST to one subscription → (ok, webhook-id, error-string)."""
    import urllib.request as _rq
    headers = sign_webhook(conn, sub, body, wid=wid)
    wid = headers["webhook-id"]
    try:
        req = _rq.Request(sub["url"], data=body, method="POST", headers={
            "Content-Type": "application/json", **headers})
        from .chatlinks import _OPENER
        with _OPENER.open(req, timeout=10) as resp:
            return 200 <= resp.status < 300, wid, \
                "" if resp.status < 300 else f"HTTP {resp.status}"
    except Exception as e:                           # noqa: BLE001
        return False, wid, str(e)[:160]


def webhook_deliver(conn, types, payload: dict) -> int:
    """Deliver to every subscription whose types+projects cover this event;
    journal one webhook_log row PER SUB (the old code's record was dedented out
    of the loop — one row for the last sub, and an UnboundLocalError with zero
    subs). Runs OUTSIDE any open transaction: network I/O never holds the write
    lock; the journal write is its own.
    """
    body = json.dumps(payload, ensure_ascii=False).encode()
    subs = [dict(r) for r in conn.execute(
        "SELECT * FROM webhook_subs").fetchall()]
    sent = 0
    for sub in subs:
        allowed = json.loads(sub["types"] or "[]")
        if allowed and payload.get("type") not in allowed:
            continue
        projs = json.loads(sub.get("projects") or "[]")
        # Plan: only the CHOSEN projects' events ride out — never a team-wide
        # row (project None) — regardless of how old or empty a subscription
        # row is
        ev_p = payload.get("project")
        if not projs or ev_p is None or ev_p not in projs:
            continue
        ok, wid, err = _attempt(conn, sub, body)
        sent += 1 if ok else 0
        with store.write(conn):
            conn.execute(
                "UPDATE webhook_subs SET failures = failures + ?"
                " WHERE id = ?", (0 if ok else 1, sub["id"]))
            still_there = conn.execute(
                "SELECT 1 FROM webhook_subs WHERE id = ?",
                (sub["id"],)).fetchone()
            conn.execute(
                "INSERT INTO webhook_log (url, type, ok, at, retries, wid,"
                " attempts, next_retry, last_error, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sub["url"], str(payload.get("type") or ""),
                 1 if ok else 0, store.now(),
                 json.dumps([] if ok else RETRY_DELAYS), wid,
                 0 if ok else 1,
                 None if (ok or still_there is None)
                 else store.now() + RETRY_DELAYS[0],
                 err, body.decode("utf-8", errors="replace")))
    return sent


def retry_due(conn, limit: int = 20) -> dict:
    """The back-off runner: re-deliver every journaled failure whose next_retry is
    due, SAME webhook-id (the receiver's idempotency key), honoring
    RETRY_DELAYS; a delivery that exhausts the schedule stays failed and shows
    on the Admin page. store.now is the clock — tests advance it instead of
    sleeping. The __main__ daemon calls this.
    """
    now = store.now()
    due = conn.execute(
        "SELECT rowid, * FROM webhook_log WHERE ok = 0 AND next_retry IS NOT"
        " NULL AND next_retry <= ? AND attempts <= ? ORDER BY at LIMIT ?",
        (now, len(RETRY_DELAYS), limit)).fetchall()
    out = {"tried": 0, "ok": 0, "failed": 0, "exhausted": 0}
    for row in due:
        r = dict(row)
        sub_row = conn.execute(
            "SELECT * FROM webhook_subs WHERE url = ? ORDER BY id",
            (r["url"],)).fetchone()
        if sub_row is None:                          # Unsubscribed: stop
            with store.write(conn):
                conn.execute(
                    "UPDATE webhook_log SET next_retry = NULL WHERE rowid"
                    " = ?", (row["rowid"],))
            continue
        ok, wid, err = _attempt(conn, dict(sub_row),
                                (r["payload"] or "").encode(), wid=r["wid"])
        attempts = int(r["attempts"]) + (0 if ok else 1)
        out["tried"] += 1
        if ok:
            out["ok"] += 1
            sql_args = ("UPDATE webhook_log SET ok = 1, retries = '[]',"
                        " next_retry = NULL, last_error = '' WHERE rowid"
                        " = ?", (row["rowid"],))
        elif attempts > len(RETRY_DELAYS):
            out["exhausted"] += 1
            sql_args = ("UPDATE webhook_log SET attempts = ?, last_error ="
                        " ?, next_retry = NULL WHERE rowid = ?",
                        (attempts, err, row["rowid"]))
        else:
            out["failed"] += 1
            sql_args = ("UPDATE webhook_log SET attempts = ?, last_error ="
                        " ?, next_retry = ? WHERE rowid = ?",
                        # attempts N failed → the next wait is DELAYS[N-1]:
                        # 60 s after the first failure, 5 m after the second…
                        (attempts, err, now + RETRY_DELAYS[attempts - 1],
                         row["rowid"]))
        with store.write(conn):
            if (not ok and attempts <= len(RETRY_DELAYS) and conn.execute(
                    "SELECT 1 FROM webhook_subs WHERE id = ?",
                    (sub_row["id"],)).fetchone() is None):
                sql_args = ("UPDATE webhook_log SET attempts = ?,"
                            " last_error = ?, next_retry = NULL WHERE rowid"
                            " = ?", (attempts, err, row["rowid"]))
            conn.execute(*sql_args)
    return out


def _export_bundle(rows, pid, head_row, conn, actor, fmt_line) -> bytes:
    """The self-verifying bundle (part 7): numbered plain-text files that sort
    themselves top-down in any file manager. 00-README carries the
    four-sentence chain story + the bounds of the check (verbatim the spec's
    DOES-NOT-prove list); 10-verify.py re-implements the chain check with the
    stdlib only; 20-log.jsonl carries the FULL row set
    (source/ev_id/prev_hash/hash included — everything canonical covers);
    30-log.csv ships the same flattened (data keys as columns, no JSON blobs);
    40-head + 50-sha256sums close the copy-check loop.
    """
    import hashlib, io, json as _json, csv, zipfile
    from datetime import datetime, timezone

    def canon(r) -> str:
        return _json.dumps(
            {"p": r["project"], "s": r["source"], "i": r["ev_id"],
             "t": r["type"], "sub": r["subject"], "a": r["actor"],
             "time": round(float(r["time"] or 0), 3),
             "d": r["data_obj"]}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)

    for r in rows:
        r["data_obj"] = _json.loads(r.get("data") or "{}")
    people = {}
    for name, pid2 in conn.execute("SELECT name, id FROM people"):
        people[pid2] = name
    head_seq = head_row["seq"] if head_row else 0
    head_hash = head_row["hash"] if head_row else "-"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    readme = f"""Aura Teams log export — {today}
=====================================

This folder is an append-only log: every row carries the hash of the row
before it, so changing/deleting/re-ordering any row breaks every later
hash. Running the check re-computes every hash and compares from the
very first row.

To verify, run in this folder:
    python 10-verify.py
It prints OK (+ the event count and head) or BROKEN (and the first bad
seq). Stdlib only; a python install is enough, no pip.

What the check PROVES: the rows you hold are unchanged-and-ordered on a
hub since the first row you have.
What the check does NOT prove: that the events were TRUE; that a hub did
not get restored whole from an older copy (that is what an off-site
head copy is for — compare 40-head.txt against one taken earlier);
any claim about tone, blame, or release fitness.

Scope of this export: {fmt_line}
Head at export time: seq {head_seq}, hash starts {head_hash[:16]}
Exported by: {actor.get('name', '?')} (the log itself records this export
as an 'export.made' event).
"""

    import pathlib
    verify_py = (pathlib.Path(__file__).with_name("export_verify.py")
                 .read_text(encoding="utf-8"))

    jlines = []
    for r in rows:
        # the HASHED fields keep their raw ids (canonical() hashes the
        # actor's id); a human label rides alongside, never in the hash
        jlines.append(_json.dumps(
            {"seq": r["seq"], "project": r["project"], "source": r["source"],
             "ev_id": r["ev_id"], "type": r["type"], "subject": r["subject"],
             "actor": r["actor"], "by": people.get(r["actor"], "?"),
             "time": r["time"],
             "data": r["data"] if isinstance(r["data"], str)
                     else _json.dumps(r["data_obj"], sort_keys=True,
                                      ensure_ascii=False,
                                      separators=(",", ":")),
             "prev_hash": r["prev_hash"], "hash": r["hash"]},
            ensure_ascii=False))
    jsonl = "\n".join(jlines) + ("\n" if jlines else "")

    # CSV: flatten the union of seen data keys (≤24 cols; the rare overflow
    # lands in .extra — never a whole-object JSON column)
    key_union: list = []
    seen = set()
    for r in rows:
        for k in sorted((r["data_obj"] or {}).keys()) if isinstance(r["data_obj"], dict) else []:
            if k not in seen and len(key_union) < 24:
                seen.add(k); key_union.append(k)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "type", "subject", "actor", "time", *key_union,
                "prev_hash", "hash", ".extra"])
    for r in rows:
        d = r["data_obj"] if isinstance(r["data_obj"], dict) else {}
        rest = {k: v for k, v in d.items() if k not in seen}
        w.writerow([r["seq"], r["type"], r["subject"],
                    people.get(r["actor"], r["actor"]),
                    datetime.fromtimestamp(r["time"] or 0, timezone.utc)
                          .strftime("%Y-%m-%d %H:%M:%S UTC"),
                    *[d.get(k, "") for k in key_union],
                    r["prev_hash"], r["hash"],
                    _json.dumps(rest, ensure_ascii=False, sort_keys=True)
                      if rest else ""])
    csv_txt = "\ufeff" + buf.getvalue()      # BOM for Excel

    head_txt = (f"chain: p{pid}\nseq: {head_seq}\nhash: {head_hash}\n"
                f"exported-at: {today}\nexported-by: {actor.get('name', '?')}\n")

    parts = [("00-README.txt", readme), ("10-verify.py", verify_py),
             ("20-log.jsonl", jsonl), ("30-log.csv", csv_txt),
             ("40-head.txt", head_txt)]
    sums = "\n".join(f"{hashlib.sha256(c.encode()).hexdigest()}  {n}"
                     for n, c in parts) + "\n"
    parts.append(("50-sha256sums.txt", sums))

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in parts:
            z.writestr(name, content)
    return out.getvalue()


def export_log(handler, match, body) -> None:
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    pid = int(match.group(1))
    if actor.get("role") in ("owner", "admin"):
        ok = conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND deleted_at IS NULL",
            (pid,)).fetchone()
    else:
        ok = conn.execute(
            "SELECT 1 FROM project_members WHERE project = ? AND person = ?",
            (pid, actor.get("person_id"))).fetchone()
    if not ok:
        handler._error(404, "no such project")
        return
    try:
        require(conn, actor, "export_activity", pid)
    except Forbidden as e:
        handler._error(403, str(e))
        return
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(handler.path).query)
    fmt = (q.get("format") or ["jsonl"])[0]
    head_row = conn.execute(
        "SELECT seq, hash FROM log_heads WHERE chain = ?",
        (f"p{pid}",)).fetchone()
    if fmt == "bundle":
        # Part-7: the verify-capable pack + the witnessed export
        full = [dict(r) for r in conn.execute(
            "SELECT seq, project, source, ev_id, type, subject, actor,"
            " time, data, prev_hash, hash FROM log WHERE project = ?"
            " ORDER BY seq", (pid,)).fetchall()]
        proj_name = conn.execute("SELECT name FROM projects WHERE id = ?",
                                 (pid,)).fetchone()
        name = (proj_name["name"] if proj_name else f"project-{pid}")
        zbytes = _export_bundle(full, pid, head_row, conn, actor,
                                "the full project chain")
        from datetime import datetime as _dt, timezone as _tz
        fname = (f"{str(name).lower().replace(' ', '-')}-log-"
                 f"{_dt.now(_tz.utc).strftime('%Y-%m-%d')}.zip")
        # The export is itself evidence (export.made, once)
        from . import store as _store, log as _logmod
        with _store.write(conn):
            _logmod.append(conn, pid, *_logmod.server_event(), "export.made",
                           "log export for the project",
                           actor["person_id"],
                           {"rows": len(full), "fmt": "bundle",
                            "head": (head_row["hash"] if head_row else "")[:12]})
        handler.send_response(200)
        handler.send_header("Content-Type", "application/zip")
        handler.send_header("Content-Disposition",
                            f'attachment; filename="{fname}"')
        handler.send_header("X-Aura-Chain-Head",
                            f"{head_row['seq'] if head_row else 0},"
                            f"{(head_row['hash'] if head_row else '-')[:16]}")
        handler.send_header("Content-Length", str(len(zbytes)))
        handler.end_headers()
        handler.wfile.write(zbytes)
        return
    # the legacy single-file formats stay for old callers
    rows = [dict(r) for r in conn.execute(
        "SELECT seq, type, subject, data, time, actor FROM log WHERE"
        " project = ? ORDER BY seq", (pid,)).fetchall()]
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["seq", "type", "subject", "data", "time"])
        for r in rows:
            w.writerow([r["seq"], r["type"], r["subject"], r["data"],
                        r["time"]])
        out = buf.getvalue().encode()
        ctype = "text/csv"
    else:
        out = "\n".join(json.dumps(r, ensure_ascii=False, default=str)
                        for r in rows).encode()
        ctype = "application/x-ndjson"
    head_row = conn.execute(
        "SELECT seq, hash FROM log_heads WHERE chain = ?",
        (f"p{pid}",)).fetchone()
    head_line = (f"# chain head: seq={head_row['seq'] if head_row else 0} "
                 f"hash={head_row['hash'] if head_row else '-'}\n").encode()
    handler.send_response(200)
    handler.send_header("Content-Type", f"{ctype}; charset=utf-8")
    handler.send_header("Content-Disposition",
                        f'attachment; filename="project-{pid}-log.{fmt}"')
    handler.send_header("X-Aura-Chain-Head",
                        f"{head_row['seq'] if head_row else 0},"
                        f"{head_row['hash'] if head_row else '-'}")
    handler.send_header("Content-Length", str(len(out) + len(head_line)))
    handler.end_headers()
    handler.wfile.write(head_line + out)


def http_note(code: int) -> None:
    """Count one HTTP response code for /metrics (best-effort)."""
    conn = _db()
    try:
        with store.write(conn):
            conn.execute(
                "INSERT INTO http_codes (code, n) VALUES (?, 1)"
                " ON CONFLICT(code) DO UPDATE SET n = n + 1", (code,))
    except Exception:                                # noqa: BLE001
        pass

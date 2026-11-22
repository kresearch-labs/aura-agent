"""teamserver/auth.py — request signatures, sessions (plan). The invite/code world
was replaced by accounts (TEAMS).

The server check order is exactly 's: device exists → not revoked → person not
disabled → ±60 s → nonce unseen → signature valid; then last_seen. Any failure
is `401 not_signed_in` with the reason. The nonce table purges rows older than
10 minutes on every check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import store

NONCE_TTL = 600          # 10 minutes
CREATED_WINDOW = 60      # ±60 s
SESSION_DAYS = 30

SIGN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def signed_text(method: str, path: str, host: str, created: str, nonce: str,
                body_hash: str) -> bytes:
    """The exact text the signature covers — client and server build it
    identically or nothing works.
    """
    return "\n".join([
        "aura-sig-v1", method, path, host, created, nonce, body_hash,
    ]).encode("utf-8")


def body_hash_of(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def verify(conn, handler, body: bytes) -> dict | None:
    """Check a signed request. Returns the actor
    ({person_id, name, role, device_id}) or None (the handler already
    sent the 401)."""
    device_id = handler.headers.get("Aura-Device") or ""
    created = handler.headers.get("Aura-Created") or ""
    nonce = handler.headers.get("Aura-Nonce") or ""
    sig_b64 = handler.headers.get("Aura-Signature") or ""
    if not (device_id and created and nonce and sig_b64):
        handler._error(401, "no device signature on this request")
        return None
    row = conn.execute(
        "SELECT d.*, p.name AS person_name, p.role AS person_role,"
        " p.team_role AS person_team_role,"
        " p.global_role AS person_global_role,"
        " p.disabled_at AS person_disabled"
        " FROM devices d JOIN people p ON p.id = d.person WHERE d.id = ?",
        (device_id,)).fetchone()
    if row is None:
        handler._error(401, "unknown device — join the team first")
        return None
    if row["revoked_at"]:
        handler._error(401, "this device was removed — join again with a new invite")
        return None
    if row["person_disabled"]:
        handler._error(401, "this person is disabled — ask an admin")
        return None
    try:
        created_i = int(created)
    except ValueError:
        handler._error(401, "the request's timestamp is not a number")
        return None
    if abs(store_now() - created_i) > 60:
        handler._error(401, "the request's timestamp is too far off "
                            "(check this machine's clock)")
        return None
    # The signature proves this request first — the nonce (and the mark in the
    # nonces table) is claimed ONLY after it, AND the verify's miss reports one
    # text shape, ever.
    try:
        if len(nonce) != 32 or not all(c in "0123456789abcdef"
                                       for c in nonce):
            raise ValueError("nonce shape")
        from nacl.exceptions import BadSignatureError
        import nacl.encoding
        import nacl.signing
        vk = nacl.signing.VerifyKey(bytes(row["pubkey"]))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        vk.verify(text_of(handler, body), sig)      # BadSignatureError or binascii
    except (ValueError, TypeError) as _exc:
        handler._error(401, "the request's signature does not verify")
        return None
    except BadSignatureError as _exc:
        handler._error(401, "the request's signature does not verify")
        return None
    except Exception:                            # noqa: BLE001
        handler._error(401, "the request's signature does not verify")
        return None
    with store_write(conn):
        purge = store_now() - NONCE_TTL
        conn.execute("DELETE FROM nonces WHERE seen < ?", (purge,))
        seen = conn.execute("SELECT 1 FROM nonces WHERE nonce = ?",
                            (nonce,)).fetchone()
        if seen:
            handler._error(401, "this request was already received "
                                "(a replay?)")
            return None
        conn.execute("INSERT INTO nonces (nonce, device, seen) VALUES (?,?,?)",
                     (nonce, device_id, store_now()))
    with store_write(conn):                      # Every write is BEGIN IMMEDIATE
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?",
                     (store_now(), device_id))
    return {"person_id": row["person"], "name": row["person_name"],
            "role": row["person_role"], "team_role": row["person_team_role"],
            "global_role": row["person_global_role"], "device_id": device_id,
            "projects": projects_of(conn, row["person"])}


def projects_of(conn, person_id: int) -> dict:
    """{project_id: role} for the permission check."""
    return {r["project"]: r["role"] for r in conn.execute(
        "SELECT project, role FROM project_members WHERE person = ?",
        (person_id,)).fetchall()}


def text_of(handler, body: bytes) -> bytes:
    """The signed text, rebuilt server-side."""
    return signed_text(handler.command, handler.path,
                       handler.headers.get("Host") or "",
                       handler.headers.get("Aura-Created") or "",
                       handler.headers.get("Aura-Nonce") or "",
                       body_hash_of(body))


def store_now() -> float:
    from . import store
    return store.now()


def store_write(conn):
    from . import store
    return store.write(conn)


# ------------------------------------------------------------- sessions --
def new_session(conn, person_id: int, approved_by: str) -> str:
    """A browser session: only its SHA-256 is stored;: last_used ticks from now;
    an `ended` sign-out marker, swept after 30 days.
    """
    token = secrets.token_urlsafe(32)
    with store_write(conn):
        conn.execute(
            "INSERT INTO sessions (token_hash, person, approved_by, created,"
            " expires, last_used) VALUES (?,?,?,?,?,?)",
            (hashlib.sha256(token.encode()).digest(), person_id,
             approved_by, store_now(),
             store_now() + SESSION_DAYS * 86400, store_now()))
    return token


def session_person(conn, token: str | None) -> dict | None:
    """The person behind a session cookie, or None. A session lives while its
    last_used is fresh (: signed in until 7 days unused; 'ended' for sign-out)
    and its person is an enabled one.
    """
    if not token:
        return None
    now = store_now()
    row = conn.execute(
        " SELECT s.token_hash, s.expires, s.revoked_at, s.ended, s.last_used,"
        " s.created, p.id, p.name, p.role, p.team_role, p.global_role,"
        " p.must_change, p.disabled_at FROM sessions s"
        " JOIN people p ON p.id = s.person WHERE s.token_hash = ?",
        (hashlib.sha256(token.encode()).digest(),)).fetchone()
    if row is None or row["revoked_at"] or row["ended"] or row["expires"] < now:
        return None
    fresh = row["last_used"] or row["created"]      # Pre- rows age by created
    if fresh is None or fresh < now - 7 * 86400:    # 7 days unused → signed out
        return None
    if row["disabled_at"]:
        return None
    # The touch is a write on a read path: at most once a minute
    if row["last_used"] is None or row["last_used"] < now - 60:
        with store_write(conn):
            conn.execute("UPDATE sessions SET last_used = ?"
                         " WHERE token_hash = ?",
                         (now, row["token_hash"]))
    # The permission check reads person["projects"] — a session actor carries
    # the same memberships a device-signed one does
    return {"person_id": row["id"], "name": row["name"], "role": row["role"],
            "team_role": row["team_role"], "global_role": row["global_role"],
            "must_change": bool(row["must_change"]) if "must_change" in row.keys()
            else bool(0),
            "projects": projects_of(conn, row["id"])}


def session_by_token(conn, token: str | None):
    """The raw session row for sign-out / revocation lists."""
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?",
        (hashlib.sha256(token.encode()).digest(),)).fetchone()



"""teamserver/sessions.py — the session's lifecycle: the cookie is
`__Host-aura_team` (Secure, HttpOnly, SameSite=Strict); a session dies after 7
days unused (last_used ticks on each request it carries); ended sessions are
swept once they have been ended 30 days. No session row is ever "trusted" by
shape.
"""
from __future__ import annotations

COOKIE = "__Host-aura_team"
UNUSED_DAYS = 7            # Signed in until 7 days unused
ENDED_KEEP_DAYS = 30       # Ended sessions deleted after 30 days

COOKIE_ATTRS = ("Secure; HttpOnly; SameSite=Strict; Path=/")


def set_cookie(token: str) -> dict:
    """The Set-Cookie header value for a fresh session (Secure+HttpOnly+
    Strict, no Domain — the `__Host-` prefix's contract); rides a route's
    `_json(..., extra=...)`."""
    return {"Set-Cookie": f"{COOKIE}={token}; {COOKIE_ATTRS}"}


def clear_cookie() -> dict:
    """The header that discards the cookie now (sign-out)."""
    return {"Set-Cookie": f"{COOKIE}=; {COOKIE_ATTRS}; Max-Age=0"}


def touch(conn, token_hash: bytes, now: float) -> None:
    """last_used ticks on the request that carried the session."""
    conn.execute("UPDATE sessions SET last_used = ? WHERE token_hash = ?",
                 (now, token_hash))


def staleness(now_unused_before: float) -> float:
    """The row-stamp a session must be fresher than to still be live."""
    return now_unused_before                    # Clarity at call sites


def end(conn, token_hash: bytes, now: float) -> None:
    conn.execute("UPDATE sessions SET ended = ? WHERE token_hash = ?",
                 (now, token_hash))


def sweep(conn, now: float) -> int:
    """Ended rows leave the table after 30 days, and IDLE-for-30-days rows too.
    Returns rows removed.
    """
    cur = conn.execute(
        "DELETE FROM sessions WHERE (ended IS NOT NULL AND ended < ?)"
        " OR (last_used IS NOT NULL AND last_used < ?)",
        (now - ENDED_KEEP_DAYS * 86400, now - ENDED_KEEP_DAYS * 86400))
    return cur.rowcount

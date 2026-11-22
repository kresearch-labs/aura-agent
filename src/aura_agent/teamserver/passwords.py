"""teamserver/passwords.py — the password's only shape: scrypt from Python's
library (N=2^17, r=8, p=1, 16-byte salt), stored as `scrypt$17$8$1$<salt
b64url>$<hash b64url>`; at most two hashes at a time (the current one plus the
one being written during a change — never a history); the password itself is
8–64 characters and never lands in a log, a chat file or an event (only its
hash ever touches the store).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P = 2 ** 17, 8, 1
SALT_BYTES = 16
MIN_LEN, MAX_LEN = 8, 64                # The doc's rule; 7 and 65 refuse

# The 128 MiB per check is the — and exactly what makes a password flood a hub
# memory bomb: at most two scrypt runs in flight on the hub, everyone else
# waits its turn rather than hold memory
import threading as _thread
_the_scrypt_gate = _thread.Semaphore(2)


def validate(pw: str) -> str | None:
    """None when the password may be used, else the refusal in plain words."""
    if not isinstance(pw, str):
        return "the password is text"
    if not (MIN_LEN <= len(pw) <= MAX_LEN):
        return f"the password is {MIN_LEN} to {MAX_LEN} characters"
    return None


def hash_password(pw: str) -> str:
    """One stored string for one password — a fresh salt per call."""
    salt = secrets.token_bytes(SALT_BYTES)
    # N=2^17,r=8,p=1 needs ~128 MiB — NEVER more than two in flight
    # (a burst of sign-in answers drains itself, not the hub's memory)
    with _the_scrypt_gate:
        h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=N, r=R, p=P,
                           maxmem=256 * 1024 * 1024)
    return ("scrypt$%d$%d$%d$%s$%s" % (
        17, R, P,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(h).decode().rstrip("=")))


def verify(stored: str | None, pw: str) -> bool:
    """Slow compare, never a traceback about formats: a malformed stored
    string counts as a failed check, nothing else."""
    if not stored or not isinstance(stored, str):
        return False
    try:
        tag, n2, r2, p2, sb, hb = stored.split("$", 5)
        if tag != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(sb + "=" * (-len(sb) % 4))
        want = base64.urlsafe_b64decode(hb + "=" * (-len(hb) % 4))
        with _the_scrypt_gate:
            got = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                                 n=2 ** int(n2), r=int(r2), p=int(p2),
                                 maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError, KeyError):
        return False
    return hmac.compare_digest(got, want)

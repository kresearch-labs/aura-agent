"""site_vault.py — saved site logins the model never sees.

Lessons kept here: fill only on the site the login belongs to (browser-use
CVE-2025-47241: "allowed.com@evil.com" passed a string check — the real host is
parsed); the model sees labels only, never browses the vault (1Password agent
mode); ask before each fill; values never in tool output.

Stored in Aura's key store (OS keychain, or the AES vault), one entry per site
+ user. Added only by the user (`aura vault add`, the password typed at a
hidden prompt — never an argument, never in shell history).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
import urllib.parse

_PREFIX = "login:"
# values filled this session: scrubbed from anything the browser returns
_FILLED: set[str] = set()


def _key(site: str, user: str) -> str:
    return f"{_PREFIX}{site.lower().strip()}:{user}"


def normal_site(site: str) -> str:
    """'https://GitHub.com/login' or 'github.com' → 'github.com'."""
    s = (site or "").strip()
    host = urllib.parse.urlparse(s if "://" in s else "https://" + s).hostname or ""
    return host.lower().strip(".")


def add(site: str, user: str, password: str, totp: str | None = None) -> bool:
    from . import keystore
    site = normal_site(site)
    if not site or not user or not password:
        raise ValueError("site, user and password are needed")
    rec = {"user": user, "password": password}
    if totp:
        base64.b32decode(totp.replace(" ", "").upper() + "=" * (-len(totp.replace(" ", "")) % 8))
        rec["totp"] = totp.replace(" ", "").upper()
    return keystore.put(_key(site, user), json.dumps(rec))


def labels() -> list[dict]:
    """What the model may see: site and user name — never a password."""
    from . import keystore
    out = []
    for n in keystore.names():
        if n.startswith(_PREFIX):
            site, _, user = n[len(_PREFIX):].partition(":")
            out.append({"site": site, "user": user})
    return out


def remove(site: str, user: str | None = None) -> int:
    from . import keystore
    site = normal_site(site)
    gone = 0
    for lb in labels():
        if lb["site"] == site and (user is None or lb["user"] == user):
            keystore.remove(_key(site, lb["user"]))
            gone += 1
    return gone


def matches(page_url: str, site: str) -> bool:
    """The page's REAL host is the site or one of its subdomains. Parsed,
    so 'https://github.com@evil.test/' is evil.test (the browser-use CVE)."""
    host = (urllib.parse.urlparse(page_url or "").hostname or "").lower().strip(".")
    site = normal_site(site)
    return bool(host and site and (host == site or host.endswith("." + site)))


def entry_for(page_url: str, site: str | None = None, user: str | None = None) -> dict | None:
    """The saved login for this page (optionally a given site / user)."""
    from . import keystore
    for lb in labels():
        if site and lb["site"] != normal_site(site):
            continue
        if user and lb["user"] != user:
            continue
        if matches(page_url, lb["site"]):
            raw = keystore.get(_key(lb["site"], lb["user"]))
            if raw:
                rec = json.loads(raw)
                rec["site"] = lb["site"]
                return rec
    return None


def totp_now(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 one-time code (SHA-1, 30 s, 6 digits — what sites use)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def remember_filled(value: str) -> None:
    if value:
        _FILLED.add(value)


def scrub(text: str) -> str:
    """Anything the browser returns: filled values replaced."""
    for v in _FILLED:
        if v and v in (text or ""):
            text = text.replace(v, "[saved login value]")
    return text


def used() -> bool:
    return bool(_FILLED)


def forget_filled() -> None:
    _FILLED.clear()

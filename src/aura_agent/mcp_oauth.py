"""mcp_oauth.py — sign in to a remote MCP server in the browser.

The browser comes back to a one-time listener on 127.0.0.1. Tokens live in the
profile's key store; a refresh token renews them. Standard library only.

Started only by the user (`aura mcp login <name>`) — never by the model.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

_UA = {"User-Agent": "aura-agent (MCP client)", "Accept": "application/json"}
_KEY = "mcp-oauth:"


class OAuthError(Exception):
    pass


def _get_json(url: str, timeout: float = 15.0) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post_form(url: str, data: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={**_UA, "Content-Type": "application/x-www-form-urlencoded"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise OAuthError(f"{url} -> HTTP {e.code}: {(e.read() or b'')[:200]!r}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OAuthError(f"{url} -> {e}") from e


def _well_known(base: str, name: str) -> list[str]:
    """RFC 8414 / 9728 places: with the path inserted, then at the root."""
    u = urllib.parse.urlparse(base)
    root = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")
    out = [f"{root}/.well-known/{name}{path}"] if path else []
    return out + [f"{root}/.well-known/{name}"]


def discover(server_url: str) -> dict:
    """{'resource', 'issuer', 'authorization_endpoint', 'token_endpoint',
    'registration_endpoint'?, 'scopes'?} for a remote MCP server."""
    prm_url = None
    req = urllib.request.Request(server_url, method="POST", data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "aura-agent", "version": __version__}}}).encode(),
        headers={**_UA, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
        raise OAuthError("this server didn't ask for a sign-in")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise OAuthError(f"the server answered HTTP {e.code}") from e
        hdr = e.headers.get("WWW-Authenticate") or ""
        m = __import__("re").search(r'resource_metadata="([^"]+)"', hdr)
        prm_url = m.group(1) if m else None
    except (urllib.error.URLError, OSError) as e:
        raise OAuthError(f"can't reach {server_url}: {e}") from e
    prm = None
    for u in ([prm_url] if prm_url else []) + _well_known(server_url, "oauth-protected-resource"):
        prm = _get_json(u)
        if prm:
            break
    issuers = (prm or {}).get("authorization_servers") or []
    if not issuers:
        o = urllib.parse.urlparse(server_url)
        issuers = [f"{o.scheme}://{o.netloc}"]       # Older servers: the same host
    meta = None
    for u in _well_known(issuers[0], "oauth-authorization-server") + \
            _well_known(issuers[0], "openid-configuration"):
        meta = _get_json(u)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
        meta = None
    if not meta:
        raise OAuthError("no sign-in details found for this server")
    if "S256" not in (meta.get("code_challenge_methods_supported") or ["S256"]):
        raise OAuthError("the sign-in server doesn't offer PKCE S256 — Aura won't sign in without it")
    return {"resource": (prm or {}).get("resource") or server_url,
            "issuer": meta.get("issuer") or issuers[0],
            "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"],
            "registration_endpoint": meta.get("registration_endpoint"),
            "scopes": (prm or {}).get("scopes_supported") or []}


def _client(meta: dict) -> dict | None:
    """The registration kept for this sign-in server: {client_id, port}."""
    from . import keystore
    raw = keystore.get(f"{_KEY}client:{meta['issuer']}")
    try:
        rec = json.loads(raw) if raw else None
    except ValueError:
        rec = {"client_id": raw, "port": 0}          # An older plain value
    return rec if isinstance(rec, dict) and rec.get("client_id") else None


def _register(meta: dict, redirect_uri: str, port: int) -> str:
    """A client id for Aura at this sign-in server. Kept with its callback PORT:
    research (2026) — many servers match the redirect exactly, so a new random
    port each time is refused; the same port is reused next time.
    """
    from . import keystore
    key = f"{_KEY}client:{meta['issuer']}"
    if not meta.get("registration_endpoint"):
        raise OAuthError("this sign-in server needs a client id Aura doesn't have "
                         "(no dynamic registration) — use a token from the service instead")
    req = urllib.request.Request(meta["registration_endpoint"], method="POST",
                                 headers={**_UA, "Content-Type": "application/json"},
                                 data=json.dumps({
                                     "client_name": "Aura (local agent)",
                                     "redirect_uris": [redirect_uri],
                                     "grant_types": ["authorization_code", "refresh_token"],
                                     "response_types": ["code"],
                                     "token_endpoint_auth_method": "none"}).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cid = json.loads(r.read().decode("utf-8", "replace")).get("client_id")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OAuthError(f"registering Aura failed: {e}") from e
    if not cid:
        raise OAuthError("the sign-in server gave no client id")
    keystore.put(key, json.dumps({"client_id": cid, "port": port}))
    return cid


def login(server_url: str, open_browser=None, wait: float = 300.0, say=print,
          paste=None) -> dict:
    """The browser sign-in. Returns the saved token record. paste: asks the
    user for the address the browser ended on — for a machine the browser
    can't call back to (SSH, a container, WSL with another browser; research
    2026: loopback callbacks fail there)."""
    import webbrowser
    meta = discover(server_url)
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got
            body = ("<h3>Signed in — you can close this tab and go back to Aura.</h3>" if ok
                    else "<h3>Sign-in did not finish — go back to Aura.</h3>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    known = _client(meta)
    srv = None
    if known and known.get("port"):
        try:                                    # The registered callback port, again
            srv = http.server.HTTPServer(("127.0.0.1", int(known["port"])), H)
        except OSError:
            known = None                        # Busy: register again on a free port
    if srv is None:
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        known = None
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    client_id = known["client_id"] if known else _register(meta, redirect, port)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "resource": meta["resource"]}
    if meta["scopes"]:
        params["scope"] = " ".join(meta["scopes"])
    url = meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") \
        + urllib.parse.urlencode(params)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    srv.timeout = wait
    t.start()
    say(f"opening the sign-in page in your browser (if it doesn't open, go to):\n  {url}")
    (open_browser or webbrowser.open)(url)
    t.join(wait + 5)
    srv.server_close()
    if "code" not in got and paste is not None:
        back = (paste("paste the address your browser ended on (it starts with "
                      f"{redirect}): ") or "").strip()
        got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(back).query).items()})
    if got.get("state") != state:
        raise OAuthError("the sign-in answer didn't match (state) — not used")
    if "code" not in got:
        raise OAuthError(got.get("error_description") or got.get("error") or "no sign-in within the time")
    tok = _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier, "resource": meta["resource"]})
    return _save(server_url, tok, meta, client_id)


def _save(server_url: str, tok: dict, meta: dict, client_id: str) -> dict:
    from . import keystore
    if not tok.get("access_token"):
        raise OAuthError(f"the sign-in server gave no token: {str(tok)[:160]}")
    rec = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
           "expires_at": time.time() + float(tok.get("expires_in") or 3600) - 60,
           "token_endpoint": meta["token_endpoint"], "client_id": client_id,
           "resource": meta["resource"]}
    keystore.put(_KEY + server_url, json.dumps(rec))
    return rec


def token_for(server_url: str) -> str | None:
    """A valid access token from an earlier sign-in (renewed with the
    refresh token when it ran out), or None."""
    from . import keystore
    raw = keystore.get(_KEY + server_url)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    if time.time() < float(rec.get("expires_at") or 0):
        return rec.get("access_token")
    if not rec.get("refresh_token"):
        return None
    try:
        tok = _post_form(rec["token_endpoint"], {
            "grant_type": "refresh_token", "refresh_token": rec["refresh_token"],
            "client_id": rec["client_id"], "resource": rec.get("resource") or server_url})
    except OAuthError:
        return None
    tok.setdefault("refresh_token", rec["refresh_token"])
    return _save(server_url, tok, {"token_endpoint": rec["token_endpoint"],
                                   "resource": rec.get("resource") or server_url},
                 rec["client_id"])["access_token"]


def logout(server_url: str) -> None:
    from . import keystore
    keystore.remove(_KEY + server_url)

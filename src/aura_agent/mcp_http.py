"""mcp_http.py — MCP over HTTP with OAuth2.

Extends the MCP client family with a Streamable-HTTP transport + OAuth2:
  - bearer-token support (static, from env/config)
  - OAuth2 client-credentials flow (token_url + client_id/secret)
  - a browser sign-in (OAuth 2.1 + PKCE) for servers that need the user's
    consent: mcp_oauth.py, started by `aura mcp login` or `/mcp login`

Token cache: the profile's key store (keystore.py), keyed by
token_url+client_id. Protocol: JSON-RPC over HTTP POST (initialize / tools/list
/ tools/call), SSE responses accepted by reading the `data:` lines.

Wired: an `mcp_servers` entry with "url" (instead of "command") loads as a
RemoteMCPServer (mcp_client.load_servers_from_config).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__


class MCPHTTPError(Exception):
    pass


_TOKEN = "mcp-token:"          # Key-store names: mcp-token:<url>:<client>


def _token_cache_path() -> Path:
    """The old plain token file — only read to move its logins into the
    profile's key store, then removed."""
    from .paths import profile_path
    return profile_path("mcp_tokens.json")


def _move_old_tokens() -> None:
    p = _token_cache_path()
    if not p.exists():
        return
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    from .keystore import put
    try:
        ok = all(put(_TOKEN + k, json.dumps(v)) for k, v in old.items())
    except Exception:                              # noqa: BLE001
        ok = False
    if ok:
        p.unlink(missing_ok=True)


def _load_tokens() -> dict:
    """MCP logins live in the profile's key store."""
    _move_old_tokens()
    from .keystore import get, names
    out = {}
    for n in names():
        if n.startswith(_TOKEN):
            try:
                out[n[len(_TOKEN):]] = json.loads(get(n) or "")
            except ValueError:
                continue
    return out


def _save_tokens(tokens: dict) -> None:
    from .keystore import put
    for k, v in tokens.items():
        put(_TOKEN + k, json.dumps(v))


def _cached_token(token_url: str, client_id: str) -> str | None:
    entry = _load_tokens().get(f"{token_url}::{client_id}")
    if entry and entry.get("expires_at", 0) > time.time() + 30:
        return entry["access_token"]
    return None


def client_credentials_token(token_url: str, client_id: str,
                             client_secret: str,
                             scope: str = "") -> str:
    """OAuth2 client-credentials grant; caches the token."""
    cached = _cached_token(token_url, client_id)
    if cached:
        return cached
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id, "client_secret": client_secret,
        **({"scope": scope} if scope else {}),
    }).encode()
    req = urllib.request.Request(token_url, data=data, method="POST",
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if "access_token" not in body:
        raise MCPHTTPError(f"token endpoint returned no access_token: {body}")
    ttl = int(body.get("expires_in", 3600))
    tokens = _load_tokens()
    tokens[f"{token_url}::{client_id}"] = dict(
        access_token=body["access_token"],
        expires_at=time.time() + ttl)
    _save_tokens(tokens)
    return body["access_token"]


class MCPHTTPClient:
    """JSON-RPC over HTTP POST to an MCP streamable-HTTP endpoint. Works with both
    spec generations, like the stdio client.
    """

    def __init__(self, url: str, token: str | None = None,
                 timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._next_id = 1
        self.session_id: str | None = None
        self.protocol: str | None = None

    def _call(self, method: str, params: dict | None = None) -> dict:
        """One request → the JSON-RPC `result`; a JSON-RPC error raises."""
        rid = self._next_id
        self._next_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": rid,
                              "method": method,
                              **({"params": params} if params else {})})
        # Python's default User-Agent is blocked by Cloudflare-fronted servers
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": f"aura-agent/{__version__} (MCP client)"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        req = urllib.request.Request(self.url, data=payload.encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                sid = r.headers.get("Mcp-Session-Id")
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise MCPHTTPError(f"{method} -> HTTP {e.code}: "
                               f"{(e.read() or b'')[:200]!r}") from e
        except (urllib.error.URLError, OSError) as e:
            raise MCPHTTPError(f"{method} -> {e}") from e
        if sid:
            self.session_id = sid
        msg = None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    # keep-alives and partial lines are not JSON — skip
                    # them instead of failing the whole call
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    break
            if msg is None:
                raise MCPHTTPError("SSE response carried no data line")
        else:
            try:
                msg = json.loads(raw)
            except ValueError as e:
                raise MCPHTTPError(f"{method} -> not JSON: {raw[:120]!r}") \
                    from e
        if isinstance(msg, dict) and msg.get("error"):
            err = msg["error"]
            raise MCPHTTPError(f"{method}: "
                               f"{err.get('message', err) if isinstance(err, dict) else err}")
        return (msg or {}).get("result") or {}

    def initialize(self, protocol: str = "2025-11-25") -> dict:
        res = self._call("initialize", {
            "protocolVersion": protocol,
            "capabilities": {},
            "clientInfo": {"name": "aura-agent", "version": __version__},
        })
        self.protocol = res.get("protocolVersion") or protocol
        try:
            self._call("notifications/initialized")
        except MCPHTTPError:
            pass
        return res


class RemoteMCPServer:
    """A remote MCP server, shaped like mcp_client.MCPServer (name, allow,
    tools, list_tools, call_tool, stop) so mcp_tools registers it exactly
    the same way — same naming, grading by the server's hints, untrusted
    descriptions, approvals. Credentials come from the environment
    (config uses "${VAR}"): a static bearer token, or OAuth2
    client-credentials. Every failure surfaces as mcp_client.MCPError, so
    a dead remote server contributes zero tools instead of crashing."""

    def __init__(self, name: str, url: str, token: str | None = None,
                 oauth: dict | None = None, allow: set[str] | None = None,
                 timeout: float = 30.0):
        self.name, self.url, self.allow = name, url, allow
        self.token, self.oauth, self.timeout = token, oauth, timeout
        self.tools: list[dict] = []
        self._client: MCPHTTPClient | None = None

    def _conn(self) -> MCPHTTPClient:
        from .mcp_client import MCPError
        if self._client is not None:
            return self._client
        if not self.url.startswith(("https://", "http://")):
            raise MCPError(f"mcp '{self.name}': url must be http(s)")
        token = self.token
        try:
            if not token and self.oauth:
                o = self.oauth
                token = client_credentials_token(
                    o.get("token_url", ""), o.get("client_id", ""),
                    o.get("client_secret", ""), o.get("scope", ""))
            if not token:
                # a browser sign-in done earlier (aura mcp login <name>)
                from .mcp_oauth import token_for
                token = token_for(self.url)
            c = MCPHTTPClient(self.url, token, self.timeout)
        except (MCPHTTPError, OSError, ValueError) as e:
            raise MCPError(f"mcp '{self.name}': login failed: {e}") from e
        try:
            c.initialize()            # Older servers; stateless ones refuse
        except MCPHTTPError:
            pass
        self._client = c
        return c

    def _rpc(self, method: str, params: dict | None) -> dict:
        from .mcp_client import MCPError, _with_meta
        try:
            return self._conn()._call(method, _with_meta(params))
        except MCPHTTPError as e:
            hint = (f" — sign in: aura mcp login {self.name}"
                    if "HTTP 401" in str(e) or "HTTP 403" in str(e) else "")
            raise MCPError(f"mcp '{self.name}': {e}{hint}") from e

    def list_tools(self) -> list[dict]:
        from .mcp_client import normalize_tools
        self.tools = normalize_tools(self._rpc("tools/list", None),
                                     self.allow, self.name)
        return self.tools

    def call_tool(self, name: str, arguments: dict) -> str:
        from .mcp_client import MCPError, content_text
        if self.allow and name not in self.allow:
            raise MCPError(f"mcp '{self.name}': tool '{name}' not allowed")
        return content_text(self._rpc("tools/call", {
            "name": name, "arguments": arguments}), self.name, name)

    def stop(self) -> None:
        self._client = None

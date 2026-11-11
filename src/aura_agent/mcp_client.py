"""mcp_client.py — MCP client, pure stdlib (JSON-RPC 2.0 over stdio).

Spawn the server process, write JSON-RPC requests to its stdin, read responses
from its stdout. Every call is wrapped in a hard timeout and a crash shield — a
dead MCP server degrades to an error string like any other tool failure, never
a wedged loop.

Config (profile/config `mcp_servers` block — env-vars-only credentials):
    mcp_servers:
      fetch:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-fetch"]
        env: {FETCH_CACHE: /tmp/fetch} # values may reference ${ENV_VARS}
        allow: [fetch, ...] # optional per-server allowlist
      docs: # a REMOTE server (mcp_http.py)
        url: https://example.com/mcp
        token: ${DOCS_MCP_TOKEN} # or oauth: {token_url, client_id,
                                           # client_secret: ${...}, scope}
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field

from . import __version__


class MCPError(Exception):
    pass


_LEGACY_PROTOCOL = "2025-11-25"      # For servers that still need initialize
_CURRENT_PROTOCOL = "2026-07-28"     # Stateless: version travels in _meta
_CLIENT_INFO = {"name": "aura-agent", "version": __version__}


def _with_meta(params: dict | None) -> dict:
    """Stateless servers read protocol version + client info from _meta on
    every request (_meta is reserved by MCP, so older servers ignore it)."""
    p = dict(params or {})
    p.setdefault("_meta", {"protocolVersion": _CURRENT_PROTOCOL,
                           "clientInfo": _CLIENT_INFO})
    return p


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    allow: set[str] | None = None       # Tool allowlist (None = all)
    timeout: float = 30.0
    # runtime
    _proc: subprocess.Popen | None = None
    _job: object = None                 # Windows: the job its process tree lives in
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _next_id: int = 1
    tools: list[dict] = field(default_factory=list)   # [{name, description
    #                                      inputSchema, blast_radius}]

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        env = {**os.environ, **self.env}
        try:
            # its own process group / job: a server started through a launcher
            # (npx, uvx, a shell script) is several processes, and stopping it
            # must stop all of them
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1,
                **({} if os.name == "nt" else {"start_new_session": True}))
            if os.name == "nt":
                from .sandbox_os import _job_for
                self._job = _job_for(self._proc.pid)
        except FileNotFoundError as e:
            raise MCPError(f"mcp '{self.name}': cannot spawn "
                           f"'{self.command}': {e}") from e
        self._handshake()

    def _handshake(self) -> None:
        """Try it once and ignore a refusal, so both work."""
        try:
            self._rpc("initialize", {
                "protocolVersion": _LEGACY_PROTOCOL,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO})
        except MCPError:
            return                      # Stateless server: nothing to do
        try:
            self._proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"})
                + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, AttributeError):
            pass

    def stop(self) -> None:
        """The server and everything it started."""
        p = self._proc
        if p is not None:
            if os.name == "nt":
                from .sandbox_os import _close_job
                if self._job is not None:
                    _close_job(self._job)       # The whole tree ends with the job
                    self._job = None
                elif p.poll() is None:
                    p.kill()
            else:
                from .sandbox_os import _stop_group
                _stop_group(p)
            try:
                p.wait(timeout=3)
            except Exception:
                pass
        self._proc = None

    # -------------------------------------------------------------- protocol
    def _rpc(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC request/response with timeout + crash isolation."""
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        assert self._proc is not None
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            req = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params:
                req["params"] = params
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.stop()
                raise MCPError(f"mcp '{self.name}': pipe broken: {e}") from e
            return self._read_response(rid)

    def _read_line_with_timeout(self, timeout: float) -> str:
        """Read one line from stdout with a hard timeout (reader thread)."""
        result: dict = {}

        def reader():
            try:
                result["line"] = self._proc.stdout.readline()
            except Exception as e:                     # noqa: BLE001
                result["err"] = str(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise MCPError(f"mcp '{self.name}': response timeout "
                           f"({timeout}s) on '{self.name}'")
        if "err" in result:
            raise MCPError(f"mcp '{self.name}': read error: {result['err']}")
        return result.get("line", "")

    def _read_response(self, rid: int) -> dict:
        deadline_lines = 64                     # Skip notifications, bounded
        for _ in range(deadline_lines):
            line = self._read_line_with_timeout(self.timeout).strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                        # Not JSON — skip (banners)
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"mcp '{self.name}': "
                                   f"{err.get('message', err)}")
                return msg.get("result") or {}
        raise MCPError(f"mcp '{self.name}': no response for id {rid}")

    # ----------------------------------------------------------------- tools
    def list_tools(self) -> list[dict]:
        """tools/list → normalized [{name, description, schema, blast}].
        Blast radius: READ by default; escalated only via profile grant."""
        self.tools = normalize_tools(self._rpc("tools/list", _with_meta(None)),
                                     self.allow, self.name)
        return self.tools

    def call_tool(self, name: str, arguments: dict) -> str:
        """tools/call → content text. Errors raise MCPError (loop surfaces
        them like any tool error; the reviewer can see the traceback)."""
        if self.allow and name not in self.allow:
            raise MCPError(f"mcp '{self.name}': tool '{name}' not allowed")
        return content_text(self._rpc("tools/call", _with_meta(
            {"name": name, "arguments": arguments})), self.name, name)


# -------------------------------------- shared by stdio and HTTP servers ----
def normalize_tools(res: dict, allow: set[str] | None, server: str
                    ) -> list[dict]:
    """A tools/list result → [{name, description, schema, annotations,
    server}], filtered by the per-server allowlist."""
    out = []
    for t in res.get("tools") or []:
        name = t.get("name", "")
        if allow and name not in allow:
            continue
        out.append({"name": name,
                    "description": t.get("description", ""),
                    "schema": t.get("inputSchema") or {},
                    # ReadOnlyHint / destructiveHint decide grading
                    "annotations": t.get("annotations") or {},
                    "server": server})
    return out


def content_text(res: dict, server: str, tool: str) -> str:
    """A tools/call result → text; a tool-reported error raises MCPError."""
    if res.get("isError"):
        content = res.get("content") or []
        text = "; ".join(c.get("text", "") for c in content
                         if isinstance(c, dict))
        raise MCPError(f"mcp '{server}' {tool}: {text or 'tool error'}")
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif isinstance(c, dict) and "data" in c:
            parts.append(f"[binary content: {len(c.get('data', ''))} b64]")
    return "\n".join(parts) or "(empty result)"


def _expand_env(v):
    """'${NAME}' → the environment variable (credentials never live in
    config files); anything else unchanged."""
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        return os.environ.get(v[2:-1], "")
    return v


# ------------------------------------------------------------ registry ------
def load_servers_from_config(cfg: dict) -> list:
    """Parse the `mcp_servers` config block. Never raises on a bad entry —
    bad servers are skipped (doctor reports them). An entry with "url"
    (and no "command") is a REMOTE server over HTTP (mcp_http.py)."""
    servers: list = []
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(spec, dict):
            continue
        allow = spec.get("allow")
        if spec.get("url") and not spec.get("command"):
            from .mcp_http import RemoteMCPServer
            oauth = spec.get("oauth")
            servers.append(RemoteMCPServer(
                name=str(name), url=str(spec["url"]),
                token=_expand_env(spec.get("token")) or None,
                oauth=({k: _expand_env(v) for k, v in oauth.items()}
                       if isinstance(oauth, dict) else None),
                allow=set(allow) if allow else None,
                timeout=float(spec.get("timeout", 30.0))))
            continue
        if not spec.get("command"):
            continue
        env = {str(k): str(_expand_env(v))
               for k, v in (spec.get("env") or {}).items()}
        servers.append(MCPServer(
            name=str(name), command=str(spec["command"]),
            args=[str(a) for a in spec.get("args", [])], env=env,
            allow=set(allow) if allow else None,
            timeout=float(spec.get("timeout", 30.0))))
    return servers


def expand_tools_for_mediator(servers: list[MCPServer]
                              ) -> list[tuple[MCPServer, dict]]:
    """Flatten all servers' tools for registration. A dead server yields
    zero tools (skip), never an exception."""
    pairs: list[tuple[MCPServer, dict]] = []
    for s in servers:
        try:
            for t in s.list_tools():
                pairs.append((s, t))
        except MCPError:
            continue
    return pairs

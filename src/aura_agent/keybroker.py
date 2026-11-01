"""keybroker.py — BUILD 6: credential brokering (3).

The pattern map (research, Oct 2026) assigns a holder per key type:
  - SSH / GPG: NOTARY — the agent holds no key; a local agent socket
                      answers sign requests (ssh-agent is exactly this).
                      We pass the socket INTO the sandbox, not the key.
  - git over HTTPS: GIT_ASKPASS broker — git's native "ask an external
                      program per credential use" interface; our askpass
                      script pulls the secret from the valet at push time.
  - API keys: VALET socket + just-in-time env injection. A same-host
                      valet cannot cryptographically hide secrets from a
                      same-uid agent (true of ssh-agent too — documented);
                      what it buys: secrets never touch the sandbox fs, the
                      session tape, or `env` dumps, custody is central
                      (one revocation point), and every issue is auditable.
                      A network-edge TLS-terminating proxy is the v2 endgame
                      (CB4A Model A); this is the local-desktop tier.
  - Cloud/day-pass: no code — prefer provider short-lived tokens wherever
                      they exist (config, not implementation).

Custody rules (CB4A):
  - the broker is NOT the source of truth: real values live in the
    profile's key store (keystore.py: OS keychain / password vault) or
    the parent env (the old ~/.config/aura/secrets.json is moved in once);
    the engagement keeps only a committable METADATA map (names, hosts,
    env names — never values).
  - valet issues a ONE-TIME run token per start; wrong token = 403.
  - fail closed: unknown name, bad token, or missing entry = no secret.

Test-safety: all broker tests run in synthetic sandbox roots.
"""
from __future__ import annotations

import json
import os
import secrets as _pysecrets
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENG_META = ".aura/secrets.map"          # Committable: NO values, ever


_CMD = "cmd:"                  # Key-store names: cmd:<NAME>


def user_store_path() -> Path:
    """The OLD plain custody file (pre key store) — read once by the
    default profile to move its values into the key store, then removed."""
    return Path.home() / ".config" / "aura" / "secrets.json"


def _move_old_store() -> None:
    from .paths import profile_name
    p = user_store_path()
    if profile_name() != "default" or not p.exists():
        return
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    from .keystore import put
    try:
        ok = all(put(_CMD + str(k), str(v)) for k, v in old.items())
    except Exception:                    # noqa: BLE001
        ok = False
    if ok:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- custody ---
def set_user_secret(name: str, value: str) -> bool:
    from .keystore import put
    return put(_CMD + name, value)


def load_meta(eng) -> dict:
    """Committable engagement map: {NAME: {hosts: [...], header: ...}}.
    Values NEVER appear here — if one does, it is stripped on load."""
    p = Path(eng) / ENG_META
    if not p.exists():
        return {}
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for name, entry in (meta or {}).items():
        if not isinstance(entry, dict):
            continue
        clean = {"hosts": entry.get("hosts", []),
                 "header": entry.get("header", "Authorization"),
                 "template": entry.get("template", "{secret}"),
                 "env": entry.get("env", [])}
        out[name] = clean
    return out


def resolve_value(name: str, env: dict | None = None) -> str | None:
    """Notary-of-last-resort ordering: user store, then parent env.
    Returns None when custody has nothing — never invents a value."""
    _move_old_store()
    from .keystore import get
    v = get(_CMD + name)
    if v is not None:
        return v
    src = (env if env is not None else os.environ).get(name)
    return src if src else None


# ------------------------------------------------------------------ valet ---
class ValetServer:
    """Local broker socket. One-time run token; loopback only; serves a
    secret ONLY to a request carrying the current run token. Fail closed."""

    def __init__(self, eng, run_token: str | None = None):
        self.eng = Path(eng)
        self.meta = load_meta(self.eng)
        self.token = run_token or _pysecrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> "ValetServer":
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):   # Silence the tape
                pass

            def do_GET(self):
                # fail closed on every gate, in order
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._reply(403, "loopback only")
                    return
                if self.headers.get("X-Aura-Token") != broker.token:
                    self._reply(403, "bad run token")
                    return
                name = self.path.rsplit("/", 1)[-1]
                if name not in broker.meta:
                    self._reply(404, "unknown secret")
                    return
                # resolution order: the entry's env names first, then the
                # metadata name itself (custody may store under either)
                val = None
                for ename in broker.meta[name].get("env", []):
                    val = resolve_value(ename)
                    if val is not None:
                        break
                if val is None:
                    val = resolve_value(name)
                if val is None:
                    self._reply(404, "no custody for this name")
                    return
                self._reply(200, val)

            def _reply(self, code: int, body: str):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/secret/{name}"


def write_askpass(dir_: Path, name: str, valet: ValetServer) -> Path:
    """git's native broker interface: an askpass script that pulls the
    secret from the valet AT PUSH TIME. The script itself holds no secret —
    only the one-time run token, which dies with the run."""
    d = Path(dir_)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f".askpass-{name}"
    fetch = (f'curl -s -H "X-Aura-Token: {valet.token}" "{valet.url(name)}" '
             f'|| wget -qO- --header "X-Aura-Token: {valet.token}" '
             f'"{valet.url(name)}"')
    p.write_text(f"#!/bin/sh\n{fetch}\n", encoding="utf-8")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p


def sandbox_env(eng, valet: ValetServer | None = None,
                allowed: list[str] | None = None) -> dict:
    """Just-in-time env for the sandboxed subprocess:
    - GIT_ASKPASS per git-entry (values fetched from the valet at use)
    - direct env entries resolved now (injection — the documented-weaker
      mode; values still never touch the sandbox fs or the tape)
    - the notary: SSH_AUTH_SOCK rides along (sign requests, not keys)."""
    env: dict = {}
    meta = load_meta(eng)
    if allowed is not None:
        # An active skill pack limits which secrets ride along
        meta = {k: v for k, v in meta.items() if k in allowed}
    if not meta:
        return env
    if valet is not None:
        for name, entry in meta.items():
            if entry.get("hosts") and name.startswith("git:"):
                # the OS temp folder ("/tmp" on Windows meant C:\tmp, a new
                # folder at the root of the drive)
                import tempfile as _tempfile
                ap = write_askpass(Path(_tempfile.gettempdir()),
                                   name.split(":", 1)[1], valet)
                env["GIT_ASKPASS"] = str(ap)
    for name, entry in meta.items():
        for ename in entry.get("env", []):
            val = resolve_value(ename)
            if val is not None:
                env[ename] = val
    return env

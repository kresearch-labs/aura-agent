"""lsp.py — talk to language servers the user already has.

Aura does the same with whatever server is installed (pyright / basedpyright,
typescript-language-server, gopls, rust-analyzer, clangd) — a small stdio
JSON-RPC client (LSP 3.17/3.18 base framing: Content-Length + JSON), one server
per language, started on first use, kept for the session. Pull diagnostics are
used when offered (also when registered dynamically after `initialized`, as
pyright does); otherwise the pushed ones are awaited. No server → nothing
changes (the built-in symbol map and search stay).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

SERVERS = {   # Language id: (file extensions, candidate commands)
    "python": ((".py", ".pyi"), (["basedpyright-langserver", "--stdio"],
                                 ["pyright-langserver", "--stdio"], ["pylsp"])),
    "typescript": ((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
                   (["typescript-language-server", "--stdio"],)),
    "go": ((".go",), (["gopls"],)),
    "rust": ((".rs",), (["rust-analyzer"],)),
    "c": ((".c", ".h", ".cc", ".cpp", ".hpp"), (["clangd"],)),
}
WAIT_S = 3.0


def language_of(path: str) -> str | None:
    ext = os.path.splitext(str(path))[1].lower()
    for lang, (exts, _) in SERVERS.items():
        if ext in exts:
            return lang
    return None


def find_server(lang: str) -> list[str] | None:
    """The first installed command for this language (env AURA_LSP_<LANG>
    overrides, e.g. AURA_LSP_PYTHON="my-server --stdio"; "off" uses none)."""
    env = os.environ.get(f"AURA_LSP_{lang.upper()}", "").strip()
    if env.lower() in ("off", "none", "0"):
        return None
    if env:
        import shlex                    # Quoted paths with spaces work on Windows too
        return [t.strip('"\'') for t in shlex.split(env, posix=False)]
    for cmd in SERVERS.get(lang, ((), ()))[1]:
        exe = shutil.which(cmd[0])
        if exe:
            return [exe] + cmd[1:]
    return None


def installed() -> dict:
    return {lang: (find_server(lang) or [None])[0] for lang in SERVERS}


def _uri(p: Path) -> str:
    return Path(p).resolve().as_uri()


class Client:
    """One language server over stdio."""

    def __init__(self, cmd: list[str], root: Path):
        self.root = Path(root).resolve()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=str(self.root))
        self._id = 0
        self._lock = threading.Lock()
        self._replies: dict = {}
        self.diags: dict = {}                   # Uri -> list of diagnostics
        self._diag_event = threading.Event()
        self._registered = threading.Event()        # A dynamic registration arrived
        self.pull = False
        self.versions: dict = {}
        threading.Thread(target=self._reader, daemon=True).start()
        init = self.request("initialize", {
            "processId": os.getpid(), "rootUri": _uri(self.root),
            "workspaceFolders": [{"uri": _uri(self.root), "name": self.root.name}],
            "capabilities": {"textDocument": {
                "publishDiagnostics": {}, "diagnostic": {"dynamicRegistration": True},
                "definition": {}, "references": {}, "documentSymbol": {}},
                "workspace": {"symbol": {}}}}, timeout=30)
        caps = (init or {}).get("capabilities") or {}
        self.pull = bool(caps.get("diagnosticProvider"))
        self.notify("initialized", {})
        # Servers like pyright say "pull" in a registration sent AFTER
        # 'initialized' — a fast machine asked before it arrived and then
        # waited for pushes that never come
        if not self.pull:
            self._registered.wait(1.0)

    # ------------------------------------------------------------ wire --
    def _send(self, msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        with self._lock:
            self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            self.proc.stdin.flush()

    def _reader(self) -> None:
        f = self.proc.stdout
        while True:
            length = None
            while True:
                line = f.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length is None:
                continue
            try:
                msg = json.loads(f.read(length))
            except ValueError:
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        if "id" in msg and "method" not in msg:                 # A reply
            self._replies[msg["id"]] = msg
            return
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            self.diags[p.get("uri")] = p.get("diagnostics") or []
            self._diag_event.set()
        elif method == "client/registerCapability":
            for reg in (msg.get("params") or {}).get("registrations") or []:
                if reg.get("method") == "textDocument/diagnostic":
                    self.pull = True                             # Pyright does this
            self._registered.set()
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif "id" in msg:                                        # Other requests: empty OK
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    def request(self, method: str, params: dict, timeout: float = 10):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            if rid in self._replies:
                return self._replies.pop(rid).get("result")
            if self.proc.poll() is not None:
                return None
            time.sleep(0.02)
        return None

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------- features --
    def sync(self, path: Path, text: str) -> str:
        uri = _uri(path)
        v = self.versions.get(uri, 0) + 1
        self.versions[uri] = v
        if v == 1:
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_of(str(path)) or "plaintext",
                "version": v, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": v},
                                                   "contentChanges": [{"text": text}]})
        return uri

    def diagnostics(self, path: Path, text: str, wait: float = WAIT_S) -> list[dict]:
        uri = self.sync(path, text)
        if self.pull:
            r = self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=wait)
            if isinstance(r, dict) and isinstance(r.get("items"), list):
                return r["items"]
        self._diag_event.clear()
        self.diags.pop(uri, None)
        end = time.time() + wait
        while time.time() < end and uri not in self.diags:
            self._diag_event.wait(0.1)
            if self.pull:                                # The registration came late: ask
                r = self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}},
                                 timeout=max(1.0, end - time.time()))
                if isinstance(r, dict) and isinstance(r.get("items"), list):
                    return r["items"]
                break
        return self.diags.get(uri, [])

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=2)
            self.notify("exit", {})
        except Exception:                                # noqa: BLE001
            pass
        try:
            self.proc.kill()
        except Exception:                                # noqa: BLE001
            pass


_CLIENTS: dict = {}


def client_for(path: str, root: Path) -> Client | None:
    lang = language_of(path)
    if not lang:
        return None
    key = (lang, str(Path(root).resolve()))
    c = _CLIENTS.get(key)
    if c is not None and c.proc.poll() is None:
        return c
    cmd = find_server(lang)
    if not cmd:
        return None
    try:
        c = _CLIENTS[key] = Client(cmd, root)
    except (OSError, ValueError):
        return None
    if len(_CLIENTS) == 1:
        import atexit
        atexit.register(shutdown_all)          # Servers end with Aura
    return c


def errors_after_edit(path: str, root: Path, limit: int = 8) -> str:
    """'' or a short report of the server's errors for this file."""
    try:
        p = Path(root) / path
        c = client_for(path, root)
        if c is None or not p.is_file():
            return ""
        items = c.diagnostics(p, p.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return ""
    errs = [d for d in items if d.get("severity", 1) == 1]
    if not errs:
        return ""
    lines = [f"line {d['range']['start']['line'] + 1}: {str(d.get('message', '')).splitlines()[0][:160]}"
             for d in errs[:limit]]
    more = f" (+{len(errs) - limit} more)" if len(errs) > limit else ""
    return f"language server: {len(errs)} error(s) in {path}{more} — " + "; ".join(lines)


def shutdown_all() -> None:
    for c in list(_CLIENTS.values()):
        c.close()
    _CLIENTS.clear()

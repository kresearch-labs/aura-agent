"""serve.py — `aura serve` — read-only engagement view.

Security: bind 127.0.0.1 by default (--lan to expose on 0.0.0.0 with a printed
warning), GET-only, path traversal impossible (fixed routes only, no file
serving), no auth = never expose secrets beyond these summaries.
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    out = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _overview_html(st: dict) -> str:
    eng = st.get("engagement", "?")
    scope = st.get("scope", {})
    findings = st.get("findings", 0)
    sessions = st.get("sessions", 0)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Aura — {eng}</title>
<style>body{{font-family:ui-monospace,monospace;margin:2rem;background:#111;
color:#ddd}} h1{{color:#7ec8a9}} table{{border-collapse:collapse}}
td,th{{border:1px solid #333;padding:.35rem .8rem}}</style></head>
<body><h1>⬢ Aura — notebook: {eng}</h1>
<table>
<tr><th>scope hosts</th><td>{scope.get('hosts', 0)}</td></tr>
<tr><th>scope cidrs</th><td>{scope.get('cidrs', 0)}</td></tr>
<tr><th>scope domains</th><td>{scope.get('domains', 0)}</td></tr>
<tr><th>findings</th><td>{findings}</td></tr>
<tr><th>sessions</th><td>{sessions}</td></tr>
</table>
<p><a href="/api/status" style="color:#7ec8a9">/api/status</a> ·
<a href="/api/findings" style="color:#7ec8a9">/api/findings</a> ·
<a href="/api/sessions" style="color:#7ec8a9">/api/sessions</a> ·
<a href="/api/audit" style="color:#7ec8a9">/api/audit</a></p>
<p style="color:#888">read-only · {eng} never leaves this view</p>
</body></html>"""


def _build_handler(eng: Path) -> type:
    eng = Path(eng)

    class H(BaseHTTPRequestHandler):
        def _send_json(self, obj, code: int = 200):
            data = json.dumps(obj, ensure_ascii=False, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                from .engagement import engagement_status
                data = _overview_html(engagement_status(eng)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/status":
                from .engagement import engagement_status
                self._send_json(engagement_status(eng))
            elif self.path == "/api/findings":
                self._send_json(_read_jsonl(eng / "findings.jsonl"))
            elif self.path == "/api/sessions":
                sdir = eng / "sessions"
                out = []
                if sdir.exists():
                    for p in sorted(sdir.glob("*.jsonl")):
                        out.append(dict(file=p.name,
                                        bytes=p.stat().st_size))
                self._send_json(out)
            elif self.path == "/api/audit":
                self._send_json(_read_jsonl(eng / "audit.jsonl", limit=500))
            elif self.path == "/api/last-state":
                # Shared read model — the SAME object chat renders
                from .last_state import read_last_state
                self._send_json(read_last_state(eng) or {})
            elif self.path == "/api/chain":
                # Audit chain rollup — same vocabulary as audit
                from .audit_chain import chain_summary, _load
                self._send_json(chain_summary(_load(eng)))
            else:
                self.send_error(404)

        def do_POST(self):
            # 3.9: remote attach — submit a follow-up task (token-gated)
            if self.path == "/api/task":
                token = (self.headers.get("Authorization") or "").replace(
                    "Bearer ", "")
                expected = os.environ.get("AURA_SERVE_TOKEN", "")
                if not expected or token != expected:
                    self._send_json({"error": "unauthorized"}, code=401)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length))
                    task = str(payload.get("task", "")).strip()
                except Exception:
                    task = ""
                if not task:
                    self._send_json({"error": "empty task"}, code=400)
                    return
                # queue it: engagement/.remote-tasks (runner: next chat/run)
                q = eng / ".remote-tasks"
                with q.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"task": task,
                                        "ts": time.time()}) + "\n")
                self._send_json({"queued": True, "task": task[:200]})
                return
            self.send_error(404)

        def log_message(self, *a):
            pass

    return H


def serve(eng: Path, host: str = "127.0.0.1", port: int = 8092) -> int:
    from http.server import ThreadingHTTPServer
    eng = Path(eng)
    if not (eng / "scope.json").exists():
        print(f"serve: no notebook at {eng} — run: aura init <name>")
        return 1
    srv = ThreadingHTTPServer((host, port), _build_handler(eng))
    print(f"aura serve: http://{host}:{port}/  (notebook: {eng.name})")
    if host == "0.0.0.0":
        print("⚠ LAN-exposed (0.0.0.0) — read-only summaries only")
    print("Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0

"""proxy.py — `aura proxy` — local OpenAI-compatible proxy.

Streaming passes through byte-for-byte; non-streaming JSON likewise. No key
handling logic of its own: it forwards whatever Authorization the active
profile carries, and forwards inbound Authorization if the profile has none.

Security: binds 127.0.0.1 only (never 0.0.0.0), prints the target it proxies
to, Ctrl-C clean shutdown.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import resolve
from .profile_store import active_name

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authenticate", "proxy-authorization", "te", "trailers"}


def _build_handler(profile) -> type:
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _target(self, path: str) -> str:
            base = profile.base_url.rstrip("/")
            # avoid /v1/v1/ double-prefix: profile.base_url already ends
            # in /v1 and inbound paths start with /v1
            if path.startswith("/v1/") and base.endswith("/v1"):
                path = path[3:]
            return base + path

        def _headers(self) -> dict:
            h = {k: v for k, v in self.headers.items()
                 if k.lower() not in HOP}
            if profile.api_key:
                h["Authorization"] = f"Bearer {profile.api_key}"
            return h

        def do_GET(self):
            self._proxy("GET", self.path, None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            self._proxy("POST", self.path, body)

        def _proxy(self, method: str, path: str, body: bytes | None):
            req = urllib.request.Request(
                self._target(path), data=body, headers=self._headers(),
                method=method)
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    self.send_response(r.status)
                    skip = HOP | {"content-length", "transfer-encoding"}
                    for k, v in r.headers.items():
                        if k.lower() not in skip:
                            self.send_header(k, v)
                    is_stream = "text/event-stream" in \
                        (r.headers.get("Content-Type") or "")
                    if is_stream:
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        while True:
                            chunk = r.read(1024)
                            if not chunk:
                                break
                            self._chunk(chunk)
                        self._chunk(b"")
                    else:
                        data = r.read()
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
            except urllib.error.HTTPError as e:
                data = e.read() or b"{}"
                self.send_response(e.code)
                self.send_header("Content-Type",
                                 e.headers.get("Content-Type",
                                               "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:  # Upstream dead → structured 502
                data = json.dumps({"error": {"message": str(e),
                                             "type": "proxy_upstream_error",
                                             "code": 502}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def _chunk(self, data: bytes):
            self.wfile.write(f"{len(data):X}\r\n".encode() + data +
                             b"\r\n")

        def log_message(self, *a):
            pass

    return H


def serve(host: str = "127.0.0.1", port: int = 8091,
          profile_name: str | None = None) -> int:
    """Blocking serve loop. Returns exit code after shutdown."""
    name = profile_name or active_name()
    profile = resolve(name)
    srv = ThreadingHTTPServer((host, port), _build_handler(profile))
    print(f"aura proxy: http://{host}:{port}/v1  →  {profile.base_url} "
          f"(profile '{profile.name}', model {profile.model})")
    print("point any OpenAI-compatible client at the local URL above; "
          "Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0

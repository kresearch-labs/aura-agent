"""teamserver/http.py — the hardened HTTP server (plan).

Threaded, TLS-wrapped, 15 s handshake and read timeouts, HTTP/1.1, strict
limits, an anchored-regex router, a fixed static allow-list, security headers
on every response, JSON errors with fixed codes, a 10/min rate limit on
unauthenticated routes, and CR/LF checks on header values.
"""
from __future__ import annotations

import collections
import json
import re
import socket
import ssl
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import tls

PROTOCOL = 1

# ---- limits ----
MAX_REQUEST_LINE = 8 * 1024
MAX_HEADERS = 64
MAX_HEADER = 8 * 1024
MAX_JSON = 1024 * 1024
MAX_UPLOAD = 10 * 1024 * 1024
# Routes whose body the handler reads itself, in pieces, straight to disk
# (never whole in memory) — and their own limit
STREAM_ROUTES = {("POST", "/api/v1/chat/upload")}
MAX_STREAM = 200 * 1024 * 1024
METHODS = ("GET", "POST", "PATCH", "DELETE", "PUT")  # PUT: members/links

SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data:; style-src 'self'; "
     "script-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
]

ERRORS = {
    400: "bad_request", 401: "not_signed_in", 403: "forbidden",
    404: "not_found", 405: "method_not_allowed", 409: "conflict",
    411: "length_required", 413: "too_large", 415: "wrong_type",
    423: "locked",
    426: "update_aura", 429: "slow_down", 500: "server_error",
    503: "store_down",
    502: "bad_gateway",                      # KeyError never dresses as 500
}


def error_body(code: int, message: str) -> bytes:
    """`{"error": {"code": "<word>", "message": "<plain words>"}}`."""
    return json.dumps({"error": {"code": ERRORS.get(code, "server_error"),
                                 "message": message}},
                      ensure_ascii=False).encode()


def parse_json(body: bytes, max_bytes: int = MAX_JSON):
    """The one JSON parse for the hub: a pod (dict/list) of known size or a None +
    the error text for the 400. Every route that answered an opaque exception
    when the body wasn't JSON goes through this.
    """
    if len(body) > max_bytes:
        return None, "that request body is too large"
    try:
        import json as _json
        return _json.loads(body.decode("utf-8", "strict")), None
    except (ValueError, UnicodeDecodeError):
        return None, "the body isn't valid JSON"


class Handler(BaseHTTPRequestHandler):
    timeout = 15                       # Read timeout per request
    protocol_version = "HTTP/1.1"
    server_version = "aura-team"

    # the server wires these
    routes: list = []                  # (method, compiled_regex, handler, auth)
    static: dict = {}                  # "/app.js" -> "page/app.js"
    rate_limit: int = 10
    server_dir = None                  # team_dir, for server.log
    broadcaster = None
    run_id = ""                        # This process's id (hub.lock)

    # ---- responses ----
    def send_response(self, code, message=None):     # type: ignore[override]
        # The never-silent detector's metre: ANY status line means the request
        # got its answer. Not _headers_buffer length — that buffer is
        # flushed-and-zeroed by end_headers mid-answer, so the old growth check
        # wrote a phantom 500 after every good reply.
        self._responded = True
        super().send_response(code, message)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        try:
            self.send_response(status)
            for k, v in list(SECURITY_HEADERS) + list((extra or {}).items()):
                if "\r" in v or "\n" in v:          # Header check
                    raise ValueError(f"bad header value for {k}")
                self.send_header(k, v)
            self.send_header("Content-Type", content_type)
            # No-store everywhere — a cached page module kept a stale build
            # alive in the browser and looped against the fixed server (live).
            # EXCEPTION: Chromium ≥156 silently DROPS the cookies on a no-store
            # answer — a one-off Set-Cookie must pair "private, no-cache"
            # instead (cache-wise as strict; the hub stays uncached, the
            # session itself gets into the jar).
            if (extra or {}).get("Set-Cookie"):
                self.send_header("Cache-Control", "private, no-cache")
            elif not (extra or {}).get("Cache-Control"):
                # the fallback fires only when extra didn't already rule
                # (assets ship "immutable" — pairing it with no-store, as
                # this branch once did, silently defeats it)
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Aura-Protocol", str(PROTOCOL))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json", extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"code": ERRORS.get(status, "error"),
                                      "message": message}})

    def _server_error(self, exc: Exception) -> None:
        """500: plain words to the client, the trace to server.log."""
        try:
            if self.server_dir:
                log = Path(self.server_dir) / "server.log"
                with open(log, "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc() + "\n")
            else:
                traceback.print_exc()
        except OSError:
            traceback.print_exc()
        self._error(500, "something went wrong on the team hub — it's in the hub's log")

    # ---- request parsing ----
    def _check_limits(self) -> str | None:
        """Return an error message when a limit is exceeded."""
        if len(self.requestline.encode()) > MAX_REQUEST_LINE:
            return "request line too long"
        if len(self.headers) > 64:
            return "too many headers"
        for k, v in self.headers.items():
            if len(f"{k}: {v}".encode()) > MAX_HEADER:
                return f"header {k[:20]} too long"
        return None

    def _read_body(self, stream_ok: bool = False) -> bytes | None:
        """the shape is refused up the front — POST/PATCH/PUT with no
        Content-Length (or Transfer-Encoding: chunked) gets 411; a length
        that's not a non-negative number gets 400; over the caps gets 413. Only
        then is the body read.
        """
        method = self.command
        raw = self.headers.get("Content-Length")
        if method in ("POST", "PATCH", "PUT"):
            if "chunked" in (self.headers.get("Transfer-Encoding")
                             or "").lower():
                self._error(411, "send a Content-Length (not chunked)")
                return None
            if raw is None:
                self.close_connection = True
                self._error(411, "a Content-Length header is needed")
                return None
            try:
                length = int(raw)
            except (TypeError, ValueError):
                self.close_connection = True
                self._error(400, "the Content-Length isn't a number")
                return None
            if length < 0:
                self.close_connection = True
                self._error(400, "the Content-Length can't be negative")
                return None
        else:
            length = int(raw) if raw and raw.isdigit() else 0
        if stream_ok and 0 < length <= MAX_STREAM:
            # The handler reads these bytes itself, in pieces. Until it has
            # read them all, the connection can't carry another request.
            self._stream_left = length
            return b""
        cap = MAX_STREAM if stream_ok else MAX_UPLOAD
        if length > cap or (length > MAX_JSON and "json" in (
                self.headers.get("Content-Type") or "")):
            # drain a bounded amount first so the client (mid-write) gets
            # the 413 instead of a broken pipe; then close
            left = min(length, MAX_UPLOAD + 1024 * 1024)
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    left -= len(chunk)
            except (OSError, ValueError):
                pass
            self.close_connection = True
            self._error(413, "The file is over 200 MB." if stream_ok else
                        "the body is too large (JSON 1 MB, uploads "
                        "10 MB — that is the most)")
            return None
        try:
            return self.rfile.read(length) if length else b""
        except OSError:
            return None



    # ---- dispatch ----
    def _dispatch(self) -> None:
        try:
            # HTTP/1.1 handlers serve multiple requests. The gate/handler
            # share an actor only within THIS request, never a later one.
            self._actor_for_request = None
            too = self._check_limits()
            if too:
                self._error(400, too)
                return
            path = self.path.split("?", 1)[0]
            # 's team lane — before the page allow-list; api is guaranteed not
            # to land here by the caller order (routes win over statics in this
            # same method's fold — the api rule's own shape keeps them away).
            # The page's relative URLs decide./assets under the served path —
            # the canonical lane rides with a trailing slash.
            if self.command == "GET" and path == "/team":
                self.send_response(302)
                self.send_header("Location", "/team/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.command == "GET" and (path.startswith("/team/")):
                return self._team_page(path)
            if self.command == "GET" and path == "/":
                # The vanilla page is gone; the hub has one page (the team
                # page) — land there, never a bare 404
                self.send_response(302)
                self.send_header("Location", "/team/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Static files: the fixed allow-list only
            if self.command == "GET" and path in self.static:
                return self._static(path)
            # the body is read BEFORE any early error return — a body left
            # in the socket is parsed as the next request line on keep-alive
            # (that is what throttled sign-in pages to garbage — found live)
            self._stream_left = 0
            body = self._read_body(
                stream_ok=(self.command, path) in STREAM_ROUTES)
            if body is None:
                return
            for m, rx, handler, gate, perm, module, pspec in self.routes:
                if m != self.command:
                    continue
                match = rx.match(path)
                if not match:
                    continue
                # Protocol versions: a client OLDER than the server gets 426
                # update_aura on routes whose shape changed
                try:
                    client_proto = int(self.headers.get("Aura-Protocol")
                                       or PROTOCOL)
                except ValueError:
                    client_proto = PROTOCOL
                if client_proto < PROTOCOL and _changed_for(client_proto, path):
                    self._error(426, "this team server is newer than your "
                                     "Aura — update Aura (the card offers it)")
                    return
                if gate == "open":
                    if not self._rate_ok(path):
                        return
                elif not self._person_gate(body, match, perm, module, pspec):
                    return  # The gate already answered (401/403)
                # Detection: did any status line leave? (send_response sets
                # _responded; the _headers_buffer growth check before it was
                # wrong — end_headers resets that buffer mid-answer and wrote a
                # phantom 500 after every 200, seen live)
                self._responded = False
                handler(self, match, body)
                if not self._responded:
                    if self.server_dir:
                        import traceback as _tb
                        from pathlib import Path as _P
                        try:
                            with open(_P(self.server_dir) / "server.log",
                                      "a", encoding="utf-8") as f:
                                f.write(f"handler answered nothing: "
                                        f"{self.command} {self.path}\n")
                        except OSError:
                            pass
                    self._server_error(RuntimeError(
                        f"handler answered nothing for {self.command} {path}"))
                return
            if self.command not in METHODS:
                self._error(405, "this server answers GET, POST, PATCH, DELETE")
                return
            self._error(404, "no such address on this server")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                          # noqa: BLE001
            self._server_error(e)
        finally:
            # An answer sent before a streamed body was fully read (401, 403,
            # 415, 413…) — the rest of the body is still in the socket, so this
            # connection must not carry another request
            if getattr(self, "_stream_left", 0):
                self.close_connection = True
                self._stream_left = 0

    def _person_gate(self, body: bytes, match, perm, module,
                     pspec) -> bool:
        """'s gate: sign-in first (session or device — the same answer), then the
        one permission check of perms.allowed with the route's module + project
        source. Person-only routes (sign-out, the me faces, linked rooms…) just
        need a signed-in human.
        """
        conn = self._gate_connector
        if conn is None:
            return False                                    # Answered error
        from .api_people import _actor as _resolve
        import hashlib as _hl
        actor = _resolve(conn, self, body, need="any")
        # 's one resolve: the handler's own _actor re-uses this — a second
        # verify on the same request would read as a replay (the nonce is
        # stored at the first)
        self._actor_for_request = (self.command, self.path,
                                   _hl.sha256(body).hexdigest(), actor)
        if actor is None:
            return False                    # 401 inside, as always
        if perm is None:
            return True                     # Person-level route
        from . import perms
        project = self._project_of(match, body, pspec, conn)
        # A room with no project (DM, plain room, links bridge) — the tick is a
        # "may add/read anything somewhere you're a member" check, never the
        # team-face check!
        if pspec == "any" or project == "allow_any":
            ok = perms.allowed_any(conn, actor, perm, module=module)
        else:
            ok = perms.allowed(conn, actor, perm, project, module=module)
        if not ok:
            self._error(403, f"not allowed here ({perm})")
            return False
        return True

    @property
    def _gate_connector(self):
        """The dispatcher's per-request team connection (cached per request;
        the handler's own _db() opens anew later — SQLite-local, free)."""
        it = getattr(self, "__gate_db", None)
        if it is None:
            try:
                from .teamserver_db import open_team
                it = open_team()
            except Exception:                            # noqa: BLE001
                self._error(503, "the hub's store isn't answering")
                it = False
            setattr(self, "__gate_db", it)
        return it if it else None

    def _project_of(self, match, body, pspec, conn) -> int | str | None:
        """The project a route's perm-check resolves against — the path
        group, the query string, the JSON body or the task's row (a route
        check for a project that no one can name is simply refused; the
        caller's handler will answer the 404)."""
        if pspec is None or pspec == "any":
            return None
        kind, arg = pspec
        try:
            if kind == "path":
                return int(match.group(int(arg)))
            if kind == "query":
                from urllib.parse import parse_qs
                qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path
                              else "")
                if qs.get(arg):
                    return int(qs[arg][0])
                # No project named (a list across projects): allowed when the
                # person may do it in any of their projects; the handler then
                # keeps only those projects' rows
                return "allow_any" if arg == "project" else None
            if kind == "body":
                import json as _json
                try:
                    data = _json.loads(
                        body.decode("utf-8", "strict")) if body else {}
                except (ValueError, UnicodeDecodeError):
                    return None
                v = data.get(arg)
                # A chat route declares "slot=room" in; a room isn't its own
                # project — look that row up (the room's project = project
                # scope for the post; a DM returns None and the handler's own
                # read-check governs from there)
                if arg == "room" and v is not None:
                    row = conn.execute(
                        "SELECT kind, project FROM rooms WHERE id = ?", (int(v),))
                    row = row.fetchone()
                    if not row:
                        return None
                    if row["project"] is None:
                        # A DM or a plain room has NO project to check the tick
                        # against — 's own lane: add_messages in ANY project
                        # the person belongs to
                        return "allow_any"
                    return int(row["project"])
                return int(v) if v is not None else None
            if kind == "task":
                row = conn.execute(
                    "SELECT project FROM tasks WHERE id = ?",
                    (int(match.group(int(arg))),)).fetchone()
                return int(row["project"]) if row else None
        except (ValueError, TypeError, IndexError):
            return None
        return None

    def _rate_ok(self, path: str) -> bool:
        ip = self.client_address[0]
        bucket = "poll" if path == "/api/v1/signin/poll" else "open"
        if not self.server.rate_limit.allow(ip, bucket=bucket):
            self._error(429, "too many tries — wait a minute")
            return False
        return True

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _dispatch

    def do_TRACE(self):                                  # noqa: N802
        self._error(405, "this server answers GET, POST, PATCH, DELETE")

    def do_OPTIONS(self):                                # noqa: N802
        self._error(405, "this server answers GET, POST, PATCH, DELETE")

    # ---- helpers ----
    def _static(self, path: str) -> None:
        """Only the fixed allow-list, through importlib.resources."""
        from importlib import resources
        rel = self.static[path]
        pkg_dir = "vendor" if rel.startswith("vendor/") else "page"
        name = rel.split("/", 1)[1]
        try:
            text = (resources.files("aura_agent.teamserver") / pkg_dir
                    / name).read_bytes()
        except (OSError, FileNotFoundError):
            self._error(404, "no such page file")
            return
        ctype = ("text/html; charset=utf-8"
                 if name.endswith((".html", ".htm"))
                 else "text/javascript" if name.endswith(".js")
                 else "text/css" if name.endswith(".css")
                 else "image/svg+xml" if name.endswith(".svg")
                 else "text/plain; charset=utf-8")
        self._send(200, text, ctype)

    def _team_page(self, path: str) -> None:
        """GET team and anything under team— the built Svelte page (its entry
        lands in the dist as `team.html`). Unknown paths fall back to that
        shell (the page's own hash router owns the rest). api/* is never this
        lane: `path.startswith("/api/")` was checked before this run.
        """
        from importlib import resources
        rel = path[len("/team"):].split("?", 1)[0].lstrip("/")
        if not rel:
            parts: list[str] = []
        else:
            parts = [q for q in rel.split("/") if q and q not in (".", "..")]
            if not parts and rel:
                self._error(404, "no such file")
                return
        base = resources.files("aura_agent").joinpath("webui").joinpath("team")
        candidates = []
        if parts:
            candidates.append(base.joinpath(*parts))
        # the entry the build writes is team.html (vite's input name)
        candidates.append(base / "team.html")
        candidates.append(base / "index.html")
        for cand in candidates:
            try:
                if cand.is_dir():
                    continue
                raw = cand.read_bytes()
                break
            except (OSError, FileNotFoundError):
                continue
        else:
            self._error(404, "the team page isn't built here "
                             "(a wheel without the web build)")
            return
        name = (parts[-1] if parts else "team.html")
        ctype = ("text/html; charset=utf-8"
                 if name.endswith((".html", ".htm"))
                 else "text/javascript" if name.endswith(".js")
                 else "text/css" if name.endswith(".css")
                 else "image/svg+xml" if name.endswith(".svg")
                 else "image/png" if name.endswith(".png")
                 else "font/woff2" if name.endswith(".woff2")
                 else "application/octet-stream")
        extra = ({"Cache-Control": "max-age=31536000, immutable"}
                 if (parts and parts[0] == "assets")
                 else {"Cache-Control": "no-store"})
        self._send(200, raw, ctype, extra)


class RateLimit:
    """30 per minute per client IP on the unauthenticated routes — except the
    sign-in poll: the page's own heartbeat runs every ~2 s, so it carries a
    wider budget of its own (otherwise sign-in blocks itself after 15 s — found
    live).
    """

    def __init__(self, per_minute: int = 30, poll_per_minute: int = 60):
        self.per_minute = per_minute
        self.poll_per_minute = poll_per_minute
        self.hits: dict[tuple, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str, now: float | None = None,
              bucket: str = "open") -> bool:
        now = time.time() if now is None else now
        limit = self.poll_per_minute if bucket == "poll" else self.per_minute
        with self._lock:
            dq = self.hits.setdefault((ip, bucket), collections.deque())
            # a true rolling 60 s window — the minute-index version
            # double-counted the previous minute and blocked for up to two
            # minutes after the tenth hit (sign-in pages died on it — live)
            while dq and dq[0] <= now - 60:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


rate_limit = RateLimit()


def _changed_for(client_proto: int, path: str) -> bool:
    """True when `path` changed in a version the client predates."""
    from . import CHANGED_ROUTES
    import re
    for ver, patterns in CHANGED_ROUTES.items():
        if client_proto < ver:
            for pattern in patterns:
                if re.match(pattern, path):
                    return True
    return False


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    """The listening socket stays plain — TLS happens per connection in its
    own thread, so a client that connects and then says nothing can never
    hold the accept loop hostage."""
    def __init__(self, address, handler, ctx):
        super().__init__(address, handler)
        self.ctx = ctx                                  # The listening socket stays plain
    def process_request_thread(self, request, client_address):
        if self.ctx is not None:
            try:
                request.settimeout(10)
                request = self.ctx.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                self.shutdown_request(request)
                return
        super().process_request_thread(request, client_address)


def make_server(address, handler, ctx: ssl.SSLContext | None,
                fingerprint: str | None = None):
    """New server with the TLS context (: per-connection, in the request's own
    thread); the server's own fingerprint rides on it for invites.
    """
    srv = HubServer(address, handler, ctx)
    srv.server_fp = fingerprint or ""
    srv.rate_limit = RateLimit()          # Per server, not per process
    return srv


def serve_forever(srv) -> None:
    stop = threading.Event()
    t = threading.Thread(target=_backup_schedule, args=(stop,),
                         name="aura-backup-schedule", daemon=True)
    t.start()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        stop.set()


# -------------------------------------------------- scheduled backups --
BACKUP_TICK = 3600.0          # Seconds between due checks — due paces daily


def _backup_schedule(stop: threading.Event) -> None:
    """A backup at server start, then daily (— sqlite3 backup is online: the
    server keeps running while it copies). due decides; the retention rule
    (last 14 kept, older moved aside, never deleted) lives in backup.py and is
    not touched here. A dead database or a failed copy never kills the server —
    the next tick tries again.
    """
    from . import backup
    from . import sessions as _SESSIONS
    from .teamserver_db import backups_dir, open_team
    while not stop.is_set():
        try:
            if backup.due(backups_dir()):
                conn = open_team()
                if conn is not None:
                    try:
                        backup.backup_now(conn, backups_dir())
                    finally:
                        conn.close()
            # Ended sessions leave the table after 30 days
            conn = open_team()
            if conn is not None:
                try:
                    _SESSIONS.sweep(conn, __import__("time").time())
                    conn.commit()
                finally:
                    conn.close()
        except Exception:                                # noqa: BLE001
            # Never-silent: a dead scheduled backup must be a line in
            # server.log, never swallowed
            try:
                from .teamserver_db import team_dir as _td
                import traceback as _tb
                with open(_td() / "server.log", "a", encoding="utf-8") as f:
                    f.write("scheduled backup/sweep failed:\n"
                            + _tb.format_exc() + "\n")
            except Exception:                            # noqa: BLE001
                import traceback as _tb2
                _tb2.print_exc()
        stop.wait(BACKUP_TICK)

"""teams_chat.py — Aura Teams: the people chat. The data: teams_chat_store.py; the
page: teams_chat_page.py.

The first version's notes:

One single HTML page + one small Python server. No framework, no build step,
stdlib only. - It runs beside Vikunja on the hub machine and starts/stops with
it. - Login: the Vikunja username and password, checked LIVE against Vikunja's
own login endpoint (POST api/v1/login: 403 wrong password, 412 when the person
uses a 2-factor code — the page then asks for it). No passwords are ever stored
— not on disk, not in memory beyond the moment of the check. - The chat keeps
its own SQLite message store. One room per Vikunja instance. Refresh by polling
(no websockets in v1). - For people only. A user may point their own Aura at it
— their decision. - No chat in the online version.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .paths import aura_home

MAX_BODY = 2000                 # Characters in one message
MAX_POST = 16 * 1024            # Bytes in one request
SESSION_HOURS = 12


# ------------------------------------------------------------- storage ---
# the full chat's data: teams_chat_store.py (the first page's one-room
# `messages` table is moved into the 'general' room once)
from . import teams_chat_store as S


def db() -> sqlite3.Connection:
    return S.db()


def _db_path() -> Path:
    return S.home() / "chat.sqlite3"


# -------------------------------------------------------------- sessions ---
# session tokens live in memory only; a server restart means one re-login
_sessions: dict[str, tuple[str, float]] = {}
_streams: dict[str, int] = {}                  # Who has the page open (live)
_typing: dict[tuple[int, str], float] = {}
_codes: dict[str, tuple[str, str, float]] = {}  # Aura sign-in: code -> (user, task, expires)


def _session_user(token: str) -> str | None:
    s = _sessions.get(token)
    if not s:
        return None
    user, exp = s
    if time.time() > exp:
        _sessions.pop(token, None)
        return None
    return user


def _new_session(user: str) -> str:
    tok = secrets.token_hex(16)
    _sessions[tok] = (user, time.time() + 3600 * SESSION_HOURS)
    return tok


def check_vikunja_login(hub_base: str, username: str, password: str,
                        code: str = "", timeout: float = 8.0) -> str:
    """"ok" | "code" (a 2-factor code is needed) | "no" | "off" (hub not
    reachable). The ONLY place a password is touched: posted straight to
    Vikunja's own login and dropped. Nothing is stored."""
    body = {"username": username, "password": password}
    if code:
        body["totp_passcode"] = code
    req = urllib.request.Request(
        hub_base.rstrip("/") + "/api/v1/login", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok" if r.status == 200 else "no"
    except urllib.error.HTTPError as e:
        return "code" if e.code == 412 and not code else "no"
    except (urllib.error.URLError, OSError):
        return "off"


def _team_names() -> list[str]:
    try:
        from .teams_board import team_people
        return team_people()
    except Exception:                                # noqa: BLE001 - the hub off
        return []


def _project_names() -> list[str]:
    try:
        from .teams_board import my_projects
        return [str(p.get("title") or "") for p in my_projects()]
    except Exception:                                # noqa: BLE001
        return []


def _task_url() -> str:
    """Where `#123` in a message links to (the hub's own task page)."""
    try:
        from . import teams
        cfg = teams.load_config()
        if cfg.get("hub_kind") == "wired":
            from .teams_wiring import load_in_use
            return str((load_in_use().get("vars") or {}).get("task_url") or "")
        return cfg.get("hub_url", "").rstrip("/") + "/tasks/{id}" if cfg.get("hub_url") else ""
    except Exception:                                # noqa: BLE001
        return ""


# ---------------------------------------------------------------- page ---
from .teams_chat_page import PAGE  # noqa: E402


# --------------------------------------------------------------- server ---
class ChatHandler(BaseHTTPRequestHandler):
    hub_base = "http://127.0.0.1:3456"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _user(self) -> str | None:
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not tok and "t=" in self.path:            # The live stream can't set a header
            tok = self._q("t")
        u = _session_user(tok)
        if u:
            conn = db()
            try:
                S.seen_now(conn, u)
            finally:
                conn.close()
        return u

    def _q(self, key: str, default: str = "") -> str:
        from urllib.parse import parse_qs, urlparse
        return (parse_qs(urlparse(self.path).query).get(key) or [default])[0]

    # -------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                           "style-src 'unsafe-inline'; img-src 'self' data:"})
            return
        user = self._user()
        if path == "/messages":                      # The first page's API (kept)
            if not user:
                return self._json({"error": "sign in first"}, 401)
            try:
                after = int(self._q("after", "0"))
            except ValueError:
                after = 0
            conn = db()
            try:
                g = S.general(conn)
                rows = conn.execute("SELECT id,user AS username,body,ts FROM msgs WHERE room=? AND id>? "
                                    "AND deleted=0 ORDER BY id LIMIT 500", (g, after)).fetchall()
                out = [{**dict(r), "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))}
                       for r in rows]
            finally:
                conn.close()
            return self._json({"messages": out})
        if not user:
            return self._json({"error": "sign in first"}, 401)
        conn = db()
        try:
            if path == "/api/me":
                S.general(conn)                      # Every team has its 'general'
                S.ensure_project_rooms(conn, _project_names())
                return self._json({"user": user, "emoji": S.EMOJI, "task_url": _task_url(),
                                   "last_event": S.last_event(conn)})
            if path == "/api/rooms":
                return self._json({"rooms": S.rooms_for(conn, user)})
            if path == "/api/history":
                room = int(self._q("room", "0"))
                if not S.can_see(conn, room, user):
                    return self._json({"error": "not your room"}, 403)
                before = int(self._q("before", "0")) or None
                return self._json({"messages": S.history(conn, room, before),
                                   "seen": S.seen_by(conn, room)})
            if path == "/api/search":
                return self._json({"messages": S.search(conn, user, self._q("q"))})
            if path == "/api/people":
                return self._json({"people": S.people(conn, _team_names(), set(_streams))})
            if path.startswith("/files/"):
                f, data = S.get_file(conn, path.rsplit("/", 1)[1])
                if not f:
                    return self._json({"error": "no such file"}, 404)
                inline = f["type"].startswith("image/")      # Proven by its bytes
                disp = ("inline" if inline else "attachment") + f'; filename="{f["name"]}"'
                return self._send(200, data, f["type"], {"Content-Disposition": disp,
                                                        "Content-Security-Policy": "default-src 'none'"})
            if path == "/api/events":
                return self._stream(user, conn)
        except (ValueError, TypeError):
            return self._json({"error": "bad request"}, 400)
        finally:
            conn.close()
        self._json({"error": "not found"}, 404)

    def _stream(self, user: str, conn) -> None:
        """Server-sent events: what happened since Last-Event-ID (a reconnect
        gets what it missed), then live; typing and presence ride along."""
        try:
            after = int(self.headers.get("Last-Event-ID") or self._q("after", "0") or 0)
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        _streams[user] = _streams.get(user, 0) + 1
        last_beat = 0.0
        try:
            while True:
                c = db()
                try:
                    evs = S.events_after(c, after, user)
                finally:
                    c.close()
                for e in evs:
                    after = e["id"]
                    self.wfile.write(f"id: {e['id']}\nevent: {e['kind']}\ndata: "
                                     f"{json.dumps({'room': e['room'], **({'d': e['data']})})}\n\n".encode())
                now = time.time()
                typing = [{"room": r, "user": u} for (r, u), t in list(_typing.items())
                          if now - t < 6 and u != user]
                if typing or now - last_beat > 15:
                    self.wfile.write(f"event: beat\ndata: {json.dumps({'typing': typing, 'online': sorted(_streams)})}\n\n".encode())
                    last_beat = now
                self.wfile.flush()
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            _streams[user] = _streams.get(user, 1) - 1
            if _streams[user] <= 0:
                _streams.pop(user, None)

    # ------------------------------------------------------------- POST
    def do_POST(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
        except ValueError:
            ln = -1
        path = self.path.split("?", 1)[0]
        limit = S.MAX_FILE if path == "/api/upload" else MAX_POST
        if ln < 0 or ln > limit:
            return self._json({"error": "too big"}, 413)
        raw = self.rfile.read(ln) if ln else b""
        if path == "/api/upload":
            user = self._user()
            if not user:
                return self._json({"error": "sign in first"}, 401)
            from urllib.parse import unquote
            conn = db()
            try:
                return self._json(S.save_file(conn, user, unquote(self.headers.get("X-Name", "file")), raw))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            finally:
                conn.close()
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if path == "/login":
            return self._login(body)
        if path == "/api/aura-signin":
            return self._aura_signin(body)
        user = self._user()
        if not user:
            return self._json({"error": "sign in first"}, 401)
        conn = db()
        try:
            if path == "/send":                          # The first page's API (kept)
                m = S.post(conn, S.general(conn), user, str(body.get("body", ""))[:MAX_BODY])
                return self._json({"id": m["id"]})
            if path == "/api/post":
                room = int(body.get("room") or 0)
                if not S.can_see(conn, room, user):
                    return self._json({"error": "not your room"}, 403)
                return self._json(S.post(conn, room, user, str(body.get("body", "")),
                                          body.get("reply_to"), body.get("file")))
            if path == "/api/edit":
                return self._json(S.edit(conn, int(body["id"]), user, str(body.get("body", ""))))
            if path == "/api/delete":
                S.delete(conn, int(body["id"]), user)
                return self._json({"ok": True})
            if path == "/api/react":
                return self._json(S.react(conn, int(body["id"]), user, str(body.get("emoji", ""))))
            if path == "/api/read":
                room = int(body.get("room") or 0)
                if S.can_see(conn, room, user):
                    S.mark_read(conn, room, user, int(body.get("last") or 0))
                return self._json({"ok": True})
            if path == "/api/typing":
                _typing[(int(body.get("room") or 0), user)] = time.time()
                return self._json({"ok": True})
            if path == "/api/room":
                return self._json({"id": S.make_room(conn, str(body.get("name", "")), user)})
            if path == "/api/dm":
                return self._json({"id": S.dm(conn, user, str(body.get("user", "")))})
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except (ValueError, KeyError, TypeError) as e:
            return self._json({"error": str(e) or "bad request"}, 400)
        finally:
            conn.close()
        self._json({"error": "not found"}, 404)

    def _login(self, body: dict):
        """Vikunja hubs: the username and password, checked live (kept)."""
        u = str(body.get("u", "")).strip()[:250]
        p = str(body.get("p", ""))[:250]
        c = str(body.get("c", "")).strip()[:12]
        if not u or not p:
            return self._json({"error": "username and password, please"}, 400)
        said = check_vikunja_login(self.hub_base, u, p, c)
        del p                                         # The password is gone
        if said == "code":
            return self._json({"error": "2-factor code needed"}, 412)
        if said == "off":
            return self._json({"error": "the hub is not answering — try again soon"}, 503)
        if said != "ok":
            return self._json({"error": "wrong username, password or code"}, 401)
        self._json({"token": _new_session(u), "user": u})

    def _aura_signin(self, body: dict):
        """Any hub: step "start" gives a one-time code and the task to post it on;
        the person's own Aura posts it with their hub account; step "check"
        reads the comments back from the hub and signs in the comment's author.
        No password, no token sent here.
        """
        step = str(body.get("step", ""))
        user = str(body.get("user", "")).strip()[:120]
        now = time.time()
        for k, (_, _, exp) in list(_codes.items()):
            if exp < now:
                _codes.pop(k, None)
        if step == "start":
            if not user:
                return self._json({"error": "who is signing in?"}, 400)
            try:
                from .teams_board import signin_task
                task = signin_task(create=True)
            except Exception as e:                    # noqa: BLE001
                return self._json({"error": f"the hub can't be read here: {e}"}, 503)
            if not task:
                return self._json({"error": "no sign-in task on the hub"}, 503)
            code = secrets.token_hex(4)
            _codes[code] = (user, str(task), now + 300)
            return self._json({"code": code, "task": task, "line": f"[AURA] chat sign-in {code}"})
        if step == "check":
            code = str(body.get("code", ""))
            got = _codes.get(code)
            if not got:
                return self._json({"error": "that code isn't known (or ran out) — start again"}, 400)
            want, task, _ = got
            try:
                from .teams_board import comment_authors
                said = comment_authors(int(task))
            except Exception as e:                    # noqa: BLE001
                return self._json({"error": f"the hub can't be read here: {e}"}, 503)
            hit = [a for a, t in said if f"chat sign-in {code}" in t]
            if not hit:
                return self._json({"error": "the code isn't on the board yet"}, 404)
            if hit[0] and hit[0] != want:
                return self._json({"error": "that code was posted by someone else"}, 403)
            if not hit[0]:
                return self._json({"error": "this hub doesn't say who wrote a comment — "
                                            "the wiring needs the comment's author"}, 501)
            _codes.pop(code, None)
            return self._json({"token": _new_session(want), "user": want})
        return self._json({"error": "step is start or check"}, 400)

    def log_message(self, format, *args):   # noqa: A002 - stdlib signature
        pass                                # Quiet: no spam in the hub log


def _serve(port: int, hub_base: str) -> None:
    """The chat server itself. Runs as its own process, started and stopped
    with the hub (aura teams hub start/stop)."""
    ChatHandler.hub_base = hub_base
    httpd = ThreadingHTTPServer(("0.0.0.0", port), ChatHandler)
    httpd.daemon_threads = True
    httpd.serve_forever()


# ------------------------------------------------------ lifecycle (hub) ---
def chat_port() -> int:
    return int(os.environ.get("AURA_CHAT_PORT", "3460"))


def chat_pid_file() -> Path:
    d = aura_home() / "teams" / "hub"
    d.mkdir(parents=True, exist_ok=True)
    return d / "chat.pid"


def _alive(pid: int) -> bool:
    # never os.kill(pid, 0) here: on Windows any signal value terminates
    # the process (Python docs, os.kill)
    from .teams import _alive as alive
    return alive(pid)


def running() -> int | None:
    pf = chat_pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        return None
    return pid if _alive(pid) else None


def start(hub_base: str, port: int | None = None) -> dict:
    """Start the chat server beside the hub (: starts and stops with it)."""
    port = port or chat_port()
    pid = running()
    if pid:
        return {"ok": True, "detail": f"already running (pid {pid})",
                "port": port, "pid": pid}
    chat_pid_file().unlink(missing_ok=True)
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "aura_agent.teams_chat", "--serve",
         str(port), hub_base],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        start_new_session=True)
    chat_pid_file().write_text(str(proc.pid), encoding="utf-8")
    for _ in range(20):                              # Wait until it answers
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                if r.status == 200:
                    announce(port)
                    return {"ok": True, "port": port, "pid": proc.pid}
        except Exception:                            # noqa: BLE001
            if proc.poll() is not None:
                break
            time.sleep(0.4)
    stop()
    return {"ok": False, "detail": f"the chat page did not come up on port {port}"}


ADDRESS_LINE = "[AURA] the team chat is at "


def announce(port: int) -> None:
    """Post the chat's address on the board's sign-in task, so every Aura on
    the team finds it (any hub). Best effort — the hub may be off."""
    try:
        from .teams import lan_address
        from .teams_board import add_comment, signin_task
        task = signin_task(create=True)
        if task:
            add_comment(task, f"{ADDRESS_LINE}http://{lan_address()}:{port}/")
    except Exception:                                # noqa: BLE001
        pass


def find_address() -> str:
    """Where the team chat is: this machine's, else the newest address on the board."""
    pid = running()
    if pid:
        return f"http://127.0.0.1:{chat_port()}/"
    try:
        from .teams_board import comment_authors, signin_task
        task = signin_task(create=False)
        if task:
            for _, text in reversed(comment_authors(task)):
                if text.startswith(ADDRESS_LINE):
                    return text[len(ADDRESS_LINE):].strip()
    except Exception:                                # noqa: BLE001
        pass
    return ""


def open_for_me(say=print) -> str:
    """Sign this person in through their own Aura and open the page (choice a):
    a one-time code from the chat, posted on the board with THEIR hub account,
    read back by the chat — no password, no token sent."""
    import webbrowser
    from . import teams
    from .teams_board import add_comment
    url = find_address()
    if not url:
        return ("the team chat isn't running anywhere the board knows of — start it on one "
                "machine (ask Aura to 'turn on the team chat')")
    me = teams.load_config().get("username", "")
    if not me:
        return "connect to the team first"

    def call(body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(url.rstrip("/") + "/api/aura-signin", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    st, j = call({"step": "start", "user": me})
    if st != 200:
        return f"the chat said: {j.get('error', st)}"
    add_comment(int(j["task"]), j["line"])
    for _ in range(10):
        st, k = call({"step": "check", "code": j["code"]})
        if st == 200:
            from urllib.parse import quote
            page = f"{url.rstrip('/')}/#t={quote(k['token'])}&u={quote(k['user'])}"
            webbrowser.open(page)
            return f"the team chat is open in your browser, signed in as {k['user']} ({url})"
        if st != 404:
            return f"couldn't sign in: {k.get('error', st)}"
        time.sleep(1)
    return "the sign-in code didn't show up on the board in time — try again"


def stop() -> dict:
    pf = chat_pid_file()
    pid = running()
    if not pid:
        pf.unlink(missing_ok=True)
        return {"ok": True, "detail": "chat not running"}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "detail": str(e)}
    pf.unlink(missing_ok=True)
    return {"ok": True, "detail": f"chat stopped (pid {pid})"}


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--serve":
        _serve(int(sys.argv[2]), sys.argv[3])
    else:
        print("usage: python -m aura_agent.teams_chat --serve PORT HUB_URL")

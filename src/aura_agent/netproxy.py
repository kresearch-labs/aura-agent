"""netproxy.py — commands reach only the project's allowed sites.

Windows has no such fence without admin (a sandbox user + a firewall filter):
there the proxy settings are only honoured by programs that read them (curl,
pip, npm, git, Python, Node) — said plainly in each result.

This proxy: HTTP CONNECT (HTTPS and any TCP tunnel) and plain HTTP. The allow
list is the project's scope (`aura scope add`, scope add). A refused host is
remembered so the result can say which and how to allow it. Zero dependencies
(standard library).
"""
from __future__ import annotations

import ipaddress
import os
import select
import socket
import socketserver
import threading
from pathlib import Path
from typing import Callable

_DENIED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]


def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True
    return any(a in n for n in _DENIED_NETS)


class ScopeProxy:
    """One per process. allowed(host) -> bool decides; refused hosts are
    collected per command (take_refused)."""

    def __init__(self, allowed: Callable[[str], bool]):
        self.allowed = allowed
        self.refused: list[str] = []
        self._lock = threading.Lock()
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._serve(self.request)

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.sock_path: str | None = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # -------------------------------------------------------------- policy
    def check(self, host: str) -> str | None:
        """None = allowed; else why not."""
        h = (host or "").strip().lower().strip("[]")
        if not h:
            return "no host"
        try:
            if not self.allowed(h):
                return f"{h} is not in this project's allowed sites"
        except Exception:                                   # noqa: BLE001
            return f"{h}: the site list could not be read"
        try:
            ips = {ai[4][0] for ai in socket.getaddrinfo(h, None)}
        except OSError:
            return None                                     # Let the connect report it
        # an allowed NAME that points inside (DNS rebinding, metadata) is
        # refused — unless the user allowed that address itself
        bad = [ip for ip in ips if _private(ip) and not self.allowed(ip)]
        if bad and not _is_ip(h):
            return f"{h} points at a private address ({bad[0]})"
        return None

    def take_refused(self) -> list[str]:
        with self._lock:
            out, self.refused = self.refused, []
        return out

    def _refuse(self, conn, host: str, why: str) -> None:
        with self._lock:
            if host not in self.refused:
                self.refused.append(host)
        body = (f"Aura: {why} - Aura asks the user about {host} when this command ends\n").encode()
        try:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        except OSError:
            pass

    # ------------------------------------------------------------- serving
    def _serve(self, conn) -> None:
        try:
            conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            line, _, rest = head.partition(b"\r\n")
            parts = line.decode("latin-1").split()
            if len(parts) < 3:
                return
            method, target = parts[0].upper(), parts[1]
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host = host.strip("[]")
                why = self.check(host)
                if why:
                    return self._refuse(conn, host, why)
                up = socket.create_connection((host, int(port or 443)), timeout=30)
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                extra = head.split(b"\r\n\r\n", 1)[1]
                if extra:
                    up.sendall(extra)
                return _pipe(conn, up)
            # plain HTTP: an absolute URL; the Host header must agree
            if not target.lower().startswith("http://"):
                return self._refuse(conn, "?", "only proxied requests are served")
            hostport = target[7:].split("/", 1)[0]
            host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "")
            hdr_host = ""
            for h in rest.split(b"\r\n"):
                if h.lower().startswith(b"host:"):
                    hdr_host = h[5:].decode("latin-1").strip().split(":")[0].lower()
            if hdr_host and hdr_host != host.lower():
                return self._refuse(conn, host, "the Host header doesn't match the address")
            why = self.check(host)
            if why:
                return self._refuse(conn, host, why)
            up = socket.create_connection((host, int(port or 80)), timeout=30)
            path = "/" + target[7:].split("/", 1)[1] if "/" in target[7:] else "/"
            up.sendall(f"{method} {path} {parts[2]}\r\n".encode("latin-1")
                       + head.split(b"\r\n", 1)[1])
            return _pipe(conn, up)
        except (OSError, ValueError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------- Linux bubblewrap bridging
    def unix_socket(self, folder: Path) -> str:
        """A socket file that relays to the proxy — bind-mounted into a
        bubblewrap sandbox (which has no network at all)."""
        if self.sock_path:
            return self.sock_path
        path = str(Path(folder) / f"aura-proxy-{os.getpid()}.sock")
        try:
            os.unlink(path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                up = socket.create_connection(("127.0.0.1", self.port))
                threading.Thread(target=_pipe, args=(c, up), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        self.sock_path = path
        return path


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _pipe(a, b) -> None:
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 120)
            if x or not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


# ------------------------------------------------------------ one per process
_PROXY: ScopeProxy | None = None
_PROXY_LOCK = threading.Lock()


def get(allowed: Callable[[str], bool]) -> ScopeProxy:
    """The process's proxy; the allow rule is refreshed on each call (the
    scope can change during a chat)."""
    global _PROXY
    with _PROXY_LOCK:
        if _PROXY is None:
            _PROXY = ScopeProxy(allowed)
        else:
            _PROXY.allowed = allowed
        return _PROXY


def env_for(port: int) -> dict:
    """What tells programs to use the proxy (curl, wget, pip, npm, git,
    Python, Node read these). This machine stays direct."""
    url = f"http://127.0.0.1:{port}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}


def local_through(env: dict) -> dict:
    """When the OS sandbox lets a command connect ONLY to the proxy (bubblewrap,
    Landlock with network control, the Windows container in filter mode), a
    request to this machine (localhost, 127.0.0.1) must go through the proxy
    too — sent directly it is blocked, so an allowed local service was
    unreachable. The proxy lets it through when the user allowed that address,
    and says how to allow it otherwise. Without such a sandbox, local traffic
    stays direct.
    """
    env["NO_PROXY"] = env["no_proxy"] = ""
    return env


# a relay INSIDE bubblewrap: listens on the sandbox's own 127.0.0.1:port and
# passes each connection to the socket file mounted from outside
RELAY = ("import socket,threading,sys\n"
         "p,u=int(sys.argv[1]),sys.argv[2]\n"
         "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
         "s.bind(('127.0.0.1',p));s.listen(64)\n"
         "def pipe(a,b):\n"
         "  try:\n"
         "    while True:\n"
         "      d=a.recv(65536)\n"
         "      if not d: break\n"
         "      b.sendall(d)\n"
         "  except OSError: pass\n"
         "  finally:\n"
         "    for x in (a,b):\n"
         "      try: x.close()\n"
         "      except OSError: pass\n"
         "while True:\n"
         "  c,_=s.accept();v=socket.socket(socket.AF_UNIX);v.connect(u)\n"
         "  threading.Thread(target=pipe,args=(c,v),daemon=True).start()\n"
         "  threading.Thread(target=pipe,args=(v,c),daemon=True).start()\n")

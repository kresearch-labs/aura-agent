"""teams_server.py — the Aura side of the server process (plan).

Start runs `python -m aura_agent.teamserver run` detached (updater.detach on
Windows, start_new_session elsewhere), writes a pid file in team_dir, and waits
until GET api/v1/health answers. stop ends it. status says what is running.
Default port 7443, or the next free in 7443–7463 — the chosen port is saved and
reused.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .teamserver import PROTOCOL
from .teamserver.teamserver_db import team_dir

DEFAULT_PORT = 7443
PORT_RANGE = range(7443, 7464)
PID_FILE = "server.pid"                 # Pre- keel: superseded by LOCK_FILE
LOCK_FILE = "hub.lock"                  # {port, pid, run_id}.
PORT_FILE = "port.txt"


def _pid_path() -> Path:
    return team_dir() / PID_FILE


def _port_path() -> Path:
    return team_dir() / PORT_FILE


def saved_port() -> int | None:
    if _port_path().exists():
        try:
            return int(_port_path().read_text().strip())
        except ValueError:
            return None
    return None


def _save_port(port: int) -> None:
    try:
        _port_path().write_text(str(port))
    except OSError:
        pass


def _lock() -> dict | None:
    """The running hub's lock row, or None. A broken file is no lock."""
    p = team_dir() / LOCK_FILE
    if not p.exists():
        return None
    try:
        import json as _json
        d = _json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    return d if d.get("port") and d.get("pid") and d.get("run_id") else None


def _is_hub_pid(the_pid: int) -> bool:
    """End a process only when it IS the hub's own: checked by the command line
    holding 'aura_agent.teamserver' — Linux proc, Windows PowerShell. When we
    can't confirm, the answer is False (never kill).
    """
    import sys as _sys
    if _sys.platform.startswith(("linux", "darwin")):
        try:
            cmd = open(f"/proc/{the_pid}/cmdline", "rb").read()                 .replace(b"\x00", b" ").decode("utf-8", "ignore")
            return "aura_agent.teamserver" in cmd
        except OSError:
            return False
    try:                                       # Windows
        out = __import__("subprocess").run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId = "
             f"{the_pid}').CommandLine"],
            capture_output=True, text=True, timeout=8).stdout or ""
        return "aura_agent.teamserver" in out
    except Exception:                          # noqa: BLE001
        return False


def pid() -> int | None:
    """The hub's pid per its lock, alive-checked the way `running` does it: the
    lock + health's run_id — never signal-0 (on Windows that pokes Ctrl+C into
    the wrong debate; signals stay out of the read path).
    """
    lk = _lock()
    if not lk:
        return None
    the_pid = int(lk["pid"])
    st = probe(int(lk["port"]))
    if not (st.get("ok") and st.get("run_id") == lk["run_id"]):
        return None
    return the_pid if _is_hub_pid(the_pid) else None


def log_path() -> Path:
    return team_dir() / "server-stdout.log"


def _detach(argv: list[str]) -> subprocess.Popen | None:
    """Detached start: updater's way on Windows, a new session elsewhere."""
    if os.name == "nt":
        from .updater import detach
        return detach(argv)
    log = open(log_path(), "ab")
    return subprocess.Popen(argv, stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)


def start(port: int | None = None, wait: float = 15.0) -> dict:
    """Start the server detached; wait until health answers."""
    if running():
        return {"ok": True, "already": True, "port": status().get("port")}
    import time
    port = port or saved_port() or DEFAULT_PORT
    argv = [sys.executable, "-m", "aura_agent.teamserver", "run",
            "--port", str(port)]
    log_path().parent.mkdir(parents=True, exist_ok=True)
    proc = _detach(argv)
    if proc is not None and proc.pid:
        _pid_path().write_text(str(proc.pid))
    deadline = time.time() + wait
    while time.time() < deadline:
        st = probe(port)
        if st.get("ok"):
            _save_port(st["port"])
            return {"ok": True, "port": st["port"], "team": st.get("team")}
        time.sleep(0.3)
    return {"ok": False,
            "detail": "the server did not answer /health in time"}


def probe(port: int | None = None) -> dict:
    """GET /api/v1/health on 127.0.0.1 — plain HTTP first (behind-proxy
    mode), then TLS without verification (the pin is a client-side rule;
    the local health check just wants to know the process is alive)."""
    import http.client
    import ssl
    port = port or saved_port() or DEFAULT_PORT
    ctx = __import__("ssl").SSLContext(__import__("ssl").PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = __import__("ssl").CERT_NONE
    for p in [port] + [q for q in PORT_RANGE if q != port]:
        for make in (lambda: http.client.HTTPConnection("127.0.0.1", p,
                                                        timeout=3),
                     lambda: http.client.HTTPSConnection("127.0.0.1", p,
                                                         context=ctx,
                                                         timeout=3)):
            try:
                c = make()
                c.request("GET", "/api/v1/health")
                r = c.getresponse()
                body = json.loads(r.read() or b"{}")
                c.close()
                if body.get("ok"):
                    return dict(body, port=p)
            except OSError:
                continue
            except ValueError:
                continue
    return {"ok": False}


def stop(timeout: float = 6.0) -> dict:
    """Stop the hub: ask it (SIGTERM) only when the lock's run_id is the run_id
    health serves — a stale lock or an unconfirmed pid ends NOTHING. SIGKILL
    only when the ask didn't work, same identity check.
    """
    import time
    lk = _lock()
    if not lk:
        return {"ok": True, "detail": "the team server is not running"}
    st = probe(int(lk["port"]))
    if not (st.get("ok") and st.get("run_id") == lk["run_id"]):
        # the lock's process isn't answering as this hub — a stale lock
        # kills nothing (the audit's stale-pid hole), it just clears itself
        try:
            (team_dir() / LOCK_FILE).unlink()
        except OSError:
            pass
        return {"ok": True,
                "detail": "a stale hub lock was dropped — nothing was "
                          "running to stop"}
    the_pid = pid()
    if the_pid is None:
        return {"ok": False,
                "detail": "the hub answers but can't be confirmed as the "
                          "hub's own process — not killed, stop it from the "
                          "hub machine yourself"}
    try:
        os.kill(the_pid, 15)                 # Ask first
    except OSError:
        return {"ok": True, "detail": "team server stopped"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        if not probe(int(lk["port"])).get("ok"):
            break
    else:
        try:
            os.kill(the_pid, 9)              # Only because it ignored the ask
        except OSError:
            pass
    for f in (LOCK_FILE, PID_FILE):
        try:
            (team_dir() / f).unlink()
        except OSError:
            pass
    return {"ok": True, "detail": "team server stopped"}


def running() -> bool:
    """Running = the lock exists AND health answers with the same run_id (a
    crashed hub's leftover lock never counts).
    """
    lk = _lock()
    if not lk:
        return False
    st = probe(int(lk["port"]))
    return bool(st.get("ok") and st.get("run_id") == lk["run_id"])


def status() -> dict:
    the_pid = pid()
    st = probe()
    return {"running": the_pid is not None, "pid": the_pid,
            "healthy": st.get("ok"), "port": st.get("port") or saved_port(),
            "team": st.get("team"), "protocol": st.get("protocol", PROTOCOL)}


# ------------------------------------------------- hub take-over
def take_over(bundle_file, team_id: str) -> dict:
    """Open a hand-over bundle with THIS device's key: the key file inside
    is sealed for this device (nacl SealedBox, Ed25519->Curve25519). The
    database and certificate move in; the server starts here. The
    fingerprint stays the same because the certificate moved with it."""
    import base64
    import io
    import shutil
    import zipfile
    import nacl.bindings
    import nacl.encoding
    import nacl.public
    import nacl.signing
    from .teamserver.teamclient import load_device_key
    sk = load_device_key(team_id)
    if sk is None:
        return {"ok": False,
                "detail": "this Aura has no device key for this team — "
                          "join it first, then take the hub over"}
    raw = Path(bundle_file).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        sealed_key = base64.b64decode(z.read("team-key.pem.sealed"))
        sealed_db = base64.b64decode(z.read("team.db.sealed"))
        sealed_cert = base64.b64decode(z.read("team-cert.pem.sealed"))
        sealed_for = z.read("sealed-for").decode()
        settings = z.read("team.settings.json")
    # Unseal exactly the way the bundle was sealed (api_hub.hub_bundle): this
    # device's Ed25519 key -> Curve25519, nacl SealedBox; the 64-byte Ed25519
    # secret key (seed + public key) is what converts
    seed = sk.encode(nacl.encoding.RawEncoder)
    sk64 = seed + sk.verify_key.encode(nacl.encoding.RawEncoder)
    curve = nacl.public.PrivateKey(
        nacl.bindings.crypto_sign_ed25519_sk_to_curve25519(sk64))
    box = nacl.public.SealedBox(curve)
    key_pem = box.decrypt(sealed_key)
    db_raw = box.decrypt(sealed_db)          # The WHOLE bundle sealed (H-3)
    cert_pem = box.decrypt(sealed_cert)
    d = team_dir()
    # the before-copy rule applies to take-over too: keep what was here
    existing = _db_path()
    if existing.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        bdir = d / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(existing, bdir / f"before-takeover-{stamp}.db")
    (d / "team-cert.pem").write_bytes(cert_pem)
    (d / "team-key.pem").write_bytes(key_pem)
    _owner_only_file(d / "team-key.pem")
    (d / "team.settings.json").write_bytes(settings)
    # the database: put the backup in place (server must be stopped first —
    # the tool's card says the old hub stops serving after this)
    r = stop()
    (d / "team.db").write_bytes(db_raw)
    # Restore lanes never drag an old Write-Ahead-Log with them — a -wal/-shm
    # from the stopped server must stand alone
    for suf in ("-wal", "-shm"):
        try:
            (d / ("team.db" + suf)).unlink()
        except FileNotFoundError:
            pass
    started = start()
    from .teamserver import tls as TL
    fp = TL.fingerprint((d / "team-cert.pem").read_bytes())
    return {"ok": True, "port": started.get("port"),
            "fingerprint": fp, "same_fingerprint": started.get("ok", False)}


def _owner_only_file(p: Path) -> None:
    import os
    os.chmod(p, 0o600)


def _db_path():
    from .teamserver.teamserver_db import db_path
    return db_path()

def login_start(on: bool) -> list[str]:
    """This machine's team hub starts when the computer starts (a per-user
    start-up entry. Also remembered as `team.hub_autostart`, so opening Aura
    starts it too. Returns the lines to show the person.
    """
    from . import teams
    from . import setup_steps as S
    cfg = teams.load_config()
    cfg["team.hub_autostart"] = bool(on)
    teams.save_config(cfg)
    kind = S.runner_kind()
    f = S.runner_file(kind, "hub")
    if f is None:
        return ["This system can't start programs at login for you (a container, "
                "or WSL without systemd): the hub starts when you open Aura."] if on else []
    if not on:
        if f.exists():
            if kind == "systemd":
                S.EXEC(["systemctl", "--user", "disable", f.name])
            f.unlink()
        return ["The hub won't start by itself after a restart — run Aura's "
                "setup again and choose to start it with the computer."]
    port = saved_port() or DEFAULT_PORT
    text = S.runner_text(kind, ["-m", "aura_agent.teamserver", "run", "--port", str(port)],
                         "Aura team hub", "hub")
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists() or f.read_text(encoding="utf-8") != text:
        f.write_text(text, encoding="utf-8", newline="")
    if kind == "systemd":
        # enabled for the next start; the hub running now keeps running
        S.EXEC(["systemctl", "--user", "daemon-reload"])
        S.EXEC(["systemctl", "--user", "enable", f.name])
    return ["The hub starts again when this computer starts."]


def autostart_if_set() -> dict | None:
    """`team.hub_autostart` (default OFF — asked in the start card) starts this
    machine's team hub when Aura starts. Returns the start result or None when
    off/already running.
    """
    try:
        from . import teams
        cfg = teams.load_config()
        if not cfg.get("team.hub_autostart"):
            return None
        if running():
            return None
        if not cfg.get("aura_team"):
            return None
        return start()
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "detail": str(e)}

def update_with_backup() -> dict:
    """The hub-machine update — backup, stop, install, start again."""
    from . import teams
    steps: list[str] = []
    conn = None
    try:
        from .teamserver import backup, teamserver_db
        conn = teamserver_db.open_team()
        if conn is not None:
            res = backup.backup_now(conn, teamserver_db.backups_dir())
            steps.append(f"backed up ({str(res)[:60]})")
        stop()
        steps.append("stopped the hub")
        return {"ok": True, "steps": steps,
                "next": "install the new build now, then start the hub "
                        "(the update card says so)"}
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "steps": steps, "detail": str(e)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

"""running.py — every running piece of Aura, so it can be listed, stopped and
restarted like a service (`aura status`, `aura stop`, `aura restart`).

Each long-running Aura process (an open chat, the team hub, the scheduled-jobs
watcher, the dashboard, the proxy) writes one small file while it runs:
<aura home>/run/<pid>.json — what it is, since when, where, and how to start it
again. It is removed when the process ends; a file whose process is gone
(a crash) is cleaned up the next time the list is read.

Stopping asks first: a "please stop" file (<pid>.stop) that the process
watches — an open chat then saves where it was and closes by itself. Only a
forced stop ends a process at once.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path

from .paths import aura_home

KINDS = {"chat": "chat", "hub": "team hub", "cron": "scheduled-jobs watcher",
         "serve": "dashboard", "proxy": "model proxy"}


def run_dir() -> Path:
    d = aura_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(pid: int | None = None) -> Path:
    return run_dir() / f"{pid or os.getpid()}.json"


def alive(pid: int) -> bool:
    """Is that process still running (any system)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259         # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def register(kind: str, **info) -> None:
    """This process is a running piece of Aura until it ends."""
    rec = {"pid": os.getpid(), "kind": kind, "started": time.time(),
           "folder": os.getcwd(),
           "argv": [sys.executable, "-m", "aura_agent"] + sys.argv[1:], **info}
    try:
        _file().write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        return
    atexit.register(unregister)


def update(**info) -> None:
    """Add to this process's entry (e.g. which chat it has open)."""
    p = _file()
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec.update(info)
        p.write_text(json.dumps(rec), encoding="utf-8")
    except (OSError, ValueError):
        pass


def unregister() -> None:
    for p in (_file(), run_dir() / f"{os.getpid()}.stop"):
        try:
            p.unlink()
        except OSError:
            pass


def entries(include_self: bool = False) -> list[dict]:
    """Every running piece of Aura, oldest first (dead entries are removed)."""
    out = []
    for p in sorted(run_dir().glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            pid = int(rec.get("pid") or 0)
        except (OSError, ValueError):
            continue
        if not alive(pid):
            for q in (p, p.with_suffix(".stop")):
                try:
                    q.unlink()
                except OSError:
                    pass
            continue
        if pid == os.getpid() and not include_self:
            continue
        out.append(rec)
    return sorted(out, key=lambda r: r.get("started", 0))


def stop_requested() -> bool:
    """Has someone asked this process to stop (aura stop / aura restart)?"""
    return (run_dir() / f"{os.getpid()}.stop").exists()


def ask_stop(pid: int) -> None:
    try:
        (run_dir() / f"{pid}.stop").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def force_stop(pid: int) -> None:
    try:
        os.kill(int(pid), signal.SIGTERM)          # Windows: ends the process
    except OSError:
        pass
    try:
        _file(pid).unlink()
    except OSError:
        pass


def wait_gone(pids: list[int], timeout: float = 30.0) -> list[int]:
    """Wait for these to end; returns the ones still running."""
    end = time.time() + timeout
    left = [p for p in pids if alive(p)]
    while left and time.time() < end:
        time.sleep(0.5)
        left = [p for p in left if alive(p)]
    return left


def start_again(rec: dict) -> bool:
    """Start a stopped service the way it was started (detached)."""
    if rec.get("kind") == "hub":
        try:
            from . import teams_server as TS
            return bool(TS.start().get("ok"))
        except Exception:                            # noqa: BLE001
            return False
    argv = rec.get("argv") or []
    if not argv:
        return False
    import subprocess
    log = open(run_dir() / f"restart-{rec.get('kind', 'x')}.log", "ab")
    kw: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
                "cwd": rec.get("folder") or None}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)
        return True
    except OSError:
        return False


def describe(rec: dict) -> str:
    age = max(0, time.time() - float(rec.get("started") or 0))
    since = (f"{int(age // 3600)}h {int(age % 3600 // 60)}m" if age >= 3600
             else f"{int(age // 60)}m")
    what = KINDS.get(rec.get("kind", ""), rec.get("kind", "?"))
    extra = ""
    if rec.get("kind") == "chat":
        bits = [f"#{rec['chat']}" if rec.get("chat") else "", rec.get("title") or ""]
        extra = " ".join(b for b in bits if b)
    return (f"{what:<22} {extra[:40]:<40} in {rec.get('folder', '?')} · "
            f"running {since} · pid {rec.get('pid')}")

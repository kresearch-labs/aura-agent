"""machine.py — check the machine before a heavy job.

Example heavy job: finding a LOCAL model's context size by testing makes the
local server load ever bigger prompts — on a machine low on memory that can
freeze it. A remote server doesn't use this machine's memory, so it is never
checked. Standard library only (Windows, Linux, macOS).
"""
from __future__ import annotations

import os
import sys
import urllib.parse

LOW_FREE_BYTES = 2 * 1024 ** 3        # Under 2 GB free…
LOW_FREE_SHARE = 0.10                 # …or under 10% free → ask first


def memory() -> tuple[int, int] | None:
    """(free, total) bytes of RAM, or None when it can't be read."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys), int(m.ullTotalPhys)
            return None
        if os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for ln in f:
                    k, _, v = ln.partition(":")
                    info[k] = int(v.split()[0]) * 1024
            return info.get("MemAvailable", info.get("MemFree", 0)), info.get("MemTotal", 0)
        if sys.platform == "darwin":
            import subprocess
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
            vm = subprocess.check_output(["vm_stat"], text=True)
            page = 4096
            free = 0
            for ln in vm.splitlines():
                if "page size of" in ln:
                    page = int(ln.split("page size of")[1].split()[0])
                if ln.startswith(("Pages free", "Pages inactive", "Pages speculative")):
                    free += int(ln.split(":")[1].strip().rstrip(".")) * page
            return free, total
    except Exception:                                    # noqa: BLE001
        return None
    return None


def is_local(base_url: str) -> bool:
    host = (urllib.parse.urlsplit(base_url or "").hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def heavy_job_risk(base_url: str) -> str | None:
    """Why a heavy job on this server could hurt this machine, or None."""
    if not is_local(base_url):
        return None
    m = memory()
    if not m:
        return None
    free, total = m
    if free < LOW_FREE_BYTES or (total and free / total < LOW_FREE_SHARE):
        return (f"only {free / 1024 ** 3:.1f} GB of {total / 1024 ** 3:.0f} GB memory is free, and "
                "this makes the local model server load ever bigger prompts — it could "
                "freeze the machine")
    return None


def ok_to_run(job: str, base_url: str, ask=input, say=print, interactive: bool = True) -> bool:
    """Check first; ask only when risky (no one to ask → don't run it)."""
    why = heavy_job_risk(base_url)
    if not why:
        return True
    say(f"before {job}: {why}.")
    if not interactive:
        say("  not done (nobody here to confirm) — free some memory, or set the size by hand")
        return False
    try:
        return str(ask("  run it anyway? [y/N] ")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False



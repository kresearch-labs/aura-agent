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


# ---------------------------------------------------- Aura and the machine --
# Aura looks after only what Aura started, never the user's other programs.

def _profile_path():
    from .paths import aura_home
    return aura_home() / "machine.json"


def profile(refresh: bool = False) -> dict:
    """{ram_gb, cpus, at}: read once (at setup), kept in Aura's folder."""
    import json
    import time
    p = _profile_path()
    if not refresh:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    m = memory()
    prof = {"ram_gb": round((m[1] if m else 0) / 1024 ** 3, 1), "cpus": os.cpu_count() or 1,
            "at": round(time.time())}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(prof), encoding="utf-8")
    except OSError:
        pass
    return prof


def suggested_limit(default: int) -> int:
    """How many helpers / background programs Aura runs at once on this
    machine: the option's own default where there is room, fewer on a small
    machine. Callers use it only while the option is unchanged (a value the
    user set always wins)."""
    ram = float(profile().get("ram_gb") or 0)
    if not ram:
        return default
    cap = 2 if ram < 8 else 4 if ram < 16 else default
    return max(1, min(default, cap))


def low_line(total: int) -> int:
    """Free memory under this is 'low', worked out from the machine (not a
    setting): 1.5 GB, or 12 % of the memory on a big machine."""
    return max(int(1.5 * 1024 ** 3), int(total * 0.12))


def _linux_procs() -> dict:
    out = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                st = f.read()
            name = st[st.find("(") + 1:st.rfind(")")]
            ppid = int(st[st.rfind(")") + 2:].split()[1])
            with open(f"/proc/{d}/statm", encoding="utf-8") as f:
                rss = int(f.read().split()[1]) * page
            out[int(d)] = (ppid, rss, name)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _ps_procs() -> dict:
    import subprocess
    out = {}
    try:
        txt = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,comm="], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for ln in txt.splitlines():
        parts = ln.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit():
            out[int(parts[0])] = (int(parts[1]), int(parts[2]) * 1024,
                                  parts[3] if len(parts) > 3 else "")
    return out


def _windows_procs() -> dict:
    """pid → (parent pid, working set bytes, exe name), through the
    process snapshot and each process's memory counters."""
    import ctypes
    from ctypes import wintypes
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    out = {}
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = [int(e.th32ParentProcessID), 0,
                                         e.szExeFile.decode("mbcs", "replace")]
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)

    def rss(pid: int) -> int:
        h = k32.OpenProcess(0x1000, False, pid)          # Limited query
        if not h:
            return 0
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
            return int(pmc.WorkingSetSize) if ok else 0
        finally:
            k32.CloseHandle(h)
    out["_rss"] = rss
    return out


def own_programs() -> list[dict]:
    """Aura itself and every program it started that is still running (its
    descendants), with the memory each holds: [{pid, name, bytes, self}]."""
    me = os.getpid()
    try:
        if sys.platform.startswith("linux"):
            procs = _linux_procs()
        elif os.name == "nt":
            procs = _windows_procs()
        else:
            procs = _ps_procs()
    except Exception:                                    # noqa: BLE001
        return []
    rss_of = procs.pop("_rss", None)
    kids: dict = {}
    for pid, (ppid, _r, _n) in procs.items():
        if pid != ppid:
            kids.setdefault(ppid, []).append(pid)
    mine, todo, seen = [], [me], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        mine.append(pid)
        todo.extend(kids.get(pid, []))
    out = []
    for pid in mine:
        if pid not in procs:
            continue
        _ppid, rss, name = procs[pid]
        if rss_of is not None:
            rss = rss_of(pid)
        out.append({"pid": pid, "name": name, "bytes": rss, "self": pid == me})
    return out


def aura_bytes() -> int:
    return sum(p["bytes"] for p in own_programs())


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def after_task(before_bytes: int | None = None) -> dict | None:
    """(2) The check after a task: code only, no model call. None when the
    machine is fine; else what to tell the user: free/total, what Aura holds
    (the programs it started, biggest first) and whether Aura is the cause."""
    m = memory()
    if not m:
        return None
    free, total = m
    if free >= low_line(total):
        return None
    progs = own_programs()
    aura = sum(p["bytes"] for p in progs)
    started = sorted((p for p in progs if not p["self"]), key=lambda p: -p["bytes"])
    cause = aura >= low_line(total) // 2
    if cause:
        names = ", ".join(p["name"] for p in started[:4])
        words = (f"Memory is low: {_gb(free)} free of {_gb(total)}. Aura and the programs it "
                 f"started hold {_gb(aura)}" + (f" ({names})" if names else "") + ".")
    else:
        words = (f"Memory is low: {_gb(free)} free of {_gb(total)}. Aura isn't the cause "
                 f"(it holds {_gb(aura)}).")
    return {"free": free, "total": total, "aura": aura,
            "grew": (aura - before_bytes) if before_bytes is not None else 0,
            "programs": started, "aura_is_cause": cause, "words": words}


class Watcher:
    """(3) `/machine on`: while a task runs, look at free memory every
    second (no model call); when it drops under the low line, hand the model
    one note (what Aura's own programs hold, and to go lighter), then say
    nothing more until memory has recovered."""

    def __init__(self, put_note, interval: float = 1.0):
        import threading
        self.put_note, self.interval = put_note, interval
        self.stop_event = threading.Event()
        self.thread = None
        self.armed = True

    def start(self) -> None:
        import threading
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="aura-machine")
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def check(self) -> None:
        m = memory()
        if not m:
            return
        free, total = m
        line = low_line(total)
        if self.armed and free < line:
            self.armed = False
            progs = sorted((p for p in own_programs() if not p["self"]), key=lambda p: -p["bytes"])
            held = ", ".join(f"{p['name']} {_gb(p['bytes'])}" for p in progs[:5]) or "none"
            self.put_note(
                f"[MACHINE] memory is low: {_gb(free)} free of {_gb(total)}. Programs Aura "
                f"started that are still running: {held}. Go lighter before the machine runs "
                "out: stop programs you no longer need, run only the related tests or fewer at "
                "once, don't load large files whole.")
        elif not self.armed and free > line * 1.5:
            self.armed = True                          # Recovered: may tell again

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.check()
            except Exception:                          # noqa: BLE001 - never stops a task
                pass



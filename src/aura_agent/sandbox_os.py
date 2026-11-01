"""sandbox_os.py — the OS boundary on every system, no installs.

Gemini CLI: Seatbelt profiles or Docker/Podman. Landlock is a kernel feature
(5.13+): an unprivileged process limits itself and every child — file writes,
and TCP connect/bind from ABI 4 (UDP is not covered).

What this module adds to sandbox.py (bubblewrap → Docker): - macOS:
`sandbox-exec` with a generated profile — writes only to the project and temp
folders; network off unless allowed for the call; - Linux: Landlock in the
child before exec when bubblewrap is missing or refused — the same write
boundary; TCP off on ABI ≥ 4; - Windows: a job object for every command — the
whole process tree is killed when the command ends or times out (no stray
servers or miners); file/network boundary through Microsoft's MXC process
container when the user fetched it (sandbox_win.py), else Docker Desktop — said
plainly; - every OS: `sandbox.required` refuses to run without a boundary.
Standard library only (ctypes).
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------- macOS --
def seatbelt_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _sb_quote(p: str) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(root: Path, network: bool) -> str:
    """Read everywhere; write only the project, temp and /dev; network off
    unless allowed (local sockets stay open — tools talk to local servers)."""
    real = os.path.realpath(str(root))
    tmp = os.path.realpath(os.environ.get("TMPDIR") or "/tmp")
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write* (subpath %s) (subpath %s) (subpath \"/private/tmp\") "
        "(subpath \"/private/var/folders\") (subpath \"/dev\"))" % (_sb_quote(real), _sb_quote(tmp)),
    ]
    if not network:
        rules += ["(deny network-outbound)",
                  "(allow network-outbound (remote unix-socket) (remote ip \"localhost:*\"))"]
    return "\n".join(rules)


def seatbelt_argv(cmd: str, root: Path, network: bool) -> list[str]:
    return ["sandbox-exec", "-p", seatbelt_profile(root, network), "/bin/sh", "-c", cmd]


# ---------------------------------------------------------------- Linux --
# landlock syscalls (same numbers on every architecture: generic table)
_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446
_PR_SET_NO_NEW_PRIVS = 38
_RULE_PATH_BENEATH = 1
_FS = {"execute": 1 << 0, "write_file": 1 << 1, "read_file": 1 << 2, "read_dir": 1 << 3,
       "remove_dir": 1 << 4, "remove_file": 1 << 5, "make_char": 1 << 6, "make_dir": 1 << 7,
       "make_reg": 1 << 8, "make_sock": 1 << 9, "make_fifo": 1 << 10, "make_block": 1 << 11,
       "make_sym": 1 << 12, "refer": 1 << 13, "truncate": 1 << 14}
_NET_CONNECT_TCP = 1 << 1


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


_RULE_NET_PORT = 2


class _NetPort(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """The kernel's Landlock version (0 = none; not Linux = 0)."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        v = _libc().syscall(_SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
        return max(0, int(v))
    except Exception:                                    # noqa: BLE001
        return 0


def _write_rights(abi: int) -> int:
    w = 0
    for k in ("write_file", "remove_dir", "remove_file", "make_char", "make_dir", "make_reg",
              "make_sock", "make_fifo", "make_block", "make_sym"):
        w |= _FS[k]
    if abi >= 2:
        w |= _FS["refer"]
    if abi >= 3:
        w |= _FS["truncate"]
    return w


def landlock_preexec(root: Path, network: bool, proxy_port: int | None = None):
    """A function for subprocess preexec_fn: the child limits itself before
    the command starts (and every child after it inherits it). proxy_port:
    with the network off, TCP may still connect to that one port (Aura's
    site-filtering proxy, netproxy.py)."""
    abi = landlock_abi()
    writable = [str(Path(root).resolve()), "/tmp", "/dev", "/var/tmp"]

    def apply():
        libc = _libc()
        handled = _write_rights(abi)
        attr = _RulesetAttr(handled, _NET_CONNECT_TCP if (abi >= 4 and not network) else 0)
        size = ctypes.sizeof(_RulesetAttr) if abi >= 4 else ctypes.sizeof(ctypes.c_uint64)
        fd = libc.syscall(_SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
        if fd < 0:
            os._exit(126)
        for path in writable:
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            rule = _PathBeneath(handled, pfd)
            libc.syscall(_SYS_ADD, ctypes.c_int(fd), ctypes.c_int(_RULE_PATH_BENEATH),
                         ctypes.byref(rule), ctypes.c_uint32(0))
            os.close(pfd)
        if proxy_port and abi >= 4 and not network:
            prule = _NetPort(_NET_CONNECT_TCP, proxy_port)
            libc.syscall(_SYS_ADD, ctypes.c_int(fd), ctypes.c_int(_RULE_NET_PORT),
                         ctypes.byref(prule), ctypes.c_uint32(0))
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            os._exit(126)
        if libc.syscall(_SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
            os._exit(126)
        os.close(fd)

    return apply, abi


# -------------------------------------------------------------- Windows --
_JOB_LIMIT_KILL_ON_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


def _job_for(pid: int):
    """A job object holding the process: closing it kills the whole tree."""
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class _Ext(ctypes.Structure):
            _fields_ = [("Basic", _Basic), ("Io", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Ext()
        info.Basic.LimitFlags = _JOB_LIMIT_KILL_ON_CLOSE
        k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                    ctypes.byref(info), ctypes.sizeof(info))
        hproc = k32.OpenProcess(0x1F0FFF, False, pid)
        if not hproc or not k32.AssignProcessToJobObject(job, hproc):
            k32.CloseHandle(job)
            return None
        k32.CloseHandle(hproc)
        return job
    except Exception:                                    # noqa: BLE001
        return None


def _close_job(job) -> None:
    if job:
        try:
            ctypes.windll.kernel32.CloseHandle(job)      # Kills what is left of the tree
        except Exception:                                # noqa: BLE001
            pass


def run_in_job(cmd: str, timeout: float, cwd: str | None, env: dict):
    """Windows: the command and everything it starts live in one job; when
    the command ends or times out, nothing it started keeps running."""
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=cwd, env=env, encoding="utf-8", errors="replace")
    job = _job_for(p.pid)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _close_job(job)
        job = None
        p.kill()
        p.communicate()
        raise
    finally:
        _close_job(job)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# ---------------------------------------------------------------- shared --
def required() -> bool:
    """sandbox.required: refuse to run a command without an OS boundary."""
    try:
        from .options import get
        return bool(get("sandbox.required"))
    except Exception:                                    # noqa: BLE001
        return False


def describe() -> str:
    """What protects commands on this machine (for doctor and the notice)."""
    from .sandbox import bwrap_available, docker_available
    if seatbelt_available():
        return "macOS Seatbelt (writes: project + temp; network off unless allowed)"
    if bwrap_available():
        return "bubblewrap (writes: project + /tmp; network off unless allowed)"
    abi = landlock_abi()
    if abi:
        return (f"Landlock v{abi} (writes: project + temp; "
                + ("TCP off unless allowed)" if abi >= 4 else "network NOT limited — kernel too old)"))
    if os.name == "nt":
        from . import sandbox_win as SW
        if SW.ready():
            return ("Windows process container, Microsoft MXC (writes: project + temp; "
                    "network off unless allowed)")
    if docker_available():
        return "Docker (project mounted; network off unless allowed)"
    if os.name == "nt":
        from . import sandbox_win as SW
        return ("a Windows job object only (every process the command starts is stopped "
                "with it) — no file/network boundary: "
                + ("'aura setup safety' can get Microsoft's process container (MXC), "
                   "or start Docker Desktop" if SW.offered() else "start Docker Desktop for one"))
    return f"none on {platform.system()} — commands run with your own rights"

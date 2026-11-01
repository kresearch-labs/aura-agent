"""sandbox_win.py — a file/network boundary for commands on Windows: Microsoft's
MXC process container.

Standard library only (ctypes). Every failure falls back to the old path and
the note says so.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

VERSION = "1.0.1"
PACKAGE_URL = ("https://api.nuget.org/v3-flatcontainer/microsoft.mxc.sdk/"
               f"{VERSION}/microsoft.mxc.sdk.{VERSION}.nupkg")
# from nuget.org's catalog entry for Microsoft.Mxc.Sdk 1.0.1 (packageHash)
PACKAGE_SHA512 = ("o5AvfAA8E/0/eG/ZqAmRlWvkgwF2NW0SQztfNySnjZzz/9TsRiNATzlc4zXz5smAi0VM"
                  "/wFZ2z54PBpRUM3IbQ==")
PACKAGE_SIZE = 14615475
SCHEMA = "1.0.0"
MIN_BUILD = 26100                 # Windows 11 24H2; the exact update is checked by MXC itself


def folder() -> Path:
    from .paths import aura_home
    return aura_home() / "sandbox" / f"mxc-{VERSION}"


def _rid() -> str | None:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64"):
        return "win-x64"
    if m in ("arm64", "aarch64"):
        return "win-arm64"
    return None


def offered() -> bool:
    """Worth offering here: Windows 11 24H2 or newer on x64/ARM64."""
    if os.name != "nt" or _rid() is None:
        return False
    try:
        return sys.getwindowsversion().build >= MIN_BUILD
    except Exception:                                    # noqa: BLE001
        return False


def installed() -> Path | None:
    dll = folder() / "mxc_ffi.dll"
    return dll if dll.is_file() else None


def get(say=print) -> str:
    """Download, check, unpack. Only called after the user said yes."""
    import urllib.request
    rid = _rid()
    if rid is None:
        raise RuntimeError(f"no MXC build for this processor ({platform.machine()})")
    dest = folder()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="mxc-", suffix=".nupkg", dir=str(dest.parent))
    os.close(fd)
    try:
        say(f"  downloading Microsoft.Mxc.Sdk {VERSION} from nuget.org "
            f"({PACKAGE_SIZE / 1e6:.1f} MB)…")
        h = hashlib.sha512()
        got = 0
        req = urllib.request.Request(PACKAGE_URL, headers={"User-Agent": "aura-agent"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > PACKAGE_SIZE:
                    raise RuntimeError("the download is bigger than the published package")
                h.update(chunk)
                f.write(chunk)
        if base64.b64encode(h.digest()).decode() != PACKAGE_SHA512:
            raise RuntimeError("the download doesn't match nuget.org's published SHA-512 "
                               "— nothing was kept")
        prefix = f"runtimes/{rid}/native/"
        stage = dest.with_name(dest.name + ".new")
        if stage.exists():
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        with zipfile.ZipFile(tmp) as z:
            names = [n for n in z.namelist()
                     if n.startswith(prefix) and "/" not in n[len(prefix):] and n != prefix]
            for n in names:
                (stage / n[len(prefix):]).write_bytes(z.read(n))
        if not (stage / "mxc_ffi.dll").is_file():
            raise RuntimeError(f"the package has no {prefix}mxc_ffi.dll")
        if dest.exists():
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        stage.rename(dest)
        _LIB.clear()
        _BACKEND.clear()
        return f"MXC {VERSION} saved to {dest} (SHA-512 matched nuget.org)"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- ctypes --
class _Err(ctypes.Structure):
    _fields_ = [("message", ctypes.c_void_p), ("operation", ctypes.c_void_p),
                ("native_code", ctypes.c_void_p), ("remediation", ctypes.c_void_p)]


class _Result(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int32), ("exit_code", ctypes.c_int32),
                ("timed_out", ctypes.c_int32), ("stdout", ctypes.c_void_p),
                ("stderr", ctypes.c_void_p), ("error", _Err),
                ("metadata", ctypes.c_void_p), ("warnings", ctypes.c_void_p)]


_LIB: dict = {}
_BACKEND: dict = {}


def _text(p) -> str:
    return ctypes.string_at(p).decode("utf-8", "replace") if p else ""


def _lib():
    if "lib" not in _LIB:
        dll = installed()
        lib = None
        if dll is not None:
            try:
                os.add_dll_directory(str(dll.parent))
                lib = ctypes.CDLL(str(dll))
                lib.mxc_run_json.argtypes = [ctypes.c_char_p, ctypes.c_int32,
                                             ctypes.POINTER(_Result)]
                lib.mxc_run_json.restype = ctypes.c_int32
                lib.mxc_run_result_free.argtypes = [ctypes.POINTER(_Result)]
                lib.mxc_available_backends_json.restype = ctypes.c_void_p
                lib.mxc_string_free.argtypes = [ctypes.c_void_p]
            except (OSError, AttributeError):
                lib = None
        _LIB["lib"] = lib
    return _LIB["lib"]


def backend() -> dict | None:
    """MXC's own report of the process container on this machine — only
    the base-container tier (the older AppContainer tiers change folder
    permissions on the host; Aura doesn't use them)."""
    if "pc" not in _BACKEND:
        found = None
        lib = _lib()
        if lib is not None:
            try:
                p = lib.mxc_available_backends_json()
                raw = _text(p)
                if p:
                    lib.mxc_string_free(p)
                for b in json.loads(raw or "[]"):
                    if b.get("backend") == "processcontainer" and b.get("tier") == "base-container":
                        found = b
            except Exception:                            # noqa: BLE001
                found = None
        _BACKEND["pc"] = found
    return _BACKEND["pc"]


def ready() -> bool:
    return os.name == "nt" and installed() is not None and backend() is not None


def proxy_supported() -> bool:
    b = backend() or {}
    return "identitylessLoopbackProxy" in (b.get("capabilities") or [])


# --------------------------------------------------------------- request --
def _readable(root: Path) -> list[str]:
    """What a command may read besides the project: the Python running
    Aura and the folders on PATH (where the user's tools live). Never the
    home folder itself or anything above it — the user's files, keys and
    Aura's own store stay out of reach, like bubblewrap's empty home."""
    home = Path.home().resolve()
    out: list[str] = []
    for d in [sys.base_prefix, sys.prefix] + os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            p = Path(d).resolve()
        except OSError:
            continue
        if not p.is_dir() or p == home or p in home.parents or p == root \
                or root in p.parents:
            continue
        if str(p) not in out:
            out.append(str(p))
    return out


def request(cmd: str, root: Path, cwd: str | None, timeout: float, env: dict,
            network: bool, proxy_port: int | None) -> dict:
    root = Path(root).resolve()
    comspec = env.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    if network:
        net = {"egress": {"default": "allow"}, "ingress": {"default": "deny"}}
    elif proxy_port:
        # MXC's "unpackaged proxy" mapping: the only place a command can
        # connect to is Aura's filtering proxy on 127.0.0.1
        net = {"egress": {"default": "deny"},
               "ingress": {"default": "allow", "hostLoopback": "allow"}}
    else:
        net = {"egress": {"default": "deny"}, "ingress": {"default": "deny"}}
    req = {
        "version": SCHEMA,
        "containment": "processcontainer",
        "process": {
            "commandLine": f'"{comspec}" /d /s /c "{cmd}"',
            "cwd": str(cwd or root),
            "env": [f"{k}={v}" for k, v in env.items() if "=" not in k and k],
            "timeout": max(1, int(timeout * 1000)),
        },
        "filesystem": {
            "readwritePaths": [str(root), tempfile.gettempdir()],
            "readonlyPaths": _readable(root),
        },
        "network": net,
        "telemetry": {"enabled": False},
    }
    if proxy_port and not network:
        req["runtimeConfig"] = {"networkProxy": f"http://127.0.0.1:{proxy_port}"}
    return req


def run(cmd: str, root: Path, cwd: str | None, timeout: float, env: dict,
        network: bool = False, proxy_port: int | None = None):
    """(CompletedProcess, note), or (None, why) when the container could
    not start — the caller falls back and says so."""
    lib = _lib()
    if lib is None:
        return None, "sandbox: the Windows process container (MXC) could not load"
    if proxy_port and not network and not proxy_supported():
        # this Windows build can't pin a command to one loopback proxy:
        # keep the network off rather than open it
        proxy_port = None
        word = "off (allowed sites need a newer Windows update for the container)"
    else:
        word = "on" if network else ("allowed sites only" if proxy_port else "off")
    req = request(cmd, root, cwd, timeout, env, network, proxy_port)
    res = _Result()
    try:
        lib.mxc_run_json(json.dumps(req).encode("utf-8"), 0, ctypes.byref(res))
        if res.status != 0:
            why = _text(res.error.message) or f"status {res.status}"
            fix = _text(res.error.remediation)
            return None, (f"sandbox: the Windows process container could not start "
                          f"({why[:160]}{'; ' + fix[:120] if fix else ''})")
        if res.timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout,
                                            output=_text(res.stdout), stderr=_text(res.stderr))
        proc = subprocess.CompletedProcess(cmd, res.exit_code, _text(res.stdout), _text(res.stderr))
    finally:
        lib.mxc_run_result_free(ctypes.byref(res))
    return proc, (f"sandbox: windows process container (fs={Path(root).resolve()}, "
                  f"network={word})")


# ------------------------------------------------- background programs --
def _stream_api(lib) -> bool:
    if getattr(lib, "_aura_stream", None) is not None:
        return lib._aura_stream
    try:
        P = ctypes.c_void_p
        lib.mxc_spawn_json.argtypes = [ctypes.c_char_p, ctypes.c_int32,
                                       ctypes.POINTER(P), ctypes.POINTER(_Err)]
        lib.mxc_spawn_json.restype = ctypes.c_int32
        for f in ("mxc_sandbox_take_stdin", "mxc_sandbox_take_stdout"):
            getattr(lib, f).argtypes, getattr(lib, f).restype = [P], P
        lib.mxc_sandbox_id.argtypes, lib.mxc_sandbox_id.restype = [P], ctypes.c_uint32
        lib.mxc_stream_read.argtypes = [P, ctypes.c_char_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]
        lib.mxc_stream_write.argtypes = [P, ctypes.c_char_p, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]
        lib.mxc_stream_flush.argtypes = [P]
        lib.mxc_sandbox_try_wait.argtypes = [P] + [ctypes.POINTER(ctypes.c_int32)] * 3
        lib.mxc_sandbox_kill.argtypes = [P]
        for f in ("mxc_sandbox_free", "mxc_read_stream_free", "mxc_write_stream_free"):
            getattr(lib, f).argtypes, getattr(lib, f).restype = [P], None
        lib.mxc_error_detail_free.argtypes = [ctypes.POINTER(_Err)]
        lib._aura_stream = True
    except AttributeError:
        lib._aura_stream = False
    return lib._aura_stream


def inbound_supported() -> bool:
    """A server inside can be reached from Aura on 127.0.0.1."""
    return "ingressHostLoopbackAllow" in ((backend() or {}).get("capabilities") or [])


class _Reader:
    def __init__(self, lib, h):
        self.lib, self.h = lib, h

    def read(self, n: int = 4096) -> bytes:
        if not self.h:
            return b""
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if self.lib.mxc_stream_read(self.h, buf, n, ctypes.byref(got)) != 0:
            return b""
        return buf.raw[:got.value]


class _Writer:
    def __init__(self, lib, h):
        self.lib, self.h = lib, h

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        done = 0
        while self.h and done < len(data):
            n = ctypes.c_size_t(0)
            if self.lib.mxc_stream_write(self.h, data[done:], len(data) - done,
                                         ctypes.byref(n)) != 0 or not n.value:
                raise OSError("the program's input is closed")
            done += n.value
        return done

    def flush(self) -> None:
        if self.h:
            self.lib.mxc_stream_flush(self.h)

    def close(self) -> None:
        if self.h:
            self.lib.mxc_write_stream_free(self.h)        # Sends EOF
            self.h = None


class MxcProc:
    """What tools/process.py needs from a Popen, for a contained program:
    pid, poll(), wait(), returncode, stdin, stdout (stderr goes into it)."""
    own_kill = True

    def __init__(self, lib, handle):
        self.lib, self.h = lib, handle
        self.pid = int(lib.mxc_sandbox_id(handle))
        self.stdout = _Reader(lib, lib.mxc_sandbox_take_stdout(handle))
        self.stdin = _Writer(lib, lib.mxc_sandbox_take_stdin(handle))
        self.returncode: int | None = None
        self._lock = __import__("threading").Lock()

    def poll(self):
        with self._lock:
            if self.returncode is None and self.h:
                ex, run, to = ctypes.c_int32(0), ctypes.c_int32(1), ctypes.c_int32(0)
                if self.lib.mxc_sandbox_try_wait(self.h, ctypes.byref(ex), ctypes.byref(run),
                                                 ctypes.byref(to)) == 0 and not run.value:
                    self.returncode = int(ex.value)
        return self.returncode

    def wait(self, timeout: float | None = None):
        import time as _t
        end = None if timeout is None else _t.monotonic() + timeout
        while self.poll() is None:
            if end is not None and _t.monotonic() > end:
                raise subprocess.TimeoutExpired("(contained program)", timeout)
            _t.sleep(0.1)
        return self.returncode

    def kill(self) -> None:
        """The program and everything it started."""
        with self._lock:
            if self.h and self.returncode is None:
                self.lib.mxc_sandbox_kill(self.h)

    def __del__(self):
        try:
            self.stdin.close()
            if self.stdout.h:
                self.lib.mxc_read_stream_free(self.stdout.h)
            if self.h:
                self.lib.mxc_sandbox_free(self.h)       # Kills a child still running
        except Exception:                                # noqa: BLE001
            pass


def spawn(cmd: str, root: Path, env: dict, network: bool = False,
          proxy_port: int | None = None, port: bool = False):
    """(MxcProc, note) for a program kept running, or (None, why)."""
    lib = _lib()
    if lib is None or not _stream_api(lib):
        return None, "the Windows process container (MXC) could not load"
    if port and not inbound_supported():
        return None, ("this Windows can't reach a server inside the process container "
                      "yet (it needs a newer Windows update)")
    if proxy_port and not network and not proxy_supported():
        proxy_port = None
    req = request(cmd + " 2>&1", root, None, 0, env, network, proxy_port)
    req["process"].pop("timeout", None)                # Runs until stopped
    if port:
        # a server Aura waits on: Aura must reach it on 127.0.0.1
        req["network"]["ingress"] = {"default": "allow", "hostLoopback": "allow"}
    h = ctypes.c_void_p()
    err = _Err()
    st = lib.mxc_spawn_json(json.dumps(req).encode("utf-8"), 0, ctypes.byref(h), ctypes.byref(err))
    if st != 0 or not h.value:
        why = _text(err.message) or f"status {st}"
        lib.mxc_error_detail_free(ctypes.byref(err))
        return None, f"the Windows process container could not start ({why[:160]})"
    word = "on" if network else ("allowed sites only" if proxy_port else "off")
    return MxcProc(lib, h.value), (f"sandbox: windows process container (fs={Path(root).resolve()}, "
                                   f"network={word}{'; reachable on its port' if port else ''})")

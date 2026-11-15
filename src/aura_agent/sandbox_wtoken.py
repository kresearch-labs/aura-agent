"""sandbox_wtoken.py — the basic safe box on Windows: commands can change only the
project (and their own temp folder). No admin, nothing installed.

DORMANT: nothing calls it. Tested on Windows 11: writes inside the project
work, the home folder / Documents / Desktop are refused; git, node, npm and
Python with SSL run. What stopped it. The way on is the strong box: commands
run as a separate local account — restricted_token, _add_ace and BoxPopen are
reused there. To switch on: in sandbox.run_sandboxed (Windows, after MXC), when
works and grant(engagement_root) and grant(box_temp), run with SO.run_tree(...,
popen=BoxPopen) and env_for(env).

How: each command runs with a WRITE-RESTRICTED copy of the user's own Windows
identity. To Windows it is still the user — every program reads and runs as
normal — but a write is allowed only where Aura's own marker (a SID made for
Aura) is also allowed: the project folder and the box's temp folder, granted
once with icacls. The user's other folders, their keys and Aura's own store
can't be changed. Reading and the network stay as they are (Aura's
ask-by-action rule and its site filter guard those). Standard library only
(ctypes).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path

# Aura's marker: a SID no person or group has (Windows refuses capability
# SIDs as restricting SIDs — error 87 — so it has the shape of a domain
# account's, from a fixed hash: three sub-authorities plus a RID)
_H = hashlib.sha256(b"aura-agent workspace write").digest()
CAP_SID = "S-1-5-21-" + "-".join(str(int.from_bytes(_H[i:i + 4], "little"))
                                 for i in range(0, 16, 4))

DISABLE_MAX_PRIVILEGE, LUA_TOKEN, WRITE_RESTRICTED = 0x01, 0x04, 0x08
TOKEN_ALL = 0xF01FF
SE_GROUP_LOGON_ID = 0xC0000000
TokenGroups, TokenDefaultDacl = 2, 6
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


def _dlls():
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a.CreateRestrictedToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.HANDLE)]
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    a.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    a.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    a.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                            ctypes.POINTER(ctypes.c_void_p),
                                            ctypes.POINTER(wintypes.BOOL)]
    a.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                       wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                    ctypes.POINTER(ctypes.c_size_t)]
    k.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                            ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.LocalFree.argtypes = [ctypes.c_void_p]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return a, k


def _fail(what: str):
    raise OSError(f"{what} failed (Windows error {ctypes.get_last_error()})")


_TOKEN_LOCK = threading.Lock()


def restricted_token() -> int:
    """A write-restricted copy of this process's token: restricting SIDs = Aura's
    marker, the logon session, Everyone. Caller closes it.
    """
    a, k = _dlls()
    base = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), TOKEN_ALL, ctypes.byref(base)):
        _fail("OpenProcessToken")
    try:
        need = wintypes.DWORD()
        a.GetTokenInformation(base, TokenGroups, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not a.GetTokenInformation(base, TokenGroups, buf, need, ctypes.byref(need)):
            _fail("GetTokenInformation")
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        groups = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_void_p),
                             ctypes.POINTER(SID_AND_ATTRIBUTES))
        logon = next((groups[i].Sid for i in range(count)
                      if groups[i].Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID), None)
        if not logon:
            raise OSError("no logon session SID in this token")
        cap, world, owner = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if not a.ConvertStringSidToSidW(CAP_SID, ctypes.byref(cap)) or \
                not a.ConvertStringSidToSidW("S-1-1-0", ctypes.byref(world)) or \
                not a.ConvertStringSidToSidW("S-1-3-4", ctypes.byref(owner)):
            _fail("ConvertStringSidToSid")
        try:
            sids = (SID_AND_ATTRIBUTES * 4)()
            for i, s in enumerate((cap.value, logon, world.value, owner.value)):
                sids[i].Sid, sids[i].Attributes = s, 0
            new = wintypes.HANDLE()
            if not a.CreateRestrictedToken(base, DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
                                           0, None, 0, None, 4, sids, ctypes.byref(new)):
                _fail("CreateRestrictedToken")
            # what the command itself creates is its own (and SYSTEM's): the
            # logon SID is one of the restricting SIDs, so it can use them
            sstr = wintypes.LPWSTR()
            a.ConvertSidToStringSidW(logon, ctypes.byref(sstr))
            sd = ctypes.c_void_p()
            if a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"D:(A;;GA;;;{sstr.value})(A;;GA;;;SY)", 1, ctypes.byref(sd), None):
                present, dacl, defaulted = wintypes.BOOL(), ctypes.c_void_p(), wintypes.BOOL()
                a.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                            ctypes.byref(defaulted))
                tdd = TOKEN_DEFAULT_DACL(dacl.value)
                a.SetTokenInformation(new, TokenDefaultDacl, ctypes.byref(tdd), ctypes.sizeof(tdd))
                k.LocalFree(sd)
            k.LocalFree(sstr)
            return new.value
        finally:
            k.LocalFree(cap)
            k.LocalFree(world)
            k.LocalFree(owner)
    finally:
        k.CloseHandle(base)


def _env_block(env) -> ctypes.Array | None:
    if env is None:
        return None
    items = sorted(((str(k), str(v)) for k, v in env.items()), key=lambda kv: kv[0].upper())
    text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


def _create_process(executable, args, _pa, _ta, inherit, flags, env, cwd, startupinfo):
    """_winapi.CreateProcess, but with the restricted token (same arguments,
    same return value: process handle, thread handle, pid, tid)."""
    a, k = _dlls()
    token = restricted_token()
    try:
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        handles = []
        if startupinfo is not None:
            si.StartupInfo.dwFlags = startupinfo.dwFlags
            si.StartupInfo.wShowWindow = startupinfo.wShowWindow
            si.StartupInfo.hStdInput = startupinfo.hStdInput or None
            si.StartupInfo.hStdOutput = startupinfo.hStdOutput or None
            si.StartupInfo.hStdError = startupinfo.hStdError or None
            handles = list((getattr(startupinfo, "lpAttributeList", None) or {}).get("handle_list", []))
        attr = None
        if handles:
            size = ctypes.c_size_t()
            k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attr = ctypes.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
                _fail("InitializeProcThreadAttributeList")
            harr = (wintypes.HANDLE * len(handles))(*handles)
            if not k.UpdateProcThreadAttribute(attr, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, harr,
                                               ctypes.sizeof(harr), None, None):
                _fail("UpdateProcThreadAttribute")
            si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
            inherit = True
        pi = PROCESS_INFORMATION()
        block = _env_block(env)
        cmdline = ctypes.create_unicode_buffer(args)
        ok = a.CreateProcessAsUserW(token, executable, cmdline, None, None, bool(inherit),
                                    flags | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                    block, cwd, ctypes.byref(si), ctypes.byref(pi))
        if attr is not None:
            k.DeleteProcThreadAttributeList(attr)
        if not ok:
            raise OSError(ctypes.get_last_error(), "CreateProcessAsUser failed")
        return pi.hProcess, pi.hThread, pi.dwProcessId, pi.dwThreadId
    finally:
        k.CloseHandle(token)


class _WinapiProxy:
    """subprocess's _winapi, with CreateProcess going through the box for
    the threads that asked (only inside BoxPopen) — other launches untouched."""
    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def CreateProcess(self, *a):                     # noqa: N802 - the _winapi name
        if getattr(self._local, "boxed", False):
            return _create_process(*a)
        return self._real.CreateProcess(*a)


_PROXY: _WinapiProxy | None = None


def _install() -> _WinapiProxy:
    global _PROXY
    with _TOKEN_LOCK:
        if _PROXY is None:
            _PROXY = _WinapiProxy(subprocess._winapi)
            subprocess._winapi = _PROXY
    return _PROXY


class BoxPopen(subprocess.Popen):
    def _execute_child(self, *args, **kw):
        proxy = _install()
        proxy._local.boxed = True
        try:
            return super()._execute_child(*args, **kw)
        finally:
            proxy._local.boxed = False


# ------------------------------------------------------------- the grants --
class _TRUSTEE(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                ("ptstrName", ctypes.c_void_p)]


class _EXPLICIT_ACCESS(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", wintypes.DWORD), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", wintypes.DWORD), ("Trustee", _TRUSTEE)]


def user_sid() -> str:
    """This user's SID (TokenUser)."""
    a, k = _dlls()
    base = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(base)):   # TOKEN_QUERY
        _fail("OpenProcessToken")
    try:
        need = wintypes.DWORD()
        a.GetTokenInformation(base, 1, None, 0, ctypes.byref(need))                  # TokenUser
        buf = ctypes.create_string_buffer(need.value)
        if not a.GetTokenInformation(base, 1, buf, need, ctypes.byref(need)):
            _fail("GetTokenInformation")
        s = wintypes.LPWSTR()
        a.ConvertSidToStringSidW(ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.byref(s))
        out = s.value
        k.LocalFree(s)
        return out
    finally:
        k.CloseHandle(base)


def _add_ace(folder: Path) -> bool:
    """Aura's marker may change this folder and everything in it (an inherited
    "modify" entry).
    """
    a, k = _dlls()
    a.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
    a.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p)]
    a.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p]
    SE_FILE_OBJECT, DACL_INFO = 1, 0x4
    MODIFY = 0x1301BF                         # FILE_GENERIC_READ|WRITE|EXECUTE + DELETE
    sid, me = ctypes.c_void_p(), ctypes.c_void_p()
    old, sd, new = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if not a.ConvertStringSidToSidW(CAP_SID, ctypes.byref(sid)) or             not a.ConvertStringSidToSidW(user_sid(), ctypes.byref(me)):
        return False
    try:
        if a.GetNamedSecurityInfoW(str(folder), SE_FILE_OBJECT, DACL_INFO, None, None,
                                   ctypes.byref(old), None, ctypes.byref(sd)) != 0:
            return False
        ea = (_EXPLICIT_ACCESS * 2)(
            _EXPLICIT_ACCESS(MODIFY, 1, 3, _TRUSTEE(None, 0, 0, 0, sid.value)),   # GRANT, inherit
            _EXPLICIT_ACCESS(MODIFY, 1, 3, _TRUSTEE(None, 0, 0, 0, me.value)))    # TRUSTEE_IS_SID
        if a.SetEntriesInAclW(2, ea, old, ctypes.byref(new)) != 0:
            return False
        return a.SetNamedSecurityInfoW(str(folder), SE_FILE_OBJECT, DACL_INFO, None, None,
                                       new, None) == 0
    finally:
        for p in (new, sd, sid, me):
            if p.value:
                k.LocalFree(p)


def _grants_file() -> Path:
    from .paths import aura_home
    return aura_home() / "sandbox" / "write-grants.json"


def grant(folder: Path, remember: bool = True) -> bool:
    """Let commands in the box change this folder (and what is in it): one
    entry for Aura's marker, inherited by everything inside. Once per folder
    (remembered, unless it's a throwaway check folder)."""
    folder = Path(folder).resolve()
    f = _grants_file()
    try:
        done = set(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()
    if str(folder).lower() in done:
        return True
    if not _add_ace(folder):
        return False
    if not remember:
        return True
    done.add(str(folder).lower())
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(sorted(done)), encoding="utf-8")
    except OSError:
        pass
    return True


def box_temp() -> Path:
    """The box's own temp folder: commands' TEMP, and the caches tools keep
    (pip, npm, uv) — outside it they could not write."""
    p = Path(tempfile.gettempdir()) / "aura-box"
    p.mkdir(parents=True, exist_ok=True)
    return p


def env_for(env: dict) -> dict:
    t = box_temp()
    env = dict(env)
    for k in ("TEMP", "TMP"):
        env[k] = str(t)
    for k, sub in (("PIP_CACHE_DIR", "pip"), ("npm_config_cache", "npm"), ("UV_CACHE_DIR", "uv"),
                   ("XDG_CACHE_HOME", "cache"), ("PYTHONPYCACHEPREFIX", "pycache")):
        env.setdefault(k, str(t / sub))
    return env


_CHECKED: dict = {}


_CHECK_SCRIPT = """import _ssl, _hashlib, ctypes, pathlib, sys
pathlib.Path('in.txt').write_text('x')
try:
    pathlib.Path(sys.argv[1]).write_text('x')
    sys.exit(3)
except OSError:
    sys.exit(0)
"""


def works() -> bool:
    """Checked once per run of Aura: in the box, Python starts with its SSL,
    hashing and ctypes libraries, a write inside the project works, and a
    write to a folder the user CAN write to (outside the project) is refused."""
    if os.name != "nt":
        return False
    if "ok" in _CHECKED:
        return _CHECKED["ok"]
    import secrets
    import shutil
    import sys
    ok = False
    base = Path(tempfile.gettempdir())
    root = base / f"aura-wbox-check-{secrets.token_hex(4)}"
    outside = base / f"aura-wbox-outside-{secrets.token_hex(4)}"
    try:
        root.mkdir()
        outside.mkdir()                          # An ordinary folder: the user may write here
        if grant(root, remember=False) and grant(box_temp()):
            (root / "check.py").write_text(_CHECK_SCRIPT, encoding="utf-8")
            p = BoxPopen([sys.executable, "check.py", str(outside / "out.txt")], cwd=str(root),
                         env=env_for(dict(os.environ)), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
            out, err = p.communicate(timeout=60)
            ok = p.returncode == 0 and (root / "in.txt").exists() and \
                not (outside / "out.txt").exists()
            if not ok:
                _CHECKED["why"] = (err or out or b"").decode("utf-8", "replace").strip()[-300:] \
                    or f"exit {p.returncode}"
    except Exception as e:                               # noqa: BLE001
        _CHECKED["why"] = f"{type(e).__name__}: {e}"
        ok = False
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)
    _CHECKED["ok"] = ok
    return ok

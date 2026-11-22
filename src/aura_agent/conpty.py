"""conpty.py — a real terminal for background programs on Windows.

Linux and macOS give a background program a pseudo-terminal (tools/process.py),
so it prints as it goes and shows its prompts. Over a plain pipe many Windows
programs hold their output back and hide prompts. Windows 10 1809+ has its own
pseudo-terminal, ConPTY; this is a small ctypes binding to it — no add-on.

The known trap (other projects' notes on ConPTY): closing it can hang if its
output is not being read. So: the reader (process._pump) keeps reading the
whole time; when the program exits, a closer thread calls ClosePseudoConsole
while the reader is still draining, which ends the output pipe, and the reader
then sees the end. Output comes as a terminal draws it, so escape codes are
taken out (strip_vt) before the lines are kept.

`spawn()` returns a ConptyProc (a small Popen look-alike: pid, poll, wait,
kill, returncode) with .fd to read and .wfd to type into, or None when ConPTY
is not available (older Windows, or not Windows) — the caller then uses pipes.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading

COLS, ROWS = 400, 60           # Wide, so ordinary lines are not wrapped

_VT = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"          # CSI: colours, cursor moves
                 r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window title
                 r"|\x1b[@-Z\\-_]"                    # Other two-byte codes
                 r"|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]")  # Stray controls (not \t \n \r ESC)


def strip_vt(text: str) -> str:
    """Terminal drawing codes out, the text in."""
    return _VT.sub("", text)


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _STARTF_USESTDHANDLES = 0x00000100
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    _WAIT_OBJECT_0, _WAIT_TIMEOUT, _INFINITE = 0, 0x102, 0xFFFFFFFF

    _k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                ctypes.c_void_p, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                       wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                               ctypes.c_void_p, ctypes.c_size_t,
                                               ctypes.c_void_p, ctypes.c_void_p]
    _k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                    ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                    ctypes.POINTER(_PROCESS_INFORMATION)]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _HAS_CONPTY = hasattr(_k32, "CreatePseudoConsole")
    if _HAS_CONPTY:
        _k32.CreatePseudoConsole.argtypes = [_COORD, wintypes.HANDLE, wintypes.HANDLE,
                                             wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        _k32.CreatePseudoConsole.restype = ctypes.c_long          # HRESULT
        _k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
else:
    _HAS_CONPTY = False


def available() -> bool:
    return _HAS_CONPTY


def _env_block(env: dict) -> ctypes.Array:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


class ConptyProc:
    """A program running in its own ConPTY (the Popen parts Aura uses)."""

    vt = True                     # Its output carries terminal drawing codes

    def __init__(self, pid: int, hproc, hpc, fd: int, wfd: int):
        self.pid, self._h, self._hpc = pid, hproc, hpc
        self.fd, self.wfd = fd, wfd
        self.returncode: int | None = None
        self.stdin = self.stdout = self.stderr = None
        self._lock = threading.Lock()
        threading.Thread(target=self._close_when_done, daemon=True,
                         name=f"conpty-close-{pid}").start()

    def poll(self) -> int | None:
        if self.returncode is None and self._h:
            if _k32.WaitForSingleObject(self._h, 0) == _WAIT_OBJECT_0:
                code = wintypes.DWORD()
                _k32.GetExitCodeProcess(self._h, ctypes.byref(code))
                self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        ms = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        if self.returncode is None and self._h:
            if _k32.WaitForSingleObject(self._h, ms) == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
        return self.poll() if self.returncode is None else self.returncode

    def kill(self) -> None:
        if self.poll() is None and self._h:
            _k32.TerminateProcess(self._h, 1)

    terminate = kill

    def _close_when_done(self) -> None:
        """When the program ends, close the pseudo-console (the reader is
        still draining it, so this cannot hang); its output pipe then ends and
        the reader stops. Then let go of the handles."""
        try:
            self.wait()
        except Exception:                            # noqa: BLE001
            pass
        with self._lock:
            hpc, self._hpc = self._hpc, None
        if hpc:
            try:
                _k32.ClosePseudoConsole(hpc)
            except Exception:                        # noqa: BLE001
                pass
        for fd in (self.wfd,):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _k32.CloseHandle(self._h)
        except Exception:                            # noqa: BLE001
            pass


def spawn(cmd: str, cwd: str, env: dict) -> ConptyProc | None:
    """Start `cmd` (through the system shell, like subprocess shell=True) in a
    new ConPTY. None when ConPTY is not available or the start fails."""
    if not _HAS_CONPTY:
        return None
    in_r, in_w = wintypes.HANDLE(), wintypes.HANDLE()
    out_r, out_w = wintypes.HANDLE(), wintypes.HANDLE()
    if not _k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0):
        return None
    if not _k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0):
        for h in (in_r, in_w):
            _k32.CloseHandle(h)
        return None
    hpc = ctypes.c_void_p()
    hr = _k32.CreatePseudoConsole(_COORD(COLS, ROWS), in_r, out_w, 0, ctypes.byref(hpc))
    # the pseudo-console holds its own copies of these two ends
    _k32.CloseHandle(in_r)
    _k32.CloseHandle(out_w)
    if hr != 0:
        for h in (in_w, out_r):
            _k32.CloseHandle(h)
        return None
    size = ctypes.c_size_t()
    _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    si = _STARTUPINFOEXW()
    si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    # with no handles given, the program uses the pseudo-console — not
    # whatever Aura's own output is redirected to
    si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
    pi = _PROCESS_INFORMATION()
    ok = False
    try:
        if not _k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList")
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        if not _k32.UpdateProcThreadAttribute(si.lpAttributeList, 0,
                                              _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                              hpc, ctypes.sizeof(ctypes.c_void_p), None, None):
            raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute")
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        # the same command line subprocess builds for shell=True
        line = ctypes.create_unicode_buffer(f'{comspec} /c "{cmd}"')
        block = _env_block(env)
        ok = bool(_k32.CreateProcessW(None, line, None, None, False,
                                      _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                                      block, cwd, ctypes.byref(si), ctypes.byref(pi)))
    except OSError:
        ok = False
    finally:
        try:
            _k32.DeleteProcThreadAttributeList(attrs)
        except Exception:                            # noqa: BLE001
            pass
    if not ok:
        _k32.ClosePseudoConsole(hpc)
        for h in (in_w, out_r):
            _k32.CloseHandle(h)
        return None
    _k32.CloseHandle(pi.hThread)
    fd = msvcrt.open_osfhandle(out_r.value, os.O_RDONLY | os.O_BINARY)
    wfd = msvcrt.open_osfhandle(in_w.value, os.O_WRONLY | os.O_BINARY)
    return ConptyProc(int(pi.dwProcessId), pi.hProcess, hpc, fd, wfd)

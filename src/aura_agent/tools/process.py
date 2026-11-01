"""process.py — commands that keep running: dev servers, watchers, long builds.

  - their failures (bug reports): a second dev server started on a busy
    port because the first was forgotten; killing the wrapping shell left
    the real server running (orphan holding the port); after a crash
    nobody finds the leftovers; an agent stuck on a blocking server call;
  - beyond agents: VS Code background tasks are "ready" when the output
    matches a pattern; Docker Compose / wait-on: ready when a port opens or
    a URL answers (health checks); supervisord startsecs: started only if
    it stays up a few seconds; Overmind: restart one process alone;
    Windows job objects (kill-on-close) and POSIX process groups: the OS

What Aura does with that:
  - process_start goes through the mediator exactly like run_command
    (scope, risk grade — `git push` still asks —, skill-pack command
    limits, banned commands, sandbox when available);
  - ready when the output says so, a port opens or a local URL answers —
    or, with nothing given, when it is still up after 3 s; a crash while
    starting comes back at once with its last lines;
  - the same command already running, or the port already taken, is
    refused with what holds it (no duplicate servers);
  - every process is a whole tree: stop kills all of it; on Windows a job
    object kills it even if Aura crashes; processes not marked `keep` stop
    when Aura exits;
  - a process that ends while Aura works is reported to the model at its
    next step (no polling); `process wait` waits for text / a port / exit;
  - output: new lines since the last read, secrets scrubbed, the full log
    kept in the notebook; leftovers from an earlier session are listed and
    can be stopped — only if the PID still belongs to that process.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from ..tool_names import PROCESS, PROCESS_INPUT, PROCESS_START

MAX_RUNNING = 8
STARTSECS = 3.0
WAIT_MAX = 600
OUT_BUDGET = 6_000
_IS_WIN = sys.platform == "win32"
_ALL: list["Registry"] = []          # For the exit cleanup


# --------------------------------------------------------------- the OS --
def _win_job(pid: int):
    """A Windows job object that kills the process tree when its last
    handle closes — i.e. when Aura exits or crashes. Returns the handle
    (kept open) or None."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _Ext(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)       # Not inheritable
        if not job:
            return None
        info = _Ext()
        info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        h = k32.OpenProcess(0x0101, False, pid)          # SET_QUOTA | TERMINATE
        if not h:
            k32.CloseHandle(job)
            return None
        ok = k32.AssignProcessToJobObject(job, h)
        k32.CloseHandle(h)
        if not ok:
            k32.CloseHandle(job)
            return None
        return job
    except Exception:                                # noqa: BLE001
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _IS_WIN:
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32")
            h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return code.value == 259                     # STILL_ACTIVE
        except Exception:                            # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def started_at(pid: int) -> float | None:
    """When the process with this PID started (epoch seconds), to tell a
    leftover of ours from another program that got the same PID later."""
    if _IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32")
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            c, e, kk, u = (wintypes.FILETIME() for _ in range(4))
            ok = k32.GetProcessTimes(wintypes.HANDLE(h), ctypes.byref(c), ctypes.byref(e),
                                     ctypes.byref(kk), ctypes.byref(u))
            k32.CloseHandle(wintypes.HANDLE(h))
            if not ok:
                return None
            t = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return t / 1e7 - 11644473600.0
        except Exception:                            # noqa: BLE001
            return None
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if not out:
            return None
        days = 0
        if "-" in out:
            d, out = out.split("-", 1)
            days = int(d)
        parts = [int(x) for x in out.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        secs = days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
        return time.time() - secs
    except Exception:                                # noqa: BLE001
        return None


def kill_tree(pid: int) -> None:
    """The process and everything it started."""
    if _IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        return
    try:
        pg = os.getpgid(pid)
        os.killpg(pg, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            if not _alive(pid):
                break
        else:
            os.killpg(pg, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _kill(p) -> None:
    """Stop a program and everything it started: the process container's
    own kill for a contained one (sandbox_win.MxcProc), else by its pid."""
    if getattr(p.popen, "own_kill", False):
        p.popen.kill()
    else:
        kill_tree(p.pid)


def port_open(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            continue
    return False


_LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


def url_answers(url: str) -> bool:
    """Any HTTP answer below 500 counts (a 404 means the server is up)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"),
                                    timeout=2) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:                                # noqa: BLE001
        return False


# ------------------------------------------------------------- registry --
class Proc:
    def __init__(self, pid_: str, name: str, cmd: str, popen, log: Path, keep: bool,
                 port: int | None, job=None, note: str = "", fd: int | None = None):
        self.id, self.name, self.cmd = pid_, name, cmd
        self.popen, self.pid = popen, popen.pid
        self.log, self.keep, self.port, self.job = log, keep, port, job
        self.started = time.time()
        self.lines: deque[str] = deque(maxlen=3000)
        self.total = 0
        self.read_upto = 0            # Line count already shown to the model
        self.exit_code: int | None = None
        self.ended: float | None = None
        self.reported = False         # Its end was told to the model
        self.note = note
        self.lock = threading.Lock()
        self.fd = fd                  # The pty (Linux/macOS) to read and type into
        self.partial = ""             # The unfinished last line (a prompt)
        self.last_out = time.monotonic()
        self.prompt_told = ""         # The prompt already reported to the model

    def running(self) -> bool:
        if self.exit_code is None and self.popen.poll() is not None:
            self.exit_code = self.popen.returncode
            self.ended = self.ended or time.time()
        return self.exit_code is None

    def tail(self, n: int) -> list[str]:
        with self.lock:
            out = list(self.lines)[-n:]
            return out + [self.partial] if self.partial else out

    def waiting_prompt(self) -> str:
        """The question it stopped at ('Use 3001 instead? (Y/n)'), or ''."""
        with self.lock:
            part = self.partial.strip()
            quiet = time.monotonic() - self.last_out
        if part and quiet >= QUIET_FOR_PROMPT and _PROMPT.search(part) and self.running():
            return part[-200:]
        return ""

    def type_in(self, text: str) -> None:
        data = text.encode("utf-8")
        if self.fd is not None:
            os.write(self.fd, data)
        else:
            self.popen.stdin.write(data)
            self.popen.stdin.flush()

    def new_lines(self) -> list[str]:
        with self.lock:
            fresh = self.total - self.read_upto
            out = list(self.lines)[-fresh:] if fresh > 0 else []
            skipped = max(0, fresh - len(self.lines))
            self.read_upto = self.total
            if self.partial:
                out.append(self.partial)
        if skipped:
            out.insert(0, f"[{skipped} earlier lines are only in the log: {self.log}]")
        return out


def _short(cmd: str) -> str:
    """'"C:/Python314/python.exe" -m http.server 8000' -> 'python -m http.server 8000'"""
    import shlex
    try:
        parts = shlex.split(cmd, posix=not _IS_WIN)
    except ValueError:
        parts = cmd.split()
    if not parts:
        return cmd[:40]
    first = re.split(r"[\\/]", parts[0].strip('"'))[-1]
    first = re.sub(r"\.(exe|cmd|bat)$", "", first, flags=re.I)
    return " ".join([first] + [x.strip('"') for x in parts[1:]])[:48]


def _scrub(lines: list[str]) -> list[str]:
    try:
        from ..secrets_guard import scrub_secrets
        return scrub_secrets("\n".join(lines)).split("\n") if lines else []
    except Exception:                                # noqa: BLE001
        return ["[output withheld: the secret scrub failed]"]


def _fit(lines: list[str], budget: int = OUT_BUDGET) -> str:
    text = "\n".join(lines)
    if len(text) <= budget:
        return text
    head = text[: budget // 4]
    tail = text[-(budget * 3 // 4):]
    return f"{head}\n[... {len(text) - len(head) - len(tail):,} characters left out ...]\n{tail}"


def _ago(t: float) -> str:
    s = int(time.time() - t)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60:02d}m"


class Registry:
    def __init__(self, notebook: Path, workspace: Path):
        self.nb, self.ws = Path(notebook), Path(workspace)
        self.proxy_env = lambda: {}          # Set by register: the site-filtering proxy
        self.dir = self.nb / "processes"
        self.procs: dict[str, Proc] = {}
        self.lock = threading.Lock()
        _ALL.append(self)
        # processes not marked `keep` were meant to stop with Aura; if a
        # crash left them running (Linux/macOS have no kill-on-crash that
        # is safe here), stop them now — only when the PID still belongs
        # to that very process
        self.cleaned: list[str] = []
        for r in self.leftovers():
            if not r.get("keep"):
                kill_tree(int(r["pid"]))
                self._remember(forget=r["id"])
                self.cleaned.append(f"{r['id']} '{r['cmd'][:60]}'")

    def _seen_end(self, p: "Proc") -> None:
        """The model has seen that p ended: no separate report later."""
        if not p.running() and not p.reported:
            p.reported = True
            self._remember(forget=p.id)

    # -- persistence (so a later session finds leftovers)
    def _file(self) -> Path:
        return self.dir / "processes.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"next": 1, "procs": {}}

    def _save(self, d: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._file().write_text(json.dumps(d, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _remember(self, p: Proc | None = None, forget: str = "") -> str:
        with self.lock:
            d = self._load()
            if p is None and not forget:
                nid = f"p{d.get('next', 1)}"
                d["next"] = d.get("next", 1) + 1
                self._save(d)
                return nid
            if forget:
                d["procs"].pop(forget, None)
            if p is not None:
                d["procs"][p.id] = {"name": p.name, "cmd": p.cmd, "pid": p.pid,
                                    "started": p.started, "keep": p.keep,
                                    "port": p.port, "log": str(p.log),
                                    # which Aura started it: another live Aura
                                    # (aura fleet) keeps its own processes
                                    "owner": os.getpid(),
                                    "owner_started": started_at(os.getpid())}
            self._save(d)
            return ""

    def leftovers(self) -> list[dict]:
        """Processes recorded by an earlier session that are still running
        (same PID AND same start time — not a stranger with a reused PID)."""
        out = []
        for pid_, r in self._load().get("procs", {}).items():
            if pid_ in self.procs:
                continue
            own, own_st = int(r.get("owner") or 0), r.get("owner_started")
            if own and own != os.getpid() and _alive(own):
                now_st = started_at(own)
                if own_st is None or now_st is None or abs(now_st - float(own_st)) < 5:
                    continue          # Its Aura is still running: not a leftover
            st = started_at(int(r.get("pid", 0)))
            if st is not None and _alive(int(r["pid"])) and abs(st - float(r["started"])) < 5:
                out.append({"id": pid_, **r})
            else:
                self._remember(forget=pid_)
        return out

    # -- start
    def start(self, cmd: str, name: str = "", ready: dict | None = None,
              timeout: float = 60, keep: bool = False, restart: bool = False,
              network: bool = False) -> str:
        norm = " ".join(cmd.split())
        running = [p for p in self.procs.values() if p.running()]
        same = [p for p in running if " ".join(p.cmd.split()) == norm]
        if same and not restart:
            p = same[0]
            return (f"not started: the same command is already running as {p.id} "
                    f"(pid {p.pid}, up {_ago(p.started)}"
                    + (f", port {p.port}" if p.port else "")
                    + "). Use it, or start again with restart=true.")
        for p in same:
            self.stop(p.id)
        ready = ready or {}
        port = ready.get("port")
        url = ready.get("url")
        if url:
            host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
            if host not in _LOCAL:
                raise Violation("ready.url must be a local address (localhost / "
                                "127.0.0.1) — checking other sites isn't a readiness check")
            if not port:
                port = urllib.parse.urlsplit(str(url)).port
        if port:
            port = int(port)
            if port_open(port):
                holder = [p for p in running if p.port == port]
                return (f"not started: port {port} is already in use"
                        + (f" — by {holder[0].id} ('{holder[0].cmd}')" if holder
                           else " by another program (not started by Aura)")
                        + ". Stop it first, or use another port.")
        left = [p for p in running if p not in same]
        try:
            from ..options import get as _opt
            cap = int(_opt("background.max_running"))
        except Exception:                            # noqa: BLE001
            cap = MAX_RUNNING
        if len(left) >= cap:
            raise Violation(f"{cap} processes are already running — stop one first "
                            "(the limit is the option background.max_running)")
        pid_ = self._remember()
        self.dir.mkdir(parents=True, exist_ok=True)
        log = self.dir / f"{pid_}.log"
        # A server Aura waits on by its port must be reachable from Aura: in a
        # sandbox with the network cut off (bubblewrap --unshare-net) its
        # localhost is a different one and it never looks ready. Files stay
        # sandboxed; the command was already checked like run_command (outward
        # sends ask first).
        popen, note, fd = _spawn(cmd, self.ws, self.nb, network,
                                 proxy_env=None if network else self.proxy_env(),
                                 port=bool(port))
        job = (_win_job(popen.pid) if (_IS_WIN and not keep
                                       and not getattr(popen, "own_kill", False)) else None)
        p = Proc(pid_, name or _short(cmd), cmd, popen, log, keep, port, job, note, fd)
        self.procs[pid_] = p
        self._remember(p)
        threading.Thread(target=_pump, args=(p,), daemon=True).start()
        how, ok = self._wait_ready(p, ready, port, url, min(float(timeout or 60), WAIT_MAX))
        if not p.running():
            p.reported = True               # The start result says it
            self._remember(forget=p.id)
        lines = _scrub(p.new_lines())
        head = (f"{'started' if ok else 'NOT ready'}: {p.id} '{p.name}' (pid {p.pid}) — {how}"
                + (f" · {note}" if note else "")
                + (" · keeps running after Aura exits" if keep else ""))
        return head + ("\noutput so far:\n" + _fit(lines[-40:]) if lines else "")

    def _wait_ready(self, p: Proc, ready: dict, port, url, timeout: float) -> tuple[str, bool]:
        text = ready.get("text")
        rx = re.compile(str(text)) if text else None
        t0 = time.monotonic()
        limit = timeout if (rx or port or url) else STARTSECS
        while time.monotonic() - t0 < limit:
            if not p.running():
                return (f"exited with code {p.exit_code} after {time.monotonic() - t0:.1f}s", False)
            q = p.waiting_prompt()
            if q:
                p.prompt_told = q
                return (f"WAITING FOR INPUT after {time.monotonic() - t0:.1f}s: {q!r} — answer "
                        f"with process_input(id='{p.id}', text=...), or ask the user", False)
            if rx and any(rx.search(ln) for ln in p.tail(200)):
                return (f"ready: output matched {text!r} after {time.monotonic() - t0:.1f}s", True)
            if port and port_open(port):
                return (f"ready: port {port} open after {time.monotonic() - t0:.1f}s", True)
            if url and url_answers(url):
                return (f"ready: {url} answers after {time.monotonic() - t0:.1f}s", True)
            time.sleep(0.2)
        if not (rx or port or url):
            return (f"running (still up after {STARTSECS:.0f}s; no ready check given)", True)
        what = f"output {text!r}" if rx else f"port {port}" if port else url
        return (f"still running but not ready after {timeout:.0f}s (waiting for {what}) — "
                "check its output with process(action='output')", False)

    # -- the other actions
    def find(self, pid_: str) -> Proc:
        p = self.procs.get(pid_) or next((x for x in self.procs.values() if x.name == pid_), None)
        if p is None:
            raise Violation(f"no process {pid_!r} — process(action='list') shows them")
        return p

    def listing(self) -> str:
        rows = []
        if self.cleaned:
            rows.append("stopped at start (left running by a session that crashed): "
                        + ", ".join(self.cleaned))
            self.cleaned = []
        for p in self.procs.values():
            self._seen_end(p)
            q = p.waiting_prompt()
            state = ("WAITING" if q else "running") if p.running() else f"exited {p.exit_code}"
            last = [ln for ln in _scrub(p.tail(3)) if ln.strip()]
            rows.append(f"{p.id}  {state:10s} up {_ago(p.started):8s} pid {p.pid}"
                        + (f" port {p.port}" if p.port else "") + f"  {p.cmd[:80]}"
                        + ("  [keep]" if p.keep else "")
                        + "".join(f"\n      {ln[:160]}" for ln in last))
        for r in self.leftovers():
            rows.append(f"{r['id']}  LEFT RUNNING from an earlier session (pid {r['pid']}, "
                        f"up {_ago(float(r['started']))}) {r['cmd'][:80]} — stop it with "
                        f"process(action='stop', id='{r['id']}')")
        return "\n".join(rows) if rows else "no background processes"

    def output(self, pid_: str, tail: int | None = None, grep: str = "") -> str:
        p = self.find(pid_)
        lines = p.tail(int(tail)) if tail else p.new_lines()
        if grep:
            rx = re.compile(grep)
            lines = [ln for ln in lines if rx.search(ln)]
        state = "running" if p.running() else f"exited with code {p.exit_code}"
        self._seen_end(p)
        body = _fit(_scrub(lines)) if lines else "(no new output)"
        return f"{p.id} '{p.name}' — {state}\n{body}"

    def wait(self, pid_: str, for_: dict, timeout: float) -> str:
        p = self.find(pid_)
        timeout = min(max(1.0, float(timeout or 60)), WAIT_MAX)
        what = for_ or {"exit": True}
        if "url" in what:
            host = (urllib.parse.urlsplit(str(what["url"])).hostname or "").lower()
            if host not in _LOCAL:
                raise Violation("wait for a local URL only (localhost / 127.0.0.1)")
        rx = re.compile(str(what["text"])) if what.get("text") else None
        if rx and any(rx.search(ln) for ln in p.tail(500)):
            return (f"{p.id}: {what['text']!r} is already in its output\n"
                    + _fit(_scrub(p.new_lines() or p.tail(20))))
        seen = p.total
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if rx:
                fresh = p.tail(max(0, p.total - seen) or 1) if p.total > seen else []
                if any(rx.search(ln) for ln in fresh):
                    return f"{p.id}: output matched after {time.monotonic() - t0:.1f}s\n" + \
                        _fit(_scrub(p.new_lines()))
            if what.get("port") and port_open(int(what["port"])):
                return f"{p.id}: port {what['port']} open after {time.monotonic() - t0:.1f}s"
            if what.get("url") and url_answers(str(what["url"])):
                return f"{p.id}: {what['url']} answers after {time.monotonic() - t0:.1f}s"
            if not p.running():
                self._seen_end(p)
                return (f"{p.id} exited with code {p.exit_code} after "
                        f"{time.monotonic() - t0:.1f}s\n" + _fit(_scrub(p.new_lines())))
            time.sleep(0.2)
        return (f"{p.id}: still waiting after {timeout:.0f}s — it is "
                f"{'running' if p.running() else 'stopped'}\n" + _fit(_scrub(p.new_lines())))

    def stop(self, pid_: str) -> str:
        if pid_ == "all":
            ids = [p.id for p in self.procs.values() if p.running()]
            return "\n".join(self.stop(i) for i in ids) if ids else "nothing running"
        p = self.procs.get(pid_)
        if p is None:
            left = {r["id"]: r for r in self.leftovers()}
            if pid_ in left:
                kill_tree(int(left[pid_]["pid"]))
                self._remember(forget=pid_)
                return f"stopped {pid_} (left running from an earlier session)"
            p = self.find(pid_)
        if p.running():
            _kill(p)
            try:
                p.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            p.running()
        p.reported = True
        self._remember(forget=p.id)
        last = _scrub(p.tail(5))
        return f"stopped {p.id} '{p.name}' (exit {p.exit_code})" + \
            ("\nlast lines:\n" + "\n".join(last) if last else "")

    def prompts_untold(self) -> list[tuple[Proc, str]]:
        out = []
        for p in self.procs.values():
            q = p.waiting_prompt()
            if q and q != p.prompt_told:
                p.prompt_told = q
                out.append((p, q))
        return out

    def type_in(self, pid_: str, text: str, enter: bool = True, ctrl_c: bool = False) -> str:
        p = self.find(pid_)
        if not p.running():
            return f"{p.id} is not running (exit {p.exit_code})"
        q = p.waiting_prompt() or (p.partial.strip() if p.partial else "")
        if q and _SECRET_PROMPT.search(q):
            return (f"refused: {p.id} is asking for a secret ({q!r}). Aura never types "
                    "passwords, tokens or codes — tell the user to answer it themselves "
                    "(or stop the process).")
        if ctrl_c:
            p.type_in("\x03")
        else:
            p.type_in(text + ("\r\n" if _IS_WIN else "\n") * (1 if enter else 0))
        p.prompt_told = ""
        time.sleep(0.5)
        return f"typed into {p.id}" + (" (Ctrl-C)" if ctrl_c else f": {text!r}") + \
            "\n" + _fit(_scrub(p.new_lines()[-30:]))

    def ended_unreported(self) -> list[Proc]:
        out = []
        for p in self.procs.values():
            if not p.reported and not p.running():
                p.reported = True
                self._remember(forget=p.id)
                out.append(p)
        return out

    def shutdown(self) -> None:
        """Aura is exiting: stop what wasn't asked to keep running."""
        for p in list(self.procs.values()):
            if not p.keep and p.running():
                try:
                    _kill(p)
                except Exception:                    # noqa: BLE001
                    pass
                self._remember(forget=p.id)


_PROMPT = re.compile(r"(\(y/n\)|\[y/n\]|\(yes/no\)|\[yes/no\]|[?:>\]\)]\s*$)", re.I)
_SECRET_PROMPT = re.compile(r"\b(password|passphrase|pass code|passcode|token|secret|"
                            r"pin|otp|2fa|verification code)\b", re.I)
QUIET_FOR_PROMPT = 1.0    # Seconds without output before a question counts as waiting


def _spawn(cmd: str, workspace: Path, notebook: Path, network: bool,
           proxy_env: dict | None = None, port: bool = False):
    """Start in its own process group, output merged, in the OS sandbox like
    run_command. Order: Seatbelt (macOS) → bubblewrap → Landlock → none
    (refused when sandbox.required is on). network: the user allowed the full
    network; port: a server Aura must reach on its port — Seatbelt and Landlock
    keep the same localhost, so only bubblewrap needs its network left on for
    it. Linux/macOS: a pseudo-terminal, so a program shows its prompts (over a
    pipe many hold them back — pexpect's lesson). Windows: pipes (a ConPTY
    needs a dependency). Returns (popen, note, fd to read and write).
    """
    from ..sandbox import _SANDBOX_DISABLED, scrubbed_env, wrap_argv
    from .. import sandbox_os as SO
    env = scrubbed_env()
    # over a pipe Python holds its output back (block buffering) — the
    # answer to a prompt would sit unseen (pexpect's lesson; the pty fixes
    # it for everything on Linux/macOS, this for Python on Windows)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.update(proxy_env or {})
    proxy_port = None
    if proxy_env and not network:
        try:
            proxy_port = int(str(proxy_env.get("HTTP_PROXY", "")).rsplit(":", 1)[1])
        except (IndexError, ValueError):
            proxy_port = None
    sites = proxy_port is not None
    net_word = "on" if network else ("allowed sites only" if sites else "off")
    argv, note, pre = (None, "", None)
    if _IS_WIN:
        why = ""
        if os.environ.get("AURA_SANDBOX") != _SANDBOX_DISABLED:
            from .. import sandbox_win as SW
            if SW.ready():
                if proxy_port and not (network or port) and SW.proxy_supported():
                    from ..netproxy import local_through
                    local_through(env)           # Only the proxy is reachable
                mp, why = SW.spawn(cmd, workspace, env, network, proxy_port, port)
                if mp is not None:
                    return mp, why, None
        if SO.required():
            raise Violation("refused: sandbox.required is on and this program can't run "
                            "in a sandbox here" + (f" ({why})" if why else
                                                   f" ({SO.describe()})"))
        kw = dict(cwd=str(workspace), env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, stdin=subprocess.PIPE, bufsize=0,
                  creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                 | getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        pop = subprocess.Popen(cmd, shell=True, **kw)
        return pop, ("sandbox: none on Windows — a job object; network through Aura's proxy "
                     "for programs that use the proxy setting (not enforced)" if sites else note), None
    import pty
    off = os.environ.get("AURA_SANDBOX") == _SANDBOX_DISABLED
    if SO.seatbelt_available() and not off:
        argv = SO.seatbelt_argv(cmd, workspace, network)
        note = f"sandbox: seatbelt (fs={workspace}, network={net_word})"
    else:
        argv, note = wrap_argv(cmd, workspace, network=network or port)
        if argv is not None and sites and not (network or port):
            # bubblewrap cut the network off: the same relay as run_command
            import shlex
            import sys as _sys
            from ..netproxy import RELAY, _PROXY
            if _PROXY is not None:
                sock = _PROXY.unix_socket(Path("/tmp"))
                from ..sandbox import add_bind
                add_bind(argv, sock)
                argv[-1] = (f"{shlex.quote(_sys.executable)} -c {shlex.quote(RELAY)} {proxy_port} "
                            f"{shlex.quote(sock)} >/dev/null 2>&1 & sleep 0.2; {argv[-1]}")
                note = note.replace("network=off", "network=allowed sites only")
                from ..netproxy import local_through
                local_through(env)
        elif argv is None and SO.landlock_abi() and not off:
            # Landlock limits TCP by port, not host: a server (port) may need
            # local services (a database) — its files stay fenced, its
            # outgoing traffic gets the proxy setting
            pre, abi = SO.landlock_preexec(workspace, network or port,
                                           proxy_port=None if port else proxy_port)
            if sites and not (network or port) and abi >= 4:
                from ..netproxy import local_through
                local_through(env)               # Only the proxy's port is open
            note = (f"sandbox: landlock v{abi} (fs={workspace}, network="
                    + ("on (a server; proxy setting for outgoing)" if port and not network
                       else net_word if abi >= 4 or network else "NOT limited") + ")")
        elif argv is None:
            if SO.required():
                raise Violation("refused: sandbox.required is on and no OS sandbox is "
                                   f"available here ({SO.describe()})")
            note = "sandbox: not available — runs unsandboxed"
    master, slave = pty.openpty()
    kw = dict(cwd=str(workspace), env=env, stdin=slave, stdout=slave, stderr=slave,
              start_new_session=True, close_fds=True)
    if pre is not None:
        kw["preexec_fn"] = pre
    try:
        pop = (subprocess.Popen(argv, **kw) if argv is not None
               else subprocess.Popen(cmd, shell=True, **kw))
    finally:
        os.close(slave)
    return pop, note, master


def _pump(p: Proc) -> None:
    """Reader thread: output as it comes — whole lines into memory and the
    log, the unfinished last line kept as `partial` (a prompt has no
    newline: reading by lines would never show it)."""
    import codecs
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        with open(p.log, "a", encoding="utf-8") as f:
            while True:
                try:
                    # the pty, or the unbuffered pipe: both give what is there
                    chunk = (os.read(p.fd, 4096) if p.fd is not None
                             else p.popen.stdout.read(4096))
                except OSError:
                    break                               # The pty closes with the child
                if not chunk:
                    break
                text = dec.decode(chunk).replace("\r\n", "\n").replace("\r", "\n")
                with p.lock:
                    p.last_out = time.monotonic()
                    buf = p.partial + text
                    *done, p.partial = buf.split("\n")
                    for line in done:
                        p.lines.append(line)
                        p.total += 1
                        f.write(line + "\n")
                f.flush()
            if p.partial:
                with p.lock:
                    p.lines.append(p.partial)
                    p.total += 1
                    f.write(p.partial + "\n")
                    p.partial = ""
    except Exception:                                # noqa: BLE001
        pass
    finally:
        p.running()
        if p.fd is not None:
            try:
                os.close(p.fd)
            except OSError:
                pass
            p.fd = None


# ------------------------------------------------------------------ tools --
def register(mediator: Mediator, dispatch: dict, notebook: Path, workspace: Path) -> Registry:
    from .shell import DEFAULT_BANNED
    reg = Registry(notebook, workspace)
    mediator._procs = reg

    def _proxy_env() -> dict:
        try:
            from .. import netproxy
            from .shell import _site_ok_for
            return netproxy.env_for(netproxy.get(_site_ok_for(mediator)).port)
        except Exception:                               # noqa: BLE001
            return {}
    reg.proxy_env = _proxy_env

    def process_start(args) -> str:
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            raise Violation("process_start: give cmd")
        low = cmd.lower()
        for tok in DEFAULT_BANNED:
            if tok.lower() in low:
                raise Violation(f"banned command token '{tok}'")
        ready = args.get("ready") or {}
        if isinstance(ready, str):
            ready = {"text": ready}
        return reg.start(cmd, name=str(args.get("name") or ""), ready=ready,
                         timeout=float(args.get("timeout") or 60),
                         keep=bool(args.get("keep")), restart=bool(args.get("restart")),
                         network=str(args.get("network", "")).lower() in ("1", "true", "yes"))

    def process(args) -> str:
        action = str(args.get("action") or "list").lower()
        pid_ = str(args.get("id") or "")
        if action == "list":
            return reg.listing()
        if not pid_:
            raise Violation(f"process {action}: give id (process(action='list') shows them)")
        if action == "output":
            return reg.output(pid_, args.get("tail"), str(args.get("grep") or ""))
        if action == "wait":
            w = args.get("for") or {}
            if isinstance(w, str):
                w = {"exit": True} if w == "exit" else {"text": w}
            return reg.wait(pid_, w, float(args.get("timeout") or 60))
        if action == "stop":
            return reg.stop(pid_)
        raise Violation("process action: list | output | wait | stop")

    # starting runs a command: gated exactly like run_command (target,
    # grade, pack limits — see SHELL_TOOLS)
    mediator.register(ToolSpec(PROCESS_START, BlastRadius.EXECUTE,
                               lambda a: a.get("host") or a.get("target")))
    dispatch[PROCESS_START] = process_start
    # typing into one of Aura's processes: like running a command
    def process_input(args) -> str:
        pid_ = str(args.get("id") or "")
        if not pid_:
            raise Violation("process_input: give id")
        return reg.type_in(pid_, str(args.get("text") or ""),
                           enter=args.get("enter", True) is not False,
                           ctrl_c=bool(args.get("ctrl_c")))

    mediator.register(ToolSpec(PROCESS_INPUT, BlastRadius.EXECUTE, lambda a: None))
    dispatch[PROCESS_INPUT] = process_input
    # looking at / waiting for / stopping Aura's own processes
    mediator.register(ToolSpec(PROCESS, BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch[PROCESS] = process
    return reg

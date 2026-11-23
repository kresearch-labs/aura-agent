"""Locate — find files and folders on this computer, and what the user worked on
recently, by NAME and DATE only (it never opens a file).

Why: asked "find the work I was doing", an agent with only a project search
crawls the disk from the top, opening files. This answers in seconds from what
is already known, fastest first, and stops on Esc / Ctrl+C.

Where it looks, in this order: 1. a folder the call names (`where`), else the
folder Aura is working in; 2. if nothing is found there: the whole computer —
a. the projects Aura has worked in (with when), b. the computer's own
recent-items list (Windows Recent, macOS Spotlight "last used", Linux
recently-used.xbel), c. the computer's own file index (Everything's `es` when
installed, Windows Search, macOS Spotlight `mdfind`, Linux `plocate`/`locate`),
d. last, a walk of the user's folders and then the other drives — system,
dependency and key folders skipped, a time limit, stoppable. With no name
("what was I working on"): recent activity from 1 and 2a–2c.

Folders that hold keys (.ssh,.aws …) and Aura's own folder are never listed.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from ..stopping import UserStopped, check, progress
from ..stopping import run as _run_stoppable

LOCATE = "locate"
_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"
WALK_BUDGET = 30.0                 # Seconds for the last-resort walk of the computer
QUICK_WALK = 5.0                   # …when the index already found good matches
OUT_MAX = 40                       # Lines per answer

# never worth walking into when looking for a person's work
_SYSTEM_WIN = {"windows", "program files", "program files (x86)", "programdata",
               "$recycle.bin", "system volume information", "recovery", "msocache",
               "$windows.~bt", "$windows.~ws", "windowsapps", "perflogs", "appdata"}
_SYSTEM_POSIX = {"/proc", "/sys", "/dev", "/run", "/usr", "/lib", "/lib32", "/lib64",
                 "/bin", "/sbin", "/boot", "/etc", "/var", "/snap", "/opt", "/srv",
                 "/System", "/Library", "/private", "/cores", "/Volumes/Recovery"}
_SKIP_NAMES = {"library", ".cache", ".local", ".npm", ".cargo", ".rustup", ".gradle",
               ".m2", ".nuget", ".vscode-server", ".conda", "miniconda3", "anaconda3",
               ".pyenv", ".nvm", ".docker", "go", ".trash", "trash"}


# ------------------------------------------------------------------ helpers --
def _ago(t: float) -> str:
    s = max(0, int(time.time() - t))
    if s < 3600:
        return f"{max(1, s // 60)} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _since(text) -> float | None:
    """'2d' / '12h' / '30m' / '1w' → seconds; None when not given."""
    if text in (None, "", False):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw]?)\s*", str(text).lower())
    if not m:
        raise Violation("locate: since is like 30m, 12h, 2d or 1w")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def _hidden() -> list[str]:
    """Folders never listed: where keys live, and Aura's own folder."""
    try:
        from ..sandbox import hidden_paths
        return [os.path.normcase(str(p)) for p in hidden_paths()]
    except Exception:                                # noqa: BLE001
        return []


def _is_hidden(path: str, hidden: list[str]) -> bool:
    p = os.path.normcase(path)
    return any(p == h or p.startswith(h.rstrip("\\/") + os.sep) for h in hidden)


class _Matcher:
    """Words that must all appear in the name (or the folder names above it),
    or a pattern with * and ?."""

    def __init__(self, query: str):
        q = (query or "").strip()
        self.empty = not q
        self.glob = any(c in q for c in "*?[")
        self.q = q.lower()
        self.words = [w for w in re.split(r"[\s_\-./\\]+", self.q) if w]

    def score(self, path: str) -> float:
        """0 = no match; higher = better (exact name > starts > contains >
        only in the folders above)."""
        if self.empty:
            return 1.0
        name = os.path.basename(path.rstrip("\\/")).lower()
        if self.glob:
            return 3.0 if fnmatch.fnmatch(name, self.q) else 0.0
        stem = name.rsplit(".", 1)[0]
        if name == self.q or stem == self.q:
            return 4.0
        if all(w in name for w in self.words):
            return 3.0 if name.startswith(self.words[0]) else 2.0
        low = path.lower()
        if all(w in low for w in self.words) and any(w in name for w in self.words):
            return 1.0
        return 0.0


_NOISE_PARTS = {"appdata", "$recycle.bin", "temp", "tmp", ".cache", "cache", "caches",
                "node_modules", "__pycache__", ".git", "logs", "crashpad", "packages",
                "library", ".local", ".npm", ".vscode", ".idea", "site-packages"}
_NOISE_EXT = (".tmp", ".lock", ".lck", ".db-wal", ".db-shm", ".journal", ".pyc", ".swp",
              ".crdownload", ".part", ".etl", ".pf", ".log")
_HASHY = re.compile(r"^[0-9a-f]{16,}$|^[0-9a-f-]{32,}$", re.I)


def _noise(path: str) -> bool:
    """Not someone's work: app caches, temp and lock files, hash-named blobs."""
    low = path.lower().replace("\\", "/")
    parts = [x for x in low.split("/") if x]
    name = parts[-1] if parts else ""
    return (any(x in _NOISE_PARTS for x in parts[:-1]) or name.endswith(_NOISE_EXT)
            or bool(_HASHY.match(name.rsplit(".", 1)[0])))


class _Hits:
    """Results by path: best score and newest time kept, where it came from."""

    def __init__(self, hidden: list[str], since: float | None, kind: str,
                 recent: bool = False):
        self.rows: dict[str, dict] = {}
        self.hidden, self.since, self.kind = hidden, since, kind
        self.recent = recent              # "what was I working on": noise left out

    def add(self, path: str, score: float, when: float | None = None,
            source: str = "", is_dir: bool | None = None) -> None:
        if not path or score <= 0 or _is_hidden(path, self.hidden):
            return
        if self.recent and _noise(path):
            return
        if when is None or is_dir is None:
            try:
                st = os.stat(path)
            except OSError:
                return                                  # Gone (or no right to look)
            when = st.st_mtime if when is None else when
            is_dir = os.path.isdir(path) if is_dir is None else is_dir
        if self.kind == "files" and is_dir or self.kind == "folders" and not is_dir:
            return
        if self.since is not None and when < time.time() - self.since:
            return
        key = os.path.normcase(path)
        old = self.rows.get(key)
        if old is None or (score, when) > (old["score"], old["when"]):
            self.rows[key] = {"path": path, "score": score, "when": when,
                              "dir": is_dir, "source": source or (old or {}).get("source", "")}

    def best(self, n: int) -> list[dict]:
        rows = sorted(self.rows.values(), key=lambda r: (-r["score"], -r["when"]))
        if rows and rows[0]["score"] >= 3:
            # strong name matches found: the weak ones (a word only in the
            # folders above) are left out
            rows = [r for r in rows if r["score"] >= 2]
        return rows[:n]

    def strong(self) -> int:
        return sum(1 for r in self.rows.values() if r["score"] >= 3)

    def __len__(self) -> int:
        return len(self.rows)


def _fmt(rows: list[dict]) -> list[str]:
    out = []
    for r in rows:
        tail = f"  · {r['source']}" if r.get("source") else ""
        out.append(f"{r['path']}{os.sep if r['dir'] else ''}  ({'changed ' if not r['dir'] else ''}"
                   f"{_ago(r['when'])}){tail}")
    return out


# ------------------------------------------------------------ the walk --
def _walk(roots: list[Path], m: _Matcher, hits: _Hits, budget: float,
          skip_system: bool, skip: set[str] | None = None) -> tuple[int, bool]:
    """Breadth first (shallow places first), names only. Returns (folders
    looked at, ran out of time). A user's stop ends it (UserStopped)."""
    from .search import SKIP_DIRS
    end = time.monotonic() + budget
    queue = [p for p in roots if p.is_dir()]
    seen = 0
    while queue:
        if time.monotonic() > end:
            return seen, True
        check("locate")
        d = queue.pop(0)
        seen += 1
        if seen % 25 == 1:
            progress(f"walked {seen:,} folders, {len(hits)} found — now in {d}")
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            low = e.name.lower()
            if is_dir:
                if (e.name in SKIP_DIRS or low in _SKIP_NAMES
                        or (skip_system and _system_dir(e.path, low))):
                    continue
                if e.name.startswith(".") and low not in (".config",):
                    continue
                if (not _is_hidden(e.path, hits.hidden)
                        and os.path.normcase(e.path) not in (skip or ())):
                    queue.append(Path(e.path))
            s = m.score(e.path)
            if s:
                try:
                    hits.add(e.path, s, e.stat(follow_symlinks=False).st_mtime,
                             "found by walking", is_dir)
                except OSError:
                    pass
    return seen, False


def _system_dir(path: str, low: str) -> bool:
    if _IS_WIN:
        return low in _SYSTEM_WIN
    return path in _SYSTEM_POSIX


def _drive_roots() -> list[Path]:
    if _IS_WIN:
        try:
            drives = os.listdrives()                     # Python 3.12+
        except AttributeError:
            drives = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
        out = []
        for d in drives:
            try:
                import ctypes
                if ctypes.windll.kernel32.GetDriveTypeW(d) == 3:   # Fixed disks only
                    out.append(Path(d))
            except Exception:                            # noqa: BLE001
                out.append(Path(d))
        return out
    return [Path("/")]


# ------------------------------------------------- what is already known --
def _aura_projects() -> list[tuple[str, float]]:
    """(folder, last worked) for every project Aura has a notebook for."""
    out = []
    try:
        from ..paths import profile_dir, workspace_of
        for nb in (profile_dir() / "projects").iterdir():
            ws = workspace_of(nb)
            if ws is None:
                continue
            when = 0.0
            for probe in (nb / "sessions", nb / "audit.jsonl", nb):
                try:
                    when = max(when, probe.stat().st_mtime)
                except OSError:
                    pass
            out.append((str(ws), when))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x[1])


def _lnk_target(data: bytes) -> str:
    """The target path of a Windows shortcut (.lnk), from its LinkInfo
    (MS-SHLLINK); '' when it has none."""
    try:
        if data[:4] != b"\x4c\x00\x00\x00":
            return ""
        flags = struct.unpack_from("<I", data, 0x14)[0]
        pos = 0x4C
        if flags & 0x1:                                  # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if not flags & 0x2:                              # HasLinkInfo
            return ""
        size, head, lflags, _vol, base_off, _net, suf_off = struct.unpack_from("<7I", data, pos)
        if not lflags & 0x1:
            return ""
        if head >= 0x24:                                 # The Unicode copies
            ubase, usuf = struct.unpack_from("<2I", data, pos + 0x1C)
            def _w(off):
                raw = data[pos + off:pos + size]
                end = raw.find(b"\x00\x00")
                while end != -1 and end % 2:
                    end = raw.find(b"\x00\x00", end + 1)
                return raw[:end].decode("utf-16-le", "replace") if end != -1 else ""
            path = _w(ubase) + (_w(usuf) if usuf else "")
            if path:
                return path
        def _a(off):
            raw = data[pos + off:pos + size]
            return raw.split(b"\x00", 1)[0].decode("mbcs" if _IS_WIN else "latin-1", "replace")
        return _a(base_off) + _a(suf_off)
    except (struct.error, IndexError, LookupError):
        return ""


def _os_recent(m: _Matcher, hits: _Hits, limit: int = 200) -> str:
    """The computer's own list of recently used files. Returns its name."""
    if _IS_WIN:
        rec = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
        try:
            items = sorted(rec.glob("*.lnk"), key=lambda p: -p.stat().st_mtime)[:limit]
        except OSError:
            return ""
        for lnk in items:
            check("locate")
            try:
                target = _lnk_target(lnk.read_bytes())
                used = lnk.stat().st_mtime
            except OSError:
                continue
            if target:
                hits.add(target, m.score(target), None, "recently opened", None)
                r = hits.rows.get(os.path.normcase(target))
                if r is not None:
                    r["when"] = max(r["when"], used)
        return "Windows' recent items"
    if _IS_MAC and shutil.which("mdfind"):
        try:
            r = _run_stoppable(["mdfind", "kMDItemLastUsedDate >= $time.now(-1209600)"],
                               timeout=15, what="locate")
        except subprocess.TimeoutExpired:
            return ""
        for line in r.stdout.splitlines()[: limit * 5]:
            hits.add(line.strip(), m.score(line.strip()), None, "recently opened", None)
        return "Spotlight's recently used"
    xbel = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "recently-used.xbel"
    try:
        text = xbel.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    from urllib.parse import unquote, urlparse
    for href in re.findall(r'<bookmark[^>]*href="(file://[^"]+)"', text)[-limit:]:
        p = unquote(urlparse(href).path)
        hits.add(p, m.score(p), None, "recently opened", None)
    return "the desktop's recently used list"


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _os_index(m: _Matcher, hits: _Hits, since: float | None, limit: int = 200) -> str:
    """The computer's own file index. Returns which one answered ('' none)."""
    words = m.words if not m.glob else [m.q.replace("*", "").replace("?", "")]
    words = [w for w in words if w]
    es = shutil.which("es") or shutil.which("es.exe")
    try:
        if _IS_WIN and es and (words or since):
            argv = [es, "-n", str(limit), "-sort", "date-modified-descending"]
            if since:
                argv += ["dm:>=" + time.strftime("%Y-%m-%d", time.localtime(time.time() - since))]
            argv += words
            r = _run_stoppable(argv, timeout=20, what="locate")
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    hits.add(line.strip(), m.score(line.strip()), None, "Everything index", None)
                return "Everything"
        if _IS_WIN:
            where = [f"System.FileName LIKE '%{_ps_quote(w).replace('%', '').replace('_', '[_]')}%'"
                     for w in words]
            if since:
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.gmtime(time.time() - since))
                where.append(f"System.DateModified >= '{stamp}'")
            if not where:
                return ""
            sql = (f"SELECT TOP {limit} System.ItemPathDisplay FROM SystemIndex WHERE "
                   + " AND ".join(where) + " ORDER BY System.DateModified DESC")
            ps = ("$c=New-Object -ComObject ADODB.Connection;"
                  "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\");"
                  f"$r=$c.Execute('{_ps_quote(sql)}');"
                  "while(-not $r.EOF){$r.Fields.Item(0).Value;$r.MoveNext()}")
            r = _run_stoppable(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               timeout=25, what="locate")
            if r.returncode == 0 and r.stdout.strip():
                for line in r.stdout.splitlines():
                    hits.add(line.strip(), m.score(line.strip()), None, "Windows Search index", None)
                return "Windows Search"
            return ""
        if _IS_MAC and shutil.which("mdfind"):
            q = " && ".join(f"kMDItemFSName == '*{w}*'cd" for w in words)
            if since:
                q = (q + " && " if q else "") + f"kMDItemFSContentChangeDate >= $time.now(-{int(since)})"
            if not q:
                return ""
            r = _run_stoppable(["mdfind", q], timeout=20, what="locate")
            for line in r.stdout.splitlines()[: limit * 5]:
                hits.add(line.strip(), m.score(line.strip()), None, "Spotlight", None)
            return "Spotlight"
        loc = shutil.which("plocate") or shutil.which("locate")
        if loc and words:
            r = _run_stoppable([loc, "-i", "-l", str(limit * 5), words[0]], timeout=20,
                               what="locate")
            for line in r.stdout.splitlines():
                hits.add(line.strip(), m.score(line.strip()), None, "locate index", None)
            return os.path.basename(loc)
    except subprocess.TimeoutExpired:
        return ""
    except OSError:
        return ""
    return ""


# --------------------------------------------------------------- the tool --
def locate(workspace: Path, args: dict) -> str:
    query = str(args.get("name") or args.get("query") or "").strip()
    where = str(args.get("where") or "").strip()
    kind = str(args.get("kind") or "any").lower()
    if kind not in ("any", "files", "folders"):
        raise Violation("locate: kind is any, files or folders")
    since = _since(args.get("since"))
    everywhere = bool(args.get("everywhere"))
    thorough = bool(args.get("thorough"))
    limit = max(5, min(int(args.get("limit") or 25), OUT_MAX))
    m = _Matcher(query)
    if m.empty and since is None:
        since = 7 * 86400                                # "what was I working on"
    hidden = _hidden()
    hits = _Hits(hidden, since, kind, recent=m.empty)
    said: list[str] = []
    ws = Path(workspace).resolve()
    try:
        from ..paths import aura_home
        in_aura = os.path.normcase(str(ws)).startswith(os.path.normcase(str(aura_home())))
    except Exception:                                    # noqa: BLE001
        in_aura = False

    from ..paths import resolve_user_path
    if query and ("/" in query or "\\" in query):
        got, note = resolve_user_path(query)
        if got.exists():
            return ((note + "\n") if note else "") + f"found: {got}"
    try:
        # 1. the folder named, else the folder Aura works in
        if where:
            first, note = resolve_user_path(where)
            if note:
                said.append(note)
        else:
            first = None if in_aura else ws
        if first is not None and not everywhere:
            progress(f"looking in {first}")
            if not first.is_dir():
                raise Violation(f"locate: {where} is not a folder")
            n, out_of_time = _walk([first], m, hits, WALK_BUDGET, skip_system=False)
            said.append(f"looked in {first} ({n} folders"
                        + (", stopped at the time limit" if out_of_time else "") + ")")
            if len(hits) and not m.empty:
                return _answer(hits, limit, said, more=True, where=bool(where))
        # 2. the whole computer
        progress("checking the projects Aura has worked in")
        for folder, when in _aura_projects():
            s = m.score(folder)
            if s:
                hits.add(folder, s + 0.5, when, "a project Aura worked in", True)
        said.append("checked the projects Aura has worked in")
        progress("checking the computer's recently used files")
        name = _os_recent(m, hits)
        if name:
            said.append(f"checked {name}")
        progress("asking the computer's file index")
        idx = _os_index(m, hits, since)
        said.append(f"asked the {idx} file index" if idx
                    else "no file index answered (none on this computer, or it is off)")
        if m.empty:
            return _answer_recent(hits, limit, said)
        # nearby: the folder around the project (its parent and siblings),
        # a short walk — never the whole computer without a yes
        if first is not None and first.parent != first:
            progress(f"looking near it, in {first.parent}")
            n, _late = _walk([first.parent], m, hits, QUICK_WALK, skip_system=True,
                             skip={os.path.normcase(str(first))})
            said.append(f"looked nearby in {first.parent} ({n} folders)")
        if not everywhere:
            if not len(hits):
                said.append("not found nearby — the whole computer was NOT searched. "
                            "To search it, call locate with everywhere=true: the user is "
                            "asked first (it can take minutes)")
            return _answer(hits, limit, said)
        # good matches already: a short walk (the closest folders, where an
        # unindexed exact match usually is); none: the full time limit
        budget = QUICK_WALK if hits.strong() and not thorough else WALK_BUDGET
        # d. last resort: walk the user's folders, then the other drives
        # the home folder first (shallow places first), then every drive —
        # the home folder is not walked twice
        home = Path.home()
        roots = [home] + [d for d in _drive_roots() if d != home]
        n, out_of_time = _walk(roots, m, hits, budget, skip_system=True,
                               skip={os.path.normcase(str(home))})
        said.append(f"walked {n} folders across the computer"
                    + (f" (stopped at its {budget:.0f}s limit — "
                       + ("thorough=true looks further" if budget < WALK_BUDGET
                          else "name a folder with where= to look further") + ")"
                       if out_of_time else ""))
    except UserStopped:
        said.append("stopped by the user")
    return _answer_recent(hits, limit, said) if m.empty else _answer(hits, limit, said)


_PROJECT_MARKS = (".git", "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
                  "go.mod", "pom.xml", "build.gradle", ".sln", "Makefile", "AGENTS.md",
                  "CLAUDE.md", "README.md")


def _project_of(path: str, known: set[str], cache: dict) -> str:
    """The folder a changed file belongs to: the nearest folder above it that
    is an Aura project or looks like one (.git, package.json …), else the
    folder it is in."""
    d = os.path.dirname(path)
    start = d
    for _ in range(8):
        key = os.path.normcase(d)
        if key in cache:
            return cache[key]
        if key in known or any(os.path.exists(os.path.join(d, mk)) for mk in _PROJECT_MARKS):
            cache[os.path.normcase(start)] = d
            return d
        up = os.path.dirname(d)
        if up == d or os.path.normcase(up) == os.path.normcase(str(Path.home())):
            break
        d = up
    cache[os.path.normcase(start)] = start
    return start


def _answer_recent(hits: _Hits, limit: int, said: list[str]) -> str:
    """Recent activity, grouped: the folders worked in (newest first), how
    many files changed there, and the latest few by name."""
    head = "; ".join(said)
    rows = sorted(hits.rows.values(), key=lambda r: -r["when"])
    if not rows:
        return f"no recent activity found — {head}. Try a longer since= (e.g. 2w) or a name."
    known = {os.path.normcase(f) for f, _ in _aura_projects()}
    cache: dict = {}
    groups: dict[str, dict] = {}
    for r in rows:
        folder = r["path"] if r["dir"] else _project_of(r["path"], known, cache)
        g = groups.setdefault(os.path.normcase(folder),
                              {"folder": folder, "when": 0.0, "files": [], "aura": False})
        g["when"] = max(g["when"], r["when"])
        if os.path.normcase(folder) in known:
            g["aura"] = True
        if not r["dir"]:
            g["files"].append(r)
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["when"])[: max(5, limit // 3)]:
        n = len(g["files"])
        out.append(f"{g['folder']}{os.sep}  (last change {_ago(g['when'])}"
                   + (f", {n} file{'s' if n != 1 else ''} changed" if n else "")
                   + (", a project Aura worked in" if g["aura"] else "") + ")")
        for f in g["files"][:3]:
            out.append(f"    {os.path.relpath(f['path'], g['folder'])}  ({_ago(f['when'])})")
    return f"{head}\nfolders with recent work, newest first:\n" + "\n".join(out)


def _answer(hits: _Hits, limit: int, said: list[str], more: bool = False,
            where: bool = False) -> str:
    rows = hits.best(limit)
    head = "; ".join(said)
    if not rows:
        return f"nothing found — {head}. Try other words, a pattern like *report*, or since=."
    body = "\n".join(_fmt(rows))
    extra = ""
    if len(hits) > limit:
        extra += f"\n({len(hits) - limit} more — narrow it with more words, kind= or since=)"
    if more:
        extra += ("\n(only " + ("that folder" if where else "the folder Aura works in")
                  + " was searched — call again with everywhere=true to look across the computer)")
    return f"{head}\n{body}{extra}"


def register(mediator: Mediator, dispatch: dict, workspace: Path) -> None:
    def _ask(a):
        if a.get("everywhere"):
            return ("it searches the whole computer — every drive, folder by folder "
                    "(can take minutes)")
        return None
    mediator.register(ToolSpec(LOCATE, BlastRadius.READ, lambda a: None, ask_when=_ask),
                      plan_safe=True)
    dispatch[LOCATE] = lambda a: locate(Path(getattr(mediator, "work_dir", None) or workspace), a)

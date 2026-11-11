"""outside.py — folders outside the project that Aura's file tools may use.

Two lists: folders allowed for this chat (in memory, on the mediator) and
folders saved for the project (<notes>/outside_dirs.json). Some places are
never allowed — keys and credentials, Aura's own folder.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FILE = "outside_dirs.json"

# where keys and credentials live: never read or written from a chat, at any
# asking level (the never-read file names in secrets_guard cover the rest)
_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
                ".config/gcloud", ".config/gh", ".password-store")
# single files that hold tokens or passwords (git, pip upload, npm, curl/ftp)
_SECRET_FILES = (".netrc", ".git-credentials", ".pypirc", ".npmrc")


def never_reason(path: Path) -> str | None:
    """Why `path` may never be used from a chat, or None."""
    p = Path(path).resolve()
    from .paths import aura_home
    try:
        p.relative_to(aura_home().resolve())
        return "it is Aura's own folder (settings, keys, chats)"
    except ValueError:
        pass
    home = Path.home().resolve()
    for d in _SECRET_DIRS:
        try:
            p.relative_to((home / d).resolve())
            return f"~/{d} holds keys or credentials"
        except ValueError:
            continue
    for f in _SECRET_FILES:
        if p == (home / f).resolve():
            return f"~/{f} holds passwords or tokens"
    return None


def folder_of(path: Path) -> Path:
    """The folder to allow for a path: itself when it is a folder, else its
    parent (a file that doesn't exist yet counts as a file)."""
    p = Path(path).resolve()
    return p if p.is_dir() else p.parent


def saved(eng: Path) -> list[Path]:
    try:
        raw = json.loads((Path(eng) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(x) for x in raw.get("dirs", []) if isinstance(x, str)]


def save(eng: Path, folder: Path) -> None:
    dirs = [str(d) for d in saved(eng)]
    f = str(Path(folder).resolve())
    if f not in dirs:
        dirs.append(f)
    p = Path(eng) / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"dirs": dirs}, indent=1), encoding="utf-8")


def remove(eng: Path, folder: Path) -> bool:
    f = str(Path(folder).resolve())
    dirs = [str(d) for d in saved(eng)]
    if f not in dirs:
        return False
    dirs.remove(f)
    (Path(eng) / FILE).write_text(json.dumps({"dirs": dirs}, indent=1), encoding="utf-8")
    return True


def inside(path: Path, folders) -> bool:
    p = Path(path).resolve()
    for f in folders:
        f = Path(f).resolve()
        if p == f or f in p.parents:
            return True
    return False


# Bug: the file tools asked before leaving the project, but a command didn't —
# `where r C:\ *parimal*` searched the whole drive. Paths a command names are
# checked the same way. Places every command touches anyway (the system,
# programs, temp) don't ask.
_WIN_Q = re.compile(r'"([A-Za-z]:[\\/][^"]*)"')
_WIN = re.compile(r"(?<![\w])([A-Za-z]:[\\/][^\s\"'|&;<>]*)")
_POSIX = re.compile(r"(?<![\w.:/\\-])((?:~|/)[\w.~/-]*)")
_REL = re.compile(r"(?<![\w.])(\.\.[\\/][^\s\"'|&;<>]*)")


def _system_dirs() -> list[Path]:
    import tempfile
    out = [Path(tempfile.gettempdir())]
    if os.name == "nt":
        for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            if os.environ.get(v):
                out.append(Path(os.environ[v]))
    else:
        out += [Path(p) for p in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt",
                                  "/proc", "/sys", "/dev", "/tmp", "/var", "/run", "/snap",
                                  "/Applications", "/System", "/Library", "/private")]
    import sys
    out.append(Path(sys.prefix))                 # The Python running Aura
    return out


def _expand_vars(s: str) -> str:
    """%VAR% (Windows), $VAR and ${VAR} (POSIX), $env:VAR (PowerShell) →
    their values, so a path built from them is checked like any other."""
    def val(name: str, raw: str) -> str:
        v = os.environ.get(name)
        if v is None and name.upper() in _HOME_VARS:
            v = str(Path.home())          # $env:USERPROFILE on Linux, $HOME on Windows: the same folder
        return raw if v is None else v
    s = re.sub(r"\$env:(\w+)", lambda m: val(m.group(1), m.group(0)), s, flags=re.I)
    s = re.sub(r"%(\w+)%", lambda m: val(m.group(1), m.group(0)), s)
    s = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: val(m.group(1) or m.group(2), m.group(0)), s)
    if os.name != "nt":                   # $env:USERPROFILE\Desktop on Linux: home/u/Desktop
        home = re.escape(str(Path.home()))
        s = re.sub(home + r"\\[^\s\"';|&]*", lambda m: m.group(0).replace("\\", "/"), s)
    return s


_HOME_VARS = ("HOME", "USERPROFILE")


def paths_in_command(cmd: str, work: Path) -> list[Path]:
    """Paths a shell command names that are outside `work` and not in a
    system place — the first word (the program) doesn't count."""
    # %USERPROFILE%\Downloads, $HOME/x, ${HOME}: a variable is the path it
    # names
    c = _expand_vars(str(cmd or ""))
    first = c.strip().split(None, 1)[0].strip("\"'") if c.strip() else ""
    quoted = _WIN_Q.findall(c)
    bare = _WIN_Q.sub(" ", c)                 # A quoted path isn't read again in pieces
    found: list[str] = quoted + _WIN.findall(bare) + _REL.findall(bare)
    for p in _POSIX.findall(bare):
        # "/r", "/s" are Windows switches; a POSIX path has two parts or starts with ~
        if p.startswith("~") or p.strip("/").count("/") >= 1:
            found.append(p)
    # the root itself, as its own word (`ls /`, `find / -name x`): the whole
    # drive — not a "/" between two numbers (arithmetic)
    words = bare.split()
    for i, w in enumerate(words[1:], 1):
        if w.strip("\"'") == "/" and not (words[i - 1][-1:].isdigit()
                                          or (i + 1 < len(words) and words[i + 1][:1].isdigit())):
            found.append("/")
    work_r = Path(work).resolve()
    sysd = [d.resolve() for d in _system_dirs() if str(d)]
    out: list[Path] = []
    for raw in found:
        raw = raw.rstrip(".,;:)")
        if not raw or raw == first:
            continue
        try:
            p = (work_r / os.path.expanduser(raw)).resolve()
        except (OSError, ValueError):
            continue
        if p == work_r or work_r in p.parents:
            continue
        if any(p == d or d in p.parents for d in sysd):
            continue
        if p not in out:
            out.append(p)
    return out


def addable(folder: Path) -> str | None:
    """Why `folder` can't be allowed as a whole, or None. A drive root or
    the whole home folder is too wide: allow the folder inside it."""
    f = Path(folder).resolve()
    if not f.is_dir():
        return "no such folder"
    if f == Path(f.anchor):
        return "a whole drive is too wide — allow the folder you need"
    if f == Path.home().resolve():
        return "your whole home folder is too wide — allow the folder you need"
    return never_reason(f)

"""lsp_offer.py — the error checker for a language, offered when Aura meets it.

The first time Aura edits a file in a language with no error checker (language
server) on this computer, a card asks once:

    This project is in C. Install clangd so Aura sees errors right after
    each edit? → Yes, install it / Not now

Yes: it installs in the background (the program it needs first, if missing —
Node.js for TypeScript, Go for gopls, rustup for rust-analyzer), and the next
edit uses it. Not now: never asked again for that project. Nobody to ask
(scheduled or piped runs): nothing is asked or installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# language: (its name for people, the server, what it costs, the program it needs)
CHECKERS = {
    "python": ("Python", "basedpyright", "about 20 MB", None),
    "typescript": ("JavaScript/TypeScript", "typescript-language-server", "about 30 MB", "node"),
    "go": ("Go", "gopls", "about 40 MB", "go"),
    "rust": ("Rust", "rust-analyzer", "about 60 MB", "rustup"),
    "c": ("C/C++", "clangd", "a large download (about 400 MB, from LLVM)", None),
}
NEEDS_CMD = {"node": "npm", "go": "go", "rustup": "rustup"}

_ASKED: set = set()           # (project, language) asked in this process
_BUSY: set = set()            # Languages installing now
_LOCK = threading.Lock()


def _declined_file(root: Path) -> Path:
    return Path(root) / ".aura" / "lsp-declined.json"


def _declined(root: Path) -> list:
    try:
        return list(json.loads(_declined_file(root).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _decline(root: Path, lang: str) -> None:
    try:
        f = _declined_file(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(sorted(set(_declined(root)) | {lang})), encoding="utf-8")
    except OSError:
        pass


def program_cmd(prog: str):
    """The package manager command that installs a program here, or None:
    (argv or shell string, shell?)."""
    from .setup_journeys import PROGRAMS
    p = PROGRAMS.get(prog) or {}
    if os.name == "nt" and shutil.which("winget") and p.get("winget"):
        return ["winget", "install", "-e", "--id", p["winget"], "--silent",
                "--accept-package-agreements", "--accept-source-agreements"], False
    if sys.platform == "darwin" and shutil.which("brew") and (p.get("brew") or p.get("brew_cask")):
        return (["brew", "install", "--cask", p["brew_cask"]] if p.get("brew_cask")
                else ["brew", "install", p["brew"]]), False
    if p.get("sh"):
        return p["sh"], True
    if shutil.which("apt-get") and p.get("apt"):
        # -n: no password prompt in the middle of a chat — a refusal is said plainly
        return f"sudo -n apt-get install -y {p['apt']}", True
    return None


def steps(lang: str) -> list | None:
    """What installs this language's checker, in order; None if Aura can't here."""
    out = []
    need = CHECKERS[lang][3]
    if need and not shutil.which(NEEDS_CMD[need]):
        c = program_cmd(need)
        if c is None:
            return None
        out.append((need, c))
    if lang == "c":
        c = program_cmd("clangd")
        return None if c is None else out + [("clangd", c)]
    if lang == "python":
        from .setup_journeys import python_part_cmd
        return out + [("basedpyright", (python_part_cmd("basedpyright"), False))]
    cmd = {"typescript": "npm install -g typescript-language-server typescript",
           "go": "go install golang.org/x/tools/gopls@latest",
           "rust": "rustup component add rust-analyzer"}[lang]
    return out + [(CHECKERS[lang][1], (cmd, True))]


def _log() -> Path:
    from .paths import aura_home
    d = aura_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "lsp-install.log"


def _install(lang: str, plan: list, tell) -> None:
    """Background: each step, its output to the log; then the result as a notice."""
    name, server = CHECKERS[lang][0], CHECKERS[lang][1]
    problem = ""
    try:
        with open(_log(), "a", encoding="utf-8") as log:
            for what, (cmd, sh) in plan:
                log.write(f"\n== {what}: {cmd}\n")
                log.flush()
                try:
                    r = subprocess.run(cmd, shell=sh, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, timeout=1800)
                except (OSError, subprocess.TimeoutExpired) as e:
                    problem = f"{what} didn't install ({e})"
                    break
                from .setup_journeys import _refresh_path
                _refresh_path()
                if r.returncode != 0:
                    problem = (f"{what} needs your password to install (sudo)"
                               if isinstance(cmd, str) and cmd.startswith("sudo -n")
                               else f"{what} didn't install (exit {r.returncode})")
                    break
    except OSError as e:
        problem = str(e)
    from .lsp import find_server
    if not problem and not find_server(lang):
        problem = f"{server} installed, but Aura can't find it yet — a new terminal may be needed"
    with _LOCK:
        _BUSY.discard(lang)
    if problem:
        tell(f"The {name} error checker wasn't added: {problem}. Details: {_log()}")
    else:
        tell(f"{server} is installed — Aura now sees {name} errors right after each edit.")


def maybe_offer(path: str, root: Path, asker, tell) -> str:
    """After an edit: a missing checker for this file's language is offered
    once. Returns a line for the tool result ('' when nothing happened)."""
    from .lsp import find_server, language_of
    lang = language_of(path)
    if not lang or lang not in CHECKERS or asker is None:
        return ""
    if find_server(lang):
        return ""
    key = (str(Path(root).resolve()), lang)
    with _LOCK:
        if key in _ASKED or lang in _BUSY or lang in _declined(root):
            return ""
        _ASKED.add(key)
    plan = steps(lang)
    if plan is None:
        return ""
    name, server, size, need = CHECKERS[lang]
    first = (f" (with {need.capitalize() if need != 'node' else 'Node.js'}, which it needs)"
             if plan and plan[0][0] == need else "")
    yes, no = f"Yes, install {server}", "Not now"
    try:
        pick = asker(f"This project is in {name}. Install {server}{first} so Aura sees "
                     f"errors right after each edit? ({size})", [yes, no])
    except Exception:                                # noqa: BLE001
        return ""
    if str(pick or "").split(" — ")[0] != yes:
        _decline(root, lang)
        return ""
    with _LOCK:
        _BUSY.add(lang)
    threading.Thread(target=_install, args=(lang, plan, tell), daemon=True).start()
    return f"(installing {server} in the background — errors show after it's ready)"

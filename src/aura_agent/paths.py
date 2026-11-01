"""paths.py — the one answer to "where does Aura keep this?".

Every module asks here instead of working out `~/.aura` on its own.

Resolved lazily — AURA_HOME is read at call time, so tests and the selftest
isolate by setting it.

  aura_home shared, about the user and the models: the install, the
                per-model prompt copies and eval scores, the user's
                preferences, settings, skills and custom commands.
  profile_dir one isolated profile (AURA_PROFILE, default "default").
  project_dir(workspace)
                one project's notebook inside the profile: chats, results,
                todo, ledger, journal, project memory, rules, undo, audit.
                The notebook remembers its workspace (workspace.json).

The WORKSPACE is the user's project folder: the file tools, commands and tests
work there. Aura writes nothing into it — its notes live in the notebook.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


def aura_home() -> Path:
    """AURA_HOME, else ~/.aura."""
    env = os.environ.get("AURA_HOME")
    if env:
        return Path(env)
    return Path.home() / ".aura"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip(".-")[:40] or "x"


_ACTIVE_FILE = "active_profile"


def profile_name() -> str:
    """The work profile in use: AURA_PROFILE (set by -p/--profile for one
    launch), else the one `aura profile use` picked, else "default" — a
    new user never has to make one."""
    name = os.environ.get("AURA_PROFILE")
    if not name:
        try:
            name = (aura_home() / _ACTIVE_FILE).read_text(
                encoding="utf-8").strip()
        except OSError:
            name = ""
    return _safe(name or "default")


def set_default_profile(name: str) -> None:
    """`aura profile use <name>`: the profile Aura opens from now on."""
    p = aura_home() / _ACTIVE_FILE
    if name == "default":
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_safe(name) + "\n", encoding="utf-8")


def valid_profile_name(name: str) -> bool:
    return bool(name) and _safe(name) == name and len(name) <= 40


def profile_dir(name: str | None = None) -> Path:
    return aura_home() / "profiles" / _safe(name or profile_name())


# What belongs to a profile, not to everyone: its models and their keys,
# its saved settings, MCP logins, gateway channels, hooks, skills and slash
# commands. (MCP servers ride in profiles.json.)
PROFILE_ITEMS = ("profiles.json", "settings.json", "mcp_tokens.json",
                 "gateway.json", "hooks", "skills", "commands")


def profile_path(item: str) -> Path:
    """Where `item` lives for the active profile. Before profiles these
    sat directly in the Aura home; the first time the default profile
    asks for one, it is moved into profiles/default (nothing is copied or
    lost — if the move fails the old place keeps being used)."""
    p = profile_dir() / item
    if not p.exists() and profile_name() == "default":
        old = aura_home() / item
        if old.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                old.replace(p)
            except OSError:
                return old
    return p


def project_dir(workspace: Path) -> Path:
    """The notebook for a workspace: <profile>/projects/<folder>-<id>. The
    id (from the full path) keeps two folders both called `app` apart."""
    ws = Path(workspace).resolve()
    tag = hashlib.sha1(ws.as_posix().lower().encode()).hexdigest()[:8]
    return profile_dir() / "projects" / f"{_safe(ws.name)}-{tag}"


def no_project_dir() -> Path:
    """Where Aura works when it is not in a project (home folder, drive
    root, system folder). The default profile keeps the pre-profiles
    ~/.aura/engagement so existing data is where it always was."""
    if profile_name() == "default":
        return aura_home() / "engagement"
    return profile_dir() / "engagement"


_WS_FILE = "workspace.json"


def remember_workspace(notebook: Path, workspace: Path) -> None:
    try:
        Path(notebook, _WS_FILE).write_text(json.dumps(
            {"path": str(Path(workspace).resolve())}), encoding="utf-8")
    except OSError:
        pass


def workspace_of(notebook: Path) -> Path | None:
    """The project folder a notebook belongs to, or None (same-folder or
    no-project notebooks)."""
    try:
        p = Path(json.loads(Path(notebook, _WS_FILE).read_text(
            encoding="utf-8"))["path"])
        return p if p.is_dir() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def workspace_for(notebook: Path) -> Path:
    """Where the file tools, commands and tests work for this notebook:
    its project folder, or the notebook itself (same-folder mode)."""
    return workspace_of(notebook) or Path(notebook).resolve()


def has_aura_state(folder: Path) -> bool:
    """The folder already holds Aura's notes (an old-style same-folder
    engagement) — it keeps working there, nothing is moved."""
    f = Path(folder)
    return ((f / "scope.json").exists() or (f / "findings.jsonl").exists()
            or any(f.glob("sessions/*.jsonl")))


def unsafe_workspace(folder: Path) -> str | None:
    """Why `folder` must not be a workspace (the constant-home rule's "no
    System32 trap"), or None when it is fine."""
    f = Path(folder).resolve()
    if f == Path.home().resolve():
        return "your home folder"
    if f == Path(f.anchor):
        return "a drive root"
    try:
        f.relative_to(aura_home().resolve())
        return "Aura's own folder"
    except ValueError:
        pass
    low = f.as_posix().lower()
    win = os.environ.get("SystemRoot", "C:/Windows").replace("\\", "/").lower()
    if low.startswith(win) or low.startswith(("c:/program files",
                                              "c:/programdata")):
        return "a system folder"
    if f.parts[:2] in {(f.anchor, d) for d in ("usr", "bin", "sbin", "etc",
                                                 "lib", "proc", "sys", "dev",
                                                 "boot", "var")}:
        return "a system folder"
    if not os.access(f, os.W_OK):
        return "a folder you can't write to"
    return None

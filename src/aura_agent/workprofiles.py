"""workprofiles.py — `aura profile`: separate work profiles.

A profile is a separate desk: its own models and keys, MCP servers and logins,
settings, gateway, hooks, skills, slash commands — and its own projects and
chats. Only what is about the user (USER.md, what Aura learned about each
model) is shared.

"default" always exists, so a new user never has to make one.

  aura profile which profile is in use
  aura profile list all profiles
  aura profile create <name> [--copy-models]
  aura profile use <name> open this one from now on
  aura profile delete <name> moved to the trash folder, not erased
  aura -p <name> <command> use a profile for one launch only

Model setups (endpoint + model + key) are `aura model …`.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from .paths import (aura_home, profile_dir, profile_name, set_default_profile,
                    valid_profile_name)


def _sticky() -> str:
    try:
        return (aura_home() / "active_profile").read_text(
            encoding="utf-8").strip() or "default"
    except OSError:
        return "default"


def names() -> list[str]:
    root = aura_home() / "profiles"
    found = {d.name for d in root.iterdir() if d.is_dir()} \
        if root.is_dir() else set()
    return ["default"] + sorted(found - {"default"})


def exists(name: str) -> bool:
    return name == "default" or profile_dir(name).is_dir()


def _models_file(name: str) -> Path:
    p = profile_dir(name) / "profiles.json"
    if name == "default" and not p.exists():
        return aura_home() / "profiles.json"     # Not moved in yet
    return p


def _summary(name: str) -> str:
    try:
        data = json.loads(_models_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    models = len(data.get("profiles") or {})
    proj = profile_dir(name) / "projects"
    projects = sum(1 for d in proj.iterdir() if d.is_dir()) \
        if proj.is_dir() else 0
    return (f"{models} model setup{'s' if models != 1 else ''}, "
            f"{projects} project{'s' if projects != 1 else ''}")


def show() -> list[str]:
    cur = profile_name()
    out = [f"profile: {cur}  ({_summary(cur)})",
           f"  folder: {profile_dir(cur)}"]
    if cur != _sticky():
        out.append(f"  (for this launch only — Aura opens '{_sticky()}' "
                   "by default)")
    out.append("aura profile list · create <name> · use <name> · "
               "delete <name>")
    return out


def list_rows() -> list[str]:
    cur, sticky = profile_name(), _sticky()
    rows = []
    for n in names():
        mark = "→" if n == cur else " "
        tag = "  (default when Aura starts)" if n == sticky else ""
        rows.append(f"  {mark} {n:20s} {_summary(n)}{tag}")
    return rows


def create(name: str, copy_models: bool = False) -> tuple[bool, str]:
    if not valid_profile_name(name):
        return False, ("a profile name is letters, digits, '.', '_' or "
                       "'-' (up to 40)")
    if exists(name):
        return False, f"profile '{name}' already exists"
    d = profile_dir(name)
    d.mkdir(parents=True)
    note = "empty: set up its model with  aura -p " + name + " setup"
    if copy_models:
        import os
        from .profile_store import _save_file, load_unsealed
        src = load_unsealed() or {}
        if src.get("profiles"):
            # keys go into the NEW profile's own key store
            from_name, old = profile_name(), os.environ.get("AURA_PROFILE")
            os.environ["AURA_PROFILE"] = name
            try:
                _save_file({"active": src.get("active"),
                            "profiles": src.get("profiles")})
            finally:
                if old is None:
                    os.environ.pop("AURA_PROFILE", None)
                else:
                    os.environ["AURA_PROFILE"] = old
            note = (f"copied {len(src['profiles'])} model setup(s) and their "
                    f"keys from '{from_name}'")
        else:
            note = (f"'{profile_name()}' has no model setups to copy — " +
                    note)
    return True, f"created profile '{name}' — {note}"


def use(name: str) -> tuple[bool, str]:
    if not exists(name):
        return False, (f"no profile '{name}' — known: "
                       f"{', '.join(names())}")
    set_default_profile(name)
    return True, f"Aura now opens profile '{name}'"


def delete(name: str, assume_yes: bool = False) -> tuple[bool, str]:
    if name == "default":
        return False, "the default profile can't be deleted"
    if not exists(name):
        return False, f"no profile '{name}'"
    if not assume_yes:
        if not sys.stdin.isatty():
            return False, "add --yes to delete without a keyboard"
        try:
            typed = input(f"delete profile '{name}' ({_summary(name)})? "
                          f"type its name to confirm: ").strip()
        except (KeyboardInterrupt, EOFError):
            typed = ""
        if typed != name:
            return False, "kept"
    trash = aura_home() / "trash" / "profiles"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(profile_dir(name)), str(dest))
    if _sticky() == name:
        set_default_profile("default")
    return True, (f"profile '{name}' moved to {dest} — delete that folder "
                  "yourself to erase it for good")


def command(args) -> int:
    """`aura profile [list|create|use|delete] ...`"""
    action = getattr(args, "profile_cmd", None)
    if action in (None, "show"):
        print("\n".join(show()))
        return 0
    if action == "list":
        print("\n".join(list_rows()))
        return 0
    name = getattr(args, "name", "")
    if action == "create":
        ok, msg = create(name, bool(getattr(args, "copy_models", False)))
        print(msg)
        if ok and sys.stdin.isatty() and "aura -p" in msg:
            try:
                go = input("set up its model now? [Y/n] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                go = "n"
            if go in ("", "y", "yes"):
                import os
                os.environ["AURA_PROFILE"] = name
                from .setup import run_setup
                return run_setup("quick")
        return 0 if ok else 1
    if action == "use":
        ok, msg = use(name)
    elif action == "delete":
        ok, msg = delete(name, bool(getattr(args, "yes", False)))
    else:
        ok, msg = False, f"unknown action {action!r}"
    print(msg)
    return 0 if ok else 1

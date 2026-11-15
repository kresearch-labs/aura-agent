"""editors.py — connect an editor to Aura (it runs `aura acp`).

The user never edits a config file. Formats: Zed `agent_servers` in
settings.json (type custom, command, args, env); JetBrains IDEs `agent_servers`
in ~/.jetbrains/acp.json (command, args, env).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

NAME = "Aura"


def aura_command() -> str:
    found = shutil.which("aura")
    return str(Path(found).resolve()) if found else "aura"


def zed_settings() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Zed" / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "zed" / "settings.json"


def jetbrains_settings() -> Path:
    return Path.home() / ".jetbrains" / "acp.json"


EDITORS = {
    "zed": ("Zed", zed_settings, lambda cmd: {"type": "custom", "command": cmd, "args": ["acp"], "env": {}}),
    "jetbrains": ("JetBrains IDEs (IntelliJ, PyCharm, WebStorm …)", jetbrains_settings,
                  lambda cmd: {"command": cmd, "args": ["acp"], "env": {}}),
}


def _strip_comments(text: str) -> str:
    """JSON with comments (Zed's settings) → JSON: // and /* */ outside strings,
    and trailing commas."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def plan(editor: str) -> dict:
    """What connecting would do: {'label', 'path', 'entry', 'there'}."""
    label, where, make = EDITORS[editor]
    path = where()
    entry = make(aura_command())
    there = False
    try:
        cur = json.loads(_strip_comments(path.read_text(encoding="utf-8")) or "{}")
        there = (cur.get("agent_servers") or {}).get(NAME) == entry
    except (OSError, ValueError):
        pass
    return {"label": label, "path": path, "entry": entry, "there": there}


def connect(editor: str) -> str:
    """Write the entry; the old file is kept as <name>.aura-backup-<time>."""
    p = plan(editor)
    path = p["path"]
    data: dict = {}
    backup = None
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(_strip_comments(raw) or "{}")
        except ValueError as e:
            raise RuntimeError(f"{path} isn't valid JSON ({e}) — nothing was changed") from e
        backup = path.with_name(path.name + f".aura-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("agent_servers", {})[NAME] = p["entry"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return (f"{p['label']}: Aura added to {path}" + (f" (the old file is kept as {backup.name})"
                                                    if backup else "")
            + " — restart the editor, then pick Aura in its agent panel")

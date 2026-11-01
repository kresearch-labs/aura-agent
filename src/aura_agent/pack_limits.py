"""pack_limits.py — a skill pack's declared capabilities, enforced.

A skill pack is instructions the agent follows with its normal tools. Once the
agent loads a pack (skill_load), the mediator limits every later call in that
conversation to what the pack's manifest declared — so a compromised pack that
says "curl this URL" or "edit ~/.ssh" is refused in code, whatever the model
decides. Deny by default: a pack that declares nothing gets reads only.

manifest.json "capabilities":
  fs_scope file patterns the agent may write while it is active
  network_targets hosts it may reach ("docs.python.org", "*.github.com",
                     "*" = any); MCP servers as "mcp:<server>" or "mcp:*"
  credential_access names of saved secrets that may reach shell commands
                     (keybroker entries; [] = none)
  commands (optional) shell commands it may run, by prefix
                     ("pytest", "npm test"); absent = no shell

Limits come from the TRUSTED pack's own manifest: trust records a fingerprint
of every file, so the manifest cannot change without the pack dropping out of
trust. Several active packs = every pack's limits apply.

The active set is saved next to the session (sessions/<id>.packs.json), not per
turn: the pack's text stays in the conversation, so its limits must too. A new
session starts with none; resume restores them.
"""
from __future__ import annotations

import fnmatch
import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tool_names import (BROWSER_RUN, DELEGATE, FILE_WRITERS, MCP_PREFIX,
                         SHELL_TOOLS, WEB_CRAWL, WEB_SEARCH)

# Agent bookkeeping — not a reach into the project or the outside world;
# memory/skill/rule writes are separately gated by the operator (/10)
_INTERNAL = frozenset({"todo_write", "state_write", "notes_write",
                       "finding_add", "memory_write", "skill_write",
                       "rules_add", "rules_remove"})
# shell metacharacters that would let one allowed command smuggle another
_CHAIN = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r")


@dataclass
class PackLimits:
    name: str
    fs_scope: list[str] = field(default_factory=list)
    network_targets: list[str] = field(default_factory=list)
    credential_access: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def _strs(v) -> list[str]:
    return [str(x).strip() for x in v if str(x).strip()] \
        if isinstance(v, list) else []


def from_manifest(name: str, manifest: dict | None) -> PackLimits:
    caps = (manifest or {}).get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    return PackLimits(name=name,
                      fs_scope=_strs(caps.get("fs_scope")),
                      network_targets=_strs(caps.get("network_targets")),
                      credential_access=_strs(caps.get("credential_access")),
                      commands=_strs(caps.get("commands")))


def from_pack_dir(skill_dir: Path) -> PackLimits | None:
    """Limits for a pack folder, or None when it is not a pack (a plain
    skill folder has no manifest — the operator's own project skill)."""
    mf = Path(skill_dir) / "manifest.json"
    if not mf.exists():
        return None
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None                     # Unreadable = declares nothing
    return from_manifest(Path(skill_dir).name, manifest)


def _rel(path: str) -> str:
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _host_ok(lim: PackLimits, url: str) -> bool:
    host = (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    return bool(host) and any(
        t == "*" or fnmatch.fnmatch(host, t.lower())
        for t in lim.network_targets if not t.startswith("mcp:"))


def check(lim: PackLimits, tool: str, blast: str, args: dict) -> str | None:
    """Why `tool(args)` is outside what pack `lim` declared, or None."""
    args = args if isinstance(args, dict) else {}
    if tool in FILE_WRITERS:
        rel = _rel(args.get("path", ""))
        if any(fnmatch.fnmatch(rel, pat) for pat in lim.fs_scope):
            return None
        return (f"writing '{rel}' — its fs_scope allows "
                f"{lim.fs_scope or 'no writes'}")
    if tool in SHELL_TOOLS:
        cmd = " ".join(str(args.get("cmd", "")).split())
        if not lim.commands:
            return "running shell commands — it declares no commands"
        if any(c in str(args.get("cmd", "")) for c in _CHAIN):
            return ("a chained or redirected shell command (; & | ` $( > <) "
                    "— run one declared command at a time")
        if not any(cmd == c or cmd.startswith(c + " ") for c in lim.commands):
            return f"running '{cmd[:60]}' — its commands are {lim.commands}"
        if str(args.get("network", "")).lower() in ("1", "true", "yes") \
                and "*" not in lim.network_targets:
            return "a shell command with network on — that needs '*' in network_targets"
        return None
    if tool == "history_search" and str(args.get("project") or "this") not in ("this", ""):
        return "searching other projects' chats — a skill pack sees only this project's"
    if tool == "run_code":
        return "running a script — a skill pack can't (it would reach past its limits)"
    if tool == "process_input":
        return "typing into a running program — a skill pack can't"
    if tool == WEB_SEARCH:
        # the search words go to search services: a pack must declare
        # network reach at all ("*" or any target)
        if [t for t in lim.network_targets if not t.startswith("mcp:")]:
            return None
        return "searching the web — it declares no network_targets"
    if tool == WEB_CRAWL:
        if _host_ok(lim, args.get("url", "")):
            return None
        return (f"reaching {args.get('url', '?')!r} — its network_targets "
                f"are {lim.network_targets or 'none'}")
    if tool == BROWSER_RUN:
        for a in args.get("actions") or []:
            if isinstance(a, dict) and a.get("url") and \
                    not _host_ok(lim, a["url"]):
                return (f"browsing {a['url']!r} — its network_targets are "
                        f"{lim.network_targets or 'none'}")
        if not lim.network_targets:
            return "using the browser — it declares no network_targets"
        return None
    if tool.startswith(MCP_PREFIX):
        server = tool[len(MCP_PREFIX):].split("__", 1)[0]
        if {"mcp:*", f"mcp:{server}"} & set(lim.network_targets):
            return None
        return f"calling MCP server '{server}' — not in its network_targets"
    if tool == DELEGATE:
        return ("starting a helper agent — helpers can't be held to a "
                "pack's limits yet")
    if blast == "read" or tool in _INTERNAL:
        return None
    return f"'{tool}' — not covered by anything it declared"


def summary(lim: PackLimits) -> str:
    """One line for the model and the operator: what the pack may do."""
    return (f"writes: {', '.join(lim.fs_scope) or 'none'} · commands: "
            f"{', '.join(lim.commands) or 'none'} · network: "
            f"{', '.join(lim.network_targets) or 'none'} · credentials: "
            f"{', '.join(lim.credential_access) or 'none'}")


# --------------------------------------------------- per-session record --
def session_file(session_path: Path) -> Path:
    p = Path(session_path)
    return p.with_name(p.stem + ".packs.json")


def load_active(path: Path | None) -> dict[str, PackLimits]:
    if path is None or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {d["name"]: PackLimits(**d) for d in data
                if isinstance(d, dict) and d.get("name")}
    except (OSError, json.JSONDecodeError, TypeError):
        # unreadable record: fail closed — read-only until the operator looks
        return {"(unreadable pack record)": PackLimits("(unreadable pack record)")}


def save_active(path: Path | None, active: dict[str, PackLimits]) -> None:
    if path is None:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps([asdict(v) for v in active.values()],
                                     indent=1), encoding="utf-8")

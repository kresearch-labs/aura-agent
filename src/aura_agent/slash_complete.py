"""slash_complete.py — In-chat slash autocomplete.

Typing `/` shows the command menu; typing `/mo<TAB>` or `/mo<ENTER>`-style
prefix completion completes the command with its one-line description. Zero
dependencies: GNU readline when available (import guard), plain input fallback
(menu prints on '/', best-match completes on request).

Public surface:
  COMMANDS — [(name, help)] the full chat command set
  complete(prefix) — completions for a partial like "/mo"
  menu — aligned help listing (used by /help too)
"""
from __future__ import annotations

import re

from . import command_registry as _registry
from .command_registry import COMMANDS

COMMANDS: list[tuple[str, str]] = [
    (c.name, c.desc) for c in _registry.COMMANDS
]


def complete(prefix: str) -> list[tuple[str, str]]:
    """Commands + aliases matching a prefix like '/m' or '/res'.
    Bare '/' returns everything. Aliases carry '→ /real' hint."""
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    hits = [(n, d) for n, d in COMMANDS if n.startswith(prefix.lower())]
    for c in _registry.COMMANDS:
        for a in c.aliases:
            if a.startswith(prefix.lower()):
                hits.append((a, f"→ {c.name}  {c.desc}"))
    return hits


def menu() -> str:
    """Grouped help render: group header, then one line per command — `name
    description (usage: …)`. Plain string; color applied by caller. visual:
    76-col cap — usage on its own indented line when the combined line would
    overflow.
    """
    from .command_registry import groups as _groups
    out: list[str] = []
    for gname, cmds in _groups().items():
        if not cmds:
            continue
        out.append(gname)
        width = max(len(c.name) for c in cmds)
        for c in cmds:
            base = f"  {c.name:<{width}}  {c.desc}"
            usage = f"(usage: {c.usage})"
            if len(base) + 2 + len(usage) <= 78:
                out.append(f"{base}  {usage}")
            else:
                out.append(base)
                out.append(f"  {' ' * width}  {usage}")
    return "\n".join(out)


def _readline_setup() -> None:
    """Best-effort readline completion; silently degrades to plain input."""
    try:
        import readline  # noqa: F401
    except ImportError:
        return
    try:
        readline.set_completer(_rl_completer)
        readline.set_completer_delims(" \t\n")
        if "libedit" in (getattr(readline, "__doc__", "") or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        pass


_rl_state = {"matches": [], "idx": 0}


def _rl_completer(text: str, state: int):
    if state == 0:
        _rl_state["matches"] = [n for n, _ in complete(text)] if text \
            else [n for n, _ in COMMANDS]
    if state < len(_rl_state["matches"]):
        return _rl_state["matches"][state]
    return None


# ---------------------------------------------------------------------------
# Slash menu popup — descriptions as you type, conhost-safe. Typing "/" (or TAB
# at a "/..." prefix) prints matching commands with their one-line descriptions
# under the input. Pure input — no readline.
# ---------------------------------------------------------------------------

def popup_rows(prefix: str, limit: int = 8) -> list[str]:
    """Rows for the popup: `name  description` filtered by prefix."""
    p = (prefix or "/").lstrip("/").lower()
    rows = []
    for name, desc in COMMANDS:
        if not p or name.lstrip("/").startswith(p):
            rows.append(f"  {name:<11} {desc[:52]}")
            if len(rows) >= limit:
                break
    return rows


def exact_command_exists(text: str) -> bool:
    """True when text is exactly a registered command (with or without
    alias resolution)."""
    t = text.strip()
    if not t.startswith("/"):
        return False
    for name, _ in COMMANDS:
        if name == t.split()[0]:
            return True
    return False


# ------------------------------------------------------ fuzzyScore.ts ----
def _tokens(value: str) -> list[str]:
    """tokenizeSearchText: lowercase + alphanumeric word tokens."""
    norm = value.lower()
    return [norm] + [t for t in re.split(r"[^a-z0-9]+", norm) if t]


def _normalize_query(query: str) -> str:
    """normalizeSlashSearchQuery: trim, drop leading slashes, lowercase."""
    return query.strip().lstrip("/").lower()


def _score_fields(fields: list[str], query: str, offset: int) -> float:
    for f in fields:
        if f == query or f"/{f}" == query:
            return float(offset)
    for f in fields:
        if f.startswith(query) or f"/{f}".startswith(query):
            return float(offset + 1)
    for f in fields:
        if query in f:
            return float(offset + 2)
    return float("inf")


def score_menu_item(name: str, desc: str, query: str,
                    aliases: list[str] | None = None) -> float:
    """scoreSlashMenuItem (fuzzyScore.ts:52-58): command fields tier 0-2,
    description words tier +3; min of the two; Infinity = no match."""
    from .command_registry import COMMANDS as _CR
    aliases = aliases or []
    alias_list = [a.aliases for c in _CR if c.name == name for a in [c]]
    flat_aliases = [a for grp in alias_list for a in grp]
    cmd_fields = _tokens(name)
    for a in flat_aliases:
        cmd_fields.extend(_tokens(a))
    desc_fields = _tokens(desc)
    return min(_score_fields(cmd_fields, query, 0),
               _score_fields(desc_fields, query, 3))



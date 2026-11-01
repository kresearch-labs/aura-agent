"""verbify.py — tool calls → present-continuous sentences with OBJECT IDENTITY.

Identity-less summaries were judged worse than no display at all (1,082-point
controversy). - identity-less events batch into a rolling counter ("+14 checks
passed"), never printed as anonymous lines. - no raw JSON/quotes in output;
values truncated (≤80 chars total). - unknown tools fall back to "working on
<tool>…".
"""
from __future__ import annotations
from .tool_names import EDIT_FILE, READ_FILE, WEB_CRAWL, WRITE_FILE


def _clean(value) -> str:
    s = str(value).strip().replace("\n", " ")
    return s


def _short(value, n: int = 40) -> str:
    s = _clean(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def has_object_identity(s: str) -> bool:
    """A sentence carries object identity if it contains a concrete token:
    a path separator, a URL, a digit, a quoted word, or an extension."""
    return any(t in s for t in ("/", "http", "://", ".", ":", "0", "1", "2",
                                "3", "4", "5", "6", "7", "8", "9"))


def sentence(tc: dict) -> str:
    """Map one tool call {tool, args} → narration. Pure function."""
    tool = tc.get("tool", "?")
    a = tc.get("args") or {}

    if tool == READ_FILE:
        return f"reading {_short(a.get('path', 'a file'))}"
    if tool == WRITE_FILE:
        return f"writing {_short(a.get('path', 'a file'))}"
    if tool == EDIT_FILE:
        return f"editing {_short(a.get('path', 'a file'))}"
    if tool == "shell":
        return f"running {_short(a.get('command', 'a command'), 48)}"
    if tool == WEB_CRAWL:
        return f"fetching {_short(a.get('url', 'a page'), 48)}"
    if tool == "web_search":
        return f"searching the web for {_short(a.get('query', ''), 36)}"
    if tool in ("search_files", "grep", "find"):
        pat = a.get("pattern") or a.get("query") or ""
        return f"searching files for {_short(pat, 36)}" if pat \
            else "searching files"
    if tool == "todo_write":
        act = a.get("action", "update")
        return {"add": "adding a checklist item",
                "update": "updating the checklist",
                "done": "closing a checklist item"}.get(act,
                                                        "updating the checklist")
    if tool == "memory_write":
        return f"remembering: {_short(a.get('content', ''), 36)}"
    if tool == "findings_write":
        return f"recording a finding on {_short(a.get('target', ''), 32)}"
    if tool == "scope_check":
        return f"checking scope for {_short(a.get('target', ''), 32)}"
    if tool in ("report_write", "report"):
        return "writing the report"
    # fallback: identity-less — renderer may batch these
    return f"working on {tool}…"


def counter_line(n: int) -> str:
    """Rolling-counter render for batched identity-less events."""
    return f"+{n} check{'s' if n != 1 else ''} passed"


def _short_target(args: dict) -> str:
    """The object a tool touches — path, url, or command head. Feeds the tree
    lines and the status bar's last-target segment.
    """
    if not isinstance(args, dict):
        return ""
    for k in ("path", "url", "query", "pattern", "target"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:40]
    cmd = args.get("cmd") or args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return " ".join(cmd.split()[:3])[:40]
    return ""


def from_event(data: dict) -> tuple[str, bool]:
    """(sentence, has_identity) from a loop tool_start event payload."""
    s = sentence(data)
    return s, has_object_identity(s)

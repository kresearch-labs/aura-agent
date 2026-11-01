"""tool_names.py — names of tools that other modules refer to.

Renaming a tool in one place silently broke the others — e.g. the intent audit
checked for "browser" while the tool is "browser_run", so it never matched.
Refer to these constants instead; the selftest "tool-name drift guard" fails if
a policy list names a tool that is not registered.
"""
from __future__ import annotations

LIST_FILES = "list_files"
READ_FILE = "read_file"
WRITE_FILE = "write_file"
EDIT_FILE = "edit_file"
RUN_COMMAND = "run_command"
WEB_CRAWL = "web_crawl"
BROWSER_RUN = "browser_run"
DELEGATE = "delegate"
RUN_CHECKS = "run_checks"
TOOLS_FIND = "tools_find"
WEB_SEARCH = "web_search"
SEARCH_FILES = "search_files"
PROCESS_START = "process_start"
PROCESS = "process"
PROCESS_INPUT = "process_input"
CLARIFY = "clarify"
HISTORY_SEARCH = "history_search"
# tools that run a shell command: every rule for run_command applies
# (risk grade, skill-pack command limits, checkpoint before rm ...)
SHELL_TOOLS = frozenset({RUN_COMMAND, PROCESS_START})
MCP_PREFIX = "mcp__"

FILE_WRITERS = frozenset({WRITE_FILE, EDIT_FILE})

# 2.5: read-only for grading (the command it runs is gated as run_command),
# but its output differs every run — the proof check never replays it
NO_REPLAY = frozenset({RUN_CHECKS, TOOLS_FIND, WEB_SEARCH, "process", "clarify",
                       HISTORY_SEARCH})  # Past chats grow between runs # tools_find changes the tool list


def no_replay(name: str, args) -> bool:
    """Never re-run by the proof check. Reading one past chat (history_search with
    session=) is the exception: chats only grow, so the same messages read the
    same — a fact from an old chat can be proven by them.
    """
    if name == HISTORY_SEARCH and isinstance(args, dict) and args.get("session"):
        return False
    return name in NO_REPLAY

# Registered only when its optional extra is wired (roadmap), so the drift
# guard accepts it without a live registration
OPTIONAL_TOOLS = frozenset({BROWSER_RUN})

# Tools that only READ web pages: the "all websites" switch widens scope for
# these and nothing else
WEB_READERS = frozenset({WEB_CRAWL, BROWSER_RUN})

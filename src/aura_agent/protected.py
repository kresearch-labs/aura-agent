"""protected.py — workspace paths the agent may not write directly.

These files steer or record the agent itself: its scope, the operator's rules,
memory and skills (injected into every prompt), its audit trail and receipts,
saved permissions and pending approvals, undo snapshots, and scripts/commands
the operator runs. Changing them with a plain write would let the model widen
its own scope, rewrite standing orders, plant prompt text that persists into
future sessions, approve itself, or edit its own record.

Each has a proper path instead (rules tools, todo_write, memory/skill tools,
the mediator's own audit writer), which writes through Python, not through
write_file / edit_file — so locking those two tools here does not break any
feature.

`shell_writes_protected` is BEST-EFFORT: it catches ordinary shell writes (`>`,
rm, mv, sed -i,...) to these paths so the assist gate treats them as risky; a
deliberately obfuscated command can still hide a path. Real enforcement needs
an OS sandbox (roadmap 2.8).
"""
from __future__ import annotations

import re

# relative to the engagement root, compared case-insensitively
_FILES = {
    "scope.json": "the agent's own scope — only the operator changes it",
    "constraints.md": "the operator's rules — read them with `rules`; "
                      "propose a change with rules_add / rules_remove "
                      "(the operator is always asked)",
    "audit.jsonl": "the audit trail — written only by the mediator",
    "events.jsonl": "the event log — written only by Aura",
    "resolution.jsonl": "Aura's resolution log — written only by Aura",
    "todos.jsonl": "the plan — change it with todo_write",
    "tasks.json": "the task ledger — written only by Aura",
}
_DIRS = {
    "memories": "durable memory — written only through the memory tools",
    "skills": "skills — written only through the skill tools",
    "receipts": "run receipts — tamper-evident, never edited",
    "artifacts": "the artifact store — written only by Aura",
    "sessions": "session transcripts — written only by Aura",
    "pending": "pending approvals — operator only",
    "decided": "approval decisions — operator only",
    ".aura": "saved permissions and checkpoints — operator only",
    ".shadow": "undo snapshots — written only by Aura",
    "hooks": "operator scripts that run on events — operator only",
    "commands": "operator slash commands — operator only",
}


def protected_reason(rel: str) -> str | None:
    """Why `rel` (path relative to the engagement root) is protected, or
    None when the agent may write it."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return None
    low = [p.lower() for p in parts]
    if len(low) == 1 and low[0] in _FILES:
        return _FILES[low[0]]
    if low[0] in _DIRS:
        return _DIRS[low[0]]
    return None


_NAMES = "|".join(re.escape(n) for n in list(_FILES) + list(_DIRS))
# a protected name as a path segment, at the workspace root
_PATH_RE = re.compile(rf"(?:^|[\s\"'=(])(?:\./)?(?:{_NAMES})(?=$|[/\s\"');|&])", re.I)
_WRITE_RE = re.compile(
    r"(>{1,2}|\btee\b|\brm\b|\brmdir\b|\bmv\b|\bcp\b|\bsed\s+-i|\btruncate\b|"
    r"\bdd\b|\bln\b|\bchmod\b|\bchown\b|\btouch\b|\bunlink\b|"
    r"\bOut-File\b|\bSet-Content\b|\bAdd-Content\b|\bRemove-Item\b|\bdel\b)", re.I)


# The operator's home-level Aura folder (trusted publisher keys, the skills
# registry and its trust records, signing keys) — outside the workspace, so
# the file tools can't reach it, but a shell command could.
_HOME_AURA_RE = re.compile(
    r"(~|\$HOME|\$\{HOME\}|%USERPROFILE%|\$env:USERPROFILE)[/\\]\.aura\b", re.I)


def _home_aura_literal(cmd: str) -> bool:
    try:
        from .paths import aura_home
        home = aura_home()
        low = cmd.lower().replace("\\", "/")
        return home.as_posix().lower() in low
    except Exception:
        return False


def shell_writes_protected(cmd: str) -> bool:
    """Best-effort: a shell command that writes to a protected path."""
    if not cmd or not _WRITE_RE.search(cmd):
        return False
    return bool(_PATH_RE.search(cmd) or _HOME_AURA_RE.search(cmd)
                or _home_aura_literal(cmd))

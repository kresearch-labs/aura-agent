"""agents_md.py — persistent instructions file (the AGENTS.md convention).

Reads AGENTS.md (fallback CLAUDE.md) from the engagement root; the content is
injected into the system prompt after the base system block, so operator rules
survive compaction (they re-enter every turn with the system prompt).
"""
from __future__ import annotations

from pathlib import Path

INJECTION_HEADER = ("[OPERATOR INSTRUCTIONS — {filename}; these rules are "
                    "standing orders and outrank defaults]")


def load_agents_md(engagement_root: Path) -> tuple[str, str] | None:
    """Find and read AGENTS.md (fallback CLAUDE.md) from the engagement root.
    Returns (filename, content) or None. Never raises."""
    root = Path(engagement_root)
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = root / name
        if p.exists() and p.is_file():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    return name, content
            except Exception:
                continue
    return None


def inject_block(found: tuple[str, str] | None) -> str:
    """Build the system-prompt block from a load_agents_md() result."""
    if not found:
        return ""
    filename, content = found
    return f"{INJECTION_HEADER.format(filename=filename)}\n{content.strip()}"


def system_prompt_with_agents_md(base_prompt: str,
                                 engagement_root: Path) -> tuple[str, str | None]:
    """Returns (final_system_prompt, loaded_filename_or_None)."""
    found = load_agents_md(engagement_root)
    block = inject_block(found)
    if not block or found is None:
        return base_prompt, None
    return f"{base_prompt.rstrip()}\n\n{block}", found[0]

"""quality.py — what counts as a lesson.

The SWE campaign's step learner stored 1,715 "lessons" that were step noise:
"write_file: resolved this step (mid-run success)", raw tool output with line
numbers, JSON error blobs. Retrieval then ranked them above the trained seed
knowledge. One rule, used at write time (the learners) and at read time
(store.retrieve), so a fragment never becomes, or acts as, a lesson.
"""
from __future__ import annotations

import re

_FRAGMENT_MARKS = (
    "resolved this step", "(mid-run success)", "(mid-run failure)",
    "mid-run step verified", "### GROUND FROM", "### TASK CONTEXT",
    "### AGENT INPUT", "### RULES",
)
_TOOL_PREFIX = re.compile(
    r"^\s*(read_file|write_file|edit_file|search_files|run_command|web_crawl|"
    r"web_search|tools_find|list_dir|todo_write|finding_add|view_image)\b")
_NUMBERED_DUMP = re.compile(r"\b\d+:[0-9a-f]{2}\|")       # Line-tagged file output
_JSON_BLOB = re.compile(r"\{\s*\"[A-Za-z_]+\"\s*:")


def is_fragment(text: str) -> bool:
    """True when the text is step noise or raw tool output, not a reasoned
    lesson a model could use on another task."""
    t = (text or "").strip()
    if len(t) < 40:
        return True
    low = t.lower()
    if any(m.lower() in low for m in _FRAGMENT_MARKS):
        return True
    if _TOOL_PREFIX.search(t) or _NUMBERED_DUMP.search(t) or _JSON_BLOB.search(t):
        return True
    words = re.findall(r"[A-Za-z]{3,}", t)
    return len(words) < 8 or len(words) / max(1, len(t.split())) <= 0.5


def retire_fragments(store) -> int:
    """Mark lived fragment rows as retired (kept as history, never deleted,
    never retrieved). Seed rows are never touched. Returns how many."""
    n = 0
    for c in getattr(store, "cases", []):
        if c.get("origin") == "seed" or c.get("status") == "retired":
            continue
        if is_fragment(c.get("framing", "")):
            c["status"] = "retired"
            n += 1
    if n:
        store.save()
    return n


_SIT_PREFIX = re.compile(
    r"^\s*(?:[a-z_]+:\s*check,\s*[^:]*:\s*(?:a case under review\s*)?|"
    r"vocabulary pass\s+[^:]*:\s*)", re.I)


def lesson_text(case: dict) -> str:
    """The text a briefing shows for a case. Many seed rows carry a label as
    their framing ("interaction (declared expectation)", "vocabulary present
    row") and the actual lesson in the situation; show the lesson, not the
    label."""
    fr = (case.get("framing") or "").strip()
    if fr and not is_fragment(fr):
        return fr
    sit = (case.get("situation") or "").strip()
    sit = _SIT_PREFIX.sub("", sit)
    sit = re.sub(r"\s*[—-]\s*the practice (?:fails|holds) in this record\s*$", "", sit)
    return sit or fr

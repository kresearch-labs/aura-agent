"""session_titles.py — human-readable session titles + markdown export.

Title = humanized slug from the filename (auto-titled from the first user
message at creation — new_session already slugs it). Export = session jsonl →
readable markdown transcript (roles, tool calls, results).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def session_title(filename: str) -> str:
    """'20260905-124523-scan-the-web-root' → 'scan the web root'."""
    stem = Path(filename).stem
    # strip leading timestamp YYYYMMDD-HHMMSS-
    stem = re.sub(r"^\d{8}-\d{6}-", "", stem)
    return (stem.replace("-", " ").replace("_", " ").strip()
            or "(untitled)")


def export_markdown(session_path: Path, title: str | None = None) -> str:
    """Session jsonl → readable markdown. Tool calls shown as fenced code,
    tool results truncated to 40 lines each (full data lives in the file)."""
    out: list[str] = [f"# Session: {title or session_title(session_path.name)}",
                      ""]
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = rec.get("role", "?")
        if rec.get("record") == "compact":
            out += ["> [compaction applied — earlier turns summarized]", ""]
            continue
        content = str(rec.get("content", "") or "")
        tcs = rec.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                fn = tc.get("function", {})
                out += [f"**assistant → tool `{fn.get('name', '?')}`**", "",
                        "```json", str(fn.get("arguments", "")), "```", ""]
            if content:
                out += [content, ""]
            continue
        if role == "tool":
            lines = content.splitlines()
            body = "\n".join(lines[:40])
            if len(lines) > 40:
                body += f"\n… (+{len(lines) - 40} more lines)"
            out += ["<details><summary>tool result</summary>", "",
                    "```", body, "```", "", "</details>", ""]
            continue
        label = {"user": "## you", "assistant": "## agent",
                 "system": "<!-- system -->"}.get(role, f"## {role}")
        if role == "system" and not content:
            continue
        out += [label if role != "system" else f"<!-- system: {content[:60]} -->",
                "" if role == "system" else "", content if role != "system" else "",
                ""] if role != "system" else []
    return "\n".join(out).rstrip() + "\n"

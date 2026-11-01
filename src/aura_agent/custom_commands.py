"""custom_commands.py — User-defined slash commands from.md files.

  engagement/commands/review.md → /review <args>
  ~/.aura/profiles/<profile>/commands/*.md → the profile's commands

Frontmatter (optional YAML-ish, parsed leniently):
  ---
  description: one-line help
  agent: worker (ignored for now — future lanes)
  ---
  Prompt body. $ARGUMENTS expands to everything after the command name;
  $1..$9 expand to positional words.

Public surface:
  discover(eng) -> dict[name, CustomCommand]
  expand(cmd, args) -> prompt text
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CustomCommand:
    name: str
    body: str
    description: str = ""
    source: str = ""            # 'engagement' | 'global'
    path: Path | None = None


def discover(eng: Path) -> dict[str, CustomCommand]:
    """engagement commands shadow global ones."""
    out: dict[str, CustomCommand] = {}
    eng_dir = Path(eng) / "commands"
    if eng_dir.exists():
        for p in sorted(eng_dir.glob("*.md")):
            out[p.stem] = _parse_md_file(p, "engagement")
    from .paths import profile_path
    home_dir = profile_path("commands")       # The profile's commands
    if home_dir.exists():
        for p in sorted(home_dir.glob("*.md")):
            out.setdefault(p.stem, _parse_md_file(p, "global"))
    # Slash commands carried by TRUSTED packs, as /<pack>:<command>
    try:
        from .packs import trusted_packs
        for pack in trusted_packs(eng):
            for p in sorted((pack / "commands").glob("*.md")):
                out.setdefault(f"{pack.name}:{p.stem}", _parse_md_file(p, f"pack {pack.name}"))
    except Exception:
        pass
    return out


def _parse_md_file(p: Path, source: str) -> CustomCommand:
    return _parse_md_impl(p, source)


def _parse_md_impl(p: Path, source: str) -> CustomCommand:
    raw = p.read_text(encoding="utf-8", errors="replace")
    desc = ""
    body = raw
    if raw.lstrip().startswith("---"):
        m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.S)
        if m:
            front, body = m.group(1), m.group(2)
            dm = re.search(r"description:\s*(.+)", front)
            if dm:
                desc = dm.group(1).strip()
    return CustomCommand(name=p.stem, body=body.strip(), description=desc,
                         source=source, path=p)


def expand(cmd: CustomCommand, argstring: str) -> str:
    """$ARGUMENTS → full argstring; $1..$9 → positional words."""
    out = cmd.body.replace("$ARGUMENTS", argstring)
    words = argstring.split()

    def _pos(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        return words[i] if 0 <= i < len(words) else ""

    return re.sub(r"\$(\d)", _pos, out)

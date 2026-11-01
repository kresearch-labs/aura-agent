"""docs_map.py

Aura's check needs no network and no install: every RELATIVE link and image in
the repo's Markdown files must point at a file or folder that exists, and a
`#heading` part must match a heading in that file (GitHub's slug: lower case,
punctuation dropped, spaces to dashes). Web links are left alone — checking
them needs the network and they change on their own. docs-map.json (optional)
names extra files that must exist.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MAP_PATH = "docs-map.json"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "testkit/results"}
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REF = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.M)
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)


def load_docs_map(repo: Path) -> list[dict]:
    p = Path(repo) / MAP_PATH
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _slug(heading: str) -> str:
    h = re.sub(r"[`*_~]|<[^>]+>", "", heading.strip().lower())
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _anchors(md: Path) -> set[str]:
    out, seen = set(), {}
    try:
        text = _FENCE.sub("", md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return out
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        s = _slug(m.group(1))
        n = seen.get(s, 0)
        out.add(s if n == 0 else f"{s}-{n}")
        seen[s] = n + 1
    out |= set(re.findall(r'<a\s+(?:name|id)="([^"]+)"', text))
    return out


def markdown_files(repo: Path) -> list[Path]:
    repo = Path(repo)
    out = []
    for p in repo.rglob("*.md"):
        rel = p.relative_to(repo).as_posix()
        if any(rel == d or rel.startswith(d + "/") or f"/{d}/" in f"/{rel}" for d in SKIP_DIRS):
            continue
        out.append(p)
    return sorted(out)


def check_links(repo: Path, mapping: list[dict] | None = None) -> list[str]:
    """Broken links as 'file:line → target (why)'; [] = all good."""
    repo = Path(repo)
    broken = [f"{MAP_PATH} → {e.get('path', '?')} (missing)"
              for e in (mapping or []) if not (repo / e.get("path", "")).exists()]
    cache: dict = {}
    for md in markdown_files(repo):
        try:
            raw = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
        text = re.sub(r"`[^`\n]*`", "", text)               # Inline code isn't a link
        targets = [(m.start(), m.group(1)) for m in _LINK.finditer(text)]
        targets += [(m.start(), m.group(1)) for m in _REF.finditer(text)]
        for pos, t in targets:
            if re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) or t.startswith("//"):
                continue                                    # Web, mailto …: not checked
            path, _, frag = t.partition("#")
            dest = md if not path else (md.parent / path.split("?")[0])
            line = text.count("\n", 0, pos) + 1
            where = f"{md.relative_to(repo).as_posix()}:{line} → {t}"
            if not dest.exists():
                broken.append(f"{where} (no such file)")
                continue
            if frag and dest.suffix.lower() == ".md":
                if dest not in cache:
                    cache[dest] = _anchors(dest)
                if frag.lower() not in cache[dest] and frag not in cache[dest]:
                    broken.append(f"{where} (no such heading)")
    return broken

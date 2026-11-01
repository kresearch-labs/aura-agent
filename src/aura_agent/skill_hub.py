"""skill_hub.py — find and add public skills (agentskills.io SKILL.md) from GitHub
skill repositories.

Aura: search the repositories in `skills.sources` (default anthropics/skills)
with one GitHub call per repo, read the matching skills' descriptions, and add
one as an UNTRUSTED pack — its source and licence recorded; it loads only after
you review it and `aura skills adopt <name>` (signed with your own key). Skill
folders can carry scripts, so nothing is trusted on download.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

DEFAULT_SOURCES = ("anthropics/skills",)
MAX_FILE = 2_000_000          # Bytes per file
MAX_FILES = 60


def _get(url: str, fetch=None) -> bytes:
    if fetch:
        return fetch(url)
    req = urllib.request.Request(url, headers={"User-Agent": "aura-agent",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=20).read()


def sources() -> list[str]:
    try:
        from .options import get
        v = get("skills.sources")
        if v:
            return [s.strip() for s in str(v).split(",") if s.strip()]
    except Exception:                                    # noqa: BLE001
        pass
    return list(DEFAULT_SOURCES)


def _tree(repo: str, fetch=None) -> list[str]:
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", fetch))
    return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for ln in (m.group(1).splitlines() if m else []):
        k, _, v = ln.partition(":")
        if k.strip() in ("name", "description", "license"):
            out[k.strip()] = v.strip().strip('"')
    return out


_OPEN = ("apache", "mit", "bsd", "isc", "mpl", "cc0", "cc-by", "unlicense", "0bsd", "zlib")


def licence_note(lic: str) -> str:
    """Plain words for a skill's licence: open, or 'read its terms first'."""
    low = (lic or "").lower()
    if any(re.search(r"(?<![a-z])" + re.escape(o) + r"(?![a-z])", low) for o in _OPEN):
        return lic
    return f"{lic or 'not stated'} — NOT an open licence: read its terms before using it"


def search(words: str, fetch=None, limit: int = 10) -> list[dict]:
    """Skills whose folder name or description matches the words."""
    terms = [w.lower() for w in re.findall(r"\w+", words)]
    out = []
    for repo in sources():
        try:
            paths = _tree(repo, fetch)
        except Exception as e:                           # noqa: BLE001
            out.append({"repo": repo, "error": str(e)[:120]})
            continue
        for p in paths:
            if not p.endswith("/SKILL.md") or p.startswith("template/"):
                continue
            folder = p[: -len("/SKILL.md")]
            name = folder.rsplit("/", 1)[-1]
            hay = name.lower().replace("-", " ")
            try:
                fm = _frontmatter(_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{p}",
                                       fetch).decode("utf-8", "replace")) \
                    if not terms or any(t in hay for t in terms) else None
            except Exception:                            # noqa: BLE001
                fm = {}
            if fm is None:
                continue
            desc = fm.get("description", "")
            if terms and not any(t in (hay + " " + desc.lower()) for t in terms):
                continue
            lic = next((f for f in paths if f.startswith(folder + "/") and
                        f.rsplit("/", 1)[-1].upper().startswith("LICENSE")), "")
            out.append({"repo": repo, "folder": folder, "name": name, "description": desc[:200],
                        "license": licence_note(fm.get("license") or (lic.rsplit("/", 1)[-1] if lic else ""))})
            if len([o for o in out if "name" in o]) >= limit:
                return out
    return out


def add(repo: str, folder: str, registry: Path, fetch=None) -> dict:
    """Download one skill folder as an untrusted pack (source + licence kept)."""
    name = "".join(c for c in folder.rsplit("/", 1)[-1] if c.isalnum() or c in "-_")[:40]
    dest = Path(registry) / name
    if dest.exists():
        return {"ok": False, "error": f"'{name}' is already installed (remove it first)"}
    files = [p for p in _tree(repo, fetch) if p.startswith(folder + "/")][:MAX_FILES]
    if not any(p.endswith("/SKILL.md") for p in files):
        return {"ok": False, "error": f"no SKILL.md in {repo}/{folder}"}
    skill_dir = dest / "skills" / name
    lic, scripts = "not stated", []
    for p in files:
        data = _get(f"https://raw.githubusercontent.com/{repo}/HEAD/{p}", fetch)
        if len(data) > MAX_FILE:
            continue
        rel = p[len(folder) + 1:]
        target = (skill_dir / rel).resolve()
        if skill_dir.resolve() not in target.parents:
            continue                                     # Never outside the pack
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if rel.rsplit("/", 1)[-1].upper().startswith("LICENSE"):
            lic = rel
        if rel.endswith((".py", ".sh", ".js", ".ps1", ".ts")):
            scripts.append(rel)
    fm = _frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    manifest = {"name": name, "version": "", "description": fm.get("description", "")[:300],
                "source": f"github:{repo}/{folder}", "license": fm.get("license") or lic,
                "capabilities": {"fs_scope": [], "network_targets": [], "commands": [],
                                 "mcp_servers": []}}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    from .skills_registry import _fingerprint, _load_trust, _save_trust
    t = _load_trust(Path(registry))
    t[name] = {"verified": False, "fingerprint": _fingerprint(dest), "installed": time.time(),
               "imported": manifest["source"]}
    _save_trust(Path(registry), t)
    return {"ok": True, "name": name, "path": str(dest), "license": licence_note(manifest["license"]),
            "scripts": scripts}

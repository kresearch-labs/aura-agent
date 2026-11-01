"""skills.py — procedural memory (agentskills.io SKILL.md format).

A skill = a directory with SKILL.md (frontmatter: name, description; body: the
procedure). The agent indexes name+description for free and loads the full body
only when a task matches — progressive disclosure, the 2026 cross-lab converged
standard.

Layout (per engagement + global fallback):
    <engagement>/skills/<skill-name>/SKILL.md
    ~/.aura/profiles/<profile>/skills/<skill-name>/SKILL.md

Agent-created skills: after a complex turn (>= SKILL_SUGGEST_TOOL_CALLS tool
calls), the REPL offers to save the procedure; the model can also call
skill_write (mediator WRITE-scoped) directly.

Cache note: skill INDEX (name+description, one compact block) is injected each
turn — small and static per engagement; full bodies enter only on activation
(volatile content stays out of the static prefix).
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL_FILE = "SKILL.md"
SKILL_SUGGEST_TOOL_CALLS = 8      # Complexity heuristic for "save that?"
MAX_INDEX_CHARS = 1500            # Index block budget (progressive disclosure)


def _parse_skill_md(text: str) -> dict | None:
    """Parse frontmatter (--- name: ... description: ... ---) + body."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None
    meta: dict = {}
    for ln in m.group(1).splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    if not meta.get("name"):
        return None
    return {"name": meta["name"],
            "description": meta.get("description", ""),
            "body": (m.group(2) or "").strip()}


def _trusted_here(skill_dir: Path, trust: dict, is_global: bool) -> bool:
    """Loader trust gate. A pack (has manifest.json) loads only when `aura skills
    trust` recorded it AND its content still matches the fingerprint taken then
    — any later edit drops it. A plain skill folder (no manifest, so no
    signature) loads only from the engagement, where it is the operator's own
    project skill; in the global registry nothing vouches for it.
    """
    if (skill_dir / "manifest.json").exists():
        from .skills_registry import _fingerprint
        entry = trust.get(skill_dir.name, {})
        return bool(entry.get("trusted")
                    and entry.get("fingerprint") == _fingerprint(skill_dir))
    return not is_global


def untrusted_skills(eng: Path, global_home: Path | None = None) -> list[str]:
    """Skill folders present on disk but refused by the trust gate."""
    out = []
    for d, is_global in ((_global_home(global_home), True),
                         (Path(eng) / "skills", False)):
        if not d.exists():
            continue
        from .skills_registry import _load_trust
        trust = _load_trust(d)
        for skill_dir in sorted(d.iterdir()):
            if (skill_dir / SKILL_FILE).exists() and \
                    not _trusted_here(skill_dir, trust, is_global):
                out.append(skill_dir.name)
    return out


def _global_home(global_home: Path | None) -> Path:
    if global_home is not None:
        return Path(global_home)
    from .paths import profile_path
    return profile_path("skills")             # The profile's skills


def _load_dir(d: Path, is_global: bool = False) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not d.exists():
        return out
    from .skills_registry import _load_trust
    trust = _load_trust(d)
    for skill_dir in sorted(d.iterdir()):
        if not skill_dir.is_dir():
            continue
        files = [skill_dir / SKILL_FILE] if (skill_dir / SKILL_FILE).exists() else []
        # A pack may carry several skills (skills/<name>/SKILL.md)
        if (skill_dir / "manifest.json").exists() and (skill_dir / "skills").is_dir():
            files += sorted((skill_dir / "skills").glob(f"*/{SKILL_FILE}"))
        if not files:
            continue
        if not _trusted_here(skill_dir, trust, is_global):
            continue                      # Installed but not trusted
        for f in files:
            try:
                parsed = _parse_skill_md(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if parsed:
                parsed["path"] = str(f)
                parsed["pack_dir"] = (str(skill_dir)
                                      if (skill_dir / "manifest.json").exists()
                                      else None)
                parsed["source"] = "engagement" if "engagement" in str(d) \
                    else "global"
                out[parsed["name"]] = parsed
    return out


def builtin_skills() -> dict[str, dict]:
    """Skills that ship with Aura (written for it, Apache-2.0 like Aura)."""
    out = _load_dir(BUILTIN_DIR, is_global=False)
    for v in out.values():
        v["source"] = "built-in"
    return out


_COMMON = {"the", "and", "for", "with", "from", "that", "this", "into", "what", "when", "how",
           "you", "your", "its", "are", "can", "all", "one", "out", "before", "after", "about",
           "please", "make", "use", "using", "get", "new", "add", "file", "files", "project",
           "code", "work", "some", "any", "not", "but", "does", "has", "have", "need", "want"}


def load_skills(eng: Path, global_home: Path | None = None,
                builtin: bool = True) -> dict[str, dict]:
    """Engagement skills shadow global ones (same name = engagement wins).
    Only skills that pass the trust gate are returned (see _trusted_here).
    builtin: include the skills that ship with Aura (lowest priority)."""
    merged = builtin_skills() if builtin else {}
    merged.update(_load_dir(_global_home(global_home), is_global=True))
    merged.update(_load_dir(Path(eng) / "skills", is_global=False))
    return merged


BUILTIN_DIR = Path(__file__).parent / "skills_builtin"


MAX_INDEX_NAMES_CHARS = 600      # The skills that don't fit: names only


def skill_index_block(skills: dict[str, dict], task: str = "",
                      usage: dict | None = None) -> str:
    """Compact index for system-prompt injection: name + description only.
    Progressive disclosure — bodies load on activation via skill_load.
    """
    if not skills:
        return ""
    usage = usage or {}
    words = {w for w in re.findall(r"[a-z]{3,}", (task or "").lower())} - _COMMON

    def rank(item):
        name, s = item
        u = usage.get(name) or {}
        sw = set(re.findall(r"[a-z]{3,}", f"{name.replace('-', ' ')} {s.get('description', '')}".lower()))
        return (not u.get("pinned"), -len(words & sw), -int(u.get("uses") or 0), name)

    lines = ["[SKILLS AVAILABLE — call skill_load(name) for the full "
             "procedure when relevant]"]
    budget = MAX_INDEX_CHARS
    ordered = sorted(skills.items(), key=rank)
    rest: list[str] = []
    for name, s in ordered:
        ln = f"- {name}: {s['description'][:100]}"
        if rest or budget - len(ln) < 0:
            rest.append(name)
            continue
        lines.append(ln)
        budget -= len(ln)
    if rest:
        named, size = [], 0
        for n in rest:
            if size + len(n) + 2 > MAX_INDEX_NAMES_CHARS:
                break
            named.append(n)
            size += len(n) + 2
        more = len(rest) - len(named)
        lines.append("- also: " + ", ".join(named)
                     + (f" (+{more} more)" if more else ""))
    return "\n".join(lines)


MAX_SKILL_BODY_CHARS = 8000       # Loaded whole on skill_load
MAX_SKILL_DESC_CHARS = 160        # Injected every turn in the index


def _safe_name(name) -> str:
    return re.sub(r"[^a-z0-9-]", "-", str(name).lower()).strip("-")[:40] \
        or "skill"


def skill_problem(eng: Path, args: dict) -> str | None:
    """Why this skill_write is refused outright (checked before asking)."""
    name = str((args or {}).get("name", "")).strip()
    desc = str((args or {}).get("description", "")).strip()
    body = str((args or {}).get("body", "")).strip()
    if not name or not body:
        return "skill needs name and body"
    if "\n" in desc or "\r" in desc:
        # a newline in the frontmatter could add or override keys
        return "description is one line"
    if len(desc) > MAX_SKILL_DESC_CHARS:
        return f"description is at most {MAX_SKILL_DESC_CHARS} characters"
    if len(body) > MAX_SKILL_BODY_CHARS:
        return f"body is at most {MAX_SKILL_BODY_CHARS} characters"
    d = Path(eng) / "skills" / _safe_name(name)
    if (d / "manifest.json").exists():
        return (f"'{_safe_name(name)}' is an installed skill pack — it is "
                "never overwritten; pick another name")
    return None


def write_skill(root: Path, name: str, description: str, body: str,
                scope: str = "engagement") -> Path:
    """Create/update a skill dir + SKILL.md (agentskills.io format)."""
    safe = _safe_name(name)
    base = Path(root) / "skills" if scope == "engagement" else \
        Path(root) / "skills"   # Global handled by caller passing home
    d = base / safe
    d.mkdir(parents=True, exist_ok=True)
    front = (f"---\nname: {safe}\n"
             f"description: {description.strip()[:160]}\n---\n")
    (d / SKILL_FILE).write_text(frontmatter_wrap(front=front, body=body),
                                encoding="utf-8")
    return d / SKILL_FILE


def frontmatter_wrap(front: str, body: str) -> str:
    return front + (body.strip() + "\n" if body.strip() else "")


def register_skills(mediator, fs_dispatch: dict, eng: Path) -> None:
    """skill_load (READ, plan-safe) + skill_write (WRITE) + index injection."""
    from .mediator import BlastRadius, ToolSpec
    skills = load_skills(eng)

    def skill_load(args) -> str:
        name = str(args.get("name", "")).strip()
        s = load_skills(eng).get(name)
        if s is None:
            if name in untrusted_skills(eng):
                return (f"refused: skill pack '{name}' is installed but not "
                        "trusted (or changed since it was trusted). The "
                        "operator can review it and run: aura skills trust "
                        f"{name}")
            return f"error: no skill '{name}' — known: " \
                   f"{', '.join(sorted(load_skills(eng))) or 'none'}"
        note = ""
        if s.get("pack_dir"):
            # A pack's declared limits hold from here on
            from .pack_limits import from_pack_dir, summary
            lim = from_pack_dir(Path(s["pack_dir"]))
            if lim is not None:
                mediator.activate_pack(lim)
                note = (f"\n[PACK LIMITS — '{lim.name}' is a skill pack; for "
                        f"the rest of this conversation you may only: "
                        f"{summary(lim)}. Anything else is refused.]")
        return f"[SKILL {name} — full procedure]\n{s['body']}{note}"

    def skill_write(args) -> str:
        problem = skill_problem(eng, args)
        if problem:
            return f"refused: {problem}"
        name = str(args.get("name", "")).strip()
        desc = str(args.get("description", "")).strip()
        body = str(args.get("body", "")).strip()
        p = write_skill(eng, name, desc, body, scope="engagement")
        return f"skill saved: {p}"

    def ask_when(args) -> str | None:
        if skill_problem(eng, args):
            return None                    # Refused anyway — don't ask
        old = Path(eng) / "skills" / _safe_name(args.get("name", "")) / SKILL_FILE
        if old.exists():
            return "replaces an existing skill — a skill is instructions for future runs"
        from .skill_pick import look_alike
        twin = look_alike(load_skills(eng, builtin=False), _safe_name(args.get("name", "")),
                          str(args.get("description", "")))
        if twin:
            return (f"looks like your skill '{twin}' — saving it as a new skill; to "
                    f"improve '{twin}' instead, say no and ask to update '{twin}'")
        return "a skill is instructions for future runs"

    def describe(args) -> str:
        body = str(args.get("body", ""))
        return f"size: {len(body)} chars, {len(body.splitlines())} lines"

    mediator.register(ToolSpec("skill_load", BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    fs_dispatch["skill_load"] = skill_load
    mediator.register(ToolSpec("skill_write", BlastRadius.WRITE, lambda a: None,
                               ask_when=ask_when, describe=describe))
    fs_dispatch["skill_write"] = skill_write


def suggest_skill(rec, tool_call_count: int) -> str | None:
    """Complexity heuristic: >=SKILL_SUGGEST_TOOL_CALLS tool calls in one
    turn → the REPL offers to save the procedure as a skill. Returns the
    suggestion text or None."""
    if rec is None or getattr(rec, "error", None):
        return None
    if tool_call_count >= SKILL_SUGGEST_TOOL_CALLS:
        return ("complex task completed — save this approach as a skill? "
                "(skill_write(name=…, description=…, body=…)) or tell me "
                "the steps to keep")
    return None

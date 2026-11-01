"""constraints.py — file-backed operator constraints, pointer-injected.

The compaction-cliff fix: safety rules and hard constraints die in narrative
summarization — 53% survive one compaction, 10% survive five. Instead of ever
letting them enter the summary squeeze, they live in engagement/constraints.md
(a plain file compaction cannot touch) and enter context as:

  1. a FIXED-SIZE pointer block every turn (~20 tokens, one line per rule
     truncated — bounded no matter how many rules exist), and
  2. full rule text pulled ONLY at write/execute trigger points (mediator
     pre-flight) or on explicit `rules` tool read — progressive disclosure.

Design rules: - Never raises. Missing/corrupt file = no constraints, empty
blocks. - Operator writes the file directly or with rules. The agent can only
propose changes (rules_add / rules_remove), and the operator is always asked
first. - The agent cannot edit constraints without the write going through the
mediator (audited like everything else).
"""
from __future__ import annotations

import re
from pathlib import Path

CONSTRAINTS_FILE = "constraints.md"
CONSTRAINTS_HEADER = """# Operator Constraints (hard rules)
<!-- one rule per line, starting with '- '. Injected as pointers every -->
<!-- turn; full text is read before write/execute actions. Edit freely. -->
"""


def _path(root: Path) -> Path:
    return Path(root) / CONSTRAINTS_FILE


def _read_rules(root: Path) -> list[str]:
    """Rule lines, cleaned. Never raises."""
    p = _path(root)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("- ") and not s.startswith("<!--"):
            rule = s[2:].strip()
            if rule:
                out.append(rule)
    return out


def ensure_file(root: Path) -> Path:
    p = _path(root)
    if not p.exists():
        p.write_text(CONSTRAINTS_HEADER, encoding="utf-8")
    return p


def add_rule(root: Path, text: str) -> str:
    """Append one rule (dedup case-insensitive). Returns the stored line."""
    p = ensure_file(root)
    text = text.strip().rstrip(".")
    existing = {r.lower() for r in _read_rules(root)}
    if text.lower() in existing:
        return f"already constrained: {text}"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {text}\n")
    return f"constrained: {text}"


def resolve_rule(root: Path, index_or_text: str) -> str | None:
    """The rule a 1-based index or exact text refers to, or None."""
    rules = _read_rules(root)
    key = str(index_or_text).strip().rstrip(".").lower()
    if key.isdigit():
        i = int(key) - 1
        return rules[i] if 0 <= i < len(rules) else None
    return next((r for r in rules if r.lower() == key), None)


def remove_rule(root: Path, index_or_text: str) -> str:
    """Remove by 1-based index or exact text. Never raises."""
    p = _path(root)
    target = resolve_rule(root, index_or_text)
    if target is None:
        return f"no such rule: {index_or_text[:60]}"
    lines, keep = p.read_text(encoding="utf-8").splitlines(), []
    removed = False
    for ln in lines:
        if not removed and ln.strip() == f"- {target}":
            removed = True
            continue
        keep.append(ln)
    p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return f"removed: {target}"


# --------------------------------------------------------------- injection ---

def pointer_block(root: Path, max_rules: int = 8, width: int = 72) -> str:
    """FIXED-SIZE per-turn block: one truncated line per rule, plus the file
    pointer. Cost is bounded (max_rules × width) no matter how many rules
    the operator adds."""
    rules = _read_rules(root)
    if not rules:
        return ""
    shown = rules[:max_rules]
    # the rules live in the project's notebook, out of the file tools'
    # reach — the `rules` tool reads them in full
    lines = [f"[CONSTRAINTS — {len(rules)} operator rule(s); read them in "
             "full with the `rules` tool before any file write or shell "
             "command. These override defaults.]"]
    for i, r in enumerate(shown, 1):
        r = r if len(r) <= width else r[: width - 3] + "..."
        lines.append(f"{i}. {r}")
    if len(rules) > max_rules:
        lines.append(f"… +{len(rules) - max_rules} more in {CONSTRAINTS_FILE}")
    return "\n".join(lines)


def full_block(root: Path) -> str:
    """Complete rules text — injected at write/execute trigger points, or
    when the agent reads the file itself."""
    rules = _read_rules(root)
    if not rules:
        return ""
    lines = ["[OPERATOR CONSTRAINTS — binding, full text. A rule breach is a "
             "task failure even if the task otherwise succeeds.]"]
    for i, r in enumerate(rules, 1):
        lines.append(f"{i}. {r}")
    return "\n".join(lines)


def relevant_block(root: Path, args: dict, max_rules: int = 6) -> str:
    """Rules whose keywords intersect the tool args (path/cmd/host...).
    Falls back to full_block when nothing matches — never silently empty
    on a write action."""
    blob = " ".join(str(v) for v in (args or {}).values()).lower()
    words = {w for w in re.split(r"\W+", blob) if len(w) > 3}
    scored: list[tuple[int, str]] = []
    for r in _read_rules(root):
        rwords = {w.lower() for w in re.split(r"\W+", r) if len(w) > 3}
        overlap = len(words & rwords)
        if overlap:
            scored.append((overlap, r))
    scored.sort(key=lambda t: -t[0])
    top = [r for _, r in scored[:max_rules]]
    if not top:
        return full_block(root)
    lines = [f"[OPERATOR CONSTRAINTS — most relevant to this action; full "
             f"text in {CONSTRAINTS_FILE}]"]
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. {r}")
    return "\n".join(lines)

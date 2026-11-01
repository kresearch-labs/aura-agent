"""anaphora.py — deterministic anaphora/scope resolution (v2).

Closes the last gap: "do it" / "that task" / "resume the fix" must bind to
WHICH task. Design constraints (carried from): - No LLM calls. Deterministic,
auditable, cheap (this runs every turn). - Never resurrect silently: the
resolution is INJECTED as evidence in the scope block, and the ledger is the
binding source of truth. - Ambiguity is surfaced, not guessed: when a pronoun
cannot be bound to exactly one live task, the scope block says so explicitly
and names the candidates (the model then asks or picks with the operator's
context — it does not silently choose).

Binding algorithm (ordered):
  1. Explicit id reference: "T7", "task 3", "the auth task" (ledger id).
  2. Unique keyword overlap between the pronoun-bearing prompt and exactly
     one OPEN (non-dead) ledger entry's description.
  3. Exactly one open task in the ledger -> unambiguous by elimination.
  4. Otherwise: unresolved -> the scope block lists live candidates and
     instructs the model to ask, not assume.

The resolved binding is APPENDED to the scope message as a machine block.
Failure to bind is equally explicit: [UNRESOLVED REFERENCE] + candidates +
ask-instruction.
"""
from __future__ import annotations

import re
from pathlib import Path

from .taskstate import load_ledger

DEAD = {"done", "cancelled", "superseded"}

# Pronoun/pro-forma anchors that mark a message as anaphoric.
ANAPHORA_PATTERNS: list[tuple[str, str]] = [
    (r"^\s*(run|do|execute|handle|start|resume|continue|finish|retry)\s+"
     r"(it|this|that|them|the\s+\w+)\s*[.!]?\s*$", "bare imperative"),
    (r"\b(it|that|this|them)\b(?=\s*(task|fix|issue|one|again)\b)", "pronoun+noun"),
    (r"\bthe\s+(?:same|above|previous|earlier)\b", "ordinal reference"),
    (r"\b(?:continue|resume|retry)\b", "continuation verb"),
    (r"\bdo\s+it\b|\bdid\s+you\s+(?:understand|finish)\b|\bis\s+it\s+done\b",
     "conversational follow-up"),
    # "fix/handle <known noun>" with no new specifics — anaphoric when the
    # message carries a ledger keyword but no new file/target of its own
    (r"^(?:please\s+)?(?:fix|patch|update|finish|complete)\b", "bare fix-verb"),
]


def is_anaphoric(message: str) -> tuple[bool, list[str]]:
    """True when the message leans on prior context for its object."""
    text = (message or "").strip()
    if not text:
        return False, []
    hits: list[str] = []
    for pat, why in ANAPHORA_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            hits.append(why)
    # "it"/"that" as object with no other noun anchor in a short message
    words = text.split()
    if len(words) <= 8 and re.search(r"\b(it|that|this|them)\b", text, re.I):
        hits.append("short pronoun-bearing turn")
    return bool(hits), hits


def _explicit_id(message: str, entries: list[dict]) -> str | None:
    """Binding rule 1: an explicit ledger id in the message."""
    ids = {e["id"].lower(): e["id"] for e in entries if e.get("id")}
    m = re.search(r"\b([A-Za-z]\d{1,3})\b", message)
    if m and m.group(1).lower() in ids:
        return ids[m.group(1).lower()]
    m = re.search(r"\btask\s*#?(\d{1,3})\b", message, re.I)
    if m:
        n = int(m.group(1))
        for i, e in enumerate(entries):
            if i + 1 == n:
                return e["id"]
    return None


def _keyword_overlap(message: str, entries: list[dict]) -> str | None:
    """Binding rule 2: exactly one open entry shares a salient keyword."""
    stop = {"the", "a", "an", "and", "or", "to", "for", "of", "in", "on",
            "with", "task", "fix", "do", "it", "that", "this", "please",
            "again", "resume", "continue", "run", "finish"}
    words = {w.lower().strip(".,!?;:'\"()") for w in re.split(r"\W+", message)}
    words = {w for w in words if len(w) > 3 and w not in stop}
    if not words:
        return None
    open_entries = [e for e in entries if e.get("status") not in DEAD]
    matches: list[str] = []
    for e in open_entries:
        desc_words = {w.lower() for w in re.split(r"\W+", e.get("desc", ""))}
        if words & desc_words:
            matches.append(e["id"])
    return matches[0] if len(matches) == 1 else None


def _sole_open(entries: list[dict]) -> str | None:
    """Binding rule 3: only one live task exists — no ambiguity possible."""
    open_entries = [e for e in entries if e.get("status") not in DEAD]
    return open_entries[0]["id"] if len(open_entries) == 1 else None


def _closed_keyword(message: str, entries: list[dict]) -> str | None:
    """Dead entries whose keywords match the message uniquely. Returned as a
    closed_match (confirm-first), never a binding — resurrection guard."""
    stop = {"the", "a", "an", "and", "or", "to", "in", "with", "task", "do"}
    words = {w.lower().strip(".,!?;:'\"()") for w in re.split(r"\W+", message)}
    words = {w for w in words if len(w) > 3 and w not in stop}
    if not words:
        return None
    hits = [e for e in entries
            if e.get("status") in DEAD
            and words & {w.lower() for w in re.split(r"\W+", e.get("desc", ""))}]
    return hits[0]["id"] if len(hits) == 1 else None


def resolve_reference(message: str, root: Path) -> dict:
    """Bind an anaphoric message to a ledger task. Returns:
    {anaphoric, binding, via, binding_status, candidates, entry, closed_match}
    closed_match: a DEAD entry whose keywords match uniquely — surfaced as
    'likely but closed; confirm before resurrecting' rather than bound."""
    entries = load_ledger(root)
    anaphoric, hits = is_anaphoric(message)
    if not anaphoric or not entries:
        return {"anaphoric": anaphoric, "binding": None, "via": None,
                "binding_status": None,
                "candidates": [e["id"] for e in entries
                               if e.get("status") not in DEAD],
                "entry": None, "closed_match": None, "hits": hits}
    for rule, fn in (("explicit id", lambda: _explicit_id(message, entries)),
                     ("keyword match", lambda: _keyword_overlap(message, entries)),
                     ("closed keyword match", lambda: _closed_keyword(message, entries)),
                     ("sole open task", lambda: _sole_open(entries))):
        bid = fn()
        if bid:
            entry = next((e for e in entries if e["id"] == bid), None)
            if rule == "closed keyword match":
                # dead entry matches uniquely: surface for confirmation,
                # never silently bind (resurrection guard)
                return {"anaphoric": True, "binding": None, "via": rule,
                        "binding_status": entry.get("status") if entry else None,
                        "candidates": [e["id"] for e in entries
                                       if e.get("status") not in DEAD],
                        "entry": entry, "closed_match": bid, "hits": hits}
            return {"anaphoric": True, "binding": bid, "via": rule,
                    "binding_status": entry.get("status") if entry else None,
                    "candidates": [e["id"] for e in entries
                                   if e.get("status") not in DEAD],
                    "entry": entry, "closed_match": None, "hits": hits}
    return {"anaphoric": True, "binding": None, "via": None,
            "binding_status": None,
            "candidates": [e["id"] for e in entries
                           if e.get("status") not in DEAD],
            "entry": None, "closed_match": None, "hits": hits}


def render_resolution_block(message: str, root: Path) -> str:
    """The machine block appended to the scope message. Every binding is
    explicit and auditable; ambiguity asks instead of assuming."""
    r = resolve_reference(message, root)
    if not r["anaphoric"]:
        return ""
    lines: list[str] = ["[REFERENCE RESOLUTION — how this instruction binds to "
                        "the task ledger]"]
    if r["binding"] and r["entry"]:
        e = r["entry"]
        lines.append(
            f"\"{message.strip()[:80]}\" -> {e['id']} (via {r['via']}) "
            f"— \"{e.get('desc', '')}\" [{e.get('status', 'discussed')}]. "
            "Work on THIS task; the binding is logged and auditable.")
        if e.get("status") in DEAD:
            lines.append(
                f"NOTE: {e['id']} is {e['status']}. If the operator really "
                "means to redo it, confirm before resurrecting — do not "
                "silently reopen closed work.")
    elif r.get("closed_match") and r.get("entry"):
        e = r["entry"]
        lines.append(
            f"\"{message.strip()[:80]}\" most closely matches {e['id']} "
            f"— \"{e.get('desc', '')}\" [{e.get('status', 'dead')}], which is "
            "CLOSED. Confirm with the operator before redoing closed work; "
            "otherwise ask which open task (if any) they mean.")
    else:
        cands = ", ".join(r["candidates"][:6]) or "none"
        lines.append(
            "This message uses a pronoun/short reference with NO unambiguous "
            f"ledger match. Live candidates: {cands}. Ask the operator which "
            "task they mean — do NOT pick silently.")
    return "\n".join(lines)

"""habits.py — the personal pblock learns its person's habits (variants/personal.md).

What it learns from: ONLY the person's own words and their own choices — never
tool output, web pages or files, so a page that says "the user prefers X" can't
plant a habit. The host passes only text the person typed.

When a habit is trusted:
  - a direct word ("always", "never", "I want", "don't", "from now on") —
    trusted at once, it is their own word
  - a correction ("no — …", "why did you …") — the host words it as a habit
    (one short model call, `phrase`); a candidate until another task shows it
    again
  - the same choice on a card, three times in different tasks — a habit
  - praise ("good", "perfect") right after — a small plus for the habits that
    were in that task's briefing
  - silence — nothing

Where a habit applies starts narrow (the kind of task it came from: it is
briefed only when a new task is similar) and widens when another task proves
it (then it is briefed everywhere). The newer direct word wins: an older habit
on the same thing is superseded — crossed out and linked, never deleted.
Habits shape HOW the agent works, never what is true. Never raises.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

PERSONAL = "personal"
CHOICE_HABIT_AT = 3            # The same choice in this many different tasks
SAME_THING = 0.60              # Habits this similar are about the same thing (measured:
                               # a change of mind 0.63-0.92, unrelated habits ~0.23)
NARROW_MATCH = 0.45            # A narrow habit is briefed when the task is this similar
_DIRECT = re.compile(
    r"\b(always|never|from now on|every time|don'?t ever|do not ever|i (?:always )?want|"
    r"i prefer|i'd prefer|i like it when|make sure (?:you|to)|stop (?:doing|asking|telling)|"
    r"don'?t (?:ever )?(?:tell|ask|use|add|make|do|give|show|run|write)|"
    r"please (?:always|never|don'?t))\b", re.I)
_CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|wrong\b|not (?:that|like that|this)\b|that'?s not\b|why (?:did|do|are|is) "
    r"you\b|you (?:didn'?t|did not|just|forgot|missed)\b|i (?:said|told you|asked)\b|"
    r"stop\b|don'?t\b|instead\b)", re.I)
_PRAISE = re.compile(r"^\s*(good|great|perfect|nice|excellent|well done|exactly|that'?s "
                     r"(?:it|right|good|great)|love it|thanks?|thank you)\b", re.I)


def _ref(task: str) -> str:
    key = re.sub(r"\s+", " ", (task or "").strip().lower())[:400]
    return "task-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
    return [p.strip() for p in parts if 8 <= len(p.strip()) <= 300]


def kind_of(text: str) -> str:
    """'direct', 'correction', 'praise' or '' for one message of the person."""
    t = str(text or "")
    if any(_DIRECT.search(s) for s in _sentences(t)):
        return "direct"
    if _CORRECTION.search(t):
        return "correction"
    if _PRAISE.search(t) and len(t) < 80:
        return "praise"
    return ""


def _store(root):
    from .store import PersonaStore
    return PersonaStore(PERSONAL, str(root))


def _embed(root, text: str):
    try:
        from .ground import _orchestrator
        return _orchestrator(str(root)).embedder.embed(text)
    except Exception:                                # noqa: BLE001
        return None


def _vec(e):
    import numpy as np
    v = np.asarray(e, dtype=float).ravel()
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


def _habits(store, statuses=("active", "candidate")) -> list:
    return [c for c in store.cases if c.get("kind") == "habit" and c.get("status") in statuses]


def _same_thing(store, emb, exclude: str = "") -> list:
    """Active habits about the same thing, most similar first."""
    if emb is None:
        return []
    q = _vec(emb)
    out = []
    for c in _habits(store, ("active",)):
        if c["id"] == exclude or not c.get("embedding"):
            continue
        sim = float(q @ _vec(c["embedding"]))
        if sim >= SAME_THING:
            out.append((sim, c))
    return [c for _s, c in sorted(out, key=lambda x: -x[0])]


def _write(root, habit: str, quote: str, task: str, trusted: bool, how: str) -> dict | None:
    """Store one habit; returns {'id', 'habit', 'new', 'trusted'} or None."""
    habit = re.sub(r"\s+", " ", str(habit or "")).strip().rstrip(".")
    if len(habit) < 8:
        return None
    store = _store(root)
    situation = (task or "")[:300]
    emb = _embed(root, habit)
    before = {c["id"] for c in store.cases}
    case = store.propose_case(situation, habit, f"habit ({how})", origin="lived",
                              embedding=emb, origin_ref=_ref(task),
                              task_summary=(task or "")[:120])
    new = case["id"] not in before
    case["kind"] = "habit"
    case["quote"] = str(quote or "")[:300]
    case["source"] = "the person's own words" if how != "choice" else "the person's own choices"
    if new:
        case.setdefault("scope", "narrow")
    else:
        case["scope"] = "wide"                # Shown again in another task: it applies widely
    if trusted:
        case["status"] = "active"
        case["confirmed"] = True
        case["confidence"] = max(float(case.get("confidence") or 0), 0.9)
        # the newer word wins: an older habit about the same thing is crossed out
        # (said in the "noted" line; undo brings it back)
        replaced = []
        for old in _same_thing(store, emb, exclude=case["id"]):
            if old["framing"].strip().lower() != habit.lower():
                store.supersede(old["id"], case["id"])
                replaced.append(old["framing"])
    store.save()
    return {"id": case["id"], "habit": habit, "new": new,
            "trusted": case.get("status") == "active",
            "replaced": replaced if trusted else []}


def learn_message(root, text: str, prev_answer: str, task: str, phrase=None) -> list[dict]:
    """One message the person typed (and the agent's answer before it).
    `phrase(prev_answer, text)` -> one-sentence habit or "" — the host's one
    short model call, used only for corrections."""
    try:
        k = kind_of(text)
        out = []
        if k == "direct":
            for s in _sentences(text):
                if _DIRECT.search(s):
                    r = _write(root, s, s, task, trusted=True, how="said directly")
                    if r:
                        out.append(r)
        elif k == "correction" and phrase is not None:
            habit = str(phrase(prev_answer, text) or "").strip()
            if habit and habit.upper() != "NONE":
                r = _write(root, habit, text, task, trusted=False, how="a correction")
                if r:
                    out.append(r)
        return out
    except Exception:                                # noqa: BLE001
        return []


def praise(root, chat: str) -> int:
    """A small plus for the habits that were in this chat's last briefing."""
    try:
        ids = set(_LAST.get(chat) or [])
        if not ids:
            return 0
        store = _store(root)
        n = 0
        for c in store.cases:
            if c["id"] in ids:
                c["confidence"] = min(1.0, float(c.get("confidence") or 0.3) + 0.05)
                n += 1
        if n:
            store.save()
        return n
    except Exception:                                # noqa: BLE001
        return 0


def record_choice(root, question: str, choice: str, task: str) -> dict | None:
    """The person's answer on a card. The same answer to the same kind of
    question in CHOICE_HABIT_AT different tasks becomes a habit."""
    try:
        f = Path(root) / "personal_choices.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        key = re.sub(r"\s+", " ", str(question or "").strip().lower())[:160]
        refs = data.setdefault(key, {}).setdefault(str(choice), [])
        ref = _ref(task)
        if ref not in refs:
            refs.append(ref)
        f.write_text(json.dumps(data, indent=1), encoding="utf-8")
        if len(refs) == CHOICE_HABIT_AT:
            return _write(root, f'answers "{choice}" when asked: {question}',
                          f"{choice} ({len(refs)} times)", task, trusted=True, how="choice")
        return None
    except Exception:                                # noqa: BLE001
        return None


def forget(root, case_id: str) -> bool:
    """Undo: the habit is retired (kept as history, never briefed)."""
    try:
        store = _store(root)
        for c in store.cases:
            if c["id"] == case_id and c.get("kind") == "habit":
                c["status"] = "retired"
                c["valid_to"] = time.time()
                # what it had replaced comes back
                for old in store.cases:
                    if old.get("superseded_by") == case_id and old.get("status") == "superseded":
                        old["status"] = "active"
                        old["valid_to"] = None
                        old["superseded_by"] = None
                store.save()
                return True
        return False
    except Exception:                                # noqa: BLE001
        return False


_LAST: dict = {}               # Chat -> the habit ids in its last briefing


def brief(root, task: str, chat: str = "", limit: int = 8) -> str:
    """The habits that fit this task: every trusted wide habit, and the
    narrow ones from similar tasks. "" when none."""
    try:
        store = _store(root)
        active = _habits(store, ("active",))
        if not active:
            return ""
        wide = [c for c in active if c.get("scope") == "wide"]
        narrow = [c for c in active if c.get("scope") != "wide"]
        picked = sorted(wide, key=lambda c: -float(c.get("confidence") or 0))[:limit]
        e = _embed(root, task) if narrow else None
        if e is not None:
            q = _vec(e)
            scored = []
            for c in narrow:
                se = _embed(root, c.get("situation") or c.get("framing") or "")
                if se is None:
                    continue
                sim = float(q @ _vec(se))
                if sim >= NARROW_MATCH:
                    scored.append((sim, c))
            picked += [c for _s, c in sorted(scored, key=lambda x: -x[0])][:max(0, limit - len(picked))]
        if not picked:
            return ""
        _LAST[chat] = [c["id"] for c in picked]
        lines = [f"- {c['framing']}" for c in picked]
        return ("### HOW THIS USER LIKES TO WORK (their own words and choices — how to "
                "work with them, never what is true; tests, sources and facts come first)\n"
                + "\n".join(lines))
    except Exception:                                # noqa: BLE001
        return ""

"""skill_pick.py — the right skills for the work, picked with the person.

The problem: over months people collect many skills. Listing them all to the
model on every turn costs tokens and doesn't help.

  - a small task gets no skills (nothing listed, nothing spent);
  - when the work is big or semi-big (testing a system, building an app,
    a plan of several steps), Aura matches the TASK STATE — the soul of the
    chat: the goal in the person's words, their latest reply, the open
    calls) and shows the best 2–3 with their descriptions and how well they
    worked before; the person picks (or none). Only the picked ones reach
    the model, in full, once;
  - at most 3 offers in one chat;
  - once per big task, the person is asked whether to search for a skill
    that would suit the work better (the public skill sources — added as
    untrusted, to review before use; nothing downloads without a yes);
  - nobody to ask (scheduled / piped runs): the top match is used when the
    work is big, and that is said;
  - how each skill did is kept (the runs it was picked for, and how many
    ended well) and shown when it is offered.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

MAX_OFFERS = 3                 # Offers in one chat
SHOW = 3                       # Skills shown in one offer
BIG_TODOS = 3                  # Open to-dos that make the work big
BIG_WORDS = 25                 # A request this long is big work by itself
BODY_MAX = 8000                # One skill's text sent to the model
PICKS = ".skill_picks.json"    # Per chat: offers, picked, declined, searched
OUTCOMES = ".outcomes.json"    # Per skill: runs picked for, runs that ended well
_COMMON = {"the", "and", "for", "with", "from", "that", "this", "into", "what", "when", "how",
           "you", "your", "its", "are", "can", "all", "one", "out", "before", "after", "about",
           "please", "make", "use", "using", "get", "new", "add", "file", "files", "project",
           "code", "work", "some", "any", "not", "but", "does", "has", "have", "need", "want",
           "then", "also", "them", "they", "will", "should", "would", "could", "just", "like"}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower())
            if w not in _COMMON]


# ---------------------------------------------------------- how big ---
def is_big(goal: str, todos: list[dict], multi: bool = False) -> bool:
    """Big or semi-big: a plan of several open steps, a task Aura already
    plans as many steps, or a long request."""
    open_ = [t for t in todos or [] if t.get("status") not in ("done", "archived")]
    return len(open_) >= BIG_TODOS or multi or len(_words(goal)) >= BIG_WORDS


def state_text(goal: str, reply: str, todos: list[dict]) -> str:
    """The soul of the chat, as words to match on."""
    items = [str(t.get("text") or t.get("title") or "") for t in todos or []
             if t.get("status") not in ("done", "archived")]
    return " ".join([goal or "", reply or "", *items])


# ---------------------------------------------------------- matching ---
def rank(text: str, skills: dict[str, dict], skip: set[str] = frozenset(),
         limit: int = SHOW) -> list[tuple[str, float]]:
    """Two different words in common at least, so 'the' never picks a skill."""
    docs = {n: _words(n.replace("-", " ") + " " + s.get("description", ""))
            for n, s in skills.items() if n not in skip}
    if not docs:
        return []
    q = set(_words(text))
    n = len(docs)
    avg = sum(len(d) for d in docs.values()) / n or 1
    df = {w: sum(1 for d in docs.values() if w in d) for w in q}
    out = []
    for name, d in docs.items():
        hits = q & set(d)
        if len(hits) < 2:
            continue
        score = 0.0
        for w in hits:
            tf = d.count(w)
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        out.append((name, round(score, 3)))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:limit]


def look_alike(skills: dict[str, dict], name: str, description: str) -> str | None:
    """A skill of the person's that says nearly the same — asked about when
    a new one is saved (stop the pile-up before it starts)."""
    mine = set(_words(name.replace("-", " ") + " " + description))
    for other, s in skills.items():
        if other == name:
            continue
        theirs = set(_words(other.replace("-", " ") + " " + s.get("description", "")))
        both = mine & theirs
        if len(both) >= 3 and len(both) / max(1, min(len(mine), len(theirs))) >= 0.7:
            return other
    return None


# ------------------------------------------------------------ records ---
def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def chat_state(eng: Path, chat: str) -> dict:
    return _read(Path(eng) / PICKS).get(chat) or {"offers": 0, "picked": [], "declined": [],
                                                  "searched": []}


def save_chat_state(eng: Path, chat: str, st: dict) -> None:
    p = Path(eng) / PICKS
    allc = _read(p)
    allc[chat] = st
    _write(p, allc)


def _outcomes_path() -> Path:
    from .skills import _global_home
    return _global_home(None) / OUTCOMES


def outcomes() -> dict:
    return _read(_outcomes_path())


def record(names: list[str], ok: bool) -> None:
    """A run that used these skills ended (ok = no error, checks passed)."""
    if not names:
        return
    p = _outcomes_path()
    d = _read(p)
    for n in names:
        r = d.setdefault(n, {"runs": 0, "ok": 0})
        r["runs"] += 1
        r["ok"] += 1 if ok else 0
    _write(p, d)


def label(name: str, s: dict, out: dict) -> str:
    r = out.get(name) or {}
    how = f" (worked {r['ok']}/{r['runs']} times)" if r.get("runs") else ""
    return f"{name} — {s.get('description', '')[:110]}{how}"


def body_block(chosen: list[str], skills: dict[str, dict], by: str) -> str:
    parts = [f"[SKILLS FOR THIS WORK — {by}. Follow them where they fit.]"]
    for n in chosen:
        parts.append(f"--- skill {n} ---\n{str(skills[n].get('body', ''))[:BODY_MAX]}")
    return "\n".join(parts)


# --------------------------------------------------------------- offer ---
def offer(eng: Path, chat: str, text: str, skills: dict[str, dict], ask,
          tell) -> list[str]:
    """One offer for big work. ask(question, options, multi=...) -> answer |
    None, or None when nobody can answer. Returns the skills picked."""
    st = chat_state(eng, chat)
    if st["offers"] >= MAX_OFFERS:
        return []
    cands = rank(text, skills, skip=set(st["picked"]) | set(st["declined"]))
    if not cands:
        return []
    st["offers"] += 1
    out = outcomes()
    if ask is None:
        top = cands[0][0]
        st["picked"].append(top)
        save_chat_state(eng, chat, st)
        tell(f"using the skill '{top}' for this work (the best match; nobody to ask)")
        return [top]
    labels = [label(n, skills[n], out) for n, _ in cands]
    try:
        ans = ask("Skills that fit this work — use any?", labels + ["none of these"],
                  multi=True)
    except TypeError:
        ans = ask("Skills that fit this work — use any?", labels + ["none of these"])
    picked_labels = ans if isinstance(ans, (list, tuple)) else ([ans] if ans else [])
    picked = [cands[labels.index(a)][0] for a in picked_labels if a in labels]
    st["picked"] += picked
    st["declined"] += [n for n, _ in cands if n not in picked]
    save_chat_state(eng, chat, st)
    return picked


def offer_search(eng: Path, chat: str, task_key: str, text: str, ask, tell,
                 registry: Path, fetch=None) -> str | None:
    """Once per big task: search the public skill sources for a better
    fit — only on a yes; what is added stays untrusted until reviewed."""
    st = chat_state(eng, chat)
    if ask is None or task_key in st["searched"]:
        return None
    st["searched"].append(task_key)
    save_chat_state(eng, chat, st)
    a = ask("Want me to search the public skill sources for a skill that suits this "
            "work better? (I only look; nothing is added without your yes)",
            ["yes, search", "no"])
    if not (a and str(a).startswith("yes")):
        return None
    from . import skill_hub as H
    words = " ".join(sorted(set(_words(text)), key=lambda w: -len(w))[:6])
    try:
        hits = [h for h in H.search(words, fetch=fetch, limit=5) if "name" in h]
    except Exception as e:                           # noqa: BLE001
        tell(f"couldn't search the skill sources: {e}")
        return None
    if not hits:
        tell("no public skill matched this work")
        return None
    labels = [f"{h['repo']}/{h['folder']} — {h['description'][:90]} [licence: {h['license']}]"
              for h in hits]
    a = ask("Found these — add one? (added as untrusted: review it, then "
            "`aura skills adopt <name>` to use it)", labels + ["none"])
    if a not in labels:
        return None
    h = hits[labels.index(a)]
    r = H.add(h["repo"], h["folder"], registry, fetch=fetch)
    if not r.get("ok"):
        tell(f"not added: {r.get('error')}")
        return None
    tell(f"added '{h['name']}' as untrusted — review it, then `aura skills adopt "
         f"{h['name']}` to use it")
    return h["name"]

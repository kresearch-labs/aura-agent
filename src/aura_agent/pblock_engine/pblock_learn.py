"""learn.py — the pblocks learn from the work, as it happens.

What becomes a lesson (zero extra model calls), read from the conversation's
own structure — each tool result and the model's next words — never from raw
transcript lines:
  - fixed: a tool failed, a later call of the same tool worked: what the model
    did differently (outcome: the next step worked)
  - not fixed, at the end of a task: what failed and the model's reading of
    why — a warning for next time (outcome: failed). A failure is worth more
    than a success.
  - never: the host's own refusals (set_host_faults) and step noise
    (quality.is_fragment)

Each lesson goes to the pblock whose territory it belongs to, and to the
personal pblock. Two-stage law: a lesson is a candidate until a DIFFERENT
task proves it again (the same lesson twice in one task is a copy and is not
stored); verdict-linked failures supersede. Learning never raises.

Env: AURA_PBLEARN=0 disables. AURA_PBSTORES overrides the stores root (else
PBLOCKS_STORES, else ./pblock-stores). PERSONA_HOST_FAULTS: the host's own
refusal patterns, separated by "||".
"""
from __future__ import annotations
from ._log import dbg as print  # Quiet unless AURA_PBLEARN_DEBUG
from .quality import is_fragment as _is_fragment


def _stores_default() -> str:
    from .ground import _aura_stores
    return _aura_stores()


import json
import os
import re
import sys as _sys
from pathlib import Path

_STORES_DEFAULT = ""  # Set at call time from the host or the environment
_ENGINE_DEFAULT = ""  # In-body organ; host layout via AURA_ENGINE_PATH only
_ERR_MARKERS = ("Error", "Fatal", "Traceback", "OM", "assert")
_OK_MARKERS = ("ok\": true", "EXIT=0", "PASS", "fixed", "solved", "FIX",
               "resolved", "verdict: PASS")


def _cached_orch(stores_root):
    """."""
    from .ground import _orchestrator
    return _orchestrator(str(stores_root))


def _summary(text: str, limit: int = 140) -> str:
    """One readable line from a tool result: the first line that says what
    went wrong, without line tags, JSON quoting, or paths noise."""
    import re as _re
    s = str(text or "")
    s = _re.sub(r"\b\d+:[0-9a-f]{2}\|", " ", s)          # Line-tagged dumps
    s = s.replace('\\"', '"').replace('{"status": "error", "error": "', "")
    for ln in s.splitlines():
        ln = ln.strip().strip('{}"')
        if any(k in ln for k in ("Error", "error", "failed", "not found",
                                  "No such", "denied", "Traceback", "refused")):
            return _re.sub(r"\s+", " ", ln)[:limit]
    return _re.sub(r"\s+", " ", s.strip())[:limit]


def _first_sentence(text: str, limit: int = 170) -> str:
    import re as _re
    s = _re.sub(r"\s+", " ", str(text or "")).strip()
    m = _re.search(r"(.{30,}?[.!?])(\s|$)", s)
    return (m.group(1) if m else s)[:limit]


def _panel_seats(orch, known, task: str, top: int = 2) -> list[str]:
    """."""
    try:
        import numpy as _np
        for k in known:
            if k not in orch.personas:
                orch.add_persona(k)
        e = _np.asarray(orch.embedder.embed(task or ""), dtype=float).ravel()
        scored = []
        for k in known:
            m = orch.personas[k].territory_mean()
            if m is None:
                continue
            m = _np.asarray(m, dtype=float).ravel()
            scored.append((float(e @ m), k))
        return [k for _, k in sorted(scored, reverse=True)[:top]]
    except Exception:
        return []


# -------------------------------------------------- what counts as work --
# A host's own refusals (its sandbox, its permission prompts, its loop guard,
# its plan mode) are faults of the host, not of the work: they are never
# learned. The host names them: set_host_faults([...]) or PERSONA_HOST_FAULTS
# (patterns separated by "||").
_HOST_FAULTS: list = []


def set_host_faults(patterns) -> None:
    """The host's own refusal messages, as regular expressions."""
    global _HOST_FAULTS
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            continue
    _HOST_FAULTS = out


def _host_fault(text: str) -> bool:
    env = [p for p in os.environ.get("PERSONA_HOST_FAULTS", "").split("||") if p.strip()]
    pats = list(_HOST_FAULTS)
    for p in env:
        try:
            pats.append(re.compile(p, re.I))
        except re.error:
            continue
    t = str(text or "")
    return any(p.search(t) for p in pats)


def _task_ref(task: str) -> str:
    """One id per task: a lesson seen again in the SAME task is a copy; only
    another task can prove it."""
    import hashlib
    key = re.sub(r"\s+", " ", (task or "").strip().lower())[:400]
    return "task-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _is_error(content: str) -> bool:
    c = str(content or "")
    if re.search(r'"ok":\s*false|"status":\s*"(error|refused|blocked)"', c):
        return True
    m = re.search(r'"exit_code":\s*(-?\d+)', c)
    if m and m.group(1) != "0":
        return True
    hits = [mk for mk in _ERR_MARKERS if mk != "OM" and mk in c]
    return bool(hits) or bool(re.search(r"\bOM\b", c))


def _tool_name(m: dict, msgs: list) -> str:
    name = m.get("name")
    if name:
        return str(name)[:40]
    cid = m.get("tool_call_id")
    for a in msgs:
        for tc in (a.get("tool_calls") or []) if isinstance(a, dict) else []:
            if tc.get("id") == cid:
                return str((tc.get("function") or {}).get("name") or "tool")[:40]
    return "tool"


def _error_line(content: str) -> str:
    """The line that says what went wrong: from a JSON tool result its
    stderr / error (nested results too), the last meaningful line."""
    text = str(content or "")
    for _ in range(3):
        try:
            d = json.loads(text)
        except (ValueError, TypeError):
            break
        if not isinstance(d, dict):
            break
        for key in ("stderr", "error", "stdout"):
            v = d.get(key)
            if isinstance(v, str) and v.strip() and (key != "stdout" or not d.get("stderr")):
                lines = [ln.strip() for ln in v.strip().splitlines() if ln.strip()]
                hit = [ln for ln in lines if re.search(
                    r"error|exception|failed|denied|not found|no such|refused", ln, re.I)]
                return ((hit or lines)[-1])[:160]
        inner = d.get("result")
        if not isinstance(inner, str):
            break
        text = inner
    return _summary(content)


def _events(msgs: list) -> list:
    """The work as it happened: each tool result (ok / error, host faults
    left out) with the model's own next words — its reading of what came
    back. Raw transcripts are never mined line by line."""
    out = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        content = str(m.get("content") or "")
        if _host_fault(content):
            continue
        reading = ""
        for nxt in msgs[i + 1:]:
            if not isinstance(nxt, dict):
                continue
            if nxt.get("role") == "assistant":
                reading = _first_sentence(nxt.get("content") or "")
                break
            if nxt.get("role") == "user":
                break
        if reading and (_is_fragment(reading) or _host_fault(reading)):
            reading = ""
        out.append({"i": i, "tool": _tool_name(m, msgs), "error": _is_error(content),
                    "summary": _error_line(content), "reading": reading})
    return out


def _lessons(events: list, final: bool) -> list:
    """(framing, outcome) pairs.

    Fixed: a tool failed and a later call of the same tool worked — what the
    model did differently is the lesson (outcome ok: the next step worked).
    Not fixed (only at the end of the task): what failed and the model's own
    reading of why — a warning for the next time (outcome failed). A failure
    is worth more than a success: it keeps the next run from the same
    mistake."""
    lessons = []
    for k, e in enumerate(events):
        if not e["error"]:
            continue
        fix = next((x for x in events[k + 1:] if x["tool"] == e["tool"] and not x["error"]), None)
        if fix is not None:
            how = e.get("reading") or fix.get("reading")    # Its words right after the error
            if how:
                lessons.append((f"after {e['tool']} failed ({e['summary']}), what worked "
                                f"next: {how}", "outcome: ok (the next step worked)"))
        elif final:
            why = e.get("reading")
            lessons.append((f"{e['tool']} failed ({e['summary']}) and it was not solved in "
                            "this task" + (f"; the reading at the time: {why}" if why else ""),
                            "outcome: failed (not solved in this task)"))
    seen, out = set(), []
    for fr, oc in lessons:
        key = fr.strip().lower()
        if key in seen or _is_fragment(fr):
            continue
        seen.add(key)
        out.append((fr, oc))
    return out[:6]


def _brief_seats(msgs: list, known: list) -> list:
    """The pblocks named in this task's briefings."""
    text = " ".join(str(m.get("content", "")) for m in msgs
                    if isinstance(m, dict) and m.get("role") == "system"
                    and ("### GROUND FROM" in str(m.get("content", ""))
                         or "PANEL GROUND" in str(m.get("content", "")))).upper()
    return [k for k in known if k.upper() in text]


def _owner(orch, seats: list, text: str) -> list:
    """Each lesson goes to the pblock whose territory it belongs to (the
    closest one among the panel), and to the personal pblock, which learns
    from every task."""
    panel = [x.strip() for x in os.environ.get("AURA_PBSEATS", "").split(",") if x.strip()]
    pool = [s for s in seats if s != "personal" and (not panel or s in panel)]
    best = pool[:1]
    if len(pool) > 1:
        try:
            emb = orch.embedder.embed(text)
            scored = []
            for s in pool:
                if s not in orch.personas:
                    orch.add_persona(s)
                hits = orch.personas[s].store.retrieve(emb, k=3, min_similarity=0.0)
                scored.append((max((sim for _c, sim in hits), default=0.0), s))
            best = [max(scored)[1]] if scored else pool[:1]
        except Exception:
            best = pool[:1]
    out = list(best)
    if (not panel or "personal" in panel) and "personal" not in out:
        out.append("personal")
    return out


def _write(orch, stores_root, seats_all: list, lessons: list, task: str, ref: str) -> int:
    n = 0
    for framing, outcome in lessons:
        for seat in _owner(orch, seats_all, (task or "") + " " + framing):
            try:
                if not (Path(stores_root) / f"{seat}_store.jsonl").exists() and seat != "personal":
                    continue
                if seat not in orch.personas:
                    orch.add_persona(seat)
                store = orch.personas[seat].store
                try:
                    emb = orch.embedder.embed((task or "")[:300] + " " + framing)
                except Exception:
                    emb = None
                store.propose_case((task or "")[:900], framing[:400], outcome,
                                   origin="lived", embedding=emb, origin_ref=ref,
                                   task_summary=(task or "")[:120])
                store.save()
                n += 1
            except Exception as _se:
                print(f"[pblock_learn] seat {seat} failed: {_se}", flush=True)
    return n


def _gate(stores_root) -> bool:
    if (os.environ.get("AURA_PBLEARN", "1") == "0"
            or os.environ.get("AURA_PBLOCKS", "1") == "0"):
        return False
    return Path(stores_root).exists()


def learn_from_session(session, work_dir, rec, task: str) -> int:
    """At the end of a task: what was fixed, and what failed and was never
    fixed, become lessons — candidates until another task proves them.
    Each goes to the pblock it belongs to (and the personal one). Never
    raises."""
    try:
        stores_root = Path(os.environ.get("AURA_PBSTORES") or _stores_default())
        if not _gate(stores_root):
            return 0
        orch = _cached_orch(stores_root)
        known = [p.stem.replace("_store", "") for p in stores_root.glob("*_store.jsonl")]
        msgs = [m for m in (getattr(session, "messages", []) or []) if isinstance(m, dict)]
        lessons = _lessons(_events(msgs), final=True)
        if not lessons:
            return 0
        seats = _brief_seats(msgs, known) or _panel_seats(orch, known, task, top=3)
        n = _write(orch, stores_root, seats + ["personal"], lessons, task, _task_ref(task))
        try:
            from .selftrain import maybe_retrain
            for seat in set(seats):
                try:
                    _r = maybe_retrain(str(stores_root), seat)
                    if _r.get("retrained"):
                        print(f"[pblock_learn] retrained {seat}: {_r.get('reason', '')}", flush=True)
                except Exception:
                    continue
        except Exception:
            pass
        return n
    except Exception as _e:
        print(f"[pblock_learn] skipped: {_e}", flush=True)
        return 0


def learn_step_pairs(session, work_dir: str, step: int, task: str) -> int:
    """During the task: a failure followed by a working call of the same tool
    is a lesson the moment it happens (the model's own words for what it did
    differently). Never raises."""
    try:
        stores_root = Path(os.environ.get("AURA_PBSTORES") or _stores_default())
        if not _gate(stores_root):
            return 0
        msgs = [m for m in (getattr(session, "messages", []) or []) if isinstance(m, dict)]
        lessons = _lessons(_events(msgs), final=False)
        if not lessons:
            return 0
        orch = _cached_orch(stores_root)
        known = [p.stem.replace("_store", "") for p in stores_root.glob("*_store.jsonl")]
        seats = _brief_seats(msgs, known) or _panel_seats(orch, known, task, top=3)
        return _write(orch, stores_root, seats + ["personal"], lessons, task, _task_ref(task))
    except Exception as _e:
        print("[learn_step_pairs] EXC:", type(_e).__name__, str(_e)[:150], flush=True)
        return 0

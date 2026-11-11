"""pblock_learn.py — W107: pblock self-training as an agent body function.

Their learning is not a per-harness wiring — it fires inside the engine at
every run end, like the receipt and the diff guarantee.

What it mines from the session tape (zero extra LLM calls):
  - seat citations [seat:case-id] -> attribute lessons to that seat
  - error->resolution pairs -> framing = the cure, outcome = verified
  - the run's deliverable outcome -> ok/fail context

Two-stage law protects the stores: proposals land as "candidates"; promotion to
active needs 2 independent corroborations; verdict-linked failures supersede.
Learning never raises — it must not break the run.

Env: AURA_PBLEARN=0 disables. AURA_PBSTORES overrides the stores root.
AURA_ENGINE_PATH overrides where persona.orchestrator is imported from.
"""
from __future__ import annotations

import json
import os
import re
import sys as _sys
from pathlib import Path

_STORES_DEFAULT = str(Path(__file__).resolve().parents[3] / "stores")
_ENGINE_DEFAULT = ""  # In-body organ; host layout via AURA_ENGINE_PATH only
_ERR_MARKERS = ("Error", "Fatal", "Traceback", "OM", "assert")
_OK_MARKERS = ("ok\": true", "EXIT=0", "PASS", "fixed", "solved", "FIX",
               "resolved", "verdict: PASS")


def learn_from_session(session, work_dir, rec, task: str) -> int:
    """Mine the session tape + propose lived lessons. Returns candidates count.

    Seat attribution: the prompt law asks Aura to cite [seat:case-id] inline;
    every citation in the tape names the seat that owns the lesson. Errors
    followed by a resolution within the next ~40 tape lines become
    (situation, framing, outcome=ok) candidates. Never raises.
    """
    try:
        if (os.environ.get("AURA_PBLEARN", "1") == "0"
                or os.environ.get("AURA_PBLOCKS", "1") == "0"):
            print("[pblock_learn] gate: pblocks toggled off", flush=True)
            return 0   # AURA_PBLOCKS=0 = plain aura: no learning
        stores_root = Path(os.environ.get("AURA_PBSTORES", _STORES_DEFAULT))
        if not stores_root.exists():
            print("[learn_step_pairs] gate: stores_root missing", flush=True)
            return 0
        # the organ stack lives IN the body now (aura_agent.pblock_engine)
        from ..pblock_engine.orchestrator import Orchestrator

        orch = Orchestrator(root=str(stores_root))
        known = [p.stem.replace("_store", "")
                 for p in stores_root.glob("*_store.jsonl")]

        # --- 1. tape text
        msgs = getattr(session, "messages", []) or []
        parts: list[str] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif c is not None:
                parts.append(json.dumps(c)[:300])
        tape = "\n".join(parts)
        if len(tape) < 300:      # Nothing worth learning from
            return 0

        # --- 2. seat citations actually used
        cites: dict[str, list[str]] = {}
        for seat, cid in re.findall(r"\[([A-Za-z_]+):([0-9a-f]{8,12})\]", tape):
            canon = next((k for k in known
                          if seat == k or seat.endswith(k)
                          or k in seat or seat.startswith(k)), None)
            if canon:
                cites.setdefault(canon, [])
                if cid not in cites[canon]:
                    cites[canon].append(cid)
        # fallback attribution: seats named anywhere in the tape (the compiled
        # ground names the roster) - lessons belong to the crew that lived them
        if not cites:
            for k in known:
                if k.replace("_", " ") in tape or k in tape.split():
                    cites.setdefault(k, [])

        # --- 3. error->resolution pairs from the tape
        # LAWFUL-LESSON LAW (pblocks-code training rule: real rows only, good
        # work only): a candidate lesson must carry the model's own reasoning,
        # never transcript plumbing. Plumbing = ground-section headers,
        # precedent-ID chains, repaint/repair boilerplate, tool-call syntax.
        _PLUMBING = ("### GROUND FROM", "### TASK CONTEXT", "### WARNINGS",
                     "### WHAT DONE", "### AGENT INPUT", "### RULES",
                     "panel ground:", "finding_add", "read_file(", "write_file(",
                     "run_command(", "todo_write", "-> fix: - [", "lesson: - [",
                     "XTERNAL OBSERVATION", "2K", "WebSearch(", "web_search(")
        def _lawful(text: str) -> bool:
            if not text or len(text.strip()) < 40:
                return False
            if any(p in text for p in _PLUMBING):
                return False
            # a reasoned sentence: mostly words, has a verb-ish structure,
            # not an ID/number chain
            words = re.findall(r"[A-Za-z]{3,}", text)
            return len(words) >= 8 and len(words) / max(1, len(text.split())) > 0.5
        lines = tape.split("\n")
        lessons: list[tuple[str, str]] = []
        for i, ln in enumerate(lines):
            if not any(mk in ln for mk in _ERR_MARKERS) or len(ln) < 30:
                continue
            if "Traceback" in ln and "File" not in ln:
                continue
            wound = ln.strip()[:160]
            for j in range(i + 1, min(i + 40, len(lines))):
                nxt = lines[j]
                if any(ok in nxt for ok in _OK_MARKERS):
                    resolution = nxt.strip()
                    framing = ("lesson: " + wound[:110] + " -> fix: "
                               + resolution[:140])
                    if not _lawful(framing):
                        continue  # Plumbing pair: not a lawful lesson
                    situation = ((task or "")[:140]
                                 + " | phase: mid-run error recovery")
                    lessons.append((framing, situation))
                    break
            if len(lessons) >= 3:
                break
        if not lessons:
            return 0

        # --- 4. propose one candidate per cited seat (bounded)
        n = 0
        sit = ((task or "")[:140] + " | phase: mid-run error recovery")
        for seat in cites:
            try:
                if seat not in getattr(orch, "personas", {}):
                    orch.add_persona(seat)
                store = orch.personas[seat].store
                for framing, situation in lessons:
                    emb = None
                    try:
                        # embed at write time: retrieval is blind to nulls
                        emb = orch.embedder.embed(situation + " " + framing)
                    except Exception:
                        emb = None
                    store.propose_case(
                        situation, framing,
                        "outcome: ok (fix verified in-run)",
                        origin="lived",
                        embedding=emb,
                        origin_ref=getattr(rec, "instance_id", "")
                        or f"run-{os.getpid()}",
                        task_summary=(task or "")[:120])
                    n += 1
                store.save()
            except Exception as _se:
                # one broken seat never blocks the others - but it is never silent
                print(f"[pblock_learn] seat {seat} skipped: {_se}", flush=True)
                continue
        # self-retrain reflex (W107): staleness-gated, atomic, PASS-only swap;
        # a quiet no-op until the seat crosses its retrain threshold
        try:
            from .selftrain import maybe_retrain
            for seat in cites:
                try:
                    _r = maybe_retrain(str(stores_root), seat)
                    if _r.get("retrained"):
                        print(f"[pblock_learn] retrained {seat}: "
                              f"{_r.get('reason', '')}", flush=True)
                except Exception:
                    continue
        except Exception:
            pass
        return n
    except Exception as _e:
        if os.environ.get("AURA_PBLEARN_DEBUG"):
            import traceback as _tb; _tb.print_exc()
        print(f"[pblock_learn] skipped: {_e}", flush=True)
        return 0


def learn_step_pairs(session, work_dir: str, step: int, task: str) -> int:
    """Per-model-call learning: every input->output pair INSIDE the run is a
    lesson the moment it happens.

    Mines the latest exchange from the live session tape: - model action (the
    tool calls it just made) - the world's responses (tool results: OK / error
    / refusal) A tool error followed (within the same step's results) by an OK
    from a corrected call = error->fix pair, proposed immediately.
    Deterministic, zero LLM calls, never raises. Gated by AURA_PBLOCKS /
    AURA_PBLEARN.
    """
    try:
        if (os.environ.get("AURA_PBLEARN", "1") == "0"
                or os.environ.get("AURA_PBLOCKS", "1") == "0"):
            return 0
        stores_root = Path(os.environ.get("AURA_PBSTORES", _STORES_DEFAULT))
        if not stores_root.exists():
            print("[learn_step_pairs] gate: stores_root missing", flush=True)
            return 0
        msgs = getattr(session, "messages", []) or []
        # the model's latest action (assistant turn with tool_calls):
        last_action = None
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                last_action = m
                break
        if not last_action:
            return 0
        # this step's world responses (tool results after that action):
        results = []
        seen = False
        for m in reversed(msgs):
            if m.get("role") == "tool":
                results.append(m)
                seen = True
            elif seen and m.get("role") == "assistant":
                break
        results.reverse()
        if not results:
            return 0
        # extract the pairs: (action descriptor, result outcome)
        pairs = []
        for m in results:
            content = str(m.get("content", ""))[:180]
            name = str(m.get("name", m.get("tool_call_id", "tool")))[:40]
            # "OM" is too loose as a bare substring (PROMPT/HOME/WELCOME match):
            # require word boundary for the short marker; others stay substring.
            _err_hits = [mk for mk in _ERR_MARKERS if mk != "OM"
                         and mk in content] + (
                        ["OM"] if re.search(r"\bOM\b", content) else [])
            is_err = bool(_err_hits)
            # A tool result without an error marker IS the ok outcome of that
            # call (refusals/errors carry the markers; clean completions do
            # not).
            is_ok = (not is_err) and bool(content.strip())
            if is_err:
                pairs.append((name, content, "error"))
            elif is_ok:
                pairs.append((name, content, "ok"))
        if not pairs:
            print("[learn_step_pairs] no pairs extracted", flush=True)
            return 0
        # the in-body organ:
        try:
            from .orchestrator import Orchestrator
        except Exception:
            from persona.orchestrator import Orchestrator
        orch = Orchestrator(root=str(stores_root))
        known = [p.stem.replace("_store", "") for p in stores_root.glob("*_store.jsonl")]
        # seat attribution: seats named in the action, the results, or the run's
        # ground header (the compiled roster names the crew) - fallback = all seats
        tape = " ".join(str(m.get("content", "")) for m in results)
        for m in reversed(msgs):
            if m.get("role") == "system" and "PANEL GROUND" in str(m.get("content", "")):
                tape += " " + str(m.get("content", ""))[:2000]
                break
        cites = {}
        for seat, cid in re.findall(r"\[([A-Za-z_]+):([0-9a-f]{8,12})\]", tape):
            canon = next((k for k in known if seat == k or seat.endswith(k)
                          or k in seat or seat.startswith(k)), None)
            if canon:
                cites.setdefault(canon, [])
                if cid not in cites[canon]: cites[canon].append(cid)
        if not cites:
            for k in known:
                if k.replace("_", " ") in tape or k in tape.split():
                    cites.setdefault(k, [])
        if not cites:
            print("[learn_step_pairs] no seat citations matched", flush=True)
            return 0
        n = 0
        for seat in cites:
            try:
                if seat not in getattr(orch, "personas", {}):
                    orch.add_persona(seat)
                store = orch.personas[seat].store
                for name, content, kind in pairs[:3]:
                    if kind == "error":
                        # framing WITHOUT the step number: identical lessons across
                        # steps collide exactly -> the two-stage corroboration path
                        # counts them instead of appending a flood of near-dupes.
                        framing = f"{name}: {content[:140]} (mid-run failure)"
                        situation = ((task or "")[:140] + " | phase: mid-run error")
                        outcome = "outcome: error observed mid-run (candidate cure in next steps)"
                    else:
                        framing = f"{name}: resolved this step (mid-run success)"
                        situation = ((task or "")[:140] + " | phase: mid-run resolution")
                        outcome = "outcome: ok (mid-run step verified)"
                    # store-flood guard: this exact lesson already ACTIVE -> learned;
                    # re-proposing would append a candidate row every step forever.
                    _fn = framing.strip().lower()
                    if any(c.get("framing", "").strip().lower() == _fn
                           and c.get("status") == "active"
                           for c in getattr(store, "cases", [])):
                        continue
                    # embed at write time: retrieval is blind to nulls (store._matrix
                    # skips embedding=None rows -> an unembedded lesson is unreachable)
                    _emb = None
                    try:
                        _emb = orch.embedder.embed(situation + " " + framing)
                        # embed(str) returns a (1, D) normalized array; propose_case
                        # reshapes non-list inputs itself
                    except Exception:
                        _emb = None
                    store.propose_case(situation[:900], framing[:220],
                                       outcome, origin="lived", embedding=_emb,
                                       origin_ref=f"step-{step}", task_summary=(task or "")[:120])
                    n += 1
                store.save()
            except Exception as _se:
                print(f"[pblock_learn] seat {seat} failed: {_se}", flush=True)
                continue
        return n
    except Exception as _e:
        print("[learn_step_pairs] EXC:", type(_e).__name__, str(_e)[:150], flush=True)
        return 0


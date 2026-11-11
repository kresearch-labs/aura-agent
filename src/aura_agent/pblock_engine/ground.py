"""ground.py — compile the pblock panel's ground into run context (in-body).

The judgment layer of aura, called by the loop at every run start: panel
review -> compiled ground (cited precedents from the lived stores) injected
as a system message. AURA_PBGROUND=0 strips it (A/B no-panel arm). Never
raises — a pblock hiccup never breaks a run.
"""
from __future__ import annotations
from ._log import dbg as print  # Quiet unless AURA_PBLEARN_DEBUG
from .quality import lesson_text as _lesson_text

import os
import sys as _sys
# self-wiring: this file's own dir (for standalone use, the sibling imports below)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

_CACHE = {"root": None, "orch": None}


def _aura_stores() -> str:
    """The pblocks folder: the host's (Aura: <aura home>/pblocks), else
    PBLOCKS_STORES, else ./pblock-stores (the engine used on its own)."""
    try:
        from ..pblocks import stores_dir
        return str(stores_dir())
    except (ImportError, ValueError):
        return os.environ.get("PBLOCKS_STORES", os.path.join(os.getcwd(), "pblock-stores"))

try:
    from .orchestrator import Orchestrator              # Package use
except ImportError:                                     # Standalone use
    from orchestrator import Orchestrator


def _orchestrator(root: str):
    if _CACHE["orch"] is None or _CACHE["root"] != root:
        _CACHE["orch"] = Orchestrator(root)
        _CACHE["root"] = root
    return _CACHE["orch"]


def compile_run_ground(task: str, root: str, instance_id: str = "") -> str:
    """Panel ground for this task, from the seats' lived stores.

    Store root resolution: AURA_PBSTORES env, else <aura home>/pblocks/stores.
    Returns "" when stores are absent (honest empty, never invented).
    """
    if (os.environ.get("AURA_PBGROUND", "1") == "0"
            or os.environ.get("AURA_PBLOCKS", "1") == "0"):
        return ""   # AURA_PBLOCKS=0 = plain aura: no ground, no pblock machinery
    stores = os.environ.get("AURA_PBSTORES") or _aura_stores()
    if not os.path.isdir(stores):
        return ""
    orch = _orchestrator(stores)   # Orchestrator root == the stores dir
    import sys as _s
    # AURA_PBSEATS: comma-separated seat allow-list (per-test scoping). Empty
    # or unset = every seat with a lived store (the engine's default breadth).
    seats_filter = [x.strip() for x in os.environ.get("AURA_PBSEATS", "").split(",") if x.strip()]
    print("[ground] scanning stores:", stores, "->", [x for x in sorted(os.listdir(stores)) if x.endswith("_store.jsonl")], "seats:", seats_filter or "ALL", file=_s.stderr)
    # register the scoped seats (only these can be summoned by select_panel)
    for f in sorted(os.listdir(stores)):
        if not f.endswith("_store.jsonl"):
            continue
        n = f[:-len("_store.jsonl")]
        if seats_filter and n not in seats_filter:
            continue
        if n not in orch.personas:
            orch.add_persona(n)
    # PANEL PROFILE "simple": arm4's flat compile, verbatim behavior —
    # every seat's top-3 by cosine, one flat line each, single header, no
    # meta-structure (the dose-shape experiment arm: pass-1 arm4 = 25.5/30).
    if os.environ.get("AURA_PB_PANEL_PROFILE", "flash").lower() == "simple":
        try:
            from .orchestrator import _clean_lesson, _is_dupe
        except ImportError:
            from orchestrator import _clean_lesson, _is_dupe
        lines, kept = [], []
        for f in sorted(os.listdir(stores)):
            if not f.endswith("_store.jsonl"):
                continue
            n = f[:-len("_store.jsonl")]
            if seats_filter and n not in seats_filter:
                continue
            if n not in orch.personas:
                orch.add_persona(n)
            pp = orch.personas[n]
            if not pp.store.cases:
                continue
            e = orch.embedder.embed(task)
            for c, sim in pp.store.retrieve(e, k=5)[:3]:
                fr, ok, ws = _clean_lesson(f"[{c['id']}] {_lesson_text(c)} (outcome: {c['outcome']})")
                if _is_dupe(ws, kept):
                    continue  # An idea already written is never rewritten
                kept.append(ws)
                lines.append(f"[{n}] {fr} — {ok}")
        if not lines:
            return ""
        print(f"[ground] SIMPLE profile: {len(lines)} flat lines", file=_s.stderr)
        return ("### PANEL GROUND (compiled from lived stores - work from these; "
                "if something is not here, say so)") + chr(10) + chr(10).join(lines)
    # the DESIGN pipeline: panel selection -> judge-head input review ->
    # compiled prompt (relevance-gated ground with cited precedents)
    prompt, verdicts, stamp = orch.compile_prompt(
        task, (task or "")[:120], instance_id=instance_id)
    seats_seen = sorted({seg.split("]")[0][1:] for seg in (prompt or "").split("[")[1:]})
    print(f"[ground] precedents: {len(seats_seen)} seats: {seats_seen}", file=_s.stderr)
    # LIVED READ-BACK (learning loop closes here): lessons written by the host's
    # writeback into lived stores (AURA_PBLIVED, default: a "fullflow/personal"
    # folder beside the stores root) are appended to the ground - a lesson
    # learned on run N must be visible to run N+1 (the soul of pblocks: they
    # learn from every lived run, not just the training corpus).
    lived_dirs = [d for d in (
        os.environ.get("AURA_PBLIVED", ""),
        os.path.join(os.path.dirname(stores), "fullflow", "personal"),
    ) if d and os.path.isdir(d)]
    if lived_dirs:
        try:
            from .orchestrator import _clean_lesson, _is_dupe
        except ImportError:
            from orchestrator import _clean_lesson, _is_dupe
        _l_seen: list[str] = []
        _lived_lines: list[str] = []
        for _ld in lived_dirs:
            for _f in sorted(os.listdir(_ld)):
                if not _f.endswith("_store.jsonl"):
                    continue
                _n = _f[:-len("_store.jsonl")]
                try:
                    _o = Orchestrator(root=_ld)
                    if _n not in _o.personas:
                        _o.add_persona(_n)
                    _pp = _o.personas[_n]
                    if not _pp.store.cases:
                        continue
                    _e = orch.embedder.embed(task)
                    for _c, _sim in _pp.store.retrieve(_e, k=5)[:2]:
                        _fr, _ok, _ws = _clean_lesson(
                            f"{_lesson_text(_c)} (outcome: {_c['outcome']})")
                        if _is_dupe(_ws, _l_seen):
                            continue
                        _l_seen.append(_ws)
                        _lived_lines.append(f"[{_n}] {_fr} — {_ok}")
                except Exception as _le:
                    import sys as _ls
                    print(f"[ground] lived store {_n} skipped: {_le}", file=_ls.stderr)
                    continue
        if _lived_lines:
            print(f"[ground] lived read-back: {len(_lived_lines)} lessons", file=_s.stderr)
            prompt = (prompt or "") + chr(10) + chr(10).join(
                ["### LIVED LESSONS (learned from this test's own runs)"]
                + _lived_lines)
    return prompt

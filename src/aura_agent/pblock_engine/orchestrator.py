"""Orchestrator + persona panel + compile loop (Tier 0 wiring).

Flow (design doc sec 3a):
  input -> orchestrator picks panel -> each persona judges (working state +
  store retrieval + v0 judge) -> consolidator merges with precedence rules ->
  compiled prompt -> [brain is external in Tier 0] -> output audit stub.
"""
from __future__ import annotations

import os
import re

import numpy as np
from .quality import lesson_text as _lesson_text

from .judge_v0 import V0Judge
from .schemas import (Case, Judgment, PanelVerdicts, PersonaInputVerdict)
from .store import PersonaStore
from .working_state import HashEmbedder, MiniLMEmbedder, make_embedder, WorkingState, cosine

# Precedence is declared, never hardcoded: a pblock template may carry
# `precedence: <int>` (lower = outranks). Undeclared personas rank after all
# declared ones; ties break alphabetically so merges stay deterministic.
# Domain law is unchanged: security > correctness > style.
def _clean_lesson(h: str) -> tuple[str, str, set[str]]:
    """Pure-knowledge rendering of a precedent line: drop the [id], the
    archetype-tag chain, the sim number; translate the outcome to
    worked/failed. Returns (clean_text, worked|failed, word-set for dedupe)."""
    fr = re.sub(r"^\[[0-9a-f]+\]\s*", "", h)
    ok = "worked" if re.search(r"outcome: 1\.0", fr) or "outcome: ok" in fr else "failed"
    fr = re.sub(r"\s*\(outcome: [^)]*\)", "", fr)
    fr = re.sub(r"^[a-z0-9][a-z0-9\-/]*(?:\.[a-z0-9\-/]+)*:\s*", "", fr)
    fr = re.sub(r"^[^:\n]+/[^:\n]+:\s+", "", fr)   # Slash-chain tags (spaces/caps ok)
    fr = re.sub(r", sim [\d.]+$", "", fr)
    fr = re.sub(r"\s+", " ", fr).strip()
    words = set(re.findall(r"[A-Za-z]{3,}", fr.lower())) - {
        "the", "and", "with", "for", "that", "this", "from", "not", "its",
        "their", "they", "out", "into", "while", "what", "when", "than", "then"}
    return fr, ok, words


def _is_dupe(words: set, kept: list[set[str]], bar: float = 0.85) -> bool:
    """Render-time dedupe. Jaccard on content words vs every kept lesson."""
    for kw in kept:
        inter = len(words & kw)
        if inter / max(1, len(words | kw)) >= bar:
            return True
    return False


PRECEDENCE: dict[str, int] = {}

# ---------------------------------------------------------------------------
# The diary (spec v3 items 1+2): what the panel compiled, stamped for join.
# JSONL at <root>/diary/prompt_log.jsonl. The stamp (instance_id + attempt)
# is written here AND must be written by the dispatcher into its receipt;
# that shared key is what makes (framing -> outcome) pairs joinable.
# ---------------------------------------------------------------------------
import json as _json
import time as _time
import uuid as _uuid


class PromptDiary:
    """Append-only record of every compile: which framings/judgments the
    panel injected, stamped (instance_id, attempt) for the verdict join.
    Spec v3 items 1-2. Read side lives with the writeback (item 3)."""

    def __init__(self, root: str):
        d = os.path.join(root, "diary")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, "prompt_log.jsonl")

    def log(self, *, instance_id: str, attempt: int, situation: str,
            framing_ids: list[str], judgment_ids: list[str],
            personas: list[str]) -> str:
        stamp = f"{instance_id}#{attempt}"
        entry = {"ts": _time.time(), "stamp": stamp,
                 "instance_id": instance_id, "attempt": attempt,
                 "framing_ids": framing_ids, "judgment_ids": judgment_ids,
                 "personas": personas}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
        return stamp

    def entries(self, instance_id: str | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = _json.loads(line)
                if instance_id is None or e["instance_id"] == instance_id:
                    out.append(e)
        return out




class Persona:
    BG = ["zebra migration patterns in kenya",
          "sourdough starter feeding schedule",
          "bass guitar string gauge comparison",
          "history of byzantine coinage",
          "mountain weather forecasting basics"]

    def __init__(self, name: str, root: str, embedder: HashEmbedder):
        self.name = name
        self.embedder = embedder
        self.store = PersonaStore(name, root, frame_embedder=embedder)
        # the seat's declared floors (from its trained artifact's gates) —
        # fallback to module defaults when no artifact is present yet
        gates = {}
        art = os.path.join(root, "aura_engine", "persona", "weights", f"{name}_heads.json")
        if not os.path.exists(art):
            art = os.path.join(root, "weights", f"{name}_heads.json")
        if not os.path.exists(art):
            # trained artifacts live beside the stores root too. Search order:
            # AURA_PBWEIGHTS roots (colon-separated), then <sibling>/<seat>/weights
            for wroot in [r for r in os.environ.get("AURA_PBWEIGHTS", "").split(":") if r]:
                cand = os.path.join(wroot, name, "weights", f"{name}_heads.json")
                if os.path.exists(cand):
                    art = cand
                    break
        if not os.path.exists(art):
            sib = os.path.join(os.path.dirname(root.rstrip(os.sep)), name,
                               "weights", f"{name}_heads.json")
            if os.path.exists(sib):
                art = sib
        if not os.path.exists(art):
            # <stores>/../new-pblocks/<seat>/weights — the campaign layout
            # (works even when the launcher env predates AURA_PBWEIGHTS)
            npb = os.path.join(os.path.dirname(os.path.dirname(root.rstrip(os.sep))),
                               "new-pblocks", name, "weights", f"{name}_heads.json")
            if os.path.exists(npb):
                art = npb
        if os.path.exists(art):
            try:
                gates = _json.load(open(art, encoding="utf-8")).get("gates") or {}
            except Exception:
                gates = {}
        self.judge = V0Judge(name, embedder, self.store,
                             min_train_cases=gates.get("min_cases"),
                             min_positives_per_label=gates.get("min_positives_per_label"))
        # trained judgment: load the seat's fitted head weights so ask_noul
        # consults the trained artifact instead of abstaining forever
        if os.path.exists(art):
            self.judge.load_artifact(art)
        self.state = WorkingState(embedder)
        self.state.calibrate_background(self.BG)

    def territory_mean(self) -> list[float] | None:
        vecs = [c["embedding"] for c in self.store.cases if c["embedding"]]
        if not vecs:
            return None
        m = np.asarray(vecs, dtype=np.float32).mean(axis=0)
        n = np.linalg.norm(m)
        return (m / n).tolist() if n > 0 else m.tolist()


class Orchestrator:
    def __init__(self, root: str):
        self.root = root
        self.embedder = make_embedder()  # MiniLM if present, hash fallback
        self.personas: dict[str, Persona] = {}
        self.diary = PromptDiary(root)  # Spec v3 item 1

    def add_persona(self, name: str) -> Persona:
        p = Persona(name, self.root, self.embedder)
        self.personas[name] = p
        return p

    # -- panel selection (typed judgment; v0: similarity gate) ---------------

    def select_panel(self, situation: str) -> list[str]:
        e = self.embedder.embed(situation)
        chosen = []
        for name, p in self.personas.items():
            mean = p.territory_mean()
            sim = 0.0 if mean is None else float(
                np.asarray(e) @ np.asarray(mean))
            # Summoning gate is deliberately generous: a persona's territory
            # mean underestimates topical overlap (MiniLM-calibrated: unrelated
            # bg ~0.02, cross-domain ~0.05-0.15). Sharper relevance calls are
            # the working-state's job (judge_relevance), not the summoning gate.
            if len(p.store) == 0 or sim > 0.05:
                chosen.append(name)
        return chosen

    # -- input-side panel judgment (sec 6) ------------------------------------

    def panel_input_review(self, situation: str,
                           task_summary: str) -> PanelVerdicts:
        panel = self.select_panel(situation)
        input_side: list[PersonaInputVerdict] = []
        conflicts: list[str] = []
        for name in panel:
            p = self.personas[name]
            p.state.set_task(task_summary)
            verdict, conf = p.state.judge_relevance(situation)
            e = self.embedder.embed(situation)
            # wider net so warning/done-signal framings survive to the
            # compile split (guard A); compile shapes the final slots.
            preced = p.store.retrieve(e, k=8)
            # grounded judgment: does precedent support acting on this?
            j = p.judge.ask_noul(f"{name}_should_act", e, preced)
            enhancements = [
                f"[{c['id']}] {_lesson_text(c)} (outcome: {c['outcome']})"
                for c, _ in preced[:5]
            ]
            note = ""
            if j["confidence"] < 0.2:
                # ANNOUNCED abstention: never silent — say what
                # is inactive, why, and what still works.
                n_conf = sum(1 for c in p.store.cases if c.get("confirmed"))
                note = (f"trained judgment INACTIVE: {n_conf}/"
                        f"{p.judge.min_train_cases} confirmed cases — the persona is "
                        f"still learning; precedents above are applied, the "
                        f"fitted verdict waits for the full gate")
            input_side.append(PersonaInputVerdict(
                persona=name, verdict=verdict if verdict != "sideways" or conf > 0.5
                else "on_task",  # Relevance gate, refine in harness
                confidence=float(max(conf, j["confidence"])),
                enhancements=enhancements, note=note))
        # resolve conflicts by precedence
        if len(input_side) > 1:
            ordered = sorted(input_side,
                             key=lambda v: (PRECEDENCE.get(v["persona"], 10**9),
                                            v["persona"]))
            if ordered[0]["verdict"] == "sideways":
                for v in ordered[1:]:
                    if v["verdict"] == "on_task":
                        conflicts.append(
                            f"{ordered[0]['persona']} says sideways; "
                            f"{v['persona']} says on_task -> "
                            f"precedence: {ordered[0]['persona']} wins")
        return PanelVerdicts(input_side=input_side, output_side=[],
                             conflicts=conflicts)

    # -- compile loop (sec 6 step 5): the quality amplifier -------------------

    def compile_prompt(self, situation: str, task_summary: str,
                       agent_output: str = "", *, instance_id: str = "",
                       attempt: int = 1):
        verdicts = self.panel_input_review(situation, task_summary)
        blocked = any(v["verdict"] == "sideways" and v["confidence"] > 0.6
                      for v in verdicts["input_side"])
        lines = [
            "### TASK CONTEXT",
            f"Current task: {task_summary}",
            # The lessons are reference, not a wall: a follow-up turn's facts
            # are in the conversation, the files and the tool results
            "The lessons below are reference from earlier work. The conversation, "
            "the files and tool results come first; if a fact is in none of them, "
            "say so instead of inventing it.",
            "",
        ]
        if blocked:
            lines.append("### WARNING: panel judged this input possibly off-task. "
                         "Re-confirm the task with the user before acting.")
        # spec v3 items 6+7 + guard A: split compiled ground into patterns
        # and warnings; reserve one slot for the highest done-signal.
        import re as _re2
        # GROUND BUDGET (drift fix: arm3 dumped ~34 precedent lines vs arm4's
        # ~18 — over-stuffing dilutes attention). Total emitted lines across
        # seats are capped by AURA_PB_GROUND_BUDGET (default 18); seats are in
        # similarity order, so the strongest seats keep their lines first.
        import os as _os
        # PANEL PROFILE: both options, per host model tier.
        #   "flash"  — tight stack for speed-tier models: 18 lines, the
        #              arm4-style budget (measured best on GLM-5.3 Flash).
        #   "full"   — the complete structured briefing for frontier reasoning
        #              models: 38 lines, all organs (headers, warnings,
        #              done-signals, abstention notes) with room to breathe.
        # AURA_PB_GROUND_BUDGET overrides the profile's number if set.
        _profile = _os.environ.get("AURA_PB_PANEL_PROFILE",
                                   _os.environ.get("AURA_PB_PANEL_PROFILE_DEFAULT", "flash")).lower()
        _profile_budget = {"flash": 18, "full": 38}.get(_profile, 18)
        _budget = int(_os.environ.get("AURA_PB_GROUND_BUDGET", str(_profile_budget)))
        _emitted = 0
        _kept_ws: list[set[str]] = []
        for v in verdicts["input_side"]:
            if not v["enhancements"]:
                continue
            pats, warns, dones = [], [], []
            for h in v["enhancements"]:
                if _emitted >= _budget:
                    break
                fr, ok, ws = _clean_lesson(h)
                if _is_dupe(ws, _kept_ws):
                    continue  # An idea already written is never rewritten
                _emitted += 1
                _kept_ws.append(ws)
                low = h.lower()
                if low.startswith("[") and "outcome: verified-failure" in low:
                    warns.append(f"{fr} — {ok}")
                elif "done-signal" in low or "done:" in low:
                    dones.append(f"{fr}")
                else:
                    pats.append(f"{fr} — {ok}")
            if not (pats or warns or dones):
                continue  # A seat with nothing left to say gets no empty section
            lines.append(f"### GROUND FROM {v['persona'].upper()} PERSONA")
            lines += [f"- {h}" for h in pats]
            if warns:
                lines.append("### WARNINGS (from failed runs)")
                lines += [f"- {h}" for h in warns]
            if dones:
                lines.append("### WHAT DONE LOOKS LIKE")
                lines += [f"- {h}" for h in dones]
            lines.append("")
        lines += ["### AGENT INPUT", agent_output or situation, ""]
        # spec v3 item 6: briefing, not bossing; keep the citation rule.
        lines += ["### RULES", "1. Use the ground above as reference; cite "
                              "when a claim is influenced by it.",
                  "2. If a required fact is in neither the ground nor the "
                  "conversation, files or tool results, say so.",
                  "3. Cite which ground item each claim rests on."]
        # spec v3 item 1+2: diary entry + stamp. framing ids are recovered
        # from the enhancement prefixes "[id] ..." set in panel_input_review.
        import re as _re
        framing_ids = sorted({m.group(1)
                              for v in verdicts["input_side"]
                              for h in v["enhancements"]
                              for m in [_re.match(r"\[([a-f0-9]+)\]", h)]
                              if m})
        stamp = ""
        if instance_id:
            stamp = self.diary.log(
                instance_id=instance_id, attempt=attempt,
                situation=task_summary[:300],
                framing_ids=framing_ids,
                judgment_ids=[f'{v["persona"]}_should_act'
                              for v in verdicts["input_side"]],
                personas=[v["persona"] for v in verdicts["input_side"]])
        return "\n".join(lines), verdicts, stamp


def bootstrap_seed(store: PersonaStore, seeds: list[tuple[str, str, str]],
                   embedder: HashEmbedder, task_summary: str = "") -> None:
    """seeds: list of (situation, framing, outcome); all origin:seed."""
    for sit, framing, outcome in seeds:
        store.add_case(sit, framing, outcome, confirmed=True, origin="seed",
                       task_summary=task_summary,
                       embedding=embedder.embed(sit))

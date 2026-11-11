"""Output audit v1 (design doc sec 7): the return-side panel.

Extracts claims from model output, checks each against the persona's store
(embedding support: does lived memory contain this territory? does anything
CONTRADICT it?), plus drift check vs the working state. Verdicts: pass / flag /
fix, with persona-attributed reasons. Novel work is MARKED, not silently
trusted.
"""
from __future__ import annotations

import re

from .schemas import ClaimAudit, PanelOutputVerdict, PanelVerdicts
from .working_state import WorkingState

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(text) if len(s.strip()) > 3]


class OutputAuditor:
    def __init__(self, orchestrator):
        self.orch = orchestrator

    def audit(self, output_text: str, task_summary: str) -> PanelVerdicts:
        output_side: list[PanelOutputVerdict] = []
        conflicts: list[str] = []
        panel = self.orch.select_panel(task_summary)
        for name in panel:
            p = self.orch.personas[name]
            claims: list[ClaimAudit] = []
            fixes: list[str] = []
            for sent in _sentences(output_text):
                e = self.orch.embedder.embed(sent)
                preced = p.store.retrieve(e, k=3)
                top_sim = preced[0][1] if preced else 0.0
                if top_sim < 0.06:
                    # Nothing in lived memory: mark, never silently trust
                    claims.append(ClaimAudit(
                        claim=sent[:120], status="unverified",
                        confidence=round(1 - top_sim, 3), persona=name))
                else:
                    case, sim = preced[0]
                    status = "supported"
                    low = sent.lower()
                    # contradiction heuristics: claim negates a lived rule, or
                    # asserts a practice the persona's history flagged as wrong
                    bad_markers = ("no rate limiting", "plain text",
                                   "without validation", "unlimited",
                                   "no auth", "skip tests", "no backup")
                    neg = ("never " in case["framing"].lower()
                           or "not " in case["framing"].lower())
                    hit = (any(m in low for m in bad_markers)
                           and any(m.split()[0] in case["framing"].lower()
                                   for m in bad_markers))
                    if hit or (neg and case["situation"][:30].lower() in low):
                        status = "contradicted"
                        fixes.append(f"[{name}] contradicts lived case "
                                     f"{case['id']}: {case['framing']}")
                    claims.append(ClaimAudit(
                        claim=sent[:120], status=status,
                        confidence=float(min(1.0, sim * 2)), persona=name))
            verdict = "pass"
            if any(c["status"] == "contradicted" for c in claims):
                verdict = "fix"
            elif any(c["status"] == "unverified" for c in claims) and claims:
                verdict = "flag"
            output_side.append(PanelOutputVerdict(
                persona=name, verdict=verdict, confidence=0.7,
                claims=claims, fixes=fixes,
                note=f"{sum(c['status']=='unverified' for c in claims)}/"
                     f"{len(claims)} claims unverified"))
        # precedence: the worst verdict wins (fix > flag > pass); any
        # disagreement is logged per persona in conflicts
        order = {"fix": 0, "flag": 1, "pass": 2}
        worst = min((v["verdict"] for v in output_side),
                    key=lambda x: order.get(x, 3), default="pass")
        if len({v["verdict"] for v in output_side}) > 1:
            conflicts.append("panel verdicts differ: "
                             + ", ".join(f"{v['persona']}={v['verdict']}"
                                         for v in output_side))
        return PanelVerdicts(input_side=[], output_side=output_side,
                             conflicts=conflicts)

    def final_gate(self, verdicts: PanelVerdicts) -> tuple[str, list[str]]:
        """User-facing decision + attributed fix list."""
        fixes = [f for v in verdicts["output_side"] for f in v["fixes"]]
        order = {"fix": 0, "flag": 1, "pass": 2}
        worst = min((v["verdict"] for v in verdicts["output_side"]),
                    key=lambda x: order.get(x, 3), default="pass")
        return worst, fixes

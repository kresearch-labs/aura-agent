"""Schemas for the Personality Layer (Tier 0).

Fixed data contracts — defined BEFORE any model choice, per design doc step 1.
Every structure crossing a boundary (store, judge, orchestrator, compile loop)
is a TypedDict here. Keep this file dependency-free (stdlib only).
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional, TypedDict

# ---------------------------------------------------------------------------
# Store case (the unit of lived memory) — design doc sec 5, L2
# ---------------------------------------------------------------------------

CaseOrigin = Literal["seed", "lived", "pack", "merged"]

CaseStatus = Literal["candidate", "pending", "active", "superseded", "retired"]


class Case(TypedDict):
    id: str
    persona: str                  # Any persona name (its pblock's `name`)
    situation: str                # Raw text of the situation (for re-embedding)
    embedding: Optional[list[float]]  # Filled by the embedder; None until set
    framing: str                  # What framing/lesson this case carries
    outcome: str                  # What happened when it was used
    confirmed: bool               # Only confirmed cases are authoritative
    origin: CaseOrigin            # Seed cases are overridable by lived ones
    timestamp: float
    # working-state context at write time (lets us reconstruct judgments)
    task_summary: str
    # --- Step 0.3: bi-temporal + provenance + contradicts (absent = legacy) ---
    # transaction time: when the layer learned it (valid_from = timestamp;
    # valid_to set when superseded) vs event time: when it was true in the
    # world (occurred_at, defaults to timestamp)
    valid_from: float             # Transaction-time validity start
    valid_to: Optional[float]     # None = currently valid
    superseded_by: Optional[str]  # Id of the case that replaced this one
    contradicts: list[str]        # Ids of cases this one contradicts (edge list)
    origin_ref: str               # Provenance: source of the case (url, commit
                                  # conversation turn, pack id) — free text
    confidence: float             # 0..1, evidence weight (Bayesian blend input)
    status: CaseStatus            # Two-stage writeback lifecycle
    corroboration_count: int      # Independent confirmations beyond the first


def new_case(persona: str, situation: str, framing: str, outcome: str,
             confirmed: bool, origin: CaseOrigin,
             task_summary: str = "") -> Case:
    now = time.time()
    return Case(
        id=uuid.uuid4().hex[:12],
        persona=persona,
        situation=situation,
        embedding=None,
        framing=framing,
        outcome=outcome,
        confirmed=confirmed,
        origin=origin,
        timestamp=now,
        task_summary=task_summary,
        # bi-temporal + provenance defaults (Step 0.3)
        valid_from=now,
        valid_to=None,
        superseded_by=None,
        contradicts=[],
        origin_ref="",
        confidence=1.0 if confirmed else 0.3,
        status="active" if confirmed else "candidate",
        corroboration_count=0,
    )


# ---------------------------------------------------------------------------
# Typed judgments (System One style; our v0 judge implements these with
# log-loss-calibrated heads — the MB-scale version of RLCD)
# ---------------------------------------------------------------------------

QuestionType = Literal["choice", "noul", "score"]


class Judgment(TypedDict):
    question_id: str
    qtype: QuestionType
    value: str | float | bool      # Chosen option / probability / score
    probabilities: dict[str, float]
    confidence: float              # Calibrated: honest on average, not per-answer
    persona: str



# ---------------------------------------------------------------------------
# CONTRACT — frozen before persona corpus work (build plan Step 1) The
# consolidator's input contract: every persona judgment/enhancement crosses the
# boundary in exactly these shapes. Do not extend casually.
# ---------------------------------------------------------------------------

EnhancementType = Literal["constraint", "requirement", "watchout"]


class EnhancementRecord(TypedDict):
    """One injectable item a persona contributes to the compiled prompt.
    Domain-specific by law (Focus Law): action + topic + stance + text +
    the evidence case it rests on. No generic platitudes."""
    type: EnhancementType
    action: str                    # What the brain should do
    topic: str                     # What area it concerns
    stance: str                    # Position (for conflict detection)
    text: str                      # Rendered one-liner for the prompt slot
    evidence_case_id: str          # Provenance: the store case backing it
    confidence: float              # From the case's confidence


SlotName = Literal["L1_RULES", "TASK", "WORKING_STATE", "CONSTRAINTS",
                   "REQUIREMENTS", "WATCH_OUTS", "RULES_OF_ENGAGEMENT"]


class JudgeDecision(TypedDict):
    """The frozen v1 decision contract (supersedes raw Judgment at the
    consolidator boundary; Judgment remains the internal head output)."""
    question_id: str
    qtype: QuestionType
    value: str | float | bool
    probabilities: dict[str, float]
    confidence: float
    persona: str
    cited_precedent: Optional[str]   # Store case id backing this decision
    escalation: bool                 # True: head unreliable here -> defer
    escalation_reason: str


# ---------------------------------------------------------------------------
# Panel verdicts — design doc sec 3a
# ---------------------------------------------------------------------------

InputVerdict = Literal["on_task", "sideways", "unnecessary"]
ClaimStatus = Literal["supported", "unverified", "contradicted"]


class PersonaInputVerdict(TypedDict):
    persona: str
    verdict: InputVerdict
    confidence: float
    enhancements: list[str]        # rules/lessons/precedents to inject
    note: str


class ClaimAudit(TypedDict):
    claim: str
    status: ClaimStatus
    confidence: float
    persona: str                   # Which persona's expertise judged it


class PanelOutputVerdict(TypedDict):
    persona: str
    verdict: Literal["pass", "flag", "fix"]
    confidence: float
    claims: list[ClaimAudit]
    fixes: list[str]               # Attributed fix items ("persona-name: issue")
    note: str


class PanelVerdicts(TypedDict):
    input_side: list[PersonaInputVerdict]
    output_side: list[PanelOutputVerdict]
    conflicts: list[str]           # Logged panel disagreements (precedence resolves)

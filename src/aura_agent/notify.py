"""notify.py — notification tiers + mode-aware escalation.

Three tiers: push (approval/finding/death — interrupt the operator) · digest
(per-phase summary — batched at run end) · silent (dashboard only). Escalation
is mode-aware: UNATTENDED runs escalate to push; supervised runs keep digest.
Disclosure levels per type: approval = CONCLUSION (action + blast radius +
countdown) — rendered by approvals.card; finding push = ABOUTNESS (one line);
digest = PRESENCE.
"""
from __future__ import annotations

_PUSH = ("approval_needed", "finding_proven", "run_ended", "killed")
_DIGEST = ("phase_complete", "compaction", "gate_resume", "skip_guard")
_SILENT = ("prune", "parallel_reads", "llm_start", "llm_done",
           "tool_start", "tool_done", "status_drift", "intent_drift")

ESCALATE_AFTER_MIN = 10


def pick_tier(kind: str) -> str:
    if kind in _PUSH:
        return "push"
    if kind in _DIGEST:
        return "digest"
    return "silent"



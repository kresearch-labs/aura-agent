"""intent.py — Intent Alignment & Verification Engine.

Three parts (user's blueprint, validated against Ask-before-Plan 2024 /
SAGE-Agent 2026; ASPI 2026 caveat folded in — the clarification channel is
itself an injection surface, so user answers enter as MARKED user messages and
the mediator remains the enforcement point):

  1. Ambiguity Guard — heuristic Ambiguity Score 0.0–1.0 on the incoming
     task. >0.4 in chat at L2/L3 → pause and ask before executing.
     Heuristics only at small-model sizes (LLM self-assessment is
     unreliable on 7B); deterministic, zero-dep, auditable.
  2. Intent Contract — lightweight spec: targets, in-scope, non-goals.
     Generated from the task + scope.json + AGENTS.md before the graph.
  3. Drift audit — ongoing tool calls are checked against the contract
     (audit_calls) and findings are shown to the model.

Not injected into the prompt: the contract's content — the task and the
AGENTS.md non-goals — already reaches the model every turn (the active-task
block and the system prompt), so pinning it would only repeat it.
`contract_block` is kept for that reason but unused.

The drift audit can flag ordinary work (running tests, editing a file the named
target depends on). The loop therefore does not order a stop: it tells the
model to re-read the user's instruction, continue with a reason if the action
fits, and ask the user if still unsure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from .tool_names import BROWSER_RUN, WEB_CRAWL

AMBIGUITY_THRESHOLD = 0.4

# Heuristic signals — each adds weight to the score. Deliberately dumb:
# deterministic, explainable, and identical on every model size.
AMBIGUITY_RULES: list[tuple[str, float, str]] = [
    # (regex, weight, why)
    (r"\bit\b(?!\s+service)|\bthat\b(?!\s+is)|\bthem\b", 0.15,
     "unresolved pronoun ('it'/'that'/'them' with no antecedent in prompt)"),
    (r"\bfix\b|\bfixes\b|\bbroken\b|\bissue\b(?!s?\s*:)", 0.15,
     "'fix/broken/issue' without naming the concrete defect"),
    (r"\bimprove\b|\bmake\s+it\s+better\b|\boptimize\b", 0.15,
     "improvement verb without a measurable target"),
    (r"\b(all|everything|whatever)\b", 0.1,
     "unbounded quantifier ('all'/'everything') with no list"),
    (r"\bmaybe\b|\bperhaps\b|\bnot\s+sure\b|\bsomehow\b", 0.2,
     "explicit uncertainty in the request"),
    (r"^\s*(run|do|execute|handle)\s+(it|this)\s*$", 0.3,
     "imperative with pronoun object and no task noun"),
]


@dataclass
class IntentContract:
    task: str
    targets: list[str]                 # Explicit targets (hosts/paths/goals)
    in_scope: list[str]                # In-scope resources (from scope.json)
    non_goals: list[str]               # Explicit non-goals
    ambiguity: float
    questions: list[str]               # Asked when ambiguous
    source: str = "heuristic"

    def render(self) -> str:
        lines = ["[INTENT CONTRACT — pinned; all tool calls serve this]",
                 f"TASK: {self.task}",
                 "TARGETS: " + ("; ".join(self.targets) if self.targets
                                else "(as stated in task)")]
        if self.in_scope:
            lines.append("IN-SCOPE: " + "; ".join(self.in_scope))
        if self.non_goals:
            lines.append("NON-GOALS: " + "; ".join(self.non_goals))
        return "\n".join(lines)


def ambiguity_score(task: str) -> tuple[float, list[str]]:
    """Heuristic 0.0–1.0 + the reasons. Deterministic; no LLM needed.
    Scores are additive, capped at 1.0. A prompt with concrete targets,
    scope hints, and no pronouns/uncertainty scores ~0."""
    text = task or ""
    score = 0.0
    reasons: list[str] = []
    for pattern, weight, why in AMBIGUITY_RULES:
        if __import__("re").search(pattern, text, __import__("re").I):
            score += weight
            reasons.append(why)
    # length: very short imperative prompts are structurally underspecified
    words = len(text.split())
    if 0 < words <= 3:
        score += 0.2
        reasons.append("3 words or fewer — no room for specifics")
    # concrete anchors REDUCE ambiguity
    if re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):        # An IP
        score -= 0.15
    if re.search(r"[\w./-]+\.[a-z]{2,4}\b", text):             # A filename
        score -= 0.1
    if re.search(r"\b(?:not|don't|dont|skip|exclude|avoid)\b[^.\n]{0,60}",
                 text):                                        # A non-goal
        score -= 0.1
    return round(max(0.0, min(1.0, score)), 2), reasons


def clarify_question(task: str, reasons: list[str]) -> str:
    """The pause-and-ask prompt. Scoped per ASPI: asks about the task only."""
    top = "; ".join(reasons[:3]) if reasons else "underspecified target"
    return ("Before I run this, clarify (intent check — ambiguity in your "
            f"request: {top}):\n"
            f"  1. Exact target? (host/path/feature — one line)\n"
            f"  2. What should I explicitly NOT touch?\n"
            f"Task was: {task[:160]}")


def build_contract(task: str, scope: dict | None,
                   agents_md: str | None = None) -> IntentContract:
    """Intent Contract from task + engagement scope + AGENTS.md non-goals.
    Deterministic: pulls hosts/cidrs from scope.json; non-goals from
    explicit 'don't/avoid/skip' clauses + AGENTS.md rules."""
    targets: list[str] = []
    m = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", task)
    targets.extend(dict.fromkeys(m))
    m = re.findall(r"\b[\w./-]+\.[a-z]{1,4}\b", task)
    targets.extend(dict.fromkeys(m))
    non_goals = []
    for m in re.finditer(
            r"(?:don'?t|do not|avoid|skip|no need to|without)\s+([^.;\n]+)",
            task, re.I):
        non_goals.append(m.group(1).strip())
    if agents_md:
        for ln in agents_md.splitlines():
            ln = ln.strip(" -*")
            if ln.lower().startswith(("don't ", "do not ", "never ", "avoid ")):
                non_goals.append(ln)
    in_scope = []
    if scope:
        in_scope = (list((scope or {}).get("hosts") or [])
                    + list((scope or {}).get("cidrs") or []))
    return IntentContract(task=task, targets=list(dict.fromkeys(targets))[:12],
                          in_scope=in_scope, non_goals=non_goals[:8],
                          questions=[], ambiguity=0.0, source="heuristic")


def contract_block(contract: IntentContract) -> str:
    """System-prompt block for the pinned contract. STATIC by definition:
    byte-stable per task, so it lands in the KV-cache prefix (gate compatible —
    no timestamps, no counters).
    """
    return ("[INTENT CONTRACT — all tool calls must serve this; "
            "drift is a defect]\n"
            f"TASK: {contract.task[:300]}\n"
            f"TARGETS: {'; '.join(contract.targets) or '(per task)'}\n"
            + (f"IN-SCOPE: {'; '.join(contract.in_scope)}\n"
               if contract.in_scope else "")
            + (f"NON-GOALS: {'; '.join(contract.non_goals)}\n"
               if contract.non_goals else ""))


# ----------------------------------------------------- reviewer drift audit --
def audit_calls(calls: list[dict], contract: IntentContract) -> list[str]:
    """Out-of-Band drift audit: calls = [{'name', 'args'}] executed so far
    this turn. Deterministic heuristics; never blocks — the REVIEWER decides
    what to do with findings. Aligned = empty list."""
    findings: list[str] = []
    targets_l = [t.lower() for t in contract.targets]
    for c in calls:
        name = str(c.get("name", ""))
        args = c.get("args") or {}
        blob = json.dumps(args, default=str).lower()
        if contract.targets:
            hit = any(t in blob for t in targets_l)
            if not hit and name.startswith(("write_", "edit_", "run_")):
                findings.append(
                    f"drift: {name} acts outside contract targets "
                    f"({', '.join(contract.targets[:3]) or 'none stated'})")
        if name in (WEB_CRAWL, BROWSER_RUN) and "http" in blob:
            url = re.search(r"https?://[^\"'\\s]+", blob)
            if url and not any(t in url.group(0) for t in targets_l):
                findings.append(f"drift: {name} reached {url.group(0)[:80]} — "
                                "not in intent targets")
    return findings

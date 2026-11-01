"""reviewer.py — Separate-context Strategist-Executor-Reviewer pass.

Never the agent's own chain-of-thought (zero self-review contamination,
enforced by the prompt builder).

Output contract: diagnosis + 3 branch options + recommendation (parsed).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..engine.graph import TaskGraph
from ..mediator import Mediator

REVIEWER_SYSTEM = """You are the REVIEWER for an autonomous agent working on
a task. The agent is stuck (repeated failures) or finished. You see ONLY:
the task graph and the failed tool calls with their errors. You never see
the agent's reasoning — judge from evidence alone.

Respond in exactly this format:
DIAGNOSIS: <one paragraph, evidence-based>
OPTION 1: <approach>
OPTION 2: <approach>
OPTION 3: <approach>
RECOMMEND: OPTION <n> — <one-line why>"""


def build_reviewer_context(mediator: Mediator, graph: TaskGraph,
                           failed_calls: list[dict],
                           max_calls: int = 20) -> list[dict]:
    """THE contamination gate: only these three sources, nothing else."""
    parts: list[str] = []
    s = graph.summary()
    parts.append("TASK GRAPH:\n" + (graph.context_block() or "(empty)"))
    if failed_calls:
        lines = []
        for c in failed_calls[-max_calls:]:
            where = c.get("target") or json.dumps(c.get("args", {}))[:80]
            line = f"- {c.get('call')}({where}) → {c.get('status')}"
            if c.get("error"):
                line += f": {str(c['error'])[:160]}"
            lines.append(line)
        parts.append("FAILED CALLS (this run):\n" + "\n".join(lines))
    parts.append(f"STATUS COUNTS: {json.dumps(s.get('by_status', {}))}")
    return [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def failed_from_audit(audit_path: Path, limit: int = 50) -> list[dict]:
    """Pull refusal/error lines from the audit log (tail)."""
    if not Path(audit_path).exists():
        return []
    out = []
    for line in Path(audit_path).read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status", "OK") == "OK":
            continue
        out.append(rec)
    return out


REVIEWER_JSON = ('Answer as one JSON object instead: {"diagnosis": "<one paragraph>", '
                 '"options": ["<approach 1>", "<approach 2>", "<approach 3>"], '
                 '"recommend": "OPTION <n> — <one-line why>"}')


def parse_review(text: str) -> dict:
    """Extract {diagnosis, options, recommend}; tolerant of drift. A JSON answer
    is read first.
    """
    d = {"diagnosis": "", "options": [], "recommend": None}
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    if body.startswith("{"):
        try:
            j = json.loads(body)
            if isinstance(j, dict) and j.get("diagnosis"):
                opts = j.get("options") or []
                return {"diagnosis": str(j["diagnosis"]),
                        "options": [str(o) for o in opts][:3] if isinstance(opts, list) else [str(opts)],
                        "recommend": (str(j["recommend"]) if j.get("recommend") else None)}
        except (ValueError, TypeError):
            pass
    for line in text.splitlines():
        ls = line.strip()
        low = ls.lower()
        if low.startswith("diagnosis:"):
            d["diagnosis"] = ls.split(":", 1)[1].strip()
        elif low.startswith(("option", "option %d:" % (len(d["options"]) + 1))) \
                and low.startswith("option"):
            d["options"].append(ls.split(":", 1)[1].strip() if ":" in ls else ls)
        elif low.startswith("recommend:"):
            d["recommend"] = ls.split(":", 1)[1].strip()
    return d


class Reviewer:
    """Separate-context reviewer pass (Strategy pattern; LLM injected)."""

    def __init__(self, llm, mediator: Mediator, graph: TaskGraph,
                 audit_path: Path):
        self.llm = llm
        self.m = mediator
        self.graph = graph
        self.audit_path = Path(audit_path)

    def should_review(self, failure_streak: int, before_report: bool) -> bool:
        return failure_streak >= 2 or before_report

    def review(self, failure_streak: int = 0, before_report: bool = False,
               failed_calls: list[dict] | None = None) -> dict:
        """failed_calls: this run's failures with real error text, supplied
        by the loop. The audit log is only a fallback — it stores an args
        hash, records mediator refusals but not tool errors (edit mismatch,
        missing file), and spans every past run."""
        if not self.should_review(failure_streak, before_report):
            return {"ran": False}
        ctx = build_reviewer_context(
            self.m, self.graph,
            failed_calls if failed_calls is not None
            else failed_from_audit(self.audit_path))
        side = getattr(self, "side_chat", None)
        # the loop's cache-safe side call when wired (same opening bytes as
        # the main calls; still no conversation — the gate above holds)
        res = (side(ctx[0]["content"], ctx[1]["content"], temperature=0.2,
                    json_ask=REVIEWER_JSON) if side
               else self.llm.chat(ctx, tools=None, temperature=0.2))
        parsed = parse_review(res.content)
        return {"ran": True, "failure_streak": failure_streak,
                "before_report": before_report, **parsed}

"""tools/constraints_tool.py — model-facing operator-constraint operations.

Read is plan-safe (READ); add/remove are WRITE-class (mediator-audited). The
constraints FILE itself is never exposed to arbitrary write_file — only these
surgical ops, so the agent cannot silently delete rules.

the agent may PROPOSE adding or removing a rule when it thinks one should
change, but it always asks first — rules_add and rules_remove are `always_ask`:
the operator is asked at every assist level, sees the rule and the agent's
reason, and can never pre-approve rule changes. With nobody to ask, the change
is refused.
"""
from __future__ import annotations

from pathlib import Path

from .. import constraints as C
from ..mediator import BlastRadius, Mediator, ToolSpec

MAX_RULE_CHARS = 300


def register(mediator: Mediator, dispatch: dict, root: Path) -> None:
    root = Path(root)

    def rules_read(_args) -> str:
        full = C.full_block(root)
        return full if full else "no operator constraints set"

    def rules_add(args) -> str:
        text = (args.get("text") or "").strip()
        if not text:
            return "refused: empty rule"
        # one rule = one line: a newline would write extra "- " rules the
        # operator did not see as separate rules
        if "\n" in text or "\r" in text:
            return "refused: a rule is a single line"
        if len(text) > MAX_RULE_CHARS:
            return f"refused: a rule is at most {MAX_RULE_CHARS} characters"
        return C.add_rule(root, text)

    def rules_remove(args) -> str:
        return C.remove_rule(root, str(args.get("index_or_text", "")))

    def describe_remove(args) -> str:
        rule = C.resolve_rule(root, str(args.get("index_or_text", "")))
        return (f"removes rule: {rule}" if rule
                else "no such rule — nothing would be removed")

    mediator.register(ToolSpec(
        "rules", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["rules"] = rules_read

    mediator.register(ToolSpec(
        "rules_add", BlastRadius.WRITE, lambda a: None, always_ask=True))
    dispatch["rules_add"] = rules_add

    mediator.register(ToolSpec(
        "rules_remove", BlastRadius.WRITE, lambda a: None, always_ask=True,
        describe=describe_remove))
    dispatch["rules_remove"] = rules_remove

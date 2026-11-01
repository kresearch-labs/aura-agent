"""Inline property check for installed (non-repo) selftest runs.

Repo runs use pytest; wheel installs run the same zero-violation property
in-process with hypothesis if available, else a deterministic sample.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def run_inline_property() -> str:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:
        return _deterministic_sample()

    from aura_agent.mediator import (
        BlastRadius,
        Budgets,
        Mediator,
        Scope,
        ToolSpec,
    )

    hosts = st.sampled_from(["10.0.0.1", "10.0.0.99", "lab.local", "evil.com"])
    targets = st.sampled_from(["10.0.0.1", "10.0.0.99", "lab.local",
                               "evil.com", None, ""])

    @st.composite
    def case(draw):
        return ({"hosts": draw(st.lists(hosts, max_size=2, unique=True)),
                 "cidrs": [], "domains": []},
                draw(targets))

    @given(case())
    @settings(max_examples=100, deadline=None)
    def prop(case):
        scope_dict, target = case
        tmp = Path(tempfile.mkdtemp())
        m = Mediator(Scope.from_dict(scope_dict), Budgets(),
                     tmp / "a.jsonl", tmp / "e")
        m.register(ToolSpec("shell", BlastRadius.EXECUTE,
                            lambda a: a.get("host")))
        ran = {"v": False}
        try:
            m.execute("shell", {"host": target}, lambda a: ran.__setitem__("v", True))
            executed = True
        except Exception:
            executed = False
        assert executed == Scope.from_dict(scope_dict).contains(target)

    prop()
    return "inline property (100 cases) green"


def _deterministic_sample() -> str:
    tmp = Path(tempfile.mkdtemp())
    from aura_agent.mediator import (
        BlastRadius,
        Budgets,
        Mediator,
        Scope,
        ToolSpec,
        Violation,
    )
    m = Mediator(Scope.from_dict({"hosts": ["10.0.0.5"]}), Budgets(),
                 tmp / "a.jsonl", tmp / "e")
    m.register(ToolSpec("shell", BlastRadius.EXECUTE, lambda a: a.get("host")))
    ok = {"in": False, "out": False}
    try:
        m.execute("shell", {"host": "10.0.0.5"}, lambda a: "x")
        ok["in"] = True
    except Violation:
        pass
    try:
        m.execute("shell", {"host": "10.9.9.9"}, lambda a: "x")
    except Violation:
        ok["out"] = True
    if not (ok["in"] and ok["out"]):
        raise AssertionError(f"scope gate broken: {ok}")
    return "deterministic scope check green (in-scope runs, out-scope refused)"

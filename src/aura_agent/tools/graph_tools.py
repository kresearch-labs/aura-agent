"""graph_tools.py — model-facing task-graph operations.

All graph mutations go through the mediator as WRITE-class tools, so plan mode
blocks them automatically. Retry-gate: task_retry consults the failure
registry; blocked retries raise ToolError with the registry reason so the model
must branch or override explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..engine.graph import TaskGraph
from ..mediator import BlastRadius, Mediator, ToolSpec


def register(mediator: Mediator, dispatch: dict, engagement_root: Path) -> None:
    g = TaskGraph(Path(engagement_root) / "graph.json")

    def task_add(args) -> str:
        node = g.add(args["goal"], parents=args.get("parents") or [])
        return node.id

    def task_switch(args) -> str:
        node = g.switch(args["id"])
        return f"active: {node.id} {node.goal}"

    def task_attempt(args) -> str:
        nid = args["id"]
        verdict = g.check_retry(nid, override=args.get("override", False))
        if not verdict["allowed"]:
            return json.dumps({"blocked": True, **verdict})
        tries = g.record_attempt(nid)
        return f"attempt recorded: {nid} try={tries}"

    def task_dead(args) -> str:
        node = g.mark_dead(args["id"], args["why"])
        return f"{node.id} marked dead: {node.dead_end}"

    def task_clue(args) -> str:
        g.add_clue(args["id"], args["clue"])
        return "clue recorded"

    def task_status(_args) -> str:
        return json.dumps(g.summary(), ensure_ascii=False)

    specs = [
        ("task_add", "Register a new task/hypothesis node.",
         {"goal": "str", "parents": "list[str]?"}, "task_add", True),
        ("task_switch", "Switch to another task branch.",
         {"id": "str"}, "task_switch", True),
        ("task_attempt", "Record an attempt on a task (failure-registry gated).",
         {"id": "str", "override": "bool?"}, "task_attempt", True),
        ("task_dead", "Mark a hypothesis dead with the reason (failure registry).",
         {"id": "str", "why": "str"}, "task_dead", True),
        ("task_clue", "Attach evidence/artifact ref to a task.",
         {"id": "str", "clue": "str"}, "task_clue", True),
        ("task_status", "Read task graph summary.",
         {}, "task_status", False),
    ]
    fns = {"task_add": task_add, "task_switch": task_switch,
           "task_attempt": task_attempt, "task_dead": task_dead,
           "task_clue": task_clue, "task_status": task_status}
    for name, desc, _props, fn, is_write in specs:
        mediator.register(ToolSpec(
            name,
            BlastRadius.WRITE if is_write else BlastRadius.READ,
            lambda a: None), plan_safe=not is_write)
        dispatch[name] = fns[fn]

    mediator._graph = g  # loop/reviewer access (resume-from-graph)
    return

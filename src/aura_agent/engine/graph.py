"""graph.py — Task & clue graph, plan/act modes, failure registry.

Nonlinear task nodes (BLUEPRINT): {id, status, goal, tries, parents[], clues[],
dead_end?} Stored engagement/graph.json. Branch switching + resume-from-graph.
Failure registry: a failed hypothesis is a node with dead_end note; retrying it
is BLOCKED unless the agent explicitly overrides (audit-logged).

Plan mode: mediator mode state — in 'plan', write/execute tools refuse. The LLM
cannot flip the mode; only the operator CLI / auto-policy can.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- nodes -----
@dataclass
class TaskNode:
    id: str
    goal: str
    status: str = "open"          # Open|active|done|dead|blocked
    tries: int = 0
    parents: list[str] = field(default_factory=list)
    clues: list[str] = field(default_factory=list)   # Artifact refs/evidence
    dead_end: str | None = None   # Failure-registry note (why hypothesis died)
    created_ts: float = field(default_factory=time.time)


class TaskGraph:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.nodes: dict[str, TaskNode] = {}
        self.active: str | None = None
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return  # Crash-safe: rebuild from scratch, sessions still truth
        self.nodes = {n["id"]: TaskNode(**{k: v for k, v in n.items()
                                           if k in TaskNode.__dataclass_fields__})
                      for n in data.get("nodes", [])}
        self.active = data.get("active")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "active": self.active,
            "nodes": [asdict(n) for n in self.nodes.values()],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    # -------------------------------------------------------------- mutation
    def add(self, goal: str, parents: list[str] | None = None) -> TaskNode:
        nid = f"T{uuid.uuid4().hex[:6]}"
        node = TaskNode(id=nid, goal=goal, parents=parents or [])
        self.nodes[nid] = node
        if self.active is None:
            self.active = nid
            node.status = "active"
        self.save()
        return node

    def switch(self, nid: str) -> TaskNode:
        """Branch switch: old active → open, new → active."""
        if nid not in self.nodes:
            raise KeyError(f"unknown node {nid}")
        if self.active and self.active in self.nodes:
            if self.nodes[self.active].status == "active":
                self.nodes[self.active].status = "open"
        self.active = nid
        node = self.nodes[nid]
        node.status = "active" if node.status == "open" else node.status
        self.save()
        return node

    # ------------------------------------------------------ failure registry
    def record_attempt(self, nid: str) -> int:
        node = self.nodes[nid]
        node.tries += 1
        self.save()
        return node.tries

    def mark_dead(self, nid: str, why: str) -> TaskNode:
        """Register a failed hypothesis. The node is a dead end."""
        node = self.nodes[nid]
        node.status = "dead"
        node.dead_end = why
        self.save()
        return node

    def check_retry(self, nid: str, override: bool = False) -> dict:
        """FAILURE REGISTRY gate: retry of dead hypothesis is blocked unless
        explicitly overridden (caller audit-logs the override)."""
        node = self.nodes.get(nid)
        if node is None:
            return {"allowed": True}
        if node.status == "dead" and not override:
            return {"allowed": False,
                    "reason": f"hypothesis {nid} marked dead: {node.dead_end}",
                    "hint": "pick a new branch or retry with override=true"}
        return {"allowed": True, "tries": node.tries}

    def add_clue(self, nid: str, clue: str) -> None:
        self.nodes[nid].clues.append(clue)
        self.save()

    # ----------------------------------------------------------------- views
    def summary(self) -> dict:
        by_status: dict[str, int] = {}
        for n in self.nodes.values():
            by_status[n.status] = by_status.get(n.status, 0) + 1
        active = self.nodes.get(self.active) if self.active else None
        return {"nodes": len(self.nodes), "by_status": by_status,
                "active": {"id": active.id, "goal": active.goal,
                           "tries": active.tries} if active else None,
                "dead_ends": [{"id": n.id, "goal": n.goal, "why": n.dead_end}
                              for n in self.nodes.values() if n.status == "dead"]}

    def context_block(self) -> str:
        """Injectable graph state for prompts (resume-from-graph)."""
        if not self.nodes:
            return ""
        lines = ["[TASK GRAPH]"]
        for n in self.nodes.values():
            mark = {"open": " ", "active": ">", "done": "+",
                    "dead": "x", "blocked": "~"}[n.status]
            lines.append(f"{mark} {n.id} tries={n.tries} {n.goal[:70]}")
            if n.dead_end:
                lines.append(f"    dead-end: {n.dead_end[:100]}")
            for c in n.clues[-3:]:
                lines.append(f"    clue: {c[:100]}")
        return "\n".join(lines)

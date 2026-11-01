"""tools/symbols_tool.py — model-facing real-entity manifest (#1).

`symbols` (READ, plan-safe): the enumerated sheet — files/defs/imports/cmds.
The loop injects the sheet header automatically when a task starts (see loop
wiring); this tool lets the model pull the FULL or FILTERED sheet before
writing code that calls project APIs.
"""
from __future__ import annotations

from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec
from ..symbols import SymbolTable


def register(mediator: Mediator, dispatch: dict, root: Path,
             table: SymbolTable | None = None) -> SymbolTable:
    table = table or SymbolTable(Path(root))

    def symbols_read(args) -> str:
        q = (args.get("filter") or "").strip().lower()
        if not q:
            return table.sheet(max_lines=80)
        # filtered view
        d = table.data
        lines = [f"[PROJECT SYMBOLS matching '{q}']"]
        files = [f for f in d["files"] if q in f.lower()][:20]
        funcs = [f for f in d["funcs"] if q.lower() in f.lower()][:20]
        if files:
            lines.append("files: " + ", ".join(files))
        if funcs:
            lines.append("defs: " + ", ".join(funcs))
        if not files and not funcs:
            lines.append(f"no real entity matches '{q}' — do not invent one; "
                         "ask the operator or list the directory")
        return "\n".join(lines)

    mediator.register(ToolSpec(
        "symbols", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["symbols"] = symbols_read
    return table

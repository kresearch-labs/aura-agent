"""todo.py — externalized plan (combats goal drift).

Store: engagement root `todos.jsonl`, one record per item: {seq, id, text,
status: pending|in_progress|done, ts} `todo_write` (WRITE blast radius):
add/update/tick items; returns the list. The open list is injected into the
prompt every step (loop), so there is no separate read tool. The loop
instruction tells the model to plan at task start and tick off as it goes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

TODO_FILE = "todos.jsonl"
TODO_INSTRUCTION = """Planning discipline: for any multi-step task, FIRST call
todo_write with one pending item per step, then work through them — set
in_progress when starting an item, done when it's finished. The operator sees
the checklist live, and it is shown to you every step."""

STATUS_ORDER = {"pending": 0, "in_progress": 1, "done": 2}


def _todo_path(root: Path) -> Path:
    return Path(root) / TODO_FILE


def _load(root: Path) -> list[dict]:
    """Current plan: the LATEST record per id (the file is an append-only history
    — every status change appends a line), archived items hidden, first-seen
    order kept. Public alias: load_items.
    """
    latest: dict[str, dict] = {}
    for rec in _load_history(root):
        latest[rec["id"]] = rec          # Dict keeps first-seen order
    return [r for r in latest.values() if r.get("status") != "archived"]


def _load_history(root: Path) -> list[dict]:
    """Every todo record ever written, in file order."""
    p = _todo_path(root)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("record") == "todo":
            out.append(rec)
    return out


load_items = _load  # Public alias for loop injection


def _append(root: Path, rec: dict) -> None:
    p = _todo_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _next_id(items: list[dict]) -> str:
    nums = [int(i["id"][1:]) for i in items
            if i["id"].startswith("T") and i["id"][1:].isdigit()]
    return f"T{max(nums, default=0) + 1}"


def archive_done(root: Path) -> int:
    """A new task starts: the finished steps of earlier tasks leave the plan. Open
    steps stay. Returns how many were archived.
    """
    n = 0
    for it in _load(root):
        if it.get("status") == "done":
            _append(root, {"record": "todo", "ts": round(time.time(), 3),
                           "id": it["id"], "text": it["text"], "status": "archived"})
            n += 1
    return n


def made_at(root: Path) -> dict[str, float]:
    """When each shown step was added (an id reused after archiving counts
    from its new add)."""
    out: dict[str, float] = {}
    for rec in _load_history(root):
        if rec.get("status") == "archived":
            out.pop(rec["id"], None)
        else:
            out.setdefault(rec["id"], float(rec.get("ts") or 0))
    return out


def render_checklist(items: list[dict]) -> str:
    """."""
    if not items:
        return "(no todos)"
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    lines = []
    for it in sorted(items, key=lambda i: (STATUS_ORDER.get(i["status"], 0),
                                           int(i["id"][1:])
                                           if i["id"][1:].isdigit() else 0)):
        mark = marks.get(it["status"], "[?]")
        if it["status"] == "done":
            lines.append(f" {mark} {it['id']} {it['text']} (done)")
        elif it["status"] == "in_progress":
            lines.append(f">{mark} {it['id']} {it['text']}")
        else:
            lines.append(f" {mark} {it['id']} {it['text']}")
    return "\n".join(lines)


def register_todo(mediator, fs_dispatch: dict, root) -> None:
    """Wire todo_write through the mediator."""
    from .mediator import BlastRadius, ToolSpec

    root = Path(root)

    def todo_write(args) -> str:
        action = (args.get("action") or "add").lower()
        items = _load(root)
        if action == "add":
            text = str(args["text"]).strip()
            if not text:
                return "error: empty text"
            rec = {"record": "todo", "ts": round(time.time(), 3),
                   "id": _next_id(items), "text": text, "status": "pending"}
            _append(root, rec)
            items.append(rec)
        elif action in ("status", "set"):
            tid = str(args.get("id", "")).strip()
            status = str(args.get("status", "pending")).lower()
            if status not in STATUS_ORDER:
                return f"error: bad status '{status}' (pending|in_progress|done)"
            want = tid.upper() if tid.upper().startswith("T") else f"T{tid}"
            target = next((i for i in items if i["id"].upper() == want), None)
            if target is not None:
                tid = target["id"]
            if target is None:
                return f"error: no todo {tid}"
            rec = {"record": "todo", "ts": round(time.time(), 3),
                   "id": tid, "text": target["text"], "status": status}
            _append(root, rec)
            target["status"] = status
        elif action == "clear_done":
            for it in items:
                if it["status"] == "done":
                    _append(root, {"record": "todo", "ts": round(time.time(), 3),
                                   "id": it["id"], "text": it["text"],
                                   "status": "archived"})
            items = [i for i in _load(root) if i["status"] != "archived"]
        else:
            return f"error: unknown action '{action}' (add|status|clear_done)"
        return render_checklist(_load(root))

    mediator.register(ToolSpec("todo_write", BlastRadius.WRITE, lambda a: None))
    fs_dispatch["todo_write"] = todo_write


def render_block(items: list[dict]) -> str:
    """."""
    return ("[TODO STATE — externalized plan; keep it updated]\n"
            + render_checklist(items))

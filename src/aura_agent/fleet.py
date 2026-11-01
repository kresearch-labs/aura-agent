"""fleet.py — Background agent fleet (claude-code pattern).

`aura fleet dispatch "task1" "task2"...` — N parallel agents over ONE project,
each with its own notes folder (background.py machinery), with named roles from
the profile's model lanes when available. The helpers are threads of this
process, so dispatch STAYS until all of them finish, printing each result as it
lands; Ctrl+C asks them to wrap up.

  aura fleet status — table of live/finished dispatches
  aura fleet steer <id> MSG — course-correct a running child
  aura fleet collect — drain finished children, print summaries
  aura fleet stop <id> — cooperative stop (steer with a stop note)

Persistence: handles live in-process AND mirrored to engagement/fleet.json so
`aura fleet status` works from a new process for finished dispatches (running
ones show as 'unknown' cross-process — documented, honest).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .background import (drain_finished, list_handles, spawn_background,
                         steer)


def _fleet_file(eng: Path) -> Path:
    return Path(eng) / "fleet.json"


def _mirror(eng: Path) -> None:
    handles = list_handles()
    data = {"updated": time.time(),
            "dispatches": [dict(h) for h in handles]}
    _fleet_file(eng).write_text(json.dumps(data, indent=1),
                                encoding="utf-8")


def dispatch(eng: Path, tasks: list[str], profile, scope, audit_path: Path,
             names: list[str] | None = None) -> list[dict]:
    """Spawn one background child per task; mirror to fleet.json."""
    handles = []
    for i, task in enumerate(tasks):
        name = (names[i] if names and i < len(names)
                else f"agent-{i + 1}")
        h = spawn_background(Path(eng), profile, scope, audit_path,
                             task, name=name)
        handles.append(h)
    _mirror(eng)
    return handles


def status_table() -> str:
    handles = list_handles()
    if not handles:
        return "fleet: no dispatches in this process"
    lines = [f"fleet: {len(handles)} dispatch(es)"]
    for h in handles:
        mark = {"running": "▸", "done": "✓", "finished": "✓",
                "failed": "✗"}.get(
            h["status"], " ")
        dur = ""
        if h.get("finished_ts") and h.get("started_ts"):
            dur = f" {h['finished_ts'] - h['started_ts']:.0f}s"
        lines.append(f"  {mark} {h['handle']}  {h['name']:12s} "
                     f"{h['status']:8s} steps={h.get('steps') or '-'}"
                     f"{dur}  {h['task'][:48]}")
    return "\n".join(lines)


def collect(eng: Path) -> list[dict]:
    done = drain_finished()
    _mirror(eng)
    return done


def wait_all(eng: Path, on_done, poll_s: float = 1.0,
             grace_s: float = 60.0) -> list[dict]:
    """Stay until every helper of this process has finished; on_done(h)
    for each as it lands (mirrored to fleet.json, so `aura fleet status`
    in another terminal sees the results). Ctrl+C: every running helper
    is told to wrap up, given grace_s, then the process ends."""
    finished: list[dict] = []
    deadline = None
    while True:
        try:
            for h in drain_finished():
                finished.append(h)
                on_done(h)
            _mirror(eng)
            if not any(h["status"] == "running" for h in list_handles()):
                # A helper that finished between the drain above and this check
                # was never reported — collect once more before leaving
                for h in drain_finished():
                    finished.append(h)
                    on_done(h)
                _mirror(eng)
                return finished
            if deadline is not None and time.time() > deadline:
                return finished
            time.sleep(poll_s)
        except KeyboardInterrupt:
            if deadline is not None:
                return finished             # Second Ctrl+C: leave now
            for h in list_handles():
                if h["status"] == "running":
                    stop(h["handle"])
            deadline = time.time() + grace_s


def stop(h_id: str) -> bool:
    ok = steer(h_id, "[FLEET STOP] wrap up now: finish the current step, "
                     "write deliverables, end the run.")
    return ok


def load_mirrored(eng: Path) -> dict:
    p = _fleet_file(eng)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}

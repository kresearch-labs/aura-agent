"""background.py — background sub-agents + live steer + role routing.

`delegate(background=True)` returns a handle at once; the child runs in a
daemon thread with the SAME harness (AgentLoop + Mediator + Governor in its
jailed delegate root). Results re-enter the parent as a turn payload the caller
polls or drains. Live steer: append a message into a RUNNING child's queue —
the child's loop picks it up as a user turn before its next proposal.

Per-role model routing (profile field):
    profile.roles = {"planner": <ProviderProfile>, "worker": …,
                     "reviewer": …}
    run_delegate(role={"model_lane": "worker", …}) → the child LLM runs
    on that lane's profile; fallback to the parent profile when the lane
    is missing or its provider errors (failover chain).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .delegate import _child_root, run_delegate

# ------------------------------------------------------------ the registry --
_HANDLES: dict[str, dict] = {}          # handle_id → handle dict
_HANDLE_SEQ = 0
_LOCK = threading.Lock()


def _next_handle_id() -> str:
    global _HANDLE_SEQ
    with _LOCK:
        _HANDLE_SEQ += 1
        return f"bg-{int(time.time())}-{_HANDLE_SEQ}"


def spawn_background(parent_root: Path, profile, scope,
                     audit_path: Path, task: str, name: str = "sub",
                     max_steps: int | None = None, role: dict | None = None,
                     steer_queue: list[str] | None = None) -> dict:
    """Start a child in a daemon thread. Returns the handle dict at once:
    {handle, name, root, session(dir), status='running', steer: queue}."""
    role = role or {}
    h_id = _next_handle_id()
    # the child root must exist before the thread starts (poll surface)
    root = _child_root(parent_root, name)
    handle = {
        "handle": h_id,
        "name": name,
        "task": task[:120],
        "root": str(root),
        "status": "running",
        "started_ts": time.time(),
        "finished_ts": None,
        "steps": None,
        "summary": None,
        "error": None,
        "steer": steer_queue if steer_queue is not None else [],
    }
    with _LOCK:
        _HANDLES[h_id] = handle

    # per-role model lane: role["model_lane"] names a lane in
    # profile.roles; missing lane → parent profile (graceful)
    lane = role.get("model_lane")
    lane_profile = profile
    if lane:
        lanes = getattr(profile, "roles", None) or {}
        lane_profile = lanes.get(lane, profile)

    from .paths import workspace_for as _workspace_for

    def _run():
        try:
            out = run_delegate(parent_root, lane_profile, scope,
                               audit_path, task, name=name,
                               max_steps=max_steps, role=role,
                               steer_queue=handle["steer"],
                               workspace=_workspace_for(parent_root))
            handle["summary"] = out.get("summary", "")
            handle["steps"] = out.get("steps")
            handle["changes"] = out.get("changes") or []
            handle["write"] = bool(role.get("write"))
            handle["session"] = out.get("session", "")
            handle["status"] = "done"
        except Exception as e:                 # Child failure ≠ registry death
            handle["status"] = "failed"
            handle["error"] = f"{type(e).__name__}: {e}"
        finally:
            handle["finished_ts"] = time.time()

    t = threading.Thread(target=_run, daemon=True, name=f"bg:{name}")
    t.start()
    handle["thread"] = t
    return handle


def list_handles() -> list[dict]:
    with _LOCK:
        return [{k: v for k, v in h.items() if k != "thread"}
                for h in _HANDLES.values()]


def steer(h_id: str, message: str) -> bool:
    """Live steer: append into a RUNNING child's queue. Returns False when
    the child already finished (the steer would never be consumed)."""
    with _LOCK:
        h = _HANDLES.get(h_id)
        if h is None or h["status"] != "running":
            return False
        h["steer"].append(message)
        return True


def drain_finished() -> list[dict]:
    """Handles that completed since the last drain — the parent consumes
    these as new turns."""
    with _LOCK:
        done = [dict(h) for h in _HANDLES.values()
                if h["status"] in ("done", "failed")
                and not h.get("_drained")]
        for h in _HANDLES.values():
            if h["status"] in ("done", "failed"):
                h["_drained"] = True
    return done



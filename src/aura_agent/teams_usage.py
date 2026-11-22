"""teams_usage.py — per-task model usage (plan).

The loop records each model call's usage here (model, tokens in/out, and
callers may add real seconds / a known cost); teams_run's finish attaches the
chat's snapshot to the task.finished report. Visible only under
activity-detail; the page shows totals per task and per project — NEVER a
per-person ranking (this module never even names a person).
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_BY_CHAT: dict[str, dict] = {}


def record(chat_id: str, model: str, tokens_in: int, tokens_out: int,
           seconds: float = 0.0, cost_usd: float | None = None) -> None:
    """Accumulate one model call. `seconds` are REAL per-task seconds the caller
    measured; record also brackets the chat's active window (first→last call)
    so the finish-time snapshot has honest seconds even without receipts.
    `cost_usd` only when actually known (spirit: never guessed).
    """
    now = time.time()
    with _LOCK:
        u = _BY_CHAT.setdefault(str(chat_id), {"model": model or "",
                                               "tokens_in": 0,
                                               "tokens_out": 0,
                                               "seconds": 0.0,
                                               "cost_usd": 0.0,
                                               "cost_known": False,
                                               "first": now})
        if model:
            u["model"] = model
        u["tokens_in"] += int(tokens_in or 0)
        u["tokens_out"] += int(tokens_out or 0)
        u["seconds"] += max(0.0, float(seconds or 0.0))
        if cost_usd is not None:
            u["cost_usd"] += float(cost_usd)
            u["cost_known"] = True
        u["last"] = now


def snapshot(chat_id: str) -> dict | None:
    """The shape: model, tokens in/out, real seconds (measured calls plus the
    active window), cost_usd ONLY when a caller knew it.
    """
    with _LOCK:
        u = _BY_CHAT.get(str(chat_id))
        if not u:
            return None
        window = max(0.0, float(u.get("last") or 0)
                     - float(u.get("first") or 0))
        out = {"model": u["model"], "tokens_in": u["tokens_in"],
               "tokens_out": u["tokens_out"],
               "seconds": round(u["seconds"] + window, 1)}
        if u.get("cost_known"):
            out["cost_usd"] = round(u["cost_usd"], 6)
        return out


def clear(chat_id: str) -> None:
    """The snapshot went out with task.finished: the next task in this chat
    starts a fresh accumulation (the usage is PER TASK)."""
    with _LOCK:
        _BY_CHAT.pop(str(chat_id), None)

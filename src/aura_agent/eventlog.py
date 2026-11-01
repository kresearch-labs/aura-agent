"""eventlog.py — Structured event stream for non-tty/CI consumers.

AURA_EVENTS=1 → every loop event appends one JSON line to
<engagement>/events.jsonl: {"ts", "kind",...payload}. CI gets the SAME events
the human sees, machine-parseable (clig / stream-json pattern). Zero effect
when unset; never raises (logging must not kill the loop).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class EventLog:
    def __init__(self, engagement_root: Path, enabled: bool | None = None):
        self.path = Path(engagement_root) / "events.jsonl"
        if enabled is None:
            enabled = os.environ.get("AURA_EVENTS", "") == "1"
        self.enabled = bool(enabled)

    def __call__(self, kind: str, data: dict) -> None:
        if not self.enabled:
            return
        try:
            rec = {"ts": round(time.time(), 3), "kind": kind}
            if isinstance(data, dict):
                rec.update(data)
            else:
                rec["data"] = str(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # Logging must never kill the loop


_SWALLOW_MAX_BYTES = 1_000_000


def _swallow_path() -> Path:
    from .paths import aura_home
    return aura_home() / "logs" / "swallowed.jsonl"


def log_swallowed(where: str, exc: BaseException | None = None) -> None:
    """FIX-3: record an exception a never-crash guard swallowed.

    Call sites keep their guarantee (the guarded path still continues);
    this only makes the failure visible in ~/.aura/logs/swallowed.jsonl
    instead of vanishing. Rotates at 1 MB (one .1 backup). Never raises.
    """
    try:
        import traceback
        rec = {"ts": round(time.time(), 3), "where": where}
        if exc is not None:
            rec["type"] = type(exc).__name__
            rec["msg"] = str(exc)[:500]
            tb = traceback.extract_tb(exc.__traceback__)
            if tb:
                last = tb[-1]
                rec["at"] = f"{Path(last.filename).name}:{last.lineno}"
        p = _swallow_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > _SWALLOW_MAX_BYTES:
            p.replace(p.with_suffix(".jsonl.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # The logger itself must never raise

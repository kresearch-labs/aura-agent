"""resolution.py — STEPS-TO-RESOLUTION instrumentation.

The metric is resolution, not deflection: after an error card, count the user
actions until the next success. Local jsonl only (engagement/ resolution.jsonl)
— telemetry stays off; this never leaves disk. Single-step target for the
top-10 failure paths.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from .eventlog import log_swallowed


class ResolutionTracker:
    def __init__(self, eng):
        self.eng = Path(eng)
        self.path = self.eng / "resolution.jsonl"
        self._pending: dict | None = None
        self._steps = 0

    def mark_error(self, card_fix: str) -> None:
        """An error card was shown; start watching for the fix."""
        self._pending = {"fix_suggested": str(card_fix)[:80],
                         "ts": round(time.time(), 3)}
        self._steps = 0

    def mark_command(self, command: str) -> None:
        if self._pending is not None:
            self._steps += 1
            self._pending["fix_used"] = str(command)[:80]

    def mark_success(self) -> None:
        """Next successful operation closes the record."""
        if self._pending is None:
            return
        rec = {**self._pending, "steps": self._steps,
               "resolved_ts": round(time.time(), 3)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as _swallowed:
            log_swallowed("resolution.record_write", _swallowed)
        self._pending = None

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

"""ledger.py — Seq'd, always-on event ledger.

Every UI render consumes read; seq is monotonic from 1; append never raises
(logging must not kill the loop). Always-on: no env gate — the ledger is the
app's memory of what happened, not a debug switch.

Persistence: JSONL at <root>/events.jsonl; unicode-safe (ensure_ascii False);
reopen resumes the seq from the file so restarts never duplicate.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class Ledger:
    """Append-only event ledger. in_memory() for tests; rooted for prod."""

    def __init__(self, root: Path | str | None = None):
        self._events: list[Event] = []
        self._seq = 0
        self._path = Path(root) / "events.jsonl" if root else None
        self.persist_errors: int = 0
        self.last_persist_error: str | None = None
        if self._path:
            self._load()

    @classmethod
    def in_memory(cls) -> "Ledger":
        return cls(None)

    def _load(self) -> None:
        """Resume seq from an existing file; never raise."""
        try:
            if self._path and self._path.exists():
                with self._path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            self._seq = max(self._seq, int(rec.get("seq", 0)))
                            self._events.append(Event(
                                seq=int(rec.get("seq", 0)),
                                ts=float(rec.get("ts", 0.0)),
                                kind=str(rec.get("kind", "")),
                                data={k: v for k, v in rec.items()
                                      if k not in ("seq", "ts", "kind")},
                            ))
                        except Exception:
                            continue  # Skip corrupt lines, keep going
        except Exception:
            pass

    def emit(self, kind: str, data: dict[str, Any] | None = None) -> None:
        """Append one event. NEVER raises — logging must not kill the loop."""
        try:
            self._seq += 1
            ev = Event(self._seq, round(time.time(), 3), kind, dict(data or {}))
            self._events.append(ev)
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                rec: dict[str, Any] = {"seq": ev.seq, "ts": ev.ts, "kind": kind}
                rec.update(ev.data)
                from .sessions import locked_append     # Shared by every Aura on the project
                locked_append(self._path, json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self.last_persist_error = None
        except Exception as e:
            # Keep seq consistent; RECORD the loss — silent swallow let
            # restarts lose every event since the failure with no signal
            self.persist_errors += 1
            self.last_persist_error = repr(e)

    def read(self) -> tuple[Event, ...]:
        """Immutable snapshot of all events, seq order."""
        return tuple(self._events)

    def last_seq(self) -> int:
        return self._seq

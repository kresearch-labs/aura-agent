"""Engine wiring — bridge loop events into the Ledger.

The engine already emits ~28 event kinds via loop.on_event (paid for this).
This module adapts them: a LedgerBridge instance passed as on_event feeds EVERY
event into the seq'd ledger (the L1 truth source), so the UI renders from one
place.

Also: the heartbeat emitter — an event-state-driven heartbeat that the engine
calls when the model is streaming but no delta arrived, so the aliveness
contract's middle arm has a ledger trace.
"""
from __future__ import annotations

import time
from typing import Any

from aura_agent.ledger import Ledger


class LedgerBridge:
    """on_event adapter: (kind, data) -> ledger.emit(kind, data).
    Never raises (the loop must not die because logging did)."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.last_seq_at_start = ledger.last_seq()

    def __call__(self, kind: str, data: dict[str, Any] | None = None) -> None:
        try:
            self.ledger.emit(kind, dict(data or {}))
        except Exception:
            pass

    # Heartbeat: called by the engine when streaming but quiet -
    def heartbeat(self, why: str = "model streaming, connection open") -> None:
        """Record a heartbeat event — the aliveness middle arm gets a
        ledger trace (never a silent wall-clock tick)."""
        self.ledger.emit("heartbeat", {"why": why})


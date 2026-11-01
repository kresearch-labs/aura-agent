"""away_summary.py — The leave-and-return contract.

Silent when nothing changed or the gap was short. First sighting never emits
(nothing to diff against). One sentence, no fiction.
"""
from __future__ import annotations

import time

_GAP_FLOOR_S = 60.0


class AwayTracker:
    """Holds the last-seen engagement state + timestamp."""

    def __init__(self):
        self._last: dict | None = None
        self._ts: float = 0.0

    def mark(self, engagement: dict) -> None:
        self._last = dict(engagement or {})
        self._ts = time.monotonic()


def away_line(tracker: AwayTracker, engagement: dict, gap_s: float) -> str:
    """One-line state-change summary, or '' when nothing worth saying."""
    last = tracker._last
    tracker.mark(engagement)
    if last is None:
        return ""
    if gap_s < _GAP_FLOOR_S:
        return ""
    parts = []
    d_find = int(engagement.get("findings", 0)) - int(last.get("findings", 0))
    d_hosts = int(engagement.get("hosts", 0)) - int(last.get("hosts", 0))
    d_blocked = int(engagement.get("blocked", 0)) - int(last.get("blocked", 0))
    if d_blocked:
        parts.append(f"{abs(d_blocked)} blocked call"
                     f"{'s' if abs(d_blocked) != 1 else ''}")
    if d_find > 0:
        parts.append(f"{d_find} new result{'s' if d_find != 1 else ''}")
    elif d_find < 0:
        parts.append("results updated")
    if d_hosts:
        parts.append(f"{abs(d_hosts)} host{'s' if abs(d_hosts) != 1 else ''} "
                     f"{'found' if d_hosts > 0 else 'removed'}")
    if not parts:
        return ""
    return "while you were away: " + ", ".join(parts)

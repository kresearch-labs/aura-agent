"""teamserver/sse.py — the ONE live stream per page (plan).

GET api/v1/stream holds the connection; `: ping` every 20 s; a ring buffer of
the last 2000 events; each subscriber gets only events its person may see;
`Last-Event-ID` replays from the buffer (older → a `reset` event tells the page
to reload).

Subscriptions are per CONNECTION: two tabs of one person are two subscriptions
— one closing never kills the other, and delivery is still filtered by that
connection's person. Every ring entry carries its may_see, so Last-Event-ID
replay filters per subscriber exactly like live delivery: a reconnecting guest
receives nothing it may not see.
"""
from __future__ import annotations

import collections
import json
import queue
import threading
PING_SECONDS = 20
BUFFER = 2000

KINDS = ("task", "comment", "log", "chat", "presence", "people",
         "project", "map")


class _Connections(dict):
    """Connection-key → queue. `.get(person_id)` ALSO answers "any open
    connection of this person" — teamserver_db.is_online() relies on that
    exact read (`subs.get(pid) is not None`)."""

    def __init__(self):
        super().__init__()
        self.person_of: dict[str, int] = {}

    def get(self, key, default=None):
        val = super().get(key)
        if val is not None:
            return val
        if isinstance(key, int) and key in self.person_of.values():
            return True                      # Presence marker, not a queue
        return default


class _Sub(queue.Queue):
    """One connection's queue; carries its connection key and person so a
    stream can unsubscribe exactly itself (never a sibling tab)."""

    def __init__(self, maxsize: int = 1000):
        super().__init__(maxsize=maxsize)
        self.conn_key: str = ""
        self.person_id: int = 0


class Broadcaster:
    """Publish (project, kind, data) once; every subscriber's queue gets it
    if that subscriber may see the project."""

    def __init__(self, buffer_size: int = BUFFER):
        self.buffer: collections.deque = collections.deque(
            maxlen=buffer_size)     # (id, project, kind, data, may_see)
        self.subs: _Connections = _Connections()
        self._next_id = 1
        self._next_conn = 1
        self._lock = threading.RLock()

    def publish(self, project, kind: str, data: dict,
                may_see=None) -> None:
        """may_see(person_id, project, kind) -> bool decides who receives
        it. It rides the ring entry, so replay NEVER shows what the live
        stream would have hidden."""
        with self._lock:
            eid = self._next_id
            self._next_id += 1
            self.buffer.append((eid, project, kind, data, may_see))
            dead = []
            for ckey, q in self.subs.items():
                pid = self.subs.person_of.get(ckey)
                if may_see is None or may_see(pid, project, kind):
                    try:
                        q.put_nowait((eid, project, kind, data))
                    except queue.Full:
                        dead.append(ckey)
            for ckey in dead:
                self._drop(ckey)

    def subscribe(self, person_id: int):
        """Every call is a NEW connection (a tab) of that person — the
        stream endpoint holds one; several tabs never starve each other
        and one closing never kills the rest. Returns (queue, last eid)."""
        q: _Sub = _Sub()
        with self._lock:
            ckey = f"c{self._next_conn}"
            self._next_conn += 1
            self.subs[ckey] = q
            self.subs.person_of[ckey] = person_id
            q.conn_key = ckey                  # Unsubscribe-by-connection
            q.person_id = person_id
            last = max((e for e, *_ in self.buffer), default=0)
        return q, last

    def connection_count(self, person_id: int) -> int:
        """Open streams of this person right now (presence transitions)."""
        with self._lock:
            return sum(1 for p in self.subs.person_of.values()
                       if p == person_id)

    def _drop(self, ckey) -> None:
        self.subs.pop(ckey, None)
        self.subs.person_of.pop(ckey, None)

    def unsubscribe(self, ref) -> int:
        """By queue (or its connection key): close just that tab. By person
        id: all of theirs (teardown / admin). Returns the person's
        remaining connections, for the offline transition."""
        with self._lock:
            ckey = getattr(ref, "conn_key", None)
            if ckey is None and isinstance(ref, str):
                ckey = ref
            pid = None
            if ckey is not None:
                pid = self.subs.person_of.get(ckey)
                self._drop(ckey)
            else:
                pid = ref                      # A person id: every tab
                for k in [k for k, p in self.subs.person_of.items()
                          if p == ref]:
                    self._drop(k)
            if pid is None:
                return 0
            return sum(1 for p in self.subs.person_of.values() if p == pid)

    def replay(self, last_event_id: int, person_id: int | None = None):
        """Events after last_event_id; (None) signals a reset when the gap is
        older than the buffer holds. With a person_id, each entry's may_see
        applies exactly as in live delivery — a guest reconnecting with an old
        id receives nothing outside what its person may see.
        """
        with self._lock:
            first = min((e for e, *_ in self.buffer), default=None)
            if first is None or last_event_id < first - 1:
                return None
            return [(eid, project, kind, data)
                    for eid, project, kind, data, may_see in self.buffer
                    if eid > last_event_id and
                    (person_id is None or may_see is None or
                     may_see(person_id, project, kind))]


def format_event(eid: int, kind: str, data: dict) -> bytes:
    return (f"id: {eid}\nevent: {kind}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode()


def ping() -> bytes:
    return b": ping\n\n"

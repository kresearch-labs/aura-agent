"""teamserver/teamserver_db.py — where the team database lives and how it opens.
One place, so the CLI, the server and the tools all agree (plan: data lives
inside Aura's own folder).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..paths import aura_home
from . import store


def team_dir() -> Path:
    d = aura_home() / "teamserver"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return team_dir() / "team.db"


def backups_dir() -> Path:
    d = team_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_team():
    """The team's connection, or None when there is no team yet (never
    creates one implicitly — first start is an explicit act)."""
    p = db_path()
    if not p.exists():
        return None
    return store.connect_thread(p)


_BROADCASTER = None


def broadcaster():
    """The ONE SSE Broadcaster for this process (the detached server); the
    tests use it to assert who received what."""
    global _BROADCASTER
    if _BROADCASTER is None:
        from .sse import Broadcaster
        _BROADCASTER = Broadcaster()
    return _BROADCASTER


def is_online(person_id: int) -> bool:
    """An open SSE stream right now (online)."""
    bc = broadcaster()
    with bc._lock:
        return bc.subs.get(person_id) is not None

"""teams_ui.py — Aura Teams in the chat screen.

So: a TEAM part in the sidebar (the hub, you and this chat's board task, who
else is working on what, waiting updates), a short mark on the status line, and
the board beside the chat (/team board in fullscreen).

The screen never waits on the network: snapshot returns what was read last and
starts a refresh in the background (every REFRESH seconds); the hub being off
is normal and shown as such.
"""
from __future__ import annotations

import threading
import time

REFRESH = 30.0
_LOCK = threading.Lock()
_STATE: dict = {"t": 0.0, "busy": False, "data": None}


def connected() -> bool:
    try:
        from .teams_run import active
        return active()
    except Exception:                                # noqa: BLE001
        return False


def pending() -> str:
    """A hub address saved but not wired yet (any-hub): its address, else ""."""
    try:
        from . import teams
        cfg = teams.load_config()
        return cfg.get("hub_url", "") if cfg.get("hub_kind") == "pending" else ""
    except Exception:                                # noqa: BLE001
        return ""


def snapshot(chat_key: str = "") -> dict:
    """What the screen shows about the team, never blocking. {} when this
    machine isn't connected to a hub."""
    if not connected():
        return {}
    now = time.monotonic()
    with _LOCK:
        stale = now - _STATE["t"] > REFRESH
        if stale and not _STATE["busy"]:
            _STATE["busy"] = True
            threading.Thread(target=_refresh, daemon=True, name="aura-team").start()
        data = dict(_STATE["data"] or {})
    data["link"] = _link(chat_key)
    try:
        from .teams_queue import queue_len
        data["queued"] = queue_len()
    except Exception:                                # noqa: BLE001
        data["queued"] = 0
    if "user" not in data:
        from . import teams
        data["user"] = teams.load_config().get("username", "")
        data.setdefault("online", None)            # Not read yet
    return data


def refresh_now() -> None:
    """Read again on the next draw (after /team start, done, connect …)."""
    with _LOCK:
        _STATE["t"] = 0.0


def _link(chat_key: str) -> dict:
    if not chat_key:
        return {}
    try:
        from .teams_run import _load
        return dict(_load().get(chat_key) or {})
    except Exception:                                # noqa: BLE001
        return {}


def _refresh() -> None:
    from . import teams
    out: dict = {}
    try:
        cfg = teams.load_config()
        out["user"] = cfg.get("username", "")
        out["hub"] = cfg.get("hub_url", "")
        if cfg.get("hub_kind") == "wired":
            try:
                from .teams_board import hub
                ok = bool(hub().whoami())
            except Exception:                        # noqa: BLE001
                ok = False
        else:
            ok, _ = teams._probe_info(cfg.get("hub_url", ""), timeout=3.0)
        out["online"] = bool(ok)
        out["app"] = cfg.get("hub_app") or ("vikunja" if cfg.get("hub_kind") != "pending" else "")
        if cfg.get("hub_kind") == "wired":
            from .teams_wiring import broken
            out["broken"] = sorted(broken())
        if ok:
            out.update(_board(out["user"]))
    except Exception as e:                           # noqa: BLE001
        out["online"] = False
        out["error"] = str(e)[:120]
    with _LOCK:
        _STATE.update(t=time.monotonic(), busy=False, data=out)


def _board(me: str) -> dict:
    """My open tasks, and what the others are working on (in progress, with
    the files their start update planned)."""
    from . import teams_board as B
    from . import teams_collide as C
    mine, working = [], []
    try:
        for t in B.my_tasks(done=False)[:20]:
            mine.append({"id": t["id"], "title": t.get("title", ""),
                         "due": B._fmt_due(t), "blocked": bool(B._blocked_by(t))})
    except Exception:                                # noqa: BLE001
        pass
    try:
        tasks = B.open_tasks()
        for t in tasks:
            labs = B.marks(t)
            who = [a.get("username", "?") for a in (t.get("assignees") or [])]
            if B.IN_PROGRESS not in labs or not who or who == [me]:
                continue
            files: list[str] = []
            try:
                for c in reversed(C._comments(t["id"])):
                    st = C.parse_start_comment(c)
                    if st:
                        files = st.get("planned") or []
                        break
            except Exception:                        # noqa: BLE001
                pass
            working.append({"id": t["id"], "title": t.get("title", ""),
                            "who": [w for w in who if w != me] or who, "files": files})
            if len(working) >= 8:
                break
    except Exception:                                # noqa: BLE001
        pass
    return {"mine": mine, "working": working}


def status_mark(data: dict) -> tuple[str, str] | None:
    """(text, role) for the status line; None when not connected."""
    if not data:
        return None
    q = data.get("queued") or 0
    if data.get("online") is None:
        return "team …", "faint"
    if not data.get("online"):
        return "team ○ offline" + (f" · {q} waiting" if q else ""), "warn"
    n = len(data.get("working") or [])
    return "team ● " + (f"{n} working" if n else "online") + (f" · {q} waiting" if q else ""), "ok"

"""teams_check.py — prove a wiring on the live hub before anyone relies on it.

The check makes ONE task — "Aura wiring check — safe to delete" — runs every
operation on it, reads each result back, and closes it. Nothing else on the
board is written. The result is a list for the card: each operation works (✓),
doesn't (✗, why), or falls back (~, how).
"""
from __future__ import annotations

import time

from .teams_hub import OPS

CHECK_TITLE = "Aura wiring check — safe to delete"


def run(hub, project_id=None, existing_task: int | None = None) -> dict:
    """{"results": [(op, "ok"|"fail"|"fallback", detail)], "usable": bool,
    "made": [ids], "me": name}. existing_task: when the wiring can't create
    tasks, the check uses one the user named (it only comments on it)."""
    res: list[tuple[str, str, str]] = []
    made: list = []

    def ok(op, why=""):
        res.append((op, "ok", why))

    def fail(op, why):
        res.append((op, "fail", why))

    def fb(op):
        res.append((op, "fallback", OPS.get(op, (False, ""))[1]))

    me = ""
    try:
        me = hub.whoami()
        ok("whoami", f"signed in as {me}") if me else fail("whoami", "the hub gave no name")
    except Exception as e:                           # noqa: BLE001
        fail("whoami", str(e)[:160])
        return {"results": res, "usable": False, "made": made, "me": ""}

    task = None
    if hub.has("create_task"):
        try:
            pid = project_id
            if pid is None and hub.has("projects"):
                ps = hub.projects()
                pid = ps[0]["id"] if ps else None
            stamp = time.strftime("%Y-%m-%d %H:%M")
            t = hub.create_task(pid, f"{CHECK_TITLE} ({stamp})",
                                "Made by Aura to check how it talks to this board. It is closed "
                                "when the check ends; safe to delete.")
            if t and t.get("id"):
                made.append(t["id"])                 # Tracked from the moment it exists
                task = t["id"]
                ok("create_task", f"#{task}")
            else:
                fail("create_task", "the hub returned no task")
        except Exception as e:                       # noqa: BLE001
            fail("create_task", str(e)[:160])
    else:
        fb("create_task")
        task = existing_task
    if task is None:
        res.append(("get_task", "fail", "no task to check on — name one, or let the wiring create tasks"))
        return {"results": res, "usable": False, "made": made, "me": me}

    try:
        t = hub.get_task(task)
        good = str(t.get("id")) == str(task) and (existing_task or CHECK_TITLE in str(t.get("title")))
        ok("get_task", "read back") if good else fail("get_task", f"read back something else: {t.get('title')!r}")
    except Exception as e:                           # noqa: BLE001
        fail("get_task", str(e)[:160])

    marker = f"[AURA] wiring check {int(time.time())}"
    try:
        hub.add_comment(task, marker)
        ok("add_comment")
    except Exception as e:                           # noqa: BLE001
        fail("add_comment", str(e)[:160])
    try:
        cs = hub.comments(task)
        ok("comments", "the check's comment read back") if any(marker in c for c in cs) \
            else fail("comments", "the comment didn't come back")
    except Exception as e:                           # noqa: BLE001
        fail("comments", str(e)[:160])

    if hub.has("mark") and hub.has("labels"):
        try:
            hub.mark(task, "in progress")
            got = hub.labels(task)
            ok("mark") if "in progress" in got else fail("mark", f"labels read back: {got}")
        except Exception as e:                       # noqa: BLE001
            fail("mark", str(e)[:160])
        if hub.has("unmark"):
            try:
                hub.unmark(task, "in progress")
                got = hub.labels(task)
                ok("unmark") if "in progress" not in got else fail("unmark", "the label stayed")
            except Exception as e:                   # noqa: BLE001
                fail("unmark", str(e)[:160])
        else:
            fb("unmark")
    else:
        fb("mark")

    try:
        mine = hub.list_tasks(assignee=me, done=False)
        if not existing_task:
            ok("list_tasks", f"{len(mine)} open") if any(str(x.get("id")) == str(task) for x in mine) \
                else fail("list_tasks", "the check task isn't in your open tasks (assignee or filter wrong?)")
        else:
            ok("list_tasks", f"{len(mine)} open")
    except Exception as e:                           # noqa: BLE001
        fail("list_tasks", str(e)[:160])

    if not existing_task:                            # Never closes a real task
        try:
            cur = hub.get_task(task)
            hub.set_done(task, cur)
            ok("set_done", "closed, read back") if hub.get_task(task).get("done") \
                else fail("set_done", "it isn't done when read back")
        except Exception as e:                       # noqa: BLE001
            fail("set_done", str(e)[:160])
    else:
        res.append(("set_done", "fallback", "not tried on a real task — it would close it"))

    usable = all(s != "fail" for op, s, _ in res if OPS.get(op, (False, ""))[0])
    return {"results": res, "usable": usable, "made": made, "me": me}


def card_lines(result: dict) -> list[str]:
    """The check, for the card: one line per operation."""
    mark = {"ok": "✓", "fail": "✗", "fallback": "~"}
    out = []
    for op, s, why in result["results"]:
        out.append(f"{mark[s]} {op}" + (f" — {why}" if why else ""))
    if result.get("made"):
        out.append(f"made and closed: " + ", ".join(f"#{i}" for i in result["made"])
                   + " (titled 'safe to delete')")
    out.append("usable as the team's hub" if result["usable"]
               else "NOT usable yet — a required operation failed")
    return out

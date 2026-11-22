"""teams_check.py — prove a wiring on the live hub before anyone relies on it.

The check makes ONE task — "Aura wiring check — safe to delete" — in the
project the person picks, runs every operation on it (blocked-by against a
SECOND check task, a due date set and read back), reads each result back, and
closes what it made. Nothing else on the board is written. The result is a list
for the card: each operation works (✓), doesn't (✗, why), or falls back (~, how
— said in the open, e.g. blocked-by "not known on this hub"). With
read_only=True (adoption) only the read operations run against the teammate's
own token; nothing is ever written.
"""
from __future__ import annotations

import time

from .teams_hub import OPS

CHECK_TITLE = "Aura wiring check — safe to delete"


def _resolve_project(hub, project) -> int | None:
    """The check task goes in the project the person PICKED (step 1): an id as-is,
    a name looked up on the hub. None → the hub's default (the first project,
    as before).
    """
    if project is None or isinstance(project, int):
        return project
    s = str(project).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    try:
        ps = hub.projects() if hub.has("projects") else []
    except Exception:                                # noqa: BLE001
        ps = []
    hits = [p for p in ps if str(p.get("title", "")).lower() == s.lower()]
    if not hits:
        hits = [p for p in ps if s.lower() in str(p.get("title", "")).lower()]
    if len(hits) == 1:
        return hits[0].get("id")
    known = ", ".join(str(p.get("title", "?")) for p in ps) or "none"
    raise WiringCheckError(f"which project? the hub knows: {known}")


class WiringCheckError(ValueError):
    """A question for the user before the check can run (plain words)."""


def run(hub, project_id=None, existing_task: int | None = None,
        read_only: bool = False) -> dict:
    """{"results": [(op, "ok"|"fail"|"fallback", detail)], "usable": bool, "made":
    [ids], "me": name}. existing_task: when the wiring can't create tasks, the
    check uses one the user named (it only comments on it).

    project_id: the project the person picks for the check task — an id or a
    project name (resolved against the hub; ambiguous/unknown raises
    WiringCheckError so the tool layer asks, per).

    read_only (, adoption): a teammate's check of the team's published wiring
    with THEIR token — only the read-only operations run (the token may carry
    no write permissions); nothing is created, commented, labelled, closed or
    dated.
    """
    res: list[tuple[str, str, str]] = []
    made: list = []

    def ok(op, why=""):
        res.append((op, "ok", why))

    def fail(op, why):
        res.append((op, "fail", why))

    def fb(op, why=None):
        res.append((op, "fallback", OPS.get(op, (False, ""))[1] if why is None else why))

    me = ""
    try:
        me = hub.whoami()
        ok("whoami", f"signed in as {me}") if me else fail("whoami", "the hub gave no name")
    except Exception as e:                           # noqa: BLE001
        fail("whoami", str(e)[:160])
        return {"results": res, "usable": False, "made": made, "me": ""}

    pid = _resolve_project(hub, project_id)          # The project's choice
    stamp = time.strftime("%Y-%m-%d %H:%M")

    task = None
    if read_only:
        res.append(("create_task", "fallback",
                    "not written — the adoption check never writes"))
        task = existing_task
        if task is None:
            try:                                     # Read one real task instead
                any_open = hub.list_tasks(done=None)
                task = any_open[0]["id"] if any_open else None
            except Exception:                        # noqa: BLE001
                task = None
    elif hub.has("create_task"):
        try:
            if pid is None and hub.has("projects"):
                ps = hub.projects()
                pid = ps[0]["id"] if ps else None
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
        if existing_task or read_only:
            good = str(t.get("id")) == str(task)
        else:
            good = str(t.get("id")) == str(task) and CHECK_TITLE in str(t.get("title"))
        ok("get_task", "read back") if good else fail("get_task", f"read back something else: {t.get('title')!r}")
    except Exception as e:                           # noqa: BLE001
        fail("get_task", str(e)[:160])

    marker = f"[AURA] wiring check {int(time.time())}"
    if read_only:
        res.append(("add_comment", "fallback", "not written — the adoption check never writes"))
        try:
            cs = hub.comments(task)
            ok("comments", f"{len(cs)} read") if isinstance(cs, list) \
                else fail("comments", "the answer wasn't a list of comments")
        except Exception as e:                       # noqa: BLE001
            fail("comments", str(e)[:160])
    else:
        try:
            hub.add_comment(task, marker)
            ok("add_comment")
        except Exception as e:                       # noqa: BLE001
            fail("add_comment", str(e)[:160])
        try:
            cs = hub.comments(task)
            ok("comments", "the check's comment read back") if any(marker in c for c in cs) \
                else fail("comments", "the comment didn't come back")
        except Exception as e:                       # noqa: BLE001
            fail("comments", str(e)[:160])

    if read_only:
        res.append(("mark", "fallback", "not written — read-only"))
        res.append(("unmark", "fallback", "not written — read-only"))
    elif hub.has("mark") and hub.has("labels"):
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
        if not existing_task and not read_only:
            ok("list_tasks", f"{len(mine)} open") if any(str(x.get("id")) == str(task) for x in mine) \
                else fail("list_tasks", "the check task isn't in your open tasks (assignee or filter wrong?)")
        else:
            ok("list_tasks", f"{len(mine)} open")
    except Exception as e:                           # noqa: BLE001
        fail("list_tasks", str(e)[:160])

    # Blocked-by, against a SECOND check task (row 9: a hub without it says
    # 'not known on this hub' — a fallback said in the open)
    if hub.has("blocked_by"):
        second = None
        if read_only:
            second = task                            # Read-only: against the task read
        else:
            try:
                t2 = hub.create_task(pid, f"{CHECK_TITLE} ({stamp}) second",
                                     "The check's second task, for the blocked-by "
                                     "read; closed when the check ends. Safe to delete.")
                second = t2.get("id")
                if second:
                    made.append(second)
            except Exception as e:                   # noqa: BLE001
                fail("blocked_by", f"couldn't make the second check task: {str(e)[:120]}")
        if second is not None:
            try:
                rel = (hub.get_task(second).get("related_tasks") or {})
                if rel.get("blocked_error"):
                    fail("blocked_by", rel["blocked_error"])
                else:
                    bl = rel.get("blocked")
                    if isinstance(bl, list) and all(isinstance(x, dict) and "id" in x for x in bl):
                        ok("blocked_by", f"the blocked list reads back, against #{second}")
                    else:
                        fail("blocked_by", "the blocked list didn't come back "
                                           "in the contract shape")
            except Exception as e:                   # noqa: BLE001
                fail("blocked_by", str(e)[:160])
    else:
        res.append(("blocked_by", "fallback", "not known on this hub"))

    # Due — set a date on the check task, read it back (write side is skipped
    # in read-only; a hub without a due op says so in the open)
    runner = getattr(hub, "_run", None)
    if read_only:
        res.append(("due", "fallback", "not written — the adoption check never writes"))
    elif runner and "due" in (getattr(hub, "ops", {}) or {}):
        want = "2031-01-15"
        try:
            runner("due", task=task, due=want)
            got = str(hub.get_task(task).get("due_date") or "")[:10]
            ok("due", f"set {want}, read back") if got == want \
                else fail("due", f"read back {got or 'nothing'}")
        except Exception as e:                       # noqa: BLE001
            fail("due", str(e)[:160])
    else:
        res.append(("due", "fallback", "this hub's wiring has no due write — "
                                       "due dates are read-only here"))

    if read_only:
        res.append(("set_done", "fallback",
                    "not written — the adoption check never writes"))
    elif not existing_task:
        try:
            for made_id in made:                     # Clean up what it made
                cur = hub.get_task(made_id)
                hub.set_done(made_id, cur)
            back = hub.get_task(task)
            ok("set_done", "closed, read back") if back.get("done") \
                else fail("set_done", "it isn't done when read back")
        except Exception as e:                       # noqa: BLE001
            fail("set_done", str(e)[:160])
    else:
        res.append(("set_done", "fallback", "not tried on a real task — it would close it"))

    if read_only:
        # The adoption promise: what THIS token can do — the reads
        from .teams_wiring import READ_ONLY
        required_reads = [o for o, (req, _) in OPS.items() if req and o in READ_ONLY]
        usable = all(s != "fail" for op, s, _ in res if op in required_reads)
    else:
        usable = all(s != "fail" for op, s, _ in res if OPS.get(op, (False, ""))[0])
    # H-12's law: a reads-only check never certifies writes — the scope
    # rides the result so team_wire_use refuses to put it in use blind
    return {"results": res, "usable": usable, "made": made, "me": me,
            "scope": ("reads" if read_only else "full"),
            "writes_checked": not read_only}


def card_lines(result: dict) -> list[str]:
    """The check, for the card: one line per operation."""
    mark = {"ok": "✓", "fail": "✗", "fallback": "~"}
    out = []
    for op, s, why in result["results"]:
        out.append(f"{mark[s]} {op}" + (f" — {why}" if why else ""))
    if result.get("made"):
        out.append(f"made and closed: " + ", ".join(f"#{i}" for i in result["made"])
                   + " (titled 'safe to delete')")
    if result["usable"]:
        if result.get("scope") == "reads":
            out.append("reads check out — writes weren't tried (the team's "
                       "published version); team_wire_use stays off until a "
                       "check of your own draft runs the full way")
        else:
            out.append("usable as the team's hub")
    else:
        out.append("NOT usable yet — a required operation failed")
    return out

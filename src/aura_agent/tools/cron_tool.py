"""cron_tool.py — the model schedules work.

Aura: the same store as `aura cron` (cronops.py); creating or resuming a job
ASKS the user first (graded G3); at most MAX_JOBS active; a schedule Aura can't
read is refused (the old parser quietly made it hourly); scheduled runs can't
schedule (cron runs drop execute tools — surfaces.gate_mediator). Results: the
run's log, plus any gateway channel the user set up.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec

CRONJOB = "cronjob"
MAX_JOBS = 20
_IN = re.compile(r"\bin\s+(\d+)\s*(m|min|minutes?|h|hours?|d|days?)\b", re.I)


def schedule_of(text: str) -> tuple[dict | None, str]:
    """(schedule, the task text left) — None when the schedule isn't clear."""
    from ..cronops import _AT, _INTERVAL, parse_schedule
    t = (text or "").strip()
    m = _IN.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0].lower()
        secs = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        rest = (t[:m.start()] + " " + t[m.end():]).strip(" :-,")
        return {"kind": "once", "at_ts": round(time.time() + secs)}, rest
    if not (_INTERVAL.search(t) or _AT.search(t)):
        return None, t
    # "every day at 07:30" is a time of day — the old parser read "every day"
    # and dropped the 07:30
    every = _INTERVAL.search(t)
    at = _AT.search(t)
    if at and (not every or every.group(2).lower().startswith("day")) and not every_n_days(every):
        hh, mm = int(at.group(1)), int(at.group(2))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return {"kind": "daily", "hh": hh, "mm": mm}, t
    return parse_schedule(t)


def every_n_days(m) -> bool:
    """'every 3 days at 9:00' — more than one day apart (kept as an interval)."""
    return bool(m and m.group(1) and int(m.group(1)) > 1)


def _show(j: dict) -> str:
    s = j.get("schedule", {})
    when = (f"every {int(s['seconds']) // 60} min" if s.get("kind") == "interval"
            else f"daily at {s['hh']:02d}:{s['mm']:02d}" if s.get("kind") == "daily"
            else time.strftime("once at %Y-%m-%d %H:%M", time.localtime(s.get("at_ts", 0))))
    last = j.get("last_status") or "not run yet"
    return f"{j['id']}  [{j.get('status', 'active')}]  {when}  — {j['task'][:80]}  ({last})"


def register(mediator: Mediator, dispatch: dict, eng: Path) -> None:
    from .. import cronops as C

    def cronjob(args) -> str:
        action = str(args.get("action") or "list").lower()
        jobs = C.load_jobs(eng)
        if action == "list":
            return "\n".join(_show(j) for j in jobs) or "no scheduled jobs in this project"
        if action == "create":
            task = str(args.get("task") or "").strip()
            sched, _rest = schedule_of(str(args.get("schedule") or ""))
            if not task:
                return "error: give the task to run"
            if sched is None:
                return ("error: schedule not understood — use 'every 2 hours', 'every day', "
                        "'at 09:30' (daily) or 'in 30 minutes' (once)")
            if sum(1 for j in jobs if j.get("status", "active") == "active") >= MAX_JOBS:
                return f"error: {MAX_JOBS} jobs are active already — remove or pause one first"
            j = C.add_job(eng, task, sched, job_id=f"job-{int(time.time() * 1000) % 10**10}")
            return ("scheduled: " + _show(j) + "\nIt runs while `aura cron watch` is "
                    "running (start it once; the system's scheduler can keep it on).")
        jid = str(args.get("id") or "")
        if not any(j.get("id") == jid for j in jobs):
            return f"error: no job {jid!r} — action list shows them"
        if action == "remove":
            C.remove_job(eng, jid)
            return f"removed {jid}"
        if action in ("pause", "resume"):
            for j in jobs:
                if j.get("id") == jid:
                    j["status"] = "paused" if action == "pause" else "active"
            C.save_jobs(eng, jobs)
            return f"{jid} {'paused' if action == 'pause' else 'active again'}"
        return "error: action is create, list, pause, resume or remove"

    mediator.register(ToolSpec(CRONJOB, BlastRadius.EXECUTE, lambda a: None))
    dispatch[CRONJOB] = cronjob

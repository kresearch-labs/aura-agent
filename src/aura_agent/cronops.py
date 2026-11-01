"""cronops.py — local cron + hooks (zero infra, engagement-native).

Cron: a jsonl job store in the engagement root (engagement/cron.jsonl). Jobs
run UNATTENDED through the SAME harness — a cron run is a headless chat turn
(build_agent → loop.run against a fresh session in the job's own delegate-style
dir). Delivery: terminal log first (messaging gateways are adapter territory).
One-shot (`at`) and interval jobs; `aura cron list|add|run|remove`.

Hooks: user scripts wired at mediator audit points — tool_start / tool_done /
compact. A hook is an executable script receiving a JSON event on stdin; exit
code 2 BLOCKS the call (Violation), exit 0 allows, stdout of hook_tool_done can
MUTATE the recorded result note (append-only audit note, never the tool's
return value).

Natural language: `aura cron add "every morning: sweep the output directory"` —
parsed deterministically (every N minutes/hours/days, at HH:MM, weekday names);
anything unparsed stays in the task text.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

CRON_FILE = "cron.jsonl"
HOOK_EVENTS = ("tool_start", "tool_done", "compact")


# ------------------------------------------------------------------ store --
def _cron_path(eng: Path) -> Path:
    return Path(eng) / CRON_FILE


_OTHER_FOLDERS = "cron_folders.json"


def _remember_folder(eng: Path) -> None:
    """Old single-folder projects keep their notes in the project itself,
    outside the profile's projects/ folder, so `cron watch` can't find them
    by scanning. Remember any such folder that has jobs (never raises)."""
    try:
        from .paths import no_project_dir, profile_dir
        f = Path(eng).resolve()
        if f == no_project_dir().resolve():
            return
        try:
            f.relative_to((profile_dir() / "projects").resolve())
            return                        # Found by the normal scan
        except ValueError:
            pass
        reg = profile_dir() / _OTHER_FOLDERS
        known = json.loads(reg.read_text(encoding="utf-8"))             if reg.exists() else []
        if str(f) not in known:
            reg.parent.mkdir(parents=True, exist_ok=True)
            reg.write_text(json.dumps(known + [str(f)], indent=1),
                           encoding="utf-8")
    except (OSError, ValueError, TypeError) as _e:
        from .eventlog import log_swallowed
        log_swallowed("cron.remember_folder", _e)


def load_jobs(eng: Path) -> list[dict]:
    p = _cron_path(eng)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if out:
        _remember_folder(eng)        # Jobs made before folders were remembered
    return out


def save_jobs(eng: Path, jobs: list[dict]) -> None:
    p = _cron_path(eng)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j, ensure_ascii=False, sort_keys=True) + "\n")


def add_job(eng: Path, task: str, schedule: dict,
            job_id: str | None = None) -> dict:
    """schedule = {"kind": "interval", "seconds": N} |
    {"kind": "daily", "hh": H, "mm": M} | {"kind": "once", "at_ts": T}"""
    job = {"id": job_id or f"job-{int(time.time())}",
           "task": task, "schedule": schedule,
           "created_ts": round(time.time(), 3),
           "last_run_ts": None, "runs": 0, "status": "active"}
    jobs = load_jobs(eng)
    jobs.append(job)
    save_jobs(eng, jobs)
    _remember_folder(eng)
    return job


def remove_job(eng: Path, job_id: str) -> bool:
    jobs = load_jobs(eng)
    kept = [j for j in jobs if j.get("id") != job_id]
    if len(kept) == len(jobs):
        return False
    save_jobs(eng, kept)
    return True


def get_job(eng: Path, job_id: str) -> dict | None:
    for j in load_jobs(eng):
        if j.get("id") == job_id:
            return j
    return None


def mark_run(eng: Path, job_id: str, ok: bool, note: str = "") -> None:
    jobs = load_jobs(eng)
    for j in jobs:
        if j.get("id") == job_id:
            j["last_run_ts"] = round(time.time(), 3)
            j["runs"] = int(j.get("runs", 0)) + 1
            j["last_status"] = "ok" if ok else "failed"
            if note:
                j["last_note"] = note[:200]
    save_jobs(eng, jobs)


# ----------------------------------------------------- natural language ----
_INTERVAL = re.compile(
    r"every\s+(\d+)?\s*(minute|min|hour|hr|day|week)s?", re.I)
_AT = re.compile(r"at\s+(\d{1,2}):(\d{2})", re.I)


def parse_schedule(text: str) -> tuple[dict, str]:
    """Deterministic NL → schedule. Returns (schedule, remaining_text).
    Unparsed text stays in the task — nothing silently dropped."""
    t = text.strip()
    m = _INTERVAL.search(t)
    if m:
        n = int(m.group(1) or 1)
        unit = m.group(2).lower()
        seconds = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600,
                   "day": 86400, "week": 604800}[unit] * n
        remaining = (t[:m.start()] + " " + t[m.end():]).strip(" :-,")
        return {"kind": "interval", "seconds": seconds}, remaining or t
    m = _AT.search(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh < 24 and 0 <= mm < 60:
            remaining = (t[:m.start()] + " " + t[m.end():]).strip(" :-,")
            return {"kind": "daily", "hh": hh, "mm": mm}, remaining or t
    return {"kind": "interval", "seconds": 3600}, t     # Fallback: hourly


def due(job: dict, now: float | None = None) -> bool:
    """Is this job due to run? (pure predicate, clock injectable)"""
    now = now if now is not None else time.time()
    if job.get("status") == "paused":
        return False
    s = job.get("schedule", {})
    last = job.get("last_run_ts")
    if s.get("kind") == "interval":
        return last is None or (now - last) >= float(s["seconds"])
    if s.get("kind") == "daily":
        if last is None:
            return True
        # due again when the wall clock has passed the hh:mm since last run
        lt = time.localtime(last)
        nt = time.localtime(now)
        job_min = s["hh"] * 60 + s["mm"]
        last_min = lt.tm_hour * 60 + lt.tm_min
        now_min = nt.tm_hour * 60 + nt.tm_min
        if nt.tm_yday != lt.tm_yday:
            return True
        return last_min < job_min <= now_min
    if s.get("kind") == "once":
        return last is None and now >= float(s.get("at_ts", 0))
    return False


def next_run_ts(job: dict, now: float | None = None) -> float | None:
    """When the job runs next (None: paused, or a one-time job already run)."""
    now = now if now is not None else time.time()
    if job.get("status") == "paused":
        return None
    s, last = job.get("schedule", {}), job.get("last_run_ts")
    if s.get("kind") == "interval":
        return now if last is None else float(last) + float(s["seconds"])
    if s.get("kind") == "daily":
        lt = time.localtime(now)
        today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, s["hh"], s["mm"], 0, 0, 0, -1))
        ran_today = last is not None and time.localtime(last).tm_yday == lt.tm_yday \
            and time.localtime(last).tm_hour * 60 + time.localtime(last).tm_min >= s["hh"] * 60 + s["mm"]
        return today if (today >= now and not ran_today) else today + 86400
    if s.get("kind") == "once":
        return None if last is not None else float(s.get("at_ts", now))
    return None


def schedule_words(s: dict) -> str:
    """A schedule as a person would say it."""
    if s.get("kind") == "interval":
        sec = int(s.get("seconds", 0))
        for n, w in ((604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
            if sec % n == 0:
                k = sec // n
                return f"every {w}" if k == 1 else f"every {k} {w}s"
        return f"every {sec} s"
    if s.get("kind") == "daily":
        return f"every day at {s['hh']:02d}:{s['mm']:02d}"
    if s.get("kind") == "once":
        return "once, " + time.strftime("%a %d %b %H:%M", time.localtime(float(s.get("at_ts", 0))))
    return str(s)


def when_words(ts: float | None, now: float | None = None) -> str:
    """'in 3h 10m' · 'now' · 'overdue 25m' for a next-run time."""
    if ts is None:
        return "—"
    d = ts - (now if now is not None else time.time())
    late = d < -60
    m = int(abs(d) // 60)
    span = (f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else
            f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m")
    if abs(d) <= 60:
        return "now"
    return f"overdue {span}" if late else f"in {span}"


# --------------------------------------------------------------- the run --
def run_job(eng: Path, job_id: str, profile_name: str | None = None,
            max_steps: int | None = None, profile=None) -> dict:
    """Headless run: the SAME harness (build_agent → fresh session →
    loop.run). Terminal log IS the delivery (generic-first). `profile`
    injects a resolved ProviderProfile (tests); profile_name resolves
    from the profile store (operators)."""
    from .cli import build_agent
    from .sessions import new_session

    job = get_job(eng, job_id)
    if job is None:
        return {"ok": False, "error": f"unknown job {job_id}"}
    if profile is not None:
        built = build_agent(eng, profile_name)
        profile = profile if profile else built[0]
        profile_obj, m, loop = profile, built[1], built[2]
        # rebuild the loop's LLM with the injected profile
        from .llm import LLMClient
        loop.llm = LLMClient(profile_obj)
    else:
        profile_obj, m, loop = build_agent(eng, profile_name)
    from .surfaces import gate_mediator
    gate_mediator(m, "cron")
    # Nobody is at the keyboard: a call that needs approval stops the job and
    # waits for `aura approve|reject <id>`
    from .approvals import waiting_approver
    m.approver = waiting_approver(eng)
    session = new_session(eng / "sessions", f"cron-{job_id}")
    state: dict = {}
    rec = loop.run(session, job["task"],
                   kill_check=lambda: state.get("killed", False))
    # A run that ended cleanly but without the files its task names is not "ok"
    missing = list(getattr(rec, "gate_missing", None) or [])
    ok = rec.error is None and not missing
    _why = rec.error or (f"missing deliverables: {', '.join(missing)}"
                         if missing else "")
    mark_run(eng, job_id, ok, _why or rec.content or "")
    # 3.11: deliver the result to any configured gateway channels
    try:
        from .gateway import _config, deliver_message
        gw_cfg = _config(eng)
        summary = (_why or rec.content or "")[:1500]
        for ch in ("telegram", "discord", "slack", "webhook"):
            if gw_cfg.get(ch):
                deliver_message(f"cron {job_id}: "
                                f"{'ok' if ok else 'FAILED'}\n{summary}",
                                ch, eng=eng)
    except Exception:
        pass  # Delivery is best-effort; the run result is primary
    return {"ok": ok, "job_id": job_id, "steps": rec.step,
            "summary": (_why or rec.content or "")[:400],
            "session": str(session.path)}


def run_due(eng: Path, profile_name: str | None = None,
            profile=None) -> list[dict]:
    """Scheduler tick: run every active due job. Returns per-job results."""
    results = []
    for j in load_jobs(eng):
        if j.get("status") != "active" or not due(j):
            continue
        results.append(run_job(eng, j["id"], profile_name, profile=profile))
    return results


def cron_notebooks() -> list[Path]:
    """Every notebook of the active profile that has scheduled jobs — the
    profile's projects plus the no-project notebook."""
    from .paths import no_project_dir, profile_dir
    out = [p for p in sorted((profile_dir() / "projects").glob("*"))
           if (p / CRON_FILE).exists()]
    if (no_project_dir() / CRON_FILE).exists():
        out.append(no_project_dir())
    # old single-folder projects, remembered when their jobs were made
    try:
        reg = profile_dir() / _OTHER_FOLDERS
        for f in (json.loads(reg.read_text(encoding="utf-8"))
                  if reg.exists() else []):
            p = Path(f)
            if (p / CRON_FILE).exists() and p not in out:
                out.append(p)
    except (OSError, ValueError, TypeError) as _e:
        from .eventlog import log_swallowed
        log_swallowed("cron.other_folders", _e)
    return out


def _beat_file() -> Path:
    from .paths import profile_dir
    return profile_dir() / "cron_watch.alive"


def beat() -> None:
    """The watcher says it's alive (setup's background check reads it)."""
    try:
        p = _beat_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": time.time(), "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def watcher_alive(within_s: float = 180.0) -> dict | None:
    """The last heartbeat if a watcher wrote one recently, else None."""
    try:
        d = json.loads(_beat_file().read_text(encoding="utf-8"))
        return d if time.time() - float(d.get("at", 0)) <= within_s else None
    except (OSError, ValueError):
        return None


def watch(on_result, tick_s: float = 60.0, once: bool = False) -> int:
    """The scheduler: every tick, run the due jobs of every project of the
    profile. Nothing else runs scheduled jobs — this (or an OS scheduler
    calling `aura cron run`) has to be running. Returns jobs run."""
    n = 0
    while True:
        beat()
        for nb in cron_notebooks():
            for r in run_due(nb):
                n += 1
                on_result(nb, r)
        if once:
            return n
        time.sleep(tick_s)


# ------------------------------------------------------------------ hooks --
def _hook_dirs(eng: Path) -> list[Path]:
    """Project hooks dir shadows the profile's (profiles/<name>/hooks)."""
    from .paths import profile_path
    dirs = [profile_path("hooks")]
    dirs.append(Path(eng) / "hooks")
    return [d for d in dirs if d.is_dir()]


def run_hooks(eng: Path, event: str, payload: dict) -> dict:
    """Fire user scripts for an event. Event payload → stdin JSON.
    Exit 0 = allow; exit 2 = BLOCK (caller turns it into a Violation);
    any other exit = logged, non-blocking (a broken hook must not kill
    the loop). stdout of tool_done hooks can append an audit note.
    Returns {ran, blocked, note}."""
    if event not in HOOK_EVENTS:
        return {"ran": 0, "blocked": False, "note": None, "reason": None}
    body = json.dumps({"event": event, **payload}, ensure_ascii=False,
                      default=str).encode()
    ran = 0
    blocked = False
    note = None
    reason = None
    for d in _hook_dirs(eng):
        for script in sorted(d.glob(f"{event}.*")):
            cmd = hook_command(script)
            if cmd is None:
                continue
            ran += 1
            try:
                r = subprocess.run(cmd, input=body, capture_output=True,
                                   timeout=10, cwd=str(eng))
            except Exception:
                continue                      # Broken hook ≠ loop death
            if r.returncode == 2:
                blocked = True
                # what the hook said is why — the model is told
                said = (r.stderr or r.stdout or b"").decode(
                    "utf-8", errors="replace").strip()
                reason = reason or (said[:300] or f"{script.name} exit 2")
            elif event == "tool_done" and r.returncode == 0 and r.stdout:
                try:
                    note = json.loads(r.stdout.decode()).get("note")
                except Exception:
                    note = r.stdout.decode("utf-8", errors="replace")[:200]
    return {"ran": ran, "blocked": blocked, "note": note, "reason": reason}


def hook_command(script: Path) -> list[str] | None:
    """2.9: how to run a hook script on this OS — by its type, so a .py or
    .ps1 hook works on Windows too (running the file directly only works
    for POSIX executables). None = not runnable here."""
    import shutil
    import sys
    if not script.is_file():
        return None
    ext = script.suffix.lower()
    if ext == ".py":
        return [sys.executable, str(script)]
    if ext == ".ps1":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(script)] if ps else None
    if ext in (".cmd", ".bat"):
        return ["cmd", "/c", str(script)] if os.name == "nt" else None
    if ext == ".sh":
        sh = shutil.which("bash") or shutil.which("sh")
        return [sh, str(script)] if sh else None
    if ext == ".exe" or (os.name != "nt" and os_access(script)):
        return [str(script)]
    return None


def os_access(script: Path) -> bool:
    return os.access(script, os.X_OK)


# ------------------------------------------------- mediator hook points --
def register_hook_points(mediator, eng: Path) -> None:
    """2.9: hooks now live inside Mediator.execute (the one door) — this just
    switches them on for a workspace.
    """
    mediator.hooks_root = Path(eng)

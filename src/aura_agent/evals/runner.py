"""runner.py — run, validate and compare the eval suite.

  check_suite()            no model: every checker fails on the starting
                           files and passes on the reference solution
  run_task / run_suite     the agent on each task, in a fresh temporary
                           workspace, with whatever profile is configured
                           (or an injected LLM — the selftest uses a
                           scripted one); headless, so anything that needs
                           approval is refused, never waited for
  compare(a, b)            what got better or worse between two results

Results are JSON: suite, model, active /features, Aura version, and per
task: passed, steps, tokens, seconds, receipt status, proof status, why.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .suite import SUITE, TASKS

CHECK_TIMEOUT_S = 60


def _write_files(root: Path, files: dict) -> None:
    for rel, content in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")


def run_check(task: dict, root: Path) -> tuple[bool, str]:
    """Run the task's checker in `root`. (passed, last lines of output)."""
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "check.py"
        script.write_text("import sys\nsys.path.insert(0, '.')\n"
                          + task["check"], encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(root),
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            r = subprocess.run([sys.executable, str(script)], cwd=str(root),
                               capture_output=True, text=True, env=env,
                               timeout=CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"checker timed out after {CHECK_TIMEOUT_S}s"
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        # the reason is the last line (e.g. "AssertionError: add(2, 3) should
        # be 5"), not the traceback's first lines
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return False, (lines[-1] if lines else f"exit {r.returncode}")[-400:]
    return True, out[-400:]


def select(ids: list[str] | None = None) -> list[dict]:
    if not ids:
        return list(TASKS)
    want = set(ids)
    return [t for t in TASKS if t["id"] in want or t["category"] in want]


# ------------------------------------------------------- suite validation --
def check_suite(tasks: list[dict] | None = None) -> list[dict]:
    """Prove each checker is right without any model: it must FAIL on the
    starting files (the task isn't already solved) and PASS on the
    reference solution."""
    rows = []
    for t in tasks or TASKS:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_files(root, t["files"])
            before, why_before = run_check(t, root)
            _write_files(root, t["solution"])
            after, why_after = run_check(t, root)
        ok = (not before) and after
        rows.append({"id": t["id"], "ok": ok,
                     "fails_unsolved": not before, "passes_solved": after,
                     "why": "" if ok else (
                         "checker passes on the unsolved files" if before
                         else f"reference solution fails: {why_after[-200:]}")})
    return rows


# ---------------------------------------------------------------- running --
def run_task(task: dict, profile_name: str | None = None, llm=None,
             max_steps: int = 30, time_limit_s: float = 600.0,
             keep: bool = False) -> dict:
    """The agent on one task in a fresh workspace. Never raises."""
    from ..cli import build_agent
    from ..engagement import init_engagement
    from ..sessions import new_session
    base = Path(tempfile.mkdtemp(prefix=f"aura-eval-{task['id']}-"))
    root = base / "ws"
    t0 = time.monotonic()
    res = {"id": task["id"], "category": task["category"], "passed": False,
           "steps": 0, "tokens": 0, "seconds": 0.0, "receipt": None,
           "proof": None, "why": "", "prompt": None, "cache_mode": None,
           "cache_hits": 0, "cache_misses": 0}
    try:
        init_engagement(root, f"eval-{task['id']}")
        _write_files(root, task["files"])
        _p, m, loop = build_agent(root, profile_name)
        m.approver = None                  # Headless: approvals are refused
        loop.cfg.max_steps = max_steps
        if llm is not None:
            loop.llm = llm
        # the model-specific setup this score belongs to
        res["prompt"] = getattr(loop, "prompt_source", None)
        res["cache_mode"] = getattr(loop, "cache_mode", None)
        deadline = time.monotonic() + time_limit_s
        rec = loop.run(new_session(root / "sessions", task["id"]),
                       task["prompt"], work_dir=root,
                       kill_check=lambda: time.monotonic() > deadline)
        res["steps"] = getattr(rec, "step", 0) or 0
        res["tokens"] = int(getattr(m.budgets, "tokens_spent", 0) or 0)
        res["receipt"] = (getattr(rec, "receipt", None) or {}).get("status")
        res["proof"] = (getattr(rec, "proof", None) or {}).get("status")
        res["cache_hits"] = int(getattr(loop, "cache_hits", 0) or 0)
        res["cache_misses"] = int(getattr(loop, "cache_misses", 0) or 0)
        if getattr(rec, "error", None):
            res["why"] = f"run ended: {rec.error}"
        passed, out = run_check(task, root)
        res["passed"] = passed
        if not passed:
            res["why"] = (res["why"] + " | " if res["why"] else "") + out[-300:]
    except Exception as e:                           # noqa: BLE001
        res["why"] = f"harness error: {type(e).__name__}: {e}"
    finally:
        res["seconds"] = round(time.monotonic() - t0, 1)
        if keep:
            res["workspace"] = str(root)
        else:
            shutil.rmtree(base, ignore_errors=True)
    return res


def run_suite(tasks: list[dict], profile_name: str | None = None,
              llm_factory=None, on_result=None, **kw) -> dict:
    """Run tasks in order; returns the results document."""
    from .. import __version__
    from ..features import active
    model = None
    try:
        from ..config import resolve
        model = getattr(resolve(profile_name), "model", None)
    except Exception:                                # noqa: BLE001
        pass
    doc = {"suite": SUITE, "aura_version": __version__,
           "profile": profile_name, "model": model,
           "features": sorted(active()),
           "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": []}
    for t in tasks:
        r = run_task(t, profile_name,
                     llm=llm_factory(t) if llm_factory else None, **kw)
        doc["tasks"].append(r)
        if on_result:
            on_result(r)
        # no model to talk to: every other task would fail the same way
        if "unreachable" in r["why"] or "harness error: RuntimeError" in r["why"]:
            doc["aborted"] = ("the model could not be reached — set one up "
                              "(aura setup / --profile) and run again")
            break
    n = len(doc["tasks"])
    first = doc["tasks"][0] if doc["tasks"] else {}
    doc["prompt"] = first.get("prompt")
    doc["cache_mode"] = first.get("cache_mode")
    hits = sum(r.get("cache_hits", 0) for r in doc["tasks"])
    misses = sum(r.get("cache_misses", 0) for r in doc["tasks"])
    doc["cache_hit_ratio"] = round(hits / (hits + misses), 3)         if hits + misses else None
    doc["passed"] = sum(1 for r in doc["tasks"] if r["passed"])
    doc["score"] = round(doc["passed"] / n, 3) if n else 0.0
    doc["tokens"] = sum(r["tokens"] for r in doc["tasks"])
    return doc


# -------------------------------------------------------------- compare --
def compare(a: dict, b: dict) -> list[str]:
    """Lines describing what changed from results `a` to results `b`."""
    ra = {r["id"]: r for r in a.get("tasks", [])}
    rb = {r["id"]: r for r in b.get("tasks", [])}
    lines = [f"score {a.get('passed', 0)}/{len(ra)} → "
             f"{b.get('passed', 0)}/{len(rb)} · tokens {a.get('tokens', 0):,} "
             f"→ {b.get('tokens', 0):,}"]
    for key, label in (("model", "model"), ("prompt", "prompt"),
                       ("features", "features"), ("cache_mode", "cache"),
                       ("cache_hit_ratio", "cache hits")):
        if a.get(key) != b.get(key):
            lines.append(f"  {label}: {a.get(key)} → {b.get(key)}")
    for tid in sorted(set(ra) | set(rb)):
        pa = ra.get(tid, {}).get("passed")
        pb = rb.get(tid, {}).get("passed")
        if pa == pb:
            continue
        mark = ("FIXED " if pb else "BROKE ") if pa is not None and pb is not None \
            else ("NEW   " if pa is None else "GONE  ")
        lines.append(f"  {mark} {tid}" + (f" — {rb[tid]['why'][:100]}"
                                         if tid in rb and not pb else ""))
    if len(lines) == 1:
        lines.append("  no task changed result")
    return lines

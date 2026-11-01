"""referee.py — #3+#6: independent completion verification.

The SEAL finding: self-authored verification is a closed loop with no ground
truth — all 35 self-tested agent runs scored >0.70 on their own tests while 15
deployed below baseline. The Transluce taxonomy: agents overstate completion
(skipped tests reported green, fabricated artifacts committed).

The referee is the outside ground truth:
  - runs AFTER the loop returns, from the StepRecord + work_dir;
  - re-checks every named deliverable ON DISK (presence + non-empty +
    claimed-content marker where derivable);
  - for code tasks, executes the named scripts fresh and records exit codes;
  - produces a SCORECARD (present-works / present-broken / missing) that
    the final report must carry — the agent's "done" is a claim ABOUT the
    scorecard, never a substitute for it.

The referee consumes: the task text, TASK.md deliverable names, and the
work_dir. It cannot be edited by the agent mid-run (module + file live outside
the loop's write path; verdict written post-return).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Verdict:
    name: str
    status: str                  # Present-works | present-broken | missing | present
    detail: str = ""


@dataclass
class Scorecard:
    task: str
    checked_at: float = field(default_factory=time.time)
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        n = {"present-works": 0, "present-broken": 0, "missing": 0,
             "present": 0}
        for v in self.verdicts:
            n[v.status] = n.get(v.status, 0) + 1
        return (f"{n['present-works']} ok · {n['present-broken']} broken · "
                f"{n['missing']} missing · {n['present']} present"
                f" ({len(self.verdicts)} checked)")

    @property
    def all_good(self) -> bool:
        return bool(self.verdicts) and all(
            v.status in ("present-works", "present") for v in self.verdicts)

    def render(self) -> str:
        lines = [f"[COMPLETION SCORECARD — independent check, {self.summary}]"]
        for v in self.verdicts:
            mark = {"present-works": "✔", "present": "○",
                    "present-broken": "✗", "missing": "∅"}.get(v.status, "?")
            lines.append(f"  {mark} {v.name}: {v.status}"
                         + (f" — {v.detail[:70]}" if v.detail else ""))
        return "\n".join(lines)


def _empty_ok(p: Path) -> bool:
    try:
        return p.stat().st_size > 0
    except OSError:
        return False


def _has_content_marker(p: Path) -> str | None:
    """Derivable broken-ness: file that parses as JSON but is empty object,
    or a .py that fails ast.parse. Returns reason or None."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    if not text.strip():
        return "empty file"
    if p.suffix == ".json":
        try:
            json.loads(text)
        except ValueError:
            return "invalid JSON"
    if p.suffix == ".py":
        try:
            compile(text, str(p), "exec")
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} (line {e.lineno})"
    return None


def check_task(work_dir: Path, task: str, task_md: Path | None = None,
               run_scripts: bool = True) -> Scorecard:
    """Score every named deliverable. Names come from the task text and
    TASK.md write-verb lines (same parser as the deliverable gate, so the
    referee checks exactly what the gate demands)."""
    from .symbols import SymbolTable  # noqa: F401  (ensures module importable)
    from .engine.loop import (_parse_deliverable_names,
                              _task_deliverable_names)
    names = list(_parse_deliverable_names(task or ""))
    if task_md and Path(task_md).exists():
        try:
            names += _task_deliverable_names(
                Path(task_md).read_text(encoding="utf-8"))
        except OSError:
            pass
    names = list(dict.fromkeys(names))
    sc = Scorecard(task=task or "")
    wd = Path(work_dir)
    for name in names:
        p = wd / name
        if not p.is_file():
            sc.verdicts.append(Verdict(name, "missing"))
            continue
        if not _empty_ok(p):
            sc.verdicts.append(Verdict(name, "present-broken", "empty file"))
            continue
        broken = _has_content_marker(p)
        if broken:
            sc.verdicts.append(Verdict(name, "present-broken", broken))
            continue
        # script deliverables: run them fresh (best-effort, timeout-bounded)
        if run_scripts and p.suffix == ".py" and p.stat().st_size > 10:
            try:
                # the interpreter running Aura — a bare "python3" is the
                # Microsoft Store stub on many Windows machines and made
                # every working script look broken
                r = subprocess.run(
                    [sys.executable or "python", str(p.name)], cwd=wd,
                    capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    sc.verdicts.append(Verdict(
                        name, "present-broken",
                        f"exit {r.returncode}: {r.stderr.strip()[-60:]}"))
                    continue
                sc.verdicts.append(Verdict(
                    name, "present-works", f"ran clean ({r.stdout.strip()[:30]})"))
                continue
            except subprocess.TimeoutExpired:
                sc.verdicts.append(Verdict(name, "present-broken", "timeout"))
                continue
            except OSError:
                pass
        sc.verdicts.append(Verdict(name, "present"))
    return sc

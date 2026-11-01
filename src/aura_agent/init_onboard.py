"""onboard.py — Finish-what-you-started engagement onboarding.

`aura init` today prints "fill scope.json" and exits — the stranger is dropped
with no next step (the exact gap from the 10-doc study: every market wizard
ENDS in a verified working state).

This module completes the init flow:
  1. create the engagement skeleton (existing init_engagement)
  2. interactive scope walk: objective + hosts/cidrs/domains with sensible
     "empty scope = localhost-only lab" default (safe default > lecture)
  3. write scope.json + a filled objective into state.md
  4. verify: scope parses, mediator can be constructed, endpoint reachable
  5. print the exact next command with the engagement dir prefilled
"""
from __future__ import annotations

import json
from pathlib import Path

from .engagement import init_engagement


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def _ask_list(prompt: str, default: str) -> list[str]:
    raw = _ask(prompt, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


def verify_engagement(root: Path) -> tuple[bool, str]:
    """Scope parses + mediator constructible with it. Never raises."""
    from .mediator import Scope
    try:
        data = json.loads((Path(root) / "scope.json").read_text(
            encoding="utf-8"))
        scope = Scope.from_dict(data)
    except Exception as e:
        return False, f"scope.json invalid: {e}"
    try:
        from .mediator import Mediator, Budgets
        root = Path(root)
        Mediator(scope, Budgets(), root / "audit.jsonl", root)
    except Exception as e:
        return False, f"mediator construction failed: {e}"
    return True, "scope parses; mediator constructs"


def run_onboard(name: str | None, dir_hint: str | None = None,
                assume_yes: bool = False) -> int:
    """Set up THIS project (the folder you are in, or --dir): its notebook
    gets the scope, objective and state. Aura writes nothing into the
    project itself."""
    from .cli import engagement_dir
    from .paths import workspace_for
    root = engagement_dir(dir_hint)
    project = workspace_for(root)
    name = name or project.name
    root = init_engagement(root, name)
    scope_p = root / "scope.json"
    marker = root / ".onboarded"
    # Idempotency: init_engagement never overwrites existing files, and we
    # complete onboarding exactly once — re-init over a finished engagement
    # verifies it and keeps every user value.
    onboarded = marker.exists()
    print(f"\n[init] project: {project}")
    if root != project:
        print(f"       Aura's notes for it: {root}")
    next_steps = '  aura chat      (or: aura run "<task>")\n'
    if onboarded:
        ok, msg = verify_engagement(root)
        print(f"  {'✓' if ok else '✗'} already set up: {msg}")
        print("\nReady (existing settings kept). Next, in that folder:")
        print(next_steps)
        return 0 if ok else 1

    if assume_yes:
        scope = json.loads(scope_p.read_text(encoding="utf-8"))
        # keep anything already allowed (aura scope add before init)
        scope.update(engagement=name,
                     notes="set up with aura init --yes — "
                           "aura scope add <site> allows more")
        scope_p.write_text(json.dumps(scope, indent=2), encoding="utf-8")
        ok, msg = verify_engagement(root)
        print(f"  {'✓' if ok else '✗'} {msg}")
        if not ok:
            return 1
        marker.write_text("onboarded\n", encoding="utf-8")
        print("\nReady. Next, in that folder:")
        print(next_steps)
        return 0

    print("Files are always limited to this project. Now: what may the")
    print("agent reach on the network? Anything not listed is refused.")
    print("Blank is fine — add more later with /scope add <site>.")

    objective = _ask("What is this project for? (one line, optional)", "")
    domains = _ask_list("Websites it may read (e.g. docs.python.org, "
                        "comma-separated)", "")
    hosts = _ask_list("Other hosts or IPs (blank for none)", "")
    cidrs = _ask_list("IP ranges, e.g. 10.0.0.0/24 (blank for none)", "")

    scope = json.loads(scope_p.read_text(encoding="utf-8"))
    # added to what is already allowed, never replacing it
    for _k, _new in (("hosts", hosts), ("cidrs", cidrs), ("domains", domains)):
        scope[_k] = list(dict.fromkeys(list(scope.get(_k) or []) + _new))
    scope.update(engagement=name, notes=objective_note(objective))
    scope_p.write_text(json.dumps(scope, indent=2), encoding="utf-8")

    # objective also lands in state.md so the loop sees it on step 1
    state_p = root / "state.md"
    if state_p.exists() and objective:
        txt = state_p.read_text(encoding="utf-8")
        txt = txt.replace("(fill: what does done look like?)",
                          objective, 1)
        state_p.write_text(txt, encoding="utf-8")

    ok, msg = verify_engagement(root)
    print(f"\n  {'✓' if ok else '✗'} {msg}")
    if not ok:
        print("  something in the network list is wrong — check it with "
              "aura scope, fix with aura scope remove / add")
        return 1
    marker.write_text("onboarded\n", encoding="utf-8")

    print("\nReady. Next, in that folder:")
    print(next_steps)
    return 0


def objective_note(objective: str) -> str:
    return f"set via aura init: {objective}"

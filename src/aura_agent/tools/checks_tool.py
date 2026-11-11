"""tools/checks_tool.py — 2.5: run the project's own checks.

run_checks(kind) runs the project's test / lint / typecheck command —
detected from its config files, or the operator's `.aura/checks.json`
(project_checks) — so the model never has to guess the command, and the
interpreter running Aura is used instead of a bare `python3`.

The command itself goes through the mediator as run_command: assist-level
approval, sandbox, audit and skill-pack limits all apply to the REAL
command. This wrapper is registered READ so the operator is not asked
twice; its output changes every run, so the proof check never replays it
(tool_names.NO_REPLAY).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec
from ..project_checks import KINDS, check_for
from ..tool_names import RUN_CHECKS


def register(mediator: Mediator, dispatch: dict, root: Path) -> None:
    root = Path(root)

    def run_checks(args) -> str:
        from ..engine.validator import run_check
        kind = str(args.get("kind") or "test").lower()
        if kind not in KINDS:
            return f"error: kind is one of {', '.join(KINDS)}"
        # the project being worked on (loop.work_dir), else the workspace
        where = Path(getattr(mediator, "work_dir", None) or root)
        # which tests: the ones asked for; else, for tests, the ones that
        # belong to the files this run changed; the whole suite only when
        # asked (all: true) or when no related test is found
        tests = [str(t) for t in (args.get("tests") or []) if str(t).strip()]
        if kind == "test" and not tests and not args.get("all"):
            from ..project_checks import related_tests
            changed = list(getattr(mediator, "_changed_paths", None) or [])
            tests = related_tests(where, changed) if changed else []
            if tests:
                args = dict(args, tests=tests)       # The proof re-run repeats exactly this
        # Way 2: the same checks run again with nothing changed since they
        # PASSED gives the same answer — said, not re-run (testx's: only after
        # a pass — a failing run runs again, tests can be flaky; force: true
        # always runs)
        cache = getattr(mediator, "_checks_cache", None)
        if cache is None:
            cache = mediator._checks_cache = {}
        key = (kind, str(where), tuple(tests))
        prev = cache.get(key)
        if (prev and not args.get("force") and prev[0] == getattr(mediator, "_changes", 0)
                and prev[1].get("ok")):
            return json.dumps(dict(prev[1], unchanged=(
                "nothing was written or run since these checks passed — the same result, not "
                "run again (force: true runs them anyway)")), ensure_ascii=False)
        res = run_check(mediator, dispatch, where, kind, tail_chars=4000, tests=tests or None)
        if res.get("ran"):
            cache[key] = (getattr(mediator, "_changes", 0), res)
            if res.get("scope", "all") != "all":
                res["whole_suite"] = ("only the tests related to the change ran — "
                                      "run_checks with all: true runs the whole suite")
        if not res.get("ran"):
            found = [k for k in KINDS if check_for(where, k)]
            res["available"] = found or "none — the operator can set them " \
                                        "in .aura/checks.json"
        return json.dumps(res, ensure_ascii=False)

    mediator.register(ToolSpec(RUN_CHECKS, BlastRadius.READ, lambda a: None))
    dispatch[RUN_CHECKS] = run_checks

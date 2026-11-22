"""teams_report.py — making the work events, by code only (plan).

Events are made by CODE from the loop's own hooks — never by the model:
tool_done of write/edit/patch -> file.changed (counts from the diff);
run_checks / test-runner run_command -> test.ran (parsed pass/fail); a mutating
shell command (run_command first-verb heuristics, CHANGE_WORDS below) ->
command.changed (the NAME only, scrubbed — never the arguments, never paths);
teams_run start/blocked/finish/decision -> task.started/
blocked/finished/decision.made; teams_map.upload -> map.updated. Every text
passes the scrubber. Only when a project is linked AND the hub is AuraHub; the
team.reports setting (default on) gates it all; resends are harmless
(device-local ids vs the log's UNIQUE (source, ev_id)).

"never sent":.env / keys / generated folders / anything outside the project
root — path_off_limits reuses teams_map._skip's exclusion class (plus
id_rsa/*.pem, node_modules, dist/build/out trees and../ escapes) and screens
every file path a report carries: an excluded file.changed is DROPPED (the
map's own convention — a skipped file never appears at all);
planned_files/changed lists are FILTERED to the safe entries.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid

_SEQ = {"n": 0}
# The log keys events on UNIQUE (source, ev_id) — an id made from a counter
# alone collides with the same device's stored rows after a restart (and the
# resend is silently dropped). The per-process session suffix keeps ids unique
# across restarts; the counter keeps them ordered within a session.
_SESSION = uuid.uuid4().hex[:10]

TEST_RUNNERS = ("pytest", "npm test", "go test", "cargo test", "mvn test",
                "dotnet test", "python -m pytest", "tox", "gradle test")

# command.changed — "a command that changed things": the shell tool's own
# mutating first-words (tools/shell.py _MUTATING_WORDS, kept in step) plus
# plain file-creators. The event carries the NAME ONLY.
CHANGE_WORDS = ("rm", "rmdir", "mv", "dd", "chmod", "chown", "truncate",
                "ln", "cp", "tee", "mkfs", "mkdir", "touch", "patch")
# state-changing subcommands of otherwise-read tools (git status/diff/...
# are reads; pip list is a read — only these count as 'changed things')
CHANGE_SUB = {"git": {"add", "commit", "push", "merge", "rebase", "reset",
                      "checkout", "switch", "restore", "tag", "mv", "rm",
                      "cherry-pick", "apply", "stash", "init"},
              "pip": {"install", "uninstall"}, "pip3": {"install",
                                                         "uninstall"},
              "npm": {"install", "uninstall", "update", "ci"},
              "pnpm": {"install", "uninstall", "update", "add", "remove"},
              "yarn": {"install", "add", "remove"},
              "apt": {"install", "remove", "purge"},
              "apt-get": {"install", "remove", "purge"},
              "brew": {"install", "uninstall"}}
# Never-sent file classes, ON TOP of teams_map._skip's list
_SKIP_NAMES_EXTRA = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                     ".env.local", ".env.production", ".env.development",
                     "credentials.json")
_SKIP_DIRS = ("node_modules", "dist", "build", "out", "__pycache__",
              ".git", ".venv", "venv", "target", "coverage")


def path_off_limits(path) -> bool:
    """A reported path that must NEVER leave the machine — the map's own exclusion
    class (teams_map._skip:.env*/secret/key/... + binary suffixes) plus key
    files, generated/dependency trees, and anything pointing outside the
    project root (absolute, drive-letter,..).
    """
    if not isinstance(path, str) or not path.strip():
        return True                              # No path, no event
    import re as _re
    p = path.strip().replace("\\", "/")
    low = p.lower()
    if low.startswith("/") or _re.match(r"^[a-z]:", low) or \
            ".." in low.split("/"):
        return True                              # Outside the project root
    name = low.rsplit("/", 1)[-1]
    if name in _SKIP_NAMES_EXTRA or name.endswith((".pem", ".key", ".p12",
                                                   ".pfx", ".keystore")):
        return True
    if any(seg in _SKIP_DIRS for seg in low.split("/")):
        return True
    try:
        from . import teams_map
        return bool(teams_map._skip(p))
    except Exception:                            # noqa: BLE001
        return True            # The exclusion check failing closes the door


def clean_paths(paths: list) -> list:
    """planned_files / changed filtered to what may leave the machine."""
    return [p for p in paths if isinstance(p, str) and not path_off_limits(p)]


def _scrub(text: str) -> str:
    try:
        from .secrets_guard import scrub_secrets
        return scrub_secrets(text)
    except Exception:                            # noqa: BLE001
        return text


def _changed_verb(cmd: str) -> str | None:
    """The NAME of a command that changed things (command.changed) — the first
    verb of any &&/;/| segment; `git` & package managers count by subcommand;
    VAR=value prefixes ride the next word. None = read-only.
    """
    import re as _re
    for seg in _re.split(r"&&|\|\||[;|\n]", cmd or ""):
        words = seg.strip().split()
        while words and _re.match(r"^[A-Za-z_][\w.]*=", words[0]):
            words = words[1:]                    # FOO=bar prefixes
        if not words:
            continue
        verb = words[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        verb = verb.removesuffix(".exe")
        if verb in CHANGE_WORDS:
            return verb
        sub = words[1].lower() if len(words) > 1 else ""
        if sub in CHANGE_SUB.get(verb, ()):
            return f"{verb} {sub}"
    return None


def command_changed_event(cmd: str, project) -> dict | None:
    """A mutating shell command -> command.changed with the command NAME only
    (scrubbed) — arguments (paths, values, secrets) never ride. None for
    read-only commands. The loop's tool_done hook already calls from_tool_done
    (engine/loop.py, not editable), which calls this for run_command — no hook
    change needed.
    """
    verb = _changed_verb(cmd)
    if verb is None:
        return None
    verb = _scrub(verb)[:60]
    return make_event("command.changed", project, verb,
                      {"command": verb})


def enabled() -> bool:
    """team.reports (default on when joined to an Aura team)."""
    try:
        from . import teams
        cfg = teams.load_config()
        if not cfg.get("aura_team"):
            return False
        return bool(cfg.get("team.reports", True))
    except Exception:                            # noqa: BLE001
        return False


def make_event(etype: str, project, subject: str, data: dict) -> dict | None:
    """One event with a device-local id; scrubbed; None when reports are off or
    nothing is linked.
    """
    if not enabled():
        return None
    if project is None:
        # H-9 / -6.3's: nothing was linked (the only link lane is the ask card)
        # — unlinked work NEVER reports, whoever the person
        return None
    from . import teams
    at = teams.load_config().get("aura_team") or {}
    device = at.get("device_id", "aura")
    _SEQ["n"] += 1
    return {"id": f"{device}-{_SESSION}-{_SEQ['n']}", "type": etype,
            "project": project, "subject": subject,
            "time": round(__import__("time").time(), 3), "data": data}


def _runner_of(cmd: str) -> str:
    """the runner's own name only (pytest/vitest/npm …) — counts and verdict
    answer inside; the command's form itself stays off the chain."""
    low = (cmd or "").strip().lower()
    for tag in ("pytest", "vitest", "jest", "npm test", "pnpm test",
                "go test", "cargo test", "ctest"):
        if tag in low:
            return tag.split()[0]
    return low.split(None, 1)[0][:24] if low else ""


def test_counts(text: str) -> tuple[int, int]:
    """Passed and failed counts from the common runners' summaries: pytest /
    vitest / jest ("3 passed, 1 failed", "Tests: 3 passed"), node --test
    ("pass 7", "fail 0"), Python unittest ("Ran 3 tests", "FAILED (failures=1,
    errors=1)"), go test ("ok"/"FAIL" lines are not counted). Several runs in
    one command add up."""
    passed = failed = 0
    found = False
    for m in re.finditer(r"(\d+) passed", text):
        passed += int(m.group(1)); found = True
    for m in re.finditer(r"(\d+) failed", text):
        failed += int(m.group(1)); found = True
    for m in re.finditer(r"^\s*(?:\S+\s+)?(pass|fail)\s+(\d+)\s*$", text, re.M):
        found = True
        if m.group(1) == "pass":
            passed += int(m.group(2))
        else:
            failed += int(m.group(2))
    for m in re.finditer(r"^Ran (\d+) tests?\b", text, re.M):
        found = True
        ran = int(m.group(1))
        bad = re.search(r"FAILED \(([^)]*)\)", text[m.end():])
        n_bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)",
                                                  bad.group(1))) if bad else 0
        passed += max(0, ran - n_bad)
        failed += n_bad
    return (passed, failed) if found else (0, 0)


def from_tool_done(tool: str, result, project, args: dict | None = None,
                   task=None, work_dir: str | None = None) -> dict | None:
    """tool_done hooks: fs writes -> file.changed; checks / test runners ->
    test.ran. The result is the tool's result text (a str) or a dict; both
    arrive here. Returns the event or None.
    """
    text = result if isinstance(result, str) else json.dumps(result)
    # a JSON result carries its fields in the clear — parse once so the
    # diff's real newlines (not \n escapes) are counted; keep the dict for
    # direct field reads (cmd/command, exit_code)
    fields: dict = {}
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            fields = d
            text = "\n".join(str(v) for v in d.values())
    except Exception:                            # noqa: BLE001
        pass
    if tool in ("write_file", "edit_file", "apply_patch"):
        # path: the call's own path argument (what was really written), then
        # a "path": "..." in the result. A file name guessed out of the result
        # text reported words ("assert.deep", "e.g") as files — never again.
        args = args if isinstance(args, dict) else {}
        path = str(args.get("path") or args.get("file_path") or "")
        if not path:
            m = re.search(r'"path"\s*:\s*"([^"]+)"', text)
            path = m.group(1) if m else ""
        if path and work_dir:
            try:
                from .teams_collide import rel_to_repo
                path = rel_to_repo(path, work_dir)
            except Exception:                    # noqa: BLE001
                pass
        if path_off_limits(path):
            return None      # .env/keys/generated/outside-root never
        if "new_string" in args or "old_string" in args:
            added = len(str(args.get("new_string") or "").splitlines())
            removed = len(str(args.get("old_string") or "").splitlines())
        elif "content" in args:
            added, removed = len(str(args.get("content") or "").splitlines()), 0
        else:
            added = len(re.findall(r"^\+[^+]", text, re.M))
            removed = len(re.findall(r"^-(?!-)", text, re.M))
        data = {"path": path, "added": added, "removed": removed}
        if task is not None:
            data["task"] = task      # The map's overlays and clashes need it
        return make_event("file.changed", project, path, data)
    if tool in ("run_checks", "run_command"):
        cmd = str(fields.get("command") or fields.get("cmd") or "")
        if not cmd:
            m = re.search(r'"command"\s*:\s*"([^"]+)"', text) or \
                re.search(r'"cmd"\s*:\s*"([^"]+)"', text)
            if m:
                cmd = m.group(1)
            else:
                m = re.search(r"'command'\s*:\s*'([^']+)'", text) or \
                    re.search(r"'cmd'\s*:\s*'([^']+)'", text)
                cmd = m.group(1) if m else text.strip().splitlines()[0][:120]
        low = cmd.lower()
        if tool == "run_command" and not any(t in low for t in TEST_RUNNERS):
            # A non-test run_command that changed things -> command.changed
            # (name only, scrubbed); read-only -> nothing
            try:
                code = fields.get("exit_code")
                if code is not None and int(code) != 0:
                    return None                  # It failed — nothing changed
            except (TypeError, ValueError):
                pass
            return command_changed_event(cmd, project)
        passed, failed = test_counts(text)
        try:
            code = fields.get("exit_code")
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        data = {"runner": _runner_of(cmd), "passed": passed, "failed": failed,
                # the run's own exit code decides when there is one
                "ok": (code == 0) if code is not None else failed == 0}
        if task is not None:
            data["task"] = task
        return make_event("test.ran", project, _scrub(cmd[:60]), data)
    return None


def from_loop(kind: str, project, subject: str, data: dict) -> dict | None:
    """teams_run hooks: start/blocked/finish/decision. File lists are screened:
    planned_files/changed keep only paths allowed to leave the machine.
    """
    map_ = {"start": "task.started", "blocked": "task.blocked",
            "finish": "task.finished", "decision": "decision.made"}
    etype = map_.get(kind)
    if etype is None:
        return None
    data = dict(data or {})
    for key in ("planned_files", "changed"):
        if isinstance(data.get(key), list):
            data[key] = clean_paths(data[key])
    return make_event(etype, project, _scrub(str(subject))[:200], data)


def queue_report(event: dict) -> dict:
    """The event rides the existing teams_queue (kind 'report'); the send
    is a signed POST /reports; the id makes resends harmless."""
    if event is None:
        return {"ok": False, "detail": "no event"}
    from . import teams_queue as Q
    return Q._send_or_queue("report", 0, {"event": event})


def send_batch(events: list[dict]) -> dict:
    """≤ 200 events signed to POST /reports through the pinned client."""
    from .teamserver.teamclient import AuraClient
    from . import teams
    at = teams.load_config().get("aura_team")
    if not at:
        return {"ok": False, "detail": "not on your own team server"}
    c = AuraClient(at)
    status, data = c.request("POST", "/api/v1/reports",
                             {"events": events[:200]})
    return {"ok": status == 200, "status": status, "detail": data}


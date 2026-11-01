"""approvals_suggest.py — Mine the audit tape → allowlist.

Reads the engagement audit.jsonl, groups OK-tool calls by (tool,
target-prefix), and suggests targets/tools that were executed cleanly MANY
times — candidates for an explicit allowlist (assist=auto for that shape).

Never auto-applies: prints suggestions + a ready-to-paste snippet; the operator
decides. Consent model preserved.
"""
from __future__ import annotations

import json
from pathlib import Path

MIN_RUNS = 3            # Seen this many times, all clean → suggestable
MIN_DISTINCT = 2        # ...as at least 2 DIFFERENT calls (args): one call
                        # repeated by a loop is not a habit worth a
                        # standing permission (the audit has no session id,
                        # so distinct arguments stand in for "2 sessions")


def _iter_audit(eng: Path):
    p = Path(eng) / "audit.jsonl"
    if not p.exists():
        return
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue
    except OSError:
        return


def suggest(eng: Path, min_runs: int = MIN_RUNS) -> list[dict]:
    """[(tool, target, clean_runs)] — safe repeated calls, newest insight."""
    counts: dict[tuple[str, str], dict] = {}
    for rec in _iter_audit(eng):
        call = rec.get("call")
        status = rec.get("status", "")
        if call and status == "OK":
            target = rec.get("target") or ""
            key = (call, target.split("/")[0] if target else "")
            entry = counts.setdefault(key, dict(tool=call, target=key[1],
                                                runs=0, distinct=set()))
            entry["runs"] += 1
            entry["distinct"].add(rec.get("args_hash"))
    out = [dict(tool=t, target=e["target"], runs=e["runs"])
           for (t, _), e in counts.items()
           if e["runs"] >= min_runs and e["target"]
           and len(e["distinct"]) >= MIN_DISTINCT]
    out.sort(key=lambda x: (-x["runs"], x["tool"]))
    return out


def render(suggestions: list[dict]) -> str:
    if not suggestions:
        return ("no repeated clean targets yet — suggestions appear after "
                "the same target/tool runs cleanly 3+ times")
    lines = ["approval suggestions (repeated clean calls — review, then "
             "add to AGENTS.md or notebook constraints):"]
    for s in suggestions[:12]:
        lines.append(f"  • {s['tool']:12s} {s['target']:32s} "
                     f"({s['runs']} clean runs)")
    return "\n".join(lines)


# ------------------------------------------- from your own answers --
# The audit tape stores no file paths or commands (args are hashed), so it
# can never suggest a permission for a file write. The approval records
# can: each one names the tool, the target and what you answered.

def from_answers(eng: Path, min_yes: int = 2) -> list[dict]:
    """Calls you approved "once" at least `min_yes` times that a saved
    permission could cover (G1/G2 only) and none does yet."""
    from .autonomy import match_grant
    d = Path(eng) / "decided"
    counts: dict[tuple, int] = {}
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if rec.get("outcome") != "approved" or \
                    rec.get("grade") not in ("G1", "G2"):
                continue
            key = (rec.get("grade"), rec.get("tool"), rec.get("target"))
            if key[2] in (None, "", "?", "*"):
                continue
            counts[key] = counts.get(key, 0) + 1
    out = [dict(grade=g, tool=t, target=tg, yes=n)
           for (g, t, tg), n in counts.items()
           if n >= min_yes and match_grant(eng, g, t, tg) is None]
    out.sort(key=lambda x: (-x["yes"], x["tool"], x["target"]))
    return out


def command(eng: Path, parts: list[str]) -> list[str]:
    """/approvals                  saved permissions + suggestions
    /approvals allow <n>         save suggestion n as a permission
    /approvals revoke <id>       remove a saved permission"""
    from .autonomy import list_grants, persist_grant, revoke_grant
    sub = parts[1].lower() if len(parts) > 1 else ""
    sugg = from_answers(eng)
    if sub == "allow":
        try:
            s = sugg[int(parts[2]) - 1]
        except (IndexError, ValueError):
            return ["usage: /approvals allow <n> — n from the list in /approvals"]
        try:
            g = persist_grant(eng, s["grade"], s["tool"], s["target"],
                              label="saved from /approvals")
        except (ValueError, OSError) as e:
            return [f"not saved: {e}"]
        return [f"saved permission {g['id']}: {s['tool']} → {s['target']} "
                f"(no longer asks; /approvals revoke {g['id']} to undo)"]
    if sub == "revoke":
        if len(parts) < 3:
            return ["usage: /approvals revoke <id>"]
        return [f"removed {parts[2]}" if revoke_grant(eng, parts[2])
                else f"no saved permission with id {parts[2]}"]
    lines = ["saved permissions (these calls run without asking):"]
    grants = list_grants(eng)
    lines += [f"  {g['id']}  {g['tool']} → {g['target']}  ({g['grade']})"
              for g in grants] or ["  none"]
    lines.append("suggestions (you said yes to these more than once):")
    lines += [f"  {i}. {s['tool']} → {s['target']}  ({s['yes']}× yes)"
              for i, s in enumerate(sugg[:12], 1)] or ["  none yet"]
    lines.append("/approvals allow <n> saves one · /approvals revoke <id> "
                 "removes one · irreversible actions can never be saved")
    return lines

"""claim_check.py — the final answer against what happened.

With the proof feature on, Aura re-checks the model's claims before it answers.
With it off (the default), only the run report said what happened — nothing
compared the ANSWER with it, so a model could write "all tests pass, fixed"
when no test ran after its last change.

So, with no extra model call: 1. claims in the answer vs the run's facts (tests
run after the last change and their result, files changed, anything run at
all); 2. signs of faking it in the text the model ADDED (skips, stubs,
not-implemented, a forced pass); 3. things the user named (files, `code`) that
nothing changed or read. It only reports — the answer is never blocked or
rewritten.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------- claims --
_TESTS_PASS = re.compile(
    r"\b(?:all\s+(?:the\s+)?tests?\s+(?:now\s+)?pass(?:es|ed|ing)?|tests?\s+(?:now\s+|all\s+)?pass(?:es|ed|ing)?"
    r"|tests?\s+(?:are|were)\s+(?:all\s+)?(?:passing|green)|test\s+suite\s+(?:passes|passed|is\s+green))\b", re.I)
# first-person claims only — "it works as expected" can be a plain answer
_VERIFIED = re.compile(
    r"\bi(?:'ve|\s+have)?\s+(?:verified|confirmed|tested|checked|ran)\b", re.I)
_CHANGED = re.compile(
    r"\b(?:fixed|updated|changed|edited|modified|rewrote|refactored|added\s+\w+\s+to|created)\s+"
    r"(?:the\s+(?:file\s+)?)?`?([\w./\\-]+\.[A-Za-z0-9]{1,6})`?", re.I)
# The task asked to deliver something to someone / somewhere outside, and the
# answer says it happened
_ASKS_SEND = re.compile(r"\b(?:send|e-?mail|publish|tweet|deploy|upload|notify)\b", re.I)
_SAYS_SENT = re.compile(
    r"\b(?:sent|e-?mailed|published|tweeted|deployed|uploaded|notified)\b"
    r"|^\s*done[.!]?\s*$", re.I | re.M)
_NEGATED = re.compile(r"\b(?:not|n't|couldn't|could\s+not|didn't|did\s+not|unable|haven't|no)\b[^.\n]{0,40}$", re.I)


# A sentence about what would happen, not what did
_HYPOTHETICAL = re.compile(r"\b(?:if|when|whenever|once|unless|must|should|would|will|could)\b", re.I)


def _claimed(rx: re.Pattern, text: str) -> list[str]:
    """Each match not preceded by a 'not / couldn't' in the same sentence."""
    out = []
    for m in rx.finditer(text or ""):
        before = text[max(0, m.start() - 60):m.start()]
        before = re.split(r"[.!?\n]", before)[-1]
        if not _NEGATED.search(before) and not _HYPOTHETICAL.search(before):
            out.append(m.group(0).strip())
    return out


def _base(p: str) -> str:
    return str(p).replace("\\", "/").rsplit("/", 1)[-1].lower()


def check_claims(answer: str, facts: dict) -> list[str]:
    """facts: changed (paths), tests_ran_after_change (bool|None: None =
    nothing changed), tests_ok (bool), ran_anything (bool); task (the
    user's words) and reached_outside (bool: a call that sends out ran)."""
    out: list[str] = []
    ask = _ASKS_SEND.search(facts.get("task") or "")
    said = _claimed(_SAYS_SENT, answer)
    if ask and said and facts.get("reached_outside") is False:
        out.append(f"the task asked to {ask.group(0).lower()}, and the answer says "
                   f"'{said[0]}', but nothing in this task sent anything out — "
                   "at most a draft was written")
    for c in _claimed(_TESTS_PASS, answer)[:1]:
        after = facts.get("tests_ran_after_change")
        if facts.get("tests_ever") is False:
            out.append(f"the answer says '{c}', but no tests ran in this task")
        elif after is False:
            out.append(f"the answer says '{c}', but no tests ran after the last change")
        elif not facts.get("tests_ok", False):
            out.append(f"the answer says '{c}', but the last test run FAILED")
    changed = {_base(p) for p in facts.get("changed") or []}
    for m in list(_CHANGED.finditer(answer or ""))[:6]:
        before = re.split(r"[.!?\n]", answer[max(0, m.start() - 60):m.start()])[-1]
        if _NEGATED.search(before):
            continue
        name = _base(m.group(1))
        if name not in changed:
            out.append(f"the answer says it {m.group(0).split()[0].lower()} {m.group(1)}, "
                       "but that file wasn't changed in this task")
    if not facts.get("ran_anything") and _claimed(_VERIFIED, answer):
        out.append(f"the answer says '{_claimed(_VERIFIED, answer)[0]}', but nothing was run "
                   "to check it")
    return out


# --------------------------------------------------------- faking signs --
_FAKE = [
    (re.compile(r"@pytest\.mark\.(?:skip|xfail)\b|\bunittest\.skip\b|@unittest\.skip|\bpytest\.skip\("), "a test switched off (skip)"),
    (re.compile(r"\b(?:it|describe|test)\.(?:skip|todo)\(|\bxit\(|\bxdescribe\("), "a test switched off (skip)"),
    (re.compile(r"\bt\.Skip\(|#\[ignore\]"), "a test switched off (skip)"),
    (re.compile(r"\braise\s+NotImplementedError\b|\bthrow\s+new\s+Error\(\s*['\"]not\s+implemented", re.I), "'not implemented' left in"),
    (re.compile(r"\btodo!\(\)|\bunimplemented!\(\)|panic\(\s*\"not implemented", re.I), "'not implemented' left in"),
    (re.compile(r"^\s*(?:#|//)\s*(?:TODO|FIXME)\b.*(?:implement|real|later|actual)", re.I | re.M), "a TODO where the real code should be"),
    (re.compile(r"\bassert\s+True\b|\bexpect\(\s*true\s*\)\.toBe\(\s*true\s*\)|\bassert\s+1\s*==\s*1\b"), "a check that always passes"),
    (re.compile(r"\bsys\.exit\(\s*0\s*\)|\bprocess\.exit\(\s*0\s*\)"), "a forced 'success' exit"),
]


def fake_signs(added: list[tuple[str, str]]) -> list[str]:
    """added: [(path, text the model added)] → plain findings, once each."""
    out: list[str] = []
    seen = set()
    for path, text in added:
        for rx, what in _FAKE:
            if rx.search(text or "") and (what, _base(path)) not in seen:
                seen.add((what, _base(path)))
                out.append(f"{what} in {path}")
    return out[:6]


def added_text(name: str, args: dict) -> str:
    """The lines a write added (edit_file: lines in new_string that weren't
    in old_string; write_file: the content)."""
    if not isinstance(args, dict):
        return ""
    if name == "write_file":
        return str(args.get("content") or "")[:20000]
    parts = [args] + [e for e in (args.get("edits") or []) if isinstance(e, dict)]
    out = []
    for e in parts:
        old = set(str(e.get("old_string") or "").splitlines())
        out += [ln for ln in str(e.get("new_string") or "").splitlines() if ln not in old]
    return "\n".join(out)[:20000]


# -------------------------------------------------------- named things --
_NAMED_FILE = re.compile(r"(?<![\w/.-])([\w-]+(?:/[\w.-]+)*\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|cs|c|cpp|h|md|json|ya?ml|toml|html|css|sh|sql))\b")
_BACKTICK = re.compile(r"`([^`\s]{3,60})`")


def untouched(task: str, changed: list[str], read: list[str]) -> list[str]:
    """Files the user named that nothing in the task changed or read."""
    seen = {_base(p) for p in (changed or []) + (read or [])}
    names = {m.group(1) for m in _NAMED_FILE.finditer(task or "")}
    names |= {m.group(1) for m in _BACKTICK.finditer(task or "") if "." in m.group(1)
              and _NAMED_FILE.fullmatch(m.group(1))}
    return sorted(n for n in names if _base(n) not in seen)[:5]

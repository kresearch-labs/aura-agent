"""loopwatch.py — loop detection v2: duplicate-burst + stall + mentor turn.

  - normalize: strip digits/timestamps/random ids → `/v300`≈`/v301`
  - 5 identical normalized calls in a row → duplicate burst → abort to
    mentor turn
  - stall detector: the last N tool calls brought NO NEW INFORMATION —
    every result was one already seen this run. Catches what the burst
    check can't: ping-pong (A, B, A, B) and cycling through a few calls
    read-only research task a stall.)
  - mentor prompt: diagnosis + 3 options + branch recommendation —
    injected as a system message, loop continues (PentAGI pattern)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

BURST_LIMIT = 5          # Identical normalized calls → burst
STALL_CALLS = 6          # Calls in a row with nothing new → stall
SAME_WARNING = 3         # Mentor nudge threshold (soft)


TARGET_LIMIT = 12       # Reads of one target with no progress → warning
IGNORED_AFTER = 4       # More loop calls after a warning → it was ignored


_PATHISH = re.compile(r"(?:https?://[^\s'\"]+|(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})")
_PROGRESS = ("write_file", "edit_file", "finding_add", "todo_write")


def target_of(name: str, args: dict) -> str | None:
    """What a call is probing — a file, URL or page. Varied probes of one target
    (the campaign's 517 regex variants on one page) share it; a call with no
    clear target has none.
    """
    a = args if isinstance(args, dict) else {}
    for k in ("path", "url", "file"):
        if isinstance(a.get(k), str) and a[k].strip():
            return a[k].strip().replace("\\", "/").lstrip("./")
    if name in ("run_command", "process_start") and isinstance(a.get("cmd"), str):
        found = [m for m in _PATHISH.findall(a["cmd"]) if not m.startswith("-")]
        if found:                       # The last file-ish word: the one being probed
            return found[-1].replace("\\", "/").lstrip("./")
    return None


_KEEP_EXACT = ("path", "file")    # URLs stay blurred: v300 ≈ v301 (enumeration)


def exact_call(name: str, args: dict) -> str:
    try:
        return f"{name} {json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name} {args!r}"


def normalize_call(name: str, args: dict) -> str:
    """Canonical form of a tool call for repeat comparison. File paths and URLs
    stay exact (: reading m1.py, m2.py, m3.py is not "the same action" — digit
    blurring there made normal reading look like a loop).
    """
    keep = {}
    try:
        a = dict(args or {})
        for k in _KEEP_EXACT:
            if isinstance(a.get(k), str):
                keep[k] = a.pop(k)
        payload = json.dumps(a, sort_keys=True, default=str)
    except (TypeError, ValueError, AttributeError):
        payload = str(args)
    s = f"{name} {payload}"
    s = re.sub(r"\d+", "N", s)              # Digits → N (/v300 ≈ v301)
    s = re.sub(r"\b\d{2}:\d{2}(:\d{2})?\b", "T", s)   # Timestamps
    s = re.sub(r"[a-f0-9]{8,}", "H", s, flags=re.I)   # hashes/ids
    s = re.sub(r"NN+\.NN+\.NN+\.NN+", "IP", s)        # Keep IPs comparable
    if keep:
        s += " " + json.dumps(keep, sort_keys=True)
    return re.sub(r"\s+", " ", s).strip()


def _command_output(text: str) -> str | None:
    """A shell result carries its own command text, so two wordings of the
    same search never look alike. Return just exit code + output, or None
    when it isn't a shell result or printed nothing (a silent command —
    `mkdir a`, `mkdir b` — is no evidence of going in circles)."""
    try:
        outer = json.loads(text)
        inner = outer.get("result") or outer.get("content") or ""
        d = json.loads(inner) if isinstance(inner, str) else inner
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(d, dict) or "stdout" not in d:
        return None
    out = f"{d.get('stdout', '')}{d.get('stderr', '')}".strip()
    return f"{d.get('exit_code')}|{out}" if out else None


def result_key(name: str, args: dict, result: dict, read_only: bool) -> str:
    """Fingerprint of what a call brought back. Reads: tool + result (the
    same content from another path is nothing new). Writes/commands: the
    call too (doing something new is progress). Timers, clock times and
    long hex ids are blanked so they don't make a repeat look new; whole
    numbers stay ("3 failed" → "2 failed" IS progress)."""
    ok = result.get("status") == "ok"
    text = str(result.get("result") if ok else result.get("error"))[:20000]
    body = _command_output(text) if ok else None
    if body is not None:
        text, read_only = body, True    # Same output, any wording = nothing new
    text = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\b", "T", text)
    text = re.sub(r"\d+\.\d+", "N.N", text)
    text = re.sub(r"\b[a-f0-9]{8,}\b", "H", text, flags=re.I)
    try:
        call = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        call = str(args)
    head = name if read_only else f"{name} {call}"
    blob = f"{head}\x00{result.get('status')}\x00{text}"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class LoopState:
    normalized_history: list[str] = field(default_factory=list)
    exact_history: list[str] = field(default_factory=list)
    seen_results: set = field(default_factory=set)
    fresh: list[bool] = field(default_factory=list)
    turn: int = 0

    # ---------------------------------------------------------------- burst
    def record_call(self, name: str, args: dict) -> dict:
        """Call after EVERY tool execution. Returns verdict dict."""
        sig = normalize_call(name, args)
        self.normalized_history.append(sig)
        self.exact_history.append(exact_call(name, args))
        self.turn += 1
        recent = self.normalized_history[-BURST_LIMIT:]
        burst = (len(recent) == BURST_LIMIT
                 and len(set(recent)) == 1)
        same_run = 0
        for s in reversed(self.normalized_history):
            if s == sig:
                same_run += 1
            else:
                break
        return {
            "burst": burst,
            "same_run": same_run,
            "nudge": same_run >= SAME_WARNING and not burst,
        }

    # ---------------------------------------------------------------- stall
    def record_result(self, name: str, args: dict, result: dict,
                      read_only: bool = True) -> bool:
        """Call after EVERY tool execution. True = the last STALL_CALLS
        results were all repeats (going in circles). Fires once, then the
        window restarts — one nudge per stall, not one per call."""
        key = result_key(name, args, result, read_only)
        self.fresh.append(key not in self.seen_results)
        self.seen_results.add(key)
        if len(self.fresh) >= STALL_CALLS and not any(self.fresh[-STALL_CALLS:]):
            self.fresh.clear()
            return True
        return False

    # ------------------------------------------- targets + escalation
    # A warning is text a model can ignore (the campaign: the same grep ~543
    # times after 2 nudges). Like a circuit breaker: an ignored warning
    # OPENS the call / target (refused with the reason, not run); real
    # progress closes it; looping on after a block stops the run.
    target_hits: dict = field(default_factory=dict)
    target_warned: set = field(default_factory=set)
    watching: dict = field(default_factory=dict)      # Key -> calls since its warning
    blocked: dict = field(default_factory=dict)       # Key -> why
    blocked_tries: int = 0
    stalls: int = 0
    stop_reason: str = ""

    @staticmethod
    def _keys(name: str, args: dict) -> list[str]:
        keys = ["call:" + exact_call(name, args)]   # Escalation follows EXACT repeats
        t = target_of(name, args)
        if t:
            keys.append("target:" + t)
        return keys

    def blocked_reason(self, name: str, args: dict) -> str | None:
        """Before running a call: refused while its breaker is open."""
        for k in self._keys(name, args):
            if k in self.blocked:
                self.blocked_tries += 1
                if self.blocked_tries >= 3 and not self.stop_reason:
                    self.stop_reason = (f"the loop guard stopped the run: calls kept going "
                                        f"to something blocked ({k.split(':', 1)[1][:80]}) — "
                                        f"{self.blocked[k]}")
                return (f"blocked by the loop guard: {self.blocked[k]}. Write what you "
                        "have, take a different approach, or tell the user what is in "
                        "the way. It opens again after real progress (a file written "
                        "or edited, a result recorded).")
        return None

    def after(self, name: str, args: dict, ok: bool, verdict: dict,
              stalled: bool) -> list[str]:
        """After a call: progress, target counts, ignored warnings. Returns
        the warnings to show (and fills blocked / stop_reason)."""
        out: list[str] = []
        had_block = bool(self.blocked)        # The model was refused before this call
        if ok and name in _PROGRESS:            # Real progress closes everything
            self.target_hits.clear()
            self.target_warned.clear()
            self.watching.clear()
            self.blocked.clear()
            self.blocked_tries = 0
            self.stalls = 0
            return out
        keys = self._keys(name, args)
        # a warning ignored: the same call / target goes on
        for k in keys:
            if k in self.watching:
                self.watching[k] += 1
                if self.watching[k] >= IGNORED_AFTER and k not in self.blocked:
                    self.blocked[k] = ("you were warned about this and went on "
                                       f"{self.watching[k]} more times")
                    out.append(f"[LOOP GUARD] blocked: {k.split(':', 1)[1][:120]} — "
                               f"{self.blocked[k]}. Calls to it are refused until real "
                               "progress (a file written or edited, a result recorded).")
        # One target probed again and again with no progress
        t = target_of(name, args)
        if t:
            self.target_hits[t] = self.target_hits.get(t, 0) + 1
            if self.target_hits[t] >= TARGET_LIMIT and t not in self.target_warned:
                self.target_warned.add(t)
                self.watching.setdefault("target:" + t, 0)
                out.append(f"[LOOP GUARD] {t} was read or probed {self.target_hits[t]} times "
                           "with nothing written. It probably doesn't hold what you're "
                           "looking for in that form. Write down what you have, or take a "
                           "different approach (another source, ask the user).")
        if verdict.get("nudge") or verdict.get("burst"):
            self.watching.setdefault(keys[0], 0)
        if stalled:
            self.stalls += 1
            for s in self.exact_history[-STALL_CALLS:]:
                self.watching.setdefault("call:" + s, 0)
            if self.stalls >= 3 and not self.stop_reason:
                self.stop_reason = ("the loop guard stopped the run: three times in a "
                                    f"row, {STALL_CALLS} calls brought nothing new")
        if had_block and (verdict.get("burst") or stalled) and not self.stop_reason:
            self.stop_reason = ("the loop guard stopped the run: it kept looping after "
                                "a block")
        return out


MENTOR_PROMPT = """[MENTOR TURN — loop guard triggered]
You have repeated the same action without progress. Before continuing:
1. DIAGNOSE: why did the last {n} identical calls fail to advance the objective?
2. OPTIONS: give exactly 3 alternative approaches.
3. RECOMMEND: pick one and proceed.
Do not repeat the same call again — it will return the same thing."""

STALL_PROMPT = """[MENTOR TURN — no new information]
Your last {n} tool calls only returned results you had already seen in this
run — you are going in circles. Before continuing:
1. DIAGNOSE: what are you looking for that those calls did not give you?
2. OPTIONS: give exactly 3 different approaches (or ask the user).
3. RECOMMEND: pick one and proceed."""


def mentor_message(same_run: int) -> dict:
    return {"role": "system",
            "content": MENTOR_PROMPT.format(n=same_run)}


def stall_message() -> dict:
    return {"role": "system", "content": STALL_PROMPT.format(n=STALL_CALLS)}


# ------------------------------------------------ the same failure, again
# OpenHands stops after the same action fails 3 times; Aura also helps find the
# cause and tells the user. The ladder is keyed on the ERROR, so rewording the
# command doesn't reset it; a file written or edited (real progress) does.
FAIL_TELL_USER = 2       # Same error this many times → the user is told
FAIL_BLOCK = 3           # → the calls that hit it are refused
FAIL_STOP = 4            # → the run stops with a report

_CAUSES = (
    (r"not in this project's allowed sites|site list refused",
     "the site isn't on this project's allowed list (Aura's proxy refused it) — retrying can't "
     "work; Aura asks the user about the site (a card) — or use web_search / web_crawl"),
    (r"no network|could not resolve host|name resolution|network is unreachable|getaddrinfo|"
     r"failed to establish a new connection|urlopen error|\benotfound\b|\beai_again\b",
     "no network for this command (the sandbox keeps commands offline, or the machine is "
     "offline) — retrying can't work; use web_search / web_crawl, or ask the user"),
    (r"command not found|is not recognized as an internal or external command|not recognized as the name|"
     r"no such file or directory: '?[\w./-]*(?:python|node|npm|pip|git|cargo|go|java)",
     "the program isn't installed or isn't on PATH — check what is installed, use another tool, "
     "or tell the user (installing needs them)"),
    (r"no module named|modulenotfounderror|cannot find module|importerror",
     "a package is missing — check the project's requirements; installing needs the user"),
    (r"permission denied|access is denied|operation not permitted|\beacces\b|\beperm\b",
     "no permission — retrying won't change it; work inside the project, or ask the user"),
    (r"no such file or directory|cannot find the (?:file|path)|filenotfounderror|\benoent\b|does not exist",
     "the path is wrong — list the folder to find the real name"),
    (r"syntaxerror|syntax error|unexpected token|parse error|invalid syntax|unrecognized arguments|"
     r"unknown option|invalid option|usage:",
     "the command or code itself is wrong — read the message and fix it (check --help)"),
    (r"timed out|timeout",
     "it takes too long — make the step smaller, or run it in the background (process_start)"),
    (r"address already in use|\beaddrinuse\b",
     "the port is taken — use another port or stop what holds it"),
    (r"401|403|unauthori[sz]ed|forbidden|invalid api key",
     "access was refused — a key or login is missing; ask the user"),
)
_CAUSE_RX = [(re.compile(p, re.I), why) for p, why in _CAUSES]


_ERROR_WORDS = re.compile(r"\b(?:error|exception|traceback|fatal|failed|failure|not found|"
                          r"cannot|can't|could not|unable to|denied|refused|invalid)\b", re.I)


def likely_cause(err: str) -> str:
    for rx, why in _CAUSE_RX:
        if rx.search(err or ""):
            return why
    return "not one Aura knows — read the whole message: what does it name?"


def failure_of(name: str, result: dict) -> str | None:
    """The error text of a failed call, or None. A command that ran but
    exited non-zero failed too (its tool status is still "ok")."""
    if result.get("status") not in ("ok", "awaiting_approval"):
        err = str(result.get("error") or result.get("status"))[:600]
        # Aura's own refusals (loop guard, the user's "no") already say
        # what to do — they aren't failures to diagnose
        if err.startswith(("blocked by the loop guard", "skipped —", "assist:",
                           "plan mode:", "mode: tool", "discuss mode:")):
            return None
        # a place Aura never lets a chat use: Aura's own block, said with its
        # reason — not the model's failure (never blamed on the model)
        if err.startswith("path: '") and ("can't be used" in err
                                         or "wasn't allowed" in err):
            return None
        return err
    if name == "run_checks":
        # tests / lint that fail again with nothing edited in between are
        # the same failure (an edit starts the ladder over)
        try:
            d = json.loads(result.get("result") or "")
        except (TypeError, ValueError):
            return None
        if isinstance(d, dict) and d.get("ran") and d.get("ok") is False:
            return f"{d.get('command') or 'the check'} failed (exit {d.get('exit_code')}): " \
                   + str(d.get("why") if d.get("exit_code") == 0 else d.get("tail") or "")[-600:]
        return None
    if name not in ("run_command", "run_code", "process_start"):
        return None
    try:
        d = json.loads(result.get("result") or "")
        if isinstance(d, dict) and isinstance(d.get("result"), str) and "stdout" not in d:
            d = json.loads(d["result"])      # The tool wrapper around the command's own result
    except (TypeError, ValueError):
        return None
    if isinstance(d, dict) and d.get("exit_code") not in (None, 0):
        # exit 1 with nothing on stderr is an answer, not an error: grep
        # found no match, diff found a difference, test said false
        # — unless what it printed reads as an error (many tools print
        # errors to normal output)
        if d.get("exit_code") == 1 and not str(d.get("stderr") or "").strip() \
                and not _ERROR_WORDS.search(str(d.get("stdout") or "")[-2000:]):
            return None
        tail = (str(d.get("stderr") or "") + "\n" + str(d.get("stdout") or "")).strip()
        return f"exit {d.get('exit_code')}: {tail[-600:]}"
    return None


def error_key(name: str, err: str) -> str:
    """The same failure, however the call was worded: digits, paths,
    quoted words and ids blanked."""
    e = (err or "").lower()
    e = re.sub(r"(['\"`]).*?\1", "Q", e)
    e = re.sub(r"(?:[a-z]:)?[\\/][^\s:]*", "P", e)
    e = re.sub(r"\b[\w-]+(?:\.[\w-]+)+\b", "D", e)          # Hosts, file names
    e = re.sub(r"\b[a-f0-9]{8,}\b", "H", e)
    e = re.sub(r"\d+", "N", e)
    e = re.sub(r"\s+", " ", e).strip()
    return f"{name}|{e[-240:]}"


def _short(call: str, n: int = 90) -> str:
    """Start and end of a call — commands often differ only at the end."""
    return call if len(call) <= n else call[:30] + " … " + call[-(n - 33):]


@dataclass
class FailureLadder:
    counts: dict = field(default_factory=dict)       # Error key -> times
    tried: dict = field(default_factory=dict)        # Error key -> exact calls
    told: set = field(default_factory=set)
    last: dict = field(default_factory=dict)         # Error key -> short text (end-of-run line)

    def clear(self) -> None:
        self.counts.clear()
        self.tried.clear()
        self.told.clear()
        self.last.clear()

    def got_past(self, name: str) -> None:
        """The same tool then worked: its failures leave the end-of-run
        line (the ladder's counts stay, so the same error still climbs)."""
        for k in [k for k, v in self.last.items() if v.startswith(f"{name}:")]:
            del self.last[k]

    def open_failures(self) -> list[str]:
        """Failures not followed by progress — for the end-of-run line."""
        return [f"{self.last[k]} ({n}×)" if n > 1 else self.last[k]
                for k, n in self.counts.items() if k in self.last]

    def record(self, name: str, args: dict, err: str, lw: "LoopState") -> tuple[str, str | None]:
        """One failure. Returns (note for the model, notice for the user or
        None). Blocks / stops through the loop guard (lw)."""
        k = error_key(name, err)
        n = self.counts[k] = self.counts.get(k, 0) + 1
        call = exact_call(name, args)
        tried = self.tried.setdefault(k, [])
        if call not in tried:
            tried.append(call)
        cause = likely_cause(err)
        short = " ".join(err.split())[:160]
        self.last[k] = f"{name}: {short[:90]}"
        if n == 1:
            return (f"[FAILED] {name}: {short}\nLikely cause: {cause}. Find the cause before "
                    "doing anything else — don't run it again unchanged.", None)
        ways = "; ".join(_short(t.split(" ", 1)[-1]) for t in tried[-3:])
        if n < FAIL_BLOCK:
            return (f"[SAME ERROR ×{n}] {name} failed the same way again: {short}\n"
                    f"Tried: {ways}. Running it again the same way will fail the same way. "
                    f"Likely cause: {cause}. Now: (1) say what causes it, (2) fix that cause or "
                    "do it a different way, (3) if you can't, tell the user what is blocking "
                    "and what they can do.",
                    f"{name} failed twice with the same error ({short[:90]}) — Aura told the "
                    "model to find the cause and change its approach")
        if n < FAIL_STOP:
            for t in tried:
                lw.blocked["call:" + t] = f"it failed {n} times with the same error ({short[:80]})"
            return (f"[BLOCKED] {name} has failed {n} times with the same error: {short}\n"
                    f"Those calls are refused now. Likely cause: {cause}. Do it a different way, "
                    "or stop and tell the user what is blocking and what they can do.",
                    f"{name} keeps failing ({short[:90]}) — those calls are blocked; the model "
                    "must change its approach or tell you")
        if not lw.stop_reason:
            lw.stop_reason = (f"stopped: {name} keeps failing with the same error — {short[:200]}. "
                              f"Tried {len(tried)} way(s): {ways}. Likely cause: {cause}. "
                              "Fix that (or tell Aura another way to do it), then /retry")
        return (f"[STOPPED] {name} failed {n} times with the same error.", None)

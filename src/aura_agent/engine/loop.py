"""engine/loop.py — the agent loop: LLM proposes, mediator disposes.

Scope: single-agent tool loop with streaming, tool-call execution through the
mediator, kill-switch polling, and audit-everything. Governor/graph land later;
this file stays the spines they attach to.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import hud
from ..hud import SpinDuring
from ..kv_cache import align_messages, cache_usage_fields
from ..llm import LLMClient
from ..mediator import BlastRadius, Mediator, Violation
from ..receipts import render_summary as render_receipt_summary

# Todo discipline instruction (import late to avoid a circular import — todo.py
# only imports mediator, but keep the pattern consistent)
from ..todo import TODO_INSTRUCTION
from ..todo import load_items as _load_todos
from ..tools.artifacts import wrap_result
from .governor import Governor
from .reviewer import Reviewer
from ..eventlog import log_swallowed
from ..tool_names import (BROWSER_RUN, DELEGATE, FILE_WRITERS, MCP_PREFIX, no_replay,
                          READ_FILE, RUN_CHECKS, RUN_COMMAND, WEB_CRAWL, WEB_SEARCH)


@dataclass
class LoopConfig:
    max_steps: int | None = None   # None = NO step cap: the run
                                   # ends when the model finishes or the
                                   # context governor intervenes. An int keeps
                                   # the legacy wall (test fixtures, golden
                                   # mini-loops).
    max_tool_output: int = 12_000          # Chars; hard cap, metadata tells truth
    _run_settings: object | None = None    # RunSettings handle (for budget)
    system_prompt: str | None = None


@dataclass
class StepRecord:
    step: int
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    error: str | None = None
    streamed: bool = False
    gate_missing: list = field(default_factory=list)  # Missing names at final exit
    receipt: dict | None = None         # Run receipt (status basis)
    receipt_summary: str = ""           # 5-line end-of-run summary
    # Every exit carries diff-contract facts.
    diff_emitted: bool = False          # A ```diff block left the model
    diff_source: str = ""               # Model | engine-salvage | aura-snapshot | none
    diff_checked: bool = False          # A source looked and found nothing changed
    work_diff: str = ""                 # The diff text the engine guarantees
    proof: dict | None = None           # Run proof before the final answer


# --------------------------------------------- files the task names
# The names below are still read to REPORT what is missing.
def _task_deliverable_names(task):
    """Fix #2 (gate was blind): deliverable names parsed from the task OWN words -
    only imperative write-verbs count ("Write X", "Produce X", "Log every
    action to X"). Bare mentions (fix targets, input files, placeholders like
    $X.YZ, section headers, run hints) must NOT become gate requirements.
    """
    import re
    if not task:
        return []
    seen, out = set(), []
    patterns = [
        r"(?:write|produce|create|save|generate)\s+(?:every\s+|a\s+|an\s+|the\s+)?"
        r"(`[^`]+`|[A-Za-z0-9_.\-]+)",
        r"log\s+every\s+action\s+to\s+(`[^`]+`|[A-Za-z0-9_.\-]+)",
        r"brief\s+(`[^`]+`|[A-Za-z0-9_.\-]+)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, task, re.IGNORECASE):
            name = m.group(1).strip().strip("`'\"")
            if not name or "/" in name or name.endswith("/"):
                continue
            if "." not in name:
                continue
            if "<" in name or ">" in name or name.startswith("$"):
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


QUESTION_ROUNDS = 4     # a question: at most this many rounds of looking, then the answer


def _silent_skip_check(root, since: float = 0.0, chat: str = "",
                       force: bool = False) -> list[str]:
    """Silent-skip guard (LOCAL, zero tokens).

    Reads todos.jsonl (the externalized plan the model itself wrote) and
    returns ids+text of items still pending/in_progress. Empty list = all plan
    steps closed (or no plan was made — single-step tasks unaffected).
    """
    try:
        items = _load_todos(root, chat, force)
    except Exception:
        return []
    # A one-step plan made for this task is the task itself; the final answer
    # closes it (files the task named but didn't write, and the proof, still
    # catch a claimed change that wasn't made)
    if since:
        from ..todo import made_at
        made = made_at(root, chat)
        new = [it for it in items if made.get(it.get("id"), 0) >= since]
        older_open = [it for it in items if it not in new
                      and it.get("status") in ("pending", "in_progress")]
        if len(new) == 1 and not older_open:
            return []
    out = []
    for it in items:
        if it.get("status") in ("pending", "in_progress"):
            out.append(f"{it.get('id', '?')} {str(it.get('text', ''))[:40]}")
    return out[:5]          # Cap: the nudge stays one line


_SKIP_GUARD_BUDGET = 2    # Nudge at most twice per run, then stand down
_REVIEW_BUDGET = 3        # 1.4: at most 3 reviewer calls per run

# 2.6: bug fixes start by reproducing the bug. A nudge, not a gate (some
# fixes can't be tested); the receipt records whether it happened.
_BUGFIX_RE = re.compile(
    r"\b(fix|fixes|fixing|bug|bugs|buggy|broken|crash|crashes|crashing|"
    r"regression|traceback|exception|fails|failing|failure|"
    r"doesn'?t work|not working|wrong (?:result|output|answer))\b", re.I)
REPRO_FIRST = (
    "[REPRODUCE FIRST] This looks like a bug fix. Before changing the code: "
    "write a small test that shows the bug (or find an existing one), run "
    "it and see it FAIL. Then fix the code and run it again to see it pass. "
    "If the bug can't be shown by a test, say why in your answer.")
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]+$|"
    r"\.(test|spec)\.[^/]+$", re.I)


def looks_like_bugfix(task: str) -> bool:
    return bool(_BUGFIX_RE.search(task or ""))


def _wrapped_already(data: bytes) -> bool:
    """A tool result that already went through the output limit (it says
    truncated + artifact_ref at its top level)."""
    if data[:1] != b"{" or b'"artifact_ref"' not in data:
        return False
    try:
        d = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(d, dict) and "truncated" in d and "artifact_ref" in d


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(str(path or "").replace("\\", "/")))


# "don't touch config/", "do not change the tests", "without modifying x.py"
_DONT_TOUCH = re.compile(
    r"\b(?:don'?t|do not|never|without)\s+(?:touch|chang|modif|edit|delet|renam|mov|rewrit)\w*"
    r"\s+(?:the\s+|any\s+|my\s+)?([\w./\\-]+)", re.I)


def dont_touch(task: str) -> list[str]:
    """What the user said not to change, in their words (a path, a folder,
    'tests').
    """
    out = []
    for m in _DONT_TOUCH.finditer(task or ""):
        w = m.group(1).rstrip("./\\").lower()           # ".env" stays ".env"
        w = w[2:] if w.startswith("./") else w
        if w and w not in ("it", "that", "this", "them", "anything", "something") and w not in out:
            out.append(w)
    return out


def _exit_code_of(raw) -> int | None:
    """exit code from a run_command / run_checks result, or None."""
    try:
        d = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    except (TypeError, ValueError):
        return None
    code = d.get("exit_code")
    return code if isinstance(code, int) else None
# 1.11: tools whose output carries content nobody in the session authored
# (web pages, files from a cloned repo, command output, MCP servers) — the
# routes prompt injection arrives by. MCP tools match by the mcp__ prefix.
_EXTERNAL_TOOLS = frozenset({WEB_CRAWL, WEB_SEARCH, BROWSER_RUN, READ_FILE, RUN_COMMAND,
                             "search_files", "process_start", "process",
                             "history_search"})   # Old chats hold old outside text
# the agent's own bookkeeping: writes, but not changes to the user's work —
# recording a result or ticking a todo must not make the reads behind it
# look stale to the proof
_BOOKKEEPING = frozenset({"finding_add", "state_write", "todo_write",
                          "notes_write", "memory_write"})
# a shell command that runs the project's tests (decision 1.4: passing
# tests can prove a result, judged on pass/fail when re-run)
_TEST_CMD = re.compile(
    # any python interpreter, bare or by path, quoted or not
    # (python3, .venv/bin/python, "C:/…/python.exe")
    r"^\s*(?:\"?[^\"\s]*python[\w.]*(?:\.exe)?\"?\s+-m\s+)?"
    r"(?:pytest|unittest|tox|nox)\b"
    r"|^\s*(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test\b"
    r"|^\s*(?:go|cargo|dotnet|mvn|gradle|\./gradlew)\s+test\b"
    r"|^\s*make\s+(?:test|check)\b")


# Bug: the model checked its fix with `python -c "from app import add; assert
# add(2, 3) == 5"`, it passed, and the proof said "no reads or test runs since
# the last change" — and blamed the model. An inline check (python -c / node -e
# with an assertion) that writes nothing is a test run: re-running it proves
# the result
_INLINE = re.compile(r'^\s*"?[^"\s]*(?:python[\w.]*|py|node|ruby)(?:\.exe)?"?\s+-[ce]\s', re.I)
_ASSERTS = re.compile(r"\bassert\b|console\.assert|require\(['\"]assert", re.I)
_WRITES = re.compile(r"open\([^)]*['\"][wax]b?\+?['\"]|\.write\(|\bos\.(?:remove|unlink|rmdir|"
                     r"rename|replace|system)|shutil\.|subprocess|\bunlink\(|writeFile|"
                     r"rmSync|urllib|requests\.|\bhttp\b|>\s*[\w./\\]", re.I)


def _is_inline_check(cmd: str) -> bool:
    c = str(cmd or "")
    return bool(_INLINE.match(c) and _ASSERTS.search(c) and not _WRITES.search(c))


def _is_test_run(name: str, args) -> bool:
    if not isinstance(args, dict):
        return False
    if name == RUN_CHECKS:
        return str(args.get("kind") or "test").lower() == "test"
    cmd = str(args.get("cmd", ""))
    return name == RUN_COMMAND and bool(_TEST_CMD.match(cmd) or _is_inline_check(cmd))


# ------------------------------------------------------------ diff contract
def _track_model_diff(session) -> str:
    """Scan the session tape for the LAST ```diff block the model emitted. Returns
    the extracted diff ('' when the model never wrote one). Cheap local scan —
    zero tokens.
    """
    try:
        texts = []
        for m in session.messages:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if c and "```diff" in str(c):
                texts.append(str(c))
        if not texts:
            return ""
        from ..contracts.session_location import extract_diff_block
        return extract_diff_block(texts[-1])
    except Exception:
        return ""


def _prompt_suppresses_writes(task):
    """TRUE when the CURRENT operator instruction explicitly forbids
    creating/writing files ("do not create files", "no files", "without
    writing", "answer only", "only answer", "reply only"). When set, the
    deliverable gate must NOT demand TASK.md-named artifacts: the instruction
    outranks the task file (resurrection-matrix v2/v4/v5 — the gate
    resume-wrote files the prompt had forbidden 58/60 apparent hits).
    """
    import re
    if not task:
        return False
    t = task.lower()
    negative = [
        r"do\s+not\s+(create|write|make|produce|generate)\b",
        r"don't\s+(create|write|make|produce|generate)\b",
        r"\bno\s+files?\b",
        r"without\s+(creating|writing|any\s+files)",
        r"(answer|reply|respond)(\s+\w+){0,3}\s+only\b",
        r"only\s+(answer|reply|respond|tell|show|list|count|say)\b",
        r"nothing\s+else\b",
        r"do\s+not\s+write\s+any\s+files",
    ]
    return any(re.search(p, t) for p in negative)


_MAKE_VERB = re.compile(
    r"\b(write|writes|writing|wrote|written|create|creates|creating|created|"
    r"save|saves|saving|saved|make|makes|making|made)\b(?!\s+sure)", re.I)


WRITE_EARLY_STEPS = 8          # A named file still missing by now → a note
GOAL_REMINDER_FROM = 6         # Intent anchoring: from this step on (when switched on)


def _parse_deliverable_names(task: str) -> list[str]:
    """Filenames the task names as deliverables: backticked or plain tokens
    that look like files (carry a dot-suffix), excluding directories
    (trailing '/'), deduped, order preserved."""
    import re
    if not task:
        return []
    seen, out = set(), []
    # Plain tokens keep their folders; the lookbehind keeps URL paths
    # ("https://x.org/a.html") and absolute paths out
    for mt in re.finditer(r"`([^`]+)`|(?<![\w:/.\-])((?:[\w.\-]+/)*"
                          r"[\w.\-]+\.[\w\-]+)", task):
        tok = (mt.group(1) or "", mt.group(2) or "")
        if not tok[0]:
            before = re.split(r"[\n!?]|\.\s", task[:mt.start()])[-1]
            if not _MAKE_VERB.search(before):
                continue
        name = (tok[0] or tok[1]).strip().strip("'\"")
        if not name or name.endswith("/") or "/" in name and not Path(name).parent == Path("."):
            # backticked paths with dirs still count if they end in a suffix
            if not name or name.endswith("/"):
                continue
        if "." not in name and "`" not in tok[0]:
            continue
        # IPs and host:port pairs never count as filenames.
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){1,3}(?::\d+)?", name):
            continue
        # Finding (Oct 3). Bare dotted fragments need a KNOWN FILE EXTENSION to
        # count; BACKTICKED names are explicit operator intent and always count
        # (test_r6_gate: `actions.log` is a deliverable).
        if not (tok[0] or re.search(
                r"\.(py|json|txt|md|cfg|toml|ya?ml|rst|sh|html?|js|"
                r"csv|ini|tsx?|jsx|ipynb|log)$", name, re.I)):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _blank_file(p: Path) -> bool:
    """An empty or whitespace-only file is not a deliverable."""
    try:
        if p.stat().st_size == 0:
            return True
        with open(p, "rb") as f:
            return not f.read(65536).strip()
    except OSError:
        return True


def _missing_deliverables(work_dir, names: list[str]) -> list:
    try:
        wd = Path(work_dir)
        return [n for n in names
                if not (wd / n).is_file() or _blank_file(wd / n)]
    except Exception:
        return []


def _grade_of_safe(name, spec, args=None, overrides=None):
    """Tool → autonomy grade; falls back to G2 (execute) when unknown. args
    matter: run_command is G3 when the command reaches outside.
    """
    try:
        from ..autonomy import grade_of
        blast = spec.blast_radius.value if spec else "execute"
        return grade_of(name, blast, overrides=overrides, args=args)
    except Exception:
        return "G2"


# BUILD 2: shell commands that can destroy state — auto-checkpoint first
_DESTRUCTIVE_WORDS = ("rm", "rmdir", "mv", "dd", "mkfs", "truncate",
                      "shutdown", "reboot", "chmod", "chown")


def _postcondition_probe(result: dict, tc: dict) -> str:
    """Verify-before-retry (LOCAL, no LLM, no tokens).

    On an ambiguous failure (timeout / unknown / connection), cheaply check
    whether the intended effect ALREADY landed, from evidence on disk: -
    write_file / edit_file: does the target file exist now? - run_command: the
    timeout error itself proves nothing about the side effect; the audit trail
    (mediator) is the record — a second entry for the same command signature
    means a retry would DUPLICATE it. Returns a one-line fact string for the
    relabel note, or None (nothing provable locally — normal retry advice
    stands).
    """
    if result.get("status") == "ok":
        return None
    err = str(result.get("error", "")).lower()
    ambiguous = any(k in err for k in
                    ("timeout", "timed out", "unknown", "connection"))
    if not ambiguous:
        return None
    name = tc.get("name", "")
    args = tc.get("parsed_arguments") or {}
    try:
        if name in FILE_WRITERS:
            p = Path(args.get("path", ""))
            if p.is_file():
                return (f"local check: '{args.get('path')}' EXISTS on disk —"
                        " the write likely landed; verify content before any"
                        " retry, do NOT blindly re-write")
            return (f"local check: '{args.get('path')}' absent — retry is "
                    "safe (no partial state observed)")
        if name == RUN_COMMAND:
            return ("side effect unverified after timeout — inspect target "
                    "state with a read-only call BEFORE re-running the same "
                    "command (duplicate actions can harm the target)")
    except Exception:
        return None
    return None


DEFAULT_SYSTEM = ("""You are Aura — a general-purpose AI agent that works
on the user's machine: run commands, read and write files, crawl a given
website, and help with whatever they ask. You are talking with a human in an
interactive session. Rules:
- Answer directly and conversationally first — short, natural, human.
  Greetings get greetings; questions get answers.
- Prove what you claim: record results about the user's work (a bug
  fixed, a feature working, tests passing) with finding_add, right after
  the reads or test run that show it — not answers, recollections or
  plans. Aura re-runs them; if it says "not proven", try once more at
  most, then say so in your answer; only proven results enter the report.
- Stay inside scope; Aura enforces it and refuses anything outside.
- Prefer evidence over guessing; validate before claiming success.
- If a fact you need isn't given and you can't find it, say it's missing
  instead of filling it in.
- When the objective is achieved, reply with a concise final report.
""")

# 2.3 CANDIDATE generic prompt (AURA_PROMPT=generic) — not the default. Left
# out on purpose: "skip plans for simple tasks" (planning_instruction already
# does it per task), "irreversible actions need the user's OK" (false at L3 —
# the mediator decides), personas, capitals. Kept short (instruction count
# lowers compliance, most for small models). Becomes the default only if `aura
# eval` shows it scores at least as well.
GENERIC_SYSTEM = ("""You are Aura — a general-purpose AI agent that works
on the user's machine: run commands, read and write files, read web pages,
and help with whatever they ask. You are talking with a human in an
interactive session.

Rules:
- Answer directly and conversationally first — short, natural, human.
  Greetings get greetings; questions get answers.
- Keep going until the request is fully handled, then end your turn with a
  concise final report: what changed (file paths), how you verified it,
  anything left open. If you are blocked, or the request is ambiguous in a
  way that matters, ask one short question instead of guessing.
- Use your tools to find things out — never guess file contents, commands
  or results. Read the relevant code, tests and config before changing it.
- Prove what you claim: record results about the user's work (a bug
  fixed, a feature working, tests passing) with finding_add, right after
  the reads or test run that show it — not answers, recollections or
  plans. Aura re-runs them; if it says "not proven", try once more at
  most, then say so in your answer; only proven results enter the report.
- Stay inside scope; Aura enforces it and refuses anything outside.
  Never work around a refusal or a "no" from the user.

When changing code:
- Follow the project's existing conventions, style and libraries.
- Fix the root cause with the smallest change that solves it. Don't fix
  unrelated problems — mention them. Never undo changes you didn't make.
- Check your work with run_checks (the project's own test / lint /
  typecheck commands), the tests closest to your change first. Don't claim
  success until they pass — or say plainly why you couldn't run them.

Remember: keep going until it's done, verify before you claim it, keep
answers short.""")


# ------------------------------------------------- change 4: plan class
def classify_scope(task: str, root=None) -> str:
    """Plan classifier (LOCAL, zero tokens, zero model calls).

    Decides the planning posture BEFORE the first LLM call: "multi" —
    multi-machine / multi-phase task: full-horizon plan pays for itself (: 2-3x
    fewer tokens than reactive re-planning) "single" — focused quick task: skip
    the plan ritual entirely (todo_write for 1-2 steps is pure overhead)
    Heuristics on the task text only.
    """
    if not task:
        return "single"
    t = task.lower()
    # several machines named by address (ops work across servers)
    hosts = set()
    try:
        import re as _re
        hosts = set(_re.findall(
            r"\b\d{1,3}(?:\.\d{1,3}){3}\b", task))
    except Exception:
        pass
    multi_signals = [
        len(hosts) >= 2,
        any(w in t for w in ("each host", "all hosts", "every host",
                             "across the", "phases", "fleet")),
        t.count(" and ") >= 2 and len(t) > 200,   # Long compound task
    ]
    return "multi" if any(multi_signals) else "single"


def planning_instruction(task: str, root=None) -> str:
    """Scope-classified planning posture. Zero-token accounting: - multi -> ""
    (TODO_INSTRUCTION in the static prompt already mandates the full-horizon
    plan; a second line would be pure add) - single -> one short line that
    SUPPRESSES the plan ritual. Net negative: without it, todo_write creates
    items and the render block then injects EVERY turn.
    """
    # the plan is the user's switch now: off = no plan talk at all; on = the
    # per-step plan block says what to do
    return ""


class AgentLoop:
    def __init__(self, llm: LLMClient, mediator: Mediator,
                 tool_schemas: list[dict], dispatch: dict[str, Callable],
                 cfg: LoopConfig | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 governor: "Governor | None" = None,
                 root: Path | None = None):
        self.llm = llm
        self.m = mediator
        self.schemas = tool_schemas
        self.dispatch = dispatch
        self.cfg = cfg or LoopConfig()
        self.on_event = on_event or (lambda kind, data: None)
        self.governor = governor
        self.root = root or Path(".")
        self._skip_guard_used = 0    # Silent-skip guard budget per run
        self.reviewer: Reviewer | None = None  # Wired by cli when graph present
        self.failure_streak = 0
        self._run_failures: list[dict] = []
        self._reviews_left = 0
        # Roadmap 1.5 (pending decision). Wiring the reviewer (1.4) must not
        # silently switch it on.
        self.inject_graph = False
        self.ctx_source = "default"    # .1: probe/override/table/cache/default
        self.agents_md: str | None = None  # Loaded instructions filename
        self.last_usage: dict = {}     # {prompt, completion} of last call
        # Cache alignment: default OFF (revert of un-discussed auto-on —
        # pending the settings discussion). cache_mode can still be set
        # explicitly (profile / future settings).
        self.cache_mode = "off"        # Off|prefix|block|explicit
        self.settings = getattr(cfg, "_run_settings", None)  # RunSettings
        self.cache_hits = 0            # Measured cache telemetry
        self.cache_misses = 0
        self._receipt = None           # Per-run receipt (set in run)
        self._active_spin = None       # Set only outside the TUI; read in finally
        self.intent_scope: dict = {}   # scope.json for intent contracts
        self.intent_contract = None    # Active IntentContract
        self.intent_calls: list[dict] = []  # Calls this turn (drift audit)

    # ------------------------------------------------------- persistence
    def session_persist_compact(self, session, c) -> None:
        """Write the compaction into the session as an append-only record and
        align the live message list — replay on resume stays compacted."""
        try:
            session.record_compact(c.summary_msg["content"],
                                   kept=len([m for m in c.kept
                                             if m["role"] != "system"]),
                                   dropped=c.dropped)
            session.messages = list(Governor.as_messages(c))
        except Exception as _swallowed:
            log_swallowed("loop.compaction", _swallowed)  # Compaction must never kill the loop
        # The summary replaced earlier read outputs: a re-read must send the
        # file again, never "still above"
        self._reads_seen = {}
        # the cache starts over at a summary anyway: put away tool groups
        # not used since the last one (option tools.park)
        self._park_tools()
        # Compaction safety: once the history behind finished tasks is
        # summarized away, mark them superseded so a half-remembered fragment
        # in the summary is never resumed (open tasks stay open)
        try:
            from ..taskstate import supersede_tasks
            supersede_tasks(self.root, None)
        except Exception as _swallowed:
            log_swallowed("loop.ledger_supersede", _swallowed)

    def compact_now(self, session, focus: str = "") -> dict:
        """Manual compact [focus]: LLM-driven summary when the provider is
        reachable, heuristic fallback otherwise. Persists either way.
        """
        msgs = list(session.messages)
        n_pruned = 0
        if self.governor:
            msgs, n_pruned = self.governor.prune(msgs)
            if n_pruned:
                session.messages = msgs
        summary_text, by = "", ""
        try:                                    # LLM structured summary
            old = [m for m in msgs if m["role"] != "system"]
            old = old[:max(0, len(old) - self.governor.keep_recent)] \
                if self.governor else old
            if old:
                blob = "\n".join(f"[{m['role']}] {str(m.get('content', ''))[:400]}"
                                 for m in old)
                # The helper model writes it when one is set and passed its
                # check; otherwise the main model
                from ..helper import client as _helper_client
                _hl, by = _helper_client(self.llm)
                res = self._side_chat(
                    _hl, "Summarize this agent conversation segment for "
                    "handoff. Preserve verbatim: decisions, file paths, "
                    "identifiers, URLs, error messages and stack "
                    "traces, test names. Never copy secrets, keys or "
                    "passwords. Drop noise. Max 20 lines.", blob[:16000])
                summary_text = res.content or ""
        except Exception:
            summary_text = ""                   # Heuristic fallback below
        c = (self.governor.compact_with_summary(msgs, summary_text, focus=focus)
             if self.governor else None)
        if c is None:
            return {"pruned": n_pruned, "dropped": 0, "llm": False}
        self.session_persist_compact(session, c)
        if summary_text and getattr(self, "_receipt", None) is not None:
            self._receipt.note(f"summary written by {by}")
        return {"pruned": n_pruned, "dropped": c.dropped,
                "verbatim": len(c.verbatim_lines), "llm": bool(summary_text),
                "by": by if summary_text else ""}

    # ------------------------------------------- 0.1: context-overflow recovery
    _OVERFLOW_CLIP_CHARS = (32_000, 8_000)   # (normal, tight)

    def _overflow_compact(self, session, messages: list[dict],
                          tight: bool) -> list[dict]:
        """Shrink the context after the provider rejected it as too long.
        normal: prune + compact + clip any message over 32k chars. tight: keep
        only the last 4 messages and clip at 8k. Clipping matters even in the
        normal pass: one giant RECENT tool output (the default preset allows
        1 MB inline) survives compaction untouched. The session tape is
        compacted too, so the next turn starts small."""
        g = self.governor
        saved = g.keep_recent
        try:
            if tight:
                g.keep_recent = min(saved, 4)
            msgs, _ = g.prune(list(messages))
            out = Governor.as_messages(g.compact(msgs))
            tape, _ = g.prune(list(session.messages))
            self.session_persist_compact(session, g.compact(tape))
        finally:
            g.keep_recent = saved
        limit = self._OVERFLOW_CLIP_CHARS[1 if tight else 0]
        out = [Governor.clip_message(m, limit) for m in out]
        session.messages = [Governor.clip_message(m, limit)
                            for m in session.messages]
        return out

    def _note_for_proof(self, name: str, args, raw) -> None:
        """Remember read-only results (to re-check later) and where the last
        change happened. Never raises.
        """
        try:
            from .validator import output_digest
            from ..mediator import BlastRadius
            spec = self.m.tools.get(name)
            self._seq = getattr(self, "_seq", 0) + 1
            # 2.6: did a run FAIL before the first change to non-test code?
            if name in (RUN_COMMAND, RUN_CHECKS) and \
                    not getattr(self, "_src_changed", False):
                code = _exit_code_of(raw)
                if code not in (None, 0):
                    self._repro_seen = True
            if name in FILE_WRITERS and isinstance(args, dict) and \
                    not _is_test_path(args.get("path", "")):
                self._src_changed = True
            # the answer is compared with these at the end (claim_check)
            if name in (RUN_COMMAND, RUN_CHECKS, "run_code"):
                self._ran_anything = True
            if not getattr(self, "_reached_outside", False):
                # something that sends out really ran: the same calls the
                # approval grade marks as reaching outside (G3), or an MCP tool
                from ..autonomy import grade_of
                from ..tool_names import MCP_PREFIX
                if str(name).startswith(MCP_PREFIX) or grade_of(
                        name, "read", args=args if isinstance(args, dict) else None) == "G3":
                    self._reached_outside = True
            if name in FILE_WRITERS and isinstance(args, dict) and args.get("path"):
                from ..claim_check import added_text
                added = getattr(self, "_added", [])
                if len(added) < 60:
                    added.append((str(args["path"]), added_text(name, args)))
                self._added = added
            if _is_test_run(name, args):
                # running the tests CHECKS the work, it doesn't change it —
                # remembered so a recorded result can be proven by them
                self._tests_seq, self._test_call = self._seq, (name, args)
                self._tests_ok = _exit_code_of(raw) in (0,)     # The end-of-run report
                return
            if no_replay(name, args):
                return                     # Output differs every run
            if name in _BOOKKEEPING:
                return     # The agent's own notes, not a change to the work
            if spec is not None and spec.blast_radius == BlastRadius.READ:
                reads = getattr(self, "_reads", [])
                reads.append({"tool": name, "args": dict(args or {}),
                              "digest": output_digest(raw), "seq": self._seq})
                self._reads = reads[-200:]
            else:
                self._last_change_seq = self._seq
                if name in FILE_WRITERS:
                    self._wrote_files = True
                    if isinstance(args, dict) and args.get("path"):
                        self._changed_files = getattr(self, "_changed_files", [])
                        if str(args["path"]) not in self._changed_files:
                            self._changed_files.append(str(args["path"]))
                        if _is_test_path(args["path"]):
                            # EvilGenie / "Verification Horizon" (2026): agents
                            # edit tests until they pass — said in the report
                            self._tests_edited = getattr(self, "_tests_edited", [])
                            if str(args["path"]) not in self._tests_edited:
                                self._tests_edited.append(str(args["path"]))
                        self._lint_pending = getattr(self, "_lint_pending", set())
                        self._lint_pending.add(str(args["path"]))
        except Exception as _swallowed:
            log_swallowed("loop.proof_note", _swallowed)

    def _model_trouble(self, why: str) -> None:
        """When it isn't giving proper answers, say so once per run, with the
        choices, and let the user decide.
        """
        if getattr(self, "_trouble_told", False):
            return
        self._trouble_told = True
        model = getattr(getattr(self.llm, "p", None), "model", "the model")
        self.on_event("notice", {"text": (
            f"{model} isn't giving proper answers here ({why}). Your choice: "
            "/model to try another model, /retry to try again, or tell it what "
            "to do differently.")})

    def _unchanged_reread(self, name: str, args) -> str | None:
        """Fix list #4: read_file of a file unchanged since this run read it, with
        that output still in the conversation (not trimmed) → say so instead of
        sending it again. Trimmed or changed → read normally.
        """
        if name != READ_FILE or not isinstance(args, dict) or not args.get("path"):
            return None
        try:
            key = json.dumps({k: args.get(k) for k in ("path", "offset", "limit")}, sort_keys=True)
            p = Path(getattr(self, "work_dir", None) or self.root) / str(args["path"])
            digest = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else None
            seen = getattr(self, "_reads_seen", None)
            if seen is None:
                seen = self._reads_seen = {}
            prev = seen.get(key)
            seen[key] = (digest, getattr(self, "_seq", 0))
            if digest is None or prev is None or prev[0] != digest:
                return None
            ws = getattr(self, "_workset", None)
            label = f"read_file {str(args['path']).replace(chr(92), '/').lstrip('./')}"
            if ws is not None and any(w[0] == label for w in ws.why.values()):
                return None                    # Its earlier output was trimmed
            return (f"{args['path']} is unchanged since you read it earlier in this task — "
                    "that output is still above. (Read a range with offset/limit if you "
                    "need one part again.)")
        except (OSError, ValueError, TypeError):
            return None

    _REPEAT_STOP = 10         # Same exact call this many times in a row → skip

    def _repeat_stop(self, name: str, args) -> str | None:
        """Fix list #14 (panel verdict: runs re-ran one command 79–182×, 46% of a
        's tokens): the EXACT same call (same arguments, not digit-blurred —
        chunked reads must never trip it) already run _REPEAT_STOP times in a
        row is skipped, and the model is told why. The 3 / 5 mentor nudges come
        first; this is the backstop.
        """
        try:
            sig = name + " " + json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            sig = f"{name} {args!r}"
        if sig == getattr(self, "_rep_sig", None):
            self._rep_n += 1
        else:
            self._rep_sig, self._rep_n = sig, 1
        if self._rep_n <= self._REPEAT_STOP:
            return None
        self.on_event("loopwatch", {"tool": name, "hard_stop": True,
                                    "same_run": self._rep_n - 1})
        return (f"skipped — this exact {name} call already ran "
                f"{self._rep_n - 1} times in a row; running it again will "
                "not change anything. Do something different, or tell the "
                "user what is blocking you.")

    _PROOF_STEPS = 8          # Most recent reads a result is checked against

    def _prove_finding(self, raw):
        """(THE differentiator, built but never called): a recorded result is
        proven by re-running the read-only steps it rests on — the reads made
        since the last change, newest first, up to _PROOF_STEPS — through the
        mediator, and comparing each output with what the model saw. Pass →
        proof attached, status validated. The model is told the outcome in the
        same tool reply. Never raises.
        """
        try:
            rec = json.loads(raw) if isinstance(raw, str) else dict(raw)
            fid = rec.get("id")
            store = getattr(self.m, "_findings", None)
            if not fid or store is None:
                return raw
            since = getattr(self, "_last_change_seq", -1)
            chain, seen = [], set()
            for r in reversed(getattr(self, "_reads", [])):
                if r.get("seq", 0) <= since or len(chain) >= self._PROOF_STEPS:
                    break
                key = (r["tool"], json.dumps(r.get("args", {}), sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                chain.append({"tool": r["tool"], "args": r.get("args", {}),
                              "digest": r.get("digest")})
            chain.reverse()
            # decision 1.4: tests run since the last change are part of the
            # proof — re-run the same test command, judged on pass/fail
            tested = getattr(self, "_tests_seq", -1) > since

            # . A task that changed nothing is an answer — the reads above are
            # its evidence; the reply says stop here.
            answer_only = since < 0 and not getattr(self, "_wrote_files", False)

            def done(proven: bool, text: str):
                # only a PROVEN answer stops here — "bug fixed" with nothing
                # changed stays not proven (the normal path below)
                if answer_only and proven:
                    text += (" — this task changed nothing, so your answer needs no more "
                             "checks or records: reply to the user now")
                    rec["proof"] = text
                    if proven:                       # The end-of-run report
                        self._proven = getattr(self, "_proven", 0) + 1
                    self.on_event("result_recorded", {"id": fid, "proven": proven,
                                                      "steps": len(chain)})
                    return json.dumps(rec, ensure_ascii=False)
                if not proven:
                    # One more try at most, then the run moves on.
                    self._unproven = getattr(self, "_unproven", 0) + 1
                    if self._unproven >= 2:
                        self._model_trouble("its results keep failing the proof check")
                    text += (" — you may record it once more after a fresh read or test "
                             "run; if it still isn't proven, say so in your answer"
                             if self._unproven == 1 else
                             " — don't record it again: say in your final answer that "
                             "it isn't proven, and move on")
                rec["proof"] = text
                if proven:                       # The end-of-run report
                    self._proven = getattr(self, "_proven", 0) + 1
                self.on_event("result_recorded", {
                    "id": fid, "proven": proven,
                    "steps": len(chain) + (1 if tested else 0)})
                return json.dumps(rec, ensure_ascii=False)

            # Aura never calls a result proven while a file it rests on (a
            # file re-read for it, or its target) does not parse — the
            # re-read would only show the file still says the same thing
            broken = self._unparsable(chain, rec)
            if broken:
                return done(False, f"not proven: {broken} does not parse — "
                                   "the result rests on code that can't run")
            if not chain and not tested:
                return done(False, "not proven: no reads or test runs since "
                                   "the last change to re-check — read or "
                                   "test what shows the result, then record "
                                   "it again")
            if tested:
                passed, why = self._rerun_tests()
                if not passed:
                    return done(False, f"not proven: {why}")
            from .validator import Validator
            v = Validator(self.m, store, self.root)
            if chain:
                v.save_chain(fid, chain)
                res = v.validate(fid, self.dispatch)
                if not res.ok:
                    return done(False, f"not proven: {res.error or 'a step failed'}")
            else:                   # Tests alone are the proof
                test_step = {"tool": self._test_call[0],
                             "args": self._test_call[1]}
                v.save_chain(fid, [test_step])
                store.attach_proof(fid, v._make_proof(
                    fid, [test_step],
                    [{"step": 0, "tool": test_step["tool"], "status": "ok"}],
                    "tests passed when re-run"))
            return done(True, "proven — " + " + ".join(
                ([f"{len(chain)} read(s) re-run with the same output"]
                 if chain else [])
                + (["the tests pass again"] if tested else [])))
        except Exception as _swallowed:
            log_swallowed("loop.prove_finding", _swallowed)
            return raw

    def _unparsable(self, chain: list, rec: dict) -> str | None:
        """The first file a result rests on that does not parse, as
        "serializers.py (line 2: unmatched '}')", or None."""
        from ..tools.fs import parse_problem
        wd = Path(getattr(self, "work_dir", None) or self.root)
        names = [s["args"].get("path") for s in chain
                 if s.get("tool") == READ_FILE
                 and isinstance(s.get("args"), dict)]
        stored = next((r for r in reversed(getattr(self.m._findings,
                                                    "records", []))
                       if r.get("id") == rec.get("id")), {})
        names.append(stored.get("target") or rec.get("target"))
        for n in dict.fromkeys(x for x in names if isinstance(x, str) and x):
            try:
                p = (wd / n).resolve()
                p.relative_to(wd.resolve())
            except (ValueError, OSError):
                continue
            why = parse_problem(p)
            if why:
                return f"{n} ({why})"
        return None

    def _rerun_tests(self) -> tuple[bool, str]:
        """Re-run the test command the agent last ran, through the mediator
        (audited, gated). Pass/fail only — timings and ordering in the
        output are allowed to differ."""
        name, args = getattr(self, "_test_call", (None, None))
        try:
            if name == RUN_CHECKS:
                # The same check the agent ran — the same tests, never widened
                # to the whole suite
                raw = self.m.execute(RUN_CHECKS, dict(args or {}, kind="test", force=True),
                                     self.dispatch[RUN_CHECKS])
                try:
                    r = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (TypeError, ValueError):
                    r = {"ran": False, "why": str(raw)[:200]}
                if not r.get("ran"):
                    return False, f"the tests could not be re-run: {r.get('why')}"
                return bool(r.get("ok")), ("" if r.get("ok") else
                                           "the tests fail when re-run")
            raw = self.m.execute(RUN_COMMAND, args, self.dispatch[RUN_COMMAND])
            code = _exit_code_of(raw)
            return code == 0, ("" if code == 0 else
                               f"the tests fail when re-run (exit {code})")
        except Exception as e:                            # noqa: BLE001
            return False, f"the tests could not be re-run: {e}"
        except Exception as _swallowed:
            log_swallowed("loop.prove_finding", _swallowed)
            return raw

    _LINT_EXT = (".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                 ".go", ".rs", ".rb", ".java", ".kt", ".c", ".cc", ".cpp",
                 ".h", ".hpp", ".cs", ".php", ".swift", ".scala", ".sh")

    def _auto_lint(self, wd) -> str | None:
        """2.5: after a step that changed code, run the project's linter
        once and return problems IN THE CHANGED FILES (old lint debt
        elsewhere is not this change's business). Skipped in strict mode
        (L1) — the operator should not be asked about checks the agent
        did not choose to run — and capped per run. Never raises."""
        try:
            paths = sorted(getattr(self, "_lint_pending", set()))
            self._lint_pending = set()
            code = [p for p in paths if p.lower().endswith(self._LINT_EXT)]
            if (not code or getattr(self.m, "assist", "L2") == "L1"
                    or getattr(self.m, "mode", "act") == "plan"
                    or getattr(self, "_lint_left", 0) <= 0):
                return None
            from ..project_checks import check_for
            check = check_for(Path(wd), "lint")
            if check is None or not check.available():
                return None
            from .validator import run_check
            res = run_check(self.m, self.dispatch, wd, "lint", tail_chars=6000)
            if not res.get("ran") or res.get("ok"):
                return None
            names = {Path(p).name for p in code}
            hits = [ln for ln in str(res.get("tail", "")).splitlines()
                    if any(n in ln for n in names)]
            if not hits:
                return None
            self._lint_left -= 1
            shown = hits[:25]
            more = (f"\n… +{len(hits) - len(shown)} more"
                    if len(hits) > len(shown) else "")
            return ("[LINT] " + str(res.get("command")) + " reports problems "
                    "in files you just changed:\n" + "\n".join(shown) + more
                    + "\nFix them if your change caused them.")
        except Exception as _swallowed:
            log_swallowed("loop.auto_lint", _swallowed)
            return None

    def _needs_proof(self, wd) -> bool:
        """Prove only runs that changed something, and only once per change."""
        if getattr(self.m, "mode", "act") == "plan":
            return False
        if getattr(self, "_last_change_seq", -1) <= getattr(self, "_proved_seq", -1):
            return False
        if getattr(self, "_wrote_files", False):
            return True
        try:
            from ..contracts.session_location import git_diff_checked
            patch, _err = git_diff_checked(wd)
            self._git_notice(_err)
            return bool(patch.strip())
        except Exception:
            return False

    def _ledger_open(self, task: str, reply: bool = False) -> None:
        """Keep the task ledger the anaphora resolver reads. A new instruction
        becomes a task; a follow-up ("continue it") re-opens the OPEN task it
        binds to; an unbound follow-up or a closed match creates nothing (the
        resolver already told the model to ask). Resolution runs BEFORE the new
        entry exists, so a new task can never bind to itself. Never raises.
        """
        self._ledger_task, self._ledger_new = None, False
        if not task or not str(task).strip():
            return
        if reply:
            try:
                from ..anaphora import DEAD
                from ..taskstate import load_ledger, upsert_task
                goal = " ".join(str(getattr(self, "_goal", "")).split())[:120]
                for e in reversed(load_ledger(self.root)):
                    if e.get("status") not in DEAD and goal and \
                            " ".join(str(e.get("desc", "")).split()) == goal[:len(e.get("desc", ""))]:
                        upsert_task(self.root, e["id"], "", "in-progress")
                        self._ledger_task = e["id"]
                        break
            except Exception as _swallowed:
                log_swallowed("loop.ledger_reply", _swallowed)
            return
        try:
            from ..anaphora import DEAD, resolve_reference
            from ..taskstate import new_task, upsert_task
            wd = self.root            # The ledger is Aura's note: notebook
            r = resolve_reference(task, wd)
            if r.get("binding"):
                if r.get("binding_status") not in DEAD:
                    upsert_task(wd, r["binding"], "", "in-progress")
                    self._ledger_task = r["binding"]
            elif not r.get("anaphoric"):
                self._ledger_task = new_task(wd, task)
                self._ledger_new = True
        except Exception as _swallowed:
            log_swallowed("loop.ledger_open", _swallowed)

    def _ledger_close(self, rec, tool_calls: int | None) -> None:
        """Run end: done on a final answer, open when interrupted or
        failed; a NEW entry from a run that used no tools is dropped (a
        question, not a task). Once per run. Never raises."""
        tid = getattr(self, "_ledger_task", None)
        if not tid:
            return
        self._ledger_task = None
        try:
            from ..taskstate import drop_task, upsert_task
            wd = self.root
            if tool_calls == 0 and getattr(self, "_ledger_new", False):
                drop_task(wd, tid)
            elif rec.content and not rec.error:
                upsert_task(wd, tid, "", "done")
            # else: stays in-progress — "continue" finds it
        except Exception as _swallowed:
            log_swallowed("loop.ledger_close", _swallowed)

    def _git_notice(self, err: str) -> None:
        """Once per run: git refusing the folder is the operator's to fix
        (the message carries the one-folder command) — say it in full."""
        if err and "dubious ownership" in err \
                and not getattr(self, "_git_warned", False):
            self._git_warned = True
            self.on_event("notice", {"text": err})

    def _discuss_switch(self, task: str) -> bool:
        """Discuss mode is on and the message asks for work ("ok fix it"): a card
        asks whether to switch discuss off and do it. True = switched off.
        Nobody to ask, or no: it stays a conversation.
        """
        ask = getattr(self.m, "asker", None)
        if ask is None or not self._check_on():
            return False
        from .. import task_view as TV
        try:
            res = self.llm.chat([{"role": "system", "content": TV.INSTRUCTIONS},
                                 {"role": "user", "content": TV.question([], task)}],
                                tools=None, temperature=0, side=True)
            kind = TV.kind_of(getattr(res, "content", "") or "", ("task", "question", "chat"), "question")
        except Exception as _swallowed:                   # noqa: BLE001
            log_swallowed("loop.discuss_switch", _swallowed)
            return False
        if kind != "task":
            return False
        yes, no = "yes — switch discuss off and do it", "no — keep talking"
        try:
            pick = ask("Discuss mode is on, and this asks for work. Switch it off and do it now?",
                       [yes, no])
        except Exception:                                 # noqa: BLE001
            return False
        if pick != yes:
            return False
        before = getattr(self.m, "_mode_before_discuss", "act")
        self.m.mode = before if before != "discuss" else "act"
        self.on_event("notice", {"text": f"discuss mode off — back to {self.m.mode}; doing it"})
        return True

    def _plan_items(self) -> list[dict]:
        """This chat's plan items — empty while the plan switch is off."""
        try:
            return _load_todos(self.root, getattr(self.m, "plan_chat", ""),
                               getattr(self.m, "plan_forced", False))
        except Exception:                                 # noqa: BLE001
            return []

    def _answered_and_ticked(self, res) -> bool:
        """The reply is a real answer and its only calls tick plan steps done
        (todo_write status done / clear_done), and nothing made for this task
        is left open — the work is finished, so the answer stands."""
        if len((res.content or "").strip()) < 40 or not res.tool_calls:
            return False
        for tc in res.tool_calls:
            a = tc.get("parsed_arguments") or {}
            if not isinstance(a, dict):
                try:
                    a = json.loads(tc.get("arguments") or "{}")
                except (TypeError, ValueError):
                    return False
            if tc.get("name") != "todo_write":
                return False
            act = str(a.get("action") or "").lower()
            if not (act == "clear_done" or (act in ("status", "set")
                                            and str(a.get("status", "")).lower() == "done")):
                return False
        try:
            from ..todo import made_at
            made = made_at(self.root, getattr(self.m, "plan_chat", ""))
            t0 = getattr(self, "_run_t0", 0.0)
            return not [i for i in self._plan_items()
                        if i.get("status") in ("pending", "in_progress")
                        and made.get(i.get("id"), 0) >= t0]
        except Exception:                                 # noqa: BLE001
            return False

    @staticmethod
    def _label(kind: str, talk: bool) -> str:
        """The turn's label (legend.py): TALK in discuss mode, ASK for a
        question or small talk, TASK for work."""
        return "TALK" if talk else ("ASK" if kind in ("question", "chat") else "TASK")

    def _talk_only(self) -> bool:
        """Only reading tools this turn: discuss mode, or a message that only asks
        a question.
        """
        return getattr(self.m, "mode", "act") == "discuss" or bool(getattr(self.m, "turn_readonly", False))

    def _check_on(self) -> bool:
        """The message check (task_view.py) runs: not for a helper's run, and
        not when the user switched it off (/settings chat.message_check)."""
        if getattr(self, "parent_run", "") or self.llm is None:
            return False
        try:
            from ..options import get as _oget
            return bool(_oget("chat.message_check"))
        except Exception:                                 # noqa: BLE001
            return True

    def _task_view(self, session, task: str) -> tuple[dict, str]:
        """One question to the model about a new message (task_view.py): what
        it wants — task, question, chat — and which earlier tasks it needs.
        Returns (what to leave out, kind). ({}, "task") = as before: send
        everything, do the work (a helper's run, the check off or failed)."""
        if not self._check_on():
            return {}, "task"
        from .. import task_view as TV
        segs = TV.segments(session.messages)
        earlier = segs[:-1]
        if TV.greeting(task):                    # "hi": no earlier task, no call
            return (TV.hidden(session.messages, set(), earlier) if earlier else {}), "chat"
        shown = earlier[-TV.MAX_SHOWN:]          # Older tasks: not needed
        base = len(earlier) - len(shown)
        try:
            # alone — no chat, no tools, no Aura instructions — so in a long
            # chat it reads as a sorting job, not a conversation to carry on
            res = self.llm.chat([{"role": "system", "content": TV.INSTRUCTIONS},
                                 {"role": "user", "content": TV.question(shown, task)}],
                                tools=None, temperature=0, side=True)
            said = getattr(res, "content", "") or ""
        except Exception as _swallowed:                   # noqa: BLE001
            log_swallowed("loop.task_view", _swallowed)
            return {}, "task"
        kind = TV.kind_of(said, ("task", "question", "chat"), "task")
        related = TV.parse(said, len(shown)) if earlier else set()
        if related is None or not earlier:
            return {}, kind
        related = {base + n for n in related}
        if TV.points_back(task):                 # "do it", "that one": never alone
            related |= set(range(max(1, len(earlier) - TV.KEEP_NEWEST + 1), len(earlier) + 1))
        view = TV.hidden(session.messages, related, earlier)
        if view and kind == "task":
            n = len(earlier) - len(related)
            self.on_event("notice", {"text": (
                f"new task — {n} earlier task{'s' if n > 1 else ''} in this chat "
                "left out (not needed for this one)")})
        return view, kind

    def _steer_kind(self, task: str, msg: str) -> str:
        """A message typed while the task runs: question, change or later
        (task_view.STEER_INSTRUCTIONS). "change" when it can't be told.
        """
        if not self._check_on():
            return "change"
        from .. import task_view as TV
        try:
            res = self.llm.chat([{"role": "system", "content": TV.STEER_INSTRUCTIONS},
                                 {"role": "user", "content": TV.steer_question(task, msg)}],
                                tools=None, temperature=0, side=True)
            return TV.kind_of(getattr(res, "content", "") or "", ("question", "change", "later"),
                              "change")
        except Exception as _swallowed:                   # noqa: BLE001
            log_swallowed("loop.steer_kind", _swallowed)
            return "change"

    def _side_chat(self, llm, instructions: str, text: str, json_ask: str = "", **kw):
        """Only the opening instructions go along, never the conversation: the
        reviewer still judges from evidence alone. Anthropic recommends the
        same for side requests ("cache-safe forking"). A side call to another
        model (the helper), to a server that hasn't shown it caches, or one
        answered with a tool call, goes plain.
        """
        plain = [{"role": "system", "content": instructions},
                 {"role": "user", "content": text}]
        main = getattr(self, "llm", None)
        same = llm is main or (getattr(llm, "p", None) is not None and main is not None
                               and llm.p.base_url == main.p.base_url
                               and llm.p.model == main.p.model)
        sess = getattr(self, "_session_ref", None)
        lead: list[dict] = []
        caches = bool(getattr(main, "server_caches", False))
        if same and caches and sess is not None:
            for m in sess.messages:
                if m.get("role") != "system":
                    break
                lead.append(m)
        if lead and self.schemas:
            from ..notes import aura_note
            ask = aura_note("A side task for Aura — answer it in plain text and call no "
                            "tools. Ignore the tool list for this answer.\n\n"
                            + instructions + "\n\n" + text)
            try:
                res = llm.chat(lead + [{"role": "user", "content": ask}],
                               tools=self.schemas, side=True, **kw)
                if (res.content or "").strip() and not res.tool_calls:
                    return res
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.side_chat", _swallowed)
        if json_ask:
            try:
                from ..onboard import response_format_kwargs
                jf = response_format_kwargs(llm.p.base_url, llm.p.model)
            except Exception:                    # noqa: BLE001
                jf = {}
            if jf:
                try:
                    res = llm.chat([{"role": "system", "content": instructions + "\n\n" + json_ask},
                                    {"role": "user", "content": text}],
                                   tools=None, side=True, extra=jf, **kw)
                    if (res.content or "").strip():
                        return res
                except Exception as _swallowed:  # noqa: BLE001
                    log_swallowed("loop.side_chat_json", _swallowed)
        try:
            return llm.chat(plain, tools=None, side=True, **kw)
        except TypeError:                        # A client without `side`
            return llm.chat(plain, tools=None, **kw)

    def _chat_recovering(self, session, messages: list[dict]):
        """llm.chat that survives a context overflow: compact (normal, then
        tight) and retry. Returns (result, messages actually sent)."""
        from ..llm import ContextOverflow
        self._refresh_tools()
        for attempt in range(3):
            # task_view.py: earlier tasks this one doesn't need are not sent
            _view = getattr(self, "_view", None)
            if _view:
                from ..task_view import visible
                _send = visible(messages, _view)
            else:
                _send = messages
            # what this call sends, for the context meter (ctx_state)
            if self.governor:
                try:
                    self._sent_est = self.governor.count(_send)
                except Exception:                         # noqa: BLE001
                    self._sent_est = 0
            # Discuss: only the tools that read are offered — the model can't
            # reach for an edit it would only be refused
            _tools = self.schemas
            # a question gets a short look, then an answer: after a few rounds of
            # reading the tools go, and the model answers from what it has
            if getattr(self.m, "turn_readonly", False) and                     getattr(self.m, "mode", "act") != "discuss":
                self._q_rounds = getattr(self, "_q_rounds", 0) + 1
                if self._q_rounds > QUESTION_ROUNDS:
                    _tools = []
                    _send = list(_send) + [{"role": "system", "content": (
                        "[ANSWER NOW] The user asked a question. Answer it now, in words, "
                        "from what you already have — no more tools. If you don't know, "
                        "say so and say what you'd check.")}]
            if self._talk_only() and _tools:
                from ..mediator import BlastRadius as _BR
                _tools = [t for t in _tools
                          if t["function"]["name"] in ("tools_find", "use_tool")
                          or getattr(self.m.tools.get(t["function"]["name"]), "blast_radius",
                                     None) == _BR.READ]
            try:
                return (self.llm.chat(_send, tools=_tools,
                                      on_delta=self._delta_printer()),
                        messages)
            except ContextOverflow as e:
                # the server names its real window: use it from now on, so
                # compaction fits the prompt to what it actually accepts
                from ..llm import context_limit_in
                real = context_limit_in(str(e))
                if real and self.governor is not None \
                        and real < self.governor.ctx:
                    self.on_event("notice", {"text": (
                        f"the model server accepts only {real:,} tokens "
                        f"(Aura assumed {self.governor.ctx:,}) — fitting "
                        "the conversation to that from now on. More room: "
                        "raise the context size in your model server")})
                    self.governor.ctx = real
                    # keep room for the reply inside the smaller window
                    self.governor.reply_reserve = min(
                        getattr(self.governor, "reply_reserve", 0) or real,
                        max(1, real // 4))
                    try:
                        self.llm.p.context_length = real
                    except Exception:                     # noqa: BLE001
                        pass
                if self.governor is None or attempt == 2:
                    raise ContextOverflow(
                        f"{e} — still too large after compacting twice; "
                        "lower the tool-output limit (/budget) or start a "
                        "new session") from e
                tight = attempt == 1
                # First a trim batch (nothing lost — every trimmed output can
                # be reopened); summarize only if that's not enough
                ws = getattr(self, "_workset", None)
                if attempt == 0 and ws is not None:
                    trimmed, rep = ws.apply(messages, self.governor.count,
                                            max(1, int(self.governor._usable() * 0.5)),
                                            store=self.root)
                    if rep["trimmed"]:
                        messages = trimmed
                        self.on_event("phase", {"name": "context overflow", "detail": (
                            f"provider rejected the context size; trimmed "
                            f"{rep['trimmed']} old outputs, retrying")})
                        continue
                messages = self._overflow_compact(session, messages, tight)
                self.on_event("phase", {
                    "name": "context overflow",
                    "detail": ("provider rejected the context size; "
                               + ("tight compaction" if tight else "compacted")
                               + ", retrying")})
        raise AssertionError("unreachable")

    def _execute_tool(self, name: str, args: dict,
                      parse_error: str | None = None) -> dict:
        # ADD-2: unrepairable argument JSON → error result the model sees
        # and can retry from; never execute on guessed-empty args.
        if parse_error:
            return {"status": "error", "error": parse_error}
        fn = self.dispatch.get(name)
        if fn is None:
            return {"status": "error", "error": f"unknown tool '{name}'"}
        _tl = getattr(self, "tool_list", None)
        if _tl is not None and _tl.is_blocked(name):
            from ..toolsets import set_of
            return {"status": "error", "error": (
                f"'{name}' is in the '{set_of(name)}' tools, which the user "
                "switched off — do it another way, or ask the user")}
        # A call without an argument its schema requires: say which, instead of
        # the tool failing with "KeyError: 'path'"
        _sc = _tl.all.get(name) if _tl is not None else None
        _req = ((_sc or {}).get("function", {}).get("parameters") or {}).get("required") or []
        _gap = [a for a in _req if not isinstance(args, dict) or args.get(a) is None]
        if _gap:
            return {"status": "error", "error": (
                f"{name} needs {', '.join(repr(a) for a in _gap)} — call it again with "
                f"{'it' if len(_gap) == 1 else 'them'} (required: {', '.join(_req)})")}
        if name in (WEB_CRAWL, WEB_SEARCH, BROWSER_RUN):
            self._web_used = True
        stop = self._repeat_stop(name, args)
        if stop:
            return {"status": "error", "error": stop}
        same = self._unchanged_reread(name, args)
        if same:
            return {"status": "ok", "result": same}
        # A call / target the loop guard blocked is refused, not run
        _lw = getattr(self, "_loopwatch", None)
        if _lw is not None:
            _why = _lw.blocked_reason(name, args)
            if _why:
                self.on_event("loopwatch", {"tool": name, "blocked": True})
                return {"status": "error", "error": _why}
        # Aura Teams: a file in someone else's open or unmerged work is
        # asked about before it is changed (the user decides)
        _tm = getattr(self, "_teams", None)
        if _tm is not None and name in FILE_WRITERS and isinstance(args, dict) \
                and args.get("path"):
            try:
                _no = _tm.before_write(str(args["path"]))
            except Exception as _swallowed:          # noqa: BLE001
                log_swallowed("loop.teams_write", _swallowed)
                _no = None
            if _no:
                return {"status": "error", "error": _no}
        # The minimizer is a TAPE tool (display/ audit sizing), never an
        # execution gate: execute on full args, minimize only the recorded
        # copy.
        try:
            from ..secrets_guard import minimize_args
            args_for_tape = minimize_args(args)
        except Exception:
            args_for_tape = args
        # Record calls for the out-of-band intent drift audit
        if self.intent_contract is not None:
            self.intent_calls.append({"name": name,
                                      "args": args_for_tape})
        t0 = time.monotonic()
        try:
            # BUILD 2: auto-checkpoint before destructive shell commands —
            # the restore point exists BEFORE the agent can break anything.
            # Skips silently when the tree is too large (guarded inside).
            if name in (RUN_COMMAND, "process_start") and isinstance(args, dict):
                _w = str(args.get("cmd", "")).strip().split()[:1]
                if _w and _w[0] in _DESTRUCTIVE_WORDS:
                    try:
                        from ..checkpoints import create as _ckpt_create
                        _ckpt_create(self.work_dir, label=f"pre-{_w[0]}",
                                     auto=True, store=self.root)
                    except Exception as _swallowed:
                        log_swallowed("loop.auto_checkpoint", _swallowed)  # Checkpointing must never kill the tool path
            raw = self.m.execute(name, args, fn)
            # A missing dependency → the right install command, offered (never
            # run by Aura; deps_hint.py)
            if name in ("run_command", "run_checks") and isinstance(raw, str) \
                    and ("No module named" in raw or "Cannot find module" in raw):
                try:
                    from ..deps_hint import hint as _dep_hint
                    _h = _dep_hint(raw.replace("\\n", "\n").replace("\\'", "'"),
                                   getattr(self, "work_dir", None) or self.root)
                    if _h:
                        raw = raw + "\n" + _h
                except Exception as _swallowed:      # noqa: BLE001
                    log_swallowed("loop.deps_hint", _swallowed)
            self._note_for_proof(name, args, raw)
            if name == "finding_add":
                raw = self._prove_finding(raw)
            # After an edit, the installed language server's errors for that
            # file come back in the same result (none installed → nothing)
            if name in FILE_WRITERS and isinstance(raw, str) and isinstance(args, dict) \
                    and args.get("path") and not raw.lower().startswith(("error", "refused")):
                try:
                    from ..options import get as _oget
                    if _oget("code.lsp"):
                        from ..lsp import errors_after_edit
                        _rep = errors_after_edit(str(args["path"]),
                                                 Path(getattr(self, "work_dir", None) or self.root))
                        if _rep:
                            raw = raw + "\n" + _rep
                except Exception as _swallowed:      # noqa: BLE001
                    log_swallowed("loop.lsp", _swallowed)
            data = (raw if isinstance(raw, bytes)
                    else (raw if isinstance(raw, str)
                          else json.dumps(raw, ensure_ascii=False, default=str)
                          ).encode())
            # Contract: truncation via artifact store, typed metadata. inline
            # cap = the resolved preset's tool-output limit.
            _limit = self.cfg.max_tool_output
            if _wrapped_already(data):
                # The tool cut it and saved the whole output itself — wrapping
                # again cut its JSON mid-way and saved a 2nd copy
                _limit = max(_limit or 0, len(data) + 1)
            wrapped = wrap_result(self.root, name, data,
                                  duration_ms=int((time.monotonic() - t0) * 1000),
                                  inline_limit=_limit)
            # Sanitizer: scrub secrets from tool output before it enters the
            # session tape (secrets-without-secrets)
            try:
                from ..secrets_guard import sanitize_output
                # 1.11: content the model and operator did not author gets
                # the instruction-likeness flag (a marker is appended; the
                # payload is never altered). Was dead: nothing ever passed
                # is_external=True.
                _ext = name in _EXTERNAL_TOOLS or name.startswith(MCP_PREFIX)
                # a helper's report can carry what its own pages told it
                if _ext or name == DELEGATE:
                    from ..secrets_guard import scan_untrusted
                    _body = wrapped.get("result") if isinstance(wrapped.get("result"), str) \
                        else wrapped.get("content")
                    if not isinstance(_body, str) and name == DELEGATE:
                        _body = json.dumps(wrapped.get("result"), default=str)
                    _sc = scan_untrusted(_body) if isinstance(_body, str) else {"strong": []}
                    if (name == DELEGATE and isinstance(_body, str)
                            and "outside text this helper read tried to give instructions" in _body):
                        self._taint("a helper")
                    if _sc["strong"] and not getattr(self.m, "tainted", False):
                        self.m.tainted = True        # Commands ask first for the rest of the task
                        self.on_event("notice", {"text": (
                            f"outside text from {name} tried to give instructions ("
                            + ", ".join(_sc["strong"]) + ") — Aura treats it as data, and every "
                            "command asks you first for the rest of this task")})
                        if getattr(self, "_receipt", None) is not None:
                            self._receipt.note(f"injection attempt in {name} output: "
                                               + ", ".join(_sc["strong"]))
                if isinstance(wrapped.get("result"), str):
                    wrapped["result"] = sanitize_output(wrapped["result"],
                                                        is_external=_ext)
                elif isinstance(wrapped.get("content"), str):
                    wrapped["content"] = sanitize_output(wrapped["content"],
                                                         is_external=_ext)
            except Exception as _swallowed:
                # fail closed: the tool still "succeeded", but its unscrubbed
                # output never enters the session tape
                log_swallowed("loop.output_firewall", _swallowed)
                from ..secrets_guard import withheld
                for _k in ("result", "content"):
                    if isinstance(wrapped.get(_k), str):
                        wrapped[_k] = withheld(f"tool {name}", _swallowed)
            if self._receipt is not None:
                try:
                    _wp = (args.get("path") if isinstance(args, dict)
                           else None)
                    if name in FILE_WRITERS and _wp:
                        _abs = Path(self.work_dir) / str(_wp)
                        if _abs.is_file():
                            self._receipt.record_output(_abs)
                except Exception as _swallowed:
                    log_swallowed("loop.receipt_output", _swallowed)  # Receipt must never kill the tool path
            wrapped["ok"] = True
            # Receipt counts tool ok/error
            if self._receipt is not None:
                self._receipt.count_tool(True)
            # Big-output tools get a delegate-isolation mentor nudge
            from ..delegate import wrap_with_route_hint
            wrapped = wrap_with_route_hint(wrapped, name, args)
            return {"status": "ok", **wrapped}
        except Violation as e:
            # mediator refusal — the model MUST see why, verbatim
            if self._receipt is not None:
                self._receipt.count_tool(False)
            # Approval-refusals render as the graded approval card (real action
            # + grade), not a bare refusal string
            if "needs operator approval" in str(e):
                try:
                    from ..autonomy import ApprovalRequest, approval_card
                    from ..approvals import PendingApproval
                    _spec = self.m.tools.get(name)
                    _grade = _grade_of_safe(name, self.m.tools.get(name), args,
                                             getattr(self.m, "grade_overrides", None))
                    _target = _spec.extract_target(args) if _spec else "?"
                    card = approval_card(ApprovalRequest(
                        tool=name, args=args, grade=_grade,
                        target=_target,
                        intent="operator approval required (L1 assist)"))
                    # Durable pending approval — the tap-to-approve round-trip
                    # consumes this; G3 never auto-parks (no remote approval
                    # for irreversible verbs in v1)
                    if _grade in ("G1", "G2"):
                        pa = PendingApproval.create(self.root, {
                            "tool": name, "args": args, "grade": _grade,
                            "target": _target,
                            "intent": "L1 assist approval"})
                        self.on_event("approval_needed", {
                            "step": getattr(self, "_step", 0),
                            "approval_id": pa.id,
                            "tool": name, "grade": _grade,
                            "card": pa.card()})
                        # nobody can answer here: the run stops after
                        # this step instead of carrying on without it
                        self._parked = (pa.id, name, _target)
                        return {"status": "awaiting_approval",
                                "approval_id": pa.id,
                                "approval_card": card,
                                "error": str(e)}
                    return {"status": "refused", "error": str(e),
                            "approval_card": card}
                except Exception:
                    pass  # Card must never mask the refusal
            return {"status": "refused", "error": str(e)}
        except Exception as e:
            if self._receipt is not None:
                self._receipt.count_tool(False)
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------- loop
    def run(self, session, task: str, kill_check: Callable[[], bool] | None = None,
            steer_queue: list[str] | None = None,
            work_dir: Path | None = None
            ) -> StepRecord:
        """Run until the model replies without tool calls, budget, or .kill.

        One run, in named stages (2.1 — the stages are the old body, moved
        unchanged): _start_run → _open_turn → per step: _before_call →
        _context_for_call → _call_model → _finish_or_resume (no tool
        calls) or _run_tools."""
        kill_check = kill_check or (lambda: False)
        self._start_run(session, task, work_dir)
        go = self._teams_start(session, task)
        if go is not None and not go["go"]:
            rec = StepRecord(step=0, content=go["why"])
            self._receipt_snapshot(0)
            self._receipt_finalize(rec)
            return rec
        if go is not None and go["context"]:
            task = task + "\n\n" + go["context"]
        messages = self._open_turn(session, task)
        watch = self._machine_watch()
        try:
            rec = self._steps(session, task, messages, kill_check, steer_queue)
        finally:
            if watch is not None:
                watch.stop()
        self._skills_outcome(rec)
        self._teams_finish(rec)
        return rec

    def _machine_watch(self):
        """Machine on: watch free memory while this run works; a low-memory note
        waits for the next step. Never for a helper's run (the parent's watch
        covers it).
        """
        self.machine_notes = []
        try:
            from ..options import get
            if not get("machine.watch") or getattr(self, "parent_run", ""):
                return None
            from ..machine import Watcher
            w = Watcher(self.machine_notes.append)
            w.start()
            return w
        except Exception as _swallowed:                     # noqa: BLE001
            log_swallowed("loop.machine_watch", _swallowed)
            return None

    def _offer_skills(self, session, messages: list) -> list:
        """skill_pick.py: when the work turns big (the task state: the goal,
        the latest reply, the open to-dos), the best 2-3 skills are offered
        to the person once per task (3 offers a chat at most); the picked
        ones are sent in full. Once per big task: search the public sources?
        Never for a helper's run. Never stops the work."""
        if getattr(self, "_skills_offered", True) or getattr(self, "parent_run", "") \
                or getattr(self.m, "turn_readonly", False):        # a question: no offers
            return messages
        try:
            from .. import skill_pick as SP
            todos = self._plan_items()
            goal = getattr(self, "_goal", "") or getattr(self, "_task_text", "")
            if not SP.is_big(goal, todos, classify_scope(goal) == "multi"):
                return messages             # Small (so far): look again next step
            self._skills_offered = True
            from ..skills import load_skills
            skills = load_skills(self.root)
            text = SP.state_text(goal, getattr(self, "_reply", ""), todos)
            chat = Path(session.path).stem
            ask = getattr(self.m, "asker", None)
            tell = lambda t: self.on_event("notice", {"text": t})
            picked = SP.offer(self.root, chat, text, skills, ask, tell)
            if ask is not None:
                from ..packs import _registry
                SP.offer_search(self.root, chat, " ".join(goal.split())[:200], text, ask, tell,
                                _registry(True, None))
            if picked:
                self._skills_used += picked
                by = "picked by the user" if ask is not None else "the best match, nobody to ask"
                messages = messages + [{"role": "system",
                                        "content": SP.body_block(picked, skills, by)}]
        except Exception as e:                       # noqa: BLE001
            log_swallowed("loop.offer_skills", e)
        return messages

    def _skills_outcome(self, rec: StepRecord) -> None:
        """How the picked skills did: the run ended without an error, and
        the checks that ran after the last change passed."""
        used = getattr(self, "_skills_used", None)
        if not used:
            return
        try:
            from ..skill_pick import record
            tested = getattr(self, "_tests_seq", -1) > getattr(self, "_last_change_seq", -1)
            ok = not rec.error and (getattr(self, "_tests_ok", False) or not tested)
            record(used, ok)
        except Exception as e:                       # noqa: BLE001
            log_swallowed("loop.skills_outcome", e)

    def _teams_start(self, session, task: str) -> dict | None:
        """Aura Teams: link the work to the board, the collision check, the start
        update — only on a machine connected to a hub. None = Teams is not in
        use here. A Teams failure never stops the work: it is said, and the run
        goes on.
        """
        self._teams = None
        if getattr(self, "parent_run", ""):
            return None          # A helper's run is part of its parent's task
        from ..editions import teams_present
        if not teams_present():
            return None
        try:
            from .. import teams_run as _TR
            if not _TR.active():
                return None
            self._teams = _TR.TeamsRun(
                Path(session.path).stem, getattr(self.m, "asker", None),
                lambda t: self.on_event("notice", {"text": t}), self.work_dir,
                emit=lambda d: self.on_event("team", d))
            return self._teams.start(task)
        except Exception as e:                       # noqa: BLE001
            log_swallowed("loop.teams_start", e)
            self.on_event("notice", {"text": f"Teams: {e}"})
            return None

    def _teams_finish(self, rec: StepRecord) -> None:
        """After the run: with proof (the checks ran after the last change
        and passed), ask whether the board task is finished."""
        if getattr(self, "_teams", None) is None:
            return
        try:
            proof = None
            if getattr(self, "_tests_ok", False) and \
                    getattr(self, "_tests_seq", -1) > getattr(self, "_last_change_seq", -1):
                _n, _a = getattr(self, "_test_call", None) or (None, None)
                _a = _a if isinstance(_a, dict) else {}
                cmd = _a.get("cmd") or _a.get("command") or _n or "the checks"
                proof = f"`{str(cmd)[:120]}` passed after the last change"
            self._teams.finish(list(getattr(self, "_changed_files", []) or []),
                               proof, bool(rec.error))
        except Exception as e:                       # noqa: BLE001
            log_swallowed("loop.teams_finish", e)
            self.on_event("notice", {"text": f"Teams: {e}"})

    def _steps(self, session, task: str, messages: list, kill_check,
               steer_queue) -> StepRecord:
        """The run's steps, until an exit (the old body of run())."""
        step = 0
        while True:                      # No step cap by default (LoopConfig):
            step += 1                    # Run ends on clean exit, kill switch
            if (self.cfg.max_steps is not None   # Or context exhaustion — not
                    and step > self.cfg.max_steps):  # On an arbitrary counter
                break
            if kill_check():
                self.on_event("killed", {"step": step})
                rec = StepRecord(step=step, error="kill switch")
                self._receipt_snapshot(step)
                self._receipt_finalize(rec)
                return rec
            messages = self._before_call(session, messages, step, steer_queue)
            messages = self._context_for_call(session, messages, step, task)
            res, messages = self._call_model(session, messages, step)
            if not res.tool_calls:
                rec, messages = self._finish_or_resume(
                    session, messages, res, step, task, kill_check)
                if rec is not None:
                    return rec
                continue
            self._run_tools(session, res, step)
            # pblocks: learn at every step's input->output (the tested build's
            # design): this step's action + the world's answers = a lesson pair
            try:
                from .. import pblocks as _pb
                _sp = _pb.learn_step(session, self.work_dir, step,
                                     getattr(self, "_task_text", "") or "")
                if _sp:
                    self.on_event("pblock_step_learn", {"step": step, "candidates": _sp})
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.pblock_step_learn", _swallowed)
            if self._answered_and_ticked(res):
                # Bug: the answer came twice — the model wrote it while ticking
                # the plan done, then wrote it again. The answer with only
                # "done" ticks, nothing left open, IS the end: no second model
                # call
                import copy as _copy
                _end = _copy.copy(res)
                _end.tool_calls = []
                rec, messages = self._finish_or_resume(
                    session, list(session.messages), _end, step, task, kill_check)
                if rec is not None:
                    return rec
                continue
            _lw = getattr(self, "_loopwatch", None)
            if _lw is not None and _lw.stop_reason:
                # Stopped by the loop guard — said plainly, and the receipt
                # says so (not a silent end, not a success)
                self.on_event("notice", {"text": _lw.stop_reason})
                self._model_trouble("it kept repeating itself")
                rec = StepRecord(step=step, error=_lw.stop_reason)
                if getattr(self, "_receipt", None) is not None:
                    self._receipt.add_check("not_stopped_by_loop_guard", False,
                                            _lw.stop_reason)
                self._receipt_snapshot(step)
                self._receipt_finalize(rec)
                return rec
            if getattr(self, "_parked", None):
                pid, ptool, ptarget = self._parked
                self._parked = None
                self.on_event("notice", {"text": (
                    f"stopped: '{ptool}' → {ptarget} needs approval and "
                    "nobody here can give it")})
                rec = StepRecord(step=step, error=(
                    f"waiting for approval {pid}: '{ptool}' → {ptarget}. "
                    "Nothing else was run. Run the task again where "
                    "someone can approve it (a terminal, or chat)."))
                self._receipt_snapshot(step)
                self._receipt_finalize(rec)
                return rec
            messages = list(session.messages)

        # Finalize receipt at max_steps exit too
        _rec_max = StepRecord(step=self.cfg.max_steps,
                              error="max_steps reached without final answer")
        self._receipt_finalize(_rec_max)
        return _rec_max

    def _pick_tools(self, task: str) -> None:
        """Add the toolsets this task needs (toolsets.py). The list only grows
        within a chat, so the cached prompt start stays.
        """
        tl = getattr(self, "tool_list", None)
        if tl is None:
            return
        hints: set[str] = set()
        try:
            from ..skill_curator import pinned as _pinned
            if _pinned(self.root):
                hints.add("skills")          # The pinned skills' list names skill_load
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.tools_hint_skills", _swallowed)
        try:
            from ..constraints import _read_rules
            if any(_read_rules(Path(r)) for r in {self.root, self.work_dir}):
                hints.add("rules")           # The rules block names the rules tool
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.tools_hint_rules", _swallowed)
        try:
            from ..toolsets import set_of
            sp = self.cfg.system_prompt or ""
            for n in tl.all:
                if set_of(n) not in ("core", "other") and re.search(
                        r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9_])", sp):
                    hints.add(set_of(n))     # The prompt names this tool
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.tools_hint_prompt", _swallowed)
        try:
            from ..options import get as _opt
            if (getattr(self, "research_mode", None) or _opt("research.mode")) == "deep":
                hints.add("web")                 # Deep research searches
        except Exception as _swallowed:          # noqa: BLE001
            log_swallowed("loop.tools_hint_research", _swallowed)
        ctx = self.governor.ctx if self.governor is not None else 0
        try:
            try:
                from ..model_facts import tool_skill
                _skill = tool_skill()
            except Exception:                    # noqa: BLE001
                _skill = None
            added = tl.pick(task or "", ctx=ctx, hints=hints, skill=_skill)
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.tools_pick", _swallowed)
            return
        self._refresh_tools()
        sent, everything = tl.chars()
        self.on_event("toolsets", {"added": added, "sent": len(tl.sent),
                                   "registered": len(tl.all),
                                   "chars_sent": sent, "chars_all": everything})

    def _park_tools(self) -> None:
        tl = getattr(self, "tool_list", None)
        if tl is None:
            return
        try:
            from ..options import get as _opt
            if not _opt("tools.park"):
                return
            gone = tl.park()
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.tools_park", _swallowed)
            return
        if gone:
            self._refresh_tools()
            if self._receipt is not None:
                self._receipt.note("tools put away at the summary (unused): " + ", ".join(gone))
            self.on_event("toolsets", {"added": [], "parked": gone, "sent": len(tl.sent),
                                       "registered": len(tl.all),
                                       "chars_sent": tl.chars()[0]})

    def _refresh_tools(self) -> None:
        tl = getattr(self, "tool_list", None)
        if tl is None:
            return
        if [x["function"]["name"] for x in self.schemas] != tl.sent:
            self.schemas = tl.schemas()
            if self.governor is not None:
                self.governor.schema_chars = len(json.dumps(self.schemas))

    def _start_run(self, session, task: str, work_dir) -> None:
        """Per-run state: workspace, rules folder, skill-pack limits, resets, receipt."""
        self._later = []          # Messages typed mid-task that run after it
        # Deliverable gate — where TASK.md-named artifacts live (sandbox cwd in
        # arena runs, engagement dir otherwise) the workspace: the user's
        # project (build_agent sets it; same as the notebook in old-style
        # engagements and helpers)
        self.work_dir = (work_dir or getattr(self, "workspace", None)
                         or self.root)
        self.m.work_dir = self.work_dir   # 2.5: where run_checks runs
        self._work_baseline = self._take_baseline()
        self._pick_tools(task)
        # every session remembers what it runs with (aura run, cron;
        # chat records its own, with the chat-only toggles)
        if not getattr(session, "settings", True):
            from ..session_settings import remember
            remember(self, session)
        # Constraints live with the ENGAGEMENT (where the rules tool and rules
        # write them) — not the gate root. Pointer + mediator hook both read
        # self.m.constraints_root; fall back to work_dir only when the
        # engagement has no constraints file but work_dir does (arena sandbox
        # pre-seeded with one).
        self.m.constraints_root = self.root
        _wc = self.work_dir / "constraints.md"
        if not (self.root / "constraints.md").exists() and _wc.exists():
            self.m.constraints_root = self.work_dir
        # Skill packs loaded earlier in THIS conversation keep their limits
        # (their text is still in it); another session has its own
        try:
            from ..pack_limits import load_active, session_file
            _rec = session_file(session.path)
            if _rec != self.m.pack_record:
                self.m.pack_record = _rec
                self.m.pack_limits = load_active(_rec)
        except Exception as _swallowed:
            log_swallowed("loop.pack_limits", _swallowed)
        self._skip_guard_used = 0    # Silent-skip guard reset per run
        self._run_t0 = time.time()   # Plan items made in this run (skip guard)
        # the plan of THIS chat (the switch decides whether there is one)
        self.m.plan_chat = Path(getattr(session, "path", "") or "chat").stem
        try:
            from ..session_titles import session_title
            _t = session_title(Path(session.path).name) or ""
        except Exception:                                # noqa: BLE001
            _t = ""
        _id = getattr(session, "id", "") or ""
        self._chat_label = (f"#{_id}" if _id else self.m.plan_chat) + (f" '{_t}'" if _t else "")
        if getattr(self, "ctx_source", "") == "default" and not getattr(self, "_ctx_told", False):
            self._ctx_told = True
            self.on_event("notice", {"text": f"{getattr(getattr(self.llm, 'p', None), 'model', None) or 'this model'}'s "
                          "context size isn't known — using 128k. Set it: /model ctx <tokens> · "
                          "find it: /model ctx find"})
        self.m.plan_forced = False
        try:
            from ..todo import archive_done
            archive_done(self.root, self.m.plan_chat)  # The last task's finished steps leave the plan
        except Exception as _swallowed:
            log_swallowed("loop.todo_archive", _swallowed)
        self._run_failures = []      # 1.4: reviewer sees THIS run's failures
        self._reviews_left = _REVIEW_BUDGET
        # Run-proof state — reads (with what they returned), the sequence
        # number of the last change, one fix-up turn for failing tests
        self._reads: list[dict] = []
        self._tests_seq, self._test_call = -1, None   # 1.4: per run
        self._rep_sig, self._rep_n = None, 0     # #14: per run
        # Per task, not per chat.
        self._retry_step = None
        # kept aside: a message that continues the same task ("try again",
        # "continue") gets them back in _open_turn — the same failing command
        # must not get three fresh tries every turn
        self._kept_guard = (getattr(self, "_loopwatch", None), getattr(self, "_ladder", None))
        self._loopwatch = None
        self._ladder = None                    # The same failure, again (loopwatch)
        self._run_watch = None                 # The same thoughts across replies
        self._parked = None
        self._seq = 0
        self._reads_seen = {}                  # Fix list #4: unchanged re-reads
        self._last_change_seq = -1
        self._proved_seq = -1
        self._wrote_files = False
        self._proof_landings_left = 1
        self._cite_landings_left = 1     # One turn to fix citations
        self._web_used = False           # Set when a web tool runs this run
        try:
            from ..options import get as _opt
            from ..research import ResearchMonitor
            _mode = getattr(self, "research_mode", None) or _opt("research.mode")
            self._research = ResearchMonitor(_mode, _opt("research.deep_max_searches"))
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.research_init", _swallowed)
            self._research = None
        self.m._research = self._research
        self.m._history_session = session   # History leaves this chat out
        # Helpers (delegate) and the reviewer use the model in use now
        self.m._main_llm = self.llm
        self.m._loop = self
        if getattr(self, "reviewer", None) is not None and self.llm is not None:
            self.reviewer.llm = self.llm
            self.reviewer.side_chat = lambda ins, txt, **kw: self._side_chat(
                self.reviewer.llm, ins, txt, **kw)
        # A task pointing at an earlier chat → search first (local)
        self._recall_note = None
        try:
            from ..options import get as _oget
            if _oget("history.auto_recall"):
                from ..history import recall_note
                self._recall_note = recall_note(task, Path(self.root), Path(session.path).stem)
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.history_recall", _swallowed)
        # The model in use, for per-model choices (line tags, early check)
        try:
            from .. import model_facts as _MF
            _MF.ACTIVE = _MF.model_key(self.llm.p.base_url, self.llm.p.model)
            # A model checked and found unable to call tools — say so once, at
            # the start, instead of odd failures later
            if _MF.fact("tools") == "no" and not getattr(self, "_no_tools_told", False):
                self._no_tools_told = True
                self.on_event("notice", {"text": (
                    f"{self.llm.p.model} didn't call tools when checked (aura eval "
                    "model --show) — Aura can only talk with it, not read, edit or run "
                    "anything. Pick a model that calls tools for real work.")})
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.model_facts_active", _swallowed)
        self._early_told = False
        self._deep_told = False
        self._skills_offered = False           # skill_pick: one offer per task
        self._skills_used: list[str] = []      # Picked for this run (outcomes)
        _cs = getattr(self.m, "_clarify_state", None)
        if _cs is not None:              # Questions per run
            _cs["asked"].clear()
            _cs["count"] = 0
        if getattr(self.m, "_assumptions", None) is not None:
            self.m._assumptions.clear()
        self._lint_pending: set[str] = set()   # 2.5: files changed since last lint
        self._lint_left = 5                    # 2.5: auto-lint notes per run
        from ..features import active as _features_on
        self._features = _features_on()        # 2.2: features, read per run
        self._last_proof = None
        self._unproven = 0                     # 'not proven' replies this run
        self._proven = 0                       # The end-of-run report
        self._changed_files = []
        self._tests_edited = []
        self._added, self._ran_anything = [], False     # claim_check: per task
        self._reached_outside = False
        self._task_text = task
        self._dont_touch = dont_touch(task)
        self._tests_seq, self._tests_ok = -1, False
        self._trouble_told = False
        self.m.tainted = False                 # Promptware gate: per task
        self._cycles = 0                       # Replies stopped for going in circles
        self._fw_nudged = False                # find_way: one note per run
        self.m._research_left = None           # find_way: research allowed by a `get` yes
        self.m._get_declined = False
        self._task_text = task                 # The deliverable watch
        self._early_notes = 0
        self.failure_streak = 0
        self._session_ref = session  # Diff guarantee needs the tape
        # Run receipt — created per run, finalized at every exit
        try:
            from ..receipts import RunReceipt
            _rid = time.strftime("run-%Y%m%d-%H%M%S") + "-" + \
                secrets.token_hex(3)   # Same-second runs must not collide
                # (visual-audit find: two runs in one second overwrote the
                # first receipt — hash chain silently lost a link)
            self._receipt = RunReceipt(self.root, _rid, task)
            self.m._receipt_ref = self._receipt          # Helpers link to it
            self.m._helper_runs = []
            if getattr(self, "parent_run", ""):
                self._receipt.data["parent_run"] = self.parent_run
            try:   # Past-chat search links runs to their chat
                self._receipt.data["session"] = Path(session.path).stem
            except Exception:      # noqa: BLE001
                pass
            self._receipt_steps = 0
        except Exception:
            self._receipt = None  # Receipt must never kill the loop

    def _with_images(self, task: str, session=None):
        """The user's text plus images pasted with it (set by the chat as
        pending_images, only for a model checked to see images). Each image is
        copied next to the chat and kept as a path.
        """
        from ..notes import image_ref, user_text
        imgs = getattr(self, "pending_images", None) or []
        self.pending_images = []
        task = user_text(task)
        parts = []
        folder = (Path(session.path).parent / (Path(session.path).stem + "-images")
                  if session is not None and getattr(session, "path", None) else None)
        for p in imgs:
            try:
                src = Path(p)
                dst = src
                if folder is not None:
                    import shutil
                    folder.mkdir(parents=True, exist_ok=True)
                    n = len(list(folder.iterdir())) + 1
                    dst = folder / f"{n}{src.suffix or '.png'}"
                    shutil.copyfile(src, dst)
                src.stat()
                parts.append(image_ref(str(dst), n=len(parts) + 1))
            except OSError as _swallowed:
                log_swallowed("loop.image_attach", _swallowed)
        return [{"type": "text", "text": task}] + parts if parts else task

    def _environment(self) -> str:
        """."""
        try:
            from ..project_checks import environment_line
            chat = getattr(self, "_chat_label", "")
            return "\n" + environment_line(Path(self.work_dir)) + (
                f" · this chat: {chat}" if chat else "")
        except Exception:                            # noqa: BLE001
            return ""

    def _open_turn(self, session, task: str) -> list:
        """The user's message into the session (first turn or a new chat turn),
        the ledger, the active model's prompt, repro-first, the intent
        contract. Returns the messages."""
        messages = list(session.messages) or []
        _n_before = len(session.messages)
        _user = self._with_images(task, session)
        self._goal, self._reply = task, ""
        talk = getattr(self.m, "mode", "act") == "discuss"
        if talk and self._discuss_switch(task):
            talk = False                 # The user said: switch off and do it
        self.m.turn_readonly = False
        self._q_rounds = 0
        kind = "task"
        if not messages:
            self._view = {}
            from ..legend import with_legend
            session.append("system", content=self.cfg.system_prompt or with_legend(DEFAULT_SYSTEM))
            session.append("user", content=_user, by="user", **({"talk": True} if talk else {}))
            if not talk:
                _v, kind = self._task_view(session, task)
            # Priority-context: mark the CURRENT message as the authoritative
            # instruction + task ledger state
            from ..taskstate import scope_message
            session.append("system", content=scope_message(
                task, self.root, label=self._label(kind, talk)) + self._environment())
            # pblocks: the panel's cited precedents for the FIRST task of a
            # chat (the tested build's place; later turns get theirs below)
            if not talk:
                from .. import pblocks as _pb
                self._pb_chat = str(getattr(getattr(session, "path", None), "stem", "") or "")
                os.environ["AURA_PB_CHAT"] = self._pb_chat
                if _pb.available():
                    _w = _pb.welcome_once()
                    if _w:
                        self.on_event("notice", {"text": _w, "calm": True})
                _pg = _pb.ground(task, str(self.root),
                                 instance_id=self._pb_chat, chat=self._pb_chat)
                if _pg:
                    session.append("system", content=_pg)
                    self.on_event("pblock_ground", {"lines": len(_pg.splitlines()),
                                                    "seats": _pb.ground_seats(_pg)})
            # Scope-classified planning posture. Multi -> no extra block
            # (static TODO_INSTRUCTION already mandates a plan); single -> one
            # short suppression line.
            posture = planning_instruction(task, self.root)
            if posture:
                session.append("system", content=posture)
            messages = list(session.messages)
        elif task and session.last_user_task() != task:
            from ..taskstate import is_reply, scope_message, take_scope
            session.messages, _prev = take_scope(session.messages)
            _last_said = next((str(m.get("content") or "") for m in reversed(session.messages)
                               if m.get("role") == "assistant" and m.get("content")), "")
            if _prev and is_reply(task, _last_said):
                self._goal, self._reply = _prev, task
                # the same task goes on: its failure counts and blocks too
                _lw, _ld = getattr(self, "_kept_guard", (None, None))
                if _ld is not None:
                    self._loopwatch, self._ladder = _lw, _ld
                    if _lw is not None:
                        _lw.stop_reason = ""     # A new turn may try again once
            session.append("user", content=_user, by="user", **({"talk": True} if talk else {}))
            if talk:
                self._view = {}
            elif not self._reply:
                self._view, kind = self._task_view(session, task)
            # Re-mark authority on every new turn (last message wins)
            wd_now = self.root            # Ledger: notebook
            from ..taskstate import last_question
            msg = scope_message(task, wd_now, continues=self._reply and self._goal,
                                ledger_too=not getattr(self, "_view", None),
                                label=self._label(kind, talk),
                                question=last_question(_last_said) if self._reply else "")
            # Anaphora: "do it"/"that fix" binds to a ledger task — the
            # resolution block rides the scope message (auditable), and
            # unresolvable references instruct the model to ASK, not guess.
            from ..anaphora import render_resolution_block
            resolution = render_resolution_block(task, wd_now)
            if resolution:
                msg += "\n\n" + resolution
            session.append("system", content=msg + self._environment())
            # Pblocks: the panel's cited precedents for this task, as one
            # system message — never a prompt change; "" when off, not
            # installed, or no seat stores
            if not talk:
                from .. import pblocks as _pb
                self._pb_chat = str(getattr(getattr(session, "path", None), "stem", "") or "")
                os.environ["AURA_PB_CHAT"] = self._pb_chat
                if _pb.available():
                    _w = _pb.welcome_once()
                    if _w:
                        self.on_event("notice", {"text": _w, "calm": True})
                _pg = _pb.ground(task, str(self.root),
                                 instance_id=self._pb_chat, chat=self._pb_chat)
                if _pg:
                    session.append("system", content=_pg)
                    self.on_event("pblock_ground", {"lines": len(_pg.splitlines()),
                                                    "seats": _pb.ground_seats(_pg)})
            messages = list(session.messages)
        if kind in ("question", "chat") and not self._reply and not talk:
            # answered, nothing changed for it (the user can say "do it")
            self.m.turn_readonly = True
            if kind == "question":
                self.on_event("notice", {"text": (
                    "a question — Aura answers it and changes nothing (say what to do "
                    "if you want it done)")})
        if not talk and (kind == "task" or self._reply):
            self._ledger_open(task, reply=bool(self._reply))   # Talk and questions open no task
        # model switch: the session's base prompt follows the ACTIVE model
        # (its copy or the default). Found by the default core text, which
        # every version carries word for word — a compaction summary at the
        # top is never touched. One-time cache miss on a switch; the new
        # model has a cold cache anyway.
        _sp = self.cfg.system_prompt
        _core = (getattr(self, "core_prompt", None) or DEFAULT_SYSTEM).strip()
        if _sp:
            for _i, _m in enumerate(session.messages):
                _c = str(_m.get("content", ""))
                # a base prompt: carries the default core (default, copies)
                # or opens with the identity line (generic candidate)
                if (_m.get("role") == "system"
                        and (_core in _c or "You are Aura" in _c[:200])):
                    if _m.get("content") != _sp:
                        session.messages[_i] = dict(_m, content=_sp)
                        messages = list(session.messages)
                    break
        # 2.6: a bug fix starts by reproducing the bug (new turns only)
        self._bugfix = False
        self._src_changed = False
        self._repro_seen = False
        if (len(session.messages) > _n_before and looks_like_bugfix(task)
                and "repro_first" in self._features):
            try:
                from ..project_checks import check_for
                _has_tests = check_for(Path(self.work_dir), "test") is not None
            except Exception:
                _has_tests = False
            if _has_tests:
                self._bugfix = True
                session.append("system", content=REPRO_FIRST)
                messages = list(session.messages)

        if len(session.messages) > _n_before:
            self._suggest_features(task)

        # Intent Contract — built per task and used ONLY by the drift audit
        # below. It is deliberately not injected into the prompt: its content
        # (the task, AGENTS.md non-goals) is already there every turn. Drift
        # audit state resets per turn.
        self.intent_calls = []
        self._drift_seen = set()      # Report each finding once per run
        from ..intent import build_contract
        self.intent_contract = build_contract(
            self._goal, self.intent_scope, self.agents_md and
            (self.work_dir / "AGENTS.md").read_text(encoding="utf-8")
            if (self.work_dir / "AGENTS.md").exists() else None)
        return messages

    _CODE_WORK = re.compile(
        r"\b(fix|add|implement|refactor|change|update|write|build|create|rename|"
        r"remove|migrate|port|optimi[sz]e|rewrite)\b", re.I)
    _SUGGEST = {
        "repro_first": "this looks like a bug fix and the project has tests — "
                       "`/features on repro_first` makes Aura show the bug with a "
                       "failing test first, then prove the fix",
        "proof": "this changes code and the project has tests — `/features on proof` "
                 "runs them before the final answer (one turn to fix a failure)",
        "auto_lint": "this changes code and the project has a linter — "
                     "`/features on auto_lint` checks each changed file right away",
    }

    def _suggest_features(self, task: str) -> None:
        """Never switches anything on by itself."""
        try:
            from ..features import mark_suggested, may_suggest
            from ..options import get as _oget
            if not _oget("assist.suggest_checks"):
                return
            told = getattr(self, "_suggested", None)
            if told is None:
                told = self._suggested = set()
            bugfix = looks_like_bugfix(task)
            if not (bugfix or self._CODE_WORK.search(task or "")):
                return
            from ..project_checks import check_for
            wd = Path(self.work_dir)
            has = {k: check_for(wd, k) is not None for k in ("test", "lint")}
            want = []
            if bugfix and has["test"]:
                want.append("repro_first")
            if has["test"]:
                want.append("proof")
            if has["lint"]:
                want.append("auto_lint")
            for name in want:
                if name not in self._features and name not in told and \
                        may_suggest(self.root, name):
                    told.add(name)
                    mark_suggested(self.root, name)
                    self.on_event("notice", {"text": "suggestion: " + self._SUGGEST[name]
                                             + f" (uses more tokens; /features never {name} "
                                             "stops this suggestion here)"})
        except Exception as _swallowed:                      # noqa: BLE001
            log_swallowed("loop.suggest_features", _swallowed)

    def _before_call(self, session, messages: list, step: int,
                     steer_queue) -> list:
        """Token-limit notice, queued steer messages, lint notes."""
        from ..notes import aura_note
        # the same chat open in another Aura: their messages aren't seen
        # here (each window keeps its own) — said once
        if getattr(session, "other_writer", False) and not getattr(session, "_other_told", False):
            session._other_told = True
            self.on_event("notice", {"text": (
                "this chat is also being written by another Aura window — what happens there "
                "isn't seen here; keep one window per chat (/resume shows them)")})
        # Token ceiling (opt-in; uncapped by default) — a WARNING, never a
        # stop. Once per ceiling value: raising it re-arms the warning.
        _b = self.m.budgets
        if _b.over_ceiling() and \
                getattr(_b, "warned_at", None) != _b.token_ceiling:
            _b.warned_at = _b.token_ceiling
            self.on_event("notice", {"text": (
                f"token budget passed: {_b.tokens_spent:,} of your "
                f"{_b.token_ceiling:,}-token limit this session — the "
                "work continues. Raise it with /budget set token_ceiling "
                "<n>, or Ctrl+C to stop.")})

        messages = self._offer_skills(session, messages)
        # Live steer: a queued operator/parent message enters as a user turn
        # BEFORE the next proposal. The check says what it is (legend.py):
        # [ASK] answered, the task unchanged · [CHANGE] the task changes · a
        # separate request waits and runs after this task.
        if steer_queue:
            items = []
            while steer_queue:
                msg = steer_queue.pop(0)
                kind = self._steer_kind(getattr(self, "_goal", "") or "", msg)
                items.append({"kind": kind, "text": msg})
                if kind == "later":
                    self._later = getattr(self, "_later", []) + [msg]
                    continue
                label = "[ASK]" if kind == "question" else "[CHANGE]"
                session.append("user", content=f"{label} {msg}", step=step, by="user")
                messages = messages + [{"role": "user", "content": f"{label} {msg}"}]
            self.on_event("steer", {"step": step, "items": items})

        # /machine on: memory got low while the last step ran — the model is
        # told once, as Aura's note (never as the user's words)
        _mn = getattr(self, "machine_notes", None)
        while _mn:
            _note = _mn.pop(0)
            self.on_event("notice", {"text": _note.replace("[MACHINE] ", "")})
            session.note(_note, step=step)
            messages = messages + [{"role": "user", "content": aura_note(_note)}]

        # 2.5: lint the files the last step changed; problems in them go
        # back to the model before its next move
        if self._lint_pending and "auto_lint" in self._features:
            _lint = self._auto_lint(self.work_dir)
            if _lint:
                self.on_event("phase", {"name": "lint",
                                        "detail": _lint.splitlines()[0][:120]})
                session.note(_lint, step=step)
                messages = messages + [{"role": "user", "content": aura_note(_lint)}]

        # A file the task asks for, still missing after several steps — create
        # it now and fill it as you go
        if step in (WRITE_EARLY_STEPS, 2 * WRITE_EARLY_STEPS) and \
                getattr(self, "_early_notes", 0) < 2:
            try:
                _t = getattr(self, "_task_text", "") or ""
                if not _prompt_suppresses_writes(_t):
                    _wd = getattr(self, "work_dir", None) or self.root
                    _miss = _missing_deliverables(_wd, list(_parse_deliverable_names(_t)))
                    if _miss:
                        self._early_notes += 1
                        _note = (f"[WRITE EARLY] {', '.join(_miss[:3])} doesn't exist yet after "
                                 f"{step - 1} steps. Create it now with what you have so far, "
                                 "then add to it as you go — if the run stops early, the work "
                                 "is on disk instead of lost.")
                        session.note(_note, step=step)
                        messages = messages + [{"role": "user", "content": aura_note(_note)}]
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.write_early", _swallowed)
        # Images view_image asked for — attached right after the tool results
        # (tool results can't carry images), for a model that sees
        _iq = getattr(self.m, "_image_queue", None)
        if _iq:
            self.m._image_queue = []
            from ..notes import image_ref
            _wd = Path(getattr(self, "work_dir", None) or self.root)
            parts = [{"type": "text", "text": "[IMAGE] " + ", ".join(i["path"] for i in _iq)}]
            parts += [image_ref(str(Path(i["path"]) if Path(i["path"]).is_absolute()
                                    else _wd / i["path"]), by="aura") for i in _iq]
            _n = session.note(parts, step=step)
            messages = messages + [{"role": "user", "content": _n["content"]}]
        # Background helpers that finished — their result, once
        try:
            from ..background import drain_finished
            for h in drain_finished():
                note = (f"[HELPER DONE] {h.get('handle')} '{h.get('name')}' "
                        f"{'failed: ' + str(h.get('error')) if h.get('status') == 'failed' else 'finished'}"
                        f" ({h.get('steps')} steps)\n{str(h.get('summary') or '')[:3000]}")
                if h.get("write"):
                    from ..helper_copy import describe
                    chs = h.get("changes") or []
                    note += f"\nchanges in its copy: {describe(chs)}"
                    if chs:
                        note += f" — apply with helper_apply(helper='{h.get('handle')}')"
                session.note(note, step=step)
                messages = messages + [{"role": "user", "content": aura_note(note)}]
                if "outside text this helper read tried to give instructions" in note:
                    self._taint("a background helper")
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.helpers_done", _swallowed)
        # What past chats say, when the task points at one — once
        _rn = getattr(self, "_recall_note", None)
        if _rn:
            self._recall_note = None
            self.on_event("phase", {"name": "history", "detail": "searched past chats"})
            session.note(_rn, step=step)
            messages = messages + [{"role": "user", "content": aura_note(_rn)}]
        # The ask-first check — when on, or measured to help this model
        if not getattr(self, "_early_told", True):
            self._early_told = True
            try:
                from ..options import get as _oget
                _ec = _oget("assist.early_check")
                if _ec == "auto":
                    from ..model_facts import fact as _mf
                    _ec = _mf("early_check") or "off"
                if _ec == "on":
                    from ..model_facts import EARLY_CHECK
                    session.note(EARLY_CHECK, step=step)
                    messages = messages + [{"role": "user", "content": aura_note(EARLY_CHECK)}]
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.early_check", _swallowed)
        # Deep research — the way of working, once, at the start; then notes
        # when searches stop finding new things
        _mon = getattr(self, "_research", None)
        if _mon is not None:
            try:
                if _mon.mode == "deep" and not self._deep_told:
                    from ..research import DEEP_INSTRUCTION
                    self._deep_told = True
                    self.m.plan_forced = True    # deep research plans first, by design
                    session.note(DEEP_INSTRUCTION, step=step)
                    messages = messages + [{"role": "user", "content": aura_note(DEEP_INSTRUCTION)}]
                    self.on_event("notice", {"text": "deep research: plan first, two sources "
                                                     "per question, until the scope is exhausted"})
                _rn = _mon.next_note(self._plan_items())
                if _rn:
                    self.on_event("phase", {"name": "research", "detail": _rn.splitlines()[0][:120]})
                    session.note(_rn, step=step)
                    messages = messages + [{"role": "user", "content": aura_note(_rn)}]
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.research_note", _swallowed)

        # A background process that ended since the last step is reported now —
        # the model never has to poll for it
        _reg = getattr(self.m, "_procs", None)
        if _reg is not None:
            try:
                from ..tools.process import _ago, _scrub
                for _p, _q in _reg.prompts_untold():
                    _note = (f"[BACKGROUND] {_p.id} '{_p.cmd[:80]}' is WAITING FOR INPUT: "
                             f"{_q!r} — answer with process_input(id='{_p.id}', text=...), "
                             "or ask the user if it's their decision")
                    self.on_event("notice", {"text": _note.splitlines()[0][:160]})
                    session.note(_note, step=step)
                    messages = messages + [{"role": "user", "content": aura_note(_note)}]
                for _p in _reg.ended_unreported():
                    _last = [ln for ln in _scrub(_p.tail(8)) if ln.strip()]
                    _note = (f"[BACKGROUND] {_p.id} '{_p.cmd[:80]}' exited with code "
                             f"{_p.exit_code} after {_ago(_p.started)}"
                             + (". Last lines:\n" + "\n".join(_last) if _last else "."))
                    self.on_event("notice", {"text": _note.splitlines()[0]})
                    session.note(_note, step=step)
                    messages = messages + [{"role": "user", "content": aura_note(_note)}]
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.process_notes", _swallowed)
        return messages

    def _task_state(self, task: str, messages: list) -> str:
        """What the run is doing, from Aura's own records (no model): the goal,
        the files changed so far, the open todo items — kept through a
        compaction.
        """
        try:
            from .workset import WorkSet
            changed = list(dict.fromkeys(
                o.res[1] for o in WorkSet.scan(messages)
                if o.name in ("write_file", "edit_file") and o.res[0] == "file"))
            todos = [i for i in self._plan_items()
                     if i.get("status") not in ("done", "archived")]
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.task_state", _swallowed)
            changed, todos = [], []
        goal = getattr(self, "_goal", "") or task
        lines = ["[TASK STATE — kept by Aura through compaction]",
                 f"goal (the user's words): {' '.join(str(goal).split())[:400]}"]
        if getattr(self, "_reply", ""):
            lines.append(f"the user's latest reply: {self._reply[:200]}")
        if changed:
            lines.append("files changed so far: " + ", ".join(changed[:20]))
        if todos:
            lines.append("open todo: " + "; ".join(
                str(i.get("text") or i.get("title") or "")[:80] for i in todos[:10]))
        return "\n".join(lines)

    def _context_for_call(self, session, messages: list, step: int,
                          task: str) -> list:
        """What the model sees this step: prune/compact, then journal, rules,
        symbols, todo, memory, skills, graph, drift check, cache alignment."""
        self.on_event("llm_start", {"step": step})
        # Trim old outputs by use, in batches — sticky stubs keep the start of
        # the conversation the same between batches (the old age-based prune
        # changed it on every step: 22% cache reuse)
        if self.governor:
            from .workset import WorkSet
            ws = getattr(self, "_workset", None)
            if ws is None:
                ws = self._workset = WorkSet()
            messages, rep = ws.apply(messages, self.governor.count,
                                     self.governor.trim_trigger(), store=self.root)
            if rep["trimmed"]:
                self.on_event("prune", {"step": step, "pruned": rep["trimmed"],
                                        "batch": rep["batch"]})
                self.on_event("phase", {"name": "context trim", "detail": (
                    f"{rep['trimmed']} old outputs trimmed (used or superseded; "
                    "each can be reopened) — /context lists them")})
            told = getattr(self, "_thrash_told", set())
            for path in ws.thrashing():
                if path not in told:
                    told.add(path)
                    messages = messages + [{"role": "system", "content": (
                        f"[CONTEXT] {path} was trimmed and fetched back twice. Read "
                        "only the lines you need (read_file with offset/limit, or "
                        "search_files) instead of the whole file.")}]
            self._thrash_told = told
        # Governor gate — compact when over 60% of ctx window
        if self.governor and self.governor.needs_compaction(messages):
            c = self.governor.compact(messages, state=self._task_state(task, messages))
            messages = Governor.as_messages(c)
            self._reads_seen = {}          # Earlier outputs are gone now
            # Persist compaction so replay doesn't resurrect turns
            self.session_persist_compact(session, c)
            self.on_event("compaction",
                          {"step": step, "dropped": c.dropped,
                           "verbatim_kept": len(c.verbatim_lines)})
            # 2.9: the operator's compact hooks (notice only; can't block)
            if getattr(self.m, "hooks_root", None) is not None:
                try:
                    from ..cronops import run_hooks
                    run_hooks(self.m.hooks_root, "compact",
                              {"step": step, "dropped": c.dropped})
                except Exception as _swallowed:
                    log_swallowed("loop.compact_hook", _swallowed)
            self.on_event("phase", {"name": "compaction",
                                    "detail": f"{c.dropped} turns summarized at 60% ctx"})
        # Tools found after the list was fixed — described at the end of the
        # call (the tail never breaks the server's cache)
        _tl = getattr(self, "tool_list", None)
        _wn = _tl.waiting_note() if _tl is not None else ""
        if _wn:
            messages = messages + [{"role": "system", "content": _wn}]
        # Journal injection — structured notes beat retrieval
        if self.governor:
            j = (self.root / "notes.md")
            if j.exists():
                inj = Governor.journal_injection(j.read_text(encoding="utf-8", errors="replace"))
                if inj:
                    messages = messages + [{
                        "role": "system",
                        "content": "[AGENT JOURNAL — last entries]\n" + inj,
                    }]
        # The project part (rules pointer, memory, skills index) sits right
        # after the system prompt.
        project_part: list = []
        # Operator constraints — file-backed, pointer-injected. Fixed-size
        # block every turn; full text pulled at write/execute time by the
        # mediator hook (never enters compaction at all).
        try:
            from ..constraints import pointer_block
            cb = pointer_block(self.m.constraints_root or self.root)
            if cb:
                project_part.append({"role": "system", "content": cb})
        except Exception as _swallowed:
            log_swallowed("loop.constraints_inject", _swallowed)  # Constraints must never kill the loop
        # Project symbols — enumerated real entities (files/defs/ imports).
        # Injected once at step 1 (cache-aligned); the model pulls
        # fresh/filtered views via the `symbols` tool. Kills the
        # 85%-project-API hallucination class at the prompt level.
        if step == 1 and task:
            try:
                table = getattr(self, "_symbol_table", None)
                if table is None:
                    from ..symbols import SymbolTable
                    table = SymbolTable(getattr(
                        self, "work_dir", None) or self.root)
                    self._symbol_table = table
                sh = table.sheet(max_lines=40)
                if sh:
                    messages = messages + [{
                        "role": "system", "content": sh}]
                    # the sheet points at the `symbols` tool: send it too
                    if getattr(self, "tool_list", None) is not None:
                        self.tool_list.add_set("code", "the symbol sheet is in the prompt")
            except Exception as _swallowed:
                log_swallowed("loop.symbols_inject", _swallowed)  # Symbols must never kill the loop
        # Todo state — this chat's plan, visible every step while the switch is on
        todo_items = self._plan_items()
        from ..todo import plan_on as _plan_on
        if _plan_on() and not todo_items and not getattr(self.m, "turn_readonly", False):
            messages = messages + [{"role": "system", "content": "[PLAN SWITCH ON] "
                                    + TODO_INSTRUCTION}]
        if todo_items:
            open_items = [i for i in todo_items if i["status"] != "archived"]
            if open_items:
                from ..todo import render_block
                messages = messages + [{
                    "role": "system", "content": render_block(todo_items)}]
        # Persistent memory — durable facts + operator preferences
        try:
            from ..memory_store import memory_injection_block, pick_memory
            # What's relevant to THIS task, picked once per task (the same text
            # on every call of the run, so the cache keeps it)
            if getattr(self, "_mem_for", None) != task:
                self._mem_for = task
                self._mem_text = pick_memory(self.root, task,
                                             getattr(self, "work_dir", None))
            block = memory_injection_block(self._mem_text)
            if block:
                project_part.append({"role": "system", "content": block})
        except Exception as _swallowed:
            log_swallowed("loop.memory_inject", _swallowed)  # Memory must never kill the loop
        # Skills: only the person's PINNED skills are listed every turn; the
        # skills for big work are picked with the person (_offer_skills) and
        # sent once, in full. Listing every skill each turn cost tokens and
        # didn't help (SkillSeek 2026).
        try:
            from ..skill_curator import pinned as _pinned
            from ..skills import load_skills, skill_index_block
            _pins = _pinned(self.root)
            if _pins:
                _all = load_skills(self.root)
                _sk = {n: _all[n] for n in sorted(_pins) if n in _all}
                if _sk:
                    project_part.append({"role": "system", "content": skill_index_block(_sk)})
        except Exception as _swallowed:
            log_swallowed("loop.skills_inject", _swallowed)  # Skills must never kill the loop
        if project_part:
            at = 1 if messages and messages[0].get("role") == "system" else 0
            messages = messages[:at] + project_part + messages[at:]
        # Intent anchoring: in a long run, the user's current request word for
        # word at the END of each call — where attention is strongest (Manus
        # "recitation"); the tail never breaks the server cache
        if step >= GOAL_REMINDER_FROM:
            try:
                from ..options import get as _oget
                if _oget("assist.goal_reminder"):
                    _goal = " ".join(str(getattr(self, "_task_text", "") or task).split())[:600]
                    if _goal:
                        messages = messages + [{"role": "system", "content": (
                            "[TASK — the user's current request, word for word] " + _goal)}]
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.goal_reminder", _swallowed)
        # Resume-from-graph — task graph state visible every step
        if self.reviewer is not None and self.inject_graph:
            gb = self.reviewer.graph.context_block()
            if gb:
                messages = messages + [{"role": "system", "content": gb}]
        # Intent drift audit — out-of-band, after tool activity
        if self.intent_contract is not None and self.intent_calls \
                and step > 1 and "drift_check" in self._features:
            from ..intent import audit_calls
            drift = audit_calls(self.intent_calls, self.intent_contract)
            # Each finding once per run.
            seen = getattr(self, "_drift_seen", set())
            new = [d for d in drift if d not in seen]
            self._drift_seen = seen | set(new)
            if new:
                # re-read what the user asked; if it still doesn't fit, ask the
                # user.
                messages = messages + [{
                    "role": "system",
                    "content": "[INTENT CHECK — out-of-band]\n"
                               "These actions don't obviously match the "
                               "task:\n"
                               + "\n".join(f"- {d}" for d in new[:5])
                               + "\nFirst re-read the user's current "
                               "instruction (the ACTIVE TASK block). If "
                               "the actions serve it, say in one line why "
                               "and continue. If you are still not sure "
                               "the user wants this, stop and ask the "
                               "user before doing more."}]
                self.on_event("intent_drift", {"step": step,
                                               "findings": len(new),
                                               "first": new[0]})
        # KV-cache alignment — restructure for the backend's cache
        # (static-first, block-boundary padding, volatile-last). No-op when the
        # profile sets cache_mode: off.
        if self.llm is not None and getattr(self, "cache_mode", "off") != "off":
            messages = align_messages(messages, self.cache_mode)
        return messages

    def _call_model(self, session, messages: list, step: int):
        """Call the model, account its tokens, record its reply. Returns
        (reply, messages)."""
        # UX: spinner until the first streaming delta lands (kills the dead-air
        # between tool result and first token). The delta printer receives it
        # via self._active_spin and cancels it on first content/reasoning/tool
        # delta; the finally below is the backstop. the old spinner prints to
        # the screen; a chat UI with its own live area (stream_sink) draws
        # activity itself
        self._active_spin = (SpinDuring(label="thinking")
                             if getattr(self, "stream_sink", None) is None
                             else None)
        if self._active_spin:
            self._active_spin.__enter__()
        try:
            res, messages = self._chat_recovering(session, messages)
            _sw = getattr(self.llm, "switched", None)
            if _sw:                         # A fallback model took over
                self.llm.switched = None
                self.on_event("notice", {"text": _sw})
        finally:
            if self._active_spin:
                self._active_spin.cancel()
            self._active_spin = None
            if getattr(self, "stream_sink", None) is None:
                hud.nl_if_needed()
        if getattr(self, "tool_list", None) is not None:
            self.tool_list.calls += 1          # Parking: when each group came
            self.tool_list.frozen = True       # The list sent stays the same now
        # cache inspector: what stayed the same from the last call, and why not
        _lc = getattr(self.llm, "last_cache", None)
        if _lc and self._receipt is not None:
            try:
                from ..cache_inspect import fold
                from ..model_facts import _cached_tokens
                self._receipt.data["cache"] = fold(
                    self._receipt.data.get("cache"), _lc, _cached_tokens(res.usage or {}),
                    (res.usage or {}).get("prompt_tokens") or 0)
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.cache_inspect", _swallowed)
        # Real usage tokens into the ledger when the provider sends them
        if res.usage:
            self.m.budgets.add_tokens(
                (res.usage.get("prompt_tokens") or 0)
                + (res.usage.get("completion_tokens") or 0))
            if self.governor:
                self.governor.record_usage(res.usage)
                # Learn the real chars-per-token from this call
                self.governor.calibrate(
                    messages, res.usage.get("prompt_tokens") or 0)
            # Receipt token snapshot (independent of governor)
            self._receipt_snapshot(step, res.usage)
            # Per-call accounting retained for the last reply line
            self.last_usage = {"prompt": res.usage.get("prompt_tokens") or 0,
                               "completion": res.usage.get("completion_tokens") or 0}
            # Cache telemetry (any provider shape) — measured, not assumed
            hit, miss = cache_usage_fields(res.usage)
            if hit or miss:
                self.cache_hits += hit
                self.cache_misses += miss
        self.on_event("llm_done", {"step": step, "usage": res.usage})
        # V: ctx% must track EVERY llm call (main loop + landing), not just the
        # finish-verify path — the bar read 0% while the ledger grew because
        # ctx_state only fired at line ~1000.
        if self.governor:
            # What the call really sent: the server's own count when it gives
            # one, else Aura's estimate of the messages sent — never the whole
            # saved chat
            try:
                g = self.governor
                real = int((res.usage or {}).get("prompt_tokens") or 0)
                self.on_event("ctx_state", {
                    "used": real or int(getattr(self, "_sent_est", 0) or g.count(messages)),
                    "measured": bool(real),
                    "trim_at": g.trim_trigger(),
                    "compact_at": int(g._usable() * g.threshold)})
            except Exception:
                pass

        # The same thoughts across replies: the run's replies are watched
        # together until real progress (a file written or edited, a result
        # recorded) — reading new things while thinking the same doesn't reset
        # it. Stricter than inside one reply (90 %): replies may rightly share
        # some lines
        if getattr(res, "finish_reason", None) != "cycle_guard":
            from ..stream_guard import ProgressWatch
            if getattr(self, "_run_watch", None) is None:
                self._run_watch = ProgressWatch(share=0.9)
            if self._run_watch.feed((res.reasoning or "") + "\n" + (res.content or "") + "\n"):
                res.finish_reason = "cycle_guard"
                self._run_watch = None
        # The reply was thinking in circles and was stopped mid-stream
        # (stream_guard): drop it whole — thinking, text, half-made tool calls
        # — never stored or sent back. _finish_or_resume tells the model.
        if getattr(res, "finish_reason", None) == "cycle_guard":
            self._cycle_chars = len(res.reasoning or "")
            res.reasoning, res.content, res.tool_calls = "", "", []
            return res, list(session.messages)

        assistant: dict[str, Any] = {"role": "assistant"}
        if res.content:
            assistant["content"] = res.content
        elif not res.tool_calls:
            # An empty reply: stored with no content at all, the next request
            # carried it and Ollama refused the whole request
            assistant["content"] = ""
        if res.reasoning:
            assistant["reasoning_content"] = res.reasoning  # VLLM extension
        if getattr(res, "native", None):
            assistant["native"] = res.native    # Sent back to the API that made it
        if res.tool_calls:
            assistant["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": tc["arguments"]}}
                for tc in res.tool_calls]
        session.append(assistant["role"], **{
            k: v for k, v in assistant.items() if k != "role"},
            step=step,   # Step-index stamp for checkpoint lookup
            usage=res.usage or None)
        return res, messages

    def _find_way_check(self, session, task: str, res, step: int) -> bool:
        """find_way.py layer 3: only with the toggle on, only when the answer
        has can't-words (most answers cost nothing), once per run, one small
        side call. True = a note was added; the run goes on one more step."""
        if getattr(self, "parent_run", "") or self.llm is None or getattr(self, "_fw_nudged", False)                 or getattr(self.m, "_get_declined", False):    # The user said no: that's the answer
            return False
        from .. import find_way as FW
        answer = getattr(res, "content", "") or ""
        if not FW.on() or not FW.maybe_gave_up(answer):
            return False
        try:
            r = self.llm.chat([{"role": "system", "content": FW.INSTRUCTIONS},
                               {"role": "user", "content": FW.question(task, answer)}],
                              tools=None, temperature=0, side=True)
            kind, missing = FW.parse(getattr(r, "content", "") or "")
        except Exception as _swallowed:                   # noqa: BLE001
            log_swallowed("loop.find_way", _swallowed)
            return False
        if kind != "gave_up":
            return False
        self._fw_nudged = True
        self.on_event("notice", {"text": (
            "Aura stopped short" + (f" — {missing} is missing" if missing else "")
            + "; 'find a way' is on, so it looks for one (it asks you before any effort)")})
        session.note(FW.note(missing), step=step)
        return True

    def _finish_or_resume(self, session, messages: list, res, step: int,
                          task: str, kill_check):
        """The model replied without tool calls. A check may send it back for
        another step — (None, messages) — otherwise the run ends:
        (StepRecord, messages)."""
        # a reply stopped for thinking in circles (stream_guard; dropped in
        # _call_model): the model is told to decide; the 2nd time in a run
        # the user is told, the 3rd stops the run.
        if getattr(res, "finish_reason", None) == "cycle_guard":
            _chars = getattr(self, "_cycle_chars", 0)
            self._cycles = getattr(self, "_cycles", 0) + 1
            self.on_event("loopwatch", {"signal": "thinking_in_circles", "times": self._cycles,
                                        "thinking_chars": _chars})
            if self._receipt is not None:
                self._receipt.note(f"reply stopped: thinking in circles ({_chars:,} chars)")
            if self._cycles >= 2:
                self._model_trouble("it keeps thinking in circles")
            if self._cycles >= 3:
                self.on_event("notice", {"text": "the model went in circles a third time — stopping here"})
                rec = StepRecord(step=step, content="",
                                 error="the model kept thinking in circles (stopped 3 times)")
                self._receipt_snapshot(step)
                self._receipt_finalize(rec, model_claim="")
                return rec, messages
            self.on_event("notice", {"text": "the model was thinking in circles — "
                                             "stopped that reply and asked it to decide"})
            session.note(
                "[GOING IN CIRCLES] Your last reply kept repeating the same thoughts and was "
                "stopped. Do not reconsider again: decide with what you already know and act "
                "(call a tool or answer). If something you need is missing, say what it is.",
                step=step)
            return None, list(session.messages)

        # find_way.py (the user's /findaway): an answer that gives up because
        # something is missing gets one note and one more step
        if self._find_way_check(session, task, res, step):
            return None, list(session.messages)

        # At a clean exit, check the named files on disk; if any are missing
        # and the one-shot budget allows, inject a resume turn and continue the
        # loop instead of returning.: gate must see the REAL task deliverables,
        # not just the operator prompt (arena prompt names only TASK.md -> gate
        # was a no-op). Merge prompt-derived names with write-verb names parsed
        # from work_dir/TASK.md.
        wd = getattr(self, "work_dir", None) or self.root
        # Scope priority: the CURRENT instruction outranks TASK.md. When the
        # prompt explicitly forbids creating files / demands answer-only,
        # TASK.md-derived deliverable demands are superseded — no gate resume
        # for them.
        suppress = _prompt_suppresses_writes(task)
        names = list(_parse_deliverable_names(task))
        if not suppress:
            _task_md = Path(wd) / "TASK.md"
            names += _task_deliverable_names(
                _task_md.read_text(encoding="utf-8")
                if _task_md.exists() else None)
        names = list(dict.fromkeys(names))
        missing = _missing_deliverables(wd, names)
        # Ask once more — to carry on, naming any files still owed — and tell
        # the user; if the very next reply fails the same way, stop and say
        # why. (Replaces the old one-per-run "final answer now" landing, which
        # made models answer in text instead of writing the file — campaign.)
        empty = not (res.content or "").strip()
        cut = getattr(res, "finish_reason", None) == "length"
        if (empty or cut) and not kill_check():
            what = ("was empty" if empty
                    else "was cut off by the model's length limit")
            if getattr(self, "_retry_step", None) == step - 1:
                self.on_event("notice", {"text": (
                    f"the model's reply {what} again — stopping here")})
                self._model_trouble(f"its reply {what} twice")
                rec = StepRecord(step=step, content=res.content or "",
                                 error=f"the model's reply {what} twice "
                                       "in a row")
                rec.gate_missing = missing
                self._receipt_snapshot(step)
                self._receipt_finalize(rec, model_claim=res.content or "")
                return rec, messages
            self._retry_step = step
            self.on_event("notice", {"text": (
                f"the model's reply {what} — asking it once more")})
            owed = (" These files the task asks for are still missing: "
                    + ", ".join(missing) + " — write them."
                    if missing else "")
            session.note((
                ("[EMPTY REPLY] Your last reply was empty. Continue the "
                 "task from where you are." if empty else
                 "[CUT OFF] Your last reply was cut off by the length "
                 "limit. Continue exactly where it stopped.") + owed),
                step=step)
            return None, list(session.messages)
        # Silent-skip guard (LOCAL, zero tokens): the #1 tool-reliability
        # failure in the corpus is the model NARRATING success without calling
        # the tool (up to 80% on some tools). A todo item still
        # pending/in_progress at a clean exit is a narrated-but-unexecuted
        # step. Block completion once (budget below) with a mechanical nudge;
        # never invents content, only demands the artifact.
        skipped = ([] if getattr(self.m, "turn_readonly", False) else   # a question: never
                   _silent_skip_check(self.root, getattr(self, "_run_t0", 0.0),
                                      getattr(self.m, "plan_chat", ""),
                                      getattr(self.m, "plan_forced", False)))
        if skipped and self._skip_guard_used < _SKIP_GUARD_BUDGET:
            self._skip_guard_used += 1
            self.on_event("phase", {
                "name": "silent-skip guard",
                "detail": f"unfinished plan items: {', '.join(skipped)}"})
            session.note((
                "[SILENT-SKIP GUARD] Your plan still has unfinished "
                "items (" + ", ".join(skipped) + "). Either complete "
                "them with real tool calls now, or mark them done "
                "with evidence — do not narrate completion. Your answer "
                "above has already reached the user: don't repeat it — "
                "after this, say only what is new (or just 'done')."), step=step)
            messages = list(session.messages)
            self.on_event("skip_guard", {"step": step,
                                         "skipped": skipped})
            return None, messages
        # Proof before the final answer — re-check the reads the answer rests
        # on and run the project's tests; one turn to fix failing tests, then
        # the result is recorded either way
        if "proof" in self._features and self._needs_proof(wd):
            from .validator import proof_summary, prove_run
            _reads = [r for r in self._reads
                      if r["seq"] > self._last_change_seq]
            proof = prove_run(self.m, self.dispatch, wd, _reads)
            self._proved_seq = self._seq
            self._last_proof = proof
            self.on_event("phase", {"name": "proof",
                                    "detail": proof_summary(proof)})
            if (proof["status"] == "tests_failed"
                    and self._proof_landings_left > 0):
                self._proof_landings_left -= 1
                t = proof["tests"]
                verify_msg = "\n".join((
                    "[VERIFY] Before you finish: the project's tests "
                    f"fail after your changes ({t.get('command')}, "
                    f"exit {t.get('exit_code')}):",
                    str(t.get("tail", ""))[-600:],
                    "Fix the failure, or if it is unrelated to your "
                    "change, say so plainly in your final answer."))
                session.note(verify_msg, step=step)
                messages = list(session.messages)
                return None, messages
        # The answer's citations — links really opened, quotes really in the
        # pages read (sources.py); one turn to fix, then the result goes into
        # the receipt either way only when this run searched or read the web:
        # an ordinary answer that mentions a link (or quotes an error message)
        # is not a research citation
        cite = None
        if getattr(self, "_web_used", False):
            try:
                from ..sources import check as _cite_check
                _ctx = "\n".join(str(mm.get("content") or "")
                                  for mm in session.messages[:-1]
                                  if mm.get("role") in ("user", "tool"))
                cite = _cite_check(self.root, res.content or "", _ctx)
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.citations", _swallowed)
        if cite is not None and not cite["ok"] and self._cite_landings_left > 0:
            from ..sources import fix_message
            self._cite_landings_left -= 1
            self.on_event("phase", {"name": "sources", "detail": (
                "some citations aren't backed by the pages read — a short correction follows")})
            session.note(fix_message(cite), step=step)
            return None, list(session.messages)
        _asm = list(getattr(self.m, "_assumptions", None) or [])
        if _asm:
            # What Aura had to assume because nobody could answer
            for a in _asm:
                self.on_event("notice", {"text": f"assumed (nobody to ask): {a['question'][:100]} -> {a['assumed'][:80]}"})
            if self._receipt is not None:
                self._receipt.data["assumptions"] = _asm
        if cite is not None:
            from ..sources import summary as _cite_summary
            self.on_event("notice", {"text": _cite_summary(cite)})
            if self._receipt is not None:
                self._receipt.add_check("citations", cite["ok"], _cite_summary(cite))
        rec = StepRecord(step=step, content=res.content)
        rec.proof = self._last_proof
        rec.streamed = bool(res.content)
        rec.gate_missing = missing
        # Finalize receipt + 5-line summary at clean exit
        self._receipt_snapshot(step)
        self._receipt_finalize(rec, model_claim=res.content or "")
        return rec, messages

    def _run_tools(self, session, res, step: int) -> None:
        """Run this step's tool calls (reads in parallel), loop watch, failure
        streak, reviewer."""
        # Parallel read-only dispatch — READ-class tools run concurrently via a
        # thread pool; write/execute stay sequential
        readonly, ordered = [], []
        # use_tool(name, arguments) is the real call to a waiting tool (the
        # list stays fixed for the cache): from here on it IS that call —
        # same gates, same grade, same result
        for tc in res.tool_calls:
            if tc.get("name") == "use_tool" and not tc.get("parse_error"):
                a = tc.get("parsed_arguments") or {}
                inner, inner_args = str(a.get("name") or "").strip(), a.get("arguments") or {}
                if isinstance(inner_args, str):
                    try:
                        inner_args = json.loads(inner_args or "{}")
                    except ValueError:
                        tc["parse_error"] = "use_tool: 'arguments' isn't valid JSON"
                if not inner or inner == "use_tool":
                    tc["parse_error"] = "use_tool needs the tool's name"
                elif not tc.get("parse_error"):
                    tc["name"], tc["parsed_arguments"] = inner, (inner_args if isinstance(inner_args, dict) else {})
        tl = getattr(self, "tool_list", None)
        if tl is not None:
            for tc in res.tool_calls:
                if tc["name"] in self.m.tools and not tl.is_sent(tc["name"]):
                    tl.add_tool(tc["name"], "the model called it")
                tl.mark_used(tc["name"])
        for tc in res.tool_calls:
            spec = self.m.tools.get(tc["name"])
            if spec and spec.blast_radius.value == "read":
                readonly.append(tc)
            else:
                ordered.append(tc)
        parallel_results: dict[str, dict] = {}
        if len(readonly) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(self._execute_tool, tc["name"],
                                    tc["parsed_arguments"],
                                    tc.get("parse_error")): tc
                        for tc in readonly}
                for fut, tc in futs.items():
                    parallel_results[tc["id"]] = fut.result()
            self.on_event("parallel_reads", {"step": step,
                                             "count": len(readonly)})
        for tc in res.tool_calls:
            _spec_blast = (self.m.tools.get(tc["name"]).blast_radius.value
                           if self.m.tools.get(tc["name"]) else "read")
            self.on_event("tool_start", {"step": step, "tool": tc["name"],
                                         "blast": _spec_blast,
                                         "args": tc.get("parsed_arguments") or {}})
            if tc["id"] in parallel_results:
                result = parallel_results[tc["id"]]
            else:
                result = self._execute_tool(tc["name"], tc["parsed_arguments"],
                                            tc.get("parse_error"))
            spec = self.m.tools.get(tc["name"])
            # Loopwatch: duplicate-burst + stall detection feeds a mentor turn
            # — STOP THE LOOPING, never kill the session (agent self-corrects).
            try:
                from .loopwatch import (STALL_CALLS, LoopState,
                                        mentor_message, stall_message)
                if getattr(self, "_loopwatch", None) is None:
                    self._loopwatch = LoopState()
                args_ = tc.get("parsed_arguments") or {}
                verdict = self._loopwatch.record_call(tc["name"], args_)
                stalled = self._loopwatch.record_result(
                    tc["name"], args_, result,
                    read_only=bool(spec and spec.blast_radius.value == "read"))
                if (result.get("status") == "ok"
                        and tc["name"] in (*FILE_WRITERS, "finding_add")):
                    self._run_watch = None       # Real progress: thinking may repeat
                # mentor turn ONLY on the loop signals (3-same soft nudge,
                # 5-identical burst, 6 calls with nothing new) — never on
                # normal calls
                if verdict.get("nudge") or verdict.get("burst"):
                    msg = mentor_message(verdict.get("same_run", 0))
                    session.append("system", content=msg.get("content", ""),
                                   step=step)
                    self.on_event("loopwatch", {"step": step,
                                                "tool": tc["name"],
                                                "same_run": verdict.get(
                                                    "same_run", 0),
                                                "verdict": str(verdict)[:80]})
                elif stalled:
                    session.append("system",
                                   content=stall_message()["content"],
                                   step=step)
                    self.on_event("loopwatch", {"step": step,
                                                "tool": tc["name"],
                                                "stall": True,
                                                "calls": STALL_CALLS})
                # One target probed again and again; warnings ignored →
                # blocked; looping on after a block → the run stops
                for _w in self._loopwatch.after(
                        tc["name"], args_, result.get("status") == "ok", verdict, stalled):
                    session.append("system", content=_w, step=step)
                    self.on_event("loopwatch", {"step": step, "tool": tc["name"],
                                                "note": _w[:100]})
            except Exception as _swallowed:
                log_swallowed("loop.loopwatch", _swallowed)
            self.on_event("tool_done", {
                "step": step, "tool": tc["name"],
                "status": result["status"],
                "blast": spec.blast_radius.value if spec else "read",
                "duration_ms": result.get("duration_ms", 0),
                "detail": str(result.get("result", "")
                              or result.get("error", ""))[:60],
                "approval_card": result.get("approval_card", ""),
                # for chat UIs that show results and diffs
                "args": tc.get("parsed_arguments") or {},
                "result": str(result.get("result", "")
                              or result.get("error", ""))[:6000],
                "truncated": bool(result.get("truncated")),
            })
            content = result["result"] if result["status"] == "ok" \
                else json.dumps(result)
            # Mentor turn — appended as its own system message so the model
            # sees the nudge BEFORE its next proposal
            if mentor := result.pop("mentor", None):
                session.append("system", content=mentor)
            # Failure-streak for the reviewer trigger. Waiting for operator
            # approval is not a failure — it must not summon a reviewer. Real
            # failures are kept (with their error text) for the reviewer's
            # context: the audit log has neither.
            _awaiting = (result["status"] == "awaiting_approval"
                         or bool(result.get("approval_card")))
            # Malformed tool-call JSON is a formatting slip the model fixes by
            # re-sending (ADD-2) — not a strategy problem a reviewer can help
            # with, so it doesn't build the streak
            _malformed = bool(tc.get("parse_error"))
            if _malformed:
                pass
            elif result["status"] != "ok" and not _awaiting:
                self.failure_streak += 1
                _spec_f = self.m.tools.get(tc["name"])
                try:
                    _tgt = _spec_f.extract_target(
                        tc.get("parsed_arguments") or {}) if _spec_f else None
                except Exception:
                    _tgt = None
                self._run_failures.append({
                    "call": tc["name"], "target": _tgt,
                    "args": tc.get("parsed_arguments") or {},
                    "status": result["status"],
                    "error": str(result.get("error", ""))[:300]})
            elif result["status"] == "ok":
                self.failure_streak = 0
            # how well this model calls tools, from real use (model_facts):
            # malformed, unknown tool, or a required argument missing
            try:
                _err = str(result.get("error") or "")
                _bad = (bool(tc.get("parse_error")) or _err.startswith("unknown tool")
                        or f"{tc['name']} needs " in _err)
                tu = self._tool_use = getattr(self, "_tool_use", None) or [0, 0]
                tu[0] += 1
                tu[1] += 1 if _bad else 0           # Saved once at the end of the run
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.tool_skill", _swallowed)
            session.append("tool", content=content,
                           tool_call_id=tc["id"], name=tc["name"],
                           step=step)  # Step-index stamp
            # Role-relabel: a failure is echoed as an outside fact, not left in
            # the model's own voice. Now the same ERROR climbs a ladder
            # (loopwatch.FailureLadder): the likely cause and "don't run it
            # unchanged" → the user is told → those calls are blocked → the run
            # stops with a report. A command that exited non-zero counts too. A
            # file written or edited (real progress) starts it over.
            _err = None
            if not _awaiting and not _malformed:
                from .loopwatch import FailureLadder, LoopState, failure_of
                _err = failure_of(tc["name"], result)
            # /discuss: a change refused (or a change tool called that wasn't
            # offered) is the mode working, not a failure to climb or report
            if _err and self._talk_only() and \
                    getattr(self.m.tools.get(tc["name"]), "blast_radius", None) != BlastRadius.READ:
                _err = None
            if _err and not result.get("_relabeled"):
                # Verify-before-retry: a cheap local check whether the effect
                # already landed (timeouts, unknowns)
                post = _postcondition_probe(result, tc) if result["status"] != "ok" else None
                if post:
                    result["_postcondition"] = post
                if getattr(self, "_ladder", None) is None:
                    self._ladder = FailureLadder()
                if getattr(self, "_loopwatch", None) is None:
                    self._loopwatch = LoopState()
                _note, _tell = self._ladder.record(tc["name"], tc.get("parsed_arguments") or {},
                                                   _err, self._loopwatch)
                session.note(_note + (f" [POSTCONDITION] {post}" if post else ""), step=step)
                if _tell:
                    self.on_event("notice", {"text": _tell})
                    if self._receipt is not None:
                        self._receipt.note(_tell)
                result["_relabeled"] = True
            elif (not _err and result["status"] == "ok"
                  and tc["name"] in (*FILE_WRITERS, "finding_add")
                  and getattr(self, "_ladder", None) is not None):
                self._ladder.clear()             # Real progress: start over
            elif (not _err and result["status"] == "ok"
                  and getattr(self, "_ladder", None) is not None):
                self._ladder.got_past(tc["name"])
        # Auto reviewer on failure-streak >= 2 — fresh-context diagnosis
        # becomes a session system message before next proposal
        if (self.reviewer is not None and self._reviews_left > 0
                and "reviewer" in self._features
                and self.reviewer.should_review(self.failure_streak, False)):
            self._reviews_left -= 1      # Capped per run: one extra call each
            try:
                review = self.reviewer.review(
                    failure_streak=self.failure_streak, before_report=False,
                    failed_calls=self._run_failures)
            except Exception as e:  # Reviewer must never kill the loop
                self.on_event("reviewer_error", {"error": str(e)})
            else:
                if review.get("ran"):
                    self.on_event("reviewer_ran",
                                  {"streak": self.failure_streak,
                                   "recommend": review.get("recommend")})
                    session.append(
                        "system",
                        content="[REVIEWER — fresh-context diagnosis]\n"
                                f"DIAGNOSIS: {review.get('diagnosis', '')}\n"
                                "OPTIONS: " + " | ".join(
                                    review.get("options", [])) + "\n"
                                f"RECOMMEND: {review.get('recommend', '')}")
                    self.failure_streak = 0

    # ------------------------------------------------- receipt hooks
    def _engine_diff_guarantee(self, rec: "StepRecord") -> "StepRecord":
        """GUARANTEE (final turn): the loop never exits without answering 'what
        changed on disk?'. Order: (1) the model's own ```diff block (from the
        session tape — zero tokens), (2) engine salvage via git diff HEAD +
        untracked. Honesty: diff_source names where it came from; a
        genuinely-unchanged workspace stays diff_emitted=False with
        diff_source='none'.
        """
        try:
            model_diff = _track_model_diff(getattr(self, "_session_ref", None))
            if model_diff.strip():
                rec.diff_emitted = True
                rec.diff_source = "model"
                rec.work_diff = model_diff
                return rec
        except Exception as _swallowed:
            log_swallowed("loop.model_diff", _swallowed)
        try:
            wd = getattr(self, "work_dir", None) or self.root
            from ..contracts.session_location import git_diff_checked
            patch, _err = git_diff_checked(wd)
            self._git_notice(_err)
            if patch.strip():
                rec.diff_emitted = True
                rec.diff_source = "engine-salvage"
                rec.work_diff = patch
                try:
                    sal = Path(self.root) / "salvage"
                    sal.mkdir(parents=True, exist_ok=True)
                    (sal / "patch.diff").write_text(patch, encoding="utf-8")
                except Exception as _swallowed:
                    log_swallowed("loop.salvage_patch", _swallowed)
                return rec
            rec.diff_source = "none"
        except Exception:
            rec.diff_source = rec.diff_source or "none"
        # (3) no git: Aura's own snapshot from the start of the task
        base = getattr(self, "_work_baseline", None)
        if base is not None:
            try:
                from ..workdiff import changes
                if base.too_big:
                    rec.diff_source = "none (project too big for Aura's snapshot)"
                    return rec
                patch, files = changes(base)
                if patch.strip():
                    rec.diff_emitted = True
                    rec.diff_source = "aura-snapshot"
                    rec.work_diff = patch
                    if self._receipt is not None:
                        self._receipt.data["changed_files"] = files[:50]
                else:
                    rec.diff_checked = True
                    rec.diff_source = "aura-snapshot: nothing changed"
            except Exception as _swallowed:      # noqa: BLE001
                log_swallowed("loop.work_snapshot", _swallowed)
        return rec

    def _take_baseline(self):
        """Not for the notebook itself (Aura's own files change there) or a git
        project.
        """
        try:
            wd = Path(self.work_dir).resolve()
            if wd == Path(self.root).resolve() or                     any((d / ".git").exists() for d in (wd, *wd.parents)):
                return None
            from ..workdiff import Baseline
            return Baseline.take(wd)
        except Exception as _swallowed:          # noqa: BLE001
            log_swallowed("loop.work_baseline", _swallowed)
            return None

    def _receipt_snapshot(self, step: int, usage: dict | None = None) -> None:
        """Fold the current step's facts into the receipt (cheap, local)."""
        if self._receipt is None:
            return
        try:
            self._receipt_steps = max(getattr(self, "_receipt_steps", 0), step)
            if usage:
                p = usage.get("prompt_tokens") or 0
                c = usage.get("completion_tokens") or 0
                if p or c:
                    cur = self._receipt.data["tokens"]
                    # `aura usage` totals these per day / model (like ccusage)
                    self._receipt.data["model"] = getattr(self.llm.p, "model", "")
                    try:
                        from ..model_facts import _cached_tokens
                        cur["cached"] = cur.get("cached", 0) + int(_cached_tokens(usage) or 0)
                    except Exception as _swallowed:      # noqa: BLE001
                        log_swallowed("loop.cached_tokens", _swallowed)
                    cur["prompt"] += int(p)
                    cur["completion"] += int(c)
                    cur["source"] = "measured"
                    # Cost in money, from the server's counts and the price the
                    # user set (local = $0; unknown stays unknown)
                    try:
                        from .. import model_facts as _MF
                        _c = _MF.call_cost(_MF.model_key(self.llm.p.base_url,
                                                         self.llm.p.model), usage)
                        if _c is not None:
                            self._receipt.data["cost_usd"] = round(
                                (self._receipt.data.get("cost_usd") or 0.0) + _c, 6)
                    except Exception as _swallowed:      # noqa: BLE001
                        log_swallowed("loop.cost", _swallowed)
            # What the tool list cost per call, and what sending every tool
            # would have cost (measured in characters)
            tl = getattr(self, "tool_list", None)
            if tl is not None:
                sent, everything = tl.chars()
                self._receipt.data["tool_list"] = {
                    "sent": list(tl.sent), "chars_sent": sent,
                    "chars_all_tools": everything,
                    "over_budget": list(tl.over_budget)}
            _mon = getattr(self, "_research", None)
            if _mon is not None and (_mon.searches or _mon.mode == "deep"):
                self._receipt.data["research"] = _mon.summary(self._plan_items())
            _reg = getattr(self.m, "_procs", None)
            if _reg is not None and _reg.procs:
                self._receipt.data["processes"] = [
                    {"id": p.id, "cmd": p.cmd[:200], "running": p.running(),
                     "exit_code": p.exit_code, "keep": p.keep}
                    for p in _reg.procs.values()]
        except Exception as _swallowed:
            log_swallowed("loop.receipt_usage", _swallowed)  # Receipt must never kill the loop

    def _taint(self, source: str) -> None:
        """Outside text gave instructions somewhere in this task: commands ask
        first for the rest of it (promptware gate), and the user is told once."""
        if getattr(self.m, "tainted", False):
            return
        self.m.tainted = True
        self.on_event("notice", {"text": (
            f"outside text read by {source} tried to give instructions — Aura treats it "
            "as data, and every command asks you first for the rest of this task")})
        if getattr(self, "_receipt", None) is not None:
            self._receipt.note(f"injection attempt reached {source}")

    def _not_right(self, rec: StepRecord) -> list[str]:
        """The end-of-run report, from Aura's records — not left to the model's
        answer. So: what changed, what was checked and how it went, what was
        not checked, and what went wrong (failures not followed by progress,
        blocked calls, replies stopped for going in circles, results not
        proven, files the task asked for that are missing, outside text that
        gave instructions). Empty for a run that did nothing.
        """
        out: list[str] = []
        try:
            changed = list(getattr(self, "_changed_files", []) or [])
            if "proof" not in (getattr(self, "_features", None) or ()):
                from .. import claim_check as CC
                t_seq = getattr(self, "_tests_seq", -1)
                facts = {"changed": changed, "tests_ok": getattr(self, "_tests_ok", False),
                         "tests_ever": (t_seq or -1) >= 0,
                         "tests_ran_after_change": (None if not changed else
                                                    (t_seq or -1) > getattr(self, "_last_change_seq", -1)),
                         "ran_anything": bool(getattr(self, "_ran_anything", False)),
                         "task": getattr(self, "_task_text", ""),
                         "reached_outside": bool(getattr(self, "_reached_outside", False))}
                out += ["Aura check: " + c for c in CC.check_claims(rec.content or "", facts)]
                out += ["looks like faking it: " + f for f in CC.fake_signs(getattr(self, "_added", []))]
                reads = [str((r.get("args") or {}).get("path", "")) for r in getattr(self, "_reads", [])]
                for n in CC.untouched(getattr(self, "_task_text", ""), changed, reads):
                    out.append(f"you named {n} — nothing in this task changed or read it")
            if changed:
                out.append("changed: " + ", ".join(changed[:5])
                           + (f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""))
            if changed and getattr(self, "_src_changed", False):
                t_seq = getattr(self, "_tests_seq", -1)
                if t_seq is not None and t_seq > getattr(self, "_last_change_seq", -1) and t_seq >= 0:
                    out.append("checked: tests " + ("passed" if getattr(self, "_tests_ok", False)
                                                    else "FAILED") + " after the last change")
                else:
                    out.append("not checked: no tests ran after the last change")
            edited = list(getattr(self, "_tests_edited", []) or [])
            if edited and getattr(self, "_tests_ok", False):
                out.append("tests changed and then passed: " + ", ".join(edited[:3])
                           + " — check the tests weren't weakened to pass")
            elif edited:
                out.append("tests changed: " + ", ".join(edited[:3]))
            for word in getattr(self, "_dont_touch", []) or []:
                hit = [c for c in changed if word in c.replace("\\", "/").lower()
                       or (word in ("test", "tests") and _is_test_path(c))]
                if hit:
                    out.append(f"you said not to change '{word}' — changed: " + ", ".join(hit[:3]))
            if getattr(self, "_proven", 0):
                out.append(f"proven: {self._proven} result(s)")
            if getattr(self, "_unproven", 0):
                out.append(f"not proven: {self._unproven} result(s)")
            _ld = getattr(self, "_ladder", None)
            if _ld is not None:
                out += [f"failed: {f}" for f in _ld.open_failures()[:4]]
            _lw = getattr(self, "_loopwatch", None)
            if _lw is not None and _lw.blocked:
                out.append(f"blocked: {len(_lw.blocked)} repeated call(s)")
            if getattr(self, "_cycles", 0):
                out.append(f"stopped: {self._cycles} reply(ies) going in circles")
            if getattr(rec, "gate_missing", None):
                out.append("missing: " + ", ".join(rec.gate_missing[:5]))
            if getattr(self.m, "tainted", False):
                out.append("outside text tried to give instructions (commands asked first)")
        except Exception as _swallowed:      # noqa: BLE001
            log_swallowed("loop.not_right", _swallowed)
        return out

    def _receipt_finalize(self, rec: StepRecord, model_claim: str = "") -> None:
        """Write the receipt JSON + attach the 5-line summary to the record.:
        every finalize ALSO carries the diff guarantee — receipt checks name
        whether a diff exists and where it came from.
        """
        # pblocks: every exit is a lesson (clean, max steps, stopped, failed);
        # lessons land as candidates that need corroboration — the model's
        # own word never counts as a confirmed outcome
        try:
            from .. import pblocks as _pb
            _n = _pb.learn(getattr(self, "_session_ref", None), self.work_dir,
                           rec, getattr(self, "_task_text", "") or "")
            if _n:
                self.on_event("pblock_learn", {"candidates": _n})
            # run-end closure (the tested build): output audit + verdict
            # linking, on a run that ended with an answer
            if model_claim:
                _pb.close_run(model_claim, getattr(self, "_task_text", "") or "",
                              rec.error is None,
                              # the same id the ground's diary stamp used, so
                              # the join finds this run's compile
                              str(getattr(self, "_pb_chat", "") or ""), self.on_event)
        except Exception as _swallowed:          # noqa: BLE001
            log_swallowed("loop.pblock_learn", _swallowed)
        # First so the receipt check reflects it
        try:
            self._engine_diff_guarantee(rec)
        except Exception as _swallowed:
            log_swallowed("loop.diff_guarantee", _swallowed)
        self._ledger_close(rec, (self._receipt.data.get("tool_calls")
                                 if self._receipt is not None else None))
        try:                                     # How well the model calls tools (real use)
            tu = getattr(self, "_tool_use", None)
            from .. import model_facts as _MF
            if tu and tu[0] and _MF.ACTIVE:
                _MF.record_tool_calls(_MF.ACTIVE, tu[0], tu[1])
            self._tool_use = None
        except Exception as _swallowed:          # noqa: BLE001
            log_swallowed("loop.tool_use_save", _swallowed)
        _bad = self._not_right(rec)
        if _bad:
            self.on_event("notice", {"text": "run report — " + " · ".join(_bad)})
        if self._receipt is None:
            return
        if _bad:
            self._receipt.data["report"] = _bad
        try:
            self._receipt.add_check(
                "final_answer_present", bool(rec.content and not rec.error),
                "loop exited with a final answer" if rec.content
                else f"exit without final answer: {rec.error or '?'}")
            # The run proof, when the run changed something
            if getattr(rec, "proof", None):
                from .validator import proof_summary
                self._receipt.add_check(
                    "proof_before_answer", rec.proof["status"] == "verified",
                    proof_summary(rec.proof))
            # 2.6: a bug fix that changed code — was the bug shown first?
            if getattr(self, "_bugfix", False) and \
                    getattr(self, "_src_changed", False):
                seen = bool(getattr(self, "_repro_seen", False))
                self._receipt.add_check(
                    "reproduced_before_fix", seen,
                    "a failing run came before the first code change" if seen
                    else "code was changed without first showing the bug "
                         "fail (no failing test/command before the fix)")
            # Diff-contract receipt check — no silent no-diff exits
            self._receipt.add_check(
                "work_diff_accounted", rec.diff_emitted or rec.diff_checked,
                f"diff source: {rec.diff_source or 'none'}"
                + ("" if rec.diff_emitted or rec.diff_checked else " — no changes on disk or "
                   "model diff missing (A2)"))
            if rec.gate_missing:
                self._receipt.add_check(
                    "deliverables_written", False,
                    "missing: " + ", ".join(rec.gate_missing[:5]))
            if getattr(self.m, "_helper_runs", None):
                self._receipt.data["helpers"] = list(self.m._helper_runs)
            data = self._receipt.finalize(
                getattr(self, "_receipt_steps", 0) or rec.step,
                model_claim=model_claim)
            data["_eng"] = str(self.root)
            rec.receipt = data
            rec.receipt_summary = render_receipt_summary(data)
        except Exception as _swallowed:
            log_swallowed("loop.receipt", _swallowed)  # Receipt must never kill the loop

    def _delta_printer(self):
        # a chat UI that draws the stream itself sets stream_sink(kind, text)
        sink = getattr(self, "stream_sink", None)
        if sink is not None:
            def to_sink(kind, text):
                if kind == "usage":
                    self.on_event("llm_usage", text if isinstance(text, dict)
                                  else {})
                else:
                    sink(kind, text)
            return to_sink
        # UX: reasoning deltas now render as a dim live stream (was swallowed);
        # toggled off with AI_UI_REASONING=0. The spinner state rides the same
        # closure so the first delta of any kind clears it. REAL-TIME USAGE:
        # usage chunks record into the HUD — the ↑↓ counts must move while the
        # task runs, not only at llm_done.
        def usage_pr(kind: str, text: str) -> None:
            if kind == "usage":
                # real-time: usage lands as a loop event the UI layer
                # (HUD/narrator) records — no stdout writes
                self.on_event("llm_usage", text if isinstance(text, dict)
                              else {})

        state ={"started": False, "spin": None}
        try:
            from ..options import get as _oget
            show_reasoning = bool(_oget("look.show_thinking"))   # AI_UI_REASONING overrides
        except Exception:                                         # noqa: BLE001
            show_reasoning = os.environ.get("AI_UI_REASONING", "1") not in ("0", "false", "no")

        def _clear_spin():
            sp = getattr(self, "_active_spin", None)
            if sp:
                sp.cancel()
                self._active_spin = None

        def pr(kind: str, text: str) -> None:
            if kind == "usage":
                usage_pr(kind, text)
                return
            from .. import ui
            if kind == "restart":
                # the stream dropped mid-reply and is being retried: mark
                # the cut-off copy, then let the new reply print fresh
                _clear_spin()
                sys.stdout.write("\n" + ui.paint(f"[{text}]", 249) + "\n")
                sys.stdout.flush()
                state.clear()
                state.update({"started": False, "spin": None})
                return
            if kind == "content":
                _clear_spin()
                if not state["started"]:
                    state["started"] = True
                    if text.startswith("\n"):
                        text = text.lstrip("\n")
                ui.agent_delta(text, first=not state.get("agent_started", False))
                state["agent_started"] = True
            elif kind == "reasoning" and text.strip():
                if show_reasoning and state.get("agent_started", False):
                    # mid-answer reasoning: keep it visually subordinate
                    sys.stdout.write(ui.paint(text, 249))
                    sys.stdout.flush()
                elif show_reasoning:
                    _clear_spin()
                    state["started"] = True
                    sys.stdout.write("\r\033[2K" + ui.paint(
                        "⋯ " + text.strip().replace("\n", " ")[:96], 249))
                    sys.stdout.flush()
                    # single-line thinking ticker: overwrite on next delta
                # hidden when AI_UI_REASONING=0
            elif kind == "tool_call":
                _clear_spin()
                # Dedupe on the name+args signature — the same call never
                # prints twice; a growing args stream UPDATES the line in place
                # instead of stacking.
                nm = text.get("name", "?")
                ar = (text.get("arguments") or "").strip()
                sig = f"{nm}:{ar[:80]}"
                if state.get("_last_tool_sig") == sig:
                    return
                if state.get("_last_tool_nm") == nm and ar \
                        and state.get("_last_tool_partial") \
                        and sig.startswith(state["_last_tool_sig"]):
                    # same call, longer args → rewrite the line in place
                    sys.stdout.write("\r\x1b[2K"
                                     + f"→ {nm}({ar[:80]})")
                    sys.stdout.flush()
                    state["_last_tool_sig"] = sig
                    return
                state["_last_tool_sig"] = sig
                state["_last_tool_nm"] = nm
                state["_last_tool_partial"] = bool(ar)
                print(f"\n→ {nm}({ar[:80]})")
        return pr

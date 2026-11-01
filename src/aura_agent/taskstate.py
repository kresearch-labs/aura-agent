"""Minimal priority-context: active-task scope block + task-state ledger.

Design (from the resurrection-bug discussion): - ACTIVE TASK BLOCK: the CURRENT
user message, machine-marked as the authoritative instruction, injected as a
system message the model reads BEFORE acting. Kills "old message out-pulls
current command". - TASK LEDGER: <engagement>/tasks.json — every
discussed/registered task with a status
(discussed|approved|in-progress|done|cancelled|superseded). Injected like the
todo block so dead tasks are VISIBLE as dead.

Minimal on purpose: no anaphora resolution, no clarify hook, no LLM calls.
Scope text = verbatim current message. The loop keeps the ledger: each new
instruction becomes a task at the start of its run (a follow-up like "continue
it" re-opens the task it binds to instead), and the run's end closes it — done
on a final answer, open when interrupted or failed, dropped when the run used
no tools (a question, not a task). anaphora.py binds "do it" / "that task" to
these entries.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

LEDGER_NAME = "tasks.json"
_VALID = {"discussed", "approved", "in-progress", "done", "cancelled",
          "superseded"}


def _ledger_path(root: Path) -> Path:
    return Path(root) / LEDGER_NAME


def load_ledger(root: Path) -> list[dict]:
    """Read the task ledger; [] when absent/corrupt (never fatal)."""
    p = _ledger_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_ledger(root: Path, entries: list[dict]) -> None:
    _ledger_path(root).write_text(
        json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")


def upsert_task(root: Path, task_id: str, desc: str,
                status: str = "discussed") -> None:
    """Add or update one ledger entry (operator/agent API in one call)."""
    if status not in _VALID:
        raise ValueError(f"status must be one of {sorted(_VALID)}")
    entries = load_ledger(root)
    now = time.strftime("%Y-%m-%d %H:%M")
    for e in entries:
        if e.get("id") == task_id:
            e["desc"] = desc or e.get("desc", "")
            e["status"] = status
            e["updated"] = now
            save_ledger(root, entries)
            return
    entries.append({"id": task_id, "desc": desc, "status": status,
                    "updated": now})
    save_ledger(root, entries)


LEDGER_KEEP = 50          # Most recent entries kept in tasks.json


def new_task(root: Path, desc: str) -> str:
    """Record a new instruction as an in-progress task; returns its id. The loop
    calls this at the start of a run.
    """
    entries = load_ledger(root)
    nums = [int(e["id"][1:]) for e in entries
            if str(e.get("id", ""))[:1] == "T" and str(e["id"])[1:].isdigit()]
    tid = f"T{max(nums, default=0) + 1}"
    first = (desc or "").strip().splitlines()[0] if (desc or "").strip() else ""
    entries.append({"id": tid, "desc": first[:120], "status": "in-progress",
                    "updated": time.strftime("%Y-%m-%d %H:%M")})
    save_ledger(root, entries[-LEDGER_KEEP:])
    return tid


def drop_task(root: Path, task_id: str) -> None:
    """Remove an entry (a run that used no tools was a question, not a
    task — the ledger holds work only)."""
    entries = load_ledger(root)
    kept = [e for e in entries if e.get("id") != task_id]
    if len(kept) != len(entries):
        save_ledger(root, kept)


SCOPE_TAG = "[ACTIVE TASK"            # Chats saved before the legend
_SCOPE_END = "[/ACTIVE TASK]"
_LABELS = ("[TASK] ", "[ASK] ", "[TALK] ", "[REPLY to: ")
_END = "[/TASK]"
_CONT = "[CONTINUES] "


def is_scope(m: dict) -> bool:
    """The turn's task block (a system message Aura wrote), new or old."""
    c = str(m.get("content", ""))
    return m.get("role") == "system" and c.startswith((SCOPE_TAG,) + _LABELS)
_CONTINUES = "THE TASK IT CONTINUES (the user's words):\n"
_CURRENT = "CURRENT INSTRUCTION:\n"

# a short answer to the agent's last message, not a new task: "yes", "go",
# "ok", "2", "the second one", "use postgres" (after a question)
_REPLY = re.compile(
    r"^\s*(y|yes|yeah|yep|yup|ok|okay|k|sure|go|go ahead|go on|do it|done|"
    r"proceed|continue|carry on|start|start it|sounds good|looks good|lgtm|"
    # "try again" is the same task, not a new one called "try again"
    r"try again|retry|again|keep going|go again|one more time|resume|finish it|"
    # "what now" carries on with the work in hand
    r"next|what next|what'?s next|what now|now what|then what|and then|"
    r"what to do|what do i do|what should i do|what should we do|"
    r"fine|good|great|perfect|agreed|correct|right|exactly|approved|"
    r"no|nope|not now|stop|wait|"
    # Small talk keeps the task
    r"thanks|thank you|thx|ty|nice|cool|awesome|great job|well done|got it|noted|"
    r"\d{1,2}|option \w+|the (first|second|third|fourth|last|other) one|"
    r"both|all of them|none)\b", re.I)
_WORK = re.compile(r"^\s*(please\s+)?(fix|add|create|write|build|make|delete|remove|refactor|"
                   r"implement|update|change|rename|move|run|open|install|deploy|test|"
                   r"debug|review|explain|search|find)\b", re.I)
_NEW_TARGET = re.compile(r"https?://|[\w-]+\.[a-z]{1,5}\b|[\\/]\w")


def is_reply(message: str, last_assistant: str = "") -> bool:
    """A short answer to the agent's last message. Up to 8 words; a yes/no/choice
    word, or anything after a question that names no new file or address.
    """
    text = (message or "").strip()
    if not text or len(text.split()) > 8 or not last_assistant.strip():
        return False
    if _NEW_TARGET.search(text):          # Names a file or address: new work
        return False
    if _REPLY.match(text):
        return True
    # after a question, a short answer — unless it starts new work
    return "?" in last_assistant[-600:] and not _WORK.match(text)


def _instruction_of(block: str) -> str:
    """The task a scope block names (what a reply continues)."""
    if block.startswith(_LABELS):
        body = block.split(_END, 1)[0]
        if _CONT in body:
            return body.split(_CONT, 1)[1].strip()
        return re.sub(r"^\[(TASK|ASK|TALK)\] ", "", body).strip()
    body = block.split(_SCOPE_END, 1)[0]
    for mark in (_CONTINUES, _CURRENT):
        if mark in body:
            return body.split(mark, 1)[1].strip()
    return ""


def last_question(text: str) -> str:
    """What a short reply answers: the last sentence of the agent's message that
    asks something.
    """
    t = " ".join(str(text or "").split())
    qs = re.findall(r"[^.!?]*\?", t)
    return (qs[-1].strip() if qs else t[-160:].strip())[:200]


def render_scope_block(current_task: str, continues: str = "", label: str = "TASK",
                       question: str = "") -> str:
    """The current message as THE instruction — a label, explained once in the
    base prompt (legend.py). `continues`: the task a short reply ("yes", "go",
    "2") answers, which stays the task; `question`: what the reply answers.
    label: TASK (do it) · ASK (a question: answer it, change nothing) · TALK
    (discuss mode).
    """
    if continues:
        return (f"[REPLY to: {question or 'your last message'}] {current_task}\n"
                f"{_CONT}{continues}\n{_END}")
    return f"[{label}] {current_task}\n{_END}"


def current_instruction(messages: list) -> str:
    """The task the newest task block names (the user's words)."""
    for m in reversed(messages):
        if is_scope(m):
            return _instruction_of(str(m["content"]))
    return ""


HANDOVER_CAP = 1200       # Characters (~300 tokens) of the user's words per helper


def users_handover(messages: list) -> str:
    """What a helper gets from the user, word for word: the request the main agent
    is working on and the user's standing rules.
    """
    from .engine.governor import standing_rules
    goal = current_instruction(messages)
    rules = standing_rules(messages)
    if not goal and not rules:
        return ""
    out = ["[FROM THE USER — their own words; the task above is the main "
           "agent's reading of them. If the two disagree, the user's words win.]"]
    if goal:
        out.append("The user's request: " + goal[:HANDOVER_CAP // 2])
    room = HANDOVER_CAP - sum(len(x) for x in out)
    kept = []
    for r in reversed(rules):                # The newest rules win if it must cut
        if len(r) + 3 > room:
            break
        kept.insert(0, "- " + r)
        room -= len(r) + 3
    if kept:
        out += ["The user's standing rules:"] + kept
    return "\n".join(out)


def take_scope(messages: list) -> tuple[list, str]:
    """Remove every earlier ACTIVE TASK block (only the newest may claim
    priority 1 — four piled-up blocks each said "current instruction") and
    return the task the newest one named."""
    last = ""
    kept = []
    for m in messages:
        if is_scope(m):
            last = _instruction_of(str(m["content"]))
            continue
        kept.append(m)
    return kept, last


def render_ledger_block(root: Path) -> str:
    """Ledger as a compact block; only NON-dead tasks are actionable."""
    entries = load_ledger(root)
    if not entries:
        return ""
    lines = ["[TASK LEDGER — state, not suggestions]"]
    for e in entries[-12:]:
        st = e.get("status", "discussed")
        dead = st in {"done", "cancelled", "superseded"}
        mark = "x" if dead else " "
        suffix = " [SUPERSEDED — do not resume]" if st == "superseded" else ""
        lines.append(f"- [{mark}] {e.get('id','?')}: {e.get('desc','')} "
                     f"({st}){suffix}")
    lines.append(
        "Tasks marked x are CLOSED — do not resume them. Only the [TASK] "
        "above plus open (non-x) ledger entries approved for work are "
        "in scope.")
    return "\n".join(lines)


def scope_message(current_task: str, root: Path, continues: str = "",
                  ledger_too: bool = True, label: str = "TASK", question: str = "") -> str:
    """One system message = block + (optional) ledger. ledger_too False: earlier
    tasks are left out of this task (task_view.py), so their names stay out
    too. label / question: render_scope_block.
    """
    block = render_scope_block(current_task, continues, label=label, question=question)
    ledger = render_ledger_block(root) if ledger_too else ""
    return block + ("\n\n" + ledger if ledger else "")

def supersede_tasks(root, task_ids):
    """Compaction safety — mark ledger rows superseded. task_ids=None compacts
    every non-open row (done/cancelled already dead stay as-is;
    discussed/approved/in-progress flip to superseded ONLY when explicitly
    listed; None means 'compact everything that is already closed').
    """
    entries = load_ledger(root)
    if task_ids is None:
        for e in entries:
            if e.get("status") in ("done", "cancelled"):
                e["status"] = "superseded"
                e["updated"] = time.strftime("%Y-%m-%d %H:%M")
    else:
        ids = set(task_ids)
        for e in entries:
            if e.get("id") in ids:
                e["status"] = "superseded"
                e["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save_ledger(root, entries)



"""memory_store.py — persistent memory (MEMORY.md + USER.md).

Two curated files per engagement, injected into every turn at the journal slot
(loop already appends injected blocks after history — volatile-last,
cache-aligned):

  memories/MEMORY.md — durable facts about this project (its notebook)
  <aura home>/USER.md — durable operator preferences (style, conventions);
                       ONE file shared by every profile and project — it
                       is about the person, not the work. An old per-
                       project memories/USER.md is merged in once and
                       renamed USER.md.merged (nothing is lost).

memory_write (mediator WRITE-scoped): append a fact under a section. Both files
are injected in full every turn, so there is no read tool.

Proof-tagged memory tiers (zero prompt growth): Evidence from the corpus:
agents write CONFIDENT WRONG self-diagnoses into memory and then get worse
(memory-confabulation studies: a task tried 1x without memory, 7-8x WITH
poisoned memory). Tier tags are metadata on the stored line, never injected
into prompts: [PROVEN] artifact-backed (carries an artifact/evidence ref) —
permanent [OBSERVED] plain observation — decays (flagged stale) after
MEMORY_TTL_DAYS [LESSONS] operator correction — permanent, must cite evidence
to enter Read side: OBSERVED entries past TTL are flagged "[stale — verify]" in
the injected block so the model re-verifies instead of trusting; the line is
NEVER silently deleted (auditability beats tidiness).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MEMORY_DIR = "memories"
MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"

MEMORY_HEADER = "# Notebook Memory (durable facts)\n<!-- sections: ## Facts | ## Decisions | ## Preferences -->\n"
USER_HEADER = "# Operator Memory (durable preferences)\n<!-- style, conventions, corrections worth keeping -->\n"

# Tiers — write-side tags, read-side TTL
MEMORY_TTL_DAYS = 30.0
_TIER_RE = re.compile(r"^\[(PROVEN|OBSERVED|LESSONS)\]\s*")
_OBSERVED_TS_RE = re.compile(r"<!--obs:([0-9.]+)-->")


def _mem_dir(root: Path) -> Path:
    return Path(root) / MEMORY_DIR


def user_file() -> Path:
    from .paths import aura_home
    return aura_home() / USER_FILE


def _merge_old_user_file(old: Path, usr: Path) -> None:
    """Move entries from a pre-sharing per-project USER.md into the shared
    one (lines not already there), then rename the old file."""
    try:
        have = set(usr.read_text(encoding="utf-8").splitlines())
        lines = [ln for ln in old.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("- ") and ln not in have]
        if lines:
            with usr.open("a", encoding="utf-8") as f:
                f.write("\n## Preferences\n" + "\n".join(lines) + "\n")
        old.replace(old.with_name(USER_FILE + ".merged"))
    except OSError:
        pass


def ensure_files(root: Path) -> tuple[Path, Path]:
    d = _mem_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    mem = d / MEMORY_FILE
    usr = user_file()
    if not mem.exists():
        mem.write_text(MEMORY_HEADER, encoding="utf-8")
    if not usr.exists():
        usr.parent.mkdir(parents=True, exist_ok=True)
        usr.write_text(USER_HEADER, encoding="utf-8")
    old = d / USER_FILE
    if old.exists() and old.resolve() != usr.resolve():
        _merge_old_user_file(old, usr)
    return mem, usr


def read_memory(root: Path) -> str:
    """Both files concatenated — this is what gets injected each turn.

    Tier read side: OBSERVED entries past MEMORY_TTL_DAYS get a "[stale —
    verify]" flag in the injected text (never deleted here).
    """
    mem, usr = ensure_files(root)
    now = time.time()
    parts = []
    for p in (mem, usr):
        text = p.read_text(encoding="utf-8").strip()
        # drop pure header files from injection (nothing learned yet)
        body_lines = []
        for ln in text.splitlines():
            if ln.startswith(("#", "<!--")):
                continue
            ln = _META_RE.sub("", ln)          # Tracking notes stay in the file
            m = _OBSERVED_TS_RE.search(ln)
            if m:
                try:
                    if now - float(m.group(1)) > MEMORY_TTL_DAYS * 86400:
                        ln = ln.replace(m.group(0), "").rstrip() \
                            + " [stale — verify]"
                except ValueError:
                    pass
            body_lines.append(ln)
        body = "\n".join(body_lines)
        if body.strip():
            parts.append(f"[{p.name}]\n{body.strip()}")
    return "\n\n".join(parts)


def write_memory(root: Path, target: str, text: str, section: str = "Facts",
                 replaces: str = "", source: str = "", workspace: Path | None = None,
                 quote: str = "") -> str:
    """Append a fact under a section in MEMORY.md or USER.md.

    Tier tags (write side): a leading [PROVEN]/[OBSERVED]/[LESSONS] in the text
    is recognized and preserved as the tier; OBSERVED lines get an invisible
    <!--obs:ts--> timestamp for TTL decay at read time. Untagged text defaults
    to OBSERVED (nothing unverified is ever permanent).
    """
    mem, usr = ensure_files(root)
    p = usr if str(target).lower() in ("user", "user.md") else mem
    heading = f"## {section.title()}"
    cur = p.read_text(encoding="utf-8")
    text = text.strip().rstrip(".")
    tier_m = _TIER_RE.match(text)
    tier = tier_m.group(1) if tier_m else "OBSERVED"
    if tier_m:
        text = _TIER_RE.sub("", text, count=1).strip()
    entry = f"- {text} [{tier}]"
    if tier == "OBSERVED":
        entry += f" <!--obs:{time.time():.3f}-->"
    # Where it came from and the files it is about (checked later)
    meta = {"at": round(time.time()), **({"from": source} if source else {})}
    meta["said"] = "user" if quote else "agent"
    if quote:
        meta["quote"] = " ".join(quote.split())[:QUOTE_MAX]
    files = _file_refs(text, workspace)
    if files:
        meta["files"] = files
    entry += f" <!--m:{json.dumps(meta, separators=(',', ':'))}-->"
    if replaces:
        err = _mark_replaced(root, replaces, text)
        if err:
            return f"not saved: replaces — {err}"
        cur = p.read_text(encoding="utf-8")
    if heading_exists(cur, heading):
        cur = insert_under_section(cur, heading, entry)
    else:
        cur = cur.rstrip() + f"\n\n{heading}\n{entry}\n"
    p.write_text(cur, encoding="utf-8")
    return (f"remembered [{p.name}/{section.title()}/{tier}]: {text[:80]}"
            + (" (the old entry is kept as history, not sent)" if replaces else ""))


def heading_exists(cur: str, heading: str) -> bool:
    """True when the heading exists OUTSIDE the HTML comment header block."""
    for ln in cur.splitlines():
        if ln.strip().startswith("<!--") or ln.strip().startswith("# "):
            continue
        if ln.strip() == heading:
            return True
    return False


def insert_under_section(cur: str, heading: str, entry: str) -> str:
    """Insert the entry under the LAST real occurrence of the heading,
    before the next '## ' section."""
    lines = cur.splitlines()
    at = -1
    for i, ln in enumerate(lines):
        if ln.strip().startswith("<!--"):
            continue
        if ln.strip() == heading:
            at = i
    if at < 0:
        return cur.rstrip() + f"\n\n{heading}\n{entry}\n"
    end = at + 1
    while end < len(lines) and not lines[end].strip().startswith("## "):
        end += 1
    lines.insert(end, entry)
    return "\n".join(lines) + "\n"


# ------------------------------------------- guarded agent writes --
# Memory is injected into every future turn, so an order saved here would steer
# every later run.
MAX_ENTRY_CHARS = 300          # One fact, one line
MAX_FILE_CHARS = 20000         # Per file; only relevant entries are sent once big

# standing orders to the agent — facts and preferences are phrased as
# statements ("the user prefers tabs"), orders as commands ("always use tabs")
_ORDER_START = re.compile(
    r"^\s*(always|never|do not|don't|dont|must|you must|you should|you are|"
    r"you will|from now on|ignore|disregard|forget|skip|stop|make sure|"
    r"remember to|be sure to|only ever)\b", re.I)
_GATE_WORDS = re.compile(
    r"\b(without asking|no need to ask|don'?t ask|do not ask|auto-?approve|"
    r"approval|permission|pre-?approved|constraints?|the rules?|"
    r"system prompt|instructions?)\b", re.I)


def instruction_reason(text: str) -> str | None:
    """Why `text` reads like an instruction to the agent, or None."""
    from .secrets_guard import injection_flag
    t = str(text or "")
    marker = injection_flag(t)
    if marker:
        return f"reads like a prompt injection ('{marker}')"
    body = re.sub(r"^\s*\[(PROVEN|OBSERVED|LESSONS)\]\s*", "", t)
    m = _ORDER_START.match(body)
    if m:
        return f"phrased as an order to the agent ('{m.group(1)}…')"
    m = _GATE_WORDS.search(body)
    if m:
        return f"mentions approvals, rules or instructions ('{m.group(1)}')"
    return None


def _target_path(root: Path, target: str) -> Path:
    mem, usr = ensure_files(root)
    return usr if str(target).lower() in ("user", "user.md") else mem


def memory_problem(root: Path, args: dict) -> str | None:
    """Why this memory_write is refused outright (checked before anything
    is asked, so the operator is never asked about a write that would fail)."""
    text = str((args or {}).get("text", "")).strip()
    if not text:
        return "empty memory text"
    if "\n" in text or "\r" in text:
        return "one fact per call, on one line"
    if len(text) > MAX_ENTRY_CHARS:
        return f"a fact is at most {MAX_ENTRY_CHARS} characters"
    section = str((args or {}).get("section", "Facts"))
    if not re.fullmatch(r"[A-Za-z][A-Za-z ]{0,29}", section):
        return "section is a short plain word (Facts, Decisions, Preferences)"
    p = _target_path(root, str((args or {}).get("target", "memory")))
    try:
        size = len(p.read_text(encoding="utf-8"))
    except OSError:
        size = 0
    if size + len(text) > MAX_FILE_CHARS:
        return (f"{p.name} is full ({MAX_FILE_CHARS} chars) — nothing is "
                "deleted automatically; tell the operator so they can tidy it")
    return None


def register_memory(mediator, fs_dispatch: dict, root) -> None:
    """Wire memory_write (WRITE); memory is injected every turn."""
    from .mediator import BlastRadius, ToolSpec
    root = Path(root)
    ensure_files(root)

    def memory_write(args) -> str:
        problem = memory_problem(root, args)
        if problem:
            return f"refused: {problem}"
        text = str(args.get("text", "")).strip()
        target = str(args.get("target", "memory"))
        section = str(args.get("section", "Facts"))
        ref = getattr(mediator, "_history_session", None)
        src = getattr(getattr(ref, "path", None), "stem", "") if ref is not None else ""
        quote = str(args.get("quote", "") or "").strip()
        unchecked = ""
        if quote and not check_quote(quote, getattr(ref, "messages", None) or []):
            unchecked = (" — the quote is not in the user's messages, so it is saved "
                         "as your reading, not as the user's words")
            quote = ""
        return write_memory(root, target, text, section,
                            replaces=str(args.get("replaces", "") or ""), source=src,
                            workspace=getattr(mediator, "work_dir", None),
                            quote=quote) + unchecked

    def ask_when(args) -> str | None:
        if memory_problem(root, args):
            return None                    # Refused anyway — don't ask
        return instruction_reason(str(args.get("text", "")))

    mediator.register(ToolSpec("memory_write", BlastRadius.WRITE,
                               lambda a: None, ask_when=ask_when))
    fs_dispatch["memory_write"] = memory_write

    def memory_forget(args) -> str:
        return forget(root, str((args or {}).get("match", "")))
    mediator.register(ToolSpec("memory_forget", BlastRadius.WRITE, lambda a: None))
    fs_dispatch["memory_forget"] = memory_forget



# ------------------------------------------- (step 6): pick, track, forget
# None picks what's relevant to the task, keeps where an entry came from, or
# notices a memory about a file went stale when the file changed.
INJECT_BUDGET = 2400            # Chars of memory sent per task when it's big
_META_RE = re.compile(r"\s*<!--m:(\{.*?\})-->")
_PIN_RE = re.compile(r"\b(always|never|don'?t|do not|must|prefer|from now on)\b", re.I)
REPLACED = "## Replaced"


def _file_refs(text: str, workspace: Path | None) -> dict:
    """Files the fact mentions that exist in the project → short content
    hashes, so a later change to the file marks the memory for checking."""
    if workspace is None:
        return {}
    import hashlib
    out = {}
    for m in re.findall(r"(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}", text):
        p = Path(workspace) / m
        try:
            if p.is_file() and p.stat().st_size < 5_000_000:
                out[m] = hashlib.sha1(p.read_bytes()).hexdigest()[:10]
        except OSError:
            continue
    return out


def entries(root: Path) -> list[dict]:
    """Every remembered line, with its file, section and metadata."""
    out = []
    for p in ensure_files(root):
        section = ""
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines()):
            if ln.startswith("## "):
                section = ln[3:].strip()
                continue
            if not ln.startswith("- "):
                continue
            meta = {}
            m = _META_RE.search(ln)
            if m:
                try:
                    meta = json.loads(m.group(1))
                except ValueError:
                    meta = {}
            text = _META_RE.sub("", _OBSERVED_TS_RE.sub("", ln)).strip()[2:].strip()
            out.append({"file": p, "line": i, "raw": ln, "text": text,
                        "section": section, "meta": meta,
                        "replaced": section == REPLACED[3:]})
    return out


def _stale_note(e: dict, workspace: Path | None) -> str:
    refs = e["meta"].get("files") or {}
    if not refs or workspace is None:
        return ""
    now = _file_refs(" ".join(refs), workspace)
    changed = [f for f, h in refs.items() if now.get(f) != h]
    return f" [{', '.join(changed)} changed since — verify]" if changed else ""


QUOTE_MAX = 120


def _said_note(e: dict) -> str:
    """Marked only when the user said it (their checked words); an
    unmarked entry is the agent's conclusion — one header line says so,
    instead of a label on every entry."""
    if e["meta"].get("said") == "user" and e["meta"].get("quote"):
        return f' (user said: "{e["meta"]["quote"]}")'
    return ""


MEMORY_HEAD = ('(entries marked "user said" are the user\'s own words; the rest '
               "are conclusions drawn earlier — not things the user said)")


def check_quote(quote: str, messages: list) -> bool:
    """The quote is really in one of the user's own messages (small
    differences in spaces and case are allowed)."""
    from .notes import is_users_words, users_words
    q = " ".join(str(quote).split()).lower()
    if len(q) < 3:
        return False
    return any(q in " ".join(users_words(m).split()).lower()
               for m in messages if is_users_words(m))


def pick_memory(root: Path, task: str = "", workspace: Path | None = None) -> str:
    """What memory to send for this task. Small memory → all of it (as before).
    Big → the pinned entries (your preferences, proven facts, standing rules) +
    the ones most relevant to the task, within INJECT_BUDGET; the rest is
    counted, not lost. Replaced entries are history, never sent; entries about
    a file that changed since are flagged.
    """
    live = [e for e in entries(root) if not e["replaced"]]
    if not live:
        return ""
    total = sum(len(e["text"]) for e in live)
    chosen = live
    hidden = 0
    if total > INJECT_BUDGET:
        pinned = [e for e in live if e["file"].name == USER_FILE
                  or "[PROVEN]" in e["text"] or _PIN_RE.search(e["text"])]
        rest = [e for e in live if e not in pinned]
        from .toolsets import bm25
        ranked = bm25(task or "", {str(i): e["text"] for i, e in enumerate(rest)})
        top = [int(k) for k, _ in ranked]
        order = [rest[i] for i in top] + [e for i, e in enumerate(rest) if i not in set(top)]
        chosen, used = [], 0
        for e in pinned + order:
            if used + len(e["text"]) > INJECT_BUDGET and chosen:
                continue
            chosen.append(e)
            used += len(e["text"])
        hidden = len(live) - len(chosen)
    now = time.time()
    by_file: dict = {}
    for e in chosen:
        line = "- " + e["text"]
        ts = _OBSERVED_TS_RE.search(e["raw"])
        if ts:
            try:
                if now - float(ts.group(1)) > MEMORY_TTL_DAYS * 86400:
                    line += " [stale — verify]"
            except ValueError:
                pass
        line += _stale_note(e, workspace) + _said_note(e)
        by_file.setdefault(e["file"].name, []).append(line)
    parts = [MEMORY_HEAD] + [f"[{name}]\n" + "\n".join(lines)
                             for name, lines in by_file.items()]
    if hidden:
        parts.append(f"({hidden} more remembered facts not shown — less related to this "
                     "task; the user can list them with /memory)")
    return "\n\n".join(parts)


def _find_one(root: Path, match: str, include_replaced: bool = False):
    m = " ".join(str(match).split()).lower()
    if len(m) < 3:
        return None, "give a few words of the entry"
    hits = [e for e in entries(root) if m in e["text"].lower()
            and (include_replaced or not e["replaced"])]
    if not hits:
        return None, f"no remembered fact contains {match!r}"
    if len(hits) > 1:
        return None, ("several entries match — be more specific: "
                      + " | ".join(h["text"][:60] for h in hits[:5]))
    return hits[0], ""


def forget(root: Path, match: str) -> str:
    """Delete one remembered fact (the user asked to forget it: it is
    removed, not kept somewhere else)."""
    e, err = _find_one(root, match, include_replaced=True)
    if e is None:
        return f"not forgotten: {err}"
    lines = e["file"].read_text(encoding="utf-8").splitlines()
    del lines[e["line"]]
    e["file"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"forgotten: {e['text'][:80]}"


def _mark_replaced(root: Path, match: str, by: str) -> str | None:
    """The old entry moves under '## Replaced' (history, never sent)."""
    e, err = _find_one(root, match)
    if e is None:
        return err
    p = e["file"]
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[e["line"]]
    cur = "\n".join(lines).rstrip() + "\n"
    note = f"- {e['text']} (replaced {time.strftime('%Y-%m-%d')} by: {by[:60]})"
    if heading_exists(cur, REPLACED):
        cur = insert_under_section(cur, REPLACED, note)
    else:
        cur = cur.rstrip() + f"\n\n{REPLACED}\n{note}\n"
    p.write_text(cur, encoding="utf-8")
    return None


def memory_injection_block(text: str) -> str:
    if not text.strip():
        return ""
    return "[OPERATOR MEMORY — durable facts; applies unless overridden]\n" + text

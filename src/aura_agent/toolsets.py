"""toolsets.py — which tools the model is sent.

Before: every registered tool's description went with every call (~1,900 tokens
for the 22 built-in tools, more with MCP servers). Now tools are in groups
("toolsets"). A small core is always sent; the other groups are picked before
the run, without a model call, from:

  1. what you switched on or off (`/tools`, saved settings, AURA_TOOLSETS);
  2. what the prompt already points at (a skills index → skills; the
     project's symbol sheet → code; rules exist → rules);
     descriptions);
  4. the model's room: with a large context window the generally useful
     groups come along, with a small one only what the task names.

Everything registered stays reachable: `tools_find` searches the rest and loads
what it finds, and a call to a registered tool that wasn't sent loads it too
(the mediator still checks every call as always).

The list the model sees only grows within a conversation and new tools go at
the end, so the part of the prompt a server caches stays the same.

Generic: no model- or server-specific code.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field

from .tool_names import MCP_PREFIX

# always sent (the prompt names todo_write and finding_add)
CORE: tuple[str, ...] = ("read_file", "list_files", "search_files", "write_file",
                         "edit_file", "run_command", "todo_write", "clarify",
                         "finding_add", "tools_find")


@dataclass(frozen=True)
class Toolset:
    name: str
    about: str                       # Plain words, shown in tools
    tools: tuple[str, ...]
    words: tuple[str, ...] = ()      # Task words that switch it on
    big_model: bool = False          # Sent by default when the window is large
    default: bool = False            # Sent by default always (unless switched off)


TOOLSETS: tuple[Toolset, ...] = (
    Toolset("checks", "run the project's tests, lint and type checks",
            ("run_checks",),
            ("test", "tests", "pytest", "unittest", "lint", "typecheck",
             "type check", "build", "failing", "fails", "bug", "fix", "ci"),
            big_model=True),
    Toolset("settings", "Aura's own options: look, change when you ask (you confirm)",
            ("settings", "settings_set"),
            ("aura setting", "aura settings", "aura's settings", "aura option",
             "aura options", "your settings", "your options", "line tags",
             "stop asking", "turn on", "turn off", "switch on", "switch off", "switch to",
             "enable", "disable", "use deep research", "deep research", "thinking level",
             "think harder", "think less", "dark theme", "light theme", "sidebar",
             "discuss mode", "talk only", "proof", "allow the folder", "allow folder",
             "let aura read", "let it read", "read my downloads", "fullscreen", "inline",
             "trash days", "find a way", "check for updates", "update check",
             "daily update")),
    Toolset("team", "Aura Teams: connect to your team's hub, wire any app, its token (you confirm)",
            ("team_status", "team_connect", "team_token", "team_wiring_format",
             "team_wire_check", "team_wire_use", "team_chat"),
            ("team", "teams", "teammate", "teammates", "hub", "the board", "our board",
             "vikunja", "jira", "linear", "github issues", "gitlab", "trello", "asana",
             "connect to", "connect me", "my team", "our team", "team chat", "chat with")),
    Toolset("update", "Aura's own updates: check, update, go back (you confirm)",
            ("aura_update",),
            ("update aura", "update yourself", "update you", "new version", "newer version",
             "latest version", "upgrade aura", "upgrade yourself", "aura version", "your version",
             "roll back", "rollback", "go back to the last version", "previous version")),
    Toolset("find", "find a way to what's missing — a program, a sandbox (only with /findaway on; asks first)",
            ("get",), ()),
    Toolset("background", "commands that keep running: servers, watchers, long builds",
            ("process_start", "process", "process_input"),
            ("server", "dev server", "serve", "background", "watch", "watcher",
             "long-running", "keep running", "localhost", "port", "start the app",
             "run the app", "npm run dev", "npm start", "uvicorn", "flask run",
             "runserver", "docker compose up", "in the background", "daemon")),
    Toolset("schedule", "run a task later or on a schedule (asks you first)",
            ("cronjob",),
            ("schedule", "every day", "every hour", "every morning", "each morning", "daily",
             "weekly", "cron", "remind", "later today", "in an hour", "in 30 minutes",
             "recurring", "periodically", "at 9", "nightly")),
    Toolset("web", "search the web and read pages (and the browser when installed)",
            ("web_search", "web_crawl", "browser_run"),
            ("web", "website", "url", "http", "https", "www", "online",
             "internet", "docs", "documentation", "page", "search the web",
             "browse", "look up", "research", "google", "web search", "find out",
             "latest", "news", "paper", "papers", "cite", "citation", "citations")),
    Toolset("code", "the project's symbol sheet: functions, classes, imports",
            ("symbols", "code_nav"),
            ("function", "functions", "class", "classes", "method", "symbol",
             "defined", "definition", "api", "refactor", "rename", "import",
             "imports", "signature", "codebase", "references", "used", "callers",
             "who calls", "where is")),
    Toolset("images", "look at an image file or a screenshot",
            ("view_image",),
            ("image", "images", "picture", "photo", "screenshot", "png", "jpg", "jpeg",
             "diagram", "logo", "icon", "look at", "what does it look like")),
    Toolset("script", "a Python script that calls Aura's tools (work over many files)",
            ("run_code",),
            ("script", "for each file", "for every file", "batch", "bulk", "all the files",
             "in every file", "across the codebase", "count all", "aggregate")),
    Toolset("helpers", "helper agents for separate sub-tasks",
            ("delegate", "helper_apply"),
            ("helper", "helpers", "sub-task", "subtask", "in parallel",
             "parallel", "delegate", "each of", "every file", "all files",
             "investigate"),
            big_model=True),
    Toolset("memory", "remember facts and your preferences between chats",
            ("memory_write", "memory_forget", "history_search"),
            ("remember", "always", "never", "prefer", "preference",
             "from now on", "next time", "in future", "don't forget",
             "last time", "earlier chat", "we decided", "previous",
             "yesterday", "last week", "did we", "history", "forget"),
            default=True),
    Toolset("rules", "your standing rules: read, propose to add or remove",
            ("rules", "rules_add", "rules_remove"),
            ("rule", "rules", "constraint", "constraints", "policy")),
    Toolset("results", "recorded results: list them",
            ("finding_list",),
            ("finding", "findings", "result", "results", "report",
             "proven", "prove")),
    Toolset("notes", "the project's notes and state files",
            ("notes", "notes_write", "state", "state_write"),
            ("note", "notes", "state", "status file", "notebook", "write down",
             "jot")),
    Toolset("skills", "skills: load one, save a new one",
            ("skill_load", "skill_write"),
            ("skill", "skills", "procedure", "playbook", "recipe",
             "save this as")),
)

BY_NAME = {t.name: t for t in TOOLSETS}
LARGE_CONTEXT = 64_000       # Tokens; at or above this the big_model sets come along

TOOLS_FIND_SCHEMA = {"type": "function", "function": {
    "name": "tools_find",
    "description": ("Find more tools when none you have fits the job "
                    "(e.g. reading web pages, helper agents, memory, notes, "
                    "skills, MCP servers). Matching tools are added for the "
                    "rest of this chat."),
    "parameters": {"type": "object", "properties": {
        "need": {"type": "string",
                 "description": "what you need to do, in a few words"}},
        "required": ["need"]}}}


USE_TOOL_SCHEMA = {"type": "function", "function": {
    "name": "use_tool",
    "description": ("Call a tool that is ready but not in your list (the [MORE TOOLS] "
                    "note, or one tools_find found). Same rules and approvals as "
                    "calling it directly."),
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "the tool's name"},
        "arguments": {"type": "object", "description": "its arguments"}},
        "required": ["name"]}}}


def set_of(tool: str) -> str:
    """The toolset a tool belongs to ('core', a set name, 'mcp:<server>',
    or 'other' for a tool no set lists)."""
    if tool in CORE:
        return "core"
    for t in TOOLSETS:
        if tool in t.tools:
            return t.name
    if tool.startswith(MCP_PREFIX):
        return "mcp:" + tool[len(MCP_PREFIX):].split("__", 1)[0]
    return "other"


# ------------------------------------------------------------ matching --
_WORD = re.compile(r"[a-z0-9]+")


_STOP = frozenset("""a an the and or of to in on at for with from by is are be
it its this that these those as do does did can could should would will you
your i me my we our they them their he she his her not no yes all any some
each into out up so if then than when what which who how why where there
here get got make made use used using need needs want wants please just
also more most other only own same such too very one two new file files
""".split())


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])",
                     text) is not None


def _desc(schema: dict) -> str:
    f = schema.get("function", {})
    parts = [f.get("name", "").replace("_", " "), f.get("description", "")]
    for k, v in (f.get("parameters", {}).get("properties", {}) or {}).items():
        parts.append(k.replace("_", " "))
        if isinstance(v, dict):
            parts.append(str(v.get("description", "")))
    return " ".join(parts)


def bm25(query: str, docs: dict[str, str], k1: float = 1.2,
         b: float = 0.75) -> list[tuple[str, float]]:
    """Best first, zero scores left out."""
    q = set(_tokens(query))
    if not q or not docs:
        return []
    toks = {k: _tokens(v) for k, v in docs.items()}
    n = len(toks)
    avg = sum(len(t) for t in toks.values()) / n or 1.0
    df = {w: sum(1 for t in toks.values() if w in t) for w in q}
    out = []
    for k, t in toks.items():
        s = 0.0
        for w in q:
            f = t.count(w)
            if not f:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avg))
        if s > 0:
            out.append((k, s))
    out.sort(key=lambda x: -x[1])
    return out


# ------------------------------------------------------------- choosing --
@dataclass
class Choice:
    sets: dict[str, str] = field(default_factory=dict)   # Set → why

    def add(self, name: str, why: str) -> None:
        self.sets.setdefault(name, why)


def user_switches() -> tuple[set[str], set[str], bool]:
    """(always on, never on, send everything) from AURA_TOOLSETS and the
    saved settings. AURA_TOOLSETS: 'all', or 'web,-memory' (on / -off)."""
    on: set[str] = set()
    off: set[str] = set()
    everything = False
    try:
        from .config import load_saved_settings
        saved = load_saved_settings()
        on |= set(saved.get("toolsets_on") or [])
        off |= set(saved.get("toolsets_off") or [])
    except Exception:                              # noqa: BLE001
        pass
    env = os.environ.get("AURA_TOOLSETS", "").strip()
    for part in filter(None, (p.strip() for p in env.split(","))):
        if part == "all":
            everything = True
        elif part.startswith("-"):
            off.add(part[1:])
            on.discard(part[1:])
        else:
            on.add(part)
            off.discard(part)
    return on, off, everything


def choose(task: str, registered: dict[str, dict], ctx: int = 0,
           hints: set[str] | None = None,
           chat_on: set[str] | None = None,
           chat_off: set[str] | None = None) -> Choice:
    """Which toolsets to send for this task. `registered`: name → schema of
    every tool that exists; `hints`: sets the prompt already points at;
    chat_on/chat_off: switched in this chat with /tools."""
    on, off, everything = user_switches()
    on |= chat_on or set()
    off = (off | (chat_off or set())) - (chat_on or set())
    present = {set_of(n) for n in registered} - {"core"}
    ch = Choice()
    if everything:
        for s in sorted(present):
            ch.add(s, "AURA_TOOLSETS=all")
        return ch
    for s in sorted(on & present):
        ch.add(s, "switched on")
    for s in sorted((hints or set()) & present):
        ch.add(s, "the prompt points at it")
    low = " " + task.lower() + " "
    for t in TOOLSETS:
        if t.name in present and any(_has_phrase(low, w) for w in t.words):
            ch.add(t.name, "named in the task")
    if re.search(r"https?://|www\.|\b[a-z0-9-]+\.(com|org|io|dev|net)\b", low):
        ch.add("web", "a web address in the task")
    # MCP servers and anything the word lists miss.
    rest = {n: _desc(sc) for n, sc in registered.items()
            if set_of(n) not in ch.sets and set_of(n) not in BY_NAME
            and set_of(n) != "core"}
    for n, score in bm25(task, rest)[:3]:
        if score >= 2.0:
            ch.add(set_of(n), "the task matches its description")
    for t in TOOLSETS:
        if t.default and t.name in present:
            ch.add(t.name, "on by default")
    try:                                        # find_way.py: the user's toggle
        from .find_way import on as _fw_on
        if _fw_on() and "find" in present:
            ch.add("find", "'find a way' is on")
    except Exception:                           # noqa: BLE001
        pass
    if ctx >= LARGE_CONTEXT:
        for t in TOOLSETS:
            if t.big_model and t.name in present:
                ch.add(t.name, "large context window")
    for s in off:
        ch.sets.pop(s, None)
    return ch


class ToolList:
    """The schemas sent to the model: core + chosen sets, in a stable
    order, growing at the end only."""

    def __init__(self, all_schemas: list[dict]):
        self.all: dict[str, dict] = {}
        for sc in all_schemas:
            self.all[sc["function"]["name"]] = sc
        self.all.setdefault("tools_find", TOOLS_FIND_SCHEMA)
        self.all.setdefault("use_tool", USE_TOOL_SCHEMA)
        self.sent: list[str] = [n for n in CORE if n in self.all] + ["use_tool"]
        self.frozen = False
        self.waiting: list[str] = []
        self.why: dict[str, str] = {"core": "always"}
        self.chat_on: set[str] = set()
        self.chat_off: set[str] = set()
        # Parking: groups used since the last summary, when each came, and what
        # the budget left out
        self.calls = 0
        self.used: set[str] = set()
        self.came: dict[str, int] = {}
        self.over_budget: list[str] = []
        self.parked: set[str] = set()

    # what the loop sends
    def schemas(self) -> list[dict]:
        return [self.all[n] for n in self.sent]

    def is_sent(self, name: str) -> bool:
        """In the list, or waiting to join it (callable either way)."""
        return name in self.sent or name in self.waiting

    def _grow(self, names: list[str]) -> list[str]:
        """Add to the list — or, once frozen, to the waiting tools."""
        new = [n for n in names if n not in self.sent and n not in self.waiting]
        (self.waiting if self.frozen else self.sent).extend(new)
        return new

    def waiting_note(self) -> str:
        """The note at the end of a call that describes the waiting tools."""
        if not self.waiting:
            return ""
        lines = ["[MORE TOOLS] These tools are ready but not in your tool list (the "
                 "list stays the same so the conversation is reused). Call one with "
                 "use_tool(name, arguments):"]
        for n in self.waiting:
            fn = self.all.get(n, {}).get("function", {})
            params = (fn.get("parameters") or {}).get("properties") or {}
            req = (fn.get("parameters") or {}).get("required") or []
            args = ", ".join(f"{k}{'' if k in req else '?'}: {(v or {}).get('type', 'any')}"
                             for k, v in params.items())
            lines.append(f"- {n}({args}) — {str(fn.get('description', '')).split('. ')[0][:140]}")
        return "\n".join(lines)

    def is_blocked(self, name: str) -> bool:
        """The user switched this tool's group off (chat, saved or env)."""
        s = set_of(name)
        if s == "core":
            return False
        if s in self.chat_on:
            return False
        _on, off, _all = user_switches()
        return s in self.chat_off or (s in off and s not in _on)

    def add_set(self, set_name: str, why: str) -> list[str]:
        new = self._grow([n for n in self.all if set_of(n) == set_name])
        if new:
            self.why.setdefault(set_name, why)
            self.came[set_name] = self.calls
            self.parked.discard(set_name)
        return new

    def mark_used(self, name: str) -> None:
        s = set_of(name)
        self.used.add(name if s == "other" else s)

    def kept_by_user(self, set_name: str) -> bool:
        on, _off, everything = user_switches()
        return everything or set_name in on or set_name in self.chat_on

    def park(self, recent: int = 3) -> list[str]:
        """At a summary (the server's cache starts over then anyway): stop
        sending groups not used since the last summary. Never the core, a
        group the user switched on, or one that came in the last few calls.
        The model gets a parked group back the usual way (tools_find, or
        calling one of its tools)."""
        # the cache starts over here: the waiting tools join the list
        self.sent.extend(n for n in self.waiting if n not in self.sent)
        self.waiting = []
        self.frozen = False
        groups: dict[str, list[str]] = {}
        for n in self.sent:
            s = set_of(n)
            if s != "core" and n != "use_tool":
                groups.setdefault(n if s == "other" else s, []).append(n)
        gone: list[str] = []
        for g, names in groups.items():
            if (g in self.used or self.kept_by_user(g)
                    or self.calls - self.came.get(g, -10**6) < recent):
                continue
            self.sent = [n for n in self.sent if n not in names]
            self.why.pop(g, None)
            gone.append(g)
        self.parked |= set(gone)
        self.used = set()
        return gone

    def add_tool(self, name: str, why: str) -> bool:
        """Load the whole set a tool belongs to (a lone tool from a set
        is rarely useful). False when it isn't registered."""
        if name not in self.all:
            return False
        if name in self.sent:
            return True
        if self.is_blocked(name):
            return False
        s = set_of(name)
        if s in ("other",):
            self._grow([name])
            self.why.setdefault(name, why)
            self.came[name] = self.calls
            return True
        self.add_set(s, why)
        return True

    def pick(self, task: str, ctx: int = 0, hints: set[str] | None = None,
             skill: str | None = None) -> list[str]:
        """Add the sets chosen for this task (never removes any). skill 'weak'
        (model_facts.tool_skill — measured, or seen in real runs): only what
        the task names, the prompt points at or the user switched on; the rest
        stays findable (tools_find).
        """
        ch = choose(task, {n: sc for n, sc in self.all.items()
                           if n != "tools_find"},
                    ctx=ctx, hints=hints, chat_on=self.chat_on,
                    chat_off=self.chat_off)
        added: list[str] = []
        room = budget_chars(ctx)
        for s, why in ch.sets.items():
            # the budget trims only what comes without the task asking:
            # the defaults and the large-window extras
            if why in _BUDGETED and s in self.parked:
                continue        # Put away unused: back only when a task needs it
            if skill == "weak" and (why in _BUDGETED or why == "the task matches its description") \
                    and not self.kept_by_user(s):
                if s not in self.over_budget:
                    self.over_budget.append(s)
                continue
            if room and why in _BUDGETED and not self.kept_by_user(s):
                extra = len(json.dumps([sc for n, sc in self.all.items()
                                        if set_of(n) == s and n not in self.sent]))
                if extra and self.chars()[0] + extra > room:
                    # past the budget: not sent, still found when needed
                    if s not in self.over_budget:
                        self.over_budget.append(s)
                    continue
            added += self.add_set(s, why)
        return added

    def find(self, need: str, limit: int = 5) -> list[str]:
        """tools_find: the best matches among tools not sent, loaded."""
        rest = {n: _desc(sc) + " " + " ".join(BY_NAME[set_of(n)].words
                                               if set_of(n) in BY_NAME else ())
                for n, sc in self.all.items()
                if n not in self.sent and not self.is_blocked(n)}
        ranked = bm25(need, rest)
        best = ranked[0][1] if ranked else 0.0
        # strong matches only: within half of the best score
        hits = [n for n, sc in ranked[:limit] if sc >= best * 0.5]
        for n in hits:
            self.add_tool(n, f"found for: {need[:60]}")
        return hits

    # numbers for /context and the receipt
    def chars(self) -> tuple[int, int]:
        sent = len(json.dumps(self.schemas()))
        everything = len(json.dumps(list(self.all.values())))
        return sent, everything

    def summary(self) -> list[tuple[str, int, bool, str]]:
        """(set, tool count, sent?, why) for every set present."""
        groups: dict[str, list[str]] = {}
        for n in self.all:
            groups.setdefault(set_of(n), []).append(n)
        out = []
        for s, names in groups.items():
            sent = all(n in self.sent for n in names)
            out.append((s, len(names), sent, self.why.get(s, "")))
        order = ["core"] + [t.name for t in TOOLSETS]
        out.sort(key=lambda r: (order.index(r[0]) if r[0] in order else 99, r[0]))
        return out


_BUDGETED = {"on by default", "large context window"}


def budget_chars(ctx_tokens: int) -> int:
    """The most characters the tool list may take before a run: a share of the
    context window. 0 when the window isn't known.
    """
    if not ctx_tokens:
        return 0
    try:
        from .options import get
        pct = int(get("tools.budget_pct"))
    except Exception:                              # noqa: BLE001
        pct = 10
    return int(ctx_tokens * 4 * pct / 100) if pct else 0


def register_tools_find(mediator, fs_dispatch: dict, tool_list_getter) -> None:
    """tools_find through the mediator like every tool (READ, plan-safe)."""
    from .mediator import BlastRadius, ToolSpec

    def tools_find(args) -> str:
        need = str((args or {}).get("need", "")).strip()
        tl = tool_list_getter()
        if tl is None:
            return "tools_find: no tool list in this run"
        hits = tl.find(need)
        if not hits:
            have = ", ".join(sorted({set_of(n) for n in tl.all} - {"core"}))
            return (f"no tool matches '{need}'. Groups that exist: {have}. "
                    "Try other words, or do it with the tools you have.")
        lines = []
        for n in hits:
            d = tl.all[n]["function"].get("description", "").split(". ")[0]
            lines.append(f"- {n}: {d[:120]}")
        if any(n in tl.waiting for n in hits):
            return ("ready — call them with use_tool(name, arguments) (their "
                    "arguments are in the [MORE TOOLS] note):\n" + "\n".join(lines))
        return "added (usable now):\n" + "\n".join(lines)

    mediator.register(ToolSpec("tools_find", BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    fs_dispatch["tools_find"] = tools_find


def remove_set(tl: "ToolList", set_name: str) -> list[str]:
    """Stop sending a group (the user asked; the next call re-sends the
    tool list, so a server's cache starts over once)."""
    gone = [n for n in tl.sent if set_of(n) == set_name and n not in CORE]
    tl.sent = [n for n in tl.sent if n not in gone]
    tl.why.pop(set_name, None)
    return gone


def tools_command(tl: "ToolList", args: list[str]) -> list[str]:
    """/tools in chat. Returns the lines to show."""
    if tl is None:
        return ["no tool list in this chat"]
    if args[:1] == ["list"]:
        return [f"  {'sent' if n in tl.sent else '    '}  {set_of(n):12s} {n}"
                for n in tl.all]
    if args[:1] in (["on"], ["off"]) and len(args) >= 2:
        group = args[1]
        groups = {set_of(n) for n in tl.all} - {"core"}
        if group not in groups:
            return [f"no group '{group}' — groups: {', '.join(sorted(groups))}"]
        permanent = "permanent" in args[2:]
        if args[0] == "on":
            tl.chat_on.add(group)
            tl.chat_off.discard(group)
            tl.add_set(group, "switched on")
        else:
            tl.chat_off.add(group)
            tl.chat_on.discard(group)
            remove_set(tl, group)
        msg = f"{group}: {args[0]} for this chat"
        if permanent:
            try:
                from .config import load_saved_settings, save_saved_settings
                d = load_saved_settings()
                on = set(d.get("toolsets_on") or [])
                off = set(d.get("toolsets_off") or [])
                (on if args[0] == "on" else off).add(group)
                (off if args[0] == "on" else on).discard(group)
                d["toolsets_on"], d["toolsets_off"] = sorted(on), sorted(off)
                save_saved_settings(d)
                msg = f"{group}: {args[0]} (saved as the default)"
            except Exception as e:                 # noqa: BLE001
                msg += f" — couldn't save the default: {e}"
        return [msg]
    sent, everything = tl.chars()
    out = [f"tool list sent to the model: {sent:,} of {everything:,} characters "
           f"(about {sent // 4:,} of {everything // 4:,} tokens per call)"]
    about = {t.name: t.about for t in TOOLSETS}
    about["core"] = "read, list, write, edit files · run commands · todo · record a result · find more tools"
    for group, n, is_sent, why in tl.summary():
        mark = "sent" if is_sent else "    "
        desc = about.get(group, "MCP server" if group.startswith("mcp:") else "")
        out.append(f"  {mark}  {group:12s} {n:2d} tool{'s' if n != 1 else ' '}  {desc}"
                   + (f"  ({why})" if is_sent and why and group != "core" else "")
                   + ("  (past the tool-list budget)" if not is_sent and group in tl.over_budget else ""))
    out.append("groups not sent are found by the model when needed (tools_find) · "
               "/tools on|off <group> [permanent] · /tools list")
    return out

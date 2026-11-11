"""options.py — one typed list of Aura's options.

Where they fall short: raw-file edits with no type check, no confirm per
change, no simple undo; no screen says what an option costs or whether what it
needs is there.

What this module gives:
  - every option once: key, allowed values, default, plain meaning, what
    it costs (tokens per call, for tool groups) and what it needs;
  - get / set (checked) / reset, a mark for "changed from the default",
    old setting names carried forward;
  - find by description, locally (no model call);
  - an undo log: the last changes can be reverted one by one;
  - two tools: `settings` (look) and `settings_set` (change — asks the
    user every time, shows old -> new, audited by the mediator).

The settings live in the profile's settings.json (the profile's defaults).
Picker screens come at the UI stage (the copy); setup at the install stage —
both read this list.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Option:
    key: str
    section: str
    label: str
    about: str
    kind: str                         # "choice" | "bool" | "int" | "text"
    default: Any
    choices: tuple = ()
    lo: int | None = None
    hi: int | None = None
    env: str = ""                     # An environment variable that overrides
    needs: Callable[[], str] | None = None
    cost: Callable[[], int] | None = None
    getter: Callable[[dict], Any] | None = None       # Custom storage
    setter: Callable[[dict, Any], None] | None = None
    words: tuple = field(default=())  # Extra words for find
    # how much oversight the model gets — only the user changes it (the
    # model's settings_set refuses; setup / chat commands are the way)
    user_only: bool = False


# --------------------------------------------------------------- storage --
def _load() -> dict:
    from .config import load_saved_settings
    return load_saved_settings()


def _save(d: dict) -> None:
    from .config import save_saved_settings
    save_saved_settings(d)


def _is_group(key: str) -> bool:
    """tools.<toolset> — not tools.budget_pct / tools.park."""
    from .toolsets import BY_NAME
    return key.startswith("tools.") and key[6:] in BY_NAME


def _plain(key: str):
    def g(d):
        return (d.get("options") or {}).get(key)

    def s(d, v):
        d.setdefault("options", {})[key] = v
    return g, s


def _tri(getter):
    """auto / on / off — an older true/false value carries forward."""
    def g(d):
        v = getter(d)
        if v is True:
            return "on"
        if v is False:
            return "off"
        return v
    return g


def _lsp_state() -> str:
    try:
        from .lsp import installed
        import os.path
        found = [f"{k}: {os.path.basename(v)}" for k, v in installed().items() if v]
        return "installed: " + (", ".join(found) if found else "none found on PATH")
    except Exception:                                # noqa: BLE001
        return ""


def _helper_state() -> str:
    try:
        from .helper import state
        return state()
    except Exception:                                # noqa: BLE001
        return ""


def _measured(name: str) -> str:
    try:
        from . import model_facts as MF
        v = MF.fact(name)
        return (f"measured for {MF.ACTIVE}: {v}" if v else
                f"not measured for {MF.ACTIVE or 'the current model'} — aura eval model")
    except Exception:                                # noqa: BLE001
        return ""


# tool groups keep their storage (toolsets_on / toolsets_off) — the tool
# list reads those
def _group_get(name: str):
    def g(d):
        if name in (d.get("toolsets_on") or []):
            return "on"
        if name in (d.get("toolsets_off") or []):
            return "off"
        return None
    return g


def _group_set(name: str):
    def s(d, v):
        on = [x for x in (d.get("toolsets_on") or []) if x != name]
        off = [x for x in (d.get("toolsets_off") or []) if x != name]
        if v == "on":
            on.append(name)
        elif v == "off":
            off.append(name)
        d["toolsets_on"], d["toolsets_off"] = sorted(on), sorted(off)
    return s


def _consent_get(d):
    v = d.get("web_open_sources")
    if v is None and d.get("web_free_engines") in ("all", "knowledge"):
        return "on"                               # The earlier setting name
    if v is None and d.get("web_free_engines") == "off":
        return "off"
    return v


def _consent_set(d, v):
    if v == "ask":
        d.pop("web_open_sources", None)
    else:
        d["web_open_sources"] = v
    d.pop("web_free_engines", None)


# ----------------------------------------------------------- what is there --
def _installed(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def _group_cost(name: str) -> int:
    try:
        from .tool_schemas import SCHEMAS
        from .toolsets import BY_NAME
        return sum(len(json.dumps(SCHEMAS[t])) for t in BY_NAME[name].tools
                   if t in SCHEMAS) // 4
    except Exception:                                # noqa: BLE001
        return 0


def _web_needs() -> str:
    from . import websearch as W
    own = [e.label for e in W.ENGINES if e.category == W.OWN and e.available()]
    if own:
        return "connected: " + ", ".join(own)
    return ("no search service connected (aura keys set BRAVE_API_KEY …) — "
            "no-account sources: " + (W.consent() or "on"))


def _browser_needs() -> str:
    return "Playwright installed" if _installed("playwright") else \
        "browser part needs Playwright: pip install 'aura-agent[web]'"


def _themes() -> list[str]:
    try:
        from .themes import available
        return available()
    except Exception:                                # noqa: BLE001
        return ["dark", "light"]


def _options() -> list[Option]:
    from .toolsets import TOOLSETS
    out: list[Option] = []
    needs = {"web": lambda: _web_needs() + " · " + _browser_needs(),
             "checks": None, "background": None}
    for t in TOOLSETS:
        dflt = "auto"
        meaning = ("sent when the task needs it" + (" and by default with a large "
                   "context window" if t.big_model else "")
                   + ("; sent by default" if t.default else ""))
        out.append(Option(
            key=f"tools.{t.name}", section="Tools", label=f"{t.name} tools",
            about=f"{t.about}. auto = {meaning}; on = always; off = never "
                  "(the model can't use them)",
            kind="choice", choices=("auto", "on", "off"), default=dflt,
            needs=needs.get(t.name), cost=(lambda n=t.name: _group_cost(n)),
            getter=_group_get(t.name), setter=_group_set(t.name),
            words=t.words))
    g, s = _plain("edit.line_tags")
    out += [
        Option("web.open_sources", "Web", "open public sources",
               "use Wikipedia, arXiv, PubMed, Stack Overflow and GitHub for web "
               "search (no account; they receive your search words). ask = "
               "asked on the first search",
               "choice", "ask", choices=("ask", "on", "off"),
               env="AURA_WEB_OPEN_SOURCES", needs=_web_needs,
               getter=_consent_get, setter=_consent_set,
               words=("wikipedia", "arxiv", "free", "search", "sources")),
        Option("edit.line_tags", "Editing", "line tags",
               "read_file shows lines as 12:a3|code so edits can point at a line "
               "and a changed line is caught; off = plain 12|code; auto = what "
               "was measured for this model (aura eval model), tags if not measured",
               "choice", "auto", choices=("auto", "on", "off"), env="AURA_LINE_TAGS",
               getter=_tri(g), setter=s, needs=lambda: _measured("edit_form"),
               words=("tags", "hash", "read", "edit", "lines")),
    ]
    g, s = _plain("assist.early_check")
    out.append(Option("assist.early_check", "Assist", "ask-first check",
                      "at the start of a task, remind the model to ask when the "
                      "request is unclear (helps some models, costs a little); auto "
                      "= what was measured for this model, off if not measured",
                      "choice", "auto", choices=("auto", "on", "off"), getter=_tri(g),
                      setter=s, needs=lambda: _measured("early_check"),
                      words=("ask", "unclear", "vague", "clarify", "check", "first")))
    g, s = _plain("code.lsp")
    out.append(Option("code.lsp", "Code", "language server errors after edits",
                      "after an edit, ask the language server you have installed (pyright, "
                      "typescript-language-server, gopls, rust-analyzer, clangd) for errors "
                      "in that file and show them to the model; nothing happens when none "
                      "is installed",
                      "bool", True, getter=g, setter=s,
                      needs=lambda: _lsp_state(),
                      words=("lsp", "language", "server", "diagnostics", "errors", "pyright",
                             "typescript", "gopls")))
    g, s = _plain("helpers.max_running")
    out.append(Option("helpers.max_running", "Models", "helpers at once",
                      "the most helper agents running in the background at the same time",
                      "int", 3, lo=1, hi=10, getter=g, setter=s,
                      words=("helpers", "parallel", "background", "agents", "subagents")))
    g, s = _plain("model.api")
    out.append(Option("model.api", "Models", "how Aura talks to the model",
                      "auto = Anthropic's own Messages API at api.anthropic.com, OpenAI's "
                      "Responses API at api.openai.com (thinking carried between steps, "
                      "prompt caching), the common chat-completions API everywhere else · "
                      "chat / messages / responses = always that one",
                      "choice", "auto", choices=("auto", "chat", "messages", "responses"),
                      getter=g, setter=s,
                      words=("api", "anthropic", "openai", "responses", "messages", "native")))
    g, s = _plain("model.stall_seconds")
    out.append(Option("model.stall_seconds", "Models", "silence that ends a reply",
                      "once the model has started answering, this many seconds with no "
                      "new text ends the reply and it is tried again (keep-alive lines "
                      "don't count); the first word may take longer",
                      "int", 90, lo=15, hi=1800, getter=g, setter=s,
                      words=("stall", "stuck", "timeout", "silence", "hang", "gap")))
    g, s = _plain("model.reply_minutes")
    out.append(Option("model.reply_minutes", "Models", "longest one reply may take",
                      "a reply still going after this many minutes is ended and tried again",
                      "int", 30, lo=1, hi=240, getter=g, setter=s,
                      words=("stall", "stuck", "timeout", "total", "minutes", "long")))
    g, s = _plain("helper.model")
    out.append(Option("helper.model", "Models", "helper model",
                      "a cheaper model for side jobs (chat titles, summaries): the name "
                      "of a model setup (aura model list) or a model on the same server; "
                      "used only after Aura checks it keeps names and numbers exact "
                      "(aura eval helper) — otherwise the main model does them",
                      "text", "", getter=g, setter=s,
                      needs=lambda: _helper_state(),
                      words=("helper", "cheap", "second", "small", "model", "summary",
                             "titles", "auxiliary")))
    g, s = _plain("chat.message_check")
    out.append(Option("chat.message_check", "Chat", "understand each message",
                      "one short model call per message: is it work, a question or small "
                      "talk; does it need earlier tasks; typed mid-task — a question, a "
                      "change, or a separate request? A question is answered without "
                      "changes; earlier tasks it doesn't need aren't sent. Off = every "
                      "message is work, everything is sent (as before)",
                      "bool", True, getter=g, setter=s,
                      words=("intent", "question", "understand", "message", "task", "check")))
    g, s = _plain("history.auto_recall")
    out.append(Option("history.auto_recall", "Memory", "look up earlier chats",
                      "when a task mentions an earlier chat (last time, we decided, "
                      "did we ...), search past chats first — locally, no model "
                      "calls — and tell the model what was found",
                      "bool", True, getter=g, setter=s,
                      words=("history", "past", "earlier", "previous", "chats", "recall")))
    g, s = _plain("assist.goal_reminder")
    out.append(Option("assist.goal_reminder", "Assist", "restate the task in long runs",
                      "from step 6, the user's current request is repeated word for word at "
                      "the end of each call so long runs don't drift (off until measured to "
                      "beat the drift check)",
                      "bool", False, getter=g, setter=s,
                      words=("goal", "task", "drift", "remind", "anchor", "intent", "recite")))
    g, s = _plain("sandbox.required")
    out.append(Option("sandbox.required", "Safety", "require a sandbox for commands",
                      "refuse to run a command when this machine has no OS sandbox for it "
                      "(macOS Seatbelt, Linux bubblewrap/Landlock, Docker); off = run it "
                      "and say plainly that it wasn't sandboxed",
                      "bool", False, getter=g, setter=s, user_only=True,
                      words=("sandbox", "isolation", "contain", "jail", "landlock", "seatbelt")))
    # Install stage step 2: the level a NEW chat starts at — a chat keeps its
    # own once changed (/assist). Stored in the profile, never read from a
    # project's files.
    g, s = _plain("assist.level")
    out.append(Option("assist.level", "Safety", "how much Aura asks",
                      "L1 asks before every change · L2 asks only before risky steps "
                      "(can't be undone, or reaches outside the project) · L3 never asks — "
                      "new chats start here; /assist changes one chat",
                      "choice", "L2", choices=("L1", "L2", "L3"), getter=g, setter=s,
                      user_only=True,
                      words=("ask", "approve", "permission", "autonomy", "assist", "level")))
    g, s = _plain("assist.start")
    out.append(Option("assist.start", "Safety", "start new chats in",
                      "act = Aura works · plan = it looks and plans first, changes nothing "
                      "until you switch (/mode)",
                      "choice", "act", choices=("act", "plan"), getter=g, setter=s,
                      words=("plan", "act", "mode", "read-only", "start")))
    g, s = _plain("browser.engine")
    out.append(Option("browser.engine", "Browser", "which browser",
                      "installed = the Chrome or Edge already on this machine (nothing "
                      "to download), Playwright's Chromium if neither is there · "
                      "download = Playwright's own Chromium only",
                      "choice", "installed", choices=("installed", "download"),
                      getter=g, setter=s, words=("browser", "chrome", "edge", "chromium")))
    g, s = _plain("look.screen")
    out.append(Option("look.screen", "Look", "chat screen",
                      "inline = the conversation goes into your terminal's own scrollback "
                      "(native scroll, search, select); fullscreen = Aura uses the whole "
                      "window and scrolls it itself (PgUp/PgDn, the mouse wheel, Ctrl+Home/"
                      "End) — /tui switches mid-chat (AURA_SCREEN overrides it)",
                      "choice", "fullscreen", choices=("inline", "fullscreen"), getter=g, setter=s,
                      env="AURA_SCREEN", words=("fullscreen", "screen", "inline", "tui", "scroll")))
    g, s = _plain("model.thinking")
    out.append(Option("model.thinking", "Model", "thinking level",
                      "how hard a reasoning model thinks before it answers: auto = the "
                      "model's own default; off = fastest; low / medium / high = more "
                      "thinking, slower — /think changes it for a chat",
                      "choice", "auto", choices=("auto", "off", "low", "medium", "high"),
                      getter=g, setter=s,
                      words=("thinking", "reasoning", "effort", "think", "level")))
    g, s = _plain("assist.find_a_way")
    out.append(Option("assist.find_a_way", "Assist", "find a way to what's missing",
                      "on = when something is missing (a program, a package, a sandbox) Aura "
                      "offers to find a way — it asks before any searching or installing; off = "
                      "it says what is missing and what you can do. Known limits (what the model "
                      "itself can't do) are always said plainly — /findaway switches it",
                      "bool", False, getter=g, setter=s,
                      words=("find a way", "can't", "missing", "install", "sandbox", "workaround")))
    g, s = _plain("pblocks.on")
    out.append(Option("pblocks.on", "Assist", "Persona: learns how you work",
                      "on = the pblocks in this chat's panel (your personal one and any you chose "
                      "in /persona) judge each task and learn from it, on this computer only; "
                      "off = plain Aura, nothing used or learned. AURA_PBLOCKS=0 also turns it off",
                      "bool", True, env="AURA_PBLOCKS", getter=g, setter=s,
                      needs=lambda: __import__("aura_agent.pblocks", fromlist=["x"]).needs_line(),
                      words=("pblocks", "persona", "panel", "memory", "learn", "lessons")))
    g, s = _plain("updates.check")
    out.append(Option("updates.check", "Updates", "check for a new Aura once a day",
                      "on = once a day, when a chat opens, Aura asks GitHub whether a newer "
                      "release is out and says so in one quiet line (it installs nothing — say "
                      "'update' for that); off = it checks only when you ask. Sends nothing but "
                      "that one request",
                      "bool", False, getter=g, setter=s,
                      words=("update", "updates", "new version", "check for updates", "daily check")))
    g, s = _plain("machine.watch")
    out.append(Option("machine.watch", "Machine", "watch memory while Aura works",
                      "on = while a task runs Aura checks free memory every second (no model "
                      "call); when it gets low, the model is told once what Aura's own programs "
                      "hold, to go lighter. Off = only the check after each task. /machine "
                      "switches it",
                      "bool", False, getter=g, setter=s,
                      words=("memory", "ram", "machine", "out of memory", "oom", "watch")))
    g, s = _plain("chats.trash_days")
    out.append(Option("chats.trash_days", "Chats", "days a deleted chat is kept",
                      "/resume delete puts a chat in the trash; /resume restore brings it "
                      "back; after this many days it is removed for good (on start)",
                      "int", 30, getter=g, setter=s,
                      words=("trash", "delete", "chats", "sessions", "days", "restore")))
    g, s = _plain("look.sidebar")
    out.append(Option("look.sidebar", "Look", "sidebar in fullscreen",
                      "on = a column on the right with the model and context, the switches, "
                      "the plan, the files changed and what's waiting (windows 100+ wide) "
                      "— /sidebar switches it",
                      "choice", "on", choices=("on", "off"), getter=g, setter=s,
                      words=("sidebar", "side panel", "panel", "info")))
    g, s = _plain("look.focus")
    out.append(Option("look.focus", "Look", "focus view",
                      "on = your message, the answer and one line per task; tool rows are "
                      "hidden (failures still show) — /focus switches it",
                      "choice", "off", choices=("off", "on"), getter=g, setter=s,
                      words=("focus", "quiet", "tool rows", "compact")))
    g, s = _plain("look.mouse")
    out.append(Option("look.mouse", "Look", "mouse in fullscreen",
                      "on = the wheel scrolls and dragging selects and copies inside Aura; off "
                      "= your terminal's own selection (PgUp/PgDn still scroll) — takes "
                      "effect the next time fullscreen starts",
                      "bool", True, getter=g, setter=s, words=("mouse", "select", "copy", "wheel")))
    g, s = _plain("look.theme")
    out.append(Option("look.theme", "Look", "colour theme",
                      "the terminal colours (AURA_THEME overrides it for one launch)",
                      "choice", "dark", choices=tuple(_themes()), getter=g, setter=s,
                      env="AURA_THEME", words=("theme", "colour", "color", "dark", "light")))
    g, s = _plain("look.show_thinking")
    out.append(Option("look.show_thinking", "Look", "show the model's thinking",
                      "a model that thinks before answering: its thinking shows dimmed "
                      "while it works (AI_UI_REASONING overrides it)",
                      "bool", True, getter=g, setter=s, env="AI_UI_REASONING",
                      words=("thinking", "reasoning", "think", "show")))
    g, s = _plain("look.screen_reader")
    out.append(Option("look.screen_reader", "Look", "screen-reader mode",
                      "screen-reader = plain words instead of symbols, no bars or "
                      "animation; flat = plain words, layout kept (AURA_ACCESS overrides it)",
                      "choice", "off", choices=("off", "flat", "screen-reader"),
                      getter=g, setter=s, env="AURA_ACCESS",
                      words=("screen reader", "accessibility", "blind", "ascii", "plain")))
    g, s = _plain("look.reduce_motion")
    out.append(Option("look.reduce_motion", "Look", "no animation",
                      "no spinners or moving bars (AURA_REDUCE_MOTION overrides it)",
                      "bool", False, getter=g, setter=s, env="AURA_REDUCE_MOTION",
                      words=("motion", "animation", "spinner", "accessibility")))
    g, s = _plain("model.max_reply_tokens")
    out.append(Option("model.max_reply_tokens", "Model", "longest reply",
                      "the most tokens one reply may use; 0 = the model server's own "
                      "limit (a small cap can leave a thinking model no room to answer) "
                      "(AI_MAX_TOKENS overrides it)",
                      "int", 0, lo=0, hi=1_000_000, getter=g, setter=s, env="AI_MAX_TOKENS",
                      words=("max tokens", "reply length", "output", "limit")))
    g, s = _plain("model.fallbacks")
    out.append(Option("model.fallbacks", "Model", "fallback model setups",
                      "model setups to switch to when this one fails, comma-separated "
                      "(`aura model list` names them; AI_FALLBACKS overrides it)",
                      "text", "", getter=g, setter=s, env="AI_FALLBACKS",
                      words=("fallback", "backup", "failover", "model")))
    g, s = _plain("tools.budget_pct")
    out.append(Option("tools.budget_pct", "Tools", "room for tool descriptions",
                      "the most of the context window the tool list may take before a run "
                      "(%); groups past it aren't sent but the model finds them when needed "
                      "(tools_find). Groups you switched on always come. 0 = no limit",
                      "int", 10, lo=0, hi=100, getter=g, setter=s,
                      words=("tools", "budget", "schemas", "context", "room", "limit")))
    g, s = _plain("tools.park")
    out.append(Option("tools.park", "Tools", "put unused tools away at a summary",
                      "when a long chat is summarised (the cache starts over then anyway), "
                      "tool groups not used since the last summary stop being sent; the "
                      "model gets them back when needed",
                      "bool", True, getter=g, setter=s,
                      words=("tools", "park", "unload", "unused", "summary", "trim")))
    g, s = _plain("browser.show")
    out.append(Option("browser.show", "Browser", "watch the browser",
                      "show the browser window while Aura works in it (slowed down a little "
                      "so you can follow); off = it works hidden",
                      "bool", False, getter=g, setter=s,
                      words=("browser", "show", "watch", "visible", "headed", "window")))
    g, s = _plain("assist.suggest_checks")
    out.append(Option("assist.suggest_checks", "Assist", "suggest extra checks when needed",
                      "a coding task in a project with tests or a linter gets a one-line "
                      "suggestion to switch on proof / repro_first / auto_lint (they cost "
                      "tokens, so they stay off) — at most once a week per project each; "
                      "'/features never <name>' stops one for the project",
                      "bool", True, getter=g, setter=s,
                      words=("suggest", "proof", "lint", "repro", "tests", "checks", "hint")))
    g, s = _plain("research.mode")
    out.append(Option("research.mode", "Research", "research mode",
                      "basic = only the listed sources (your keyed services, Wikipedia, "
                      "arXiv, PubMed, Stack Overflow, GitHub); normal (default) = any "
                      "topic on the web, kept small — a few searches, the top pages read; "
                      "deep = a plan first, two sources per question, keeps going until "
                      "the scope is exhausted (or the budget) — /research basic|normal|deep "
                      "switches it for a chat",
                      "choice", "normal", choices=("basic", "normal", "deep"), getter=g, setter=s,
                      words=("research", "deep", "thorough", "exhaustive", "sources")))
    g, s = _plain("research.deep_max_searches")
    out.append(Option("research.deep_max_searches", "Research", "deep research budget",
                      "the most searches one deep-research task may run", "int", 60,
                      lo=10, hi=300, getter=g, setter=s,
                      words=("budget", "searches", "limit", "deep")))
    g, s = _plain("background.max_running")
    out.append(Option("background.max_running", "Background", "processes at once",
                      "how many background processes (servers, watchers) may run "
                      "at the same time", "int", 8, lo=1, hi=32, getter=g, setter=s,
                      words=("server", "processes", "limit", "background")))
    return out


def options() -> list[Option]:
    return _options()


def by_key(key: str) -> Option:
    for o in _options():
        if o.key == key:
            return o
    # forgiving: "web" -> "tools.web", "line_tags" -> "edit.line_tags"
    hits = [o for o in _options() if o.key.endswith("." + key) or o.key == f"tools.{key}"]
    if len(hits) == 1:
        return hits[0]
    raise KeyError(f"no option {key!r} — /settings lists them, /settings find <words>")


# --------------------------------------------------------- get/set/reset --
def parse(o: Option, raw: Any) -> Any:
    if o.kind == "text":                     # A name, as typed ("" or "none" = unset)
        t = str(raw).strip()
        if t.lower() in ("none", "off", '""', "''"):
            t = ""
        if len(t) > 120 or any(c in t for c in "\n\r\t"):
            raise ValueError(f"{o.key}: a short name")
        return t
    s = str(raw).strip().lower()
    if o.kind == "choice" and "on" in o.choices:          # 1/true/yes → on, 0/false/no → off
        s = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(s, s)
    if o.kind == "bool":
        if s in ("1", "on", "yes", "true"):
            return True
        if s in ("0", "off", "no", "false"):
            return False
        raise ValueError(f"{o.key}: on or off")
    if o.kind == "int":
        try:
            v = int(s)
        except ValueError:
            raise ValueError(f"{o.key}: a whole number")
        if (o.lo is not None and v < o.lo) or (o.hi is not None and v > o.hi):
            raise ValueError(f"{o.key}: between {o.lo} and {o.hi}")
        return v
    hit = next((c for c in o.choices if str(c).lower() == s), None)   # L1 / l1
    if hit is None:
        raise ValueError(f"{o.key}: one of {', '.join(o.choices)}")
    return hit


def get(key: str) -> Any:
    o = by_key(key)
    if o.env and os.environ.get(o.env, "").strip():
        try:
            return parse(o, os.environ[o.env])
        except ValueError:
            pass
    v = o.getter(_load()) if o.getter else None
    if v is None:
        return o.default
    try:
        return parse(o, v)
    except ValueError:
        return o.default


def changed(o: Option) -> bool:
    return get(o.key) != o.default


def _undo_path():
    from .paths import profile_path
    return profile_path("settings_undo.jsonl")


def set_(key: str, raw: Any, who: str = "user") -> tuple[Any, Any]:
    """Change an option (checked). Returns (old, new). The previous value
    goes to the undo log."""
    o = by_key(key)
    new = parse(o, raw)
    old = get(o.key)
    d = _load()
    stored_before = o.getter(d) if o.getter else None
    (o.setter or (lambda dd, vv: None))(d, new)
    _save(d)
    try:
        p = _undo_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time()), "key": o.key,
                                "before": stored_before, "old": old, "new": new,
                                "by": who}) + "\n")
    except OSError:
        pass
    return old, new


def reset(key: str) -> tuple[Any, Any]:
    o = by_key(key)
    old = get(o.key)
    d = _load()
    if _is_group(o.key):
        _group_set(o.key.split(".", 1)[1])(d, "auto")
    elif o.key == "web.open_sources":
        _consent_set(d, "ask")
    else:
        (d.get("options") or {}).pop(o.key, None)
    _save(d)
    return old, o.default


def undo() -> str:
    """Revert the most recent change still in the log."""
    p = _undo_path()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if not lines:
        return "nothing to undo"
    last = json.loads(lines[-1])
    o = by_key(last["key"])
    d = _load()
    if last.get("before") is None:
        _ = reset(o.key)
        d = _load()
    else:
        (o.setter or (lambda dd, vv: None))(d, last["before"])
        _save(d)
    p.write_text("\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else ""), encoding="utf-8")
    return f"undone: {o.key} back to {fmt(get(o.key))} (was {fmt(last['new'])})"


def fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


# --------------------------------------------------------------- showing --
def listing(section: str = "") -> list[str]:
    out = []
    cur = None
    for o in _options():
        if section and o.section.lower() != section.lower():
            continue
        if o.section != cur:
            cur = o.section
            out.append(f"{cur}:")
        v = get(o.key)
        mark = "*" if changed(o) else " "
        extra = []
        if o.cost:
            c = o.cost()
            if c:
                extra.append(f"~{c} tokens/call when sent")
        if o.env and os.environ.get(o.env, "").strip():
            extra.append(f"set by {o.env}")
        line = f" {mark} {o.key:26s} {fmt(v):6s} {o.label}" + (f"  ({'; '.join(extra)})" if extra else "")
        out.append(line)
        if o.needs:
            try:
                out.append(f"      {o.needs()}")
            except Exception:                        # noqa: BLE001
                pass
    out.append("* = changed from the default · /settings set KEY VALUE · /settings "
               "reset KEY · /settings undo · /settings find WORDS")
    return out


def explain(key: str) -> str:
    o = by_key(key)
    allowed = (", ".join(o.choices) if o.kind == "choice" else
               "on / off" if o.kind == "bool" else
               "a name, or none" if o.kind == "text" else f"{o.lo}–{o.hi}")
    return (f"{o.key} = {fmt(get(o.key))} (default {fmt(o.default)}; allowed: {allowed})\n"
            f"  {o.about}" + (f"\n  {o.needs()}" if o.needs else ""))


def find(words: str, limit: int = 5) -> list[Option]:
    """Options matching a description."""
    from .toolsets import bm25
    # tool groups are found by what they do (their task trigger words
    # would pull e.g. the settings group into every settings question)
    docs = {o.key: " ".join([o.key.replace(".", " ").replace("_", " "), o.label,
                             o.about, "" if _is_group(o.key) else " ".join(o.words)])
            for o in _options()}
    hits = [k for k, _ in bm25(words, docs)[:limit]]
    return [by_key(k) for k in hits]


# ------------------------------------------------------------------ tools --
# Switches that aren't stored options, changed by the same tool when the user
# asks: the checks (features), talk-only, a folder
SWITCHES = {
    "proof": ("feature", "proof", "check the work (re-read, run the tests) before answering"),
    "lint": ("feature", "auto_lint", "run the linter after code changes"),
    "repro_first": ("feature", "repro_first", "show a bug with a failing test first"),
    "drift_check": ("feature", "drift_check", "notice when the work wanders from the task"),
    "reviewer": ("feature", "reviewer", "a second opinion after repeated failures"),
    "syntax_check": ("feature", "syntax_note", "warn when a write breaks a file's syntax"),
    "discuss": ("mode", "discuss", "talk only — no changes (on) / back to work (off)"),
    "allow_folder": ("folder", "", "let Aura read and use a folder outside the project — "
                                   "Downloads, Desktop, Documents, any path (value: the folder)"),
}


def _switch_set(mediator, key: str, value) -> str:
    kind, name, _ = SWITCHES[key]
    on = str(value).strip().lower() in ("on", "true", "yes", "1", "")
    if kind == "feature":
        from . import features
        return features.set_feature(name, on, permanent=False) + " — /features to see all"
    if kind == "mode":
        mediator.mode = "discuss" if on else "act"
        return f"discuss: {'on — talk only, no changes' if on else 'off — back to work'}"
    if kind == "folder":
        import os
        from pathlib import Path
        from .outside import addable
        f = Path(os.path.expandvars(str(value))).expanduser()
        if not f.is_absolute():
            # "Downloads", "my Desktop": the user's own folder of that name
            name = str(value).strip().lower().replace("my ", "")
            home = Path.home()
            hit = next((d for d in home.iterdir() if d.is_dir() and d.name.lower() == name), None)                 if home.exists() else None
            f = hit or (Path(getattr(mediator, "work_dir", None) or ".") / f)
        why = addable(f)
        if why:
            return f"not allowed: {why}"
        mediator.outside_ok.add(f.resolve())
        return f"{f.resolve()} allowed for this chat"
    return "unknown switch"


def _known_key(key: str) -> bool:
    if key in SWITCHES:
        return True
    try:
        by_key(key)
        return True
    except KeyError:
        return False


def _did_you_mean(key: str) -> str:
    """An unknown key: the closest real ones, with their values — so the model
    fixes it in one step.
    """
    words = key.replace(".", " ").replace("_", " ")
    out = []
    for o in find(words, limit=3):
        vals = " · ".join(o.choices) if getattr(o, "choices", None) else o.kind
        out.append(f"{o.key} ({vals})")
    out += [k for k, (_, _, why) in SWITCHES.items()
            if any(w in (k + " " + why).lower() for w in words.lower().split() if len(w) > 3)][:3]
    return (f"unknown key '{key}' — nothing changed. Did you mean: " + "; ".join(out)
            if out else f"unknown key '{key}' — call settings with find=<words> to find it")


def register(mediator, dispatch: dict) -> None:
    from .mediator import BlastRadius, ToolSpec

    def settings(args) -> str:
        what = str(args.get("find") or args.get("key") or "").strip()
        if args.get("key"):
            try:
                return explain(str(args["key"]))
            except KeyError as e:
                return str(e)
        switches = [f"{k}: {why}" for k, (_, _, why) in SWITCHES.items()
                    if not what or what.lower() in (k + " " + why).lower()]
        if what:
            hits = find(what)
            if not hits and not switches:
                return f"no option matches {what!r}"
            return "\n".join([explain(o.key) for o in hits] + switches)
        return "\n".join(listing() + ["switches (settings_set key, value on/off):"] + switches)

    def settings_set(args) -> str:
        key = str(args.get("key") or "")
        if not _known_key(key):
            return _did_you_mean(key)
        if key in SWITCHES:
            return _switch_set(mediator, key, args.get("value", "on"))
        try:
            if by_key(key).user_only:
                return (f"not changed: {key} decides how much you are asked — only you "
                        "change it (aura setup, or /assist in the chat). Tell the user "
                        "what you'd change and why.")
        except KeyError:
            pass
        if str(args.get("action") or "set") == "reset":
            old, new = reset(key)
        else:
            old, new = set_(key, args.get("value"), who="agent")
        return (f"{by_key(key).key}: {fmt(old)} -> {fmt(new)} (saved as the "
                "profile's default; /settings undo reverts it)")

    def describe_set(args) -> str:
        key = str(args.get("key") or "")
        if key in SWITCHES:
            why = SWITCHES[key][2]
            return (f"{key}: {args.get('value', 'on')} — {why}"
                    + (f" · reason: {str(args.get('reason'))[:120]}" if args.get("reason") else ""))
        try:
            o = by_key(str(args.get("key") or ""))
            new = o.default if str(args.get("action") or "") == "reset" else parse(o, args.get("value"))
            return (f"change setting {o.key}: {fmt(get(o.key))} -> {fmt(new)} — "
                    f"{o.about[:160]}" + (f" · reason: {str(args.get('reason'))[:120]}"
                                          if args.get("reason") else ""))
        except (KeyError, ValueError) as e:
            return f"invalid setting change: {e}"

    mediator.register(ToolSpec("settings", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["settings"] = settings
    # Changing Aura's own settings: the user confirms EVERY time, at every
    # assist level (never pre-approved), and sees old -> new a real key: the
    # user confirms every change, at every level (never pre-approved); an
    # unknown key is answered at once, with no card
    mediator.register(ToolSpec("settings_set", BlastRadius.WRITE, lambda a: None,
                               describe=describe_set,
                               ask_when=lambda a: ("a change to Aura's settings"
                                                   if _known_key(str(a.get("key") or "")) else None)))
    dispatch["settings_set"] = settings_set

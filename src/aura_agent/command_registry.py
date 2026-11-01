"""command_registry.py — the single source of truth for slash commands.

One table feeds help, the `/` picker, TAB completion, and startup hints. Every
entry: name, plain-English description (verb-first, no jargon), usage string, a
group (basics | project | model | session), optional aliases, and an optional
example.

Rules (plan): - descriptions say what YOU (the user) get — never internal
jargon - usage shows both bare and argument forms where both exist - bare form
= picker, never a syntax error - aliases resolve through get and completion
(/reset → new)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str          # "/model"
    desc: str          # "switch the AI model mid-chat — bare opens a picker"
    usage: str         # "usage: model [name] · bare = picker"
    group: str         # Basics | project | model | session
    example: str | None = None
    aliases: tuple[str, ...] = ()  # ("/reset")


COMMANDS: list[CommandSpec] = [
    # --- basics -----------------------------------------------------------
    CommandSpec("/help", "show every command, grouped, with how to use it",
                "usage: /help", group="basics", example="try: /help"),
    CommandSpec("/status", "where things stand: mode, model, context, spend",
                "usage: /status", group="basics", example="try: /status"),
    CommandSpec("/new", "start a fresh session (current one stays on disk)",
                "usage: /new [name]  ·  /new now skips the confirm",
                group="basics", aliases=("/reset",)),
    CommandSpec("/resume", "your chats, by id — open, search, delete (trash), restore, pin, "
                "archive, rename, export",
                "usage: /resume · /resume <#id|title|latest> · info|delete|restore|pin|unpin|"
                "archive|unarchive <#id> · rename <#id> <title> · export <#id> [md|jsonl] · "
                "search <words> · trash [empty] · archived · sort used|name|full|made",
                group="basics", aliases=("/sessions",)),
    CommandSpec("/delete", "delete a chat into the trash (= /resume delete; restorable)",
                "usage: /delete <#id>", group="basics"),
    CommandSpec("/retry", "re-send your last message to the agent",
                "usage: /retry", group="basics"),
    CommandSpec("/assist", "set how autonomous the agent is (L1 watch / L2 act)",
                "usage: /assist  ·  bare = menu  ·  /assist L1|L2",
                group="basics"),
    CommandSpec("/mode", "switch BUILD (full tools) / PLAN (read-only)",
                "usage: /mode  ·  bare = menu  ·  /mode act|plan",
                group="basics"),
    CommandSpec("/kill", "stop a running task (Esc does the same)",
                "usage: /kill", group="basics"),
    CommandSpec("/keys", "every keyboard shortcut",
                "usage: /keys  ·  or press ? in an empty box", group="basics"),
    CommandSpec("/copy", "copy the last answer to the clipboard",
                "usage: /copy", group="basics"),
    CommandSpec("/focus", "a quiet view: your message, the answer, one line per task (tool rows hidden)",
                "usage: /focus", group="basics"),
    CommandSpec("/findaway", "when something is missing, Aura offers to find a way (asks first) — on/off",
                "usage: /findaway · /findaway on|off", group="model"),
    CommandSpec("/think", "how hard the model thinks: auto, off, low, medium, high (this chat)",
                "usage: /think · /think off|low|medium|high|auto [permanent]", group="model"),
    CommandSpec("/sidebar", "the fullscreen sidebar: model, context, switches, plan, changed files (on/off)",
                "usage: /sidebar", group="basics"),
    CommandSpec("/tui", "the chat screen: inline (your terminal's scrollback) or fullscreen (the whole window)",
                "usage: /tui · /tui fullscreen|inline", group="basics"),
    CommandSpec("/vim", "vim keys in the input box (on/off, remembered)",
                "usage: /vim", group="basics"),
    CommandSpec("/statusline", "choose what the status line shows",
                "usage: /statusline [mode model context tokens project branch time | reset]",
                group="basics"),
    CommandSpec("/exit", "save a checkpoint and leave",
                "usage: /exit", group="basics", aliases=("/quit",)),
    # --- project ----------------------------------------------------------
    CommandSpec("/results", "results the agent recorded, and whether each is proven",
                "usage: /results [id]", group="project", aliases=("/findings",)),
    CommandSpec("/receipt", "show a run receipt: what ran, proof, tokens",
                "usage: /receipt [run-id | verify]  ·  bare = latest  ·  "
                "verify = check the receipt chain", group="project"),
    CommandSpec("/insights", "session analytics from the audit tape",
                "usage: /insights", group="project"),
    CommandSpec("/usage", "token ledger for this session",
                "usage: /usage", group="project"),
    CommandSpec("/mcp", "MCP servers: list, well-known ones, search, add (asks), remove, sign in",
                "usage: /mcp · /mcp popular · /mcp search <words> · /mcp add <name> · /mcp remove <name>"
                " · /mcp login|logout <name> [paste]",
                group="project"),
    CommandSpec("/cron", "scheduled jobs: a card each (next run, last result); add, pause, resume, remove, run",
                "usage: /cron · /cron add <when> <task> · /cron pause|resume|remove|run <id>",
                group="project"),
    CommandSpec("/blueprint", "ready-made scheduled jobs — shown and confirmed before adding",
                "usage: /blueprint · /blueprint <name> field=value …", group="project"),
    CommandSpec("/transcript", "the whole chat so far, scrollable (tool calls and notes too)",
                "usage: /transcript", group="basics"),
    CommandSpec("/setup", "the setup board — every choice on one screen, each checked on this machine",
                "usage: /setup", group="basics"),
    CommandSpec("/image", "attach the clipboard's image (or a file) to your next message",
                "usage: /image · /image <file>", group="basics"),
    CommandSpec("/vault", "saved site logins (the model never sees them)",
                "usage: /vault · /vault remove <site> · adding: aura vault add <site> in a terminal",
                group="project"),
    CommandSpec("/research", "how far web research goes: basic · normal (default) · deep",
                "usage: /research · /research basic|normal|deep [permanent]", group="project"),
    CommandSpec("/settings", "Aura's options: list, find, set, reset, undo (alias /config)",
                "usage: /settings · /settings find WORDS · /settings KEY · "
                "/settings set KEY VALUE · /settings reset KEY · /settings undo",
                group="project"),
    CommandSpec("/web", "web search: connected services, open public sources, all-websites switch",
                "usage: /web · /web open on|off · /web all on|off",
                group="project"),
    CommandSpec("/add-dir", "folders outside the project Aura may also use",
                "usage: /add-dir  ·  /add-dir <folder> [permanent]  ·  /add-dir remove <folder>",
                group="project"),
    CommandSpec("/scope", "websites and hosts this project may reach",
                "usage: /scope  ·  /scope add example.com  ·  /scope remove <item>",
                group="project"),
    CommandSpec("/rules", "project constraints — add, remove, list what must hold",
                "usage: /rules  ·  /rules add <text>  ·  /rules rm <n>",
                group="project"),
    CommandSpec("/team", "Aura Teams: the hub, your connection, the board, link/start/done, "
                "decisions, waiting updates, the chat page",
                "usage: /team · /team connect <hub address> · board [--all] · link <request> · "
                "start · done · decision <text> · read <#task> · queue · sync · "
                "hub setup|start|stop|status · chat on|off|status", group="project"),
    CommandSpec("/decision", "record a decision on this chat's team board task",
                "usage: /decision <what was decided>", group="project"),
    CommandSpec("/todos", "the agent's live checklist for this project",
                "usage: /todos", group="project"),
    CommandSpec("/agents", "show AGENTS.md standing instructions",
                "usage: /agents", group="project"),
    CommandSpec("/tools", "tool groups: which are sent to the model, switch them",
                "usage: /tools · /tools on|off <group> [permanent] · /tools list",
                group="project"),
    CommandSpec("/skills", "list installed skill packs",
                "usage: /skills", group="project"),
    CommandSpec("/memory", "what the agent remembers — and forget a fact",
                "usage: /memory  ·  /memory forget <words>", group="project"),
    CommandSpec("/audit", "the action chain: what it did, to what, what survived",
                "usage: /audit [N]", group="project"),
    CommandSpec("/feedback", "report a problem or idea — consent, "
                "saved locally, never silent",
                "usage: /feedback <text>", group="session",
                example="try: /feedback the report needs a severity filter"),
    CommandSpec("/diff", "what changed: in fullscreen a panel beside the chat; else the last snapshot vs the file",
                "usage: /diff [sha]", group="project"),
    CommandSpec("/undo", "restore files from a snapshot (revert agent writes)",
                "usage: /undo", group="project"),
    CommandSpec("/rewind", "rewind the conversation to an earlier step",
                "usage: /rewind <seq>  ·  bare shows the current seq",
                group="project"),
    CommandSpec("/fork", "copy this chat up to a step into a new chat (its own id; remembers where it came from)",
                "usage: /fork [seq]", group="project"),
    CommandSpec("/search", "search past chats, with how each one ended",
                "usage: /search <words> [project:all] [after:7d] [file:x] "
                "[outcome:proven]", group="project"),
    CommandSpec("/reindex", "rebuild the past-chat search index",
                "usage: /reindex", group="project"),
    # --- model ------------------------------------------------------------
    CommandSpec("/model", "switch the AI model mid-chat — context re-checked",
                "usage: /model [name] · /model use <setup> [permanent] · "
                "bare = pick from a menu",
                group="model", example="try: /model"),
    CommandSpec("/prompt", "the prompt this model uses (Aura's own, for every model)",
                "usage: /prompt [show] · /prompt reset (deletes an old tuned copy) — "
                "the model maker's own settings: aura model settings",
                group="model", example="try: /prompt"),
    CommandSpec("/profile", "old name of /model use — switch model setup for this session",
                "usage: /profile [name] [permanent]  ·  bare = pick from a menu",
                group="model"),
    CommandSpec("/connect", "provider switcher with live endpoint status",
                "usage: /connect [part] [permanent]", group="model"),
    CommandSpec("/context", "context meter: what fills the token window",
                "usage: /context", group="model"),
    CommandSpec("/compact", "compress history now to free context room",
                "usage: /compact [focus]", group="model"),
    CommandSpec("/prune", "trim old outputs not in use now (free; each can be reopened)",
                "usage: /prune", group="model"),
    CommandSpec("/budget", "show or change usage limits (preset / tool-output / prune / web depth)",
                "usage: /budget [preset <name>] [set <key> <value>] [permanent]", group="model"),
    CommandSpec("/checkpoint", "save a snapshot of the project folder",
                "usage: /checkpoint [label]", group="safety"),
    CommandSpec("/restore", "restore a workspace checkpoint (latest or by id)",
                "usage: /restore [checkpoint-id|latest]", group="safety"),
    CommandSpec("/discuss", "talk only: Aura may read and search to understand, never does the work",
                "usage: /discuss · /discuss on|off", group="model"),
    CommandSpec("/proof", "check the answer before it's given: re-read what it rests on, run the tests",
                "usage: /proof · /proof on|off [permanent]", group="model"),
    CommandSpec("/features", "switch behaviour features on/off; presets",
                "usage: /features [on|off <name> | preset recommended|minimal|off] [permanent]",
                group="model"),
    CommandSpec("/approvals", "saved permissions; save or remove one",
                "usage: /approvals [allow <n> | revoke <id>]", group="model"),
    CommandSpec("/theme", "switch the display theme",
                "usage: /theme [name]  ·  bare shows current + options",
                group="model"),
    CommandSpec("/debug", "show full tool output (on/off)",
                "usage: /debug", group="model"),
    # --- session ----------------------------------------------------------
    CommandSpec("/rename", "a title for this chat (its id stays)",
                "usage: /rename <title>", group="session",
                example="try: /rename login-bug-fix", aliases=("/title",)),
    CommandSpec("/export", "this chat (or any by #id) to md or jsonl, keys scrubbed",
                "usage: /export [#id] [md|jsonl] [path]", group="session"),
    CommandSpec("/steer", "course-correct the running task mid-turn",
                "usage: /steer <message>", group="session"),
    CommandSpec("/powerup", "one contextual tip about a feature you haven't used",
                "usage: /powerup", group="session"),
]

_DAILY_FIVE = ["/help", "/model", "/undo", "/status", "/exit"]

_ALIASES: dict[str, str] = {
    a.lstrip("/"): c.name for c in COMMANDS for a in c.aliases
}


def get(name: str) -> CommandSpec | None:
    n = name.lower().lstrip("/")
    n = _ALIASES.get(n, n).lstrip("/")   # Alias values are canonical names
    n = f"/{n}"
    for c in COMMANDS:
        if c.name == n:
            return c
    return None


def groups() -> dict[str, list[CommandSpec]]:
    ordered: dict[str, list[CommandSpec]] = {}
    for g in ("basics", "project", "model", "safety", "session"):
        ordered[g] = [c for c in COMMANDS if c.group == g]
    return ordered


def daily_five() -> list[CommandSpec]:
    by_name = {c.name: c for c in COMMANDS}
    return [by_name[n] for n in _DAILY_FIVE]

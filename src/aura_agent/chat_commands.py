"""chat_commands.py — every chat slash command, in one place.

Before this module the commands lived inside the classic chat loop and
the full-screen chat had its own short copy (11 commands). Now both chat
screens call dispatch(), so every command works everywhere.

The UI hands a ChatContext: the agent pieces (loop, mediator, session…)
plus three callbacks — ask() for a typed answer, choose() for a picker and
show_context() — so commands never call input() themselves.
"""
from __future__ import annotations


import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import ui
from .sessions import Session, new_session


@dataclass
class Result:
    handled: bool = True
    task: str | None = None        # The command turned into a task to run
    shown: str | None = None       # What the chat shows for it (the task itself goes to the model)
    exit: bool = False


@dataclass
class ChatContext:
    eng: Path
    loop: Any
    m: Any
    profile: Any
    session: Any
    state: dict
    ask: Callable[[str], str] = lambda prompt: ""
    choose: Callable[[str, list], Any] = lambda title, options: None
    show_context: Callable[[], None] = lambda: None
    open_session_settings: Callable[[Any], None] = lambda s: None
    defaults_take: Callable[..., None] = lambda *k: None
    version: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def governor(self):
        return getattr(self.loop, "governor", None)


def _say_list(lines) -> None:
    for ln in lines:
        print(ui.muted("  " + ln))


# ----------------------------------------------------------- the dispatcher --
def dispatch(ctx: ChatContext, line: str) -> Result:
    """Run one slash command. Unknown commands try the project's custom
    commands (commands/*.md) and otherwise say so."""
    parts = line.split()
    if not parts:
        return Result()
    cmd = parts[0].lower()
    from .command_registry import _ALIASES
    base = cmd[1:]
    if base in _ALIASES:
        cmd = _ALIASES[base]
        parts = [cmd] + parts[1:]
        line = " ".join(parts)
    if cmd == "/model" and len(parts) > 1 and parts[1] == "use":
        parts = ["/profile"] + parts[2:]
        cmd, line = "/profile", " ".join(parts)
    if cmd == "/resume":
        cmd = "/sessions"                  # One handler for every chat action
        parts = ["/sessions"] + parts[1:]
        line = " ".join(parts)
    fn = _COMMANDS.get(cmd)
    if fn is not None:
        r = fn(ctx, parts, line)
        return r if isinstance(r, Result) else Result()
    return _custom_or_unknown(ctx, cmd, line)


_COMMANDS: dict[str, Callable] = {}


def command(*names):
    def deco(fn):
        for n in names:
            _COMMANDS[n] = fn
        return fn
    return deco


# ------------------------------------------------------------------ basics --
@command("/exit")
def _exit(ctx, parts, line):
    try:
        from .last_state import checkpoint_line
        ui.success(checkpoint_line(ctx.eng))
    except Exception:          # noqa: BLE001
        ui.success("saved — `aura resume` continues")
    return Result(exit=True)


@command("/help")
def _help(ctx, parts, line):
    from .command_registry import groups
    titles = {"basics": "basics", "project": "this project",
              "model": "model & context", "safety": "safety",
              "session": "this chat"}
    for g, cmds in groups().items():
        if not cmds:
            continue
        print(ui.accent(f"  {titles.get(g, g)}"))
        for c in cmds:
            print(f"    {ui.value(c.name):<22} " + ui.muted(c.desc))
    try:
        from .custom_commands import discover
        custom = discover(ctx.eng)
        if custom:
            print(ui.accent("  your commands"))
            for n, c in custom.items():
                print(f"    {ui.value('/' + n):<22} "
                      + ui.muted(c.description or c.source))
    except Exception:          # noqa: BLE001
        pass
    ui.info("keys: ? shows every shortcut · Tab completes · @ adds a file")


@command("/")
def _bare_slash(ctx, parts, line):
    return _help(ctx, parts, line)


@command("/status")
def _status(ctx, parts, line):
    gov = ctx.governor
    n = gov.count(ctx.session.messages) if gov else 0
    pct = int(100 * n / gov.ctx) if gov and gov.ctx else 0
    from .config_line import config_line
    from .ctx_probe import describe_source
    p = ctx.loop.llm.p
    key_s = ("set" if (getattr(p, "api_key", None)
                       or "localhost" in (p.base_url or "")) else "missing")
    print("  " + config_line(getattr(ctx.profile, "name", "?"), p.model,
                             gov.ctx if gov else "?", key_s, "saved profile"))
    print(f"  mode {getattr(ctx.m, 'mode', 'act')} · asks "
          f"{_assist_words(getattr(ctx.m, 'assist', 'L2'))}")
    print(f"  context {n:,}/{gov.ctx if gov else '?':,} ({pct}%) — "
          f"{describe_source(getattr(ctx.loop, 'ctx_source', None))}"
          if gov else "  context: not tracked")
    print(f"  messages {len(ctx.session.messages)} · tokens this chat "
          f"{gov.token_ledger if gov else 0:,}")
    # trust state: what is switched on, what limits apply, whether the
    # record of past runs is intact
    try:
        from .features import FEATURES, active
        on = active()
        print("  features " + " · ".join(
            (ui.ok(n) if n in on else ui.muted(n + " off")) for n in FEATURES))
    except Exception:          # noqa: BLE001
        pass
    try:
        from .engagement import load_scope
        sc = load_scope(ctx.eng)
        n_reach = sum(len(sc.get(k) or []) for k in ("domains", "hosts", "cidrs"))
        from . import constraints as _c
        n_rules = len(_c._read_rules(ctx.eng))
        print(f"  network {n_reach} allowed place(s) · rules {n_rules} · "
              f"files limited to {getattr(ctx.loop, 'workspace', ctx.eng)}")
    except Exception:          # noqa: BLE001
        pass
    try:
        from .receipts import verify_chain
        v = verify_chain(ctx.eng)
        if v.get("checked"):
            print("  run receipts " + (ui.ok(f"{v['checked']} intact") if v.get("ok")
                                       else ui.warn(f"{len(v.get('breaks', []))} broken — /receipt verify")))
    except Exception:          # noqa: BLE001
        pass


def _assist_words(level: str) -> str:
    return {"L1": "before every change", "L2": "before risky actions",
            "L3": "never"}.get(level, level)


@command("/new")
def _new(ctx, parts, line):
    if len(ctx.session.messages) > 2 and not any(
            a in ("now", "--yes", "-y") for a in parts[1:]):
        ans = ctx.ask("start a fresh chat? this one stays saved [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            ui.info("kept the current chat")
            return Result()
    name = next((p for p in parts[1:] if not p.startswith(("-", "now"))), "chat")
    ctx.session = new_session(ctx.eng / "sessions", name, lazy=True)
    ctx.open_session_settings(ctx.session)
    from .engine.loop import DEFAULT_SYSTEM
    ctx.session.append("system", content=ctx.loop.cfg.system_prompt or DEFAULT_SYSTEM)
    ui.success(f"new chat #{ctx.session.id} — the last one stays in /resume")


def _ago(ts: float) -> str:
    import time as _t
    m = max(0.0, (_t.time() - float(ts or 0)) / 60)
    return (f"{m / 1440:.0f}d ago" if m >= 1440 else f"{m / 60:.0f}h ago" if m >= 60
            else f"{m:.0f}m ago" if m >= 1 else "just now")


def _chat_name(s: dict) -> str:
    """A chat's name: its title, else its first task (files are named
    '<time>-chat', which says nothing), else the file's words."""
    from .session_titles import session_title
    name = s.get("title") or ""
    if not name:
        auto = session_title(s["file"])
        name = (s.get("tasks") or [""])[0] or s.get("preview") or auto
        if auto not in ("chat", "(untitled)") and not s.get("tasks"):
            name = auto
    return name or "(empty chat)"


def _chat_detail(s: dict) -> list[str]:
    """What a chat worked on — shown under the highlighted chat in /resume."""
    tasks, files = s.get("tasks") or [], s.get("files") or []
    out = []
    if tasks:
        out.append(f"worked on ({len(tasks)} task{'s' if len(tasks) != 1 else ''}):")
        shown = tasks if len(tasks) <= 4 else tasks[:3]
        for t in shown:
            out.append(f"  • {t[:90]}")
        if len(tasks) > 4:                 # The first asks, then the newest
            out.append(f"  … {len(tasks) - 4} more")
            out.append(f"  • {tasks[-1][:90]}  (last)")
    else:
        out.append("no task yet")
    if files:
        shown = ", ".join(files[-4:]) + (f" +{len(files) - 4} more" if len(files) > 4 else "")
        out.append(f"changed: {shown}")
    use = s.get("usage") or {}
    if use.get("in") or use.get("out") or use.get("calls"):
        out.append(f"used: ↑{_k(use.get('in'))} ↓{_k(use.get('out'))} tokens · "
                   f"{use.get('calls', 0)} tool calls"
                   + (f" · {', '.join(use.get('models') or [])}" if use.get("models") else "")
                   + (f" · {_dur(s.get('active_s'))} active" if s.get("active_s") else ""))
    if s.get("parent"):
        out.append(f"forked from #{s['parent']}")
    return out


def _k(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _dur(sec) -> str:
    sec = int(sec or 0)
    return (f"{sec // 86400}d {sec % 86400 // 3600}h" if sec >= 86400 else
            f"{sec // 3600}h {sec % 3600 // 60}m" if sec >= 3600 else
            f"{sec // 60}m" if sec >= 60 else f"{sec}s")


_RESUME_HELP = ("/resume · /resume <#id|title|N|latest> · info|delete|restore|pin|unpin|"
                "archive|unarchive <#id> · rename <#id> <title> · export <#id> [md|jsonl] · "
                "search <words> · trash [empty] · archived · sort used|name|full|made")


def _pick_chats(ctx, chats: list[dict], title: str):
    """The chat picker: each chat by name with its id, what it did on the
    highlighted one. Returns the picked file name (or None)."""
    ctx_n = getattr(ctx.governor, "ctx", 0) or 0
    options = []
    for s in chats[:40]:
        full = (f"{min(999, round(100 * s['tokens'] / ctx_n))}% full" if ctx_n
                else f"~{s['tokens'] // 1000}k tokens")
        options.append((("★ " if s.get("pinned") else "") + _chat_name(s)[:44], s["file"],
                        f"#{s.get('id', '')} · {s['messages']} msgs · {full} · used {_ago(s['used_ts'])}"
                        f" · made {_ago(s['made_ts'])}",
                        s["file"] == ctx.session.path.name,
                        _chat_detail(s)))
    return ctx.choose(title, options)


def _one(ctx, ref: str, include_archived: bool = True):
    """The chat a reference names, or None (and says why)."""
    from .session_store import resolve
    path, many = resolve(ctx.eng, ref, include_archived)
    if path is None:
        if many:
            ui.warn(f"{len(many)} chats match {ref!r} — "
                    + ", ".join(f"#{s['id']} {_chat_name(s)[:24]}" for s in many[:5])
                    + " · type more of the id")
        else:
            ui.warn(f"no chat {ref!r} — /resume lists them (deleted ones: /resume trash)")
    return path


def _info(eng, path) -> dict | None:
    from .session_store import list_sessions
    return next((x for x in list_sessions(eng) if x["file"] == path.name), None)


@command("/sessions")
def _sessions(ctx, parts, line):
    """Every chat action in one command. A chat is named by its id (#a1b2c3 — or a
    unique start of it), "latest", its number in the list, or its title. Delete
    goes to the trash (30 days, restorable); nothing is removed for good
    without asking.
    """
    from .session_store import (list_sessions, trashed, restore_chat, empty_trash,
                                trash_chat)
    from .last_state import resume_honest_limits
    eng = ctx.eng
    sub = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2:]
    here = ctx.session.path.name

    if sub in ("help", "?"):
        ui.info(_RESUME_HELP)
        return Result()

    if sub == "search":
        q = " ".join(rest).strip()
        if not q:
            ui.info("usage: /resume search <words> — looks in every message of every chat")
            return Result()
        hits = _search_chats(eng, q)
        if not hits:
            ui.info(f"no chat says {q!r}")
            return Result()
        pick = _pick_chats(ctx, [h for h, _ in hits], f"chats that say {q!r}")
        if pick is None:
            return Result()
        return _switch(ctx, eng / "sessions" / pick)

    if sub in ("info", "show"):
        path = _one(ctx, " ".join(rest) or here)
        if path:
            s = _info(eng, path)
            if s:
                print(ui.accent(f"  {_chat_name(s)}") + ui.muted(f"  #{s['id']}"))
                for d in _chat_detail(s):
                    print(ui.muted("  " + d))
                print(ui.muted(f"  {s['messages']} messages · made {_ago(s['made_ts'])} · "
                               f"used {_ago(s['used_ts'])} · {s['file']}"
                               + (" · pinned" if s.get("pinned") else "")
                               + (" · archived" if s.get("archived") else "")))
        return Result()

    if sub == "delete":
        if not rest:
            ui.info("usage: /resume delete <#id> — into the trash (restorable for "
                    f"{_trash_days()} days)")
            return Result()
        path = _one(ctx, " ".join(rest))
        if path is None:
            return Result()
        if path.name == here:
            ui.warn("that's the chat you're in — open another (/resume or /new), then delete it")
            return Result()
        s = _info(eng, path) or {"id": "?", "file": path.name}
        name = _chat_name(s) if "messages" in s else path.name
        if ctx.ask(f"delete '{name[:40]}' #{s['id']}? it goes to the trash "
                   f"({_trash_days()} days, /resume restore #{s['id']}) [y/N] ").strip().lower() \
                not in ("y", "yes"):
            ui.info("kept it")
            return Result()
        trash_chat(eng, path)
        ui.success(f"deleted '{name[:40]}' #{s['id']} — /resume restore #{s['id']} brings it back")
        return Result()

    if sub == "restore":
        if not rest:
            ui.info("usage: /resume restore <#id> — /resume trash lists deleted chats")
            return Result()
        back = restore_chat(eng, " ".join(rest))
        if back is None:
            ui.warn(f"no deleted chat {' '.join(rest)!r} — /resume trash lists them")
        else:
            ui.success(f"restored #{(_info(eng, back) or {}).get('id', '?')} — /resume opens it")
        return Result()

    if sub == "trash":
        if rest and rest[0].lower() == "empty":
            items = trashed(eng)
            if not items:
                ui.info("the trash is empty")
            elif ctx.ask(f"remove {len(items)} deleted chat{'s' if len(items) != 1 else ''} "
                         "for good? this can't be undone [y/N] ").strip().lower() in ("y", "yes"):
                ui.success(f"removed {empty_trash(eng)} for good")
            else:
                ui.info("kept them")
            return Result()
        items = trashed(eng)
        if not items:
            ui.info("the trash is empty")
            return Result()
        days = _trash_days()
        print(ui.accent(f"  deleted chats — kept {days} days · /resume restore <#id> · "
                        "/resume trash empty"))
        import time as _t
        for it in items[:30]:
            left = max(0, days - (_t.time() - it["deleted_ts"]) / 86400)
            print(f"    #{it['id']}  {(it['title'] or it['file'])[:44]:44s}"
                  + ui.muted(f"  deleted {_ago(it['deleted_ts'])} · {left:.0f}d left"))
        return Result()

    if sub in ("pin", "unpin", "archive", "unarchive"):
        path = _one(ctx, " ".join(rest) or here)
        if path is None:
            return Result()
        from .sessions import Session
        s = ctx.session if path.name == here else Session(path)
        s.ensure_meta()
        if sub in ("pin", "unpin"):
            s.record_flags(pinned=sub == "pin")
        else:
            s.record_flags(archived=sub == "archive")
        ui.success(f"#{s.id} " + {"pin": "pinned — at the top of /resume",
                                   "unpin": "unpinned",
                                   "archive": "archived — hidden from /resume (/resume archived shows it)",
                                   "unarchive": "back in /resume"}[sub])
        return Result()

    if sub == "rename":
        if len(rest) < 2:
            ui.info("usage: /resume rename <#id> <title> — or /rename <title> for this chat")
            return Result()
        path = _one(ctx, rest[0])
        if path is None:
            return Result()
        from .sessions import Session
        s = ctx.session if path.name == here else Session(path)
        s.ensure_meta()
        s.record_title(" ".join(rest[1:]), by="you")
        ui.success(f"#{s.id} is now '{s.title}' (its id stays)")
        return Result()

    if sub == "export":
        path = _one(ctx, rest[0]) if rest and not rest[0].lower() in ("md", "jsonl") else \
            ctx.session.path
        if path is None:
            return Result()
        fmt = next((x.lower() for x in rest if x.lower() in ("md", "jsonl")), "md")
        dest = next((x for x in rest[1:] if x.lower() not in ("md", "jsonl")), "")
        return _export_chat(ctx, path, fmt, dest)

    if sub in ("", "list", "sort", "archived"):
        chats = list_sessions(eng)
        if not chats:
            ui.info("no saved chats yet")
            return Result()
        key = rest[0] if sub == "sort" and rest else "used"
        sorts = {"used": (lambda s: s["used_ts"], True),
                 "name": (lambda s: _chat_name(s).lower(), False),
                 "full": (lambda s: s["tokens"], True),
                 "made": (lambda s: s["made_ts"], True)}
        if key not in sorts:
            ui.info("usage: /resume sort used|name|full|made")
            return Result()
        fn, rev = sorts[key]
        chats = sorted(chats, key=fn, reverse=rev)
        if sub == "archived":
            chats = [s for s in chats if s.get("archived")]
            if not chats:
                ui.info("no archived chats")
                return Result()
            title = "archived chats — /resume unarchive <#id>"
        else:
            hidden = sum(1 for s in chats if s.get("archived"))
            chats = [s for s in chats if s.get("pinned") and not s.get("archived")] + \
                    [s for s in chats if not s.get("pinned") and not s.get("archived")]
            title = (f"open a chat (sorted by {key}) — #id, delete/pin/archive <#id>, search <words>"
                     + (f" · {hidden} archived" if hidden else ""))
        pick = _pick_chats(ctx, chats, title)
        if pick is None:
            return Result()
        return _switch(ctx, eng / "sessions" / pick)

    # /resume <#id | title | N | latest>
    path = _one(ctx, " ".join(parts[1:]))
    if path is None:
        return Result()
    return _switch(ctx, path)


def _switch(ctx, target) -> "Result":
    """Open a chat: its settings come back; the chat screen shows where it
    was left."""
    from .sessions import Session
    from .last_state import resume_honest_limits
    if target.name == ctx.session.path.name:
        ui.info(f"already in this chat (#{ctx.session.id})")
        return Result()
    ctx.session = Session(target)
    ctx.session.ensure_meta()
    ctx.open_session_settings(ctx.session)
    info = _info(ctx.eng, target)
    ui.success(f"switched → {_chat_name(info) if info else target.name}  #{ctx.session.id}")
    if not ctx.extra.get("chat_screen"):
        for d in (_chat_detail(info) if info else []):
            print(ui.dim("  " + d))
    print(ui.dim("  " + resume_honest_limits()))
    ctx.extra["session_switched"] = True
    return Result()


def _trash_days() -> int:
    try:
        from .options import get
        return int(get("chats.trash_days"))
    except Exception:          # noqa: BLE001
        return 30


def _search_chats(eng, q: str) -> list:
    """Every message of every chat (not just the names), newest chat first;
    words in any order. [(chat, snippet)]."""
    from .session_store import list_sessions
    words = [w.lower() for w in q.split() if w]
    out = []
    for s in list_sessions(eng):
        p = eng / "sessions" / s["file"]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hay = " ".join((_chat_name(s), s.get("title") or "")).lower()
        body = ""
        for ln in text.splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            c = r.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            if isinstance(c, str) and r.get("role") in ("user", "assistant"):
                body += " " + c
        low = body.lower()
        if all(w in low or w in hay for w in words):
            i = low.find(words[0])
            snip = " ".join(body[max(0, i - 40):i + 80].split()) if i >= 0 else ""
            s = dict(s)
            s["tasks"] = [f"…{snip}…"] + (s.get("tasks") or []) if snip else s.get("tasks")
            out.append((s, snip))
    return out


def _export_chat(ctx, path, fmt: str, dest: str) -> "Result":
    """A chat to a file — md (readable) or jsonl (everything); keys and
    passwords scrubbed (secrets_guard)."""
    from .session_titles import export_markdown
    try:
        from .secrets_guard import scrub_secrets
    except Exception:          # noqa: BLE001
        scrub_secrets = lambda x: x          # noqa: E731
    from .sessions import Session
    s = Session(path)
    name = f"chat-{s.id}.{fmt}"
    out = Path(dest) if dest else (getattr(ctx.loop, "workspace", None) or ctx.eng) / name
    if not out.is_absolute():
        out = (getattr(ctx.loop, "workspace", None) or ctx.eng) / out
    body = export_markdown(path) if fmt == "md" else path.read_text(encoding="utf-8")
    out.write_text(scrub_secrets(body), encoding="utf-8")
    ui.success(f"exported #{s.id} → {out} (keys and passwords scrubbed)")
    return Result()


@command("/rename")
def _rename(ctx, parts, line):
    """A title for this chat — its file and its id stay."""
    s = ctx.session
    s.ensure_meta()
    if len(parts) > 1:
        s.record_title(" ".join(parts[1:]), by="you")
        ui.success(f"this chat is now '{s.title}' (#{s.id})")
    else:
        ui.info(f"this chat: '{s.title or s.path.stem}' #{s.id} — /rename <title>")


@command("/delete")
def _delete(ctx, parts, line):
    """= /resume delete (into the trash)."""
    return _sessions(ctx, ["/sessions", "delete"] + parts[1:], line)


@command("/retry")
def _retry(ctx, parts, line):
    t = ctx.session.last_user_task()
    if not t:
        ui.info("nothing to retry yet")
        return Result()
    ui.info(f"retrying: {t[:60]}")
    return Result(task=t)


@command("/export")
def _export(ctx, parts, line):
    """This chat (or /export #id) to md or jsonl, secrets scrubbed."""
    return _sessions(ctx, ["/sessions", "export"] + parts[1:], line)


@command("/kill")
def _kill(ctx, parts, line):
    (ctx.eng / ".kill").write_text("")
    ui.warn("stop flag written — the running task stops at its next step "
            "(Esc also stops it)")


@command("/steer")
def _steer(ctx, parts, line):
    msg = " ".join(parts[1:]).strip()
    if not msg:
        ui.info("usage: /steer <message> — reaches the agent after its "
                "current step (or just type while it works)")
        return Result()
    ctx.state.setdefault("steer_queue", []).append(msg)
    ui.success(f"sent to the running task: {msg[:60]}")


# ------------------------------------------------------------- model / ctx --
def _swap_profile(ctx, target: str, perm: bool, verb: str) -> None:
    from .config import resolve
    from .ctx_probe import describe_source, probe_context_length
    from .llm import LLMClient
    from .cli import apply_model
    from .profile_store import set_active
    new_profile = resolve(target)
    ctx.loop.llm = LLMClient(new_profile)
    note = apply_model(ctx.loop, new_profile, ctx.eng)
    ctx.profile = new_profile
    if perm:
        set_active(target)
        ctx.defaults_take("provider", "model", "base_url")
    c, src = probe_context_length(new_profile.base_url, new_profile.model,
                                  configured=new_profile.context_length)
    if ctx.governor is not None:
        ctx.governor.ctx = c
    ctx.loop.ctx_source = src
    ui.success(f"{verb} → {target} · {new_profile.model} "
               f"({'saved as default' if perm else 'this chat'})")
    ui.info(f"context {c:,} ({describe_source(src)}) · prompt: {note}")
    from .model_facts import hint             # Not checked yet → say so
    h = hint(new_profile.base_url, new_profile.model)
    if h:
        ui.info(h)


@command("/profile")
def _profile(ctx, parts, line):
    from .profile_store import active_name, get_profile, list_profiles
    perm = "permanent" in parts[1:]
    parts = [p for p in parts if p != "permanent"]
    if len(parts) > 1:
        if get_profile(parts[1]) is None:
            ui.warn(f"no model setup '{parts[1]}' — known: "
                    f"{', '.join(list_profiles())}")
            return Result()
        _swap_profile(ctx, parts[1], perm, "model setup")
        return Result()
    profiles = list_profiles()
    if not profiles:
        ui.info("no model setups — `aura setup` makes one")
        return Result()
    act = active_name()
    opts = [(n, n, f"{c.get('model', '?')} · {c.get('base_url', '?')}", n == act)
            for n, c in profiles.items()]
    pick = ctx.choose("use a model setup", opts)
    if pick is not None:
        _swap_profile(ctx, pick, perm, "model setup")


@command("/connect")
def _connect(ctx, parts, line):
    from .connect import list_connectable, pick_by_prefix
    perm = "permanent" in parts[1:]
    parts = [p for p in parts if p != "permanent"]
    if len(parts) > 1:
        target = pick_by_prefix(" ".join(parts[1:]))
        if target is None:
            ui.warn(f"no model setup matches '{' '.join(parts[1:])}'")
            return Result()
    else:
        rows = list_connectable()
        if not rows:
            ui.info("no model setups — `aura setup` makes one")
            return Result()
        opts = [(name, name, f"{cfg.get('model', '?')} · {status}",
                 name == getattr(ctx.profile, "name", None))
                for name, cfg, status in rows]
        target = ctx.choose("connect to", opts)
        if target is None:
            return Result()
    _swap_profile(ctx, target, perm, "connected")


@command("/model")
def _model(ctx, parts, line):
    from .ctx_probe import describe_source, find_by_testing, probe_context_length, remember_measured
    gov = ctx.governor
    p = ctx.loop.llm.p
    if len(parts) > 1 and parts[1] == "ctx":
        if len(parts) >= 3 and parts[2] == "find":
            from .machine import ok_to_run
            if not ok_to_run("finding the context size", p.base_url, say=ui.warn):
                return Result()
            found = find_by_testing(p.base_url, p.model, getattr(p, "api_key", None),
                                    say=ui.info)
            if found:
                remember_measured(p.base_url, p.model, found)
                if gov is not None:
                    gov.ctx = found
                ctx.loop.ctx_source = "measured"
                ui.success(f"context window → {found:,} (found by testing; kept "
                           "until you change it)")
            else:
                ui.warn("couldn't find it — even a small prompt failed")
            return Result()
        if len(parts) < 3 or not parts[2].replace(",", "").isdigit():
            ui.info("usage: /model ctx <tokens> · /model ctx find")
            return Result()
        n = int(parts[2].replace(",", ""))
        p.context_length = n
        if gov is not None:
            gov.ctx = n
        ctx.loop.ctx_source = "override"
        if "permanent" in parts[3:]:
            try:
                from .profile_store import set_profile
                set_profile(getattr(p, "name", "active"), activate=False,
                            context_length=n)
            except Exception:      # noqa: BLE001
                pass
        ui.success(f"context window → {n:,} ("
                   + ("saved" if "permanent" in parts[3:] else "this chat") + ")")
        return Result()
    if len(parts) > 1:
        model = " ".join(parts[1:])
    else:
        from .setup import _fetch_models
        ok, models, err, code = _fetch_models(p.base_url, p.api_key)
        if not ok or not models:
            ui.warn(f"can't list models at {p.base_url}: {err or 'none loaded'}")
            ui.info(f"current: {p.model} · /model <id> sets one directly")
            return Result()
        # What Aura MEASURED about a model (aura eval model / real runs) — the
        # old labels guessed strength from the name
        from .model_facts import model_key, tool_skill
        opts = []
        for mid in models[:60]:
            skill = tool_skill(model_key(p.base_url, mid))
            opts.append((mid, mid, {"good": "checked: good at tools",
                                    "weak": "checked: weak at tools"}.get(skill or "", ""),
                         mid == p.model))
        model = ctx.choose(f"models on {p.base_url}", opts)
        if model is None or model == p.model:
            return Result()
    old = p.model
    p.model = model
    c, src = probe_context_length(p.base_url, p.model,
                                  configured=getattr(p, "context_length", None))
    if gov is not None:
        gov.ctx = c
    ctx.loop.ctx_source = src
    ui.success(f"model → {model} (was {old})")
    ui.info(f"context {c:,} ({describe_source(src)})")
    # its own prompt follows the new model (it kept the old model's copy)
    from .cli import apply_model
    ui.info("prompt: " + apply_model(ctx.loop, p, ctx.eng))
    _offer_model_prompt(model, p.base_url)


def _offer_model_prompt(model: str, base_url: str = "") -> None:
    """After a model switch: say once that the model maker's own settings
    can be used (sampling.py; the prompt is Aura's for every model)."""
    try:
        from . import model_facts as MF
        from . import sampling as SP
        if base_url and not SP.choice(MF.model_key(base_url, model)):
            ui.info(f"{model}: its maker's own settings (temperature …) can be "
                    "used instead of Aura's — aura model settings")
    except Exception:                                    # noqa: BLE001
        pass


@command("/prompt")
def _prompt(ctx, parts, line):
    """."""
    from . import prompt_profiles as PP
    from .cli import apply_model
    from .engine.loop import DEFAULT_SYSTEM
    p = ctx.loop.llm.p
    sub = parts[1] if len(parts) > 1 else "show"
    core = getattr(ctx.loop, "core_prompt", None) or DEFAULT_SYSTEM
    if sub == "tune":
        ui.info("there's no prompt tuning: every model uses Aura's prompt "
                "(changing it made answers worse). The model maker's own "
                "settings can be used instead — aura model settings")
    elif sub == "reset":
        gone = PP.remove_copy(p.model)
        ui.success("prompt: " + apply_model(ctx.loop, p, ctx.eng) + ("" if gone else " (there was no old copy)"))
    else:
        _text, src = PP.prompt_for(p.model, core)
        ui.info(f"{p.model} · prompt: {src}")


@command("/context")
def _context(ctx, parts, line):
    ctx.show_context()


@command("/compact")
def _compact(ctx, parts, line):
    focus = " ".join(parts[1:])
    r = ctx.loop.compact_now(ctx.session, focus=focus)
    ui.success(f"compacted: {r['dropped']} turns summarized"
               + (f", {r['pruned']} tool outputs trimmed" if r.get("pruned") else "")
               + (f" · focus: {focus}" if focus else ""))
    ctx.show_context()


@command("/prune")
def _prune(ctx, parts, line):
    gov = ctx.governor
    if not gov:
        ui.info("context isn't tracked here — nothing to trim")
        return Result()
    # A trim batch now — every old output not in use; sticky like the automatic
    # ones, each stub says how to reopen it
    from .engine.workset import WorkSet
    if getattr(ctx.loop, "_workset", None) is None:
        ctx.loop._workset = WorkSet()
    _, rep = ctx.loop._workset.apply(list(ctx.session.messages), gov.count, 1,
                                     store=ctx.loop.root)
    ui.success(f"trimmed {rep['trimmed']} old outputs (each can be reopened; "
               "/context lists them)")
    ctx.show_context()


@command("/budget")
def _budget(ctx, parts, line):
    from .config import (PRESET_TABLE, SETTINGS_KEYS, load_saved_settings,
                         resolve_settings, save_saved_settings)
    loop = ctx.loop
    args = parts[1:]
    if not args:
        s = loop.settings
        ui.info(f"preset: {s.preset}")
        ui.info(f"  tool output kept in chat : {s.tool_output:,} chars")
        ui.info(f"  trim / summarize at      : {s.prune_pct:.0%} / {s.compact_pct:.0%}")
        ui.info(f"  web depth                : {s.web_depth or 'no limit'}")
        tc = getattr(s, "token_ceiling", None)
        ui.info(f"  token warning at         : {f'{tc:,}' if tc else 'none'}"
                f" · spent {loop.m.budgets.tokens_spent:,}")
        ui.info("change: /budget preset <name> · /budget set <key> <value> [permanent]")
        return Result()
    if args[0] == "preset" and len(args) >= 2:
        name = args[1].lower()
        if name not in PRESET_TABLE:
            ui.info(f"unknown preset '{name}' — one of: {', '.join(PRESET_TABLE)}")
            return Result()
        perm = "permanent" in args[2:]
        loop.settings = resolve_settings(name)
        loop.governor.threshold = loop.settings.compact_pct
        loop.governor.prune_pct = loop.settings.prune_pct
        loop.cfg.max_tool_output = loop.settings.tool_output
        if perm:
            save_saved_settings({**load_saved_settings(), "preset": name})
            ctx.defaults_take("budget")
        ui.success(f"preset → {name} ({'saved' if perm else 'this chat'})")
        return Result()
    if args[0] == "set" and len(args) >= 3:
        key, val = args[1], args[2]
        perm = "permanent" in args[3:]
        if key not in SETTINGS_KEYS:
            ui.info(f"unknown key '{key}' — keys: {', '.join(SETTINGS_KEYS)}")
            return Result()
        try:
            v = (float if key.endswith("pct") else int)(val)
        except ValueError:
            ui.warn(f"'{val}' isn't a number")
            return Result()
        if key == "tool_output":
            loop.cfg.max_tool_output = v
        elif key == "prune_pct":
            loop.governor.prune_pct = v
        elif key == "compact_pct":
            loop.governor.threshold = v
        elif key == "token_ceiling":
            v = v if v > 0 else None
            loop.m.budgets.token_ceiling = v
        setattr(loop.settings, key, v)
        if perm:
            saved = load_saved_settings()
            saved[key] = v
            save_saved_settings(saved)
            ctx.defaults_take("budget")
        ui.success(f"{key} → {v} ({'saved' if perm else 'this chat'})")
        return Result()
    ui.info("usage: /budget [preset <name>] [set <key> <value> [permanent]]")


@command("/features")
def _features(ctx, parts, line):
    from .features import command as feat
    _say_list(feat(parts, session=True, root=ctx.eng))
    if "permanent" in parts:
        ctx.defaults_take("features")


@command("/proof")
def _proof(ctx, parts, line):
    """Before the final answer, Aura re-reads what the answer rests on and runs
    the project's tests (one turn to fix).
    """
    from . import features as F
    arg = parts[1].lower() if len(parts) > 1 else ""
    keep = "permanent" in [p.lower() for p in parts[2:]]
    if arg in ("on", "off"):
        ui.success("proof check " + F.set_feature("proof", arg == "on", permanent=keep)
                   .split(":", 1)[1].strip())
        if keep:
            ctx.defaults_take("features")
        return Result()
    on = F.enabled("proof")
    ui.info(f"proof check: {'on' if on else 'off'} — before the final answer Aura re-reads "
            "what the answer rests on and runs the project's tests (one turn to fix) · "
            "/proof on|off [permanent]")


@command("/plan")
def _plan(ctx, parts, line):
    """The plan switch: a step-by-step plan for THIS chat, only when you want one."""
    from . import features as F
    arg = parts[1].lower() if len(parts) > 1 else ""
    keep = "permanent" in [p.lower() for p in parts[2:]]
    if arg in ("on", "off"):
        ui.success("plan " + F.set_feature("plan", arg == "on", permanent=keep)
                   .split(":", 1)[1].strip())
        if keep:
            ctx.defaults_take("features")
        return Result()
    on = F.enabled("plan")
    ui.info(f"plan: {'on' if on else 'off'} — when on, Aura keeps a step-by-step plan for "
            "this chat and shows it in the sidebar; a new chat starts without one · "
            "/plan on|off [permanent] · /todos shows it")


@command("/approvals")
def _approvals(ctx, parts, line):
    from .approvals_suggest import command as appr
    _say_list(appr(ctx.eng, parts))


@command("/assist")
def _assist(ctx, parts, line):
    if len(parts) > 1 and parts[1].upper() in ("L1", "L2", "L3"):
        ctx.m.assist = parts[1].upper()
    elif len(parts) > 1:
        ui.warn("asking level is L1, L2 or L3 — bare /assist shows a menu")
        return Result()
    else:
        cur = getattr(ctx.m, "assist", "L2")
        pick = ctx.choose("when should Aura ask you first?", [
            ("L1 · every change", "L1", "asks before every file change or command", cur == "L1"),
            ("L2 · risky actions", "L2", "asks before anything that can't be undone or reaches outside", cur == "L2"),
            ("L3 · never", "L3", "never asks — fully autonomous", cur == "L3"),
        ])
        if pick is None:
            return Result()
        ctx.m.assist = pick
    ui.success(f"asks {_assist_words(ctx.m.assist)} ({ctx.m.assist})")


@command("/discuss")
def _discuss(ctx, parts, line):
    """On: it may read and search (files, the web, past chats) to understand,
    never change anything; off: back to the mode before.
    """
    arg = parts[1].lower() if len(parts) > 1 else ""
    cur = getattr(ctx.m, "mode", "act")
    if arg not in ("on", "off"):
        arg = "off" if cur == "discuss" else "on"        # Bare discuss switches
    if arg == "on":
        if cur != "discuss":
            ctx.m._mode_before_discuss = cur
        ctx.m.mode = "discuss"
        ui.success("discuss mode on — Aura talks it through and may read or search to "
                   "understand, but changes nothing and doesn't start the work (/discuss off)")
    else:
        ctx.m.mode = getattr(ctx.m, "_mode_before_discuss", "act") if cur == "discuss" else cur
        if ctx.m.mode == "discuss":
            ctx.m.mode = "act"
        ui.success(f"discuss mode off — back to {ctx.m.mode}")


@command("/mode")
def _mode(ctx, parts, line):
    new = (parts[1] if len(parts) > 1 else "").lower()
    if new not in ("act", "plan"):
        if len(parts) > 1:
            ui.warn("mode is act or plan — bare /mode shows a menu")
            return Result()
        cur = getattr(ctx.m, "mode", "act")
        new = ctx.choose("mode", [
            ("act", "act", "build — full tools", cur == "act"),
            ("plan", "plan", "plan — read and research only, no changes", cur == "plan"),
        ])
        if new is None:
            return Result()
    ctx.m.mode = new
    ui.success("mode → " + ("act (full tools)" if new == "act" else
                            "plan (read and research only)"))


@command("/theme")
def _theme(ctx, parts, line):
    from .themes import apply, available, current
    name = parts[1].lower() if len(parts) > 1 else ""
    hook = ctx.extra.get("set_theme")
    names = ctx.extra.get("themes", available)()
    cur = ctx.extra.get("current_theme", current)()
    if not name:
        opts = [(n, n, "yours" if n not in ("dark", "light", "colorblind",
                                            "high-contrast", "mono") else "",
                 n == cur) for n in names]
        name = ctx.choose("theme (saved for every chat)", opts)
        if name is None:
            return Result()
    ok = hook(name) if hook else apply(name)
    if ok:
        ui.success(f"theme → {name}")
    else:
        ui.warn(f"unknown theme '{name}' — {', '.join(names)}")


@command("/debug")
def _debug(ctx, parts, line):
    ctx.state["debug_raw"] = not ctx.state.get("debug_raw", False)
    ui.info("full tool output ON" if ctx.state["debug_raw"] else
            "full tool output OFF")


# ----------------------------------------------------------------- project --
@command("/scope")
def _scope(ctx, parts, line):
    from .engagement import load_scope, scope_add, scope_lines, scope_remove
    from .mediator import Scope
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("add", "remove", "rm") and len(parts) > 2:
        try:
            msg = (scope_add if sub == "add" else scope_remove)(ctx.eng, parts[2])
        except ValueError as e:
            ui.warn(str(e))
            return Result()
        ctx.m.scope = Scope.from_dict(load_scope(ctx.eng))
        ctx.loop.intent_scope = load_scope(ctx.eng)
        ui.success(msg)
        return Result()
    _say_list(scope_lines(ctx.eng))
    _say_list(["/scope add <example.com | IP | 10.0.0.0/24> · /scope remove <item>"])


@command("/web")
def _web(ctx, parts, line):
    from .engagement import load_scope
    from .mediator import Scope
    from .webcmd import web_command
    _say_list(web_command(ctx.eng, parts[1:]))
    ctx.m.scope = Scope.from_dict(load_scope(ctx.eng))
    ctx.loop.intent_scope = load_scope(ctx.eng)


@command("/research")
def _research(ctx, parts, line):
    words = {"basic": "basic — only the listed sources (your keyed services, Wikipedia, "
                      "arXiv, PubMed, Stack Overflow, GitHub)",
             "normal": "normal — anything on the web, kept small: a few searches, "
                       "the top pages read",
             "deep": "deep — a plan first, two sources per question, until the "
                     "scope is exhausted"}
    arg = parts[1].lower() if len(parts) > 1 else ""
    arg = {"on": "deep", "off": "normal"}.get(arg, arg)
    if arg in words:
        ctx.loop.research_mode = arg
        keep = "permanent" in parts[2:]
        if keep:
            from .options import set_
            set_("research.mode", arg)
        ui.success("research: " + words[arg]
                   + (" (saved as the default)" if keep else " (this chat)"))
    else:
        from .options import get
        cur = getattr(ctx.loop, "research_mode", None) or get("research.mode")
        ui.info(f"research: {words.get(cur, cur)} · /research basic|normal|deep [permanent]")


@command("/settings", "/config")
def _settings(ctx, parts, line):
    from .options_cmd import settings_command
    _say_list(settings_command(parts[1:]))


@command("/add-dir")
def _add_dir(ctx, parts, line):
    """Folders outside the project the file tools may use (outside.py):
    this chat, or saved for the project with `permanent`."""
    from . import outside as O
    words = line.split(None, 1)[1].strip() if len(parts) > 1 else ""
    keep = words.endswith(" permanent")
    if keep:
        words = words[: -len(" permanent")].strip()
    gone = words.startswith("remove ")
    if gone:
        words = words[len("remove "):].strip()
    if not words:
        saved = {str(d) for d in O.saved(ctx.eng)}
        dirs = sorted(str(d) for d in ctx.m.outside_ok)
        if not dirs:
            ui.info("only the project folder — /add-dir <folder> [permanent] allows "
                    "another (Aura also asks the first time it needs one)")
            return Result()
        for d in dirs:
            print(f"  {d}" + ui.muted("  (saved)" if d in saved else "  (this chat)"))
        ui.info("/add-dir remove <folder>")
        return Result()
    folder = Path(words.strip('"').strip("'")).expanduser().resolve()
    if gone:
        ctx.m.outside_ok.discard(folder)
        was = O.remove(ctx.eng, folder)
        ui.success(f"removed {folder}" + (" (was saved)" if was else ""))
        return Result()
    why = O.addable(folder)
    if why:
        ui.warn(f"{folder}: {why}")
        return Result()
    ctx.m.outside_ok.add(folder)
    if keep:
        O.save(ctx.eng, folder)
    ui.success(f"Aura may use {folder} " + ("(saved for this project)" if keep else
                                             "(this chat · add `permanent` to keep it)"))


@command("/rules")
def _rules(ctx, parts, line):
    from . import constraints as c
    if len(parts) > 1 and parts[1] == "add":
        text = " ".join(parts[2:]).strip()
        ui.success(c.add_rule(ctx.eng, text)) if text else \
            ui.info("usage: /rules add <rule>")
    elif len(parts) > 1 and parts[1] in ("rm", "remove"):
        ui.success(c.remove_rule(ctx.eng, " ".join(parts[2:])))
    else:
        rules = c._read_rules(ctx.eng)
        if rules:
            for i, r in enumerate(rules, 1):
                print(f"  {i}. {r}")
            ui.info("/rules add <text> · /rules rm <n>")
        else:
            ui.info("no rules yet — e.g. /rules add never edit config/")


@command("/team")
def _team(ctx, parts, line):
    """Aura Teams in the chat: every `aura teams …` command — the hub, connect,
    the board, link, start, done, decision, read, queue, sync, the chat page —
    run here, the same code. Questions come as cards; the hub token is typed
    hidden. In fullscreen `/team board` opens the board beside the chat.
    """
    from .editions import teams_present
    if not teams_present():
        ui.info("Aura Teams isn't part of this version of Aura.")
        return Result()
    from .cli import build_parser, cmd_teams
    from . import teams_ui
    rest = parts[1:]
    if rest[:2] == ["chat", "open"] or rest == ["chat"]:
        from . import teams_chat as CH
        ui.info(CH.open_for_me(say=ui.info))
        return Result()
    if rest[:2] == ["wire", "fix"]:
        from . import teams_wiring as W
        bad = W.broken()
        what = ", ".join(bad) if bad else "any operation that fails"
        return Result(shown="/teams wire fix — repair the hub's wiring",
                      task=(f"The team hub's wiring needs fixing ({what} stopped working — the "
                            "app may have changed). Look at team_status, research the app's "
                            "current API for those operations (official docs first), fix them in "
                            "the wiring in use, prove it with team_wire_check, show me what "
                            "changes, and use it only after I say yes."))
    if rest and rest[0] == "wire":
        from . import teams
        url = teams.load_config().get("hub_url")
        if not url:
            ui.info("first the hub's address — just tell Aura, e.g. 'connect to our hub at …'")
            return Result()
        return Result(shown=f"/teams wire — connect the hub at {url}",
                      task=(f"Wire our team hub at {url} for Aura Teams: research which app it "
                            "is and its API or MCP server (official docs first), write a wiring "
                            "(team_wiring_format), prove it with team_wire_check, and tell me "
                            "the result before team_wire_use."))
    if rest and rest[0] == "wiring":
        from . import teams_wiring as W
        try:
            w = W.load_in_use()
        except W.WiringError as e:
            ui.info(f"no wiring in use ({e}) — Vikunja needs none; another app: /team wire")
            return Result()
        print(ui.accent(f"  {w.get('hub', '?')} at {w['base']}") + ui.muted(
            f"  · made by {w.get('made_by', '?')} · from {w.get('from', '?')}"))
        for op, spec in w["ops"].items():
            how = spec.get("http") or ("mcp " + spec["mcp"]["tool"] if "mcp" in spec else "constant")
            print(ui.muted(f"    {op:<12} {how}"))
        return Result()
    if not rest:
        rest = ["status"]
    if rest[0] == "board" and len(rest) == 1 and ctx.extra.get("team_panel") \
            and ctx.extra["team_panel"]():
        return Result()
    try:
        args = build_parser().parse_args(["teams"] + rest)
    except SystemExit:
        ui.info("usage: /team status · connect <hub address> [--user NAME] · board [--all] · "
                "link <request> · start · done · decision <text> · read <#task> · queue · sync · "
                "hub setup|start|stop|status · chat on|off|status")
        return Result()
    import builtins
    import getpass
    keep = (builtins.input, getpass.getpass)
    if ctx.extra.get("chat_screen"):
        # the command's questions as cards; a token typed hidden
        builtins.input = lambda prompt="": ctx.ask(str(prompt))
        secret = ctx.extra.get("ask_secret")
        getpass.getpass = (lambda prompt="Password: ", stream=None: secret(str(prompt))) \
            if secret else keep[1]
    try:
        cmd_teams(args)
    except Exception as e:          # noqa: BLE001 - a hub problem never breaks the chat
        ui.error(f"Teams: {e}")
    finally:
        builtins.input, getpass.getpass = keep
        teams_ui.refresh_now()
    return Result()


@command("/decision")
def _decision(ctx, parts, line):
    # Aura Teams: a decision on this chat's board task — a comment + the
    # `decision` label, through the scrubber
    from . import teams_run as TR
    text = " ".join(parts[1:]).strip()
    if not TR.active():
        ui.info("not connected to a team hub — `aura teams connect`")
    elif not text:
        ui.info("usage: /decision <what was decided>")
    else:
        try:
            tr = TR.TeamsRun(Path(ctx.session.path).stem, None, ui.info,
                             getattr(ctx.loop, "work_dir", None) or ctx.eng)
            ui.success(tr.decision(text))
        except Exception as e:           # noqa: BLE001
            ui.error(f"Teams: {e}")


@command("/todos")
def _todos(ctx, parts, line):
    from .todo import load_items, plan_on, render_checklist
    if not plan_on():
        print(ui.muted("  the plan is off for this chat — /plan on keeps one"))
        return
    items = load_items(ctx.eng, ctx.session.path.stem)
    print(render_checklist(items) if items else ui.muted("  no plan yet"))


@command("/memory")
def _memory(ctx, parts, line):
    from .memory_store import read_memory
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("forget", "rm", "remove"):
        what = " ".join(parts[2:]).strip()
        if not what:
            ui.info("usage: /memory forget <words of the fact to remove>")
            return Result()
        # The engine's forget: one entry; a vague match is refused with the
        # candidates, so nothing extra is deleted by accident
        from .memory_store import forget
        out = forget(ctx.eng, what)
        (ui.success if out.startswith("forgotten") else ui.info)(out)
        return Result()
    text = read_memory(ctx.eng)
    if text:
        print(ui.muted(text))
        ui.info("/memory forget <words> removes a fact")
    else:
        ui.info("nothing remembered yet — the agent saves facts as it learns")


@command("/skills")
def _skills(ctx, parts, line):
    from .skills import load_skills
    skills = load_skills(ctx.eng)
    if skills:
        for name, s in sorted(skills.items()):
            print(f"  {name:24s} [{s['source']}] {s['description'][:60]}")
    else:
        ui.info("no skills yet — the agent can save one (it asks you first)")


@command("/agents")
def _agents(ctx, parts, line):
    from .agents_md import load_agents_md
    found = load_agents_md(getattr(ctx.loop, "workspace", None) or ctx.eng)
    if not found:
        ui.info("no AGENTS.md / CLAUDE.md in this project — standing "
                "instructions go there")
        return Result()
    name, content = found
    lines = content.strip().splitlines()
    ui.success(f"{name}: {len(lines)} lines, read every turn")
    for ln in lines[:8]:
        print(ui.dim(f"    {ln[:90]}"))
    if len(lines) > 8:
        print(ui.dim("    …"))


@command("/tools")
def _tools(ctx, parts, line):
    """Tool groups: which are sent to the model, and switching them
    (toolsets.tools_command — the merge had left a flat list here)."""
    from .toolsets import tools_command
    tl = getattr(ctx.loop, "tool_list", None)
    if tl is None:                          # No tool list (helpers, tests)
        for name, spec in sorted(ctx.m.tools.items()):
            print(f"  {spec.blast_radius.value:7s} {name}")
        return Result()
    _say_list(tools_command(tl, parts[1:]))


@command("/findings")
def _findings(ctx, parts, line):
    from .finding_card import colorize_card, finding_card, findings_snapshot
    rows = findings_snapshot(ctx.eng)
    if len(parts) > 1:
        want = parts[1]
        rec = next((r for r in rows if r["id"] == want or r["id"].endswith(want)), None)
        if rec:
            print(colorize_card(finding_card(rec)))
        else:
            ui.warn(f"no result {want}")
        return Result()
    if not rows:
        ui.info("no recorded results yet")
        return Result()
    for r in rows:
        mark = ui.ok("✓ proven") if r.get("proof") else ui.warn("○ not proven")
        print(f"  {mark} {r['id']} {r.get('title', '')[:56]}")
    ui.info(f"{len(rows)} result(s) · {sum(1 for r in rows if r.get('proof'))} "
            "proven · /results <id> for one")


@command("/receipt")
def _receipt(ctx, parts, line):
    from .receipts import latest_receipt, render_receipt_card
    if len(parts) > 1 and parts[1] == "verify":
        from .receipts import render_verify, verify_chain
        print(ui.muted(render_verify(verify_chain(ctx.eng))))
        return Result()
    rid = parts[1] if len(parts) > 1 else latest_receipt(ctx.eng)
    if rid is None:
        ui.info("no receipts yet — every run writes one")
    else:
        print(ui.muted(render_receipt_card(ctx.eng, rid)))


@command("/insights")
def _insights(ctx, parts, line):
    from .chat_extras import insights_text
    print(ui.muted(insights_text(ctx.eng)))


@command("/usage")
def _usage(ctx, parts, line):
    from .chat_extras import usage_line
    gov = ctx.governor
    n = gov.count(ctx.session.messages) if gov else 0
    print(usage_line(getattr(ctx.loop, "last_prompt_tokens", 0) or 0,
                     getattr(ctx.loop, "last_completion_tokens", 0) or 0,
                     gov.ctx if gov else 0, n, bool(gov and gov.measured)))


@command("/audit")
def _audit(ctx, parts, line):
    from .audit_chain import chain_view
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
    print(chain_view(ctx.eng, limit=max(5, min(200, n))))


@command("/search")
def _search(ctx, parts, line):
    # Past chats with how each one ended; typed filters project:all after:7d
    # before:… file:… outcome:proven
    from .history import parse_inline
    from .history import search as history_search
    if len(parts) < 2:
        ui.info("usage: /search <words> [project:all] [after:7d] [file:app.py] "
                "[outcome:proven] — past chats with how they ended")
        return Result()
    print(history_search(parse_inline(" ".join(parts[1:])), ctx.eng))


@command("/reindex")
def _reindex(ctx, parts, line):
    from .history import rebuild, update
    rebuild()
    ui.success(f"past-chat index rebuilt: {update(ctx.eng)} chats read")


@command("/diff")
def _diff(ctx, parts, line):
    from .shadow_ui import diff_snapshot, list_snapshots
    snaps = list_snapshots(ctx.eng)
    if not snaps:
        ui.info("no file changes recorded yet")
    elif len(parts) > 1:
        print(diff_snapshot(ctx.eng, parts[1]))
    else:
        for s in snaps:
            print(f"  {s['sha']}  {s['file']}")
        ui.info("/diff <id> shows one · /undo reverts")


@command("/undo")
def _undo(ctx, parts, line):
    from .shadow_ui import undo
    ui.success(undo(ctx.eng, parts[1] if len(parts) > 1 else None))


@command("/checkpoint")
def _checkpoint(ctx, parts, line):
    from .checkpoints import create
    ws = getattr(ctx.loop, "workspace", None) or ctx.eng
    try:
        rec = create(ws, label=parts[1] if len(parts) > 1 else "", store=ctx.eng)
    except Exception as e:     # noqa: BLE001
        ui.warn(f"checkpoint failed: {e}")
        return Result()
    if rec.get("skipped"):
        ui.warn(f"checkpoint skipped: {rec['skipped']}")
    else:
        ui.success(f"checkpoint {rec['id']} saved ({rec.get('bytes', 0):,} bytes)")


@command("/restore")
def _restore(ctx, parts, line):
    from .checkpoints import list_checkpoints, restore
    ws = getattr(ctx.loop, "workspace", None) or ctx.eng
    cids = list_checkpoints(ws, ctx.eng)
    if not cids:
        ui.warn("no checkpoints yet — /checkpoint makes one")
        return Result()
    target = parts[1] if len(parts) > 1 else None
    if target is None:
        opts = [(c["id"], c["id"], c.get("label", "") or c.get("ts", ""), False)
                for c in reversed(cids[-15:])]
        target = ctx.choose("restore which checkpoint?", opts)
        if target is None:
            return Result()
    if target != "latest" and not any(c.get("id") == target for c in cids):
        ui.warn(f"unknown checkpoint '{target}'")
        return Result()
    if ctx.ask(f"restore {target} over {ws}? files change [y/N] ").strip().lower() not in ("y", "yes"):
        ui.info("not restored")
        return Result()
    print(ui.dim(restore(ws, None if target == "latest" else target, store=ctx.eng)))


@command("/fork")
def _fork(ctx, parts, line):
    s = ctx.session
    try:
        n = int(parts[1]) if len(parts) > 1 else s.seq
    except ValueError:
        n = s.seq
    s.ensure_meta()
    branch = s.fork(n, label=f"fork-{n}")
    ui.success(f"forked at step {n} → #{branch.id} (from #{s.id}) — /resume #{branch.id} opens it")


@command("/rewind")
def _rewind(ctx, parts, line):
    from .last_state import rewind_honest_limits
    if len(parts) < 2 or not parts[1].isdigit():
        ui.info(f"/rewind <step> — this chat is at step {ctx.session.seq}")
        ui.info(rewind_honest_limits())
        return Result()
    snap = ctx.session.restore(int(parts[1]))
    ui.success(f"rewound to step {parts[1]} — the rest is kept in {Path(snap).name}")


@command("/feedback")
def _feedback(ctx, parts, line):
    import os
    text = " ".join(parts[1:]).strip()
    if not text:
        ui.info("usage: /feedback <what happened or what to change>")
        return Result()
    ok = ctx.ask("share this with the Aura project? (version, model setup and "
                 "OS only — none of your project) [Y/n] ").strip().lower()
    from .feedback import feedback_flow
    r = feedback_flow(text, ctx.eng, ctx.version, getattr(ctx.profile, "name", "?"),
                      os.environ.get("OS", "Linux"), consent=ok not in ("n", "no"))
    (ui.success if r["ok"] else ui.info)(f"feedback: {r['where']}")
    if r.get("note"):
        ui.info(r["note"])


@command("/powerup")
def _powerup(ctx, parts, line):
    ui.info("tip: " + random.choice([
        "type while the agent works — it reads your message at its next step",
        "Esc stops the agent; your draft stays",
        "@ then a few letters adds a file to your message",
        "Shift+Tab switches how often Aura asks before acting",
        "Ctrl+R searches everything you typed before",
        "/undo reverts the agent's last file change",
        "/model ctx find measures the real context size of your model",
        "Ctrl+O shows full tool output and diffs",
    ]))


def _custom_or_unknown(ctx, cmd: str, line: str) -> Result:
    try:
        from .custom_commands import discover, expand
        cc = discover(ctx.eng).get(cmd.lstrip("/"))
    except Exception:          # noqa: BLE001
        cc = None
    if cc is not None:
        ui.info(f"/{cc.name} → task")
        return Result(task=expand(cc, line[len(cmd):].strip()), shown=line)
    from .command_registry import COMMANDS
    import difflib
    near = difflib.get_close_matches(cmd, [c.name for c in COMMANDS], n=1)
    ui.warn(f"unknown command {cmd}" + (f" — did you mean {near[0]}?" if near
                                        else " — /help lists them"))
    return Result()



@command("/mcp")
def _mcp(ctx, parts, line):
    """MCP servers in chat (the engine's `aura mcp`): list · popular ·
    search <words> · add <name> (shows what it runs, asks) · remove <name>."""
    from . import mcp_registry as R
    sub = parts[1].lower() if len(parts) > 1 else "list"
    path = R.config_file(ctx.eng, False)
    if sub == "list":
        servers = R.listing(path)
        if not servers:
            ui.info("no MCP servers yet — /mcp popular shows well-known ones")
        for n, sp in servers.items():
            print(f"  {n}: {sp.get('url') or ' '.join([sp.get('command', '')] + sp.get('args', []))}")
        return Result()
    if sub == "popular":
        for short, pz in R.POPULAR.items():
            print(f"  {short:16}{pz['what']}")
            print(ui.dim(f"  {'':16}by {pz['by']} · needs: {pz['account']}"))
        ui.info("/mcp add <name> — it shows what it runs and asks first")
        return Result()
    if sub == "search":
        try:
            hits = R.search(" ".join(parts[2:]), limit=8)
        except Exception as e:                           # noqa: BLE001
            ui.warn(f"couldn't reach the MCP registry: {e}")
            return Result()
        for h in hits:
            print(f"  {h['name']}  — {h['description'][:70]}")
        if not hits:
            ui.info("nothing found — other words, or /mcp popular")
        return Result()
    if sub == "remove" and len(parts) > 2:
        ok = R.remove(parts[2], path)
        (ui.success if ok else ui.warn)(f"removed {parts[2]}" if ok else f"no server {parts[2]!r}")
        return Result()
    if sub == "add" and len(parts) > 2:
        name = parts[2]
        try:
            h = R.exact(name)
        except Exception as e:                           # noqa: BLE001
            ui.warn(f"couldn't reach the MCP registry: {e}")
            return Result()
        if h is None or not h["spec"]:
            ui.warn(f"no server named exactly {name!r} — /mcp popular, or /mcp search <words> "
                    "(check the publisher: lookalikes exist)")
            return Result()
        sp = h["spec"]
        runs = sp.get("url") or " ".join([sp["command"]] + sp.get("args", []))
        print(f"  {h['name']} {h['version']}\n  {h['description'][:120]}\n  it runs: {runs}")
        if h["needs"]:
            print(ui.dim(f"  it needs: {', '.join(h['needs'])} — `aura keys set NAME` in a terminal"))
        print(ui.dim("  this downloads and runs someone else's code each time Aura starts"))
        if ctx.ask("add it? [y/N] ").strip().lower() not in ("y", "yes"):
            ui.info("not added")
            return Result()
        local = name.lower() if name.lower() in R.POPULAR else R.short_name(h["name"])
        R.add(local, sp, path)
        ui.success(f"added '{local}' — it starts with the next chat")
        return Result()
    if sub in ("login", "logout") and len(parts) > 2:
        name = parts[2]
        servers = {**R.listing(R.config_file(ctx.eng, True)), **R.listing(path)}
        sp = servers.get(name)
        if not sp or not sp.get("url"):
            ui.warn(f"no remote server {name!r} — /mcp lists them (only remote ones sign in)")
            return Result()
        from . import mcp_oauth as O
        if sub == "logout":
            O.logout(sp["url"])
            ui.success(f"signed out of {name}")
            return Result()
        paste = "paste" in [p.lower() for p in parts[3:]]
        ui.info(f"signing in to {name} — finish it in the browser that opens")
        try:
            O.login(sp["url"], wait=5 if paste else 300, say=lambda t: ui.info(str(t)),
                    paste=(lambda q="": ctx.ask(q or "the address the browser ended on: ")) if paste else None)
        except O.OAuthError as e:
            ui.warn(f"sign-in failed: {e}"
                    + ("" if paste else f" — on SSH or in a container: /mcp login {name} paste"))
            return Result()
        ui.success(f"signed in to {name} — the token is kept in your key store and renewed as needed")
        return Result()
    ui.info("/mcp · /mcp popular · /mcp search <words> · /mcp add <name> · /mcp remove <name> · "
            "/mcp login|logout <name>")
    return Result()


@command("/tui")
def _tui(ctx, parts, line):
    """The new chat screen handles /tui itself (tui/app.py); this is the
    classic, line-by-line screen: the choice is saved for the next start."""
    arg = parts[1].lower() if len(parts) > 1 else ""
    from .options import get, set_
    if arg in ("fullscreen", "inline"):
        set_("look.screen", arg)
        ui.success(f"saved: the chat screen starts {arg} next time (`aura chat` in a terminal)")
    else:
        ui.info(f"chat screen: {get('look.screen')} — this is the classic screen; "
                "/tui fullscreen|inline saves the choice for the next start")


@command("/display")
def _display(ctx, parts, line):
    """The chat screen handles /display itself; on the classic screen the
    choice is saved for it."""
    from .options import get, set_
    arg = [p.lower() for p in parts[1:]]
    if arg[:1] == ["thinking"] and arg[1:2] in (["on"], ["off"]):
        set_("look.show_thinking", arg[1] == "on")
        ui.success(f"thinking {arg[1]} — saved")
        return Result()
    if arg[:1] and arg[0] in ("simple", "detailed", "everything"):
        set_("look.display", arg[0])
        ui.success(f"display: {arg[0]} — saved for the chat screen")
        return Result()
    ui.info(f"display: {get('look.display')} · usage: /display simple|detailed|everything · "
            "/display thinking on|off")
    return Result()


@command("/focus")
def _focus(ctx, parts, line):
    """The chat screen handles /focus itself; on the classic screen the
    choice is saved for it."""
    from .options import get, set_
    new = "off" if get("look.focus") == "on" else "on"
    set_("look.focus", new)
    ui.success(f"focus view {new} — saved for the chat screen (`aura chat` in a terminal)")


@command("/findaway")
def _findaway(ctx, parts, line):
    """find_way.py: the toggle."""
    from .options import get, set_
    cur = bool(get("assist.find_a_way"))
    want = {"on": True, "off": False}.get(parts[1].lower(), None) if len(parts) > 1 else (not cur)
    if want is None:
        ui.info("usage: /findaway on|off")
        return Result()
    set_("assist.find_a_way", want)
    ui.success("find a way: on — when something is missing, Aura offers a way and asks first"
               if want else "find a way: off — Aura says what is missing and what you can do")
    return Result()


@command("/machine")
def _machine(ctx, parts, line):
    """machine.py: the memory watch while Aura works; the check after each task is
    always on. Bare: the machine and what Aura holds.
    """
    from .options import get, set_
    from . import machine as M
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg in ("on", "off"):
        set_("machine.watch", arg == "on")
        ui.success("machine watch: on — while Aura works, low memory is told to the model once "
                   "(no extra model call)" if arg == "on" else
                   "machine watch: off — only the check after each task")
        return Result()
    if arg:
        ui.info("usage: /machine · /machine on|off")
        return Result()
    prof = M.profile()
    mem = M.memory()
    progs = M.own_programs()
    print(ui.muted(f"  this machine: {prof.get('ram_gb', '?')} GB memory, {prof.get('cpus', '?')} CPUs"
                   + (f" · {mem[0] / 1024 ** 3:.1f} GB free now" if mem else "")))
    print(ui.muted(f"  Aura holds {sum(p['bytes'] for p in progs) / 1024 ** 3:.1f} GB"
                   + "".join(f"\n    {p['name']}  {p['bytes'] / 1024 ** 2:,.0f} MB"
                             for p in sorted(progs, key=lambda p: -p["bytes"])[:8])))
    print(ui.muted("  watch while working: " + ("on" if get("machine.watch") else "off")
                   + " · /machine on|off"))
    return Result()


@command("/think")
def _think(ctx, parts, line):
    """How hard a reasoning model thinks (thinking.py): this chat; with
    `permanent` also the default for new chats."""
    from .thinking import LEVELS, WORDS, level, thinks
    from .model_facts import model_key
    p = ctx.loop.llm.p
    key = model_key(p.base_url, p.model)
    if len(parts) == 1:
        cur = level(p)
        print(ui.accent(f"  thinking: {cur}") + ui.muted(f" — {WORDS[cur]}"))
        for lv in LEVELS:
            print(f"    {lv:<7} " + ui.muted(WORDS[lv]) + ("  ✓" if lv == cur else ""))
        ui.info(f"{p.model}: " + ("seen thinking" if thinks(key) else
                "no thinking seen yet — off/auto may be all it does")
                + " · /think <level> [permanent]")
        return Result()
    want = parts[1].lower()
    if want not in LEVELS:
        ui.warn("usage: /think auto|off|low|medium|high [permanent]")
        return Result()
    p.thinking = want
    try:                                   # Saved with this chat now, not at the next turn
        from .session_settings import remember
        remember(ctx.loop, ctx.session)
    except Exception:          # noqa: BLE001
        pass
    if len(parts) > 2 and parts[2] == "permanent":
        from .options import set_
        set_("model.thinking", want)
    ui.success(f"thinking {want} — {WORDS[want]}"
               + (" (new chats too)" if len(parts) > 2 and parts[2] == "permanent" else " (this chat)"))
    return Result()


@command("/sidebar")
def _sidebar(ctx, parts, line):
    """The chat screen handles /sidebar itself; here the choice is saved."""
    from .options import get, set_
    new = "off" if get("look.sidebar") == "on" else "on"
    set_("look.sidebar", new)
    ui.success(f"sidebar {new} — saved for the fullscreen chat screen")


@command("/cron")
def _cron(ctx, parts, line):
    """Scheduled jobs in the chat: a card per job — what it runs, when, next run,
    last result — plus add (shown, then a yes), pause, resume, remove, run now.
    """
    from . import cronops as C
    from .tools.cron_tool import MAX_JOBS, schedule_of
    sub = parts[1].lower() if len(parts) > 1 else "list"
    jobs = C.load_jobs(ctx.eng)
    if sub in ("list", "ls"):
        if not jobs:
            ui.info("no scheduled jobs — /cron add every day at 09:00 <task> · /blueprint has ready ones")
            return Result()
        now = time.time()
        for j in jobs:
            paused = j.get("status") == "paused"
            last = j.get("last_status")
            head = (ui.muted("paused") if paused else
                    ui.ok("ok") if last == "ok" else
                    ui.err("failed") if last == "failed" else ui.muted("not run yet"))
            print(f"  {j['id']}  {head}  " + ui.muted(C.schedule_words(j.get("schedule", {}))))
            print(f"    runs: {str(j.get('task', ''))[:90]}")
            nxt = C.next_run_ts(j, now)
            lr = j.get("last_run_ts")
            print(ui.muted(f"    next {C.when_words(nxt, now)}"
                           + (f" · last {time.strftime('%a %d %b %H:%M', time.localtime(lr))}"
                              f" ({j.get('runs', 0)} run{'s' if j.get('runs', 0) != 1 else ''})" if lr else "")
                           + (f" · {str(j.get('last_note'))[:70]}" if j.get("last_note") else "")))
        if not C.watcher_alive():
            ui.warn("jobs run only while the scheduler runs — start it in a terminal: aura cron watch")
        ui.info("/cron add <when> <task> · /cron pause|resume|remove|run <id>")
        return Result()
    if sub == "add" and len(parts) > 2:
        text = line.split(None, 2)[2]
        sched, task = schedule_of(text)
        # the job runs the task, not "every day at 09:00 …"
        import re as _re
        from .tools.cron_tool import _IN
        task = _IN.sub(" ", C._AT.sub(" ", C._INTERVAL.sub(" ", task)))
        task = _re.sub(r"^\s*(every\s+day|daily|each\s+day)\b", " ", task, flags=_re.I)
        task = " ".join(task.split()).strip(" ,:-")
        if sched is None or not task.strip():
            ui.warn("say when and what: /cron add every day at 09:00 summarize new issues")
            return Result()
        if sum(1 for j in jobs if j.get("status", "active") == "active") >= MAX_JOBS:
            ui.warn(f"{MAX_JOBS} jobs are active already — remove or pause one first")
            return Result()
        print(f"  when  {C.schedule_words(sched)}\n  runs  {task.strip()}")
        if ctx.ask("add it? [y/N] ").strip().lower() not in ("y", "yes"):
            ui.info("not added")
            return Result()
        j = C.add_job(ctx.eng, task.strip(), sched)
        ui.success(f"added {j['id']} — next run {C.when_words(C.next_run_ts(j))}")
        return Result()
    if sub in ("pause", "resume", "remove", "rm", "run") and len(parts) > 2:
        jid = parts[2]
        job = next((j for j in jobs if j.get("id") == jid), None)
        if job is None:
            ui.warn(f"no job {jid!r} — /cron lists them")
            return Result()
        if sub in ("remove", "rm"):
            C.remove_job(ctx.eng, jid)
            ui.success(f"removed {jid}")
        elif sub == "run":
            ui.info(f"running {jid} here now")
            C.mark_run(ctx.eng, jid, True, "run from the chat")
            return Result(task=str(job.get("task", "")))
        else:
            job["status"] = "paused" if sub == "pause" else "active"
            C.save_jobs(ctx.eng, jobs)
            ui.success(f"{jid} {'paused' if sub == 'pause' else 'active again'}")
        return Result()
    ui.info("/cron · /cron add <when> <task> · /cron pause|resume|remove|run <id>")
    return Result()


@command("/blueprint")
def _blueprint(ctx, parts, line):
    """Ready-made scheduled jobs (the engine's `aura blueprint`): shown in
    full and added only after a yes."""
    from . import blueprints as BP
    if len(parts) < 2:
        for n, bp in BP.BLUEPRINTS.items():
            print(f"  {n:16}{bp['about']}")
        ui.info("/blueprint <name> field=value … — shows the job and asks before adding it")
        return Result()
    name = parts[1]
    if name not in BP.BLUEPRINTS:
        ui.warn(f"no blueprint {name!r} — /blueprint lists them")
        return Result()
    values = BP.parse_values(line.split(None, 2)[2].split() if len(line.split(None, 2)) > 2 else [])
    task, sched_text, missing = BP.fill(name, values)
    for k in missing:
        key = k.split(" ")[0]
        values[key] = ctx.ask(f"{key} ({k}): ").strip()
    if missing:
        task, sched_text, missing = BP.fill(name, values)
        if missing:
            ui.warn("not added — still missing: " + ", ".join(missing))
            return Result()
    from .tools.cron_tool import MAX_JOBS, schedule_of
    from .cronops import add_job, load_jobs
    sched, _ = schedule_of(sched_text)
    if sched is None:
        ui.warn(f"the schedule {sched_text!r} isn't understood")
        return Result()
    if sum(1 for j in load_jobs(ctx.eng) if j.get("status", "active") == "active") >= MAX_JOBS:
        ui.warn(f"{MAX_JOBS} jobs are active already — remove or pause one first")
        return Result()
    print(f"  {name}: {sched_text}\n  it will run: {task}")
    if ctx.ask("add it? [y/N] ").strip().lower() not in ("y", "yes"):
        ui.info("not added")
        return Result()
    j = add_job(ctx.eng, task, sched)
    ui.success(f"added {j['id']} — it runs while `aura cron watch` is running")
    return Result()


@command("/image")
def _image(ctx, parts, line):
    """Take the image on the clipboard (or a file) for the next message —
    a terminal can't carry image data when you paste."""
    from .image_paste import clipboard_image, image_paths, vision_note
    if len(parts) > 1:
        found = image_paths(line[len(parts[0]):], getattr(ctx.loop, "workspace", None))
        if not found:
            ui.warn("no image file there (png, jpg, gif, webp, bmp)")
            return Result()
        path = found[0]
    else:
        path, why = clipboard_image(Path(ctx.eng) / "images")
        if path is None:
            ui.warn(why)
            return Result()
    send, note = vision_note(ctx.loop)
    if send:
        ctx.loop.pending_images = list(getattr(ctx.loop, "pending_images", None) or []) + [path]
        ui.success(note + " — it goes with your next message")
    else:
        ui.warn(note + f" (saved: {path})")
    return Result()


@command("/vault")
def _vault(ctx, parts, line):
    """Saved site logins: the model sees site + user only. Adding one is
    done in a terminal — a password is never typed into the chat (it is
    saved, and shown on screen)."""
    from . import site_vault as V
    sub = parts[1].lower() if len(parts) > 1 else "list"
    if sub == "remove" and len(parts) > 2:
        n = V.remove(parts[2], parts[3] if len(parts) > 3 else None)
        (ui.success if n else ui.warn)(f"removed {n} login(s)" if n else "nothing to remove")
        return Result()
    if sub == "add":
        site = parts[2] if len(parts) > 2 else "<site>"
        ui.info(f"in a terminal: aura vault add {site} — the password is asked there, hidden "
                "(never in the chat: chats are saved)")
        return Result()
    labels = V.labels()
    if not labels:
        ui.info("no saved logins — aura vault add <site> in a terminal")
    for lb in labels:
        print(f"  {lb['site']}  ({lb['user']})")
    return Result()

# old name kept working
_COMMANDS["/results"] = _findings


# Persona (pblocks) commands live in their own module; loading it here
# registers /persona with the same dispatcher.
from . import persona_commands  # noqa: E402,F401

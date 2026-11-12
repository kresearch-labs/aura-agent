"""sidebar.py — the fullscreen sidebar and the "/" menu's detail pane.

Both are plain functions of the chat app's state, drawn by full.py (the
sidebar) and app._popup_lines (the detail pane).
"""
from __future__ import annotations


from .text import pad

# what each switch is called, and how its options read (the "/" detail pane)
OPTIONS: dict[str, list[tuple[str, str]]] = {
    "/research": [("basic", "only the listed sources"),
                  ("normal", "anything on the web, kept small"),
                  ("deep", "plan first, two sources each")],
    "/assist": [("L1", "asks before every change"), ("L2", "asks before risky actions"),
                ("L3", "never asks")],
    "/mode": [("act", "does the work"), ("plan", "reads and plans, changes nothing")],
    "/discuss": [("on", "talk only — no changes"), ("off", "back to work")],
    "/findaway": [("on", "offer a way to what's missing — asks before any effort"),
                  ("off", "say what's missing and what you can do")],
    "/machine": [("on", "watch memory while working; low → the model is told once"),
                 ("off", "only the check after each task")],
    "/team": [("status", "the hub and your connection"), ("connect <address>", "join a team's hub"),
              ("board [--all]", "your tasks · everyone's"), ("link <request>", "match it to a task"),
              ("start | done", "this chat's task"), ("decision <text>", "on the board"),
              ("queue | sync", "updates waiting for the hub"), ("hub setup|start|stop", "the hub here"),
              ("chat on|off", "the people chat page")],
    "/think": [("auto", "the model's own default"), ("off", "no thinking — fastest"),
               ("low", "a little thinking"), ("medium", "more thinking"),
               ("high", "the most thinking — slowest")],
    "/proof": [("on", "re-check and test before answering"), ("off", "answer right away")],
    "/plan": [("on", "keep a step-by-step plan for this chat"), ("off", "no plan")],
    "/tui": [("fullscreen", "the whole window, sidebar"), ("inline", "your terminal's scrollback")],
    "/web": [("open on|off", "the no-account sources"), ("all on|off", "read any public page")],
    "/cron": [("add <when> <task>", "schedule a job"), ("pause|resume <id>", "stop / restart one"),
              ("remove <id>", "delete one"), ("run <id>", "run it here now")],
    "/tools": [("on <group>", "send a tool group"), ("off <group>", "stop sending it"),
               ("list", "every tool and its group")],
    "/mcp": [("popular", "well-known servers"), ("search <words>", "the official registry"),
             ("add <name>", "shows what it runs, asks"), ("login <name>", "sign in in the browser")],
    "/resume": [("<#id | title | latest>", "open that chat"), ("search <words>", "every message"),
                ("info <#id>", "what it did, what it used"), ("delete <#id>", "into the trash"),
                ("restore <#id>", "out of the trash"), ("trash [empty]", "deleted chats"),
                ("pin | archive <#id>", "top / hidden"), ("rename <#id> <title>", "same id"),
                ("export <#id> [md|jsonl]", "keys scrubbed")],
    "/add-dir": [("<folder>", "allow it for this chat"), ("<folder> permanent", "save it"),
                 ("remove <folder>", "take it off")],
    "/scope": [("add <site>", "allow a website or host"), ("remove <site>", "take it off")],
    "/rules": [("add <rule>", "a rule the work must keep"), ("rm <n>", "remove one")],
}

EXAMPLES: dict[str, str] = {
    "/research": "/research deep", "/assist": "/assist L1", "/mode": "/mode plan",
    "/discuss": "/discuss on", "/team": "/team connect http://192.168.1.10:3456", "/think": "/think high", "/proof": "/proof on permanent", "/plan": "/plan on", "/tui": "/tui inline",
    "/machine": "/machine on", "/web": "/web all on", "/cron": "/cron add every day at 09:00 summarize new issues",
    "/tools": "/tools on web", "/mcp": "/mcp add context7", "/add-dir": "/add-dir ~/Downloads",
    "/scope": "/scope add docs.python.org", "/rules": "/rules add never edit config/",
    "/model": "/model llama3.3:70b", "/rename": "/rename auth refactor",
    "/resume": "/resume #a1b2c3", "/export": "/export notes.md",
    "/settings": "/settings find research", "/memory": "/memory forget 3",
    "/theme": "/theme light", "/budget": "/budget preset frugal",
}


def _state(app, name: str) -> str | None:
    """A switch's current value, as words; None for commands that aren't switches."""
    m, loop = app.m, app.loop
    try:
        from ..options import get
        if name == "/research":
            return getattr(loop, "research_mode", None) or get("research.mode")
        if name == "/assist":
            return {"L1": "L1 · asks every change", "L2": "L2 · asks before risky",
                    "L3": "L3 · never asks"}.get(getattr(m, "assist", "L2"))
        if name == "/mode":
            return getattr(m, "mode", "act")
        if name == "/team":
            from ..editions import teams_present
            if not teams_present():
                return None
            from ..teams_ui import snapshot, status_mark
            mk = status_mark(snapshot(app.ctx.session.path.stem))
            return mk[0].replace("team ", "") if mk else "not connected"
        if name == "/findaway":
            return "on" if get("assist.find_a_way") else "off"
        if name == "/machine":
            return "on" if get("machine.watch") else "off"
        if name == "/think":
            from ..thinking import level
            return level(loop.llm.p)
        if name == "/discuss":
            return "on" if getattr(m, "mode", "act") == "discuss" else "off"
        if name == "/proof":
            from .. import features
            return "on" if features.enabled("proof") else "off"
        if name == "/plan":
            from .. import features
            return "on" if features.enabled("plan") else "off"
        if name == "/focus":
            return "on" if app.focus else "off"
        if name == "/tui":
            return "fullscreen" if app.fullscreen else "inline"
        if name == "/vim":
            return "on" if app.vim is not None else "off"
        if name == "/web":
            from .. import websearch as W
            from ..engagement import load_scope
            return (f"open sources {W.consent() or 'on'} · all pages "
                    f"{'on' if load_scope(app.eng).get('web_all', True) else 'off'}")
        if name == "/theme":
            return getattr(app.s, "theme_name", None) or get("look.theme")
    except Exception:      # noqa: BLE001 - a detail pane never breaks the menu
        return None
    return None


def command_detail(app, name: str, width: int, height: int, picked: str = "") -> list[str]:
    """The right half of the "/" menu for one command (or one of its options)."""
    from ..command_registry import get
    s = app.s
    spec = get(name)
    if spec is None:
        return [s("dim", ln) for ln in _wrap("your own command (from the commands folder)", width)]
    out = [s("accent", name, bold=True)]
    for line in _wrap(spec.desc, width):
        out.append(s("text", line))
    state = _state(app, name)
    if state is not None:
        out += [""] + _hang(s, s("faint", "now  "), "strong", state, width, 5)
    opts = OPTIONS.get(name)
    if name == "/theme":
        from .style import THEMES
        opts = [(n, "") for n in THEMES]
    if opts:
        out.append("")
        key_w = min(max(len(o) for o, _ in opts) + 2, width // 2)
        for o, why in opts:
            here = state is not None and (o == state or state.startswith(o + " "))
            mark = s("ok", " ✓") if here else ""
            key = s("strong" if o == picked else "text", pad(o, key_w))
            if len(o) >= key_w:            # A long option: its words on the next line
                out.append(s("strong" if o == picked else "text", o) + mark)
                if why:
                    out += [" " * 2 + s("dim", ln) for ln in _wrap(why, width - 2)]
                continue
            body = _wrap(why, max(4, width - key_w)) if why else [""]
            out.append(key + s("dim", body[0]) + mark)
            out += [" " * key_w + s("dim", ln) for ln in body[1:]]
    ex = EXAMPLES.get(name) or spec.example
    if ex:
        out += [""] + _hang(s, s("faint", "e.g. "), "dim", ex, width, 5)
    elif not opts:
        use = spec.usage.replace("usage:", "").strip()
        if use and use != name:
            out += [""] + _hang(s, s("faint", "use  "), "dim", use, width, 5)
    return out[:height]


def _wrap(text: str, width: int) -> list[str]:
    """Words onto lines of `width`; a word longer than a line is broken (a long
    path) — nothing is cut off.
    """
    width = max(4, width)
    lines, cur = [], ""
    for w in str(text).split():
        while len(w) > width:              # A long path / id: break it after a / when it can
            if cur:
                lines.append(cur)
                cur = ""
            at = max(w.rfind(c, 0, width) for c in ("/", "\\", "-", "_", "."))
            at = at + 1 if at >= width // 3 else width
            lines.append(w[:at])
            w = w[at:]
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _hang(s, first: str, role: str, text: str, width: int, indent: int) -> list[str]:
    """`first` (a mark, a label) then text wrapped under itself."""
    body = _wrap(text, width - indent)
    return [first + s(role, body[0])] + [" " * indent + s(role, ln) for ln in body[1:]]


# ------------------------------------------------------------- sidebar --
def _head(s, text: str, extra: str = "") -> str:
    return s("heading", text.upper(), bold=True) + (s("faint", "  " + extra) if extra else "")


def _row(s, label: str, value: str, width: int, role: str = "text") -> list[str]:
    return _hang(s, s("faint", pad(label, 10)), role, value, width, 10)


def sidebar_lines(app, width: int, height: int) -> list[str]:
    """What's going on, always in view (fullscreen): the switches, the plan while
    a step is open, the team.
    """
    s = app.s
    out: list[str] = []
    # The model, context, tokens, how often Aura asks, the project and the
    # branch are on the status line already switches
    out.append(_head(s, "switches", "● on  ○ off"))
    mode = getattr(app.m, "mode", "act")
    out += _row(s, "mode", "discuss · talk only" if mode == "discuss" else mode, width,
                "warn" if mode in ("discuss", "plan") else "text")
    out += _row(s, "research", _state(app, "/research") or "normal", width)
    # every on/off switch, two to a row: ● on (accent) / ○ off (faint) —
    # the shape says it too, not just the colour
    flags = _switches(app)
    two = max((len(l) for l, _ in flags), default=0) + 3 <= width // 2
    col, per = (width // 2, 2) if two else (width, 1)
    for i in range(0, len(flags), per):
        row = ""
        for label, on in flags[i:i + per]:
            cell = ("● " if on else "○ ") + label
            row += s("accent" if on else "faint", pad(cell, col))
        out.append(row)
    hist = _history(app)
    if hist:
        out += _row(s, "history", hist, width, "dim")
    out.append("")
    out += _persona(app, width)
    # the plan — only while a step is open (a finished plan is noise)
    items = []
    try:
        app._plan_lines()                       # Refreshes the cache (1.5 s)
        items = app._todos_cache[1]
    except Exception:      # noqa: BLE001
        pass
    open_ = [i for i in items if i.get("status") != "done"]
    if open_:
        done = len(items) - len(open_)
        out.append(_head(s, "plan", f"{done}/{len(items)} done"))
        room = max(3, height - len(out) - 6)
        used, n_shown = 0, 0
        for it in open_:
            st, text = it.get("status", "pending"), str(it.get("text") or "")
            mark, role = ((s("accent", "◐ "), "strong") if st in ("in_progress", "doing")
                          else (s("dim", "○ "), "dim"))
            rows = _hang(s, mark, role, text, width, 2)
            if used and used + len(rows) > room:
                break
            out += rows
            used += len(rows)
            n_shown += 1
        if n_shown < len(open_):
            out.append(s("faint", f"  … {len(open_) - n_shown} more · Ctrl+T shows the plan"))
        out.append("")

    out += _team(app, width)
    if app.running:
        out += _hang(s, s("accent", "● "), "dim", app.activity or "working", width, 2)
    return out[:height]


def _ago(ts: float) -> str:
    import time
    secs = max(0.0, time.time() - float(ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return "now"


def _history(app) -> str:
    """How much of this chat Aura has in front of it: the whole chat, or the part
    since the last summary — the rest stays on disk and is searched when
    needed.
    """
    try:
        info = app.ctx.session.in_view()
    except Exception:      # noqa: BLE001
        return ""
    if not info["summarized"]:
        return "whole chat in view"
    since = f"last {_ago(info['since'])}" if info.get("since") else "recent part"
    parts = info["summarized"]
    return f"{since} in view · {parts} earlier part{'s' if parts != 1 else ''} summarized, all kept"


def _persona(app, width: int) -> list[str]:
    """PERSONA: on/off and only the pblocks in use for this chat — never the whole
    collection (it can hold hundreds; that is persona's job), never what goes
    on behind.
    """
    s = app.s
    try:
        from .. import pblocks as P
        if not P.enabled():
            return [_head(s, "persona", "○ off"), ""]
        if P.missing_packages():
            return [_head(s, "persona", "needs setup")] + [s("faint", ln) for ln in _wrap(
                "reinstall Aura to add " + ", ".join(P.missing_packages()), width)] + [""]
        used = P.panel(app.ctx.session.path.stem)
    except Exception:      # noqa: BLE001
        return []
    out = [_head(s, "persona", "● on")]
    if used:
        for n in used:
            out += _hang(s, s("accent", "● "), "text", n, width, 2)
    else:
        out.append(s("faint", "no pblocks in use · /persona"))
    out.append("")
    return out


def _team(app, width: int) -> list[str]:
    """TEAM (Aura Teams): the hub, you and this chat's board task, who else is
    working on what, updates waiting for the hub. Not connected: just "TEAM
    (off)".
    """
    from ..editions import teams_present
    if not teams_present():
        return []
    from ..teams_ui import snapshot
    s = app.s
    try:
        d = snapshot(app.ctx.session.path.stem)
    except Exception:      # noqa: BLE001
        d = {}
    if not d:
        from ..teams_ui import pending
        if pending():
            return [_head(s, "team", "(setting up)")] + [s("faint", ln) for ln in
                    _wrap(f"{pending()} — not wired yet; ask Aura to finish it", width)] + [""]
        return [_head(s, "team", "(off)"), ""]
    online = d.get("online")
    state = ("…" if online is None else "● online" if online else "○ offline")
    out = [_head(s, "team", state)]
    out += _row(s, "you", d.get("user") or "?", width)
    link = d.get("link") or {}
    if link.get("task"):
        st = getattr(app, "team_state", None) or ((("started", False)) if link.get("started") else None)
        state = {"started": "in progress", "done": "done", "collision": "overlaps others' work",
                 "blocked": "blocked"}.get(st[0], "") if st else ""
        if st and st[1]:
            state += " (queued — hub off)"
        out += _row(s, "this chat", f"#{link['task']} {link.get('title', '')}"
                    + (f" — {state}" if state else ""), width, "accent")
    elif link.get("off_board"):
        out += _row(s, "this chat", "not on the board", width, "faint")
    working = d.get("working") or []
    for w in working[:4]:
        who = ", ".join(w.get("who") or [])
        out += _hang(s, s("accent", "● "), "text", f"{who} — #{w['id']} {w.get('title', '')}", width, 2)
        if w.get("files"):
            out += [" " * 4 + s("faint", ln) for ln in _wrap(", ".join(w["files"][:4]), width - 4)]
    if len(working) > 4:
        out.append(s("faint", f"  … {len(working) - 4} more · /team board"))
    if online and not working:
        out.append(s("faint", "nobody else working right now"))
    if d.get("broken"):
        out += [s("warn", ln) for ln in _wrap(
            f"{', '.join(d['broken'])} stopped working (the app changed?) — ask Aura to fix "
            "the wiring, or /team wire fix", width)]
    if d.get("queued"):
        out += [s("warn", ln) for ln in _wrap(f"{d['queued']} update(s) waiting for the hub · /team sync", width)]
    out.append("")
    return out


def _switches(app) -> list[tuple[str, bool]]:
    """The switches you flip while working: talk only, check the work, the web.
    Aura's own quality machinery (message check, drift, reviewer, lint …),
    display preferences and the sandbox policy are set in settings, not shown
    here.
    """
    from .. import features
    out = [("discuss", getattr(app.m, "mode", "act") == "discuss"),
           ("proof", features.enabled("proof")),
           ("plan", features.enabled("plan"))]
    try:
        from .. import websearch as W
        out.append(("web", W.consent() != "off"))
    except Exception:      # noqa: BLE001
        pass
    try:
        from ..options import get
        out.append(("machine", bool(get("machine.watch"))))
    except Exception:      # noqa: BLE001
        pass
    return out

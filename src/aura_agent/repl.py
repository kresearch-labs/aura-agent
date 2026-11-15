"""repl.py — `aura chat`: the chat screen, or script mode without a terminal.

On a terminal, `aura chat` opens the chat screen (tui/app.py). With no terminal
(a script pipes messages in), it runs in script mode. Nobody is there to answer
a card, so whatever needs a yes is refused, as with `aura run`.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from . import ui
from .sessions import Session, new_session

def _kill_watch(eng: Path, state: dict):
    flag = eng / ".kill"
    while not state.get("stop"):
        if flag.exists():
            state["killed"] = True
            return
        time.sleep(0.5)


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


def chat(eng: Path, profile_name: str | None, session_file: str | None = None,
         task_intro: str | None = None, name: str | None = None,
         fork: str | None = None) -> int:
    from .cli import build_agent
    from .engine.loop import DEFAULT_SYSTEM
    from .session_store import list_sessions

    ui.enable()
    profile, m, loop = build_agent(eng, profile_name)
    state: dict = {"steer_queue": [], "eng_name": eng.name}
    threading.Thread(target=_kill_watch, args=(eng, state), daemon=True).start()

    from .tui.app import use_tui
    _use_tui_now = use_tui()

    # UX / / audit refactor: the renderer is RunNarrator — the SAME class the
    # visual audit drives, zero drift between tests and users.
    from .run_narrator import RunNarrator
    _narrator = RunNarrator.attach(loop)
    # Operator status bar — model + ctx window + proven count.
    try:
        _profile_model = getattr(getattr(loop, "llm", None), "p", None)
        _model_name = getattr(_profile_model, "model", "") or ""
        _ctx_total = int(getattr(_profile_model, "context_length", 0) or 0)

        def _proven_count() -> int:
            try:
                from .finding_card import findings_snapshot
                return sum(1 for f in findings_snapshot(eng)
                           if f.get("status") == "validated")
            except Exception:
                return 0

        _narrator.configure_bar(model=_model_name, ctx_total=_ctx_total,
                                proven_fn=_proven_count)
    except Exception:
        pass  # Bar must never block chat startup
    # Structured event stream (AURA_EVENTS=1 → events.jsonl)
    from .eventlog import EventLog
    _eventlog = EventLog(eng)

    def _chat_event_composed(kind: str, data: dict) -> None:
        _eventlog(kind, data)
        _narrator(kind, data)

    loop.on_event = _chat_event_composed

    if session_file:
        session = Session(Path(session_file))
    elif fork:
        # 2.9: fork the newest (or --session) session into a named copy
        import shutil
        src = Session(Path(session_file)) if session_file else None
        if src is None:
            all_sessions = list_sessions(eng)
            if all_sessions:
                fname = all_sessions[0].get("file")
                if isinstance(fname, str):
                    src_path = eng / "sessions" / fname
                    if src_path.exists():
                        src = Session(src_path)
        if src is None:
            ui.warn("fork: no source session — starting fresh")
            session = new_session(eng / "sessions", fork)
        else:
            src.ensure_meta()
            session = src.fork(src.seq, label=f"fork-{fork}")
            ui.success(f"forked #{src.id} → #{session.id}")
    elif name:
        # 2.9: named session — friendly filename instead of task-slug
        from .sessions import slugify
        session = Session(eng / "sessions" / f"{name}.jsonl")
        if not session.path.exists():
            session.path.touch()
        ui.info(f"new session: {session.path.name}")
    else:
        sessions = [s for s in list_sessions(eng) if s["messages"] > 2]
        if sessions and not task_intro:
            # ONE-GATE LAW: the Previously-On card is the ENTIRE startup
            # decision. What you did + state + the three-way choice, rendered
            # passively. The ONLY question is the large-session tradeoff
            # (inline in the card, "c" answers it); the picker appears ONLY on
            # resume. No resume-mode dialog, no forced picker, no double card.
            from .last_state import previously_on
            _age_h = (sessions[0].get("age_min") or 0) / 60.0
            _pon = previously_on(eng, age_days=_age_h / 24.0)
            if _pon and _use_tui_now:
                # the chat screen shows the card in its header
                state["_pon_card"] = _pon
            # NEW CHAT by default: aura chat always opens a fresh session. The
            # Previously-On card shows what the last session was; resuming is
            # opt-in via resume.
            session = new_session(eng / "sessions", "chat", lazy=True)
        else:
            session = new_session(eng / "sessions", task_intro or "chat", lazy=True)

    session.ensure_meta()                  # Every chat has its id from now on
    try:                                   # Deleted chats past chats.trash_days
        from .options import get as _tget
        from .session_store import empty_trash
        empty_trash(eng, older_than_days=float(_tget("chats.trash_days")))
    except Exception:                      # noqa: BLE001
        pass
    if not session.messages:
        session.append("system", content=loop.cfg.system_prompt or DEFAULT_SYSTEM)

    # every session remembers everything it runs with (model, budget,
    # features, mode, ...). What Aura starts with = the profile's defaults
    # — a new session (/new) or an older one that never saved any gets them.
    from . import session_settings as _sset
    _profile_defaults = _sset.capture(loop, state)

    def _open_session_settings(sess) -> None:
        nonlocal profile
        for _n in _sset.apply(loop, sess.settings or _profile_defaults,
                              eng, state):
            ui.warn(_n)
        profile = loop.llm.p
        _sset.remember(loop, sess, state)

    def _defaults_take(*keys: str) -> None:
        """A `permanent` change also becomes the default for new sessions."""
        _now = _sset.capture(loop, state)
        for _k in keys:
            if _k in _now:
                _profile_defaults[_k] = _now[_k]

    _open_session_settings(session)

    # The chat screen (tui/): inline and scrollback-safe, a real input box,
    # live activity, approvals as cards — the one screen. No terminal: script mode.
    if _use_tui_now:
        try:
            from .tui.app import ChatApp
            app = ChatApp(eng=eng, loop=loop, m=m, profile=profile,
                          session=session, state=state,
                          open_session_settings=_open_session_settings,
                          defaults_take=_defaults_take, version=_version(),
                          task=task_intro or None,
                          pon=state.get("_pon_card", ""))
            code = app.run()
            if app.relaunch:                     # The work's folder was chosen: go on there
                from .cli import engagement_dir
                from .work_folder import move_chat
                ws, text = app.relaunch
                new_eng = engagement_dir(str(ws))
                moved = move_chat(eng, new_eng, app.ctx.session.path)
                return chat(new_eng, profile_name, session_file=str(moved), task_intro=text)
            return code
        except Exception as _fe:            # noqa: BLE001
            # never silent: what broke and where the details are
            import traceback
            try:
                with open(eng / "ui_fallback.log", "a", encoding="utf-8") as _ff:
                    _ff.write(traceback.format_exc() + "\n")
            except OSError:
                pass
            ui.warn(f"the chat screen failed to open ({_fe}) — details in "
                    f"{eng / 'ui_fallback.log'}")
            return 1
    return _script_mode(eng, loop, m, profile, session, state, task_intro,
                        _open_session_settings, _defaults_take)


def _script_mode(eng, loop, m, profile, session, state, task_intro,
                 open_session_settings, defaults_take) -> int:
    """No terminal: each line from stdin is one message — a /command runs,
    anything else is a task, and the answer is printed. Nobody is there to
    answer a card, so whatever needs a yes is refused (as with `aura run`)."""
    from .chat_commands import ChatContext, dispatch
    ctx = ChatContext(eng=eng, loop=loop, m=m, profile=profile, session=session,
                      state=state, open_session_settings=open_session_settings,
                      defaults_take=defaults_take, version=_version())
    m.approver = None
    m.asker = None

    def lines():
        if task_intro:
            yield task_intro
        for raw in sys.stdin:
            yield raw.rstrip("\r\n")

    code = 0
    for text in lines():
        text = text.strip()
        if not text:
            continue
        if text.lower() in ("/exit", "/quit"):
            break
        if text.startswith("/"):
            res = dispatch(ctx, text)
            if getattr(res, "exit", False):
                break
            continue
        from .work_folder import needs_folder
        if needs_folder(eng):
            # no project folder and nobody to ask where: nothing is made in
            # Aura's own folder
            ui.warn("not run — there's no project folder here to work in; start Aura "
                    "in the project's folder or give it one with --dir")
            code = 1
            continue
        rec = loop.run(ctx.session, text, kill_check=lambda: bool(state.get("killed")),
                       steer_queue=state.setdefault("steer_queue", []))
        if getattr(rec, "content", None):
            print(rec.content)
        if getattr(rec, "error", None):
            ui.warn(f"stopped: {rec.error}")
            code = 1
    state["stop"] = True
    return code

"""repl.py — interactive session, full operator surface."""
from __future__ import annotations

import json
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


def _tok_per_turn(session, governor) -> int:
    """Average tokens per user-turn so far (local arithmetic)."""
    try:
        n = governor.count(session.messages)
        turns = max(1, len([m for m in session.messages
                            if m.get("role") == "user"]))
        return max(1, n // turns)
    except Exception:
        return 1


def _post_turn_ui(session, eng: Path, governor, loop, rec=None) -> None:
    """Shared post-turn output."""
    # Fix:.last-state maintenance ran only when a governor existed —
    # dead-endpoint turns (governor None) silently produced NO continuity card
    # on next start ("what am I resuming?" with nothing to read). State write
    # is governor-independent now; the token/ctx line stays governor-gated (it
    # needs the ledger).: maintain.last-state (the shared read model) every
    # turn — scrubbed at write time; dashboard/arrival/ Previously-On all read
    # it
    try:
        from .last_state import update_last_state
        from .finding_card import findings_snapshot
        _fs = findings_snapshot(eng)
        update_last_state(eng, {
            "last_session": session.path.name,
            "last_session_id": getattr(session, "id", ""),
            "last_ts": (rec.receipt.get("finished_ts")
                        if (rec is not None and getattr(rec, "receipt", None))
                        else None),
            "findings_total": len(_fs),
            "findings_proven": sum(1 for f in _fs if f.get("proof")),
            "last_task": session.last_user_task() or "",
            "last_action": (rec.content or "")[:120] if rec else "",
        })
    except Exception:
        pass  # State maintenance must never kill the turn
    # Named-session promotion — one suggestion, once per session. Generic
    # suffixes (chat/va/digits) count as UNNAMED — visual-audit find:
    # date-prefixed stems always split to a suffix, so the hint never fired for
    # default-named sessions.
    try:
        if not getattr(session, "_promotion_shown", False):
            from .last_state import promotion_hint
            _generic = {"chat", "va", "task", "demo", "session"}
            _suffix = session.path.stem.split("-", 2)[-1] \
                if "-" in session.path.stem else ""
            _is_named = _suffix and _suffix not in _generic \
                and not _suffix.isdigit()
            _hint = promotion_hint(
                session_name=_suffix if _is_named else "",
                n_messages=len(session.messages))
            if _hint:
                ui.info(_hint)
                session._promotion_shown = True
    except Exception:
        pass
    from .todo import load_items, render_checklist
    items = load_items(eng, session.path.stem)
    if items:
        print(ui.muted(render_checklist(items)))
    # Skill suggestion after complex turns
    from .skills import suggest_skill
    n_calls = len(getattr(loop, "intent_calls", []) or [])
    suggestion = suggest_skill(rec, n_calls)
    if suggestion:
        ui.info(suggestion)
    # Per-message token line when provider usage is present — governor-gated
    # (the line needs the ledger)
    if governor is not None:
        lu = getattr(loop, "last_usage", None) or {}
        # Measured cache hit ratio when the provider reports it
        hits, misses = getattr(loop, "cache_hits", 0), getattr(loop, "cache_misses", 0)
        cache_part = ""
        if hits or misses:
            ratio = 100 * hits / (hits + misses) if (hits + misses) else 0
            cache_part = f"  · cache {ratio:.0f}%"
        if lu:
            print(ui.muted(f"  · tokens ↑{lu.get('prompt', 0)} "
                           f"↓{lu.get('completion', 0)}  session "
                           f"{governor.token_ledger}" + cache_part))
        else:
            n = governor.count(session.messages)
            ctx_tag = ""
            src_tag = getattr(loop, "ctx_source", "")
            from .ctx_probe import is_guess
            if is_guess(src_tag):
                ctx_tag = "  [window GUESSED — /model ctx <n> to set]"
            print(ui.muted(f"  · ctx {int(100 * n / governor.ctx)}%"
                           + cache_part + ctx_tag))
    # UX: phase events (gate/finish-verify/compaction) fire as HUD notes
    # instead of silence — wired in chat via loop.on_event below.


def _turn_state(rec) -> str:
    """Turn state for the answer-box corner — green ok when the receipt checks all
    pass, amber warn when some missing, red fail on error. Falls back sensibly
    when no receipt exists.
    """
    if getattr(rec, "error", None):
        return "fail"
    receipt = getattr(rec, "receipt", None)
    if not receipt:
        return "ok"
    checks = receipt.get("checks", {})
    if checks and all(c.get("ok") for c in checks.values()):
        return "ok"
    if checks:
        return "warn"
    return "ok"


def _title_chat(loop, session, text: str) -> None:
    """A plain chat (file named "...-chat") gets a real title from the helper
    model — only a helper that passed its check; the main model is never spent
    on a title.
    """
    try:
        if getattr(session, "title", "") or not session.path.stem.endswith("-chat"):
            return
        from .helper import checked, profile, title_for
        p = profile(getattr(getattr(loop, "llm", None), "p", None))
        if p is None or checked(p) != "yes":
            return
        t, who = title_for(text)
        if t:
            session.record_title(t, by=who)
    except Exception:                                # noqa: BLE001
        pass                      # A title must never break a turn


def _run_turn(loop, session, state: dict, text: str, queued: list[str] | None = None):
    """One agent turn with KeyboardInterrupt protection."""
    try:
        rec = loop.run(session, text,
                       kill_check=lambda: state.get("killed", False),
                       steer_queue=state.get("steer_queue"))
        # Resolution metric — a clean turn closes any pending error
        if not rec.error:
            (state.get("_res") or _NoRes()).mark_success()
        else:
            (state.get("_res") or _NoRes()).mark_error(rec.error)
        _title_chat(loop, session, text)
        return rec
    except KeyboardInterrupt:
        print()
        ui.success("interrupted — checkpoint saved")
        return None
    except RuntimeError as e:
        ui.error(f"llm: {e}")
        ui.info("check: aura doctor — or /model to switch")
        (state.get("_res") or _NoRes()).mark_error(str(e))
        return None


def _queued_approver(state: dict):
    """Classic screen: the run is on a worker thread and the main thread owns the
    keyboard (the "· queued>" prompt), so the question is posted in
    state["approval"] and the queued reader answers it.
    """
    from .approvals import card_lines

    def approver(req: dict) -> str:
        slot = {"req": req, "done": threading.Event(), "answer": "deny"}
        state["approval"] = slot
        print()
        for ln in card_lines(req):
            print("  " + (ui.accent(ln) if ln.startswith("?") else ui.muted(ln)))
        slot["done"].wait()                  # Paused until you answer (no expiry)
        state.pop("approval", None)
        return slot["answer"]
    from .approvals import WAIT_TTL_S
    approver.ttl_s = WAIT_TTL_S
    return approver


def _queued_asker(state: dict):
    """Like _queued_approver, for a question: numbered options, several
    (1,3) when allowed, the user's own words when allowed. No timeout —
    nothing is assumed because time passed (Enter alone leaves it to Aura)."""
    def asker(question: str, options: list[str], multi: bool = False,
              free: bool = False):
        slot = {"options": options, "done": threading.Event(), "answer": None,
                "multi": multi, "free": free}
        state["question"] = slot
        print()
        print("  " + ui.accent("? " + question))
        for i, o in enumerate(options, 1):
            print("  " + ui.muted(f"  {i}. {o}"))
        how = ["a number" + (" or several (1,3)" if multi else "")] if options else []
        if free or not options:
            how.append("your own words")
        print("  " + ui.muted("  answer with " + " or ".join(how)
                              + " · Enter on its own = leave it to Aura"))
        slot["done"].wait()
        state.pop("question", None)
        return slot["answer"]
    return asker


def _answer_question(state: dict, text: str) -> bool:
    slot = state.get("question")
    if not slot:
        return False
    t = text.strip()
    if not t:
        slot["done"].set()
        return True
    opts = slot["options"]
    nums = [x.strip() for x in t.split(",")]
    if opts and all(x.isdigit() and 1 <= int(x) <= len(opts) for x in nums) \
            and (len(nums) == 1 or slot.get("multi")):
        picked = [opts[int(x) - 1] for x in nums]
        slot["answer"] = picked if slot.get("multi") else picked[0]
        slot["done"].set()
        return True
    if slot.get("free") or not opts:
        slot["answer"] = t
        slot["done"].set()
        return True
    ui.info(f"answer with a number 1-{len(opts)}")
    return True


def _answer_pending(state: dict, text: str) -> bool:
    """Route a typed line to a waiting approval. True = it was consumed."""
    if _answer_question(state, text):
        return True
    slot = state.get("approval")
    if not slot:
        return False
    from .approvals import parse_answer
    ans = parse_answer(text, slot["req"])
    if ans is None:
        ui.info("waiting for your answer to the approval above: y / "
                + ("a / " if slot["req"].get("can_always") else "") + "n")
        return True
    slot["answer"] = ans
    slot["done"].set()
    return True


def _deny_pending(state: dict) -> None:
    q = state.get("question")
    if q:
        q["done"].set()
    slot = state.get("approval")
    if slot:
        slot["answer"] = "deny"
        slot["done"].set()


class _NoRes:
    """Null-object so _run_turn works without a tracker (CLI paths)."""
    def mark_error(self, *_a, **_k): pass
    def mark_command(self, *_a, **_k): pass
    def mark_success(self, *_a, **_k): pass


def startup_hints() -> list[str]:
    """The two startup hint lines, driven by the command registry.

    Line 1: how to discover commands. Line 2: completion + one contextual
    example pulled from the registry (a command with an `example` field).
    """
    from .command_registry import COMMANDS, daily_five
    d5 = " ".join(c.name for c in daily_five())
    example = next((c.example for c in COMMANDS if c.example), "try: /help")
    # Visual: 76-col cap — the daily-five list alone hits ~100 chars with the
    # prefix; two short lines beat one wrapped line.
    return [
        f"type /help for commands · daily five: {d5}",
        f"/ starts the command menu · TAB completes · {example}",
    ]


def chat(eng: Path, profile_name: str | None, session_file: str | None = None,
         task_intro: str | None = None, name: str | None = None,
         fork: str | None = None) -> int:
    from .cli import build_agent
    from .engine.loop import DEFAULT_SYSTEM
    from .session_store import list_sessions

    ui.enable()
    profile, m, loop = build_agent(eng, profile_name)
    governor = loop.governor
    state: dict = {"steer_queue": [],   # Compatible: steer appends here
                   "eng_name": eng.name,   # Stateful prompt
                   "_proven_cache": 0}
    queued: list[str] = []        # Lines typed while the agent runs
    pending_input: list[str] = [] # Queued slash commands → main loop
    pending_slash: list[str] = []
    threading.Thread(target=_kill_watch, args=(eng, state), daemon=True).start()

    # Pinned bottom dock: the status bar + ► prompt ALWAYS sit at the bottom;
    # every turn output renders above them. A stdout hook erases/redraws the
    # dock around content writes.
    from .contracts.dock import Dock
    from .tui.app import use_tui
    _use_tui_now = use_tui()
    _dock = Dock()          # Installed below, only for the plain screen

    # UX / / audit refactor: the renderer is RunNarrator — the SAME class the
    # visual audit drives, zero drift between tests and users.
    from .run_narrator import RunNarrator
    _narrator = RunNarrator.attach(loop)
    _narrator.dock = _dock   # Pinned dock: narrator bar → dock redraw
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
    _hud = _narrator.hud
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
            from .last_state import previously_on, resume_tradeoff
            _age_h = (sessions[0].get("age_min") or 0) / 60.0
            _pon = previously_on(eng, age_days=_age_h / 24.0)
            from .tui.app import use_tui as _use_tui
            if _pon and _use_tui():
                # the new chat shows the card in its header
                state["_pon_card"] = _pon
            elif _pon:
                print(_pon)
            # large-session tradeoff: ONE line inside the card flow
            try:
                if not _use_tui():
                    # the new chat asks nothing before it starts
                    _info = {"messages": sessions[0].get("messages", 0),
                             "tokens_est": sessions[0].get("messages", 0) * 250,
                             "ctx": governor.ctx if governor else 100000}
                    _dlg = resume_tradeoff(_info)
                    if _dlg:
                        print(ui.muted(f"  · {_dlg}"))
                        _mode = input(ui.muted("  (Enter = full, c = compact) ")).strip().lower()
                        if _mode == "c":
                            state["resume_compact"] = True
            except Exception:
                pass
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
    # live activity, approvals as cards. AURA_UI=classic or a pipe keeps the
    # plain screen below.
    if _use_tui_now:
        try:
            from .tui.app import ChatApp
            app = ChatApp(eng=eng, loop=loop, m=m, profile=profile,
                          session=session, state=state,
                          open_session_settings=_open_session_settings,
                          defaults_take=_defaults_take, version=_version(),
                          task=task_intro or None,
                          pon=state.get("_pon_card", ""))
            return app.run()
        except Exception as _fe:            # noqa: BLE001
            # never a silent fallback: say why, then use the plain screen
            import traceback
            try:
                with open(eng / "ui_fallback.log", "a", encoding="utf-8") as _ff:
                    _ff.write(traceback.format_exc() + "\n")
            except OSError:
                pass
            ui.warn(f"the chat screen failed ({_fe}) — using the plain one; "
                    f"details in {eng / 'ui_fallback.log'}")
    _dock.install()
    # 3.9: remote-attach queue — tasks POSTed to `aura serve api/task` drain
    # here as visible queued inputs (pending_input path)
    remote_q = eng / ".remote-tasks"
    if remote_q.exists():
        pending_remote: list[str] = []
        for raw in remote_q.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                t = json.loads(raw).get("task", "")
                if t:
                    pending_remote.append(t)
            except json.JSONDecodeError:
                continue
        if pending_remote:
            remote_q.unlink()
            pending_input.extend(pending_remote)
            ui.info(f"{len(pending_remote)} remote task(s) queued from "
                    f"aura serve")

    # Facts are tier-tagged — the banner never asserts unchecked. Legacy banner
    # = never-fail fallback.
    _facts: list[BannerFact] = []
    try:
        from .contracts.cold_open import BannerFact, render_cold_open
        _facts.append(BannerFact("endpoint", profile.base_url, "PROBED",
                                 f"model answers · {profile.model}", ok=True))
        _facts.append(BannerFact("profile", profile.name, "SNAPSHOT",
                                 "active profile"))
        _facts.append(BannerFact("model", profile.model, "SNAPSHOT",
                                 f"{getattr(profile, 'context_length', 0) or '?'} ctx"))
        _facts.append(BannerFact("tools", f"{len(m.tools)} registered",
                                 "PROBED", "timeouts enforced"))
        print(render_cold_open(width=ui._W or 76,
                               version=_version(), profile=profile.name,
                               model=profile.model,
                               endpoint=profile.base_url,
                               tools=len(m.tools), facts=_facts))
    except Exception:
        # legacy banner is the never-fail fallback
        if not _facts:
            ui.banner(profile=profile.name, model=profile.model,
                      endpoint=profile.base_url, version=_version(),
                      tools=len(m.tools))
    # Arrival router — one question (first) or state summary (returning),
    # rendered right after the banner, before any prompt. Skipped entirely when
    # a task was passed (the task IS the answer).: returning arrivals get the
    # Previously-On SBAR card first. Order law (learned in visual pass):
    # endpoint health BEFORE the question — "the header never lies about what
    # will answer".
    _endpoint_note = ""
    try:
        from .setup import _fetch_models
        _ok, _models, _err, _code = _fetch_models(profile.base_url,
                                                  profile.api_key)
        if _ok:
            _endpoint_note = ui.ok(f"endpoint live: {len(_models)} model(s)")
        elif _code in (401, 403):
            _endpoint_note = ui.warn("endpoint needs an API key — /connect "
                                     "to switch, or aura auth list")
        else:
            from .setup import scan_endpoints
            live, walled = scan_endpoints()
            if live:
                alt = ", ".join(f"{f['name']} ({len(f['models'])} models)"
                                for f in live[:3])
                _endpoint_note = ui.warn(
                    f"configured endpoint unreachable — live alternatives: "
                    f"{alt}  (/connect to switch)")
            else:
                _endpoint_note = ui.warn(
                    "endpoint unreachable — no local alternatives found\n"
                    "  start Ollama/LM Studio or check the URL")
    except Exception:
        pass  # Banner hint is best-effort; never block chat start

    _pon = True   # R: the card was already rendered in the startup gate
    if not task_intro:
        # DEDUPE: the cold-open banner already probed and showed endpoint state
        # — the arrival card's endpoint line is redundant when healthy. Render
        # the arrival card WITHOUT its endpoint line unless the endpoint has a
        # problem.
        try:
            from .arrival import detect, render_first_arrival, \
                render_returning_arrival
            _arr = detect(eng)
            if _arr.first_run:
                if _endpoint_note:
                    print(_endpoint_note)
                print(render_first_arrival(_arr))
            else:
                # Here render ONLY the arrival state — endpoint line only when
                # NOT ok (dead/auth-walled), since healthy state is in the
                # banner.
                _ret = render_returning_arrival(_arr)
                if _arr.endpoint_live:
                    _ret = "\n".join(l for l in _ret.splitlines()
                                     if not l.startswith("endpoint live:"))
                print(_ret)
            print()
        except Exception:
            pass  # Arrival must never block chat start
    elif _endpoint_note:
        print(_endpoint_note)
        print()
    # First-run block REMOVED.
    from .contracts.visual2 import fg as _vfg
    _rail_c = "#5c9cf5"
    print("  " + _vfg("▐", _rail_c) + " " + ui.dim(f"notes      {eng}"))
    print("  " + _vfg("▐", _rail_c) + " " + ui.dim(f"session    {session.path.name}"
                 + (f"  · instructions: {loop.agents_md}"
                    if getattr(loop, "agents_md", None) else "")))
    print("  " + _vfg("─" * (ui._W or 76), ui.C.get("rule", 110)))
    for hl in startup_hints():
        print(ui.dim(f"  {hl}"))
    # 4.8: terminal title sync (claude-code pattern) — harmless when not a
    # real terminal
    try:
        if sys.stdout.isatty():
            sys.stdout.write(f"\x1b]0;Aura · {profile.name} · "
                             f"{profile.model}\x07")
            sys.stdout.flush()
    except Exception:
        pass

    # 2.3: TAB-completion for slash commands (readline best-effort)
    from .slash_complete import _readline_setup
    _readline_setup()

    # Resolution instrumentation (steps-to-resolution, local)
    from .resolution import ResolutionTracker
    _res = ResolutionTracker(eng)
    state["_res"] = _res

    # Leave-and-return — tracker marks engagement state now and after every
    # turn; a >1min gap at the next input prints one state-change line
    from .away_summary import AwayTracker
    _away = AwayTracker()

    def _away_mark() -> None:
        try:
            from .finding_card import findings_snapshot as _fsn
            _findings = len(_fsn(eng))
        except Exception:
            _findings = 0
        _hosts = 0
        try:
            import json as _j
            _gp = eng / "graph.json"
            if _gp.exists():
                _hosts = len(_j.loads(_gp.read_text(encoding="utf-8"))
                             .get("nodes", []))
        except Exception:
            _hosts = 0
        _blocked = 0
        try:
            _ap = eng / "audit.jsonl"
            if _ap.exists():
                with _ap.open(encoding="utf-8") as _f:
                    _blocked = sum(1 for _ln in _f if '"REFUSED' in _ln
                                   or '"blocked"' in _ln)
        except Exception:
            _blocked = 0
        _away.mark({"findings": _findings, "hosts": _hosts,
                    "blocked": _blocked})

    _away_mark()
    import time as _time_mod

    def show_context():
        if governor is None:
            ui.info("governor not wired"); return
        n = governor.count(session.messages)
        pct = int(100 * n / governor.ctx)
        remaining = max(0, governor.ctx - n)
        rem_pct = 100 * remaining // governor.ctx if governor.ctx else 0
        bar = "▰" * (pct // 5) + "▱" * (20 - pct // 5)
        src = "measured" if governor.measured else "estimated"
        # turn-forecast: local arithmetic from ledger velocity; huge counts
        # are noise — "500+" reads better than "~4097" (visual-pass lesson)
        per_turn = max(1, _tok_per_turn(session, governor))
        turns_left = remaining // per_turn if per_turn else 0
        turns_txt = f"~{turns_left}" if turns_left <= 500 else "500+"
        compacts = len(session.compacts)
        ui.info(f"context {bar} {n}/{governor.ctx} tokens ({pct}% used · "
                f"{rem_pct}% remaining, {src}) — {turns_txt} more turns "
                f"at this rate")
        ui.info("history re-enters every turn — long sessions cost more "
                "per turn")
        ui.info(f"old outputs trimmed by use from {governor.trim_trigger():,} tokens "
                f"(in batches) · summary at {int(governor.threshold * 100)}%"
                + (f" · {compacts} compact(s)" if compacts else ""))
        # What was trimmed, why, how to get it back; the cache the server
        # reports (measured, not assumed)
        _ws = getattr(loop, "_workset", None)
        if _ws is not None and _ws.trimmed:
            for ln in _ws.listing()[:16]:
                print(ui.muted("  " + ln))
        try:                    # What the model was checked to do
            from .model_facts import fact as _mf, model_key as _mk
            _k = _mk(loop.llm.p.base_url, loop.llm.p.model)
            _bits = [f"{lbl} {_mf(n, _k) or 'not checked'}" for n, lbl in (
                ("tools", "tools"), ("vision", "images"), ("thinking", "thinking"),
                ("caching", "server caching"), ("usage", "token counts"))]
            ui.info("model (aura eval model): " + " · ".join(_bits))
        except Exception:                                # noqa: BLE001
            pass
        _hit, _miss = getattr(loop, "cache_hits", 0), getattr(loop, "cache_misses", 0)
        if _hit or _miss:
            ui.info(f"server cache: {100 * _hit // max(1, _hit + _miss)}% of prompt "
                    f"tokens reused ({_hit:,} of {_hit + _miss:,})")
        _tl = getattr(loop, "tool_list", None)
        if _tl is not None:
            _sent, _all = _tl.chars()
            ui.info(f"tool list: {len(_tl.sent)} of {len(_tl.all)} tools sent — "
                    f"~{_sent // 4:,} of ~{_all // 4:,} tokens per call (/tools)")
        # Breakdown grid — what fills the window
        buckets: dict[str, int] = {"system (static)": 0, "system (injected)": 0,
                                   "history": 0, "tool results": 0}
        for i, m in enumerate(session.messages):
            t = governor.est_tokens_msg(m)
            if m["role"] == "system":
                key = "system (static)" if i == 0 else "system (injected)"
            elif m["role"] == "tool":
                key = "tool results"
            else:
                key = "history"
            buckets[key] = buckets.get(key, 0) + t
        total = sum(buckets.values()) or 1
        for key, val in buckets.items():
            share = 100 * val // total
            bar2 = "▰" * (share // 5) + "▱" * (max(1, 25 - share // 4))
            print(ui.muted(f"  {key:18s} {val:6d} tok  {bar2} {share}%"))
        hits = getattr(loop, "cache_hits", 0)
        misses = getattr(loop, "cache_misses", 0)
        if hits or misses:
            ui.info(f"cache hit ratio: {100 * hits / (hits + misses):.0f}% "
                    f"({hits}/{hits + misses} tokens) — measured")
        from .ctx_probe import describe_source
        ui.info(f"ctx window: {governor.ctx} "
                f"({describe_source(getattr(loop, 'ctx_source', None))}) — "
                f"ledger: {governor.token_ledger} tokens "
                f"({governor.measured} from provider usage)")

    # Gated calls ask the operator (only with a keyboard to answer)
    m.approver = _queued_approver(state) if sys.stdin.isatty() else None
    m.asker = _queued_asker(state) if sys.stdin.isatty() else None

    # the commands (chat_commands.py) — shared with the new chat screen
    from .chat_commands import ChatContext, dispatch as _dispatch

    def _classic_ask(prompt: str) -> str:
        try:
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    def _classic_choose(title: str, options: list):
        print(ui.accent(f"  {title}"))
        for _i, _o in enumerate(options, 1):
            _cur = " *" if len(_o) > 3 and _o[3] else ""
            _hint = f"  {_o[2]}" if len(_o) > 2 and _o[2] else ""
            print(f"   {_i}. {_o[0]}{_cur}" + ui.muted(_hint))
        _raw = _classic_ask("  number (Enter = cancel): ").strip()
        if _raw.isdigit() and 1 <= int(_raw) <= len(options):
            return options[int(_raw) - 1][1]
        return None

    _cctx = ChatContext(eng=eng, loop=loop, m=m, profile=profile,
                        session=session, state=state, ask=_classic_ask,
                        choose=_classic_choose, show_context=show_context,
                        open_session_settings=_open_session_settings,
                        defaults_take=_defaults_take, version=_version())

    while True:
        _sset.remember(loop, session, state)   # This session's settings
        if pending_input:
            line = pending_input.pop(0)      # Queued slash commands
        else:
            # Leave-and-return — >1min since last activity + state changed →
            # one state-change line before the prompt
            try:
                _gap = _time_mod.monotonic() - _away._ts
                if _gap >= 60:
                    from .away_summary import away_line
                    _away_now = {}
                    try:
                        from .finding_card import findings_snapshot as _fsn2
                        _away_now["findings"] = len(_fsn2(eng))
                    except Exception:
                        _away_now["findings"] = 0
                    _al = away_line(_away, _away_now, _gap)
                    if _al:
                        ui.info(_al)
            except Exception:
                pass
            try:
                # Pinned dock: the status bar and ► prompt are ONE dock, always
                # the bottom two lines. Content prints above; the dock redraws
                # via the stdout hook.
                from .console import status_bar as _sbar
                _proven = state.get("_proven_cache", 0)
                _lu = getattr(loop, "last_usage", None) or {}
                _tok = (f"↑{_hud._k(_lu.get('prompt', 0))} "
                        f"↓{_hud._k(_lu.get('completion', 0))}"
                        if (_lu.get("prompt") or _lu.get("completion")) else "")
                _dock.bar_text = _sbar(model=profile.model,
                            ctx_pct=int(100 * governor.count(
                                session.messages) / max(governor.ctx, 1))
                            if governor else None,
                            ctx_total=governor.ctx if governor else 0,
                            elapsed=_hud._fmt_elapsed(),
                            tokens=_tok,
                            proven=_proven,
                            target=getattr(_narrator, "last_target", ""))
            except Exception:
                pass
            try:
                # Bare input(colored) routes through readline, which miscounts
                # invisible chars (breaks backspace/line-edit) and on some
                # WSL/console builds strips ESC bytes → codes print as literal
                # text ('[38;5;110m[[0m…' garbage the user saw).
                from .console import prompt_line as _pline
                _proven = state.get("_proven_cache", 0)
                _dock.prompt_text = _pline(state.get("eng_name", "eng"),
                                           _proven)
                _dock.clear_for_input()
                # A pipe from PowerShell starts with an invisible byte-order
                # mark: "/research" arrived as "\ufeff/research" and went to
                # the model as text
                line = input().replace("\ufeff", "").strip()
                _away_mark()
            except (KeyboardInterrupt, EOFError):
                _dock.uninstall()
                print()
                ui.success("checkpoint saved — `aura resume` continues")
                return 0
            if line.endswith("\\") and not line.startswith("/"):
                # Multiline editor mode: trailing backslash continues
                from .onboard import read_multiline
                try:
                    line = read_multiline("you> ", line).strip()
                except (KeyboardInterrupt, EOFError):
                    _dock.uninstall()
                    print()
                    ui.success("checkpoint saved")
                    return 0
        if not line:
            continue
        # Slash commands show their description AS YOU TYPE. An unknown or
        # partial `/xyz` input prints matching commands with descriptions right
        # under the input (no help needed).
        if line.startswith("/") and " " not in line:
            from .slash_complete import popup_rows, exact_command_exists
            _rows = popup_rows(line, limit=8)
            _known = exact_command_exists(line)
            if not _known and _rows:
                print(ui.muted("\n".join(_rows)))
                print(ui.muted("  (type the full command, or /help for all)"))
            elif _known:
                pass  # Known command: descriptions live in help
            else:
                print(ui.muted(f"  unknown command {line} — try: /help"))
        # Every user action counts toward resolution
        if not line.startswith("/"):
            _res.mark_command(line)

        # Now it goes to a model measured to see images; otherwise Aura says
        # why it doesn't.
        if not line.startswith("/") and "data:image/" in line:
            from .onboard import detect_image_paste
            expanded_img = detect_image_paste(line, eng)
            if expanded_img:
                line = expanded_img
                import re as _re
                _ref = _re.search(r"\[image saved: ([^\]]+)\]", line)
                from .model_facts import fact as _mf, model_key as _mk
                _v = _mf("vision", _mk(loop.llm.p.base_url, loop.llm.p.model)) \
                    if getattr(loop, "llm", None) else None
                if _ref and _v == "yes":
                    loop.pending_images = [str(Path(eng) / _ref.group(1))]
                    ui.info("image attached — this model sees images (checked)")
                elif _v == "no":
                    ui.warn("this model can't see images (checked) — saved as "
                            f"{_ref.group(1) if _ref else 'a file'}; pick a model that can")
                else:
                    ui.warn("not known yet whether this model sees images — the image "
                            "was saved, not sent (check: aura eval model)")
        if not line.startswith("/") and "@" in line:
            from .onboard import expand_mentions
            if "@" in line:
                # @file names a file in YOUR project, not Aura's notebook
                line = expand_mentions(
                    line, getattr(loop, "workspace", None) or eng)

        # ---- slash commands: one implementation for both chat screens ---------
        if line.startswith("/"):
            _cctx.session, _cctx.profile = session, profile
            try:
                _res = _dispatch(_cctx, line)
            except Exception as _ce:            # noqa: BLE001
                ui.error(f"{line.split()[0]} failed: {_ce}")
                continue
            session, profile = _cctx.session, _cctx.profile
            if _res.exit:
                _dock.uninstall()
                return 0
            if not _res.task:
                continue
            line = _res.task

        if state.get("killed"):
            ui.warn(f"kill switch active — remove {eng}/.kill to continue")
            continue
        # Pre-Execution Ambiguity Guard (chat, L2/L3 only — L1 is direct mode;
        # ASPI lesson: clarify-state is an injection surface, so the question
        # is generated here, deterministically, and the ANSWER enters as a
        # normal user turn the mediator can audit)
        assist = getattr(m, "assist", "L2")
        if assist in ("L2", "L3"):
            from .intent import (AMBIGUITY_THRESHOLD, ambiguity_score,
                                 clarify_question)
            score, reasons = ambiguity_score(line)
            if score > AMBIGUITY_THRESHOLD and reasons:
                ui.warn(f"ambiguity {score} (> {AMBIGUITY_THRESHOLD}) — "
                        "clarifying before run")
                print(ui.muted(clarify_question(line, reasons)))
                try:
                    answer = input(ui.muted("  clarify> ")).strip()
                except (KeyboardInterrupt, EOFError):
                    continue
                if answer:
                    line = f"{line}\n[clarification] {answer}"
        # Anaphora clarify — a pronoun-heavy message the ledger cannot resolve
        # asks the operator HERE (deterministic, auditable) instead of the
        # model guessing which prior task "it" means.
        if assist in ("L2", "L3") and not line.startswith("/"):
            from .anaphora import resolve_reference
            ref = resolve_reference(line, eng)
            if (ref["anaphoric"] and not ref["binding"]
                    and not ref.get("closed_match")
                    and ref["candidates"]):
                ui.warn(f"unresolved reference — which task? "
                        f"candidates: {', '.join(ref['candidates'][:5])}")
                try:
                    answer = input(ui.muted("  which task> ")).strip()
                except (KeyboardInterrupt, EOFError):
                    answer = ""
                if answer:
                    line = f"{line}\n[clarification] task: {answer}"
        # Run the turn; lines typed during the run are queued and become
        # subsequent user turns (the queue reader collects them) (fix list #1:
        # the greeting fast path is gone — it answered "hi", "ok", "go on" and
        # "stop" with a bare model call: no system prompt, no tools, no audit,
        # and "go on" never continued the work. Every line now runs through the
        # loop; "hi" is still one call.)
        rec_holder: dict = {}
        # The user's turn opens with the you-gutter — the transcript reads as a
        # two-column chat, not bare lines.
        print("  " + ui.paint("▐", ui.C.get("label", 111)) + " "
              + ui.bold(ui.paint("you", ui.C.get("label", 111))) + "  "
              + line[:120])
        t = threading.Thread(
            target=lambda: rec_holder.update(
                rec=_run_turn(loop, session, state, line)), daemon=True)
        t.start()
        # Fix: join in 0.2s slices for up to 2s, exiting early the moment the
        # thread dies (which join already does) — the bug was the CONTINUE
        # re-checking is_alive before input; with a fast exit this loop now
        # ends promptly.
        import time as _time
        _t_turn0 = _time.monotonic()
        import sys as _sys
        _piped = not _sys.stdin.isatty()
        while t.is_alive():
            if _t_turn0 and _time.monotonic() - _t_turn0 < 2.0:
                t.join(timeout=min(0.2, 2.0 - _t_turn0 + 0.001))
                if not t.is_alive():
                    break
                continue
            if _piped:
                # Piped stdin — drain remaining lines WITHOUT prompts, deduped
                # (the user's "stop" ×4 was the queue replaying identical
                # lines).
                try:
                    for _ln in _sys.stdin:
                        _ln = _ln.replace("\ufeff", "").strip()
                        if _ln and (not queued or queued[-1] != _ln):
                            queued.append(_ln)
                except Exception:
                    pass
                break
            try:
                extra = input(ui.muted("  · queued> ")).replace("\ufeff", "").strip()
                if (extra or state.get("question")) and _answer_pending(state, extra):
                    continue                # An approval / question answer, not a steer
                if extra:
                    # Dedupe consecutive identical input — a repeated line is
                    # almost always the user retrying because the screen was
                    # busy, not a new instruction.
                    if not queued or queued[-1] != extra:
                        queued.append(extra)
                    if extra.startswith("/"):
                        continue    # Slash commands still queue for turn-end
                    sq = state.get("steer_queue")
                    if isinstance(sq, list):
                        sq.append(extra)
            except EOFError:
                _deny_pending(state)
                break               # Stdin closed — just wait for the turn
            except KeyboardInterrupt:
                print()
                _deny_pending(state)
                state["killed"] = True
                break
        t.join(timeout=1)
        rec = rec_holder.get("rec")
        # Fix:.last-state must update on EVERY turn — including dead-endpoint
        # turns where _run_turn returned None (rec None skipped the whole
        # post-turn block and the continuity card went stale). The session
        # knows its last task regardless of outcome.
        try:
            from .last_state import update_last_state as _uls
            from .finding_card import findings_snapshot as _fsn3
            __fs = _fsn3(eng)
            _uls(eng, {
                "last_session": session.path.name,
            "last_session_id": getattr(session, "id", ""),
                "last_task": session.last_user_task() or "",
                "last_action": (rec.content or "")[:120] if rec else "",
                "findings_total": len(__fs),
                "findings_proven": sum(1 for f in __fs if f.get("proof")),
            })
        except Exception:
            pass
        if rec is not None:
            if rec.error:
                ui.warn(f"stopped: {rec.error}")
            # Five-line receipt summary at the end of each turn
            if getattr(rec, "receipt_summary", ""):
                print()
                print(rec.receipt_summary)
            # Referee scorecard — independent check of the agent's completion
            # claims against disk. The "done" is a claim ABOUT the scorecard,
            # never a substitute for it.
            try:
                from .referee import check_task
                sc = check_task(eng, line, task_md=eng / "TASK.md")
                if sc.verdicts:
                    print(sc.render())
                    if not sc.all_good:
                        ui.warn("referee: not all deliverables verified")
            except Exception:
                pass  # Referee must never kill the turn
            # A streamed answer gets only the close — containment without
            # re-printing (guard).
            if rec.content and ui.supports():
                from .console import answer_card as _acard
                _w = ui._W if hasattr(ui, "_W") else 76
                if rec.streamed:
                    print()
                    print(_acard("", state=_turn_state(rec), width=_w)
                          .splitlines()[-1])   # Just the └── footer
                else:
                    print()
                    _checks = None
                    _rc = getattr(rec, "receipt", None)
                    if _rc:
                        _c = _rc.get("checks", {})
                        if _c:
                            _checks = (sum(1 for c in _c.values()
                                           if c.get("ok")), len(_c))
                    print(_acard(ui.render_markdown(rec.content),
                                 state=_turn_state(rec), width=_w,
                                 checks=_checks,
                                 target=getattr(_narrator,
                                                "last_target", "")))
            _post_turn_ui(session, eng, governor, loop, rec)
            _narrator.release_bar()   # Bar line becomes a log line
            # Refresh the proven cache for the stateful prompt
            try:
                from .finding_card import findings_snapshot as _fsn
                state["_proven_cache"] = sum(
                    1 for f in _fsn(eng) if f.get("status") == "validated")
            except Exception:
                pass
            _away_mark()   # Re-baseline after every turn
            print(ui.muted("  · done"))
        # Through the same slash dispatch as normal input (loop only takes
        # plain text). Simplest correct path. Only what the task never read
        # runs now, plus what it set aside as a separate request (loop._later);
        # commands still run from the queue
        _sq = state.get("steer_queue") if isinstance(state.get("steer_queue"), list) else []
        _unread, _later = list(_sq), list(getattr(loop, "_later", []) or [])
        _sq.clear()
        loop._later = []
        queued[:] = [q for q in queued if q.startswith("/")] + _unread + _later
        while queued:
            nxt = queued.pop(0)
            if nxt.startswith("/"):
                ui.info(f"· queued command → {nxt[:60]}")
                # slash dispatch is synchronous — reuse the main-loop handler
                pending_slash.append(nxt)
                continue
            ui.info(f"· queued → {nxt[:60]}")
            rec = _run_turn(loop, session, state, nxt)
            if rec is not None:
                if rec.error:
                    ui.warn(f"stopped: {rec.error}")
                _post_turn_ui(session, eng, governor, loop, rec)
        # process queued slash commands through the main loop's handler
        if pending_slash:
            pending_input = pending_slash + pending_input
            pending_slash = []
    return 0

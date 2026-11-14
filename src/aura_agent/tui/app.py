"""app.py — Aura's chat: input box, live activity, approvals, status line.

Threads: the main thread owns the terminal (keys in, drawing out). The
agent runs on a worker thread; everything it reports (stream text, tool
events, approval questions) arrives through one queue and is drawn by the
main thread, so nothing ever writes over anything else.

You can type while the agent works: Enter sends the message into the
running task (it is read at the agent's next step). Esc stops the task.
"""
from __future__ import annotations

import base64
import os
import shutil
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import ui
from ..chat_commands import ChatContext, dispatch
from . import cells, history
from .complete import FileIndex, Item, arg_items, command_items
from .editor import Editor
from .keys import Key
from .live import LiveRegion, LiveStdout

# keys that scroll the conversation in fullscreen (full.py)
_SCROLL_KEYS = ("pageup", "pagedown", "ctrl+home", "ctrl+end", "wheel_up", "wheel_down")
from .markdown import MarkdownStream
from .style import Style, detect_depth, guess_light_background, luminance_light
from .term import Terminal
from .text import cut, pad, width, wrap

RUNNING_OK = {"/status", "/context", "/todos", "/usage", "/help", "/keys",
              "/kill", "/steer", "/theme", "/debug", "/tools", "/memory", "/display",
              "/rules", "/scope", "/receipt", "/copy", "/agents", "/transcript", "/",
              # switches that take effect at once, on the running task too
              "/assist", "/plan", "/proof"}

MODES = [("L2", "act"), ("L1", "act"), ("L2", "plan"), ("L3", "act")]

SHORTCUTS = [
    ("Enter", "send · while Aura works: it reads your message at its next step"),
    ("Ctrl+Enter", "while Aura works: stop this step and read your message now"),
    ("Shift+Enter · Alt+Enter · Ctrl+J", "new line (or end a line with \\)"),
    ("Esc", "stop the task · close a menu"),
    ("Esc Esc", "empty box: edit an earlier message · text: clear it"),
    ("Shift+Tab", "how often Aura asks first (and plan mode)"),
    ("Tab", "complete / commands, @ files"),
    ("↑ ↓", "move in the box, then history (typed text = prefix search)"),
    ("Ctrl+R", "search everything you typed before"),
    ("Ctrl+O", "full output and diffs on/off"),
    ("Ctrl+T", "show/hide the plan"),
    ("Ctrl+G", "write the message in your editor"),
    ("Alt+V", "attach the clipboard's image (or drag the file in)"),
    ("Ctrl+S", "park the draft / bring it back"),
    ("Ctrl+A/E · Alt+B/F", "line start/end · word back/forward"),
    ("Ctrl+K/U/W · Ctrl+Y", "cut to end/start/word · paste the cut"),
    ("Ctrl+_", "undo typing"),
    ("Ctrl+L", "redraw the screen"),
    ("Ctrl+C", "stop · clear · twice to quit"),
    ("Ctrl+D", "quit (empty box, twice)"),
    ("!command", "run a shell command yourself; Aura sees the output"),
    ("@file", "add a file to the message"),
    ("/", "commands — /help lists them"),
]


SPLIT_MIN = 90       # The slash menu splits into list | details from this width


class ChatApp:
    def __init__(self, *, eng, loop, m, profile, session, state,
                 open_session_settings=None, defaults_take=None,
                 version: str = "", task: str | None = None, pon: str = ""):
        self.eng = Path(eng)
        self.loop, self.m = loop, m
        self.state = state
        self.version = version
        self.first_task = task
        self.pon = pon or ""
        self.term = Terminal()
        depth = detect_depth(isatty=True)
        unicode = True
        try:
            from ..accessibility import screen_reader
            if screen_reader():
                unicode = False
        except Exception:      # noqa: BLE001
            pass
        try:
            from ..paths import aura_home
            from .style import load_custom_themes
            load_custom_themes(aura_home() / "themes")
        except Exception:      # noqa: BLE001
            pass
        self.s = Style(self._initial_theme(), depth, unicode)
        self.region = LiveRegion(self.term)
        self.fullscreen = False           # Tui fullscreen (full.py)
        self.editor = Editor(history=history.load(self.eng))
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.turn_t0 = 0.0
        self.turn_tools = 0
        self.turn_steps = 0
        self.turn_tokens = [0, 0]
        self.chat_tokens = [0, 0]
        self.chat_cost: float | None = None   # Dollars, when the model has a price (aura model price)
        self.activity = ""
        self.reasoning = ""
        self.stream: MarkdownStream | None = None
        self.answer_started = False
        self.last_answer = ""
        self.last_tool_ev: dict | None = None
        self.before: dict = {}
        self.orig_files: dict = {}        # Path -> text before this chat first changed it
        try:                              # Focus (look.focus)
            from ..options import get as _oget
            self.focus = _oget("look.focus") == "on"
        except Exception:      # noqa: BLE001
            self.focus = False
        try:
            from ..options import get as _oget
            self.display = str(_oget("look.display") or "simple")
        except Exception:      # noqa: BLE001
            self.display = "simple"
        self.think_all = ""               # This thinking block, whole (shown when it ends)
        self.queued: list[str] = []      # Typed while working: read at the next step
        # messages that reached no task: each runs as its own turn, in the
        # order typed (never glued into one message)
        self.pending: list[str] = []
        self.later_cmds: list[str] = []
        self.verbose = False
        self.show_plan = True
        self.popup: list[Item] = []
        self.pop_i = 0
        self.pop_start = 0
        self.pop_hidden = False
        self.overlay: dict | None = None
        self.esc_at = 0.0
        self.ctrl_c_at = 0.0
        self.ctrl_d_at = 0.0
        self.hint = ""
        self.hint_until = 0.0
        self.quit = False
        self.spin_i = 0
        self.ctx_used = 0
        self.team_state: tuple | None = None    # This chat's task: (last kind, queued)
        self.t_start = time.monotonic()
        self.changed_files: set[str] = set()
        self.ctx_warned = False
        self.think_t0: float | None = None
        try:                       # Settings look.reduce_motion; AURA_REDUCE_MOTION overrides
            from ..options import get as _oget
            _still = bool(_oget("look.reduce_motion"))
        except Exception:          # noqa: BLE001
            _still = bool(os.environ.get("AURA_REDUCE_MOTION"))
        self.reduce_motion = _still or not unicode
        self.vim = None
        try:
            from ..config import load_saved_settings
            if os.environ.get("AURA_VIM") or load_saved_settings().get("ui_vim"):
                from .vim import Vim
                self.vim = Vim()
        except Exception:      # noqa: BLE001
            pass
        self.files = FileIndex(self.workspace)
        self.branch = _git_branch(self.workspace)
        self._todos_cache: tuple[float, list] = (0.0, [])
        self._overlay_cursor: tuple[int, int] | None = None
        self.ctx = ChatContext(
            eng=self.eng, loop=loop, m=m, profile=profile, session=session,
            state=state, ask=self.ask, choose=self.choose,
            show_context=self.show_context,
            open_session_settings=open_session_settings or (lambda s: None),
            defaults_take=defaults_take or (lambda *k: None),
            version=version, extra={
                "set_theme": self.set_theme,
                "themes": lambda: sorted(__import__("aura_agent.tui.style", fromlist=["THEMES"]).THEMES),
                "current_theme": lambda: self.s.theme_name,
                "chat_screen": True,
                "ask_secret": self.ask_secret,
                "team_panel": self._team_panel_toggle})

    # ----------------------------------------------------------- helpers --
    @property
    def workspace(self) -> Path:
        return Path(getattr(self.loop, "workspace", None) or self.eng)

    @property
    def w(self) -> int:
        return self.term.size()[0]

    @property
    def cw(self) -> int:
        """The conversation's width: the window, less the sidebar or the diff
        panel beside it in fullscreen (text is wrapped to fit, never cut)."""
        if getattr(self, "fullscreen", False):
            try:
                return self.region.chat_width()
            except Exception:      # noqa: BLE001
                pass
        return self.w

    def _initial_theme(self) -> str:
        env = os.environ.get("AURA_THEME", "")
        if env:
            return env
        try:
            from ..config import load_saved_settings
            saved = load_saved_settings().get("ui_theme")
            if saved:
                self._theme_saved = True
                return saved
        except Exception:      # noqa: BLE001
            pass
        light = guess_light_background()
        return "light" if light else "dark"

    def set_theme(self, name: str) -> bool:
        ok = self.s.set_theme(name)
        if ok:
            # Remembered for every chat
            try:
                from ..config import load_saved_settings, save_saved_settings
                save_saved_settings({**load_saved_settings(), "ui_theme": name})
                self._theme_saved = True
            except Exception:      # noqa: BLE001
                pass
            try:
                from ..themes import apply
                apply({"high-contrast": "mono"}.get(name, name)
                      if name in ("dark", "light", "colorblind", "high-contrast") else name)
            except Exception:      # noqa: BLE001
                pass
        return ok

    def say(self, lines: list[str]) -> None:
        self._flush_reads()
        self.region.print_above(lines)
        if lines:
            self._last_blank = not lines[-1].strip()

    def _gap(self) -> list[str]:
        """One blank line between "moments" (thinking, a group of actions,
        the answer) — never two."""
        self._flush_reads()
        return [] if getattr(self, "_last_blank", True) else [""]

    def _flush_reads(self) -> None:
        """Several reads in a row show as one row: "Read 4 files"."""
        batch = getattr(self, "read_batch", None)
        if not batch:
            return
        self.read_batch = []
        s, w = self.s, self.cw
        if len(batch) == 1:
            self.region.print_above(cells.tool_cell(batch[0], s, w, workspace=self.workspace))
        else:
            self.region.print_above(cells.read_group_cell(batch, s, w, self.workspace))
        self._last_blank = False

    def flash(self, text: str, secs: float = 2.5) -> None:
        self.hint, self.hint_until = text, time.monotonic() + secs

    # --------------------------------------------------------------- run --
    def run(self) -> int:
        self._real_stdout, self._real_stderr = sys.stdout, sys.stderr
        try:
            self.term.start()
            if not os.environ.get("AURA_THEME") and not getattr(self, "_theme_saved", False):
                self.term.query_background()
                if self.term.background_rgb is not None:
                    self.s.set_theme("light" if luminance_light(self.term.background_rgb) else "dark")
            proxy = LiveStdout(self.region)
            sys.stdout = proxy
            sys.stderr = proxy
            ui._W = self.w
            self._wire()
            try:
                from ..options import get as _oget
                if _oget("look.screen") == "fullscreen":
                    self.set_screen("fullscreen", seed=False)
            except Exception:      # noqa: BLE001 - the inline screen always works
                pass
            self._header()
            if any(m.get("role") == "user" for m in self.ctx.session.messages):
                self.say(self._recap_lines())      # A chat opened again: where it was left
            try:                                   # An update finished after Aura closed (Windows)
                from ..updater import last_result
                done = last_result()
                if done:
                    self.say([self.s("faint", "  " + done)])
            except Exception:      # noqa: BLE001 - never stops the chat from opening
                pass
            self.files.refresh(force=True)
            threading.Thread(target=self._check_endpoint, daemon=True).start()
            threading.Thread(target=self._check_update, daemon=True).start()
            threading.Thread(target=self._check_box, daemon=True).start()
            self._title()
            if self.first_task:
                self.submit(self.first_task)
            self.render()
            while not self.quit:
                self.step(timeout=0.08)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            self._shutdown()

    def _wire(self) -> None:
        prev = getattr(self.loop, "on_event", None)
        log = None
        try:
            from ..eventlog import EventLog
            log = EventLog(self.eng)
        except Exception:      # noqa: BLE001
            pass

        def on_event(kind, data):
            if log is not None:
                try:
                    log(kind, data)
                except Exception:      # noqa: BLE001
                    pass
            # capture a file's content before a write changes it (this runs
            # on the agent thread, before the tool executes)
            if kind == "tool_start" and isinstance(data, dict) and \
                    data.get("tool") in ("write_file", "edit_file"):
                # The diff panel compares with how the file was before this
                # chat first changed it
                p0 = str((data.get("args") or {}).get("path", ""))
                if p0 and p0 not in self.orig_files:
                    try:
                        self.orig_files[p0] = (self.workspace / p0).read_text(
                            encoding="utf-8", errors="replace")
                    except OSError:
                        self.orig_files[p0] = None
            if kind == "tool_start" and isinstance(data, dict) and \
                    data.get("tool") == "write_file":
                p = str((data.get("args") or {}).get("path", ""))
                try:
                    self.before[p] = (self.workspace / p).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    self.before[p] = None
            self.events.put(("event", kind, data))
        self.loop.on_event = on_event
        self.loop.stream_sink = lambda kind, text: self.events.put(("stream", kind, text))
        self.m.approver = self.approver
        self.m.asker = self.asker
        self.m.secret_asker = self.secret_asker
        self._hook_llm()
        del prev

    def _hook_llm(self) -> None:
        try:
            self.loop.llm.cancel_event = self.cancel
        except Exception:      # noqa: BLE001
            pass

    def _shutdown(self) -> None:
        if self.fullscreen:
            try:
                self.set_screen("inline")      # The conversation goes back to the scrollback
            except Exception:      # noqa: BLE001
                pass
        try:
            self.region.clear()
        except Exception:      # noqa: BLE001
            pass
        sys.stdout, sys.stderr = self._real_stdout, self._real_stderr
        self.term.write("\x1b]9;4;0\x07\x1b]0;\x07")
        self.term.stop()
        s = self.s
        try:
            from ..session_titles import session_title
            title = session_title(self.ctx.session.path.name)
        except Exception:      # noqa: BLE001
            title = ""
        bits = [cells.fmt_secs(time.monotonic() - self.t_start)]
        if self.chat_tokens[0] or self.chat_tokens[1]:
            bits.append(f"{s.g('up')}{cells.fmt_tokens(self.chat_tokens[0])} "
                        f"{s.g('down')}{cells.fmt_tokens(self.chat_tokens[1])} tokens")
        if self.chat_cost:
            bits.append(f"${self.chat_cost:.4f}" if self.chat_cost < 1 else f"${self.chat_cost:.2f}")
        if self.changed_files:
            n = len(self.changed_files)
            bits.append(f"{n} file{'s' if n != 1 else ''} changed")
        print(s("dim", "  " + (f"{title} · " if title else "") + " · ".join(bits)))
        cid = getattr(self.ctx.session, "id", "")
        print(s("faint", f"  `aura resume {cid}` continues this chat (#{cid}) · /undo reverts file changes"))

    # -------------------------------------------------------------- loop --
    def step(self, timeout: float = 0.08) -> None:
        try:
            key = self.term.keys.get(timeout=timeout)
        except queue.Empty:
            key = None
        self.drain()
        if key is not None:
            self.handle_key(key)
            # handle everything already typed before drawing (fast typing,
            # pastes on slow consoles)
            for _ in range(200):
                try:
                    k2 = self.term.keys.get_nowait()
                except queue.Empty:
                    break
                self.handle_key(k2)
        if key is None:
            self._drag_edge()              # Held still at an edge: keep scrolling
        self.spin_i += 1
        self._cron_check()
        self._waiting_check()
        self.render()

    def set_screen(self, mode: str, save: bool = False, seed: bool = True) -> str:
        """Inline (the terminal keeps the conversation) or fullscreen (full.py:
        the whole window, scrolled inside Aura). Switching keeps everything:
        fullscreen opens with the chat so far, inline gets it back."""
        from .full import FullRegion
        want = mode == "fullscreen"
        if want and getattr(self, "fullscreen", False) == want:
            return "already fullscreen"
        if want:
            try:
                from ..accessibility import screen_reader
                if screen_reader():
                    return "screen-reader mode keeps the inline screen"
            except Exception:      # noqa: BLE001
                pass
        alt = getattr(self.term, "alt_screen", None)
        try:                               # Settings look.mouse
            from ..options import get as _oget
            self.term.mouse_on = bool(_oget("look.mouse"))
        except Exception:      # noqa: BLE001
            pass
        if want:
            said = any(m.get("role") in ("user", "assistant") for m in self.ctx.session.messages)
            hist = self._transcript_lines() if said and seed else []     # A new chat: nothing to bring
            self.region.clear()
            if alt:
                alt(True)
            self.region = FullRegion(self.term, history=hist)
            if self._sidebar_on():
                self.region.side = self._sidebar
        else:
            hist = list(getattr(self.region, "history", []))
            if alt:
                alt(False)
            self.region = LiveRegion(self.term)
            self.region.print_above(hist[-2000:])
        self.fullscreen = want
        ui._W = self.cw
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, LiveStdout):
                stream.region = self.region
        if save:
            try:
                from ..options import set_
                set_("look.screen", "fullscreen" if want else "inline")
            except Exception:      # noqa: BLE001
                pass
        self.render()
        return ("fullscreen — PgUp/PgDn, the mouse wheel, Ctrl+Home/End scroll; drag to "
                "select (copied on release); /diff opens the changes beside the chat; "
                "/tui inline goes back" if want else
                "inline — the conversation is in your terminal's scrollback again")

    def _cron_check(self) -> None:
        """A scheduled job finished while the chat is open (the scheduler runs it
        in another process): say so, once.
        """
        now = time.monotonic()
        if now - getattr(self, "_cron_t", 0.0) < 30:
            return
        self._cron_t = now
        try:
            from ..cronops import load_jobs
            seen = getattr(self, "_cron_seen", None)
            cur = {j.get("id"): (j.get("last_run_ts"), j.get("last_status"), j.get("last_note"))
                   for j in load_jobs(self.eng)}
        except Exception:      # noqa: BLE001 - a broken jobs file never stops the chat
            return
        self._cron_seen = cur
        if seen is None:
            return                         # The first look: what was there before
        for jid, (ts, st, note) in cur.items():
            if ts and seen.get(jid, (None,))[0] != ts and note != "run from the chat":
                self.say(cells.notice_cell(
                    f"scheduled job {jid} ran: {st or '?'}" + (f" — {note}" if note else "")
                    + " (/cron)", self.s, self.cw, "warn" if st == "failed" else "info"))

    def drain(self) -> None:
        for _ in range(500):
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                self.on_ui_event(ev)
            except Exception as e:     # noqa: BLE001 - never kill the chat
                self.say(cells.notice_cell(f"display error: {e}", self.s, self.cw, "err"))

    # ------------------------------------------------------------ header --
    def _header(self) -> None:
        s, w = self.s, self.cw
        p = self.loop.llm.p
        gov = getattr(self.loop, "governor", None)
        try:
            from ..ctx_probe import is_guess
            guess = is_guess(getattr(self.loop, "ctx_source", None))
        except Exception:      # noqa: BLE001
            guess = False
        ctxs = (cells.fmt_tokens(gov.ctx) + (" (guessed)" if guess else "")) if gov else "?"
        proj = self.workspace.name or str(self.workspace)
        lines = [
            "",
            "  " + s.gradient("aura") + s("faint", f"  v{self.version}") + "  "
            + s("dim", "the agent that proves its work"),
            "  " + s("dim", "model ") + s("strong", p.model) + s("faint", f"  {_host(p.base_url)}")
            + s("dim", "   context ") + s("strong" if not guess else "warn", ctxs),
            "  " + s("dim", "project ") + s("strong", proj)
            + (s("faint", f"  {self.branch}") if self.branch else "")
            + s("faint", f"   {self.workspace}"),
        ]
        try:                                   # Which chat this is: new or resumed
            sess = self.ctx.session
            cid = getattr(sess, "id", "") or ""
            resumed = any(m.get("role") == "user" for m in sess.messages)
            title = ""
            if resumed:
                from ..session_titles import session_title
                title = session_title(sess.path.name) or ""
            lines.append("  " + s("dim", "chat    ") + s("strong", f"#{cid}" if cid else "this chat")
                         + s("faint", ("  resumed" + (f" · {title}" if title else ""))
                             if resumed else "  new"))
        except Exception:      # noqa: BLE001
            pass
        if self.pon:
            for ln in self.pon.strip().splitlines()[:4]:
                lines.append("  " + s("faint", cut(ln.strip(), w - 4)))
        lines.append("  " + s("faint", "? shortcuts · / commands · @ files · "
                                     "Shift+Tab asking level"))
        self.say(lines)

    def _check_endpoint(self) -> None:
        try:
            from ..setup import _fetch_models
            p = self.loop.llm.p
            ok, models, err, code = _fetch_models(p.base_url, p.api_key)
            if not ok:
                why = ("needs an API key" if code in (401, 403) else
                       "isn't answering")
                self.events.put(("notice", "warn",
                                 f"the model server at {_host(p.base_url)} {why} — "
                                 "/connect switches, `aura doctor` checks"))
        except Exception:      # noqa: BLE001
            pass

    def _check_box(self) -> None:
        """The Windows safe box, checked once (cached): what it can't run is
        said at the start, not found out mid-task."""
        try:
            from .. import sandbox_win as SW
            line = SW.limits()
            if line:
                self.events.put(("notice", "info", line))
        except Exception:      # noqa: BLE001
            pass

    def _check_update(self) -> None:
        """The daily update check — only with `updates.check` on (off by
        default); one quiet line, never an install."""
        try:
            from ..updater import daily_notice
            line = daily_notice()
            if line:
                self.events.put(("notice", "info", line))
        except Exception:      # noqa: BLE001
            pass

    def _title(self, busy: bool = False) -> None:
        name = self.workspace.name or "aura"
        self.term.write(f"\x1b]0;{'◐ ' if busy else ''}Aura · {name}\x07")
        self.term.write("\x1b]9;4;3;0\x07" if busy else "\x1b]9;4;0\x07")
        self.term.flush()

    # ------------------------------------------------------------ events --
    def on_ui_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "stream":
            self._on_stream(ev[1], ev[2])
        elif kind == "event":
            self._on_agent_event(ev[1], ev[2] if isinstance(ev[2], dict) else {})
        elif kind == "approval":
            self._flush_answer()
            self.overlay = {"kind": "approval", "req": ev[1], "slot": ev[2],
                            "i": 0, "reason": None}
            self._notify("Aura is waiting for your approval")
        elif kind == "secret":
            self._flush_answer()
            self.overlay = {"kind": "ask", "prompt": ev[1], "ed": Editor(), "answer": None,
                            "secret": True, "slot": ev[2]}
            self._notify("Aura needs a token")
        elif kind == "clarify":
            self._flush_answer()
            opts = list(ev[2] or [])
            self.overlay = {"kind": "clarify", "q": ev[1], "opts": opts,
                            "slot": ev[3], "i": 0,
                            "multi": bool(ev[4]) if len(ev) > 4 and opts else False,
                            "picked": set(),
                            "typing": Editor() if not opts else None}
            self._notify("Aura has a question")
        elif kind == "turn_done":
            self._turn_done(ev[1], ev[2])
        elif kind == "notice":
            self.say(cells.notice_cell(ev[2], self.s, self.cw, ev[1]))
        elif kind == "shell_done":
            self._shell_done(*ev[1:])

    def _on_stream(self, kind: str, text) -> None:
        if kind != "reasoning":
            self._end_thinking()
        if kind == "content" and text:
            if self.stream is None:
                self.stream = MarkdownStream(self.s, lambda: self.cw - 3)
            self.reasoning = ""
            self.activity = "writing"
            self.last_answer += text
            self._emit_answer(self.stream.feed(text))
        elif kind == "reasoning" and text:
            if self.think_t0 is None:
                self.think_t0 = time.monotonic()
                self.think_all = ""
            self.reasoning = (self.reasoning + text)[-400:]
            self.think_all = (self.think_all + text)[-20000:]
            self.activity = "thinking"
        elif kind == "tool_call" and isinstance(text, dict):
            verb, _ = cells.tool_title(text.get("name", ""), {})
            self.activity = f"preparing {verb.lower()}"

    def _end_thinking(self) -> None:
        """The model's thinking, in order with what it did: gray italic with a
        bar, the last lines of it (all of it in 'everything' or Ctrl+O)."""
        if self.think_t0 is None:
            return
        secs = time.monotonic() - self.think_t0
        self.think_t0 = None
        text, self.think_all = self.think_all.strip(), ""
        s = self.s
        if not text:
            if secs >= 2:
                self._flush_answer()
                self.say([s("faint", f"{s.g('think')} thought for {cells.fmt_secs(secs)}",
                            italic=True)])
            return
        self._flush_answer()
        from .text import wrap_text
        lines = wrap_text(" ".join(text.split()), max(20, self.cw - 4), indent="",
                          first_indent="")
        full = self.verbose or self.display == "everything"
        keep = lines if full else _think_lines(text, max(20, self.cw - 4), 6)
        head = s("accent", s.g("think")) + " " + s("dim", "Thinking", italic=True) + (
            s("faint", f"  {cells.fmt_secs(secs)}") if secs >= 2 else "")
        out = self._gap() + [head]
        if len(keep) < len(lines):
            out.append(s("faint", s.g("bar")) + " " + s("faint", "…", italic=True))
        out += [s("faint", s.g("bar")) + " " + s("dim", ln, italic=True) for ln in keep]
        self.say(out + [""])

    def _emit_answer(self, lines: list[str]) -> None:
        if not lines:
            return
        out = []
        for ln in lines:
            if not self.answer_started and ln.strip():
                out += self._gap()
                out.append(self.s("assistant", self.s.g("bullet")) + " " + ln)
                self.answer_started = True
            else:
                out.append("  " + ln if ln else "")
        self.say(out)

    def _flush_answer(self) -> None:
        if self.stream is not None:
            self._emit_answer(self.stream.finish())
            self.stream = None
            if self.answer_started:
                self.say([""])
            self.answer_started = False

    def _on_agent_event(self, kind: str, d: dict) -> None:
        s, w = self.s, self.cw
        if kind == "llm_start":
            self.turn_steps += 1
        if kind == "tool_start":
            self._end_thinking()
            self._flush_answer()
            verb, target = cells.tool_title(d.get("tool", ""), d.get("args") or {})
            self.activity = f"{verb} {target}".strip()
            self.turn_tools += 1
        elif kind == "tool_done":
            path = str((d.get("args") or {}).get("path", ""))
            before = self.before.pop(path, None) if d.get("tool") == "write_file" else None
            verbose = (self.verbose or self.state.get("debug_raw", False)
                       or self.display == "everything")
            if self.focus and d.get("status") == "ok" and not verbose:
                pass                 # Focus: no tool rows (the footer counts them)
            elif d.get("tool") in ("tools_find",) and d.get("status") == "ok" and not verbose:
                pass                 # Aura's own plumbing: not shown (Ctrl+O shows it)
            elif d.get("status") == "ok" and not verbose and self.display == "simple" and \
                    cells.kind_of(d.get("tool", ""), d.get("args") or {}, d.get("blast", "")) \
                    == "explored" and d.get("tool") not in ("web_crawl", "web_fetch",
                                                              "web_search", "browser_run"):
                if not hasattr(self, "read_batch"):
                    self.read_batch = []
                self.read_batch.append(d)
            else:
                lines = cells.tool_cell(d, s, w, verbose=verbose, before=before,
                                        workspace=self.workspace)
                self.say(lines)
            self.last_tool_ev = dict(d, _before=before)
            self.activity = "thinking"
            if d.get("tool") in ("write_file", "edit_file", "list_files"):
                self.files.refresh(force=True)
            if d.get("tool") in ("write_file", "edit_file") and d.get("status") == "ok":
                self.changed_files.add(path)
                if getattr(self.region, "panel", None) is not None:
                    self.region.panel_version += 1     # The diff panel shows it
            if d.get("tool") == "todo_write":
                self._todos_cache = (0.0, [])
        elif kind == "llm_usage":
            pass                     # Counted once, at llm_done
        elif kind == "llm_done":
            u = d.get("usage") or {}
            pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
            self.turn_tokens[0] += pt
            self.turn_tokens[1] += ct
            self.chat_tokens[0] += pt
            self.chat_tokens[1] += ct
            _det = u.get("prompt_tokens_details") or {}
            _cached = int((_det.get("cached_tokens") if isinstance(_det, dict) else 0)
                          or u.get("cache_read_input_tokens") or 0)
            self.chat_cached = getattr(self, "chat_cached", 0) + _cached
            # money, from the server's own counts and the user's price for
            # this model — never guessed (no price → not shown)
            try:
                from .. import model_facts as _MF
                c = _MF.call_cost(_MF.model_key(self.loop.llm.p.base_url, self.loop.llm.p.model), u)
                if c is not None:
                    self.chat_cost = (self.chat_cost or 0.0) + c
            except Exception:      # noqa: BLE001
                pass
        elif kind == "ctx_state":
            _before = int(getattr(self, "ctx_used", 0) or 0)
            self.ctx_used = int(d.get("used") or 0)
            if _before >= 4000 and self.ctx_used < 0.7 * _before:
                why = getattr(self, "_ctx_why", "") or "older parts left out of this call"
                self._ctx_why = ""
                self.say(cells.notice_cell(
                    f"context {cells.fmt_tokens(_before)} → {cells.fmt_tokens(self.ctx_used)} "
                    f"— {why}", s, w, "info"))
            self.ctx_marks = (int(d.get("trim_at") or 0), int(d.get("compact_at") or 0))
            gov = getattr(self.loop, "governor", None)
            at = self.ctx_marks[1]
            if gov and gov.ctx and at and self.ctx_used > 0.9 * at:
                if not self.ctx_warned:
                    self.ctx_warned = True
                    self.say(cells.notice_cell(
                        f"context {100 * self.ctx_used // gov.ctx}% — Aura summarizes older "
                        f"messages at {100 * at // gov.ctx}% ({cells.fmt_tokens(at)}) on its "
                        "own; /compact does it now, /new starts fresh", s, w, "warn"))
            elif self.ctx_used < 0.7 * (at or gov.ctx if gov else 1):
                self.ctx_warned = False            # After a summary: warn again next time
        elif kind == "steer":
            items = d.get("items") or [{"kind": "change", "text": q} for q in self.queued[:1]]
            for it in items:
                if self.queued:
                    self.queued.pop(0)
                txt = str(it.get("text", ""))
                if it.get("kind") == "later":
                    self.pending.append(txt)
                    what = " a separate request — runs after this task: "
                elif it.get("kind") == "question":
                    what = " a question — answered, the task goes on: "
                else:
                    what = " sent to the task: "
                self.say(["  " + s("faint", s.g("queue") + what) + s("dim", cut(txt, w - 46))])
        elif kind == "notice":
            self._flush_answer()
            if str(d.get("text", "")).startswith("new task —"):
                self._ctx_why = "earlier tasks in this chat left out (not needed for this one)"
            self.say(cells.notice_cell(str(d.get("text", "")), s, w,
                                       "info" if d.get("calm") else "warn"))
        elif kind == "team":                   # Teams: a team card
            self._flush_answer()
            self.say(cells.team_cell(d, s, w))
            if d.get("kind") in ("started", "done", "collision", "blocked"):
                self.team_state = (d.get("kind"), bool(d.get("queued")))
        elif kind == "phase":
            name, det = str(d.get("name", "")), str(d.get("detail", ""))
            if name in ("context overflow", "proof", "compaction", "sources") or (
                    name in ("silent-skip guard", "reasoning-landing")
                    and (self.verbose or self.state.get("debug_raw"))):    # Internal: Ctrl+O
                self.say(cells.notice_cell(f"{name}: {det}" if det else name,
                                           s, w, "info"))
            elif name == "context trim" and (self.verbose or self.state.get("debug_raw")):
                self.say(cells.notice_cell(f"{name}: {det}" if det else name, s, w, "info"))
        elif kind == "compaction":
            self.say(cells.notice_cell("older messages summarized to make room "
                                       "(your rules are kept word for word)", s, w, "info"))
            self._ctx_why = "older messages summarized"
            self.ctx_used = 0                  # Re-measured now, not at the next reply
        elif kind == "reviewer_error":
            self.say(cells.notice_cell("the second-opinion reviewer failed: "
                                       + cut(str(d.get("error", "")), w - 40), s, w, "warn"))
        elif kind == "toolsets" and (self.verbose or self.state.get("debug_raw")):
            added, parked = d.get("added") or [], d.get("parked") or []
            if added or parked:
                self.say(cells.notice_cell(
                    "tools " + ", ".join(([f"loaded: {', '.join(map(str, added))}"] if added else [])
                                         + ([f"put away: {', '.join(map(str, parked))}"] if parked else []))
                    + f" ({d.get('sent', '?')} sent)", s, w, "info"))
        elif kind == "parallel_reads" and (self.verbose or self.state.get("debug_raw")):
            self.say(cells.notice_cell(f"{d.get('count', '?')} reads run at once", s, w, "info"))
        elif kind == "intent_drift" and not (self.verbose or self.state.get("debug_raw")):
            pass                     # The drift check is noisy; Ctrl+O shows it
        elif kind in ("loopwatch", "reviewer_ran", "intent_drift", "result_recorded"):
            try:
                from ..run_narrator import watch_note
                note = watch_note(kind, d)
            except Exception:      # noqa: BLE001
                note = None
            if note:
                title, det = note
                k = "ok" if kind == "result_recorded" and d.get("proven") else "info"
                self.say(cells.notice_cell(title + (f" — {det}" if det else ""), s, w, k))
        elif kind == "approval_needed":
            self.say(cells.notice_cell(
                f"{d.get('tool', 'a call')} needs approval and nobody can answer "
                "here — the task stops", s, w, "warn"))
        elif kind == "killed":
            self.activity = "stopping"

    # -------------------------------------------------------------- turn --
    def _prepare_send(self, ed) -> None:
        """What the box's chips become: a file's text (Office, PDF, CSV with a
        summary, a zip's list), an image for a model that sees images, a plain
        line for what the model can't use — and anything too big for the room
        left in the context is saved with the chat; the message names it and
        the model reads the parts.
        """
        if not ed.pastes and not ed.files:
            return
        from .. import attach as A
        gov = getattr(self.loop, "governor", None)
        free = max(0, gov._usable() - (self.ctx_used or 0)) if gov and gov.ctx else 0
        cid = getattr(self.ctx.session, "id", "chat")
        told: list[str] = []

        def aside(name: str, what: str, text: str) -> str:
            p = A.save_aside(self.eng, cid, name, text)
            try:
                self.m.outside_exact.add(p.resolve())      # The model may read exactly this file
            except Exception:      # noqa: BLE001
                pass
            told.append(f"{what}: too big for this message (~{A.tokens(text):,} tokens) — "
                        "saved with the chat; Aura reads the parts it needs")
            return A.aside_ref(p, what, text)

        for n, text in list(ed.pastes.items()):
            if f"[Pasted text #{n} ·" in ed.text and free and A.tokens(text) > free * A.BIG_SHARE:
                ed.pastes[n] = aside(f"paste-{n}", f"pasted text #{n}", text)
        imgs = []
        for n, path in list(ed.files.items()):
            if f"[File #{n} ·" not in ed.text:
                continue
            d = A.describe(Path(path))
            if d["kind"] == "image":
                imgs.append(path)
                ed.file_text[n] = f"[image: {d['name']}]"
            elif d["ok"] and d["text"]:
                if free and A.tokens(d["text"]) > free * A.BIG_SHARE:
                    ed.file_text[n] = aside(Path(path).stem, f"file {d['name']} ({d['note']})", d["text"])
                else:
                    ed.file_text[n] = (f"[file {d['name']} — {d['note']}]\n```\n"
                                       f"{d['text']}\n```")
                    told.append(f"{d['name']}: {d['note']}")
            else:
                ed.file_text[n] = f"[file {d['name']} ({d['size']}) — {d['note']}]"
                told.append(f"{d['name']}: {d['note']}")
        if imgs:
            self.loop.pending_images = list(getattr(self.loop, "pending_images", None) or []) + imgs
        for line in told:
            self.say(cells.notice_cell(line, self.s, self.cw, "info"))

    def submit(self, text: str) -> None:
        """A finished message from the box (or a task from a command)."""
        stripped = text.strip()
        if not stripped:
            return
        history.append(self.eng, text)
        if stripped.startswith("/") and not stripped.startswith("//"):
            self.run_command(stripped)
            return
        if stripped.startswith("!") and len(stripped) > 1:
            self.run_shell(stripped[1:].strip())
            return
        if self.running:
            if self.pending:
                # earlier messages are waiting for their own turns: keep the order
                self.pending.append(stripped)
                self.flash(f"waiting — runs after {len(self.pending) - 1} earlier message"
                           + ("s" if len(self.pending) > 2 else ""))
                return
            self.queued.append(stripped)
            self.state.setdefault("steer_queue", []).append(stripped)
            self.flash("sent — Aura reads it at its next step")
            return
        self.start_turn(stripped)

    def start_turn(self, text: str, shown: str | None = None) -> None:
        try:                                    # Pasted text is never "the person's words"
            from .. import pblocks as _pb
            _pb.note_pasted(getattr(self.editor, "last_pasted", None) or [])
            _pb.CURRENT_TASK["text"] = text
        except Exception:      # noqa: BLE001
            pass
        if self.state.get("killed") and (self.eng / ".kill").exists():
            self.say(cells.notice_cell(f"the stop flag is set — remove "
                                       f"{self.eng / '.kill'} to run tasks", self.s, self.cw))
            return
        shown = shown or text
        if "@" in text:
            try:
                from ..onboard import expand_mentions
                text = expand_mentions(text, self.workspace)
            except Exception:      # noqa: BLE001
                pass
        self.say(cells.user_cell(shown, self.s, self.cw))
        # images: a dragged-in file path, or one taken with /image (pending)
        try:
            from ..image_paste import image_paths, vision_note
            imgs = list(getattr(self.loop, "pending_images", None) or []) + image_paths(text, self.workspace)
            if imgs:
                send, note = vision_note(self.loop)
                self.loop.pending_images = imgs if send else []
                self.say(cells.notice_cell(note, self.s, self.cw))
        except Exception:      # noqa: BLE001
            pass
        self.running = True
        self.cancel.clear()
        self.state["killed"] = False
        try:
            from ..sandbox_os import STOP
            STOP.clear()
        except Exception:      # noqa: BLE001
            pass
        try:                                   # What Aura held before (the check after the task)
            from ..machine import aura_bytes
            self._mem_before = aura_bytes()
        except Exception:      # noqa: BLE001
            self._mem_before = None
        self.turn_t0 = time.monotonic()
        self.turn_tools = 0
        self.turn_steps = 0
        self.turn_tokens = [0, 0]
        self.activity = "thinking"
        self.reasoning = ""
        self.last_answer = ""
        self.stream = None
        self.answer_started = False
        self._title(busy=True)
        session = self.ctx.session
        threading.Thread(target=self._turn_thread, args=(session, text),
                         daemon=True, name="aura-turn").start()

    def _turn_thread(self, session, text: str) -> None:
        rec, err = None, None
        try:
            rec = self.loop.run(session, text,
                                kill_check=lambda: bool(self.state.get("killed")),
                                steer_queue=self.state.setdefault("steer_queue", []))
        except KeyboardInterrupt:
            err = "stopped"
        except Exception as e:     # noqa: BLE001
            err = str(e) or type(e).__name__
        self.events.put(("turn_done", rec, err))

    def _title_later(self) -> None:
        """A 3–6 word title for a new chat, made once after its first
        exchange by the helper model (else a side call to the main one);
        never when the chat has one (yours from /rename wins)."""
        sess = self.ctx.session
        if getattr(sess, "title", "") or getattr(self, "_titling", False):
            return
        from ..notes import is_users_words, users_words
        first = next((users_words(m) for m in sess.messages if is_users_words(m)), "")
        if not first.strip():
            return
        self._titling = True

        def work():
            try:
                from ..helper import title_for
                t, who = title_for(first, main_llm=self.loop.llm)
                if t and not getattr(sess, "title", "") and sess is self.ctx.session:
                    sess.record_title(t, by=who)
            except Exception:      # noqa: BLE001 - a title never breaks the chat
                pass
            finally:
                self._titling = False
        threading.Thread(target=work, daemon=True).start()

    def _turn_done(self, rec, err) -> None:
        s, w = self.s, self.cw
        self._flush_answer()
        self._title_later()
        if rec is not None and not self.last_answer and getattr(rec, "content", ""):
            # nothing streamed (a server without streaming): show it now
            self.stream = MarkdownStream(s, lambda: self.cw - 3)
            self.last_answer = rec.content
            self._emit_answer(self.stream.feed(rec.content))
            self._flush_answer()
        elapsed = time.monotonic() - self.turn_t0
        stopped = bool(self.state.get("killed")) or self.cancel.is_set()
        problem = err or (getattr(rec, "error", None) if rec is not None else None)
        if problem and "kill switch" in str(problem):
            problem = None
            stopped = True
        parts = []
        if stopped:
            parts.append(s("warn", "stopped — tell Aura what to do instead"))
        if self.turn_tools:
            parts.append(f"{self.turn_tools} tool call{'s' if self.turn_tools != 1 else ''}")
        parts.append(cells.fmt_secs(elapsed))
        if self.turn_tokens[0] or self.turn_tokens[1]:
            parts.append(f"{s.g('up')}{cells.fmt_tokens(self.turn_tokens[0])} "
                         f"{s.g('down')}{cells.fmt_tokens(self.turn_tokens[1])}")
        proof = getattr(rec, "proof", None) if rec is not None else None
        if isinstance(proof, dict) and proof.get("status"):
            st = proof["status"]
            parts.append(s("ok", "checked: tests pass") if st in ("proven", "tests_pass", "passed")
                         else s("warn", f"checked: {st.replace('_', ' ')}"))
        missing = list(getattr(rec, "gate_missing", None) or []) if rec is not None else []
        if missing:
            parts.append(s("warn", "not written: " + ", ".join(missing)))
        if problem:
            self.say(cells.notice_cell(str(problem), s, w, "err"))
        self.say(cells.footer_cell(parts, s, ok=not problem and not stopped and not missing))
        # Plan mode: once the plan is given, back to act on its own — not when
        # the answer is a question back to you, or the turn stopped or failed
        if (getattr(self.m, "mode", "act") == "plan" and not problem and not stopped
                and self.last_answer.strip() and not self.last_answer.rstrip().endswith("?")):
            self.m.mode = "act"
            self.say(cells.notice_cell(
                "plan ready — back to act mode: say go to build it, or tell me what to "
                "change · /mode plan to keep planning", s, w, "info"))
        self.running = False
        self.activity = ""
        self.reasoning = ""
        self.cancel.clear()
        self.state["killed"] = False
        try:
            from ..sandbox_os import STOP
            STOP.clear()
        except Exception:      # noqa: BLE001
            pass
        self._title(busy=False)
        if elapsed > 20:
            self._notify("Aura finished" if not problem else "Aura stopped: " + str(problem)[:60])
        self._machine_check()
        # What was typed for the task but never reached it runs next — each
        # message as its own turn, in order
        left = [m for m in self.state.get("steer_queue", [])]
        self.state["steer_queue"] = []
        self.queued = []
        self.pending = left + self.pending
        try:
            from .. import session_settings
            session_settings.remember(self.loop, self.ctx.session, self.state)
        except Exception:      # noqa: BLE001
            pass
        cmds, self.later_cmds = self.later_cmds, []
        for c in cmds:
            self.run_command(c)
        if self.pending and not self.running:
            self.start_turn(self.pending.pop(0))

    def _machine_check(self) -> None:
        """After every task: code only, no model call. Memory low and Aura the
        cause → a card to stop what Aura started; low but not Aura → one line;
        fine → nothing.
        """
        try:
            from .. import machine as M
            r = M.after_task(getattr(self, "_mem_before", None))
        except Exception:      # noqa: BLE001
            return
        if not r:
            return
        if not (r["aura_is_cause"] and r["programs"]):
            self.say(cells.notice_cell(r["words"], self.s, self.cw, "warn"))
            return
        names = ", ".join(p["name"] for p in r["programs"][:4])
        pick = self.choose(r["words"] + " Stop what Aura started?", [
            ("Stop them", "stop", f"{names} — programs you asked to keep running stay", True),
            ("Keep going", "keep", "", False)])
        if pick != "stop":
            return
        stopped = []
        try:
            reg = getattr(self.m, "_procs", None)
            if reg is not None:
                reg.shutdown()
                stopped.append("background programs")
        except Exception:      # noqa: BLE001
            pass
        try:
            from ..tools import browser
            if browser._STATE.get("engine") is not None:
                browser.close()
                stopped.append("the browser")
        except Exception:      # noqa: BLE001
            pass
        try:
            from .. import lsp
            if lsp._CLIENTS:
                lsp.shutdown_all()
                stopped.append("language servers")
        except Exception:      # noqa: BLE001
            pass
        self.say(cells.notice_cell("stopped: " + (", ".join(stopped) or "nothing was running")
                                   + " — they start again when needed", self.s, self.cw, "ok"))

    def _waiting_check(self) -> None:
        """A question or approval left open: after 20 s a desktop notification
        (when the terminal isn't in front) — questions never expire, so they
        mustn't sit unnoticed.
        """
        ov = self.overlay
        if not ov or ov.get("kind") not in ("approval", "clarify"):
            return
        t0 = ov.setdefault("_opened", time.monotonic())
        if not ov.get("_told") and time.monotonic() - t0 > 20:
            ov["_told"] = True
            self._notify("Aura is waiting for your answer")

    def _notify(self, text: str) -> None:
        if self.term.focused:
            return
        safe = text.replace("\x07", "").replace("\x1b", "")
        self.term.write(f"\x07\x1b]777;notify;Aura;{safe}\x07\x1b]9;{safe}\x07")
        self.term.flush()

    def interrupt(self) -> None:
        if not self.running:
            return
        self.state["killed"] = True
        self.cancel.set()
        try:
            from ..sandbox_os import STOP
            STOP.set()                         # A running command ends now
        except Exception:      # noqa: BLE001
            pass
        self.activity = "stopping"
        self.flash("stopping…")

    # ---------------------------------------------------------- commands --
    def run_command(self, line: str) -> None:
        cmd = line.split()[0].lower()
        if cmd in ("/keys", "/shortcuts"):
            self.overlay = {"kind": "help"}
            return
        if cmd == "/transcript":
            self.overlay = {"kind": "transcript", "lines": self._transcript_lines(), "top": None}
            return
        if cmd == "/setup":
            # The setup board: every step on one screen, ready only after its
            # real check
            from .board import Board
            self.overlay = {"kind": "board", "b": Board(self)}
            return
        if cmd == "/copy":
            self.copy_last()
            return
        if cmd == "/usage":
            sent, got = self.chat_tokens
            cached = int(getattr(self, "chat_cached", 0) or 0)
            used, total = self._ctx_numbers()
            at = (getattr(self, "ctx_marks", (0, 0)) or (0, 0))[1] or int((total or 0) * 0.8)
            lines = [f"context now: {cells.fmt_tokens(used)} of {cells.fmt_tokens(total)} "
                     f"— Aura summarizes older messages at {cells.fmt_tokens(at)}"
                     if total else "context now: not measured yet",
                     f"this chat in total: sent {cells.fmt_tokens(sent)} tokens, got "
                     f"{cells.fmt_tokens(got)} — each call sends the conversation again, so "
                     "this grows much faster than the context"]
            if cached:
                lines.append(f"of what was sent, {cells.fmt_tokens(cached)} was repeated "
                             "context the server had cached (usually much cheaper)")
            if self.chat_cost:
                lines.append(f"cost so far: ${self.chat_cost:.4f}" if self.chat_cost < 1
                             else f"cost so far: ${self.chat_cost:.2f}")
            for ln in lines:
                self.say(cells.notice_cell(ln, self.s, self.cw, "info"))
            return
        if cmd == "/display":
            arg = (line.split()[1:] or [""])
            want = arg[0].lower()
            if want == "thinking" and len(arg) > 1 and arg[1].lower() in ("on", "off"):
                from ..options import set_
                set_("look.show_thinking", arg[1].lower() == "on")
                self.say(cells.notice_cell(
                    "thinking " + arg[1].lower() + " — " + (
                        "the model's thinking shows as it works" if arg[1].lower() == "on"
                        else "only what Aura does and its answer show"),
                    self.s, self.cw, "info"))
                return
            if want not in ("simple", "detailed", "everything"):
                self.say(cells.notice_cell(
                    f"display: {self.display} · /display simple — one line per action, "
                    "looking grouped, output hidden when it worked · /display detailed — "
                    "every action with its output · /display everything — all of it, with "
                    "tokens · /display thinking on|off · Ctrl+O peeks once", self.s, self.cw,
                    "info"))
                return
            self.display = want
            try:
                from ..options import set_
                set_("look.display", want)
            except Exception:      # noqa: BLE001
                pass
            self.say(cells.notice_cell(f"display: {want} — saved", self.s, self.cw, "info"))
            return
        if cmd == "/focus":
            self.focus = not self.focus
            try:
                from ..options import set_
                set_("look.focus", "on" if self.focus else "off")
            except Exception:      # noqa: BLE001
                pass
            self.say(cells.notice_cell(
                "focus on — your message, the answer and one line per task; tool rows hidden "
                "(failures still show) · /focus again to see them" if self.focus else
                "focus off — every tool call shows again", self.s, self.cw, "info"))
            return
        if cmd == "/sidebar":
            on = not self._sidebar_on()
            try:
                from ..options import set_
                set_("look.sidebar", "on" if on else "off")
            except Exception:      # noqa: BLE001
                pass
            if self.fullscreen:
                self.region.side = self._sidebar if on else None
                ui._W = self.cw
                self.region.render(force=True)
            narrow = on and self.fullscreen and self.term.size()[0] < 100
            self.flash("sidebar " + ("on" if on else "off")
                       + (" — shows on windows 100+ columns wide" if narrow else "")
                       + ("" if self.fullscreen else " — it shows in fullscreen (/tui fullscreen)"))
            return
        if cmd == "/diff" and self.fullscreen and len(line.split()) == 1:
            r = self.region
            r.panel = None if r.panel is not None else self._diff_panel
            ui._W = self.cw
            r.panel_version += 1
            self.flash("diff panel " + ("open — /diff closes it" if r.panel else "closed"))
            r.render(force=True)
            return
        if cmd == "/tui":
            arg = (line.split() + [""])[1].lower()
            if arg not in ("fullscreen", "inline"):
                self.say(cells.notice_cell(
                    f"screen: {'fullscreen' if self.fullscreen else 'inline'} — /tui fullscreen "
                    "(the whole window, scroll with PgUp/PgDn, the wheel, Ctrl+Home/End) · "
                    "/tui inline (your terminal's own scrollback)", self.s, self.cw, "info"))
                return
            self.say(cells.notice_cell(self.set_screen(arg, save=True), self.s, self.cw, "info"))
            return
        if cmd == "/statusline":
            self._statusline_cmd(line)
            return
        if cmd == "/vim":
            if self.vim is None:
                from .vim import Vim
                self.vim = Vim()
            else:
                self.vim = None
            try:
                from ..config import load_saved_settings, save_saved_settings
                save_saved_settings({**load_saved_settings(), "ui_vim": self.vim is not None})
            except Exception:      # noqa: BLE001
                pass
            self.say(cells.notice_cell("vim keys " + ("on — Esc for NORMAL mode, i to type"
                                                      if self.vim else "off"), self.s, self.cw, "info"))
            return
        if cmd in ("/clear",):
            self.term.write("\x1b[2J\x1b[3J\x1b[H")
            self.region.reset()
            self._header()
            return
        if self.running and cmd not in RUNNING_OK:
            self.later_cmds.append(line)
            self.flash(f"{cmd} runs when this task ends")
            return
        self.say([self.s("faint", self.s.g("arrow") + " " + line)])
        try:
            res = dispatch(self.ctx, line)
        except Exception as e:     # noqa: BLE001
            self.say(cells.notice_cell(f"{cmd} failed: {e}", self.s, self.cw, "err"))
            return
        finally:
            sys.stdout.flush()
        self._hook_llm()
        if self.ctx.extra.pop("session_switched", False):
            self.chat_tokens = [0, 0]
            self.chat_cached = 0
            self.chat_cost = None
            self.say(self._recap_lines())
        if res.exit:
            self.quit = True
        elif res.task:
            # the chat shows what you typed — a long task Aura wrote for the
            # model (/persona train, /teams wire) isn't shown as your message
            shown = res.shown or (res.task if len(res.task) <= 200 else line)
            self.start_turn(res.task, shown=shown)

    def run_shell(self, cmd: str) -> None:
        """!cmd — run it yourself (same safety checks as the agent's
        commands); the output lands in the chat so Aura sees it."""
        if self.running:
            self.flash("wait for the task to end, or Esc to stop it")
            return
        s = self.s
        self.say(["", s("tool_exec", "!", bold=True) + " " + s("strong", cmd)])
        self.running = True
        self.activity = f"running {cut(cmd, 40)}"
        self.turn_t0 = time.monotonic()

        def work():
            try:
                fn = self.m._fs["run_command"]
                self.m._by_user = True      # The user's own command: no outside-folder card
                try:
                    raw = self.m.execute("run_command", {"cmd": cmd}, fn)
                finally:
                    self.m._by_user = False
                self.events.put(("shell_done", cmd, str(raw), None))
            except Exception as e:     # noqa: BLE001
                self.events.put(("shell_done", cmd, "", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _shell_done(self, cmd: str, raw: str, err) -> None:
        self.running = False
        self.activity = ""
        ev = {"tool": "run_command", "args": {"cmd": cmd}, "blast": "execute",
              "status": "error" if err else "ok", "result": err or raw,
              "duration_ms": int((time.monotonic() - self.turn_t0) * 1000)}
        lines = cells.tool_cell(ev, self.s, self.cw, verbose=True)
        self.say(lines[1:] + [""])
        code, out, errout = cells.command_output(raw)
        body = (out + ("\n" + errout if errout else "")).strip()
        note = (f"I ran this command myself: `{cmd}`"
                + (f" (exit {code})" if code is not None else "")
                + (f"\n```\n{body[-4000:]}\n```" if body else "\n(no output)")
                if not err else f"I tried to run `{cmd}` myself: {err}")
        try:
            self.ctx.session.append("user", content=note)
        except Exception:      # noqa: BLE001
            pass

    def copy_last(self) -> None:
        text = self.last_answer.strip()
        if not text:
            self.flash("nothing to copy yet")
            return
        self._to_clipboard(text)
        self.flash(f"copied the last answer ({len(text):,} characters)")

    def _to_clipboard(self, text: str) -> None:
        """The terminal's clipboard (OSC 52 — works over SSH too) and, where
        there is one, the system's own clipboard tool."""
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.term.write(f"\x1b]52;c;{b64}\x07")
        self.term.flush()
        tool = (["clip"] if os.name == "nt" else ["pbcopy"] if sys.platform == "darwin" else
                ["wl-copy"] if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") else
                ["xclip", "-selection", "clipboard"] if shutil.which("xclip") else None)
        if tool:
            try:
                data = text.encode("utf-16-le") if os.name == "nt" else text.encode("utf-8")
                subprocess.run(tool, input=data, check=False, timeout=5)
            except Exception:      # noqa: BLE001
                pass

    def _mouse_select(self, k) -> None:
        """Fullscreen: drag to select, copied on release. A click without a drag
        selects nothing.
        """
        pos = (int(k.meta.get("x", 1)), int(k.meta.get("y", 1)))
        r = self.region
        if k.id == "mouse_down":
            self._sel_from, self._sel_last = pos, pos
            r.anchor(pos)
        elif k.id == "mouse_drag" and getattr(self, "_sel_from", None):
            self._sel_last = pos
            self._drag_edge()
            r.select_to(pos)
        elif k.id == "mouse_up" and getattr(self, "_sel_from", None):
            start, self._sel_from, self._sel_last = self._sel_from, None, None
            if pos == start:
                r.sel = None
                r.render(force=True)
                return
            r.select_to(pos)
            text = r.selected_text()
            if text:
                self._to_clipboard(text)
                self.flash(f"copied {len(text):,} characters")

    def _drag_edge(self) -> None:
        """A drag held at the chat's top or bottom edge scrolls it, a line a step.
        """
        pos = getattr(self, "_sel_last", None)
        if not (self.fullscreen and getattr(self, "_sel_from", None) and pos):
            return
        way = self.region.edge(pos)
        if way:
            self.region.scroll(way)
            self.region.select_to(pos)

    def _team_panel_toggle(self) -> bool:
        """/team board in fullscreen: the board beside the chat (again: off)."""
        if not self.fullscreen:
            return False
        r = self.region
        r.panel = None if getattr(r, "panel", None) == self._team_panel else self._team_panel
        r.panel_version += 1
        ui._W = self.cw
        self.flash("team board " + ("open — /team board closes it" if r.panel else "closed"))
        r.render(force=True)
        return True

    def _team_panel(self, width: int, height: int) -> list[str]:
        """The board: your open tasks (due, blocked), what the others are
        working on and the files they planned."""
        from ..editions import teams_present
        if not teams_present():
            return []
        from ..teams_ui import snapshot
        from .sidebar import _wrap
        s = self.s
        d = snapshot(self.ctx.session.path.stem)
        out = [s("heading", "team board", bold=True) + s("faint", "  · /team board closes")]
        if not d:
            return out + ["", s("faint", "not connected — /team connect <hub address>")]
        if d.get("online") is False:
            out += ["", s("warn", "the hub is offline — showing what was read last")]
        out += ["", s("heading", "YOURS", bold=True)]
        for t in d.get("mine") or []:
            marks = (["blocked"] if t.get("blocked") else []) + ([t["due"]] if t.get("due") else [])
            body = f"#{t['id']} {t['title']}" + (f"  ({'; '.join(marks)})" if marks else "")
            out += [s("text", ln) for ln in _wrap(body, width)]
        if not d.get("mine"):
            out.append(s("faint", "no open tasks"))
        out += ["", s("heading", "WORKING NOW", bold=True)]
        for w in d.get("working") or []:
            out += [s("accent", ln) for ln in _wrap(f"{', '.join(w['who'])} — #{w['id']} {w['title']}", width)]
            if w.get("files"):
                out += ["  " + s("faint", ln) for ln in _wrap(", ".join(w["files"]), width - 2)]
        if not d.get("working"):
            out.append(s("faint", "nobody else"))
        return out[:height]

    def _diff_panel(self, width: int, height: int) -> list[str]:
        """What this chat changed: each file with +/- counts and its diff
        against how it was before the chat first changed it (newest first)."""
        import difflib
        s = self.s
        out = [s("heading", "changes in this chat", bold=True) + s("faint", "  · /diff closes")]
        files = [f for f in reversed(list(self.orig_files)) if f in self.changed_files]
        if not files:
            return out + ["", s("dim", "nothing changed yet — edits show here as Aura makes them")]
        for f in files:
            old = (self.orig_files.get(f) or "").splitlines()
            try:
                new = (self.workspace / f).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                new = []
            diff = list(difflib.unified_diff(old, new, lineterm="", n=1))[2:]
            plus = sum(1 for x in diff if x.startswith("+"))
            minus = sum(1 for x in diff if x.startswith("-"))
            out += ["", s("strong", f) + "  " + s("diff_add", f"+{plus}") + " " + s("diff_del", f"-{minus}")]
            for x in diff:
                role = ("diff_add" if x.startswith("+") else "diff_del" if x.startswith("-")
                        else "faint" if x.startswith("@@") else "dim")
                out.append(s(role, cut(x, width)))
                if len(out) >= height:
                    return out[:height - 1] + [s("faint", "  … more below")]
        return out

    def show_context(self) -> None:
        s, w = self.s, self.cw
        gov = getattr(self.loop, "governor", None)
        if gov is None:
            self.say(cells.notice_cell("context isn't tracked here", s, w, "info"))
            return
        msgs = self.ctx.session.messages
        n = gov.count(msgs)
        buckets = {"instructions": 0, "notes added each step": 0,
                   "conversation": 0, "tool results": 0}
        for i, msg in enumerate(msgs):
            t = gov.est_tokens_msg(msg)
            if msg["role"] == "system":
                buckets["instructions" if i == 0 else "notes added each step"] += t
            elif msg["role"] == "tool":
                buckets["tool results"] += t
            else:
                buckets["conversation"] += t
        schema = int(getattr(gov, "schema_chars", 0) / max(1.0, gov._cpt()))
        buckets["tool descriptions"] = schema
        total = gov.ctx or 1
        sent = self.ctx_used or 0
        # What the model gets is what counts: the last call, then the whole
        # saved chat — old outputs are trimmed and earlier tasks this one
        # doesn't need are left out before sending
        lines = [s("strong", (f"context  {sent:,} of {total:,} tokens sent last call "
                              f"({100 * sent // total}%)") if sent else
                   f"context  ~{n:,} of {total:,} tokens (nothing sent yet)", bold=True)]
        if sent:
            lines.append(s("dim", f"  the saved chat is ~{n:,} tokens — old outputs are trimmed and "
                                  "earlier tasks not needed are left out before sending"))
        barw = max(10, min(40, w - 40))
        for k, v in buckets.items():
            fill = int(barw * v / total)
            lines.append(f"  {pad(k, 24)}{s('accent', s.g('bar_full') * fill)}"
                         f"{s('faint', s.g('bar_empty') * (barw - fill))}  {cells.fmt_tokens(v)}")
        try:
            from ..ctx_probe import describe_source
            lines.append(s("dim", "  window: " + describe_source(getattr(self.loop, "ctx_source", None))))
        except Exception:      # noqa: BLE001
            pass
        trim, comp = gov.trim_trigger(), int(gov._usable() * gov.threshold)
        lines.append(s("dim", f"  on its own: trims old tool output from {cells.fmt_tokens(trim)} "
                              f"({100 * trim // total}%), summarizes older messages from "
                              f"{cells.fmt_tokens(comp)} ({100 * comp // total}%) · "
                              f"{cells.fmt_tokens(total - gov._usable())} kept for the reply"))
        self.say(lines)

    # -------------------------------------------------------- approvals --
    def asker(self, question: str, options: list, multi: bool = False,
              free: bool = True) -> str:
        """Called on the agent thread by the clarify tool. multi: the user
        may pick several (Space or a number ticks one, Enter sends)."""
        slot = {"done": threading.Event(), "answer": ""}
        self.events.put(("clarify", question, options, slot, bool(multi)))
        slot["done"].wait()
        return slot["answer"]

    def secret_asker(self, prompt: str) -> str:
        """Called on the agent thread (team_token): the hidden box — what is
        pasted shows as dots and goes back to Aura's code, never the model."""
        slot = {"done": threading.Event(), "answer": ""}
        self.events.put(("secret", prompt, slot))
        slot["done"].wait()
        return slot["answer"]

    def _answer_clarify(self, answer: str) -> None:
        ov = self.overlay
        self.overlay = None
        s = self.s
        q = str(ov["q"])
        team = q.startswith("[team] ")
        lines = ["  " + s("accent", "▌TEAM" if team else "?", bold=True) + " "
                 + s("strong", q[len("[team] "):] if team else q)]
        lines.append("    " + (s("user", s.g("arrow") + " " + answer) if answer
                               else s("faint", "(no answer — Aura continues on its own)")))
        self.say(lines)
        ov["slot"]["answer"] = answer
        ov["slot"]["done"].set()

    def approver(self, req: dict) -> str:
        """Called on the agent thread when an action needs your OK."""
        slot = {"done": threading.Event(), "answer": "deny"}
        self.events.put(("approval", req, slot))
        slot["done"].wait()
        return slot["answer"]

    # The task pauses until you answer — the question never expires while
    # you're at the screen
    from ..approvals import WAIT_TTL_S as _WAIT
    approver.ttl_s = _WAIT

    def _approval_options(self, req) -> list[tuple[str, str]]:
        opts = [("once", "Yes")]
        if req.get("can_always"):
            opts.append(("always", "Yes, and don't ask again for "
                                   + cut(str(req.get("target", "this")), 40)))
        opts.append(("deny", "No — tell Aura what to do instead"))
        return opts

    def _answer_approval(self, answer: str, reason: str = "") -> None:
        ov = self.overlay
        if not ov:
            return
        req, slot = ov["req"], ov["slot"]
        if answer == "deny" and reason:
            req["deny_reason"] = reason
        slot["answer"] = answer
        self.overlay = None
        s = self.s
        verb, target = cells.tool_title(req.get("tool", ""), req.get("args") or {})
        what = f"{verb} {cut(target or str(req.get('target', '')), 60)}".strip()
        text = {"once": "",
                "always": s("ok", f"{s.g('tick')} allowed from now on: ") + s("dim", what),
                "deny": s("warn", f"{s.g('cross')} not allowed: ") + s("dim", what)
                + (s("faint", f" — “{reason}”") if reason else "")}[answer]
        if text:
            self.say(["  " + text])
        slot["done"].set()

    # ------------------------------------------------------ ask / choose --
    def _modal(self) -> None:
        while self.overlay is not None and not self.quit:
            self.step(timeout=0.08)

    def ask_secret(self, prompt: str) -> str:
        """Like ask, but what is typed shows as dots (a token, a password) —
        it is never drawn, never kept in the input history."""
        self.overlay = {"kind": "ask", "prompt": prompt, "ed": Editor(), "answer": None,
                        "secret": True}
        ov = self.overlay
        self._modal()
        return ov.get("answer") or ""

    def ask(self, prompt: str) -> str:
        self.overlay = {"kind": "ask", "prompt": prompt, "ed": Editor(), "answer": None}
        ov = self.overlay
        self._modal()
        return ov.get("answer") or ""

    def choose(self, title: str, options: list):
        """options: (label, value, hint, current). None = cancelled."""
        opts = list(options)
        cur = next((i for i, o in enumerate(opts) if len(o) > 3 and o[3]), 0)
        self.overlay = {"kind": "choose", "title": title, "opts": opts, "i": cur,
                        "filter": "", "value": None}
        ov = self.overlay
        self._modal()
        return ov.get("value")

    # -------------------------------------------------------------- keys --
    def handle_key(self, k: Key) -> None:
        if k.id == "resize":
            ui._W = self.cw
            self.region.resized()
            return
        if self.fullscreen and k.id in ("mouse_down", "mouse_drag", "mouse_up"):
            self._mouse_select(k)
            return
        if self.fullscreen and getattr(self.region, "sel", None) is not None \
                and k.id not in _SCROLL_KEYS:
            self.region.sel = None             # Any key: the selection goes
            self.region.render(force=True)
        if self.fullscreen and self.overlay is None and k.id in _SCROLL_KEYS:
            r = self.region
            {"pageup": lambda: r.page(1), "pagedown": lambda: r.page(-1),
             "ctrl+home": r.to_top, "ctrl+end": r.to_bottom,
             "wheel_up": lambda: r.scroll(3), "wheel_down": lambda: r.scroll(-3)}[k.id]()
            if getattr(self, "_sel_from", None) and getattr(self, "_sel_last", None):
                r.select_to(self._sel_last)       # The selection follows the scroll
            return
        if k.id in ("focus_in", "focus_out", "osc", "wheel_up", "wheel_down"):
            return
        if self.overlay is not None:
            self._overlay_key(k)
            return
        self._editor_key(k)

    def _overlay_key(self, k: Key) -> None:
        ov = self.overlay
        kind = ov["kind"]
        if kind == "help":
            self.overlay = None
            return
        if kind == "board":
            b = ov["b"]
            b.key(k)
            if b.closed:
                self.overlay = None
                if b.after:
                    self.run_command(b.after)
            return
        if kind == "transcript":
            n = len(ov["lines"])
            view = max(5, self.term.size()[1] - 4)
            top = ov["top"] if ov["top"] is not None else max(0, n - view)
            if ov.get("typing") is not None:
                ed: Editor = ov["typing"]
                if k.id == "enter":
                    ov["query"], ov["typing"] = ed.text.strip().lower(), None
                    self._transcript_jump(ov, top, 1, view, include=True)
                elif k.id == "escape":
                    ov["typing"] = None
                else:
                    _edit(ed, k)
                return
            ch = k.text if k.id == "char" else ""      # Typed letters arrive as Key("char", x)
            if ch == "/":
                ov["typing"] = Editor()
                return
            if ch in ("n", "N") and ov.get("query"):
                self._transcript_jump(ov, top, 1 if ch == "n" else -1, view)
                return
            step = {"up": -1, "down": 1, "pageup": -view, "pagedown": view,
                    "home": -n, "end": n}.get(k.id)
            if step is None:
                self.overlay = None              # Any other key closes
                return
            ov["top"] = max(0, min(max(0, n - view), top + step))
            return
        if kind == "approval":
            opts = self._approval_options(ov["req"])
            if ov.get("reason") is not None:
                ed: Editor = ov["reason"]
                if k.id == "enter":
                    self._answer_approval("deny", ed.text.strip())
                elif k.id == "escape":
                    ov["reason"] = None
                else:
                    _edit(ed, k)
                return
            if k.id in ("up", "ctrl+p"):
                ov["i"] = (ov["i"] - 1) % len(opts)
            elif k.id in ("down", "ctrl+n", "tab"):
                if k.id == "tab" and opts[ov["i"]][0] == "deny":
                    ov["reason"] = Editor()
                else:
                    ov["i"] = (ov["i"] + 1) % len(opts)
            elif k.id == "enter":
                choice = opts[ov["i"]][0]
                if choice == "deny":
                    ov["reason"] = Editor()
                else:
                    self._answer_approval(choice)
            elif k.id == "escape":
                self._answer_approval("deny")
            elif k.id == "ctrl+c":             # Ctrl+C: no, and stop the task
                self._answer_approval("deny")
                self.interrupt()
            elif k.id == "char" and k.text in "123456789":
                i = int(k.text) - 1
                if i < len(opts):
                    ov["i"] = i
                    if opts[i][0] == "deny":
                        ov["reason"] = Editor()
                    else:
                        self._answer_approval(opts[i][0])
            elif k.id == "char" and k.text.lower() in ("y", "a", "n"):
                want = {"y": "once", "a": "always", "n": "deny"}[k.text.lower()]
                if want == "deny":
                    ov["reason"] = Editor()
                elif any(o[0] == want for o in opts):
                    self._answer_approval(want)
            elif k.id == "shift+tab" and any(o[0] == "always" for o in opts):
                self._answer_approval("always")
            return
        if kind == "clarify":
            opts = ov["opts"]
            if ov.get("typing") is not None:
                ed: Editor = ov["typing"]
                if k.id == "enter":
                    self._answer_clarify(ed.text.strip())
                elif k.id == "escape":
                    if opts:
                        ov["typing"] = None
                    else:
                        self._answer_clarify("")
                else:
                    _edit(ed, k)
                return
            n = len(opts) + 1                     # "type your own answer"
            if ov.get("multi"):
                # several answers: Space / a number ticks one, Enter sends the ticked
                picked = ov["picked"]
                toggle = None
                if k.id == "char" and k.text == " " and ov["i"] < len(opts):
                    toggle = ov["i"]
                elif k.id == "char" and k.text.isdigit() and 0 < int(k.text) <= len(opts):
                    toggle = int(k.text) - 1
                if toggle is not None:
                    picked.symmetric_difference_update({toggle})
                    return
                if k.id == "enter" and ov["i"] < len(opts):
                    chosen = sorted(picked) or [ov["i"]]
                    self._answer_clarify("; ".join(opts[i] for i in chosen))
                    return
            if k.id in ("up", "ctrl+p"):
                ov["i"] = (ov["i"] - 1) % n
            elif k.id in ("down", "ctrl+n", "tab"):
                ov["i"] = (ov["i"] + 1) % n
            elif k.id == "enter":
                if ov["i"] < len(opts):
                    self._answer_clarify(opts[ov["i"]])
                else:
                    ov["typing"] = Editor()
            elif k.id == "escape":
                self._answer_clarify("")
            elif k.id == "ctrl+c":             # Ctrl+C: no answer, and stop the task
                self._answer_clarify("")
                self.interrupt()
            elif k.id == "char" and k.text.isdigit() and 0 < int(k.text) <= n:
                i = int(k.text) - 1
                if i < len(opts):
                    self._answer_clarify(opts[i])
                else:
                    ov["typing"] = Editor()
            elif k.id == "char":
                ov["typing"] = Editor()
                ov["typing"].insert(k.text)
            return
        if kind == "ask":
            ed = ov["ed"]
            if k.id == "enter":
                ov["answer"] = ed.text
                self.overlay = None
            elif k.id in ("escape", "ctrl+c"):
                ov["answer"] = ""
                self.overlay = None
            else:
                _edit(ed, k)
            if self.overlay is None and ov.get("slot") is not None:
                ov["slot"]["answer"] = ov["answer"] or ""   # The hidden box (team_token)
                ov["slot"]["done"].set()
                if ov.get("secret"):
                    self.say([self.s("faint", "  token pasted — stored in the key store"
                                     if ov["answer"] else "  no token pasted")])
            return
        if kind == "choose":
            items = self._filtered(ov)
            if k.id in ("up", "ctrl+p"):
                ov["i"] = max(0, ov["i"] - 1)
            elif k.id in ("down", "ctrl+n", "tab"):
                ov["i"] = min(len(items) - 1, ov["i"] + 1)
            elif k.id == "pageup":
                ov["i"] = max(0, ov["i"] - 8)
            elif k.id == "pagedown":
                ov["i"] = min(len(items) - 1, ov["i"] + 8)
            elif k.id == "enter":
                if items:
                    ov["value"] = items[min(ov["i"], len(items) - 1)][1]
                self.overlay = None
            elif k.id in ("escape", "ctrl+c"):
                self.overlay = None
            elif k.id == "backspace":
                ov["filter"] = ov["filter"][:-1]
                ov["i"] = 0
            elif k.id == "char":
                if not ov["filter"] and k.text.isdigit() and int(k.text) <= len(items) and int(k.text) > 0:
                    ov["value"] = items[int(k.text) - 1][1]
                    self.overlay = None
                else:
                    ov["filter"] += k.text
                    ov["i"] = 0
            return
        if kind == "search":
            ed: Editor = ov["ed"]
            if k.id == "ctrl+r":
                nxt = self.editor.search(ed.text, before=ov.get("hit"))
                if nxt is not None:
                    ov["hit"] = nxt
            elif k.id in ("enter", "tab", "right"):
                if ov.get("hit") is not None:
                    self.editor.set(self.editor.history[ov["hit"]])
                self.overlay = None
            elif k.id in ("escape", "ctrl+c", "ctrl+g"):
                self.overlay = None
            else:
                _edit(ed, k)
                ov["hit"] = self.editor.search(ed.text) if ed.text else None
            return
        if kind == "rewind":
            # list of earlier messages (choose-like)
            items = ov["items"]
            if k.id in ("up", "ctrl+p"):
                ov["i"] = max(0, ov["i"] - 1)
            elif k.id in ("down", "ctrl+n"):
                ov["i"] = min(len(items) - 1, ov["i"] + 1)
            elif k.id == "enter":
                seq, text = items[ov["i"]]
                self.overlay = None
                try:
                    self.ctx.session.restore(seq - 1)
                    self.say(cells.notice_cell(
                        "went back to before that message — edit it and press "
                        "Enter (file changes are not undone; /undo does that)",
                        self.s, self.cw, "info"))
                except Exception as e:     # noqa: BLE001
                    self.say(cells.notice_cell(f"couldn't go back: {e}", self.s, self.cw, "err"))
                self.editor.set(text)
            elif k.id in ("escape", "ctrl+c"):
                self.overlay = None
            return

    def _editor_key(self, k: Key) -> None:
        ed = self.editor
        kid = k.id
        now = time.monotonic()
        width_box = max(10, self.w - 5)
        if self.vim is not None and not (self.popup and not self.pop_hidden
                                         and kid in ("enter", "tab", "up", "down")):
            r = self.vim.handle(k, ed, width_box)
            if r == "submit":
                kid = "enter"
            elif r:
                self._update_popup()
                return
        if kid in ("enter", "char", "tab") and ed.pasting():
            # The rest of a paste that came in pieces: an Enter here is a new
            # line in it, not "send"
            ed.paste("\n" if kid == "enter" else "\t" if kid == "tab" else k.text)
            self._update_popup()
            return
        if kid == "enter":
            if self.popup and not self.pop_hidden:
                item = self.popup[self.pop_i]
                token, _ = ed.token_at_cursor()
                # a fully typed command runs; anything else takes the pick
                if not (item.kind == "cmd" and token.lower() == item.label):
                    self._accept_completion()
                    return
            if ed.text.endswith("\\") and ed.cursor == len(ed.text):
                ed.backspace()
                ed.insert("\n")
                return
            self._prepare_send(ed)
            text = ed.submit()
            self.popup = []
            self.submit(text)
            return
        if kid == "ctrl+enter" and self.running and ed.text.strip():
            # while Aura works: stop this step and read the message now (Enter
            # waits for the next step)
            self._prepare_send(ed)
            text = ed.submit()
            self.popup = []
            self.submit(text)
            if text.strip() in self.state.get("steer_queue", []):
                self.loop.urgent = True
                self.cancel.set()              # Cuts the reply in progress
                self.flash("sent — Aura stops this step and reads it now")
            return
        if kid in ("shift+enter", "alt+enter", "ctrl+j", "ctrl+enter"):
            ed.insert("\n")
            self._update_popup()
            return
        if kid == "tab":
            if self.popup and not self.pop_hidden:
                self._accept_completion()
            else:
                self.pop_hidden = False
                self._update_popup(force=True)
            return
        if kid == "shift+tab":
            self._cycle_mode()
            return
        if kid == "escape":
            if self.popup and not self.pop_hidden:
                self.pop_hidden = True
                return
            if self.running:
                self.interrupt()
                return
            if now - self.esc_at < 0.8:
                self.esc_at = 0.0
                if ed.text:
                    ed.history.append(ed.text)
                    ed.clear()
                    self.flash("cleared — ↑ brings it back")
                else:
                    self._open_rewind()
                return
            self.esc_at = now
            if ed.text:
                self.flash("Esc again to clear")
            else:
                self.flash("Esc again to edit an earlier message")
            return
        if kid == "ctrl+c":
            if self.running:
                self.interrupt()
                return
            if ed.text:
                ed.history.append(ed.text)
                ed.clear()
                self.popup = []
                return
            if now - self.ctrl_c_at < 2.0:
                self.quit = True
                return
            self.ctrl_c_at = now
            self.flash("Ctrl+C again to quit")
            return
        if kid == "ctrl+d":
            if ed.text:
                ed.delete()
                return
            if now - self.ctrl_d_at < 2.0:
                self.quit = True
                return
            self.ctrl_d_at = now
            self.flash("Ctrl+D again to quit")
            return
        if kid in ("up", "ctrl+p"):
            if self.popup and not self.pop_hidden:
                self.pop_i = (self.pop_i - 1) % len(self.popup)
                return
            if not ed.text and (self.pending or self.queued):
                if self.pending:
                    last = self.pending.pop()
                else:
                    last = self.queued.pop()
                    try:
                        self.state.get("steer_queue", []).remove(last)
                    except ValueError:
                        pass
                ed.set(last)
                self.flash("took back your last message")
                return
            if not ed.move_row(-1, width_box, 2, 2):
                ed.history_prev()
            return
        if kid in ("down", "ctrl+n"):
            if self.popup and not self.pop_hidden:
                self.pop_i = (self.pop_i + 1) % len(self.popup)
                return
            if not ed.move_row(1, width_box, 2, 2):
                ed.history_next()
            return
        if kid == "ctrl+r":
            self.overlay = {"kind": "search", "ed": Editor(), "hit": None}
            return
        if kid == "ctrl+o":
            self.verbose = not self.verbose
            self.flash("full output ON" if self.verbose else "full output OFF")
            if self.verbose and self.last_tool_ev:
                ev = self.last_tool_ev
                self.say([self.s("faint", "  (last tool call, in full)")]
                         + cells.tool_cell(ev, self.s, self.cw, verbose=True,
                                           before=ev.get("_before"),
                                           workspace=self.workspace))
            return
        if kid == "alt+p":
            self.run_command("/model")
            return
        if kid == "alt+v":                 # Copied files, else the clipboard's image
            from ..attach import clipboard_files
            files = [f for f in clipboard_files() if Path(f).is_file()]
            if files:
                for f in files[:10]:
                    ed.attach(f, Path(f).name)
                self.flash(f"{len(files)} file{'s' if len(files) != 1 else ''} attached")
                self._update_popup()
                return
            self.run_command("/image")
            return
        if kid == "alt+m":                 # Shift+Tab for consoles that eat it
            self._cycle_mode()
            return
        if kid == "ctrl+t":
            self.show_plan = not self.show_plan
            return
        if kid == "ctrl+l":
            self.term.write("\x1b[2J\x1b[H")
            self.region.reset()
            return
        if kid == "ctrl+g":
            self._external_editor()
            return
        if kid == "ctrl+s":
            what = ed.stash_toggle()
            if what:
                self.flash("draft parked — Ctrl+S brings it back" if what == "stashed"
                           else "draft back")
            return
        if kid == "ctrl+z" and os.name != "nt":
            self._suspend()
            return
        if kid == "char" and k.text == "?" and not ed.text:
            self.overlay = {"kind": "help"}
            return
        _edit(ed, k)
        self.pop_hidden = False if kid in ("char", "paste", "backspace") else self.pop_hidden
        self._update_popup()

    def _cycle_mode(self) -> None:
        cur = (getattr(self.m, "assist", "L2"), getattr(self.m, "mode", "act"))
        i = MODES.index(cur) if cur in MODES else 0
        assist, mode = MODES[(i + 1) % len(MODES)]
        self.m.assist, self.m.mode = assist, mode
        self.flash(_mode_words(assist, mode, long=True), 3)

    def _open_rewind(self) -> None:
        try:
            recs = self.ctx.session._records()
        except Exception:      # noqa: BLE001
            recs = []
        # The user's own messages only.
        from ..notes import is_users_words, users_words
        items = [(r.get("seq", 0), users_words(r)) for r in recs
                 if is_users_words(r)
                 and not users_words(r).startswith("I ran this command myself")]
        if not items:
            self.flash("no earlier messages in this chat")
            return
        items = items[-12:][::-1]
        self.overlay = {"kind": "rewind", "items": items, "i": 0}

    def _external_editor(self) -> None:
        ed = self.editor
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
            ("notepad" if os.name == "nt" else "vi")
        fd, path = tempfile.mkstemp(suffix=".md", prefix="aura-msg-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ed.expanded())
        self.region.clear()
        self.term.stop()
        try:
            subprocess.call(editor + f' "{path}"', shell=True)
            with open(path, encoding="utf-8") as f:
                text = f.read().rstrip("\n")
            ed.set(text)
        except Exception as e:     # noqa: BLE001
            self.flash(f"editor failed: {e}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            self.term.start()
            self._resume_screen()

    def _suspend(self) -> None:
        import signal
        self.region.clear()
        self.term.stop()
        os.kill(os.getpid(), signal.SIGTSTP)
        self.term.start()
        self._resume_screen()

    def _resume_screen(self) -> None:
        """Back from the editor or a suspend: fullscreen again if it was."""
        if self.fullscreen and getattr(self.term, "alt_screen", None):
            self.term.alt_screen(True)
            self.region.render(force=True)

    # -------------------------------------------------------- completion --
    def _update_popup(self, force: bool = False) -> None:
        ed = self.editor
        text = ed.text
        token, start = ed.token_at_cursor()
        items: list[Item] = []
        if text.startswith("/") and start == 0 and "\n" not in text:
            items = command_items(token, self.eng)
        elif text.startswith("/") and "\n" not in text and start > 0:
            first = text.split()[0].lower()
            if text[:start].strip() == first:
                items = arg_items(first, token)
        elif token.startswith("@"):
            self.files.refresh()
            items = self.files.match(token[1:])
        if not items and force and text.startswith("/") is False and token.startswith("@"):
            items = self.files.match("")
        self.popup = items[:50]
        self.pop_i = 0 if self.pop_i >= len(self.popup) else self.pop_i
        self.pop_start = start

    def _accept_completion(self) -> None:
        if not self.popup:
            return
        item = self.popup[self.pop_i]
        self.editor.replace_token(self.editor.token_at_cursor()[1], item.insert)
        if item.kind == "dir":
            self._update_popup()
        else:
            self.popup = []

    # ------------------------------------------------------------ render --
    def render(self) -> None:
        lines, cursor = self.build()
        self.region.render(lines, cursor)

    def build(self) -> tuple[list[str], tuple[int, int] | None]:
        s, w = self.s, self.w
        out: list[str] = []
        # 1. what is being written right now
        if self.running and self.stream is not None:
            pend = self.stream.pending()
            if pend:
                body = pend[-12:]
                for i, ln in enumerate(body):
                    if i == 0 and not self.answer_started and ln.strip():
                        out.append(s("assistant", s.g("bullet")) + " " + ln)
                    else:
                        out.append("  " + ln if ln else "")
        # 2. activity
        if self.running:
            spin = s.g("spin")
            frame = s.g("bullet") if self.reduce_motion else spin[self.spin_i % len(spin)]
            elapsed = cells.fmt_secs(time.monotonic() - self.turn_t0)
            act = self.activity or "working"
            if self.overlay is not None and self.overlay.get("kind") in ("approval", "clarify"):
                act = "paused — waiting for your answer"      # No expiry: it waits for you
            out.append(self._activity_line(frame, act, elapsed))
            if self.reasoning and self.activity == "thinking":
                # live thinking: its last lines, so it never looks frozen
                live = _think_lines(self.think_all, max(20, w - 6),
                                    None if self.verbose else 6)   # Ctrl+O: all of it
                for ln in live:
                    out.append("  " + s("faint", s.g("bar")) + " " + s("dim", ln, italic=True))
        # 3. queued messages
        for q in self.queued[-3:]:
            out.append("  " + s("faint", s.g("queue") + " waiting to be read: ")
                       + s("dim", cut(q.replace("\n", " "), w - 30)))
        for i, q in enumerate(self.pending[:3]):
            out.append("  " + s("faint", s.g("queue") + (" runs next: " if i == 0 else " then: "))
                       + s("dim", cut(q.replace("\n", " "), w - 22)))
        if len(self.pending) > 3:
            out.append("  " + s("faint", f"  … +{len(self.pending) - 3} more waiting"))
        # 4. the plan
        if self.show_plan:
            out.extend(self._plan_lines())
        # 5. menus above the box
        ov = self.overlay
        ov_start = len(out)
        self._overlay_cursor = None
        if ov is not None:
            out.extend(self._overlay_lines(ov))
        elif self.popup and not self.pop_hidden:
            out.extend(self._popup_lines())
        # 6. the box
        box, cur_row, cur_col = self._box_lines()
        box_top = len(out)
        out.extend(box)
        # 7. status line
        out.append(self._status_line())
        cursor = None
        if ov is None or ov.get("kind") == "help" or self._typing_field():
            cursor = (box_top + cur_row, cur_col)
        elif self._overlay_cursor is not None:
            # a menu with a text field (ask, search, a reason, an answer)
            oc = self._overlay_cursor
            cursor = (ov_start + oc[0], oc[1])
        return out, cursor

    def _plan_lines(self) -> list[str]:
        now = time.monotonic()
        if now - self._todos_cache[0] > 1.5:
            try:
                from ..todo import load_items
                items = load_items(self.eng, self.ctx.session.path.stem)
            except Exception:      # noqa: BLE001
                items = []
            self._todos_cache = (now, items)
        items = self._todos_cache[1]
        if not items:
            return []
        s, w = self.s, self.w
        open_ = [i for i in items if i.get("status") != "done"]
        if not open_ and not self.running:
            return []
        done_n = len(items) - len(open_)
        out = ["  " + s("faint", f"plan {done_n}/{len(items)}")]
        shown = items if len(items) <= 7 else (
            [i for i in items if i.get("status") == "done"][-2:] + open_[:5])
        for it in shown:
            st = it.get("status", "pending")
            text = str(it.get("text") or it.get("content") or "")
            if st == "done":
                out.append("  " + s("ok", s.g("done")) + " " + s("faint", cut(text, w - 8), strike=True))
            elif st in ("in_progress", "doing"):
                out.append("  " + s("accent", s.g("doing")) + " " + s("strong", cut(text, w - 8)))
            else:
                out.append("  " + s("dim", s.g("todo")) + " " + s("dim", cut(text, w - 8)))
        return out

    def _sidebar_on(self) -> bool:
        try:
            from ..options import get
            return get("look.sidebar") != "off"
        except Exception:      # noqa: BLE001
            return True

    def _sidebar(self, w: int, h: int) -> list[str]:
        """The fullscreen sidebar."""
        from .sidebar import sidebar_lines
        try:
            return sidebar_lines(self, w, h)
        except Exception as e:      # noqa: BLE001 - never break the screen
            return [self.s("faint", cut(f"sidebar: {e}", w))]

    def _popup_lines(self) -> list[str]:
        if self.w >= SPLIT_MIN and self.editor.text.startswith("/"):
            return self._popup_split()
        s, w = self.s, self.w
        n = len(self.popup)
        view = 8
        start = max(0, min(self.pop_i - view // 2, n - view))
        rows = self.popup[start:start + view]
        lab_w = min(max((width(i.label) for i in rows), default=10) + 2, max(16, w // 2))
        out = []
        for k, it in enumerate(rows):
            active = start + k == self.pop_i
            label = cut(it.label, lab_w - 1)
            hint = cut(it.hint, max(0, w - lab_w - 8))
            if active:
                out.append("  " + s("sel_fg", " " + pad(label, lab_w), bold=True, bg="sel_bg")
                           + s("dim", " " + hint))
            else:
                out.append("   " + s("accent" if it.kind == "cmd" else "text", pad(label, lab_w))
                           + s("faint", hint))
        if n > view:
            out.append("   " + s("faint", f"{n} matches · ↑↓ · Tab picks"))
        return out

    def _popup_split(self) -> list[str]:
        """The "/" menu on a wide window: the commands on the left, the picked one
        on the right — what it does, its state now, its options with the
        current one ticked, an example.
        """
        from .sidebar import command_detail
        s, w = self.s, self.w
        n, view = len(self.popup), 10
        start = max(0, min(self.pop_i - view // 2, n - view))
        rows = self.popup[start:start + view]
        # The names only: what each does is on the right in full
        foot = f"{n} matches · ↑↓ · Tab picks" if n > view else "↑↓ · Tab picks · Enter runs"
        lab_w = max([width(i.label) for i in self.popup] + [12]) + 2
        left_w = min(max(lab_w + 5, width(foot) + 4), max(24, w // 3))
        lab_w = min(lab_w, left_w - 5)
        left = []
        for k, it in enumerate(rows):
            active = start + k == self.pop_i
            label = it.label if width(it.label) <= lab_w else cut(it.label, lab_w)
            if active:
                left.append("  " + s("sel_fg", " " + pad(label, lab_w), bold=True, bg="sel_bg")
                            + " " * max(0, left_w - lab_w - 3))
            else:
                left.append("   " + s("accent" if it.kind == "cmd" else "text", pad(label, lab_w))
                            + " " * max(0, left_w - lab_w - 3))
        left.append("   " + s("faint", pad(cut(foot, left_w - 3), left_w - 3)))
        it = self.popup[self.pop_i] if self.popup else None
        name, picked = "", ""
        if it is not None:
            if it.kind == "cmd":
                name = it.label.split()[0]
            else:
                name = self.editor.text.split()[0].lower()
                picked = it.label
        right = command_detail(self, name, w - left_w - 4, max(len(left), view + 1),
                               picked) if name else []
        h = max(len(left), len(right))
        left += [" " * left_w] * (h - len(left))
        right += [""] * (h - len(right))
        bar = s("rule", "│")
        return [pad(l, left_w) + " " + bar + " " + r for l, r in zip(left, right)]

    def _transcript_jump(self, ov, top: int, way: int, view: int, include: bool = False) -> None:
        """Move to the next (way 1) / previous (-1) line holding the query."""
        from .text import strip_ansi
        lines, q = ov["lines"], ov.get("query") or ""
        idx = range(top if include else top + 1, len(lines)) if way > 0 else range(top - 1, -1, -1)
        hit = next((i for i in idx if q and q in strip_ansi(lines[i]).lower()), None)
        if hit is None:
            self.flash(f"'{q}' not found" + (" further down" if way > 0 else " further up"))
            return
        ov["top"] = max(0, min(hit, max(0, len(lines) - view)))
        ov["hit"] = hit

    def _recap_lines(self, last: int = 4) -> list[str]:
        """Where a chat was left: its name and id, then the last few exchanges —
        what you asked, how many tool calls, the answer (long ones cut;
        transcript shows everything).
        """
        from ..notes import is_users_words, users_words
        from ..session_store import chat_id
        from .text import wrap_text
        s, w = self.s, self.cw
        ex: list[dict] = []
        for m in self.ctx.session.messages:
            if is_users_words(m):
                ex.append({"you": users_words(m), "tools": 0, "answer": ""})
            elif m.get("role") == "assistant" and ex:
                ex[-1]["tools"] += len(m.get("tool_calls") or [])
                body = m.get("content")
                if isinstance(body, str) and body.strip():
                    ex[-1]["answer"] = body.strip()
        name = ""
        try:
            from ..chat_commands import _chat_name
            from ..session_store import list_sessions
            me = self.ctx.session.path.name
            info = next((x for x in list_sessions(self.eng) if x["file"] == me), None)
            name = _chat_name(info) if info else ""
        except Exception:      # noqa: BLE001
            pass
        shown = ex[-last:]
        head = (f" {name} · #{self.ctx.session.id} · "
                + (f"the last {len(shown)} of {len(ex)} exchanges" if len(ex) > len(shown)
                   else f"{len(ex)} exchange{'s' if len(ex) != 1 else ''}")
                + " · /transcript shows all ")
        out = ["", s("rule", "──") + s("heading", cut(head, max(10, w - 6)), bold=True) + s("rule", "──")]
        if not ex:
            out.append(s("faint", "  nothing in this chat yet"))
        for e in shown:
            out += cells.user_cell(e["you"], s, w)
            if e["tools"]:
                out.append(s("faint", f"  ⎿ {e['tools']} tool call{'s' if e['tools'] != 1 else ''}"))
            body = wrap_text(e["answer"] or "(no answer)", w - 4, indent="  ", first_indent="  ")
            out += [s("text" if e["answer"] else "faint", ln) for ln in body[:8]]
            if len(body) > 8:
                out.append(s("faint", f"  … {len(body) - 8} more lines"))
        out += [s("rule", "─" * max(10, min(w - 2, 60))), ""]
        return out

    def _transcript_lines(self) -> list[str]:
        """Every message of this chat: who said it, the text wrapped; tool
        calls and results short; Aura's own notes dim."""
        s, w = self.s, max(40, self.cw - 4)
        out: list[str] = []
        try:
            from ..notes import is_note
        except Exception:      # noqa: BLE001
            is_note = lambda m: False          # noqa: E731
        for m in self.ctx.session.messages:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text") or "[image]") for p in content if isinstance(p, dict))
            text = str(content or "")
            if role == "system" and not text.startswith("["):
                continue                            # The system prompt itself
            if role == "user":
                head = s("faint", "aura note") if is_note(m) else s("accent", "you", bold=True)
            elif role == "assistant":
                head = s("strong", "aura", bold=True)
            elif role == "tool":
                head = s("faint", f"  {m.get('name') or 'tool'} →")
                text = text[:300] + (" …" if len(text) > 300 else "")
            else:
                head = s("faint", "summary")
                text = text[:600] + (" …" if len(text) > 600 else "")
            out.append(head)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append(s("dim", f"  → {fn.get('name')}({str(fn.get('arguments'))[:120]})"))
            for para in text.splitlines() or [""]:
                while len(para) > w:
                    out.append("  " + para[:w])
                    para = para[w:]
                if para.strip() or not out[-1].strip() == "":
                    out.append("  " + para)
            out.append("")
        return out or ["  (nothing in this chat yet)"]

    def _overlay_lines(self, ov) -> list[str]:
        s, w = self.s, self.w
        kind = ov["kind"]
        self._overlay_cursor = None
        if kind == "help":
            out = ["  " + s("heading", "keyboard", bold=True)]
            kw = min(34, max(width(k) for k, _ in SHORTCUTS) + 2)
            for keyname, what in SHORTCUTS:
                out.append("  " + s("accent", pad(keyname, kw)) + s("dim", cut(what, w - kw - 4)))
            out.append("  " + s("faint", "any key closes"))
            return out
        if kind == "transcript":
            view = max(5, self.term.size()[1] - 4)
            lines = ov["lines"]
            top = ov["top"] if ov["top"] is not None else max(0, len(lines) - view)
            out = ["  " + s("heading", "transcript", bold=True)
                   + s("faint", f"  {top + 1}-{min(len(lines), top + view)} of {len(lines)} · "
                                "↑↓ PgUp PgDn Home End · / search, n N · any other key closes")]
            for i, ln in enumerate(lines[top:top + view], start=top):
                out.append(cut(s("accent", "▌") + ln if i == ov.get("hit") else ln, w - 1))
            if ov.get("typing") is not None:
                ed = ov["typing"]
                p = s("accent", "  / ")
                out.append(cut(p + ed.text, w - 1))
                self._overlay_cursor = (len(out) - 1, min(w - 2, width(p) + width(ed.text[:ed.cursor])))
            return out
        if kind == "board":
            return ov["b"].lines(s, w, self.term.size()[1])
        if kind == "approval":
            return self._approval_lines(ov)
        if kind == "clarify":
            q = str(ov["q"])
            team = q.startswith("[team] ")
            if team:
                q = q[len("[team] "):]
            out = ["", "  " + (s("accent", "▌TEAM ", bold=True) if team else s("accent", "? ", bold=True))
                   + s("strong", q, bold=True)
                   + s("faint", "  — the team board" if team else "  — Aura is asking")]
            opts = ov["opts"]
            multi = ov.get("multi")
            for i, o in enumerate(opts + ["type your own answer"]):
                own = i == len(opts)
                box = ("" if own or not multi else ("[x] " if i in ov["picked"] else "[ ] "))
                label = (s("dim", o, italic=True) if own else box + o)
                if ov.get("typing") is None and i == ov["i"]:
                    out.append("  " + s("sel_fg", s.g("arrow") + f" {i + 1}. " + box + o + " ", bold=True, bg="sel_bg"))
                elif opts:
                    out.append("    " + s("faint", f"{i + 1}. ") + label)
            if ov.get("typing") is not None:
                out.append("    " + s("faint", "type your answer in the box below · Enter sends · Esc "
                                               + ("back" if opts else "skip")))
            elif multi:
                out.append("    " + s("faint", "pick several: Space or 1-9 ticks · Enter sends · "
                                                "type to answer in your words · Esc skip"))
            else:
                out.append("    " + s("faint", "↑↓ Enter · 1-9 · type to answer in your words · Esc skip"))
            return out
        if kind == "ask":
            out = ["", "  " + s("accent", "? ", bold=True) + s("strong", cut(ov["prompt"], w - 6), bold=True),
                   "    " + s("faint", "type your answer in the box below · Enter answers · Esc cancels")]
            return out
        if kind == "choose":
            items = self._filtered(ov)
            out = ["  " + s("heading", ov["title"], bold=True)
                   + (s("dim", f"   filter: {ov['filter']}") if ov["filter"] else
                      s("faint", "   type to filter · Enter picks · Esc cancels"))]
            view = 10
            i = min(ov["i"], max(0, len(items) - 1))
            start = max(0, min(i - view // 2, len(items) - view))
            lab_w = min(max((width(o[0]) for o in items[start:start + view]), default=8) + 2, w // 2)
            for k, o in enumerate(items[start:start + view]):
                idx = start + k
                cur = len(o) > 3 and o[3]
                num = s("faint", f"{idx + 1:>2}. ") if idx < 9 else "    "
                mark = s("ok", " ✓" if s.unicode else " *") if cur else ""
                hint = s("faint", "  " + cut(str(o[2] if len(o) > 2 else ""), max(0, w - lab_w - 14)))
                if idx == i:
                    out.append("  " + s("sel_fg", s.g("arrow") + " " + pad(cut(o[0], lab_w), lab_w),
                                        bold=True, bg="sel_bg") + mark + hint)
                else:
                    out.append("  " + num + pad(cut(o[0], lab_w), lab_w) + mark + hint)
            sel = items[i] if items else None
            if sel is not None and len(sel) > 4 and sel[4]:
                # what the highlighted one is (e.g. what a chat worked on)
                out.append("")
                for d in sel[4][:8]:
                    out.append("     " + s("dim", cut(str(d), w - 7)))
            if not items:
                out.append("  " + s("dim", "nothing matches"))
            elif len(items) > view:
                out.append("  " + s("faint", f"{len(items)} items"))
            return out
        if kind == "search":
            ed: Editor = ov["ed"]
            hit = ov.get("hit")
            found = self.editor.history[hit] if hit is not None else ""
            out = ["", "  " + s("accent", "search history", bold=True)
                   + s("faint", " — type in the box below")]
            if found:
                shown = found.replace("\n", " ⏎ ")
                q = ed.text.lower()
                j = shown.lower().find(q) if q else -1
                if j >= 0 and q:
                    shown = shown[:j] + s("warn", shown[j:j + len(q)], bold=True) + shown[j + len(q):]
                out.append("  " + s("dim", s.g("arrow")) + " " + cut(shown, w - 6))
            elif ed.text:
                out.append("  " + s("faint", "no match"))
            out.append("  " + s("faint", "Ctrl+R older · Enter use it · Esc cancel"))
            return out
        if kind == "rewind":
            out = ["  " + s("heading", "go back to an earlier message", bold=True)
                   + s("faint", "  — the chat is rewound to just before it; you edit and resend")]
            for k, (seq, text) in enumerate(ov["items"]):
                t = cut(text.replace("\n", " "), w - 8)
                out.append("  " + (s("sel_fg", s.g("arrow") + " " + t, bold=True, bg="sel_bg")
                                   if k == ov["i"] else "  " + s("dim", t)))
            out.append("  " + s("faint", "Enter go back · Esc cancel"))
            return out
        return []

    def _change_preview(self, req) -> list[str]:
        """What the file will look like: a diff for edits and overwrites,
        the first lines of a new file. Empty for other tools."""
        tool = req.get("tool", "")
        if tool not in ("edit_file", "write_file"):
            return []
        a = req.get("preview_args") or req.get("args") or {}
        path = str(a.get("path", ""))
        cur = cells._read(self.workspace, path)
        s, w = self.s, self.w - 6
        if tool == "edit_file":
            old = str(a.get("old_string") or a.get("old") or "")
            new = str(a.get("new_string") or a.get("new") or "")
            if cur is not None and old and old in cur:
                from .diff import render_diff
                return render_diff(cur, cur.replace(old, new, 1), path, s, w, max_lines=16)
            from .diff import render_diff
            return render_diff(old, new, path, s, w, max_lines=16)
        new = str(a.get("content", ""))
        if cur is None:
            return cells.tool_cell({"tool": "write_file", "args": {"path": path, "content": new},
                                    "status": "ok", "blast": "write"}, s, w)[1:]
        from .diff import render_diff, stats, summary
        ad, rm = stats(cur, new)
        return ([summary(ad, rm, s) + s("faint", "  (replaces the whole file)")]
                + render_diff(cur, new, path, s, w, max_lines=16))

    def _approval_lines(self, ov) -> list[str]:
        s, w = self.s, self.w
        req = ov["req"]
        grade = str(req.get("grade", ""))
        risky = grade == "G3"
        tool = req.get("tool", "")
        verb, target = cells.tool_title(tool, req.get("args") or {})
        head_role = "err" if risky else "warn"
        try:
            from ..approvals import _GRADE_WORDS
            risk = _GRADE_WORDS.get(grade, "")
        except Exception:      # noqa: BLE001
            risk = "can't be undone or reaches outside" if risky else ""
        out = ["", "  " + s(head_role, (s.g("lock") if risky else s.g("needs")) + " ", bold=True)
               + s("strong", req.get("heading") or f"Aura wants to {verb.lower()}", bold=True)
               + (s(head_role if risky else "faint", "  " + risk) if risk else "")]
        what = target or str(req.get("target", ""))
        if what:
            for ln in wrap(what, w - 8)[:4]:
                out.append("    " + s("dim", ln))
        out.extend("    " + ln for ln in self._change_preview(req))
        det = req.get("detail")
        if det:
            for ln in str(det).splitlines()[:6]:
                out.append("    " + s("dim", cut(ln, w - 8)))
        if req.get("why"):
            out.append("    " + s("faint", "why: " + cut(str(req["why"]), w - 14)))
        if req.get("hint"):
            out.append("    " + s("warn", cut(str(req["hint"]), w - 8)))
        out.append("")
        for i, (val, label) in enumerate(self._approval_options(req)):
            num = f"{i + 1}. "
            if i == ov["i"]:
                out.append("  " + s("sel_fg", s.g("arrow") + " " + num + label + " ", bold=True, bg="sel_bg"))
            else:
                out.append("    " + s("faint", num) + label)
        if ov.get("reason") is not None:
            out.append("    " + s("warn", "what should Aura do instead? ")
                       + s("faint", "type it in the box below · Enter sends · Esc back"))
        else:
            out.append("    " + s("faint", "↑↓ Enter · 1-3 · y/a/n · Esc = no"))
        return out

    def _filtered(self, ov) -> list:
        f = ov.get("filter", "").lower()
        if not f:
            return ov["opts"]
        return [o for o in ov["opts"] if f in str(o[0]).lower() or f in str(o[2] if len(o) > 2 else "").lower()
                or (len(o) > 4 and f in " ".join(map(str, o[4] or [])).lower())]

    def _typing_field(self):
        """(editor, placeholder) when the open card takes typed text — an
        answer of your own, a "no — do this instead", a search; that text is
        typed and shown in the input box, never on a line inside the card."""
        ov = self.overlay
        if not ov:
            return None
        kind = ov.get("kind")
        if kind == "ask":
            return ov["ed"], "your answer · Enter answers · Esc cancels"
        if kind == "clarify" and ov.get("typing") is not None:
            return ov["typing"], "your answer · Enter sends · Esc back"
        if kind == "approval" and ov.get("reason") is not None:
            return ov["reason"], "what should Aura do instead? · Enter sends · Esc back"
        if kind == "search":
            return ov["ed"], "search what you typed before · Enter takes it · Esc closes"
        return None

    def _box_lines(self) -> tuple[list[str], int, int]:
        s, w = self.s, self.w
        field = self._typing_field()
        ed = field[0] if field else self.editor
        shell = ed.text.startswith("!")
        border = "tool_exec" if shell else ("border_focus" if self.overlay is None or field
                                            else "border")
        inner = max(10, w - 5)
        prompt = s("tool_exec" if shell else "accent", ("! " if shell else s.g("arrow") + " "), bold=True)
        rows, (crow, ccol) = ed.layout(inner, 2, 2)
        body: list[str] = []
        if not ed.text:
            ph = (field[1] if field else
                  "type to add to the task · Esc stops it" if self.running else
                  "ask anything · / commands · @ files · ? shortcuts")
            body = [prompt + s("faint", cut(ph, inner - 3))]
        else:
            secret = bool(field and (self.overlay or {}).get("secret"))
            for i, (a, b) in enumerate(rows):
                seg = ed.text[a:b].replace("\t", "    ")
                seg = "•" * len(seg) if secret else _style_chips(seg, s)
                body.append((prompt if i == 0 else "  ") + seg)
        max_rows = 12
        top_row = 0
        if len(body) > max_rows:
            top_row = max(0, min(crow - max_rows + 1, len(body) - max_rows))
            body = body[top_row:top_row + max_rows]
        h, v = s.g("box_h"), s.g("box_v")
        badge = ""
        if shell:
            badge = " shell "
        elif self.vim is not None and self.vim.mode == "normal":
            badge = " NORMAL "
        elif field:
            badge = " answering "
        elif self.editor._stash:
            badge = " draft parked "
        top = s(border, s.g("box_tl") + h) + (s("tool_exec" if shell else "faint", badge) if badge else "")
        top += s(border, h * max(0, w - 4 - width(badge)) + s.g("box_tr"))
        out = [top]
        for ln in body:
            out.append(s(border, v) + " " + pad(cut(ln, inner), inner) + " " + s(border, v))
        out.append(s(border, s.g("box_bl") + h * (w - 3) + s.g("box_br")))
        return out, 1 + crow - top_row, 2 + ccol

    def _activity_line(self, frame: str, act: str, elapsed: str) -> str:
        """The live line while a task runs, like the old run line:
        step · tools · time · tokens · budget meter · esc to stop."""
        s, w = self.s, self.w
        dot = s("faint", f" {s.g('dot')} ")
        bits = [elapsed]
        if self.turn_steps:
            mx = getattr(getattr(self.loop, "cfg", None), "max_steps", None)
            bits.append(f"step {self.turn_steps}" + (f"/{mx}" if mx else ""))
        more = self.display != "simple" or self.verbose
        if self.turn_tools and more:
            bits.append(f"{self.turn_tools} tool{'s' if self.turn_tools != 1 else ''}")
        if (self.turn_tokens[0] or self.turn_tokens[1]) and more:
            bits.append(f"{s.g('up')}{cells.fmt_tokens(self.turn_tokens[0])} "
                        f"{s.g('down')}{cells.fmt_tokens(self.turn_tokens[1])}")
        tail = [s("faint", b) for b in bits]
        try:
            b = self.m.budgets
            ceiling = getattr(getattr(self.loop, "settings", None), "token_ceiling", None)                 or getattr(b, "token_ceiling", None)
            if ceiling and (more or b.tokens_spent >= 0.6 * ceiling):   # Simple: only when near
                pct = min(100, int(100 * b.tokens_spent / ceiling))
                fill = max(0, min(10, pct // 10))
                role = "ok" if pct < 60 else "warn" if pct < 85 else "err"
                tail.append(s(role, s.g("meter_full") * fill)
                            + s("faint", s.g("meter_empty") * (10 - fill) + f" {pct}% budget"))
        except Exception:      # noqa: BLE001
            pass
        tail.append(s("faint", "Ctrl+O details · Esc stop"))
        head = s("accent", frame) + " " + s("strong", cut(act, max(10, w // 3)))
        # too narrow: drop from the middle (budget and esc hint stay last to go)
        while len(tail) > 2 and width(head + "  " + dot.join(tail)) > w - 1:
            tail.pop(len(tail) - 3 if len(tail) > 3 else 1)
        return cut(head + "  " + dot.join(tail), w - 1)

    STATUS_PARTS = ("mode", "model", "context", "tokens", "cost", "elapsed", "project",
                    "branch", "time", "team")
    # when the line is too narrow, parts go in this order (context and mode stay)
    STATUS_DROP = ("branch", "project", "elapsed", "cost", "tokens", "time", "team", "thinking",
                   "model")

    def _status_parts(self) -> list[str]:
        if getattr(self, "_status_cfg", None) is None:
            try:
                from ..config import load_saved_settings
                cfg = load_saved_settings().get("ui_statusline")
            except Exception:      # noqa: BLE001
                cfg = None
            self._status_cfg = [x for x in (cfg or []) if x in self.STATUS_PARTS] or \
                ["mode", "model", "context", "tokens", "cost", "elapsed", "team", "project",
                 "branch", "time"]
        return self._status_cfg

    def _ctx_numbers(self) -> tuple[int, int]:
        gov = getattr(self.loop, "governor", None)
        if not (gov and gov.ctx):
            return 0, 0
        used = self.ctx_used or 0
        if not used:
            try:
                used = gov.count(self.ctx.session.messages)
            except Exception:      # noqa: BLE001
                used = 0
        return used, gov.ctx

    def _status_line(self) -> str:
        s, w = self.s, self.w
        want = self._status_parts()
        assist = getattr(self.m, "assist", "L2")
        mode = getattr(self.m, "mode", "act")
        # (name, text) pairs; left side then right side
        left: list[tuple[str, str]] = []
        right: list[tuple[str, str]] = []
        if "mode" in want:
            role = "mode_plan" if mode in ("plan", "discuss") else {"L1": "mode_l1", "L2": "mode_l2",
                                                       "L3": "mode_l3"}.get(assist, "mode_l2")
            left.append(("mode", s(role, _mode_words(assist, mode), bold=True)))
        if "model" in want:
            left.append(("model", s("dim", cut(str(self.loop.llm.p.model), 28))))
            try:                                   # Think: next to the model it is for
                from ..thinking import level as _tlevel
                _lv = _tlevel(self.loop.llm.p)
                if _lv != "auto":                  # The default says nothing
                    left.append(("thinking", s("accent" if _lv != "off" else "faint",
                                               f"think {_lv}")))
            except Exception:      # noqa: BLE001
                pass
        used, total = self._ctx_numbers()
        ctx_short = ""
        if "context" in want and total:
            # How full the window is NOW, toward the point where Aura
            # summarizes
            at = (getattr(self, "ctx_marks", (0, 0)) or (0, 0))[1] or int(total * 0.8)
            seg = 10
            near = min(1.0, used / at) if at else 0.0
            fill = max(1 if used else 0, min(seg, round(seg * near)))
            role = "ok" if near < 0.75 else "warn" if near < 0.95 else "err"
            meter = s(role, s.g("meter_full") * fill) + s("faint", s.g("meter_empty") * (seg - fill))
            ctx_short = s("faint", "context ") + meter + " " + s("dim", cells.fmt_tokens(used))
            left.append(("context", ctx_short + s("faint", " · ")
                         + s("dim", f"{cells.fmt_tokens(max(0, at - used))} until summary")))
        if "team" in want:                         # Aura Teams, when connected
            try:
                from ..teams_ui import snapshot, status_mark
                mk = status_mark(snapshot(self.ctx.session.path.stem))
                if mk:
                    left.append(("team", s(mk[1], mk[0])))
            except Exception:      # noqa: BLE001
                pass
        if "tokens" in want and (self.chat_tokens[0] or self.chat_tokens[1]) and \
                (self.display == "everything" or self.verbose):
            left.append(("tokens", s("faint", f"this chat: sent {cells.fmt_tokens(self.chat_tokens[0])}"
                                              f" · got {cells.fmt_tokens(self.chat_tokens[1])}")))
        if "cost" in want and self.chat_cost:
            left.append(("cost", s("faint", f"${self.chat_cost:.4f}" if self.chat_cost < 1
                                   else f"${self.chat_cost:.2f}")))
        if "elapsed" in want:
            left.append(("elapsed", s("faint", _fmt_session(time.monotonic() - self.t_start))))
        if self.queued or self.pending:
            left.append(("queued", s("accent", f"{len(self.queued) + len(self.pending)} waiting")))
        if self.hint and time.monotonic() < self.hint_until:
            right.append(("hint", s("accent", self.hint)))
        else:
            if self.verbose:
                right.append(("verbose", s("warn", "full output")))
            if "project" in want:
                right.append(("project", s("dim", self.workspace.name)))
            if "branch" in want and self.branch:
                right.append(("branch", s("faint", self.branch)))
            if "time" in want:
                right.append(("time", s("faint", time.strftime("%H:%M"))))
        sep = s("faint", f" {s.g('sep')} ")

        def build() -> str:
            lt = " " + sep.join(t for _, t in left)
            rt = " ".join(t for _, t in right) + " "
            gap = w - 1 - width(lt) - width(rt)
            return lt + " " * max(1, gap) + rt if gap >= 1 else ""

        line = build()
        # too narrow: drop the least useful parts first; context only gets shorter
        for name in self.STATUS_DROP:
            if line:
                break
            left = [p for p in left if p[0] != name]
            right = [p for p in right if p[0] != name]
            line = build()
        if not line and ctx_short:
            left = [(n, ctx_short if n == "context" else t) for n, t in left]   # Narrow: the bar + now
            line = build()
        return cut(line or (" " + sep.join(t for _, t in left)), w - 1)

    def _statusline_cmd(self, line: str) -> None:
        parts = line.split()[1:]
        s, w = self.s, self.w
        if not parts:
            self.say(cells.notice_cell(
                "status line: " + " ".join(self._status_parts())
                + " — /statusline <parts> sets it, /statusline reset · parts: "
                + " ".join(self.STATUS_PARTS), s, w, "info"))
            return
        if parts == ["reset"]:
            chosen = []
        else:
            bad = [x for x in parts if x not in self.STATUS_PARTS]
            if bad:
                self.say(cells.notice_cell(f"unknown part(s): {', '.join(bad)} — parts: "
                                           + " ".join(self.STATUS_PARTS), s, w, "warn"))
                return
            chosen = parts
        try:
            from ..config import load_saved_settings, save_saved_settings
            save_saved_settings({**load_saved_settings(), "ui_statusline": chosen})
        except Exception:      # noqa: BLE001
            pass
        self._status_cfg = None
        self.say(cells.notice_cell("status line: " + " ".join(self._status_parts()),
                                   s, w, "ok"))


# ------------------------------------------------------------------ utils --
def _edit(ed: Editor, k: Key) -> None:
    """Shared text editing keys."""
    kid = k.id
    if kid == "char":
        ed.insert(k.text)
    elif kid == "paste":
        ed.paste(k.text)
    elif kid == "backspace":
        ed.backspace()
    elif kid in ("delete",):
        ed.delete()
    elif kid in ("left", "ctrl+b"):
        ed.left()
    elif kid in ("right", "ctrl+f"):
        ed.right()
    elif kid in ("home", "ctrl+a"):
        ed.line_start()
    elif kid in ("end", "ctrl+e"):
        ed.line_end()
    elif kid in ("ctrl+left", "alt+b", "alt+left"):
        ed.word_left()
    elif kid in ("ctrl+right", "alt+f", "alt+right"):
        ed.word_right()
    elif kid == "ctrl+home":
        ed.buf_start()
    elif kid == "ctrl+end":
        ed.buf_end()
    elif kid == "ctrl+k":
        ed.kill_to_end()
    elif kid == "ctrl+u":
        ed.kill_to_start()
    elif kid == "ctrl+w":
        ed.kill_word_back_ws()
    elif kid in ("ctrl+backspace", "alt+backspace", "ctrl+h"):
        ed.kill_word_back()
    elif kid in ("alt+d", "ctrl+delete"):
        ed.kill_word_fwd()
    elif kid == "ctrl+y":
        ed.yank()
    elif kid == "alt+y":
        ed.yank_pop()
    elif kid in ("ctrl+_", "ctrl+z") and os.name == "nt" or kid == "ctrl+_":
        ed.undo()
    elif kid == "ctrl+space":
        ed.insert(" ")


def _think_lines(text: str, width_: int, most: int | None) -> list[str]:
    """The model's thinking as readable lines: whole sentences from the end,
    up to `most` lines (None = all) — never starting mid-sentence."""
    import re as _re
    from .text import wrap_text
    flat = " ".join(str(text).split())
    if not flat:
        return []
    if most is None:
        return wrap_text(flat, width_, indent="", first_indent="")
    sents = _re.split(r"(?<=[.!?:])\s+", flat)
    keep: list[str] = []
    for snt in reversed(sents):
        trial = wrap_text(" ".join([snt] + keep), width_, indent="", first_indent="")
        if len(trial) > most and keep:
            break
        keep.insert(0, snt)
        if len(trial) >= most:
            break
    lines = wrap_text(" ".join(keep), width_, indent="", first_indent="")
    return lines[-most:]


def _style_chips(seg: str, s: Style) -> str:
    """Paste chips and @file mentions shown as coloured chips."""
    import re
    from .editor import CHIP
    seg = re.sub(CHIP, lambda m: s("sel_fg", m.group(0), bg="sel_bg"), seg)
    seg = re.sub(r"(?<!\S)@[\w./\\-]+", lambda m: s("link", m.group(0)), seg)
    return seg


def _fmt_session(sec: float) -> str:
    """Time since the chat opened, like the old bar: 42s, 3:07, 1:02:09."""
    t = int(sec)
    if t < 60:
        return f"{t}s"
    h, rest = divmod(t, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{t // 60}:{t % 60:02d}"


def _mode_words(assist: str, mode: str, long: bool = False) -> str:
    if mode == "discuss":
        return "discuss · talk only" if not long else "discuss mode — Aura talks and looks things up, does no work"
    if mode == "plan":
        return "plan · read only" if not long else "plan mode — Aura reads and researches, changes nothing"
    short = {"L1": "asks every change", "L2": "asks before risky", "L3": "never asks"}
    longer = {"L1": "Aura asks before every change or command",
              "L2": "Aura asks before risky actions (can't be undone / reaches outside)",
              "L3": "Aura never asks — fully autonomous"}
    return (longer if long else short).get(assist, assist)


def _host(url: str) -> str:
    u = (url or "").split("://", 1)[-1]
    return u.split("/", 1)[0]


def _git_branch(ws: Path) -> str:
    try:
        head = (Path(ws) / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref: "):
        return head.rsplit("/", 1)[-1]
    return head[:7]


def use_tui() -> bool:
    """The new chat runs on a real terminal unless AURA_UI=classic."""
    if os.environ.get("AURA_UI", "").lower() in ("classic", "plain", "0"):
        return False
    if os.environ.get("AURA_FRAME") == "0":
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:      # noqa: BLE001
        return False

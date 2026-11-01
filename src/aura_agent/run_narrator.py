"""run_narrator.py — the run-view event renderer, extracted from repl.chat so the
visual audit can drive it headless (the SAME renderer the user sees — zero
drift between tests and reality).

Feeds: loop events → verbified narration / batches / burn line / blocked-scope
/ approval cards. Attach with: loop.on_event = nr.
"""
from __future__ import annotations

import json
import os

from . import ui
from .hud import Hud, phase as hud_phase
from .verbify import counter_line as verbify_counter
from .verbify import from_event as verbify_from_event
from .verbify import _short_target

_WATCH_NOTES = {
    "reviewer_ran": lambda d: (
        f"second opinion after {d.get('streak', '?')} failures",
        str(d.get("recommend") or "")[:72]),
    "intent_drift": lambda d: (
        "off-task? re-reading your request",
        str(d.get("first") or "")[:72]),
    "result_recorded": lambda d: (
        ("result proven", f"{d.get('id')} — {d.get('steps', '?')} step(s) "
         "re-run with the same output") if d.get("proven") else
        ("result NOT proven", f"{d.get('id')} — stays out of the report")),
    "loopwatch": lambda d: (
        ("repeat stopped",
         f"{d.get('tool', 'a tool')} skipped — the same call ran "
         f"{d.get('same_run', '?')}× in a row") if d.get("hard_stop") else
        ("going in circles",
         f"last {d.get('calls', '?')} results were all repeats — "
         "changing approach") if d.get("stall") else
        ("repeat noticed",
         f"{d.get('tool', 'a tool')} called the same way "
         f"{d.get('same_run', '?')}× — changing approach")),
}


def watch_note(kind: str, data: dict) -> tuple[str, str] | None:
    """(title, detail) for a watched loop event, else None."""
    fn = _WATCH_NOTES.get(kind)
    return fn(data) if fn and isinstance(data, dict) else None


class RunNarrator:
    """The run-view renderer. Attach to loop.on_event; prints exactly what
    chat users see (blast-radius tree, verbified identity, batched
    counters, per-call durations, blocked narration, approval cards)."""

    def __init__(self, max_steps: int | None = None, budget_s: float = 0.0,
                 debug_raw: bool = False):
        self.hud = Hud(max_steps=max_steps, budget_s=budget_s, enabled=False)
        self.debug_raw = debug_raw
        self.batch = 0
        self.lines: list[str] = []   # Capture for tests/audit
        self._pending: dict[str, dict] = {}   # Tool → blast/target
        self.last_target: str = ""            # Status-bar segment
        # Status bar config (set via configure_bar from repl)
        self.status_bar = False
        self.model = ""
        self.ctx_total = 0
        self.proven_fn = lambda: 0
        self._bar_live = False

    def _out(self, text) -> None:
        self.lines.append(str(text))
        print(text)

    def _warn(self, text: str) -> None:
        """ui.warn prints itself (returns None) — call it, then capture."""
        ui.warn(text)
        self.lines.append(str(text))

    def flush_batch(self) -> None:
        if self.batch:
            self._out(ui.dim(f"  · {verbify_counter(self.batch)}"))
            self.batch = 0

    # ------------------------------------------------------- bar
    def configure_bar(self, model: str = "", ctx_total: int = 0,
                      proven_fn=None) -> None:
        """Called once by repl: what the status bar needs to know."""
        self.status_bar = True
        self.model = model
        self.ctx_total = ctx_total
        self.proven_fn = proven_fn or (lambda: 0)

    def _bar(self) -> None:
        import sys as _sys
        import shutil as _shutil
        from . import console
        ctx_pct = None
        try:
            used = getattr(self.hud, "_ctx_used", 0)
            if self.ctx_total:
                ctx_pct = int(100 * used / self.ctx_total)
        except Exception:
            ctx_pct = None
        text = console.status_bar(
            model=self.model,
            ctx_pct=ctx_pct,
            elapsed=self.hud._fmt_elapsed(),
            tokens=(f"↑{self.hud._k(self.hud._tok_in)} "
                    f"↓{self.hud._k(self.hud._tok_out)}"
                    if (self.hud._tok_in or self.hud._tok_out) else ""),
            proven=self.proven_fn(),
            target=self.last_target)
        try:
            _dock = getattr(self, "dock", None)
            if _dock is not None:
                # Pinned-dock mode: the dock owns the bottom rows — just hand
                # it the text; it redraws above nothing (content writes
                # erase/redraw via its own hook)
                _dock.bar_text = text
                _dock.draw()
                self._bar_live = True
            elif _sys.stdout.isatty():
                w = _shutil.get_terminal_size().columns
                _sys.stdout.write("\r\033[2K" + text[:w])
                _sys.stdout.flush()
                self._bar_live = True
            else:
                pass  # Non-tty: burn line already carries the numbers
        except Exception:
            pass

    def release_bar(self) -> None:
        """Turn end: the dock owns the bar in pinned mode — nothing to do
        (the bar rows are outside the scroll region and stay put). In
        legacy mode, newline so the bar line stays as a normal log line."""
        import sys as _sys
        if getattr(self, "dock", None) is not None:
            dock = self.dock
            if getattr(dock, "_pinned", False):
                self._bar_live = False
                return
        if getattr(self, "_bar_live", False):
            try:
                _sys.stdout.write("\n")
                _sys.stdout.flush()
            except Exception:
                pass
            self._bar_live = False

    def __call__(self, kind: str, data: dict) -> None:
        self.hud.observe(kind, data)
        if kind == "ctx_state" and isinstance(data, dict):
            try:
                self.hud._ctx_used = int(data.get("used", 0))
            except Exception:
                pass
            return
        if kind == "llm_done":
            self._out(self.hud.line())
            # Status bar — in-place rewrite on tty; a fresh printed bar each
            # tool/llm boundary otherwise. Never during llm wait (the spinner
            # owns the line then).
            if self.status_bar:
                self._bar()
        note = watch_note(kind, data)
        if note:
            self.flush_batch()
            hud_phase(*note)
        elif kind == "phase":
            self.flush_batch()
            hud_phase(data.get("name", ""), data.get("detail", ""))
        elif kind == "team" and isinstance(data, dict):
            self.flush_batch()
            self._warn("  team: " + str(data.get("text", ""))
                       + "".join("\n    · " + str(x) for x in data.get("lines") or []))
        elif kind == "notice" and isinstance(data, dict):
            # full-length operator notice (carries a command to run)
            self.flush_batch()
            self._warn("  " + str(data.get("text", "")))
        elif kind == "approval_needed" and isinstance(data, dict):
            self.flush_batch()
            self._warn(f"  · {data.get('tool', 'call')} needs your "
                       "approval:")
            self._out(data.get("card", ""))
        elif kind == "tool_start" and isinstance(data, dict) \
                and "tool" in data:
            # Remember the blast + target so tool_done can render the duration
            # and the status bar can show the last target.
            _tool = data.get("tool", "?")
            _args = data.get("args") or {}
            _sig = (_tool, json.dumps(_args, sort_keys=True, default=str))
            _repeat = _sig == getattr(self, "_last_sig", None)
            self._last_sig = _sig
            self._pending[_tool] = {
                "blast": str(data.get("blast", "read")),
                "target": _short_target(_args),
                "repeat": _repeat,
            }
            if self.debug_raw or _repeat:
                # Identical tool+args back-to-back → suppress the render (the
                # stutter: 100 identical ladder lines).
                pass
            else:
                s, ident = verbify_from_event(data)
                if ident:
                    self.flush_batch()
                    from .console import activity
                    blast = str(data.get("blast", "read"))
                    detail = s
                    # strip the leading verb from the verbify sentence —
                    # the ladder shows the OBJECT; the outcome glyph
                    # carries the verb-ness
                    for pre in ("reading ", "running ", "writing ",
                                "fetching ", "searching ", "editing "):
                        if s.startswith(pre):
                            detail = s[len(pre):]
                            break
                    self._out(activity(detail, 0, blast=blast,
                                       outcome="run"))
                else:
                    self.batch += 1
        elif kind == "llm_usage" and isinstance(data, dict):
            # Dzs real-time usage: hud.observe (line 121) already adds the
            # token deltas — here we only refresh the bar so the ↑↓ move while
            # the model streams, not only at llm_done.
            if self.status_bar:
                self._bar()
        elif kind == "tool_done" and isinstance(data, dict):
            st = data.get("status")
            # Real per-call duration on the tree for slow calls (>= 0.5s) —
            # fast reads stay quiet (no scroll spam).
            _ms = data.get("duration_ms") or 0
            _pend = self._pending.pop(data.get("tool", "?"), None)
            if st == "ok" and _ms >= 500 and not self.debug_raw:
                from .console import activity
                blast = (_pend or {}).get("blast",
                                          str(data.get("blast", "read")))
                self._out(activity(f"{data.get('tool', 'call')} finished",
                                   _ms, blast=blast, outcome="ok"))
            self.last_target = (_pend or {}).get("target") or self.last_target
            if st == "blocked":
                self.flush_batch()
                from .console import scope_block
                self._warn(scope_block(
                    (_pend or {}).get("target") or data.get("tool", "call"),
                    "outside what I'm allowed to touch"))
            elif st == "refused" and data.get("approval_card"):
                self.flush_batch()
                self._warn(f"  · {data.get('tool', 'call')} needs "
                           "your approval:")
                self._out(data["approval_card"])
            elif st == "refused":
                self.flush_batch()
                from .console import scope_block
                detail = str(data.get("detail", ""))
                if "scope" in detail.lower() or "outside" in detail.lower():
                    self._warn(scope_block(
                        (_pend or {}).get("target") or
                        data.get("tool", "call"),
                        detail[:56] or "outside what I'm allowed to touch"))
                else:
                    self._warn(f"  · {data.get('tool', 'call')} "
                               f"refused — {detail[:56]}")

    @classmethod
    def attach(cls, loop, budget_s: float = 0.0) -> "RunNarrator":
        import os
        try:
            budget_s = float(os.environ.get("AI_BUDGET_S", "0") or 0) or \
                budget_s
        except ValueError:
            pass
        n = cls(max_steps=None, budget_s=budget_s)
        prev = getattr(loop, "on_event", None)

        def _composed(kind: str, data: dict) -> None:
            n(kind, data)
            if callable(prev):
                prev(kind, data)

        loop.on_event = _composed
        return n

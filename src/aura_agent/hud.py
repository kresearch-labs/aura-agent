"""hud.py — live activity HUD (spinners, step/budget line, phase events).

Presentation-layer only: reads loop events, draws ANSI. No engine changes.

Components:
  SpinDuring — context-manager spinner shown while the LLM call streams
                  (first content/reasoning/tool delta replaces it).
  HudState — thread-safe step/budget/time tracker fed by loop events.
  hud_line — the one-line status render: `▐ step 7/60 · 2:14 · ↑12k ↓3k ·
                  budget 45% ▌`
  phase — visible one-liners for gate/finish-verify/steer events
"""
from __future__ import annotations

import itertools
import sys
import threading
import time

from . import ui

SPIN_FRAMES = itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def _fmt_clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


class SpinDuring:
    """Spinner shown until the first delta of a streaming call arrives.

    Usage:
        with SpinDuring(label="thinking"):
            res = self.llm.chat(...)   # first on_delta cancels the spinner
    The spinner thread writes \\r + frame + label; on cancel it clears the
    line so the streamed content starts clean. No-op on non-tty.
    """

    def __init__(self, label: str = "thinking", enabled: bool | None = None):
        self.label = label
        self.enabled = ui.supports() if enabled is None else enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spin(self) -> None:
        while not self._stop.wait(0.08):
            frame = next(SPIN_FRAMES)
            sys.stdout.write(f"\r  {ui.paint(frame, ui.C['agent_marker'])} "
                             f"{ui.muted(self.label + '…')}")
            sys.stdout.flush()

    def cancel(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self.enabled:
            sys.stdout.write("\r\033[2K")     # Clear the spinner line
            sys.stdout.flush()

    def __enter__(self):
        if self.enabled:
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self.cancel()
        return False


class Hud:
    """Step/budget/time tracker. `line()` renders; `show()` prints in place.

    Fed by the loop's existing events (llm_start/llm_done/tool_done) via
    `observe()`. Budget comes from AI_BUDGET_S when set (0 = no budget bar).
    """

    def __init__(self, max_steps: int | None = None, budget_s: float = 0.0,
                 enabled: bool | None = None):
        self.max_steps = max_steps
        self.budget = budget_s
        self.enabled = ui.supports() if enabled is None else enabled
        self._t0 = time.monotonic()
        self._step = 0
        self._step_base = 0
        self._tools = 0
        self._tok_in = 0
        self._tok_out = 0
        self._usage_seen: set = set()   # llm_usage/llm_done dedupe
        self._ctx_used = 0     # Governor ctx usage for status bar
        self._lock = threading.Lock()

    def observe(self, kind: str, data: dict) -> None:
        with self._lock:
            if kind == "llm_start":
                # A new turn restarts its own step counter at 1 — carry the
                # prior total so the burn line never lies ("step 1/60" after 20
                # real steps read as progress lost). Steps SHOW as base +
                # within-turn; the max is per-turn budget.
                raw = int(data.get("step", 0))
                if raw <= self._step - self._step_base:
                    # counter went backwards → a new turn began
                    self._step_base = self._step
                self._step = self._step_base + max(raw, 1)
            elif kind == "tool_done":
                self._tools += 1
            elif kind == "llm_done":
                u = data.get("usage") or {}
                if id(u) in self._usage_seen:
                    pass   # Already recorded at llm_usage time
                else:
                    self._tok_in += int(u.get("prompt_tokens") or 0)
                    self._tok_out += int(u.get("completion_tokens") or 0)
                if len(self._usage_seen) > 256:
                    self._usage_seen.clear()   # Bounded
            elif kind == "llm_usage":
                # Real-time: usage chunks stream in before llm_done — record
                # immediately so the bar ticks live. Dedupe: the same dict
                # object arrives again as llm_done for this call; id tracking
                # avoids double-counting.
                if id(data) not in self._usage_seen:
                    self._usage_seen.add(id(data))
                    self._tok_in += int(data.get("prompt_tokens") or 0)
                    self._tok_out += int(data.get("completion_tokens") or 0)

    def line(self, final: bool = False) -> str:
        with self._lock:
            step = self._step
            tools = self._tools
            tin, tout = self._tok_in, self._tok_out
        elapsed = time.monotonic() - self._t0
        parts = [f"step {step}" + (f"/{self.max_steps}" if self.max_steps else ""), f"{tools} tools",
                 _fmt_clock(elapsed)]
        if tin or tout:
            parts.append(f"↑{self._k(tin)} ↓{self._k(tout)}")
        if self.budget > 0:
            pct = int(100 * elapsed / self.budget)
            pct = min(pct, 99) if not final else min(pct, 100)
            bar_w = 10
            fill = int(bar_w * pct / 100)
            bar = "▰" * fill + "▱" * (bar_w - fill)
            col = ui.C["ok"] if pct < 60 else ui.C["warn"] if pct < 85 else ui.C["err"]
            parts.append(f"{ui.paint(bar, col)} {pct}% budget")
        return f"▄ {ui.muted(' · '.join(parts))}"

    # --------------------------------------------------- status bar
    def bar(self, model: str = "", ctx_pct: int | None = None,
            proven: int | None = None, last_target: str = "") -> str:
        """One operator line, rewritten in place during runs.

        V-POLISH: delegates to console.status_bar so the in-run bar and the
        idle prompt bar share ONE grammar (┃[seg | seg]┃, ▓░ bar, absolute
        ctx numbers). The old ◈/Ψ prefix duplicated console's identity and
        used a third bar glyph — the fragmentation the visual pass flagged.
        """
        from .console import status_bar
        with self._lock:
            tin, tout = self._tok_in, self._tok_out
        elapsed = _fmt_clock(time.monotonic() - self._t0)
        tokens = (f"↑{self._k(tin)} ↓{self._k(tout)}"
                  if (tin or tout) else "")
        return status_bar(model=model, ctx_pct=ctx_pct, elapsed=elapsed,
                          proven=proven, target=last_target, tokens=tokens)

    def _fmt_elapsed(self) -> str:
        """Rev-2: elapsed as a status-bar segment."""
        return _fmt_clock(time.monotonic() - self._t0)

    @staticmethod
    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def phase(text: str, detail: str = "") -> None:
    """."""
    mark = ui.paint("◇", ui.C["banner_accent"])
    line = f"  {mark} {ui.accent(text)}"
    if detail:
        line += "  " + ui.muted(detail[:72])
    print(line)


def nl_if_needed() -> None:
    """Call before printing after streamed content that may lack a newline."""
    sys.stdout.write("\n")
    sys.stdout.flush()

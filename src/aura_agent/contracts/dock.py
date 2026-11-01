"""dock.py — the pinned bottom dock:

    ┃[GLM_teams | ctx 9k/32k tok · 28% ▰▰▱▱... | 12s | 0 proven]┃
    ►

ALWAYS visible at the bottom — even while the agent streams. Mechanism: a
terminal SCROLL REGION. On install the dock is drawn on the last two rows and
the scroll region is set to rows 1..(H-2); scrolling content pushes only rows
inside the region, so the dock rows are never touched. Content writes just flow
(the cursor lives in the region); the dock rows are rewritten in place on every
bar update and before input.

Fallback (piped / region-less terminal): rev-1 redraw-after-content.
"""
from __future__ import annotations

import atexit
import shutil
import sys
import threading


def _vlen(s: str) -> int:
    """visible length (SGR-stripped) — stdlib, no deps."""
    import re
    return len(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s))


class Dock:
    def __init__(self):
        atexit.register(self.uninstall)   # Region reset even on hard exit
        self.bar_text = ""
        self.prompt_text = "► "
        self._lock = threading.RLock()
        self._active = False        # Dock drawn / cursor parked?
        self._lines = 2             # Dock height (bar + prompt)
        self._rows = 24
        self._region_bottom = 1     # Last row of the scroll region
        self._orig_write = None
        self._installed = False
        self._pinned = False        # Scroll region active?

    # ------------------------------------------------------------- setup --
    def install(self) -> None:
        """Pin the dock: draw on the last rows, set the scroll region above,
        hook stdout."""
        if self._installed:
            return
        self._installed = True
        if not sys.stdout.isatty():
            # Output to a pipe or a file: no status bar, no cursor moves — they
            # landed in the file as escape codes
            self._off = True
            return
        try:
            self._rows = shutil.get_terminal_size().lines
        except Exception:
            self._rows = 24
        if self._rows >= 6:
            self._pinned = True
            self._setup_region()
        # hook stdout (content writes just flow; feed() parks the cursor)
        dock = self
        orig = sys.stdout.write

        def write(s):
            dock.feed(s)
            return orig(s)

        sys.stdout.write = write   # type: ignore[method-assign]
        self._orig_write = orig

    def _setup_region(self) -> None:
        """\x1b[top;bottom r — scrolling affects only rows 1..bottom."""
        self._region_bottom = self._rows - self._lines
        sys.stdout.write(f"\x1b[1;{self._region_bottom}r")
        self._draw_dock_rows()

    def uninstall(self) -> None:
        """Reset the scroll region before exit."""
        with self._lock:
            if self._pinned:
                sys.stdout.write("\x1b[r")            # Full-screen region
                sys.stdout.flush()
                self._pinned = False
            if self._installed and self._orig_write:
                sys.stdout.write = self._orig_write   # type: ignore
                self._installed = False

    # ----------------------------------------------------------- drawing --
    def _draw_dock_rows(self, cursor_to_prompt: bool = False) -> None:
        """Rewrite the dock rows in place (absolute positioning). Then park
        the cursor: right after ► when input is about to be read (typed
        characters must appear there), else on the last region row."""
        bar_row = self._rows - 1
        prompt_row = self._rows
        sys.stdout.write(f"\x1b[{bar_row};1H\x1b[2K" + self.bar_text)
        sys.stdout.write(f"\x1b[{prompt_row};1H\x1b[2K" + self.prompt_text)
        if cursor_to_prompt:
            col = _vlen(self.prompt_text) + 1
            sys.stdout.write(f"\x1b[{prompt_row};{col}H")
        else:
            sys.stdout.write(f"\x1b[{self._region_bottom};1H")
        sys.stdout.flush()
        self._active = True

    def draw(self, bar_text: str = "", prompt_text: str = "") -> None:
        if getattr(self, "_off", False):
            return
        with self._lock:
            if bar_text:
                self.bar_text = bar_text
            if prompt_text:
                self.prompt_text = prompt_text
            if self._pinned:
                self._draw_dock_rows()
            else:
                sys.stdout.write(self.bar_text + "\n" + self.prompt_text)
                sys.stdout.flush()
                self._active = True

    def resize_maybe(self) -> bool:
        """Re-read the terminal size; if it changed, re-set the scroll
        region and redraw the dock on the new rows. Returns True when a
        resize was handled (frame.py's resize_maybe contract)."""
        with self._lock:
            if not self._pinned:
                return False
            old = (self._rows, self._region_bottom)
            try:
                self._rows = shutil.get_terminal_size().lines
            except Exception:
                return False
            if (self._rows, self._region_bottom) == old:
                return False
            self._setup_region()      # Re-set region + redraw dock rows
            return True

    def clear_for_input(self) -> None:
        """."""
        if getattr(self, "_off", False):
            return
        with self._lock:
            if self._pinned:
                self._draw_dock_rows(cursor_to_prompt=True)
            else:
                self._erase()
                self.draw()

    # ----------------------------------------------------------- interop --
    def feed(self, text: str) -> None:
        """stdout hook (pinned): the scroll region guarantees content never
        touches the dock rows. Park the cursor in the region before the
        first content write."""
        with self._lock:
            if self._pinned and not self._active:
                sys.stdout.write(f"\x1b[{self._region_bottom};1H")
                sys.stdout.flush()
                self._active = True

    # ------------------------------------------------- legacy (fallback) --
    def _erase(self) -> None:
        """rev-1 erase, non-pinned fallback only."""
        if not self._active:
            return
        n = self._lines
        sys.stdout.write(f"\x1b[{n}F")
        for _ in range(n):
            sys.stdout.write("\x1b[2K\x1b[1B")
        sys.stdout.write(f"\x1b[{n}F")
        sys.stdout.flush()
        self._active = False

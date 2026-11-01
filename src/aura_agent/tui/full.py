"""full.py — the fullscreen screen.

The inline screen (live.py) prints finished output into the terminal's own
scrollback. Fullscreen keeps it here instead and draws the whole window on the
terminal's alternate screen, like vim or htop: the conversation above, the
activity line, input box and status line fixed at the bottom.

Then: drag to select text, copied on release; a panel beside the chat (/diff:
what this chat changed).

Same interface as LiveRegion (render / print_above / resized / reset / clear),
so the chat draws through either one unchanged.
"""
from __future__ import annotations

import threading

from .live import SYNC_OFF, SYNC_ON
from .text import cut, pad, strip_ansi

MAX_LINES = 20_000          # Kept in memory; older lines leave the view
PANEL_MIN_COLS = 100        # Narrower: the panel takes the chat's place, no sidebar
SIDE_W = 36                 # The sidebar's width


class FullRegion:
    def __init__(self, term, history: list[str] | None = None):
        self.term = term
        self.lock = threading.RLock()
        self.lines: list[str] = []          # The live area (activity, box, status)
        self.cursor: tuple[int, int] | None = None
        self.history: list[str] = list(history or [])[-MAX_LINES:]
        self.offset = 0                     # Lines scrolled up from the bottom
        self.new_below = 0                  # Lines that arrived while scrolled up
        self.enabled = True
        self.panel = None                   # callable(width, height) -> lines
        self.panel_version = 0              # Bumped when what the panel shows changed
        self.side = None                    # The sidebar: callable(width, height) -> lines
        self.sel: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._plain: list[str] = []         # What's on screen, as plain text
        self._last = None

    # ------------------------------------------------------------ scroll --
    def view_rows(self) -> int:
        rows = self.term.size()[1]
        return max(1, rows - min(len(self.lines), max(1, rows - 3)))

    def scroll(self, delta: int) -> None:
        """delta > 0: up (older); < 0: down (newer)."""
        with self.lock:
            top = max(0, len(self.history) - self.view_rows() + 1)
            self.offset = max(0, min(top, self.offset + delta))
            if self.offset == 0:
                self.new_below = 0
            if getattr(self, "_sel_col", "chat") != "chat":
                self.sel = None            # A chat selection scrolls with it
            self.render(force=True)

    def page(self, direction: int) -> None:
        self.scroll(direction * max(1, self.view_rows() // 2))

    def to_top(self) -> None:
        self.scroll(len(self.history))

    def to_bottom(self) -> None:
        with self.lock:
            self.offset, self.new_below, self.sel = 0, 0, None
            self.render(force=True)

    # --------------------------------------------------------- selection --
    # A drag in the chat is kept in conversation lines, not screen rows, so the
    # view can scroll under it (the wheel, or holding the drag at the top /
    # bottom edge) and the selection grows to what was out of view. A drag in
    # the sidebar / diff panel stays on screen.
    def _cell(self, pos: tuple[int, int]) -> tuple[str, int, int]:
        """A mouse cell (1-based x, y) -> (column it is in, row, col); chat
        rows are conversation lines."""
        r, c = pos[1] - 1, pos[0] - 1
        split = getattr(self, "_split", None)
        view_h = getattr(self, "_view_h", 0) or 1
        if r >= view_h:
            return "bottom", r, c          # The / menu, a card, the box: as on screen
        if split is not None and c >= split:
            return "panel", r, c
        return "chat", getattr(self, "_top", 0) + max(0, min(r, view_h - 1)), c

    def anchor(self, pos: tuple[int, int]) -> None:
        """Where a drag starts."""
        with self.lock:
            self._anchor = self._cell(pos)
            self.sel = None
            self.render(force=True)

    def select_to(self, pos: tuple[int, int]) -> None:
        """The drag is at pos now: select from its start to here."""
        with self.lock:
            if getattr(self, "_anchor", None) is None:
                return
            col, r0, c0 = self._anchor
            _, r1, c1 = self._cell(pos)
            split = getattr(self, "_split", None)
            if col == "chat":
                lo, hi = 0, (split - 1 if split is not None else 10 ** 6)
            elif col == "panel":
                lo, hi = (split or 0) + 3, 10 ** 6
                r1 = pos[1] - 1
            else:                              # The bottom area: the whole width
                lo, hi = 0, 10 ** 6
                r1 = max(getattr(self, "_view_h", 0), pos[1] - 1)
            c0, c1 = max(lo, min(c0, hi)), max(lo, min(c1, hi))
            self._cols, self._sel_col = (lo, hi), col
            self.sel = ((r0, c0), (r1, c1))
            self.render(force=True)

    def select(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Mouse cells (1-based x, y) from the press to where it is now."""
        self.anchor(start)
        self.select_to(end)

    def edge(self, pos: tuple[int, int]) -> int:
        """A chat drag held at the top edge: 1 (scroll up); at the bottom
        edge: -1 (down); else 0."""
        r = pos[1] - 1
        view_h = getattr(self, "_view_h", 0)
        if not view_h or (getattr(self, "_anchor", None) or ("",))[0] != "chat":
            return 0
        return 1 if r <= 0 else -1 if r >= view_h - 1 else 0

    def _sel_lines(self) -> list[str]:
        """The lines a selection is in: the conversation, or the screen."""
        if getattr(self, "_sel_col", "chat") == "chat":
            return [strip_ansi(l) for l in self.history]
        return self._plain

    def selected_text(self) -> str:
        """The selected text, line by line, like a terminal (chat lines
        scrolled out of view included)."""
        if not self.sel:
            return ""
        (r0, c0), (r1, c1) = sorted(self.sel)
        lines = self._sel_lines()
        lo, hi = getattr(self, "_cols", (0, 10 ** 6))
        out = []
        for r in range(r0, r1 + 1):
            if not 0 <= r < len(lines):
                continue
            line = lines[r]
            a = c0 if r == r0 else lo
            b = c1 + 1 if r == r1 else min(len(line), hi + 1)
            out.append(line[a:b].rstrip())
        return "\n".join(out).strip("\n")

    def _highlight(self, row: int, styled: str) -> str:
        if not self.sel:
            return styled
        chat = getattr(self, "_sel_col", "chat") == "chat"
        if chat and row >= getattr(self, "_view_h", 0):
            return styled                  # The hint bar, the box: never
        cr = row + (getattr(self, "_top", 0) if chat else 0)
        (r0, c0), (r1, c1) = sorted(self.sel)
        if not r0 <= cr <= r1:
            return styled
        plain = self._plain[row] if row < len(self._plain) else strip_ansi(styled)
        lo, hi = getattr(self, "_cols", (0, 10 ** 6))
        a = c0 if cr == r0 else lo
        b = c1 + 1 if cr == r1 else min(len(plain), hi + 1)
        return plain[:a] + "\x1b[7m" + plain[a:b] + "\x1b[27m" + plain[b:]

    # ------------------------------------------------------------ widths --
    def _panel_w(self, cols: int) -> int:
        """How wide the diff panel or the sidebar is (0: none shown)."""
        limit = max(1, cols - 1)
        if self.panel is not None:
            return limit if cols < PANEL_MIN_COLS else max(30, cols * 45 // 100)
        if self.side is not None and cols >= PANEL_MIN_COLS:
            return SIDE_W
        return 0

    def _chat_w(self, cols: int) -> int:
        limit = max(1, cols - 1)
        if self.panel is not None:
            return 0 if cols < PANEL_MIN_COLS else max(20, limit - self._panel_w(cols) - 1)
        if self.side is not None and cols >= PANEL_MIN_COLS:
            return max(20, limit - SIDE_W - 1)
        return limit

    def chat_width(self) -> int:
        """The width text is wrapped to: the chat column, never under 20."""
        cols = self.term.size()[0]
        return max(20, self._chat_w(cols) or max(1, cols - 1))

    # -------------------------------------------------------------- draw --
    def render(self, lines: list[str] | None = None, cursor=None,
               force: bool = False) -> None:
        if not self.enabled:
            return
        with self.lock:
            if lines is not None:
                self.lines = list(lines)
                self.cursor = cursor
            cols, rows = self.term.size()
            limit = max(1, cols - 1)
            live = [cut(l, limit) for l in self.lines][-max(1, rows - 3):]
            view_h = rows - len(live)
            hint = ""
            if self.offset:
                hint = (f" ↑ scrolled up · {self.new_below} new line{'s' if self.new_below != 1 else ''} below · "
                        if self.new_below else " ↑ scrolled up · ") + "Ctrl+End or PgDn to follow "
                view_h -= 1
            # The diff panel (/diff) beside the chat — in its place on a narrow
            # window — else the sidebar on a wide one
            panel, chat_w, pw = None, self._chat_w(cols), self._panel_w(cols)
            if self.panel is not None:
                panel = list(self.panel(pw - 2, view_h) or [])[:view_h]
            elif pw:
                panel = list(self.side(pw - 2, view_h) or [])[:view_h]
            self._split = chat_w if panel is not None else None
            if panel is not None:
                panel += [""] * (view_h - len(panel))
            end = len(self.history) - self.offset
            self._top, self._view_h = end - view_h, view_h
            shown = [cut(l, max(1, chat_w)) for l in self.history[max(0, end - view_h):end]]
            body = [""] * (view_h - len(shown)) + shown
            if panel is not None:
                if chat_w == 0:
                    body = [" " + cut(p, limit - 1) for p in panel]
                else:
                    body = [pad(b, chat_w) + "\x1b[0m \x1b[2m│\x1b[0m " + cut(p, limit - chat_w - 3)
                            for b, p in zip(body, panel)]
            if hint:
                body.append("\x1b[7m" + cut(hint, limit) + "\x1b[0m")
            screen = (body + live)[:rows]
            key = (tuple(screen), self.cursor, self.sel, cols, rows)
            if not force and key == self._last:
                return                     # Nothing on screen changed
            self._last = key
            self._plain = [strip_ansi(l) for l in screen]
            out = [SYNC_ON, "\x1b[?25l", "\x1b[H"]
            for i, l in enumerate(screen):
                out.append("\x1b[0m" + self._highlight(i, l) + "\x1b[0m\x1b[K"
                           + ("\r\n" if i < len(screen) - 1 else ""))
            if self.cursor is not None and live:
                r, c = self.cursor
                r = max(0, min(r - (len(self.lines) - len(live)), len(live) - 1))
                out.append(f"\x1b[{rows - len(live) + r + 1};{c + 1}H\x1b[?25h")
            out.append(SYNC_OFF)
            self.term.write("".join(out))
            self.term.flush()

    def print_above(self, lines: list[str]) -> None:
        """Finished output joins the conversation; a scrolled-up view stays put."""
        if not lines:
            return
        with self.lock:
            self.history.extend(lines)
            over = len(self.history) - MAX_LINES
            if over > 0:
                del self.history[:over]
                if getattr(self, "_sel_col", "") == "chat" and self.sel:   # Keep it on its lines
                    (a, b), (c, d) = self.sel
                    self.sel = ((a - over, b), (c - over, d))
                if getattr(self, "_anchor", None) and self._anchor[0] == "chat":
                    self._anchor = ("chat", self._anchor[1] - over, self._anchor[2])
            if self.offset:
                self.offset += len(lines)
                self.new_below += len(lines)
            self.render(force=True)

    def resized(self) -> None:
        with self.lock:
            self.term.write("\x1b[2J")
            self.render(force=True)

    def reset(self) -> None:
        with self.lock:
            self._last = None

    def clear(self) -> None:
        with self.lock:
            self._last = None

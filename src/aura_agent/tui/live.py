"""live.py — the live area at the bottom, and printing above it.

Finished output is printed ABOVE the live area and from then on belongs
to the terminal's scrollback (native scroll, search, select, copy). The
live area — activity line, input box, status line — is redrawn in place:
move up to where it starts, clear to the end of the screen, draw again.
Redraws are wrapped in "synchronized output" so terminals that support it
paint them in one frame (no flicker); others ignore the wrapper.

LiveStdout replaces sys.stdout while the chat runs, so anything the rest
of Aura prints (commands, warnings) lands above the live area instead of
through it.
"""
from __future__ import annotations

import io
import threading

from .text import cut, width

SYNC_ON, SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"


class LiveRegion:
    def __init__(self, term):
        self.term = term
        self.lock = threading.RLock()
        self.lines: list[str] = []
        self.cursor: tuple[int, int] | None = None
        self._drawn_rows = 0          # Rows the last draw occupied
        self._cursor_row = 0          # Where the cursor was left, in rows
        self._cols = term.size()[0]
        self.enabled = True

    # -------------------------------------------------------------- draw --
    def _erase(self) -> str:
        """Escape text that moves to the region's first row and clears it
        all. Uses the row count of the previous draw."""
        if not self._drawn_rows:
            return "\r"
        up = self._cursor_row
        return (f"\x1b[{up}A" if up else "") + "\r\x1b[J"

    def _compose(self, lines: list[str], cursor) -> tuple[str, int, int]:
        cols, rows = self.term.size()
        self._cols = cols
        limit = max(1, cols - 1)
        lines = [cut(l, limit) for l in lines]
        max_rows = max(1, rows - 1)
        if len(lines) > max_rows:
            drop = len(lines) - max_rows
            lines = lines[drop:]
            if cursor:
                cursor = (max(0, cursor[0] - drop), cursor[1])
        out = "\r\n".join(lines)
        last = len(lines) - 1
        crow = last
        if cursor is not None and lines:
            r, c = cursor
            r = max(0, min(r, last))
            if last - r:
                out += f"\x1b[{last - r}A"
            out += "\r" + (f"\x1b[{c}C" if c > 0 else "")
            crow = r
        return out, len(lines), crow

    def _fit(self, lines: list[str], cursor):
        cols, rows = self.term.size()
        limit = max(1, cols - 1)
        fitted = [cut(l, limit) for l in lines]
        max_rows = max(1, rows - 1)
        if len(fitted) > max_rows:
            drop = len(fitted) - max_rows
            fitted = fitted[drop:]
            if cursor:
                cursor = (max(0, cursor[0] - drop), cursor[1])
        return fitted, cursor

    def render(self, lines: list[str] | None = None, cursor=None,
               force: bool = False) -> None:
        """Draw the live area. Nothing is written when nothing changed, and
        when only some rows changed only those rows are rewritten — the
        whole area is cleared and redrawn only when its height changes.
        (Rewriting everything on every tick is what makes other terminal
        agents flicker and smear their scrollback.)"""
        if not self.enabled:
            return
        with self.lock:
            if lines is not None:
                self.lines = list(lines)
                self.cursor = cursor
            fitted, cur = self._fit(self.lines, self.cursor)
            shown = getattr(self, "_shown", None)
            if not force and shown is not None and fitted == shown[0] \
                    and cur == shown[1] and self._drawn_rows == len(fitted):
                return
            show = "\x1b[?25h" if cur is not None else "\x1b[?25l"
            if not force and shown is not None and self._drawn_rows == len(fitted) \
                    and len(fitted) == len(shown[0]) and fitted:
                out = []
                row = self._cursor_row
                for i, (new, old) in enumerate(zip(fitted, shown[0])):
                    if new == old:
                        continue
                    if i < row:
                        out.append(f"\x1b[{row - i}A")
                    elif i > row:
                        out.append(f"\x1b[{i - row}B")
                    out.append("\r\x1b[2K\x1b[0m" + new)
                    row = i
                last = len(fitted) - 1
                target = cur[0] if cur is not None else last
                target = max(0, min(target, last))
                if target < row:
                    out.append(f"\x1b[{row - target}A")
                elif target > row:
                    out.append(f"\x1b[{target - row}B")
                out.append("\r" + (f"\x1b[{cur[1]}C" if cur and cur[1] > 0 else ""))
                self.term.write(SYNC_ON + "\x1b[?25l" + "".join(out) + show + SYNC_OFF)
                self.term.flush()
                self._cursor_row = target
                self._shown = (fitted, cur)
                return
            body, n, crow = self._compose(self.lines, self.cursor)
            self.term.write(SYNC_ON + "\x1b[?25l" + self._erase() + body + show + SYNC_OFF)
            self.term.flush()
            self._drawn_rows = n
            self._cursor_row = crow
            self._shown = (fitted, cur)

    def print_above(self, lines: list[str]) -> None:
        """Lines that become permanent scrollback, then the live area again."""
        if not lines:
            return
        with self.lock:
            if not self.enabled:
                self.term.write("\r\n".join(lines) + "\r\n")
                self.term.flush()
                return
            body, n, crow = self._compose(self.lines, self.cursor)
            text = "\r\n".join(lines) + "\r\n"
            show = "\x1b[?25h" if self.cursor is not None else "\x1b[?25l"
            self.term.write(SYNC_ON + "\x1b[?25l" + self._erase() + "\x1b[0m" + text
                            + body + show + SYNC_OFF)
            self.term.flush()
            self._drawn_rows = n
            self._cursor_row = crow
            self._shown = self._fit(self.lines, self.cursor)

    def resized(self) -> None:
        """After a resize the old drawing may have re-wrapped: clear
        generously from where it could start."""
        with self.lock:
            new_cols = self.term.size()[0]
            shown = (getattr(self, "_shown", None) or ([], None))[0]
            if self._drawn_rows and new_cols < self._cols and shown:
                # terminals that re-flow (Windows Terminal, VS Code, most
                # modern ones) now show each old line on more rows
                above = shown[:self._cursor_row]
                self._cursor_row = sum(max(1, -(-width(l) // new_cols)) for l in above)
            self._shown = None
            self.render(force=True)

    def reset(self) -> None:
        """Forget the drawing (the screen was cleared)."""
        with self.lock:
            self._drawn_rows = 0
            self._cursor_row = 0
            self._shown = None

    def clear(self) -> None:
        """Remove the live area (before exit or handing over the tty)."""
        with self.lock:
            self.term.write(self._erase() + "\x1b[?25h")
            self.term.flush()
            self.reset()


class LiveStdout(io.TextIOBase):
    """sys.stdout stand-in: complete lines go above the live area."""

    def __init__(self, region: LiveRegion, encoding: str = "utf-8"):
        self.region = region
        self._buf = ""
        self._lock = threading.RLock()
        self._encoding = encoding

    @property
    def encoding(self):          # noqa: D401 - io interface
        return self._encoding

    def isatty(self) -> bool:
        return True

    def fileno(self):
        return self.region.term.stream.fileno()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._buf += s
            if "\n" in self._buf:
                *done, self._buf = self._buf.split("\n")
                self.region.print_above([_last_cr(l) for l in done])
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                line, self._buf = self._buf, ""
                self.region.print_above([_last_cr(line)])


def _last_cr(line: str) -> str:
    """'progress 10%\\rprogress 20%' → the text after the last \\r."""
    return line.rsplit("\r", 1)[-1] if "\r" in line else line

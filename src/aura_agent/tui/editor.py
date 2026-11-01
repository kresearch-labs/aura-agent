"""editor.py — the input box's text model. No terminal code: every
operation is a pure change of (text, cursor), so it is fully testable.

Readline-style editing (Ctrl+A/E/K/U/W/Y, Alt+B/F/D, Alt+Y kill ring,
Ctrl+_ undo), multi-line text with up/down moving by screen row, history
with prefix search, a stash (Ctrl+S) and large pastes kept as a short
placeholder chip that expands when sent.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .text import char_width

PASTE_CHIP_LINES = 6        # Pastes longer than this become a chip
PASTE_CHIP_CHARS = 800
CHIP = r"\[(Pasted text|File) #(\d+) · [^\]\n]*\]"   # A chip in the box
PASTE_GAP = 0.06            # Seconds: pieces of one paste arrive closer than this


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _is_word(ch: str) -> bool:
    return ch.isalnum()


@dataclass
class Editor:
    text: str = ""
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    kill_ring: list[str] = field(default_factory=list)
    pastes: dict[int, str] = field(default_factory=dict)
    files: dict[int, str] = field(default_factory=dict)       # [File #n · …] chips → paths
    file_text: dict[int, str] = field(default_factory=dict)   # What each becomes (the app, at send)
    _undo: list[tuple[str, int]] = field(default_factory=list)
    _hist_i: int | None = None
    _hist_draft: str = ""
    _hist_prefix: str = ""
    _last_yank: tuple[int, int] | None = None
    _yank_i: int = 0
    _stash: tuple[str, int] | None = None
    _paste_n: int = 0
    # The paste being received: Windows hands a big paste over in pieces (each
    # console read is a burst of its own), so pieces that follow each other
    # within PASTE_GAP are one paste — judged (text or chip) as a whole
    _pasting: dict | None = None

    # ------------------------------------------------------------ basics --
    def set(self, text: str, cursor: int | None = None) -> None:
        self.text = text
        self.cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))

    def _snap(self) -> None:
        if not self._undo or self._undo[-1] != (self.text, self.cursor):
            self._undo.append((self.text, self.cursor))
            if len(self._undo) > 200:
                self._undo.pop(0)

    def undo(self) -> None:
        while self._undo:
            t, c = self._undo.pop()
            if (t, c) != (self.text, self.cursor):
                self.text, self.cursor = t, c
                return

    def insert(self, s: str) -> None:
        if not s:
            return
        self._snap()
        self.text = self.text[:self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)
        self._last_yank = None
        self._hist_i = None

    def pasting(self, gap: float = 0.0) -> bool:
        """A paste arrived within `gap` seconds (PASTE_GAP by default) and
        nothing was typed since: more of it may be coming."""
        p = self._pasting
        return bool(p) and self.cursor == p["end"] and \
            time.monotonic() - p["t"] < (gap or PASTE_GAP)

    def paste(self, s: str) -> None:
        """Short pastes go in as text; long ones as a chip that expands
        when the message is sent (the box stays readable). A piece that
        follows the last one at once is the same paste."""
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        if not s:
            return
        one = s.strip().strip('"').strip("'")
        if not self.pasting() and "\n" not in one and 2 < len(one) < 400:
            try:                        # A pasted path to a file (Copy as path, a drag)
                from pathlib import Path as _P
                if _P(one).is_absolute() and _P(one).is_file():
                    self.attach(one, _P(one).name)
                    return
            except (OSError, ValueError):
                pass
        prev_chip = None
        if self.pasting():
            p = self._pasting
            prev_chip = p.get("chip")
            whole = p["text"] + s
            self.text = self.text[:p["start"]] + self.text[p["end"]:]
            self.cursor = p["start"]
            if p.get("chip"):
                self.pastes.pop(p["chip"], None)
        else:
            self._snap()
            whole = s
        start = self.cursor
        lines = whole.rstrip("\n").count("\n") + 1     # A last newline is no line of its own
        chip = None
        if lines > PASTE_CHIP_LINES or len(whole) > PASTE_CHIP_CHARS:
            chip = prev_chip
            if chip is None:
                self._paste_n += 1
                chip = self._paste_n
            self.pastes[chip] = whole
            from ..attach import tokens as _tok
            shown = f"[Pasted text #{chip} · {lines} lines · ~{_k(_tok(whole))} tokens]"
        else:
            shown = whole
        self.text = self.text[:start] + shown + self.text[start:]
        self.cursor = start + len(shown)
        self._last_yank = None
        self._hist_i = None
        self._pasting = {"t": time.monotonic(), "start": start, "end": self.cursor,
                         "text": whole, "chip": chip}

    def expanded(self) -> str:
        """The text to send: paste chips replaced by their content."""
        def sub(m):
            n = int(m.group(2))
            if m.group(1) == "File":
                return self.file_text.get(n, f"[file {self.files.get(n, '?')}]")
            return self.pastes.get(n, m.group(0))
        return re.sub(CHIP, sub, self.text)

    def attach(self, path: str, label: str) -> None:
        """."""
        self._paste_n += 1
        self.files[self._paste_n] = str(path)
        self.insert(f"[File #{self._paste_n} · {label}] ")

    def clear(self) -> str:
        t = self.text
        self._snap()
        self.text, self.cursor = "", 0
        self._hist_i = None
        return t

    def submit(self) -> str:
        """Take the text out (expanded) and record it in history."""
        raw = self.text
        full = self.expanded()
        if raw.strip() and (not self.history or self.history[-1] != raw):
            self.history.append(raw)
        self.text, self.cursor = "", 0
        self._undo.clear()
        self._hist_i = None
        return full

    # ------------------------------------------------------------ motion --
    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def line_start(self) -> None:
        self.cursor = self.text.rfind("\n", 0, self.cursor) + 1

    def line_end(self) -> None:
        e = self.text.find("\n", self.cursor)
        self.cursor = len(self.text) if e < 0 else e

    def buf_start(self) -> None:
        self.cursor = 0

    def buf_end(self) -> None:
        self.cursor = len(self.text)

    def word_left(self) -> None:
        i = self.cursor
        while i > 0 and not _is_word(self.text[i - 1]):
            i -= 1
        while i > 0 and _is_word(self.text[i - 1]):
            i -= 1
        self.cursor = i

    def word_right(self) -> None:
        i, n = self.cursor, len(self.text)
        while i < n and not _is_word(self.text[i]):
            i += 1
        while i < n and _is_word(self.text[i]):
            i += 1
        self.cursor = i

    # ------------------------------------------------------------ delete --
    def backspace(self) -> None:
        if self.cursor == 0:
            return
        self._snap()
        # a paste chip is removed whole
        m = re.search(CHIP + "$", self.text[:self.cursor])
        start = m.start() if m else self.cursor - 1
        self.text = self.text[:start] + self.text[self.cursor:]
        self.cursor = start

    def delete(self) -> None:
        if self.cursor >= len(self.text):
            return
        self._snap()
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]

    def _kill(self, a: int, b: int, prepend: bool = False) -> None:
        if a == b:
            return
        self._snap()
        piece = self.text[a:b]
        self.text = self.text[:a] + self.text[b:]
        self.cursor = a
        self.kill_ring.append(piece)
        self.kill_ring[:] = self.kill_ring[-20:]
        self._last_yank = None

    def kill_to_end(self) -> None:          # Ctrl+K
        e = self.text.find("\n", self.cursor)
        e = len(self.text) if e < 0 else e
        if e == self.cursor and e < len(self.text):
            e += 1                             # At line end: join lines
        self._kill(self.cursor, e)

    def kill_to_start(self) -> None:        # Ctrl+U
        s = self.text.rfind("\n", 0, self.cursor) + 1
        if s == self.cursor and s > 0:
            s -= 1
        self._kill(s, self.cursor)

    def kill_word_back_ws(self) -> None:    # Ctrl+W: back to whitespace
        i = self.cursor
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        self._kill(i, self.cursor)

    def kill_word_back(self) -> None:       # Ctrl/Alt+Backspace
        i = self.cursor
        self.word_left()
        j = self.cursor
        self.cursor = i
        self._kill(j, i)

    def kill_word_fwd(self) -> None:        # Alt+D
        i = self.cursor
        self.word_right()
        j = self.cursor
        self.cursor = i
        self._kill(i, j)

    def yank(self) -> None:                 # Ctrl+Y
        if not self.kill_ring:
            return
        self._yank_i = len(self.kill_ring) - 1
        piece = self.kill_ring[self._yank_i]
        start = self.cursor
        self.insert(piece)
        self._last_yank = (start, self.cursor)

    def yank_pop(self) -> None:             # Alt+Y after Ctrl+Y
        if not self._last_yank or len(self.kill_ring) < 2:
            return
        a, b = self._last_yank
        self._yank_i = (self._yank_i - 1) % len(self.kill_ring)
        piece = self.kill_ring[self._yank_i]
        self.text = self.text[:a] + piece + self.text[b:]
        self.cursor = a + len(piece)
        self._last_yank = (a, self.cursor)

    # ----------------------------------------------------------- history --
    def history_prev(self) -> bool:
        """Older entry; with text typed, only entries that start with it.
        Returns False when there is nothing older."""
        if self._hist_i is None:
            self._hist_draft = self.text
            self._hist_prefix = self.text if "\n" not in self.text else ""
            i = len(self.history)
        else:
            i = self._hist_i
        while i > 0:
            i -= 1
            if self.history[i].startswith(self._hist_prefix) and \
                    self.history[i] != self.text:
                self._hist_i = i
                self.set(self.history[i])
                return True
        return False

    def history_next(self) -> bool:
        if self._hist_i is None:
            return False
        i = self._hist_i
        while i < len(self.history) - 1:
            i += 1
            if self.history[i].startswith(self._hist_prefix):
                self._hist_i = i
                self.set(self.history[i])
                return True
        self._hist_i = None
        self.set(self._hist_draft)
        return True

    def search(self, query: str, before: int | None = None) -> int | None:
        """Newest history index < before whose text contains query."""
        i = len(self.history) if before is None else before
        q = query.lower()
        seen = set()
        while i > 0:
            i -= 1
            h = self.history[i]
            if h in seen:
                continue
            seen.add(h)
            if q in h.lower():
                return i
        return None

    # ------------------------------------------------------------- stash --
    def stash_toggle(self) -> str:
        """Ctrl+S: park the draft / bring it back. Returns what happened."""
        if self.text:
            self._stash = (self.text, self.cursor)
            self.text, self.cursor = "", 0
            return "stashed"
        if self._stash:
            self.text, self.cursor = self._stash
            self._stash = None
            return "restored"
        return ""

    # ----------------------------------------------------------- layout --
    def layout(self, width: int, first_prefix: int = 0, prefix: int = 0
               ) -> tuple[list[tuple[int, int]], tuple[int, int]]:
        """Screen rows as (start, end) text indexes, and the cursor's
        (row, column-in-cells). Rows wrap at `width` cells; the first row
        starts after `first_prefix` cells, later rows after `prefix`."""
        rows: list[tuple[int, int]] = []
        cur = (0, first_prefix)
        start = 0
        col = first_prefix
        i = 0
        t = self.text
        n = len(t)
        found = False
        while i <= n:
            if i == self.cursor and not found:
                cur = (len(rows), col)
                found = True
            if i == n:
                rows.append((start, n))
                break
            ch = t[i]
            if ch == "\n":
                rows.append((start, i))
                i += 1
                start = i
                col = prefix
                continue
            w = max(char_width(ch), 1 if ch == "\t" else 0) if ch != "\t" else 4
            if col + w > width and i > start:
                rows.append((start, i))
                start = i
                col = prefix
                if i == self.cursor:
                    cur = (len(rows), col)
            col += w
            i += 1
        if cur[1] >= width:
            cur = (cur[0] + 1, prefix)
            if cur[0] >= len(rows):
                rows.append((n, n))
        return rows, cur

    def move_row(self, delta: int, width: int, first_prefix: int = 0,
                 prefix: int = 0) -> bool:
        """False when it is already on the first/last row (the caller then uses
        history).
        """
        rows, (r, c) = self.layout(width, first_prefix, prefix)
        nr = r + delta
        if nr < 0 or nr >= len(rows):
            return False
        a, b = rows[nr]
        col = first_prefix if nr == 0 else prefix
        i = a
        while i < b and col + char_width(self.text[i]) <= c:
            col += char_width(self.text[i])
            i += 1
        self.cursor = i
        return True

    # --------------------------------------------------------- completion --
    def token_at_cursor(self) -> tuple[str, int]:
        """The word being typed before the cursor and where it starts."""
        i = self.cursor
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        return self.text[i:self.cursor], i

    def replace_token(self, start: int, new: str) -> None:
        self._snap()
        end = self.cursor
        while end < len(self.text) and not self.text[end].isspace():
            end += 1
        self.text = self.text[:start] + new + self.text[end:]
        self.cursor = start + len(new)

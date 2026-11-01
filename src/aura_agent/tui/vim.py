"""vim.py — vim keys for the input box (/vim turns them on or off).

INSERT mode types as usual; Esc goes to NORMAL. NORMAL mode: move h l j k · w b
e · 0 ^ $ · gg G · f{c} F{c} edit x X · dd D · cc C S · dw de db · cw ce cb ·
r{c} · ~ · J copy yy yw · p P enter i a I A o O other u (undo) ·. (repeat last
change) · Enter sends Counts work for motions and x/dd (3w, 2dd).
"""
from __future__ import annotations

from .editor import Editor, _is_word
from .keys import Key


class Vim:
    def __init__(self):
        self.mode = "insert"
        self.pending = ""           # Operator / prefix waiting for more keys
        self.count = ""
        self.register = ""
        self.last_change: list[Key] = []
        self._recording: list[Key] | None = None

    # ---------------------------------------------------------- motions --
    @staticmethod
    def _end_of_word(ed: Editor) -> int:
        i, n = ed.cursor, len(ed.text)
        if i < n - 1:
            i += 1
        while i < n and not _is_word(ed.text[i]):
            i += 1
        while i < n - 1 and _is_word(ed.text[i + 1]):
            i += 1
        return min(i, max(0, n - 1))

    def _motion(self, ed: Editor, ch: str, width: int) -> int | None:
        """Target index for a motion key, or None if not a motion."""
        save = ed.cursor
        t = ed.text
        if ch == "h":
            return max(0, ed.cursor - 1) if ed.cursor > t.rfind("\n", 0, ed.cursor) + 1 else ed.cursor
        if ch == "l":
            end = t.find("\n", ed.cursor)
            end = len(t) if end < 0 else end
            return min(ed.cursor + 1, max(end - 1, t.rfind("\n", 0, ed.cursor) + 1))
        if ch == "w":
            ed.word_right()
            i = ed.cursor
            while i < len(t) and not _is_word(t[i]) and t[i] != "\n":
                i += 1
            ed.cursor = save
            return i
        if ch == "b":
            ed.word_left()
            r = ed.cursor
            ed.cursor = save
            return r
        if ch == "e":
            return self._end_of_word(ed)
        if ch == "0":
            return t.rfind("\n", 0, ed.cursor) + 1
        if ch == "^":
            i = t.rfind("\n", 0, ed.cursor) + 1
            while i < len(t) and t[i] in " \t":
                i += 1
            return i
        if ch == "$":
            e = t.find("\n", ed.cursor)
            return (len(t) if e < 0 else e)
        return None

    # -------------------------------------------------------------- keys --
    def handle(self, k: Key, ed: Editor, width: int) -> bool | str:
        """True = consumed. "submit" = Enter in NORMAL mode. False = not a
        vim key (the app handles it)."""
        if self.mode == "insert":
            if k.id == "escape":
                self.mode = "normal"
                if self._recording is not None:
                    self.last_change = self._recording
                    self._recording = None
                if ed.cursor > ed.text.rfind("\n", 0, ed.cursor) + 1:
                    ed.left()
                return True
            if self._recording is not None and k.id in ("char", "backspace", "paste"):
                self._recording.append(k)
            return False
        # NORMAL mode
        if k.id == "enter":
            return "submit"
        if k.id != "char":
            if k.id == "escape" and (self.pending or self.count):
                self.pending, self.count = "", ""
                return True
            return False if k.id in ("escape", "ctrl+c", "ctrl+d", "shift+tab",
                                     "ctrl+r", "ctrl+o", "ctrl+t", "ctrl+g",
                                     "ctrl+l", "up", "down", "tab") else True
        ch = k.text
        # counts
        if ch.isdigit() and (ch != "0" or self.count):
            self.count += ch
            return True
        n = int(self.count or 1)
        seq = self.pending + ch
        self.pending = ""
        self.count = ""
        ed._snap()
        # pending f/F/r targets
        if seq[:1] in ("f", "F", "r") and len(seq) == 2:
            c = seq[1]
            if seq[0] == "r":
                if ed.cursor < len(ed.text) and ed.text[ed.cursor] != "\n":
                    ed.text = ed.text[:ed.cursor] + c + ed.text[ed.cursor + 1:]
                    self.last_change = [Key("char", "r"), Key("char", c)]
            else:
                line_end = ed.text.find("\n", ed.cursor)
                line_end = len(ed.text) if line_end < 0 else line_end
                line_start = ed.text.rfind("\n", 0, ed.cursor) + 1
                if seq[0] == "f":
                    j = ed.text.find(c, ed.cursor + 1, line_end)
                else:
                    j = ed.text.rfind(c, line_start, ed.cursor)
                if j >= 0:
                    ed.cursor = j
            return True
        if seq in ("f", "F", "r", "g", "d", "c", "y") or (seq[:1] in "dcy" and len(seq) == 1):
            self.pending = seq
            self.count = str(n) if n > 1 else ""
            return True
        if seq == "gg":
            ed.cursor = 0
            return True
        if seq == "G":
            ed.cursor = ed.text.rfind("\n") + 1
            return True
        # operators with a motion
        if len(seq) == 2 and seq[0] in "dcy":
            op, mot = seq[0], seq[1]
            if mot == op:                       # Dd cc yy: the line
                a = ed.text.rfind("\n", 0, ed.cursor) + 1
                b = ed.text.find("\n", ed.cursor)
                b = len(ed.text) if b < 0 else b
                for _ in range(n - 1):
                    nb = ed.text.find("\n", b + 1)
                    b = len(ed.text) if nb < 0 else nb
                piece = ed.text[a:b]
                if op == "y":
                    self.register = piece + "\n"
                    return True
                if op == "d":
                    # take the newline too
                    if b < len(ed.text):
                        b += 1
                    elif a > 0:
                        a -= 1
                self.register = piece + "\n"
                ed.text = ed.text[:a] + ("" if op == "d" else "") + ed.text[b:]
                ed.cursor = min(a, len(ed.text))
                if op == "c":
                    ed.text = ed.text[:a] + ed.text[a:]
                    self._insert_mode(seq)
                self.last_change = [Key("char", op), Key("char", mot)]
                return True
            start = ed.cursor
            tgt = start
            for _ in range(n):
                ed.cursor = tgt
                m = self._motion(ed, "e" if (op == "c" and mot == "w") else mot, width)
                if m is None:
                    ed.cursor = start
                    return True
                tgt = m + (1 if mot in ("e",) or (op == "c" and mot == "w") else 0)
            ed.cursor = start
            a, b = sorted((start, min(tgt, len(ed.text))))
            self.register = ed.text[a:b]
            if op != "y":
                ed.text = ed.text[:a] + ed.text[b:]
                ed.cursor = a
                self.last_change = [Key("char", op), Key("char", mot)]
            if op == "c":
                self._insert_mode(seq)
            return True
        # simple commands
        t = ed.text
        if seq in "hlwbe0^$":
            for _ in range(n):
                m = self._motion(ed, seq, width)
                if m is not None:
                    ed.cursor = m
            return True
        if seq == "j":
            ed.move_row(1, width, 2, 2) or ed.history_next()
            return True
        if seq == "k":
            ed.move_row(-1, width, 2, 2) or ed.history_prev()
            return True
        if seq == "x":
            b = min(len(t), ed.cursor + n)
            self.register = t[ed.cursor:b]
            ed.text = t[:ed.cursor] + t[b:]
            ed.cursor = min(ed.cursor, max(0, len(ed.text) - 1))
            self.last_change = [Key("char", "x")]
            return True
        if seq == "X":
            a = max(0, ed.cursor - n)
            ed.text = t[:a] + t[ed.cursor:]
            ed.cursor = a
            return True
        if seq in ("D", "C"):
            e = t.find("\n", ed.cursor)
            e = len(t) if e < 0 else e
            self.register = t[ed.cursor:e]
            ed.text = t[:ed.cursor] + t[e:]
            if seq == "C":
                self._insert_mode(seq)
            else:
                ed.cursor = max(0, ed.cursor - 1) if ed.cursor >= len(ed.text) and ed.cursor else ed.cursor
            return True
        if seq == "S":
            a = t.rfind("\n", 0, ed.cursor) + 1
            e = t.find("\n", ed.cursor)
            e = len(t) if e < 0 else e
            ed.text = t[:a] + t[e:]
            ed.cursor = a
            self._insert_mode(seq)
            return True
        if seq in ("p", "P") and self.register:
            if self.register.endswith("\n"):
                if seq == "p":
                    e = t.find("\n", ed.cursor)
                    e = len(t) if e < 0 else e
                    ed.text = t[:e] + "\n" + self.register.rstrip("\n") + t[e:]
                    ed.cursor = e + 1
                else:
                    a = t.rfind("\n", 0, ed.cursor) + 1
                    ed.text = t[:a] + self.register + t[a:]
                    ed.cursor = a
            else:
                at = ed.cursor + (1 if seq == "p" and t else 0)
                ed.text = t[:at] + self.register * n + t[at:]
                ed.cursor = at + len(self.register) * n - 1
            return True
        if seq == "~":
            if ed.cursor < len(t):
                c = t[ed.cursor]
                ed.text = t[:ed.cursor] + c.swapcase() + t[ed.cursor + 1:]
                ed.cursor = min(len(ed.text) - 1, ed.cursor + 1)
            return True
        if seq == "J":
            e = t.find("\n", ed.cursor)
            if e >= 0:
                ed.text = t[:e] + " " + t[e + 1:].lstrip()
                ed.cursor = e
            return True
        if seq == "u":
            ed.undo()
            return True
        if seq == ".":
            for kk in self.last_change:
                self.handle(kk, ed, width)
            return True
        if seq in "iaIAoO":
            if seq == "a" and t:
                ed.cursor = min(len(t), ed.cursor + 1)
            elif seq == "I":
                ed.line_start()
            elif seq == "A":
                ed.line_end()
            elif seq == "o":
                ed.line_end()
                ed.insert("\n")
            elif seq == "O":
                ed.line_start()
                ed.insert("\n")
                ed.left()
            self._insert_mode(seq)
            return True
        return True

    def _insert_mode(self, how: str) -> None:
        self.mode = "insert"
        self._recording = []

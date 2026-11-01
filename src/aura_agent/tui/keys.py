"""keys.py — key events and the VT input parser.

A Key has an `id` like "a", "enter", "shift+enter", "ctrl+left", "alt+b",
"paste", "resize", "focus_in" — the app matches on ids only, so Windows
console input and POSIX/VT input end up identical.

VTParser turns terminal input text into keys. It understands CSI/SS3
cursor keys with modifiers (xterm), the kitty keyboard protocol (CSI u)
and xterm modifyOtherKeys (shift+enter as ESC[27;2;13~), bracketed
paste, focus reports and SGR mouse (ignored).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Key:
    id: str
    text: str = ""          # Printable text for "char" / paste payload
    meta: dict = field(default_factory=dict)

    @property
    def is_char(self) -> bool:
        return self.id == "char"

    def __repr__(self) -> str:   # pragma: no cover - debug aid
        return f"Key({self.id!r}{', ' + repr(self.text) if self.text else ''})"


_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left",
              "H": "home", "F": "end", "Z": "shift+tab",
              "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_TILDE = {"1": "home", "2": "insert", "3": "delete", "4": "end",
          "5": "pageup", "6": "pagedown", "7": "home", "8": "end",
          "11": "f1", "12": "f2", "13": "f3", "14": "f4", "15": "f5",
          "17": "f6", "18": "f7", "19": "f8", "20": "f9", "21": "f10",
          "23": "f11", "24": "f12"}
_CSI_U = {13: "enter", 9: "tab", 27: "escape", 127: "backspace", 8: "backspace",
          32: "space"}


def _mods(n: int) -> str:
    """xterm modifier number → 'shift+ctrl+' prefix."""
    n -= 1
    out = ""
    if n & 4:
        out += "ctrl+"
    if n & 2:
        out += "alt+"
    if n & 1:
        out += "shift+"
    return out


def control_key(ch: str) -> Key | None:
    """A single control character → key (None for printable)."""
    o = ord(ch)
    if ch == "\r":
        return Key("enter")
    if ch == "\n":
        return Key("ctrl+j")          # Newline in the input box
    if ch == "\t":
        return Key("tab")
    if ch in ("\x7f", "\x08"):
        return Key("backspace")
    if ch == "\x1b":
        return Key("escape")
    if ch == "\x00":
        return Key("ctrl+space")
    if ch == "\x1f":
        return Key("ctrl+_")
    if 1 <= o <= 26:
        return Key("ctrl+" + chr(o + 96))
    if o < 32:
        return Key("ctrl+" + chr(o + 64).lower())
    return None


class VTParser:
    """Incremental: feed() text, get keys. A lone ESC stays pending until
    flush() (the reader calls it after a short quiet gap)."""

    def __init__(self):
        self.buf = ""
        self.pasting = False
        self.paste = ""

    def feed(self, data: str) -> list[Key]:
        self.buf += data
        out: list[Key] = []
        while self.buf:
            if self.pasting:
                end = self.buf.find("\x1b[201~")
                if end < 0:
                    # keep a possible partial terminator in the buffer
                    keep = 0
                    for k in range(1, 6):
                        if "\x1b[201~".startswith(self.buf[-k:]):
                            keep = k
                    self.paste += self.buf[:len(self.buf) - keep]
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                self.paste += self.buf[:end]
                self.buf = self.buf[end + 6:]
                self.pasting = False
                out.append(Key("paste", self.paste.replace("\r\n", "\n").replace("\r", "\n")))
                self.paste = ""
                continue
            ch = self.buf[0]
            if ch != "\x1b":
                self.buf = self.buf[1:]
                ck = control_key(ch)
                out.append(ck if ck else Key("char", ch))
                continue
            k, used = self._escape(self.buf)
            if used == 0:          # Incomplete sequence: wait for more
                break
            self.buf = self.buf[used:]
            if k is not None:
                out.append(k)
        return out

    def flush(self) -> list[Key]:
        """Quiet gap: whatever is pending is a plain ESC (or alt+ESC)."""
        if self.pasting or not self.buf:
            return []
        out = []
        if self.buf == "\x1b":
            out.append(Key("escape"))
            self.buf = ""
        elif self.buf.startswith("\x1b"):
            rest = self.buf[1:]
            self.buf = ""
            out.append(Key("escape"))
            out.extend(self.feed(rest))
        return out

    # -------------------------------------------------------------- escape --
    def _escape(self, s: str) -> tuple[Key | None, int]:
        if len(s) == 1:
            return None, 0
        c1 = s[1]
        if c1 == "[":
            return self._csi(s)
        if c1 == "O":
            if len(s) < 3:
                return None, 0
            name = _CSI_FINAL.get(s[2])
            return (Key(name) if name else None), 3
        if c1 == "]":                 # OSC reply (e.g. background colour)
            end_bel = s.find("\x07", 2)
            end_st = s.find("\x1b\\", 2)
            ends = [e for e in (end_bel, end_st) if e >= 0]
            if not ends:
                return None, 0
            e = min(ends)
            body = s[2:e]
            used = e + (1 if e == end_bel else 2)
            return Key("osc", body), used
        if c1 == "\x1b":
            return Key("escape"), 1
        # alt + key
        ck = control_key(c1)
        if ck is not None:
            if ck.id == "enter":
                return Key("alt+enter"), 2
            if ck.id == "backspace":
                return Key("alt+backspace"), 2
            return Key("alt+" + ck.id), 2
        return Key("alt+" + c1.lower(), c1), 2

    def _csi(self, s: str) -> tuple[Key | None, int]:
        # find the final byte (0x40–0x7e) after parameter bytes
        i = 2
        while i < len(s) and not ("\x40" <= s[i] <= "\x7e"):
            i += 1
        if i >= len(s):
            return None, 0
        params, final = s[2:i], s[i]
        used = i + 1
        if params == "200" and final == "~":
            self.pasting = True
            self.paste = ""
            return None, used
        if final == "I" and params == "":
            return Key("focus_in"), used
        if final == "O" and params == "":
            return Key("focus_out"), used
        if params.startswith("<") and final in "Mm":     # SGR mouse
            try:
                btn, x, y = (int(v) for v in params[1:].split(";")[:3])
            except ValueError:
                return None, used
            if btn == 64:
                return Key("wheel_up"), used
            if btn == 65:
                return Key("wheel_down"), used
            # The left button: press, drag (motion with it held), release —
            # selecting text in fullscreen
            if btn == 0:
                return Key("mouse_down" if final == "M" else "mouse_up", meta={"x": x, "y": y}), used
            if btn == 32:
                return Key("mouse_drag", meta={"x": x, "y": y}), used
            return None, used
        parts = params.split(";") if params else []
        if final == "u":                                  # Kitty: CSI code;mods u
            try:
                code = int(parts[0].split(":")[0]) if parts else 0
                mods = int(parts[1].split(":")[0]) if len(parts) > 1 else 1
            except ValueError:
                return None, used
            prefix = _mods(mods)
            name = _CSI_U.get(code)
            if name:
                return Key(prefix + name), used
            if 32 < code < 0x110000:
                ch = chr(code)
                if prefix in ("", "shift+"):
                    return Key("char", ch.upper() if prefix else ch), used
                return Key(prefix + ch.lower(), ch), used
            return None, used
        if final == "~":
            if parts and parts[0] == "27" and len(parts) >= 3:  # ModifyOtherKeys
                try:
                    mods, code = int(parts[1]), int(parts[2])
                except ValueError:
                    return None, used
                name = _CSI_U.get(code)
                prefix = _mods(mods)
                if name:
                    return Key(prefix + name), used
                if 32 < code < 0x110000:
                    return Key(prefix + chr(code).lower(), chr(code)), used
                return None, used
            name = _TILDE.get(parts[0] if parts else "")
            if not name:
                return None, used
            prefix = _mods(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else ""
            return Key(prefix + name), used
        name = _CSI_FINAL.get(final)
        if name:
            prefix = ""
            if len(parts) >= 2 and parts[1].isdigit():
                prefix = _mods(int(parts[1]))
            if name == "shift+tab":
                return Key("shift+tab"), used
            return Key(prefix + name), used
        return None, used

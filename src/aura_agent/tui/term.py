"""term.py — the real terminal: output, raw input, size, modes.

Windows reads console input records (ReadConsoleInputW) so Shift+Enter,
Ctrl+arrows, Alt+keys, focus and resize arrive as real events — most CLI
tools on Windows can't tell Shift+Enter from Enter. POSIX switches the tty
to raw mode and parses VT input (with the kitty keyboard protocol and
xterm modifyOtherKeys turned on, so Shift+Enter works there too).

All modes are restored on exit, including after a crash (atexit).
"""
from __future__ import annotations

import atexit
import os
import queue
import shutil
import sys
import threading
import time

from .keys import Key, VTParser

IS_WIN = os.name == "nt"


class Terminal:
    def __init__(self, stream=None):
        self.stream = stream or sys.__stdout__
        self.keys: "queue.Queue[Key]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._restore: list = []
        self.active = False
        self.focused = True
        self.background_rgb: tuple[int, int, int] | None = None

    # ------------------------------------------------------------ output --
    def write(self, s: str) -> None:
        try:
            self.stream.write(s)
        except UnicodeEncodeError:
            enc = getattr(self.stream, "encoding", None) or "utf-8"
            self.stream.write(s.encode(enc, "replace").decode(enc, "replace"))

    def flush(self) -> None:
        try:
            self.stream.flush()
        except Exception:   # noqa: BLE001 - a closed console must not crash
            pass

    def size(self) -> tuple[int, int]:
        try:
            c, r = os.get_terminal_size(self.stream.fileno())
        except Exception:   # noqa: BLE001
            c, r = shutil.get_terminal_size((100, 30))
        return max(20, c), max(6, r)

    # ------------------------------------------------------------- setup --
    def start(self) -> None:
        """Raw input + VT output + bracketed paste + focus reports."""
        if self.active:
            return
        if IS_WIN:
            self._win_setup()
        else:
            self._posix_setup()
        # bracketed paste, focus in/out reports
        self.write("\x1b[?2004h\x1b[?1004h")
        if not IS_WIN:
            # kitty keyboard protocol (disambiguate) + xterm modifyOtherKeys:
            # Shift+Enter / Ctrl+Enter become distinguishable; terminals
            # that don't know them ignore the request.
            self.write("\x1b[>1u\x1b[>4;1m")
        self.flush()
        self.active = True
        atexit.register(self.stop)
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._win_read if IS_WIN else self._posix_read,
            daemon=True, name="aura-keys")
        self._reader.start()

    def alt_screen(self, on: bool) -> None:
        """Fullscreen (full.py): the alternate screen + mouse reports (wheel),
        or back to the normal screen and its scrollback. On Windows the
        console also has to be told to send mouse events (and Quick Edit,
        which would swallow them, is paused meanwhile)."""
        if on == getattr(self, "_alt", False):
            return
        self._alt = on
        # 1002: drags too (selecting text); off when the user keeps the
        # terminal's own selection (/settings look.mouse)
        mouse = getattr(self, "mouse_on", True)
        self.write(("\x1b[?1049h" + ("\x1b[?1000h\x1b[?1002h\x1b[?1006h" if mouse else "")
                    + "\x1b[2J\x1b[H") if on
                   else "\x1b[?1000l\x1b[?1002l\x1b[?1006l\x1b[?1049l")
        self.flush()
        if IS_WIN and getattr(self, "_hin", None) is not None:
            import ctypes
            k32, mode = self._k32, ctypes.c_uint32()
            if k32.GetConsoleMode(self._hin, ctypes.byref(mode)):
                if on and getattr(self, "mouse_on", True):
                    self._mode_before_alt = mode.value
                    k32.SetConsoleMode(self._hin, (mode.value | 0x0010 | 0x0080) & ~0x0040)
                elif getattr(self, "_mode_before_alt", None) is not None:
                    k32.SetConsoleMode(self._hin, self._mode_before_alt)

    def stop(self) -> None:
        if not self.active:
            return
        try:
            self.alt_screen(False)
        except Exception:   # noqa: BLE001 - never leave the user's terminal stuck
            pass
        self.active = False
        self._stop.set()
        try:
            if not IS_WIN:
                self.write("\x1b[<u\x1b[>4;0m")
            self.write("\x1b[?2004l\x1b[?1004l\x1b[?25h\x1b[0m")
            self.flush()
        except Exception:   # noqa: BLE001
            pass
        for undo in reversed(self._restore):
            try:
                undo()
            except Exception:   # noqa: BLE001
                pass
        self._restore.clear()

    def suspend_input(self):
        """Context: hand the tty back (external editor, shell, input())."""
        term = self

        class _Ctx:
            def __enter__(self_inner):
                term._paused = True
                term.stop()

            def __exit__(self_inner, *a):
                term.start()
                term._paused = False
        return _Ctx()

    # ----------------------------------------------------------- windows --
    def _win_setup(self) -> None:
        import ctypes
        k32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
        self._k32 = k32
        hout = k32.GetStdHandle(-11)
        hin = k32.GetStdHandle(-10)
        self._hin = hin
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(hout, ctypes.byref(mode)):
            old_out = mode.value
            # VT processing + keep "processed output" (newline handling)
            k32.SetConsoleMode(hout, old_out | 0x0004 | 0x0001)
            self._restore.append(lambda: k32.SetConsoleMode(hout, old_out))
        if k32.GetConsoleMode(hin, ctypes.byref(mode)):
            old_in = mode.value
            new_in = (old_in | 0x0008) & ~(0x0001 | 0x0002 | 0x0004 | 0x0200)
            k32.SetConsoleMode(hin, new_in)
            self._restore.append(lambda: k32.SetConsoleMode(hin, old_in))
        old_cp = k32.GetConsoleOutputCP()
        k32.SetConsoleOutputCP(65001)
        self._restore.append(lambda: k32.SetConsoleOutputCP(old_cp))
        try:
            self.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:   # noqa: BLE001
            pass

    def _win_read(self) -> None:
        import ctypes
        from ctypes import wintypes

        class KEY(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                        ("vk", wintypes.WORD), ("scan", wintypes.WORD),
                        ("ch", wintypes.WCHAR), ("state", wintypes.DWORD)]

        class COORD(ctypes.Structure):
            _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

        class FOCUS(ctypes.Structure):
            _fields_ = [("focus", wintypes.BOOL)]

        class MOUSE(ctypes.Structure):
            _fields_ = [("pos", COORD), ("buttons", wintypes.DWORD),
                        ("ctrl", wintypes.DWORD), ("flags", wintypes.DWORD)]

        class _U(ctypes.Union):
            _fields_ = [("key", KEY), ("size", COORD), ("focus", FOCUS),
                        ("mouse", MOUSE), ("raw", ctypes.c_byte * 16)]

        class REC(ctypes.Structure):
            _fields_ = [("type", wintypes.WORD), ("ev", _U)]

        k32 = self._k32
        buf = (REC * 256)()
        n = wintypes.DWORD()
        hi_surrogate = ""
        while not self._stop.is_set():
            if k32.WaitForSingleObject(self._hin, 100) != 0:
                continue
            if not k32.ReadConsoleInputW(self._hin, buf, 256, ctypes.byref(n)):
                time.sleep(0.05)
                continue
            batch: list[Key] = []
            for i in range(n.value):
                r = buf[i]
                if r.type == 4:
                    batch.append(Key("resize"))
                    continue
                if r.type == 16:
                    self.focused = bool(r.ev.focus.focus)
                    batch.append(Key("focus_in" if self.focused else "focus_out"))
                    continue
                if r.type == 2 and r.ev.mouse.flags & 0x0004:      # The wheel (fullscreen)
                    delta = ctypes.c_short((r.ev.mouse.buttons >> 16) & 0xFFFF).value
                    batch.append(Key("wheel_up" if delta > 0 else "wheel_down"))
                    continue
                if r.type == 2 and not r.ev.mouse.flags & ~0x0001:  # Press / release / drag
                    pos = {"x": r.ev.mouse.pos.x + 1, "y": r.ev.mouse.pos.y + 1}
                    left = bool(r.ev.mouse.buttons & 0x0001)
                    if r.ev.mouse.flags & 0x0001:                  # Moved
                        if left:
                            batch.append(Key("mouse_drag", meta=pos))
                    elif left and not getattr(self, "_left_down", False):
                        self._left_down = True
                        batch.append(Key("mouse_down", meta=pos))
                    elif not left and getattr(self, "_left_down", False):
                        self._left_down = False
                        batch.append(Key("mouse_up", meta=pos))
                    continue
                if r.type != 1 or not r.ev.key.down:
                    continue
                k = r.ev.key
                ch = k.ch
                if ch and 0xD800 <= ord(ch) <= 0xDBFF:
                    hi_surrogate = ch
                    continue
                if hi_surrogate and ch and 0xDC00 <= ord(ch) <= 0xDFFF:
                    ch = (hi_surrogate + ch).encode(
                        "utf-16", "surrogatepass").decode("utf-16")
                    hi_surrogate = ""
                key = _win_key(k.vk, ch, k.state)
                if key is not None:
                    for _ in range(max(1, k.repeat)):
                        batch.append(key)
            for key in _merge_paste_burst(batch):
                self.keys.put(key)

    # ------------------------------------------------------------- posix --
    def _posix_setup(self) -> None:
        import termios
        import tty
        fd = sys.stdin.fileno()
        self._fd = fd
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        # keep output post-processing (\n → \r\n) so printing stays normal
        attrs = termios.tcgetattr(fd)
        attrs[1] |= termios.OPOST | getattr(termios, "ONLCR", 0)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        self._restore.append(
            lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old))
        try:
            import signal
            if threading.current_thread() is threading.main_thread():
                prev = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH,
                              lambda *_: self.keys.put(Key("resize")))
                self._restore.append(
                    lambda: signal.signal(signal.SIGWINCH, prev))
        except Exception:   # noqa: BLE001
            pass

    def _posix_read(self) -> None:
        import select
        parser = VTParser()
        fd = self._fd
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not r:
                for k in parser.flush():
                    self._post(k)
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            for k in parser.feed(data.decode("utf-8", "replace")):
                self._post(k)

    def _post(self, k: Key) -> None:
        if k.id == "focus_in":
            self.focused = True
        elif k.id == "focus_out":
            self.focused = False
        elif k.id == "osc" and k.text.startswith("11;"):
            self.background_rgb = parse_osc_rgb(k.text[3:])
            return
        self.keys.put(k)

    def query_background(self, timeout: float = 0.15) -> None:
        """Ask the terminal for its background colour (OSC 11). POSIX only;
        the answer lands in background_rgb if the terminal replies."""
        if IS_WIN or not self.active:
            return
        self.write("\x1b]11;?\x1b\\")
        self.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.background_rgb is None:
            time.sleep(0.01)


# ---------------------------------------------------------------- helpers --
_VK = {0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "escape",
       0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
       0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
       0x2D: "insert", 0x2E: "delete",
       **{0x70 + i: f"f{i + 1}" for i in range(12)}}


def _win_key(vk: int, ch: str, state: int) -> Key | None:
    """One Windows key-down record → Key."""
    ralt, lalt = bool(state & 0x1), bool(state & 0x2)
    ctrl = bool(state & 0x4 or state & 0x8)
    shift = bool(state & 0x10)
    altgr = ralt and ctrl            # AltGr = right-alt + ctrl → a character
    alt = (lalt or ralt) and not altgr
    if vk in (0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x5D):   # Bare modifiers
        return None
    if vk in _VK:
        name = _VK[vk]
        if name == "tab" and shift and not ctrl:
            return Key("shift+tab")
        prefix = ("ctrl+" if ctrl and not altgr else "") + \
                 ("alt+" if alt else "") + ("shift+" if shift and name not in
                                            ("backspace",) else "")
        if name == "backspace" and ctrl:
            return Key("ctrl+backspace")
        return Key(prefix + name)
    if not ch or ch == "\x00":
        if ctrl and vk == 0x20:
            return Key("ctrl+space")
        return None
    o = ord(ch[0])
    if ctrl and not altgr and len(ch) == 1 and o < 32:
        from .keys import control_key
        k = control_key(ch)
        if k is not None:
            return Key(("alt+" if alt else "") + k.id)
    if ctrl and not altgr and vk == 0xBD:          # Ctrl+- / ctrl+_
        return Key("ctrl+_")
    if alt:
        return Key("alt+" + ch.lower(), ch)
    return Key("char", ch)


def _merge_paste_burst(keys: list[Key]) -> list[Key]:
    """Windows delivers a paste as a burst of key events in one read. Three
    or more plain characters/Enters arriving together are a paste: Enter
    inside it is a newline, not 'send'."""
    plain = [k for k in keys if k.id in ("char", "enter", "tab", "shift+enter")]
    if len(keys) >= 3 and len(plain) == len(keys) and \
            any(k.id == "char" for k in keys):
        # one typed line and its Enter read together (fast typing, a busy
        # screen) is typing: the text goes in, the Enter sends it
        enters = [i for i, k in enumerate(keys) if k.id == "enter"]
        if enters == [len(keys) - 1]:
            text = "".join(k.text if k.id == "char" else "\t" for k in keys[:-1])
            return [Key("paste", text), Key("enter")]
        text = "".join(k.text if k.id == "char" else
                       ("\t" if k.id == "tab" else "\n") for k in keys)
        return [Key("paste", text)]
    return keys


def parse_osc_rgb(body: str) -> tuple[int, int, int] | None:
    """'rgb:1e1e/2020/3030' → (30, 32, 48)."""
    if not body.startswith("rgb:"):
        return None
    try:
        parts = body[4:].split("/")
        vals = [int(p[:2], 16) for p in parts[:3]]
        return vals[0], vals[1], vals[2]
    except (ValueError, IndexError):
        return None

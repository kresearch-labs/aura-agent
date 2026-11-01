"""text.py — measure, cut and wrap text in terminal cells.

A wide character (CJK, most emoji) takes two cells; combining marks and
zero-width joiners take none. `wcwidth` is used when installed (it is a
declared dependency); otherwise a small built-in table gives the same
answer for the common cases, so the UI never misaligns.
"""
from __future__ import annotations

import re
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-9;:?<=>]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

try:                                     # pragma: no cover - optional
    from wcwidth import wcwidth as _wcw  # type: ignore
except Exception:                        # noqa: BLE001
    _wcw = None

# emoji / pictographs that render two cells wide in modern terminals
_WIDE_RANGES = (
    (0x1100, 0x115F), (0x231A, 0x231B), (0x2329, 0x232A), (0x23E9, 0x23EC),
    (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE), (0x2614, 0x2615),
    (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1),
    (0x26AA, 0x26AB), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26CE, 0x26CE),
    (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5),
    (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705), (0x270A, 0x270B),
    (0x2728, 0x2728), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x2E80, 0x303E),
    (0x3041, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6), (0x1F300, 0x1F64F), (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF), (0x20000, 0x3FFFD),
)


def char_width(ch: str) -> int:
    """Cells one character takes: 0, 1 or 2."""
    if _wcw is not None:
        w = _wcw(ch)
        return 1 if w < 0 else w
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if o in (0x200B, 0x200C, 0x200D, 0xFE0E, 0xFE0F) or \
            unicodedata.combining(ch):
        return 0
    for lo, hi in _WIDE_RANGES:
        if lo <= o <= hi:
            return 2
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def width(s: str) -> int:
    """Visible width in cells (escape sequences count zero)."""
    return sum(char_width(c) for c in strip_ansi(s))


def _tokens(s: str):
    """Yield (is_escape, text) pieces of an ANSI-styled string."""
    pos = 0
    for m in ANSI_RE.finditer(s):
        if m.start() > pos:
            yield False, s[pos:m.start()]
        yield True, m.group(0)
        pos = m.end()
    if pos < len(s):
        yield False, s[pos:]


def cut(s: str, cells: int, ellipsis: str = "…") -> str:
    """Cut a styled string to at most `cells`, keeping escapes; adds an
    ellipsis when something was cut."""
    if width(s) <= cells:
        return s
    if cells <= 0:
        return ""
    room = cells - width(ellipsis)
    out, used = [], 0
    for esc, piece in _tokens(s):
        if esc:
            out.append(piece)
            continue
        for ch in piece:
            w = char_width(ch)
            if used + w > room:
                out.append(ellipsis)
                out.append("\x1b[0m" if "\x1b[" in s else "")
                return "".join(out)
            out.append(ch)
            used += w
    return "".join(out)


def pad(s: str, cells: int) -> str:
    """Pad with spaces to `cells` (no cut)."""
    return s + " " * max(0, cells - width(s))


def _active_sgr(s: str) -> str:
    """The SGR state at the end of a styled line (to carry onto the next
    wrapped line)."""
    state = ""
    for m in re.finditer(r"\x1b\[([0-9;:]*)m", s):
        params = m.group(1)
        if params in ("", "0"):
            state = ""
        else:
            state += m.group(0)
    return state


def wrap(s: str, cells: int, indent: str = "", first_indent: str | None = None) -> list[str]:
    """Word-wrap one styled line to `cells`. Long words are broken. Styles
    carry over to the next line. `first_indent`/`indent` prefix lines."""
    first = indent if first_indent is None else first_indent
    if cells <= 0:
        return [first + s]
    out: list[str] = []
    cur, cur_w = first, width(first)
    limit = cells
    # split into words keeping spaces as separators
    words = re.split(r"( +)", s)
    for word in words:
        if word == "":
            continue
        ww = width(word)
        if word.startswith(" "):
            if cur_w + ww <= limit:
                cur += word
                cur_w += ww
            else:
                carry = _active_sgr(cur)
                out.append(cur + ("\x1b[0m" if carry else ""))
                cur, cur_w = indent + carry, width(indent)
            continue
        if cur_w + ww <= limit:
            cur += word
            cur_w += ww
            continue
        if cur_w > width(indent if out else first) and ww <= limit - width(indent):
            carry = _active_sgr(cur)
            out.append(cur.rstrip() + ("\x1b[0m" if carry else ""))
            cur, cur_w = indent + carry + word, width(indent) + ww
            continue
        # break a long word across lines
        for esc, piece in _tokens(word):
            if esc:
                cur += piece
                continue
            for ch in piece:
                w = char_width(ch)
                if cur_w + w > limit:
                    carry = _active_sgr(cur)
                    out.append(cur + ("\x1b[0m" if carry else ""))
                    cur, cur_w = indent + carry, width(indent)
                cur += ch
                cur_w += w
    out.append(cur)
    return out


def wrap_text(text: str, cells: int, indent: str = "", first_indent: str | None = None) -> list[str]:
    """wrap() for multi-line text; blank lines kept."""
    lines: list[str] = []
    first_done = False
    for ln in text.split("\n"):
        fi = None if first_done else first_indent
        lines.extend(wrap(ln, cells, indent=indent, first_indent=fi)
                     if ln else [(first_indent if not first_done and first_indent is not None else indent).rstrip()])
        first_done = True
    return lines

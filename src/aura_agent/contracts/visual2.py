"""visual.py

  surface ramp: bg → panel → element, 7-token derivation
  marker grammar: `_`→recessed-on-shadow, `^`→▀ fg-on-shadow,
      `~`→shadow ▀ — faux-3D from fg/bg, shadow = mix(BG, PRIMARY, 0.25)
  rail cards (panel law): rail_top/rail_row/rail_bot, title-in-card
  gutter (gutter law): `▐` agent-colored message rails

NO_COLOR → plain strings.
"""
from __future__ import annotations

from .. import ui

# ----------------------------------------------------------------- color math
def hx(h):
    """'#rrggbb' | (r,g,b) -> (r,g,b)."""
    if isinstance(h, tuple):
        return tuple(int(x) for x in h)
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hxstr(t) -> str:
    return "#%02x%02x%02x" % tuple(t)


def mix(c1, c2, t: float):
    """Linear channel lerp."""
    a, b = hx(c1), hx(c2)
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# -------------------------------------------------- identity.toml token bridge
# The 7-token surface ramp, resolved from the manifest palette. V-COLOR: the
# 7-token surface ramp IS the manifest palette — derived from identity.toml,
# never restated (drift impossible by law).
from .visual import TOKENS as _TOKENS
SURFACE_RAMP: dict[str, str] = {k: _TOKENS[k] for k in (
    "bg", "panel", "element", "menu",
    "border_subtle", "border", "border_active")}


def _bg_hex() -> str:
    return SURFACE_RAMP["bg"]


def _primary_hex() -> str:
    return SURFACE_RAMP["border_active"]


def _panel_hex() -> str:
    return SURFACE_RAMP["panel"]


def _element_hex() -> str:
    return SURFACE_RAMP["element"]


def _no_color() -> bool:
    return not ui._ENABLED


R = "\x1b[0m"


# Xterm-256 → hex for the ui.C palette ids Aura actually uses (Aura Blue
# identity). Closest truecolor equivalents.
from .visual import C256_HEX, C256_FALLBACK  # Xterm->hex table (V-COLOR)


def _as_hex(col) -> str:
    """Accept hex str OR a 256-palette int (ui.C values) — primitives must never
    crash on the caller's color idiom.
    """
    if isinstance(col, int):
        if col in C256_HEX:
            return C256_HEX[col]
        # xterm cube/grayscale approximation
        if 232 <= col <= 255:
            v = 8 + (col - 232) * 10
            return f"#{v:02x}{v:02x}{v:02x}"
        if 16 <= col <= 231:
            i = col - 16
            steps = (0, 95, 135, 175, 215, 255)
            r, g, b = steps[i // 36], steps[(i // 6) % 6], steps[i % 6]
            return f"#{r:02x}{g:02x}{b:02x}"
        return C256_FALLBACK  # Base-16 fallback: Aura blue
    return col


def fg(s: str, hexcol, bold: bool = False, dim: bool = False) -> str:
    if _no_color():
        return s
    c = hx(_as_hex(hexcol))
    pre = f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    if bold:
        pre = "\x1b[1m" + pre
    if dim:
        pre = "\x1b[2m" + pre
    return pre + s + R


def bgspan(s: str, hexbg, hexfg=None, bold: bool = False) -> str:
    if _no_color():
        return s
    bgc = hx(_as_hex(hexbg))
    pre = f"\x1b[48;2;{bgc[0]};{bgc[1]};{bgc[2]}m"
    if hexfg:
        c = hx(_as_hex(hexfg))
        pre += f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    if bold:
        pre = "\x1b[1m" + pre
    return pre + s + R


def screen_bg_line(text: str, bg, width: int = 80, fgcol=None) -> str:
    pad = max(width - ui.plainlen(text), 0)
    if _no_color():
        return text + " " * pad
    pre = f"\x1b[48;2;{hx(bg)[0]};{hx(bg)[1]};{hx(bg)[2]}m"
    if fgcol:
        c = hx(fgcol)
        pre += f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"
    return pre + text + " " * pad + "\x1b[0m"


# ------------------------------------------------------------- rail cards
def rail_row(rail_color, text: str, bg=None, indent: int = 1,
             width: int = 80) -> str:
    body = " " * indent + text
    pad = max(width - 1 - ui.plainlen(body), 0)
    bg = bg or _element_hex()
    if _no_color():
        return "┃" + body + " " * pad
    return fg("┃", rail_color) + bgspan(body + " " * pad, bg)


def rail_top(width: int = 80, label: str | None = None, rail=None,
             bg=None) -> str:
    rail = rail or _primary_hex()
    bg = bg or _panel_hex()
    lbl = f" {label} " if label else ""
    dashes = max(width - 2 - ui.plainlen(lbl) - 1, 0)
    if _no_color():
        return "╭─" + lbl + "─" * dashes + "╮"
    return (fg("╭─", rail)
            + bgspan(lbl + "─" * dashes, bg, rail, bold=bool(label))
            + fg("╮", rail))


def rail_bot(width: int = 80, rail=None, bg=None) -> str:
    rail = rail or _primary_hex()
    if _no_color():
        return "╰" + "─" * (width - 2) + "╯"
    return fg("╰" + "─" * (width - 2) + "╯", rail)


# ------------------------------------------------------------ wordmark
# The authored AURA letterforms — 4-marker grammar, left/right fragments.
LOGO_L = ["█▀▀▄ █▀▀█ █▀▀█ █▀▀▄", "█__▀ █__█ █▄▄█ █__▀",
          "▀~~▀ ▀▀▀▀ ▀  ▀ ▀~~▀"]
LOGO_R = ["█▀▀█ █▀▀▄ █▀▀▄ █▀▀█", "█__█ █__█ █__█ █__█",
          "▀  ▀ ▀▀▀▀ ▀▀▀▀ ▀  ▀"]

# ANSI-shadow wordmark: the classic block- letterform banner — 6 rows, ╔╗╚╝ + ║
# ═ strokes. Gradient = per-ROW brand-ramp colorize, never a pixel gradient.
SHADOW_BANNER = [
    "█████╗ ██╗   ██╗██████╗  █████╗",
    "██╔══██╗██║   ██║██╔══██╗██╔══██╗",
    "███████║██║   ██║██████╔╝███████║",
    "██╔══██║██║   ██║██╔══██╗██╔══██║",
    "██║  ██║╚██████╔╝██║  ██║██║  ██║",
    "╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝",
]
# Per-row gradient extended to 6 rows: -class over the ramp [primary, accent,
# border, muted] — top rows brightest


def banner_logo(width: int = 80, style: str = "aura") -> list[str]:
    """The ANSI-shadow block-letterform banner (user's chosen art), colorized
    per-ROW from the brand ramp.

    Style='aura' — house colors: primary→accent top-to-bottom, shadow-tinted
    final rows (graceful base) style='gradient' — 6-step lightness sweep across
    rows
    """
    bg = _bg_hex()
    primary = _primary_hex()
    # 6-row ramp: extended — rows get deeper toward the shadow
    ramp = [primary,
            hxstr(mix(primary, _bg_hex(), 0.15)),
            hxstr(mix(primary, _bg_hex(), 0.30)),
            hxstr(mix(primary, _bg_hex(), 0.45)),
            hxstr(mix(primary, _bg_hex(), 0.60)),
            hxstr(mix(primary, _bg_hex(), 0.72))]
    rows = []
    for i, art in enumerate(SHADOW_BANNER):
        if width < len(art) + 4:
            continue  # Responsive: drop rows that don't fit, keep widest
        rows.append("  " + fg(art, ramp[i]))
    return rows


def shadow_logo(width: int = 80, style: str = "shadow") -> list[str]:
    """The AURA wordmark. style='shadow' = 3-row marker-grammar compact;
    style='banner' = the 6-row ANSI-shadow block letterforms (user choice)."""
    if style == "banner":
        return banner_logo(width)
    bg = _bg_hex()
    primary = _primary_hex()
    txt = _TOKENS["text"]
    mut = _TOKENS["text_muted"]
    shadow = hxstr(mix(bg, primary, 0.25))
    rows = []
    for i in range(3):
        left, right = LOGO_L[i], LOGO_R[i]
        cells = []
        for half, is_bold in ((left, False), (right, True)):
            for ch in half:
                if ch == "_":
                    cells.append(bgspan(" ", shadow, bold=is_bold))
                elif ch == "^":
                    cells.append(bgspan("▀", shadow, primary, bold=is_bold))
                elif ch == "~":
                    cells.append(fg("▀", shadow))
                else:
                    cells.append(fg(ch, txt if is_bold else mut, bold=is_bold))
        rows.append("   " + "".join(cells))
    return rows

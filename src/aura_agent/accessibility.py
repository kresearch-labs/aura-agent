"""accessibility.py — AURA_ACCESS modes.

Three modes via env (or `aura config` write-through):

  AURA_ACCESS=screen-reader
      glyphs (◇◆◈ ✓✗• → ⬢ █░) replaced with ASCII words
      ([read]/[write]/[exec]/OK/MISS), banners collapsed to single lines,
      progress bars dropped, tables kept (screen readers handle them)
  AURA_ACCESS=flat
      same glyph replacement, keeps layout bars
  unset
      normal rich output

Public: mode, flat, glyph(name) — ui.py consults these.
"""
from __future__ import annotations

import os

_MODES = ("", "flat", "screen-reader")

_GLYPH_MAP = {
    # rich -> screen-reader/flat equivalent
    "◇": "[read]", "◆": "[write]", "◈": "[exec]",
    "✔": "OK", "✓": "OK", "✗": "MISS", "•": "-",
    "→": "->", "⬢": "Aura", "☤": "Aura",
    "█": "#", "░": ".",
    "⊘": "[refused]", "⚠": "[warn]",
}


def mode() -> str:
    raw = (os.environ.get("AURA_ACCESS") or "").strip().lower()
    if not raw:
        try:                       # Settings look.screen_reader
            from .options import get
            raw = str(get("look.screen_reader") or "").lower()
        except Exception:          # noqa: BLE001
            raw = ""
    return raw if raw in _MODES else ""


def flat() -> bool:
    """True when glyphs must be replaced (flat or screen-reader)."""
    return mode() in ("flat", "screen-reader")


def screen_reader() -> bool:
    return mode() == "screen-reader"


def strip_glyphs(text: str) -> str:
    """Replace every rich glyph with its accessible equivalent."""
    if not flat():
        return text
    for rich, plain in _GLYPH_ITEMS:
        text = text.replace(rich, plain)
    return text


_GLYPH_ITEMS = sorted(_GLYPH_MAP.items(), key=lambda kv: -len(kv[0]))


def paint(s: str, *codes) -> str:
    """ui.paint wrapper honoring accessibility mode (color kept in flat —
    screen readers ignore ANSI; glyphs are what matters)."""
    return strip_glyphs(s)

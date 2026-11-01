"""themes.py — UI themes with ANSI variants.

claude-code pattern: /theme <name> at runtime; AURA_THEME env at launch.
  dark (default) — the original palette
  colorblind — blue/orange distinguishable pairings (no red/green reliance)
  light — darker inks for light-background terminals
  mono — single-hue (structure by brightness only)

Public: available, apply(name) -> bool, current. ui.py consults themes.palette
instead of its module-level dict.
"""
from __future__ import annotations

import os

_PALETTES: dict[str, dict] = {
    # Same for mono/colorblind dims. V: Aura Blue for dark themes (mono keeps
    # brightness ladder)
    "dark": {
        "banner_border": 110, "banner_title": 39, "banner_accent": 117,
        "banner_dim": 110, "label": 111, "value": 255,
        "ok": 120, "warn": 215, "err": 210,
        "read": 111, "write": 183, "execute": 215,
        "refuse": 210, "user_prompt": 153, "agent_marker": 87,
        "agent_text": 255, "rule": 110,
    },
    # colorblind: ok=blue(75)/warn=orange(214)/err=deep-orange(202);
    # blast radii distinguish by lightness, not hue pairs
    "colorblind": {
        "banner_border": 249, "banner_title": 33, "banner_accent": 141,
        "banner_dim": 250, "label": 250, "value": 255,
        "ok": 81, "warn": 215, "err": 209,
        "read": 117, "write": 183, "execute": 209,
        "refuse": 209, "user_prompt": 253, "agent_marker": 80,
        "agent_text": 255, "rule": 249,
    },
    # light: dim inks so text stays readable on white
    "light": {
        "banner_border": 240, "banner_title": 24, "banner_accent": 127,
        "banner_dim": 240, "label": 238, "value": 235,
        "ok": 28, "warn": 130, "err": 124,
        "read": 30, "write": 90, "execute": 166,
        "refuse": 124, "user_prompt": 236, "agent_marker": 25,
        "agent_text": 234, "rule": 240,
    },
    # mono: brightness ladder only (floor raised for conhost legibility)
    "mono": {
        "banner_border": 249, "banner_title": 255, "banner_accent": 250,
        "banner_dim": 250, "label": 250, "value": 255,
        "ok": 250, "warn": 249, "err": 247,
        "read": 250, "write": 248, "execute": 247,
        "refuse": 245, "user_prompt": 252, "agent_marker": 252,
        "agent_text": 255, "rule": 249,
    },
}

_current = "dark"
_applied = False


def available() -> list[str]:
    return sorted(_PALETTES)


def current() -> str:
    import os
    env = os.environ.get("AURA_THEME")
    if env in _PALETTES:
        return env
    if not _applied:
        # the theme setup saved (look.theme), until /theme changes it
        try:
            from .options import get
            saved = get("look.theme")
            if saved in _PALETTES:
                return saved
        except Exception:                            # noqa: BLE001
            pass
    return _current


def palette(name: str | None = None) -> dict:
    name = name or current()
    return _PALETTES.get(name, _PALETTES["dark"])


def apply(name: str) -> bool:
    global _current, _applied
    if name not in _PALETTES:
        return False
    _current, _applied = name, True
    # ui.C is consumed at paint-time via ui.e -> rebind in place
    from . import ui
    ui.C.clear()
    ui.C.update(_PALETTES[name])
    return True

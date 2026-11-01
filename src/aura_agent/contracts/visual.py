"""contracts/visual.py — V-law primitives. Constants validate against
identity.toml (IC-): this module MUST NOT hardcode what the manifest owns — it
LOADS the manifest, so drift between constants and manifest is impossible by
construction (suite fails if the manifest is malformed).

  ellip — single-… truncation, never flush-clip
  STATE_WORDS / check_state_word — the closed registry (V-STATE)
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from wcwidth import wcswidth

_SGR = re.compile(r"\x1b\[[0-9;]*m")

_MANIFEST_PATH = Path(__file__).parents[1] / "identity.toml"

with _MANIFEST_PATH.open("rb") as _f:
    MANIFEST = tomllib.load(_f)

# Constants validated FROM the manifest (IC-) --
MOTION = MANIFEST["motion"]
TICK_MS: int = MOTION["tick_ms"]
DORMANCY_S: int = MOTION["dormancy_s"]
SHIMMER_MS: int = MOTION["shimmer_ms"]
FULL_REPAINT_EVENTS: tuple[str, ...] = tuple(MOTION["full_repaint_events"])

CHARM = MANIFEST["charm"]
CHARM_C2_MAX: int = CHARM["c2_max_per_run"]
CHARM_C3_MAX: int = CHARM["c3_max_per_run"]
CHARM_COOLDOWN_MIN: int = CHARM["cooldown_min"]
SACRED_SURFACES: tuple[str, ...] = tuple(CHARM["sacred_surfaces"])

STATE_WORDS: tuple[str, ...] = tuple(MANIFEST["words"]["states"])
FORBIDDEN_WORDS: dict[str, str] = dict(MANIFEST["words"]["forbidden"])

BANNER = MANIFEST["banner"]
GRADIENT_ROW_INDEX: tuple[int, ...] = tuple(BANNER["gradient_row_index"])
SHADOW_TINT: float = BANNER["shadow_tint"]

TOKENS: dict[str, str] = dict(MANIFEST["theme"]["tokens"])

# Xterm-256 -> hex for the ui.C palette ids Aura actually uses (Aura Blue
# identity). Closest truecolor equivalents. Lives HERE (the manifest module) so
# contract renderers stay literal-free per V-COLOR.
C256_HEX: dict[int, str] = {
    39: "#00d7ff", 87: "#5fffff", 110: "#87afff", 111: "#87afff",
    117: "#87d7ff", 120: "#87ffaf", 153: "#afd7ff", 183: "#d7afff",
    210: "#ff8787", 215: "#ffaf5f", 247: "#9e9e9e", 249: "#b2b2b2",
    250: "#bcbcbc", 255: "#eeeeee",
}
C256_FALLBACK = C256_HEX[110]   # Base-16 fallback: Aura blue


def check_state_word(word: str) -> str:
    """Raise ValueError on forbidden synonyms; pass registry words through.
    The V-STATE law: only the five registry words render as states."""
    if word in STATE_WORDS:
        return word
    if word in MANIFEST["words"]["forbidden"]:
        raise ValueError(
            f"forbidden state word {word!r} — use "
            f"{MANIFEST['words']['forbidden'][word]!r}")
    raise ValueError(
        f"unknown state word {word!r} — registry: {STATE_WORDS}")


def vlen(s: str) -> int:
    """Visible width: ANSI-stripped, wcwidth-correct (wide glyphs = 2)."""
    plain = _SGR.sub("", s)
    w = wcswidth(plain)
    return w if w >= 0 else len(plain)


def ellip(s: str, width: int) -> str:
    """Single-… truncation; never flush-clip; short strings untouched."""
    if vlen(s) <= width:
        return s
    # cut by visible width, preserving ANSI runs
    out: list[str] = []
    used = 0
    i = 0
    while i < len(s) and used < width - 1:
        m = _SGR.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(s[i])
        used += wcswidth(s[i]) or 1
        i += 1
    return "".join(out) + "…"



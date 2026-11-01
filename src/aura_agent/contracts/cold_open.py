"""cold_open.py

Laws implemented (96-ui-law-extract.md):
  35 Wordmark: 4-row AURA art in the 4-marker grammar, per-ROW gradient
      , two-tone halves, responsive (full/compact/text/hidden).
  Truthful banner: every fact carries a tier (PROBED/PREDICTED/
      SNAPSHOT); endpoint shows probe verdict; login≠serving.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import ui
from . import visual2

# Wordmark: the AUTHORED art from visual2 — rendered with per-theme shadow
# mapping. Law 35 responsive ladder lives here; the art lives in visual2.


def wordmark(width: int = 76, compact: bool = False) -> list[str]:
    """Responsive wordmark: the ANSI-shadow banner → compact → text → hidden.
    HIDE_BELOW=34, COMPACT_FROM=58.
    """
    if width < 34:
        return []
    if compact or width < 58:
        return ["  " + ui.title("Aura")]
    return visual2.shadow_logo(width, style="banner")


_TIER_WORDS = {"PROBED": "checked", "PREDICTED": "estimated",
               "SNAPSHOT": "saved"}


@dataclass
class BannerFact:
    label: str
    value: str
    tier: str = "PROBED"            # PROBED | PREDICTED | SNAPSHOT
    note: str = ""
    ok: bool | None = None

    def __post_init__(self):
        if self.tier not in ("PROBED", "PREDICTED", "SNAPSHOT"):
            raise ValueError(f"fact without tier: {self.label}")


@dataclass
class ColdOpen:
    version: str
    profile: str
    model: str
    endpoint: str
    tools: int
    facts: list[BannerFact] = field(default_factory=list)
    try_hint: str = "what does this project do?"

    def render(self, width: int = 76) -> str:
        W = min(width, ui._W or 76)
        compact = W < 58
        rule = ui.paint("─" * (W - 4), ui.C.get("rule", 110))
        out = wordmark(W, compact=compact)
        # Painted screen feel — centered dim tagline, then the facts block
        # inside a painted PANEL with gutter ┃ marks.
        from .visual2 import (bgspan, fg, _element_hex,
                              _primary_hex, _bg_hex, screen_bg_line,
                              _no_color)
        out.append("  " + ui.muted(
            f"local-first AI agent · v{self.version}"))
        out.append("  " + rule)                      # ── zone: identity
        for f in self.facts:
            mark = (ui.ok("✓ ") if f.ok is True
                    else ui.err("✕ ") if f.ok is False else "")
            note = ui.muted(f"  · {f.note}") if f.note else ""
            # the tier stays visible — in plain words, not spec vocabulary
            tier = ui.muted(f" [{_TIER_WORDS[f.tier]}]")
            # Gutter ┃ fact rows
            out.append(f"  {ui.paint('┃', ui.C.get('rule', 110))} "
                       f"{ui.label(f.label.ljust(8))} "
                       f"{ui.value(f.value)} {mark}{note}{tier}".rstrip())
        out.append("  " + rule)                      # ── zone: status
        out.append(f"  {ui.ok('✓ ready')} {ui.muted('— try:')} "
                   f"{ui.paint(self.try_hint, ui.C.get('banner_accent', 111))}")
        out.append("")
        return "\n".join(out)


def render_cold_open(width: int = 76, **kw) -> str:
    return ColdOpen(**kw).render(width)



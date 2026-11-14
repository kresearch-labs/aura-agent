"""style.py — colour roles, themes and colour depth.

Everything on screen is painted by ROLE ("accent", "tool_write",
"diff_add"…), never by a raw colour, so a theme swap repaints the whole UI.
Each theme defines roles as hex colours; they are sent as 24-bit colour
when the terminal supports it, else as the nearest of the 256 or 16
standard colours, or not at all (NO_COLOR, piped output, screen-reader
mode).
"""
from __future__ import annotations

import os

RESET = "\x1b[0m"

# ------------------------------------------------------------------ themes --
_DARK = {
    "text": "#d8dee9", "strong": "#eceff4", "dim": "#8f9bb3", "faint": "#5c677d",
    "accent": "#8ab4ff", "accent2": "#c3a6ff", "brand_a": "#7dd3fc", "brand_b": "#c084fc",
    "user": "#9ece6a", "assistant": "#8ab4ff",
    "tool_read": "#7dcfff", "tool_write": "#bb9af7", "tool_exec": "#ff9e64",
    "ok": "#9ece6a", "warn": "#e0af68", "err": "#f7768e", "info": "#7aa2f7",
    "border": "#3b4261", "border_focus": "#5a6d9a", "panel_bg": "#1f2335",
    "sel_bg": "#2e3c64", "sel_fg": "#eceff4",
    "inline_code": "#ffcb8b", "code_bg": "#1e2030", "link": "#7dcfff",
    "heading": "#c3a6ff", "quote": "#9aa5ce", "rule": "#3b4261",
    "diff_add": "#9ece6a", "diff_del": "#f7768e",
    "diff_add_bg": "#1f3326", "diff_del_bg": "#3b2028", "diff_hunk": "#7aa2f7",
    "diff_add_hl": "#2e5c3b", "diff_del_hl": "#6e2a37",
    "line_no": "#5c677d",
    "mode_l1": "#f7768e", "mode_l2": "#e0af68", "mode_l3": "#9ece6a", "mode_plan": "#7aa2f7",
    "syn_kw": "#bb9af7", "syn_str": "#9ece6a", "syn_num": "#ff9e64",
    "syn_com": "#5c677d", "syn_fn": "#7aa2f7", "syn_type": "#2ac3de",
    "syn_builtin": "#e0af68", "syn_op": "#89ddff", "syn_punct": "#a9b1d6",
}
_LIGHT = {
    "text": "#343b58", "strong": "#1a1b26", "dim": "#6172b0", "faint": "#9699a3",
    "accent": "#2e5bd8", "accent2": "#7847bd", "brand_a": "#0f80c0", "brand_b": "#8a3fd1",
    "user": "#33635c", "assistant": "#2e5bd8",
    "tool_read": "#166775", "tool_write": "#7847bd", "tool_exec": "#b15c00",
    "ok": "#33635c", "warn": "#8f5e15", "err": "#c0384b", "info": "#2e5bd8",
    "border": "#c4c8da", "border_focus": "#8c96c0", "panel_bg": "#e9e9ed",
    "sel_bg": "#c8d3f5", "sel_fg": "#1a1b26",
    "inline_code": "#965027", "code_bg": "#efeff3", "link": "#166775",
    "heading": "#7847bd", "quote": "#6172b0", "rule": "#c4c8da",
    "diff_add": "#33635c", "diff_del": "#c0384b",
    "diff_add_bg": "#dcefe0", "diff_del_bg": "#f7dfe2", "diff_hunk": "#2e5bd8",
    "diff_add_hl": "#a8dcb4", "diff_del_hl": "#f0b3bc",
    "line_no": "#9699a3",
    "mode_l1": "#c0384b", "mode_l2": "#8f5e15", "mode_l3": "#33635c", "mode_plan": "#2e5bd8",
    "syn_kw": "#7847bd", "syn_str": "#485e30", "syn_num": "#965027",
    "syn_com": "#9699a3", "syn_fn": "#2e5bd8", "syn_type": "#166775",
    "syn_builtin": "#8f5e15", "syn_op": "#006c86", "syn_punct": "#343b58",
}
# colour-blind safe: blue / orange / yellow instead of green / red pairs
_COLORBLIND = dict(_DARK, **{
    "user": "#7dcfff", "ok": "#56b4e9", "err": "#e69f00", "warn": "#f0e442",
    "diff_add": "#56b4e9", "diff_del": "#e69f00",
    "diff_add_bg": "#13293d", "diff_del_bg": "#3d2a10",
    "diff_add_hl": "#1f4a6e", "diff_del_hl": "#6b4a12",
    "mode_l1": "#e69f00", "mode_l3": "#56b4e9", "syn_str": "#56b4e9",
})
_HIGH_CONTRAST = dict(_DARK, **{
    "text": "#ffffff", "strong": "#ffffff", "dim": "#c8c8c8", "faint": "#a0a0a0",
    "border": "#a0a0a0", "border_focus": "#ffffff", "line_no": "#a0a0a0",
    "syn_com": "#a0a0a0", "rule": "#a0a0a0",
})
THEMES: dict[str, dict] = {
    "dark": _DARK, "light": _LIGHT, "colorblind": _COLORBLIND,
    "high-contrast": _HIGH_CONTRAST,
}
# older names used by /theme and AURA_THEME keep working
ALIASES = {"mono": "high-contrast", "default": "dark"}


def load_custom_themes(folder) -> list[str]:
    """Your own themes: <aura home>/themes/<name>.json, either
    {"base": "dark", "roles": {"accent": "#ff8800", ...}} or just the roles.
    Missing roles come from the base theme. Returns the names added."""
    import json
    from pathlib import Path
    added = []
    d = Path(folder)
    if not d.is_dir():
        return added
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        base = THEMES.get(ALIASES.get(str(data.get("base", "dark")), str(data.get("base", "dark"))), _DARK)
        roles = data.get("roles", data)
        good = {k: v for k, v in roles.items()
                if isinstance(v, str) and v.startswith("#") and len(v) == 7}
        THEMES[f.stem] = dict(base, **good)
        added.append(f.stem)
    return added


# ---------------------------------------------------------- colour depth --
def detect_depth(env=None, isatty: bool = True) -> int:
    """0 = no colour, 16, 256 or 24 (truecolor)."""
    env = os.environ if env is None else env
    if env.get("NO_COLOR") or not isatty and not env.get("FORCE_COLOR"):
        return 0
    if env.get("TERM") == "dumb":
        return 0
    forced = env.get("AURA_COLOR", "").lower()
    if forced in ("0", "none", "off"):
        return 0
    if forced in ("16", "256", "24", "truecolor"):
        return 24 if forced == "truecolor" else int(forced)
    ct = env.get("COLORTERM", "").lower()
    if ct in ("truecolor", "24bit"):
        return 24
    if env.get("WT_SESSION") or env.get("TERM_PROGRAM", "") in (
            "vscode", "iTerm.app", "WezTerm", "ghostty", "Hyper", "Tabby"):
        return 24
    if os.name == "nt":
        return 24        # Windows 10+ consoles take 24-bit colour in VT mode
    term = env.get("TERM", "")
    if "256" in term or "kitty" in term or "alacritty" in term:
        return 256
    return 256 if term else 16


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _to_256(r: int, g: int, b: int) -> int:
    # grey ramp when the colour is (nearly) grey
    if max(r, g, b) - min(r, g, b) < 12:
        if r < 8:
            return 16
        if r > 238:
            return 231
        return 232 + round((r - 8) / 247 * 24)
    q = lambda v: 0 if v < 48 else 1 if v < 115 else (v - 35) // 40  # noqa: E731
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


_BASIC = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16),
          (36, 114, 200), (188, 63, 188), (17, 168, 205), (229, 229, 229),
          (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
          (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]


def _to_16(r: int, g: int, b: int) -> int:
    best = min(range(16), key=lambda i: sum(
        (a - c) ** 2 for a, c in zip(_BASIC[i], (r, g, b))))
    return best


def sgr_color(hexc: str, depth: int, background: bool = False) -> str:
    if depth == 0 or not hexc:
        return ""
    r, g, b = _hex(hexc)
    if depth == 24:
        return f"\x1b[{48 if background else 38};2;{r};{g};{b}m"
    if depth == 256:
        return f"\x1b[{48 if background else 38};5;{_to_256(r, g, b)}m"
    i = _to_16(r, g, b)
    base = (40 if background else 30) if i < 8 else (100 if background else 90)
    return f"\x1b[{base + (i % 8)}m"


class Style:
    """Paints text by role for the current theme and colour depth."""

    def __init__(self, theme: str = "dark", depth: int = 24,
                 unicode: bool = True):
        self.depth = depth
        self.unicode = unicode
        self.set_theme(theme)

    def set_theme(self, name: str) -> bool:
        name = ALIASES.get(name, name)
        if name not in THEMES:
            return False
        self.theme_name = name
        self.roles = THEMES[name]
        self._fg: dict[str, str] = {}
        self._bg: dict[str, str] = {}
        return True

    def fg(self, role: str) -> str:
        if role not in self._fg:
            self._fg[role] = sgr_color(self.roles.get(role, ""), self.depth)
        return self._fg[role]

    def bg(self, role: str) -> str:
        if role not in self._bg:
            self._bg[role] = sgr_color(self.roles.get(role, ""), self.depth,
                                       background=True)
        return self._bg[role]

    def __call__(self, role: str | None, text: str, bold: bool = False,
                 italic: bool = False, underline: bool = False,
                 dim: bool = False, bg: str | None = None,
                 strike: bool = False) -> str:
        if self.depth == 0 or not text:
            return text
        pre = ""
        if bold:
            pre += "\x1b[1m"
        if dim:
            pre += "\x1b[2m"
        if italic:
            pre += "\x1b[3m"
        if underline:
            pre += "\x1b[4m"
        if strike:
            pre += "\x1b[9m"
        if role:
            pre += self.fg(role)
        if bg:
            pre += self.bg(bg)
        if not pre:
            return text
        return pre + text + RESET

    def gradient(self, text: str, a: str = "brand_a", b: str = "brand_b",
                 bold: bool = True) -> str:
        """Left-to-right colour blend (the wordmark). Plain at <24-bit."""
        if self.depth < 24 or not text:
            return self(a, text, bold=bold)
        ra, ga, ba = _hex(self.roles[a])
        rb, gb, bb = _hex(self.roles[b])
        n = max(1, len(text) - 1)
        out = "\x1b[1m" if bold else ""
        for i, ch in enumerate(text):
            t = i / n
            out += (f"\x1b[38;2;{round(ra + (rb - ra) * t)};"
                    f"{round(ga + (gb - ga) * t)};{round(ba + (bb - ba) * t)}m{ch}")
        return out + RESET

    def link(self, url: str, text: str | None = None) -> str:
        """Clickable link (OSC 8) where colour is on; plain text otherwise."""
        label = text or url
        if self.depth == 0:
            return label if label == url else f"{label} ({url})"
        return (f"\x1b]8;;{url}\x1b\\" + self("link", label, underline=True)
                + "\x1b]8;;\x1b\\")

    def hyperlink(self, url: str, styled: str) -> str:
        """Make already-styled text clickable (OSC 8), keeping its look."""
        if self.depth == 0 or not url:
            return styled
        return f"\x1b]8;;{url}\x1b\\" + styled + "\x1b]8;;\x1b\\"

    # glyphs with ASCII twins for terminals / screen readers without unicode
    def g(self, name: str) -> str:
        return (GLYPHS if self.unicode else ASCII_GLYPHS).get(name, name)


GLYPHS = {
    "bullet": "●", "dot": "·", "arrow": "›", "branch": "⎿", "tick": "✓",
    "cross": "✗", "warn": "▲", "info": "ℹ", "think": "✻", "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "box_tl": "╭", "box_tr": "╮", "box_bl": "╰", "box_br": "╯", "box_h": "─",
    "box_v": "│", "ellipsis": "…", "bar_full": "█", "bar_empty": "░", "meter_full": "▰", "meter_empty": "▱",
    "todo": "☐", "doing": "◐", "done": "☑", "lock": "⛨", "queue": "↳",
    "up": "↑", "down": "↓", "sep": "│",
    "explored": "◇", "changed": "◈", "ran": "▸", "needs": "◆", "bar": "│",
}
ASCII_GLYPHS = {
    "bullet": "*", "dot": "-", "arrow": ">", "branch": "`-", "tick": "+",
    "cross": "x", "warn": "!", "info": "i", "think": "~", "spin": "|/-\\",
    "box_tl": "+", "box_tr": "+", "box_bl": "+", "box_br": "+", "box_h": "-",
    "box_v": "|", "ellipsis": "...", "bar_full": "#", "bar_empty": ".", "meter_full": "#", "meter_empty": ".",
    "todo": "[ ]", "doing": "[~]", "done": "[x]", "lock": "#", "queue": ">",
    "up": "^", "down": "v", "sep": "|",
    "explored": "*", "changed": "+", "ran": ">", "needs": "!", "bar": "|",
}


def guess_light_background(env=None) -> bool | None:
    """True/False from COLORFGBG ("fg;bg"), None when unknown."""
    env = os.environ if env is None else env
    v = env.get("COLORFGBG", "")
    if ";" in v:
        try:
            bg = int(v.rsplit(";", 1)[1])
            return bg in (7, 15)
        except ValueError:
            return None
    return None


def luminance_light(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 140

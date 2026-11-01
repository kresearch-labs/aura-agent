"""ui.py — colored terminal presentation.

Theme: dark-terminal friendly. 256-color where supported, graceful to plain.
  accent = cyan-500 structure = gray-240 ok = green warn = amber
  danger = red tool glyphs colored by blast radius
"""
from __future__ import annotations

import os
import shutil
import sys

_ENABLED = False
_W = 76

# ---------- palette (ANSI 256) ----------
def _fg(n: int) -> str:
    return f"\033[38;5;{n}m"

C = {
    # V: Aura Blue — a blue-anchored identity, NOT gray-on-gray. Structure =
    # blues/cyan, ok=green, warn=amber, err=red. Legible on conhost black
    # (floor 81 for grays).
    "banner_border": 110,   # Soft blue border
    "banner_title": 39,     # Bright cyan
    "banner_accent": 117,   # Light blue accent
    "banner_dim": 110,
    "label": 111,
    "value": 255,
    "ok": 120,              # Green
    "warn": 215,            # Amber
    "err": 210,             # Red
    "read": 111,            # Blue (reads)
    "write": 183,           # Violet (writes)
    "execute": 215,         # Orange (executes)
    "refuse": 210,
    "user_prompt": 153,
    "agent_marker": 87,     # Cyan marker
    "agent_text": 255,
    "rule": 110,
}


def supports() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    return term != "dumb"


def enable() -> None:
    global _ENABLED
    # Two attempts: the os.system('') trick (activates VT in conhost) then
    # explicit SetConsoleMode.
    if sys.platform == "win32":
        try:
            os.system("")     # Cheap, side-effect-free VT activation
        except Exception:
            pass
        try:
            import ctypes
            k32 = ctypes.windll.kernel32    # type: ignore[attr-defined]
            h = k32.GetStdHandle(-11)       # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(
                    h, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL
        except Exception:
            pass
    _ENABLED = supports()
    global _W
    # Full-width: follow the terminal, no 76-col cap. Floor 20 for tiny panes;
    # cap 400 for pathological widths.
    _W = min(max(shutil.get_terminal_size().columns, 20), 400) if _ENABLED else 76
    # 3.16: screen-reader mode also collapses the banner width
    try:
        from .accessibility import screen_reader
        if screen_reader():
            _W = 100
    except Exception:
        pass
    # 4.1: theme from env (AURA_THEME) at enable-time
    try:
        from .themes import apply, current
        apply(current())
    except Exception:
        pass


def e(s: str, *codes) -> str:
    if not _ENABLED:
        # 3.16: even disabled, accessibility mode replaces glyphs
        try:
            from .accessibility import strip_glyphs
            return strip_glyphs(s)
        except Exception:
            return s
    prefix = "".join(_fg(c) for c in codes if isinstance(c, int))
    out = f"{prefix}{s}\033[0m"
    try:
        from .accessibility import strip_glyphs
        out = strip_glyphs(out)
    except Exception:
        pass
    return out


def paint(s: str, *codes) -> str:
    return e(s, *codes)


# semantic helpers ------------------------------------------------------------
def dim(s): return paint(s, C["banner_dim"])      # 250 — conhost-legible
def muted(s): return paint(s, 249)
def bold(s): return f"\033[1m{s}\033[0m" if _ENABLED else s
def ok(s): return paint(s, C["ok"])
def err(s): return paint(s, C["err" if "err" in C else "refuse"])
def red(s): return paint(s, 174)
def title(s): return paint(s, C["banner_title"])
def accent(s): return paint(s, C["banner_accent"])
def label(s): return paint(s, C["label"])
def value(s): return paint(s, C["value"])
def rule(s): return paint(s, C["rule"])
def plainlen(s: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


import re  # noqa: E402

# ------------------------------------------------------------------ banner ---
GLYPH_COLOR = {"read": 109, "write": 176, "execute": 209}


def banner(version: str, profile: str, model: str, endpoint: str,
           tools: int) -> None:
    W = _W
    B = paint("─" * (W - 2), 240)
    print("┌" + B + "┐")

    def row(lbl, val):
        line = f" {label(lbl.ljust(9))} {val}"
        pad = W - 2 - plainlen(line)
        print("│" + line + " " * max(0, pad) + "│")

    logo = paint("⬢ ", C["banner_accent"]) + bold(paint("Aura", C["banner_title"]))
    row("agent    ", f"{logo} {muted('v' + version)}")
    row("model    ", bold(value(model)) + "  " + dim(endpoint))
    row("profile  ", value(profile))
    row("tools    ", value(str(tools)))
    print("└" + B + "┘")
    print(dim("  /help · Ctrl-C checkpoint · touch .kill to stop"))
    print()


# ------------------------------------------------------------- tool cards ----
def tool_card(name: str, blast: str, ok_: bool, duration_ms: int,
              detail: str = "") -> None:
    color = GLYPH_COLOR.get(blast, 109)
    glyph = paint("◇" if blast == "read" else "◆" if blast == "write" else "◈", color)
    mark = ok("✔") if ok_ else red("✗")
    line = f"  {glyph} {bold(name)}"
    if detail:
        line += "  " + dim(detail[:60])
    if duration_ms:
        line += "  " + muted(f"{duration_ms}ms")
    print(f"  {mark}{line}")


def info(msg: str) -> None:
    print(f"  {muted('·')} {muted(msg)}")


def section(text: str) -> None:
    print(f"\n{rule('────')} {accent(text)} {rule('─' * max(4, 38 - len(text)))}")


def agent_delta(text: str, first: bool) -> None:
    if first:
        print("\n  " + paint("▐", C["agent_marker"]) + " "
              + bold(paint("aura", C["agent_marker"])) + "  ",
              end="", flush=True)
    print(paint(text, C["agent_text"]) if _ENABLED and "\n" not in text
          else text, end="", flush=True)


# ------------------------------------------------------------- markdown --
def _md_gutter(s: str) -> str:
    return paint("│ ", 244) + s


def render_markdown(text: str) -> str:
    """Lightweight markdown → ANSI: fenced code blocks get dimmed gutter
    lines; `inline` code gets accent color; bullet lists get dim markers.
    Lines outside fences pass through (streaming-friendly, no surprises)."""
    if not _ENABLED:
        return text
    out_lines: list[str] = []
    in_fence = False
    for ln in text.split("\n"):
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(paint("┄┄┄", 244))
            continue
        if in_fence:
            out_lines.append(_md_gutter(ln))
            continue
        if "`" in ln:
            parts = ln.split("`")
            for i in range(1, len(parts), 2):
                parts[i] = paint(parts[i], C.get("value", 39))
            ln = "".join(parts)
        if ln.strip().startswith(("- ", "* ")):
            ln = paint("  ·", 244) + ln[1:]
        elif stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            if 1 <= level <= 3 and text:
                glyph = {1: "■", 2: "▪", 3: "·"}[level]
                ln = paint(f"{glyph} ", C.get("banner_accent", 117)) \
                     + bold(text)
        elif stripped[:2] in (f"{n}." for n in range(1, 10)) \
                and stripped[2:3] == ".":
            ln = paint("  ·", 244) + " " + stripped[3:]
        out_lines.append(ln)
    return "\n".join(out_lines)


def success(msg: str) -> None:
    print(f"  {ok('✔')} {ok(msg)}")


def warn(msg: str) -> None:
    print(f"  {paint('⚠', C['warn'])} {paint(msg, C['warn'])}")


def error(msg: str) -> None:
    print(f"  {red('✗')} {red(msg)}")

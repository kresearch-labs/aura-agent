"""console.py

Aura's chat presentation, designed for a security operator's console. REV-2
IDENTITY (user correction: the first pass borrowed Ψ/◈/❯ from the reference TUI
— Aura's mark is its receipt VOCABULARY, not a logo):

  answer_card(text, state, checks, target, elapsed)
      State-word card: the header IS the receipt status
      (PROVEN / PARTIAL / FAILED / BLOCKED), the footer is the
      evidence line (checks · target · time). A lab result card,
      not a chat bubble.

  activity(detail, ms, blast, outcome)
      Outcome ladder: dot-leader line, glyph = OUTCOME
      (ok ✓ / scope ⊘ / run ·), duration right-aligned.

  scope_block(target, why) blocked call as a first-class scope event
  status_bar(...) bracket-segmented operator strip
  prompt_line(eng, proven) [aura · engagement · N proven] — the
                             engagement envelope as the prompt.

Conhost hard rules: no emoji, no logo glyphs; ASCII fallback
(set_unicode(False), auto-detected by callers).
"""
from __future__ import annotations

from .ui import paint, C, plainlen as _plainlen

# unicode switch (callers may force ASCII for non-UTF-8 consoles)
_UNICODE = True


def set_unicode(on: bool) -> None:
    global _UNICODE
    _UNICODE = bool(on)


def unicode_ok() -> bool:
    return _UNICODE


def plainlen(s: str) -> int:
    """Visible width of s (ANSI-free). Re-exported from ui for callers."""
    return _plainlen(s)


# --------------------------------------------------------------- glyphs
_GLYPHS = {
    # key: (unicode, ascii)
    "card_tl": ("┌", "+"),
    "card_h": ("─", "-"),
    "card_bl": ("└", "+"),
    "ladder": ("├", "|"),
    "ladder_end": ("└", "|"),
    "scope": ("⊘", "x"),
    "ok": ("✓", "+"),
}


def _g(key: str) -> str:
    u, a = _GLYPHS[key]
    return u if _UNICODE else a


# receipt status → header word + color key. THE identity: the five-status
# vocabulary from receipts.py rendered as the card title.
_STATE_WORD = {
    "ok": ("PROVEN", "ok"),
    "warn": ("PARTIAL", "warn"),
    "fail": ("FAILED", "err"),
    "blocked": ("BLOCKED", "err"),
}


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}".strip()
            if plainlen(cand) > width and line:
                out.append(line)
                line = word
            else:
                line = cand
        out.append(line)
    return out


def fmt_dur(ms: float | int) -> str:
    """Per-call duration: <60s → '0.4s'; >=60s → '1m02s'."""
    s = float(ms) / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s) // 60}m{int(s) % 60:02d}s"


def answer_card(text: str, state: str = "ok",
                width: int | None = None,
                checks: tuple[int, int] | None = None,
                target: str = "", elapsed: str = "",
                unicode_ok: bool | None = None) -> str:
    """State-word card: `┌─ PROVEN ──…` header, evidence footer."""
    if width is None:
        import shutil as _sh
        width = max(_sh.get_terminal_size().columns, 20)
    global _UNICODE
    saved = _UNICODE
    if unicode_ok is not None:
        _UNICODE = bool(unicode_ok)
    try:
        word, colkey = _STATE_WORD.get(state, _STATE_WORD["ok"])
        col = C.get(colkey, C.get("ok", 120))
        rule = C.get("rule", 247)
        tl, bl, h = _g("card_tl"), _g("card_bl"), _g("card_h")
        inner = max(width - 4, 20)
        head_txt = f"{tl}{h} {word} "
        head_fill = max(width - plainlen(head_txt) - 1, 3)
        top = paint(head_txt, col) + paint(h * head_fill, col)
        body: list[str] = [top]
        for ln in _wrap(text, inner):
            body.append(paint(f"{h} ", rule) + ln)
        foot_parts = []
        if checks:
            foot_parts.append(f"{checks[0]}/{checks[1]} checks")
        if target:
            foot_parts.append(target[:32])
        if elapsed:
            foot_parts.append(elapsed)
        if foot_parts:
            foot = f"{bl}{h} " + " · ".join(foot_parts) + " "
            body.append(paint(foot, col) +
                        paint(h * max(width - plainlen(foot) - 1, 3), col))
        else:
            body.append(paint(bl + h * (width - 2), col))
        return "\n".join(body)
    finally:
        _UNICODE = saved


def activity(detail: str, duration_ms: float | int = 0,
             blast: str = "read", outcome: str = "run",
             last: bool = False) -> str:
    """One outcome-ladder line: ├ branch, dot-leader to the duration,
    outcome glyph at the right. blast colors the detail text
    (read=dim / execute=bright / write=green)."""
    branch = paint(_g("ladder_end") if last else _g("ladder"),
                   C.get("rule", 247))
    dcol = {"read": C.get("label", 250),
            "execute": C.get("agent_text", 255),
            "write": C.get("ok", 120)}.get(blast, C.get("label", 250))
    detail = detail if len(detail) <= 40 else detail[:37] + "…"
    line = f"  {branch} {paint(detail, dcol)}"
    dur = fmt_dur(duration_ms) if duration_ms else ""
    if dur:
        pad = max(2, 44 - plainlen(detail) - len(dur))
        line += paint(" " * pad + "... " + dur, C.get("label", 250))
    oc = {"ok": ("✓", C.get("ok", 120)),
          "scope": ("⊘", C.get("err", 210)),
          "blocked": ("⊘", C.get("err", 210)),
          "refused": ("⊘", C.get("warn", 215)),
          "run": ("", 0)}.get(outcome, ("", 0))
    if oc[0]:
        line += "  " + paint(oc[0], oc[1])
    return line


def scope_block(target: str, why: str) -> str:
    """A blocked call as a first-class scope event — teaches scope
    in-line instead of a bare warning."""
    g = paint(_g("scope"), C.get("err", 210))
    return f"  {g} {target}: outside scope — {why}"


def divider(width: int | None = None) -> str:
    if width is None:
        import shutil as _sh
        width = max(_sh.get_terminal_size().columns, 20)
    return paint(_g("card_h") * width, C.get("rule", 247))


def status_bar(model: str = "", ctx_pct: int | None = None,
               elapsed: str = "", proven: int | None = None,
               target: str = "", tokens: str = "",
               ctx_total: int = 0, ctx_used: int | None = None) -> str:
    """Bracket-segmented operator strip, no leading glyph: [model | ctx 3k/131k
    tok · NN% ▰▰▱▱ | elapsed | tokens] Absolute numbers are the law. ctx_used
    is the real measured count — shown verbatim; the percentage is derived from
    it, never the other way round.
    """
    segs: list[str] = []
    if model:
        segs.append(model)
    if ctx_pct is not None or (ctx_used is not None and ctx_total):
        bar_w = 10
        if ctx_pct is None and ctx_total:
            ctx_pct = int(100 * (ctx_used or 0) / ctx_total)
        ctx_pct = ctx_pct or 0
        fill = max(0, min(bar_w, bar_w * ctx_pct // 100))
        col = C.get("ok", 120) if ctx_pct < 60 else \
            C.get("warn", 215) if ctx_pct < 85 else C.get("err", 210)
        # ▰▱ meter
        bar = paint("▰" * fill + "▱" * (bar_w - fill), col)
        if ctx_total:
            def _ktok(n: int) -> str:
                return f"{n // 1000}k" if n >= 1000 else str(n)
            shown_used = _ktok(ctx_used if ctx_used is not None
                               else int(ctx_total * ctx_pct / 100))
            segs.append(f"ctx {shown_used}/"
                        f"{_ktok(ctx_total)} tok · {ctx_pct}% {bar}")
        else:
            segs.append(f"ctx {ctx_pct}% {bar}")
    if elapsed:
        segs.append(elapsed)
    if tokens:
        segs.append(tokens)
    # "N proven" + cwd target segments dropped
    if not segs:
        return ""
    return (paint("┃", C.get("rule", 110))
            + paint("[", C.get("rule", 110))
            + paint(" | ", C.get("rule", 247)).join(segs)
            + paint("]", C.get("rule", 110))
            + paint("┃", C.get("rule", 110)))


def prompt_line(engagement: str, proven: int = 0) -> str:
    """The engagement envelope as the prompt — no glyph: ►"""
    return paint("►", C.get("agent_marker", 87)) + " "

"""markdown.py — markdown → styled terminal lines, built for streaming.

The model's answer arrives a few characters at a time. MarkdownStream
splits it into blocks (paragraph, heading, list, quote, code, table,
rule); a block that is finished goes straight to the scrollback and is
never redrawn, only the block still being written stays in the live area.
That keeps long answers smooth and the scrollback clean.

Inline: **bold**, *italic*, `code`, ~~strike~~, [links](url) and bare
URLs (clickable where the terminal supports OSC 8). Code blocks are
syntax-coloured and copy cleanly (no border characters inside them).
Tables get box lines and fit the width.
"""
from __future__ import annotations

import re

from .highlight import highlight
from .style import Style
from .text import width, wrap

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


# ---------------------------------------------------------------- inline --
def inline(text: str, s: Style) -> str:
    """Inline markdown → styled text."""
    if s.depth == 0:
        # keep it readable without colour: drop the markers, keep links
        t = re.sub(r"`([^`]+)`", r"\1", text)
        t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
        t = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"\1 (\2)", t)
        return t
    codes: list[str] = []

    def keep_code(m):
        codes.append(s("inline_code", m.group(1)))
        return f"\x00{len(codes) - 1}\x00"
    t = re.sub(r"`([^`]+)`", keep_code, text)
    links: list[str] = []

    def keep_link(m):
        links.append(s.link(m.group(2), m.group(1)))
        return f"\x01{len(links) - 1}\x01"
    t = re.sub(r"\[([^\]]+)\]\((\S+?)\)", keep_link, t)
    t = re.sub(r"(?<![\w(\x01])(https?://[^\s)<>\]]+[^\s)<>\].,;:!?'\"])",
               lambda m: (links.append(s.link(m.group(1))), f"\x01{len(links) - 1}\x01")[1], t)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__",
               lambda m: s("strong", m.group(1) or m.group(2), bold=True), t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])",
               lambda m: s(None, m.group(1) or m.group(2), italic=True), t)
    t = re.sub(r"~~(.+?)~~", lambda m: s("dim", m.group(1), strike=True), t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], t)
    t = re.sub(r"\x01(\d+)\x01", lambda m: links[int(m.group(1))], t)
    return t


# ---------------------------------------------------------------- blocks --
def split_blocks(text: str) -> tuple[list[list[str]], list[str], bool]:
    """(finished blocks, the open block, whether the open block is a code
    fence). A block is finished when something after it proves it ended."""
    lines = text.split("\n")
    # the last element is the line being typed (no newline yet)
    complete, tail = lines[:-1], lines[-1]
    blocks: list[list[str]] = []
    cur: list[str] = []
    in_code = False
    fence = ""
    kind = ""

    def close():
        nonlocal cur, kind
        if cur:
            blocks.append(cur)
        cur, kind = [], ""

    for ln in complete:
        if in_code:
            cur.append(ln)
            if ln.strip().startswith(fence) and ln.strip().strip(fence[0]) == "":
                in_code = False
                close()
            continue
        m = _FENCE.match(ln)
        if m:
            close()
            in_code, fence = True, m.group(2)
            cur = [ln]
            kind = "code"
            continue
        if not ln.strip():
            close()
            continue
        if _HEADING.match(ln) or _HR.match(ln):
            close()
            blocks.append([ln])
            continue
        this = ("table" if ln.lstrip().startswith("|") else
                "list" if _LIST.match(ln) or (kind == "list" and ln.startswith((" ", "\t"))) else
                "quote" if ln.lstrip().startswith(">") else "para")
        if cur and this != kind and not (kind == "list" and this == "para" and ln.startswith(" ")):
            close()
        kind = this
        cur.append(ln)
    open_block = cur + ([tail] if tail else [])
    if not open_block and tail:
        open_block = [tail]
    return blocks, open_block, in_code


def render_block(block: list[str], s: Style, w: int, streaming: bool = False) -> list[str]:
    """Styled lines for one block at width w."""
    if not block:
        return []
    first = block[0]
    m = _FENCE.match(first)
    if m:
        lang = m.group(3)
        body = block[1:]
        if body and _FENCE.match(body[-1]) and body[-1].strip().strip(m.group(2)[0]) == "":
            body = body[:-1]
        return render_code(body, lang, s, w)
    m = _HEADING.match(first)
    if m and len(block) == 1:
        level, txt = len(m.group(1)), inline(m.group(2), s)
        if level == 1:
            return [s("heading", txt, bold=True, underline=True)]
        if level == 2:
            return [s("heading", txt, bold=True)]
        return [s("strong", txt, bold=True)]
    if _HR.match(first) and len(block) == 1:
        return [s("rule", s.g("box_h") * min(w, 60))]
    if first.lstrip().startswith("|") and len(block) >= 2 and _TABLE_SEP.match(block[1]):
        return render_table(block, s, w)
    if first.lstrip().startswith(">"):
        bar = s("quote", "▎ " if s.unicode else "| ")
        text = " ".join(l.lstrip()[1:].strip() for l in block)
        return [bar + s("quote", l, italic=True) for l in wrap(inline(text, s), w - 2)]
    if _LIST.match(first):
        return render_list(block, s, w)
    text = " ".join(l.strip() for l in block if l.strip())
    # keep hard line breaks the model clearly meant (two trailing spaces)
    if any(l.endswith("  ") for l in block):
        out = []
        for l in block:
            out.extend(wrap(inline(l.strip(), s), w))
        return out
    return wrap(inline(text, s), w)


def render_list(block: list[str], s: Style, w: int) -> list[str]:
    out: list[str] = []
    items: list[tuple[int, str, str]] = []
    for ln in block:
        m = _LIST.match(ln)
        if m:
            items.append((len(m.group(1).expandtabs(4)), m.group(2), m.group(3)))
        elif items:
            d, mk, t = items[-1]
            items[-1] = (d, mk, t + " " + ln.strip())
    base = min((d for d, _, _ in items), default=0)
    for depth, marker, txt in items:
        level = (depth - base) // 2
        ind = "  " * level
        tm = _TASK.match(txt)
        if tm:
            done = tm.group(1).lower() == "x"
            mark = s("ok" if done else "dim", s.g("done") if done else s.g("todo"))
            txt = s("dim", tm.group(2), strike=done) if done else inline(tm.group(2), s)
        else:
            txt = inline(txt, s)
            if marker[0].isdigit():
                mark = s("accent", marker)
            else:
                mark = s("accent", ("•", "◦", "▪")[level % 3] if s.unicode else "-")
        lead = ind + mark + " "
        out.extend(wrap(txt, w, indent=" " * width(lead), first_indent=lead))
    return out


def render_code(body: list[str], lang: str, s: Style, w: int) -> list[str]:
    out: list[str] = []
    label = (lang or "").strip()
    if label:
        out.append(s("faint", label))
    hl = highlight("\n".join(body), lang, s) if body else []
    inner = max(10, w - 2)
    for raw, ln in zip(body, hl):
        if width(raw) > inner:
            # long code lines: soft-wrap with a continuation mark
            parts = wrap(ln, inner - 1)
            for k, p in enumerate(parts):
                out.append(_code_line(("" if k == 0 else s("faint", "↪")) + p, s, w))
        else:
            out.append(_code_line(ln, s, w))
    return out


def _code_line(ln: str, s: Style, w: int) -> str:
    """Code copies cleanly: an indent and colours only — no border
    characters and no trailing padding (both end up in what you copy)."""
    return ("    " if s.depth == 0 else "  ") + ln


def render_table(block: list[str], s: Style, w: int) -> list[str]:
    def cells(row: str) -> list[str]:
        r = row.strip()
        if r.startswith("|"):
            r = r[1:]
        if r.endswith("|"):
            r = r[:-1]
        return [c.strip() for c in re.split(r"(?<!\\)\|", r)]
    header = cells(block[0])
    aligns = []
    for c in cells(block[1]):
        aligns.append("r" if c.endswith(":") and not c.startswith(":") else
                      "c" if c.startswith(":") and c.endswith(":") else "l")
    rows = [cells(r) for r in block[2:] if r.strip()]
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    styled = [[inline(c, s) for c in header]] + [[inline(c, s) for c in r] for r in rows]
    widths = [max(width(r[i]) for r in styled) for i in range(n)]
    avail = w - (3 * n + 1)
    while sum(widths) > avail and max(widths) > 6:
        i = widths.index(max(widths))
        widths[i] -= 1
    v = s("border", s.g("box_v"))
    h = s.g("box_h")
    top = s("border", ("┌" if s.unicode else "+") + ("┬" if s.unicode else "+").join(h * (cw + 2) for cw in widths) + ("┐" if s.unicode else "+"))
    mid = s("border", ("├" if s.unicode else "+") + ("┼" if s.unicode else "+").join(h * (cw + 2) for cw in widths) + ("┤" if s.unicode else "+"))
    bot = s("border", ("└" if s.unicode else "+") + ("┴" if s.unicode else "+").join(h * (cw + 2) for cw in widths) + ("┘" if s.unicode else "+"))
    out = [top]
    for ri, row in enumerate(styled):
        wrapped = [wrap(c, widths[i]) or [""] for i, c in enumerate(row)]
        height = max(len(x) for x in wrapped)
        for k in range(height):
            parts = []
            for i in range(n):
                txt = wrapped[i][k] if k < len(wrapped[i]) else ""
                if ri == 0:
                    txt = s("strong", txt, bold=True)
                gap = widths[i] - width(txt)
                if aligns[i:i + 1] == ["r"]:
                    txt = " " * gap + txt
                elif aligns[i:i + 1] == ["c"]:
                    txt = " " * (gap // 2) + txt + " " * (gap - gap // 2)
                else:
                    txt = txt + " " * gap
                parts.append(" " + txt + " ")
            out.append(v + v.join(parts) + v)
        if ri == 0:
            out.append(mid)
    out.append(bot)
    return out


def render(text: str, s: Style, w: int) -> list[str]:
    """Whole document → lines (blank line between blocks)."""
    blocks, open_block, _ = split_blocks(text + "\n")
    if open_block and any(l.strip() for l in open_block):
        blocks.append(open_block)
    out: list[str] = []
    for i, b in enumerate(blocks):
        lines = render_block(b, s, w)
        if out and lines:
            out.append("")
        out.extend(lines)
    return out


class MarkdownStream:
    """Feed the answer as it streams; take finished lines as they settle."""

    def __init__(self, s: Style, width_fn):
        self.s = s
        self.width_fn = width_fn
        self.text = ""
        self.done_blocks = 0
        self.emitted_any = False

    def feed(self, delta: str) -> list[str]:
        self.text += delta
        return self._settle()

    def _settle(self) -> list[str]:
        blocks, _, _ = split_blocks(self.text)
        out: list[str] = []
        for b in blocks[self.done_blocks:]:
            lines = render_block(b, self.s, self.width_fn())
            if lines:
                if self.emitted_any:
                    out.append("")
                out.extend(lines)
                self.emitted_any = True
        self.done_blocks = len(blocks)
        return out

    def pending(self) -> list[str]:
        """The block still being written, rendered for the live area."""
        _, open_block, in_code = split_blocks(self.text)
        if not open_block or not any(l.strip() for l in open_block):
            return []
        lines = render_block(open_block, self.s, self.width_fn(), streaming=True)
        return ([""] if self.emitted_any else []) + lines

    def finish(self) -> list[str]:
        """Answer complete: everything left becomes final lines (an
        unclosed code block is closed)."""
        out = self._settle()
        _, open_block, _ = split_blocks(self.text)
        if open_block and any(l.strip() for l in open_block):
            lines = render_block(open_block, self.s, self.width_fn())
            if lines:
                if self.emitted_any:
                    out.append("")
                out.extend(lines)
                self.emitted_any = True
        self.done_blocks = 10 ** 9
        return out

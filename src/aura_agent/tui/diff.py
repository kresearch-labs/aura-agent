"""diff.py — coloured diffs with line numbers for edits and writes.

Shows what really changed in a file: removed lines on a red band, added
lines on a green band, both with their line numbers, syntax-coloured,
3 lines of context, and a "+N −M" summary. Long diffs are cut with a
note saying how much is hidden (Ctrl+O shows everything).
"""
from __future__ import annotations

import difflib

from .highlight import LineHighlighter, lang_for_path
from .style import Style
from .text import cut, pad, width


def stats(old: str, new: str) -> tuple[int, int]:
    a, b = old.splitlines(), new.splitlines()
    add = rem = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            rem += i2 - i1
        if op in ("replace", "insert"):
            add += j2 - j1
    return add, rem


def render_diff(old: str, new: str, path: str, s: Style, w: int,
                max_lines: int | None = 40, context: int = 3,
                start: int = 1) -> list[str]:
    """`start`: the file line number of the first line (for snippets)."""
    a, b = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    groups = list(sm.get_grouped_opcodes(context))
    if not groups:
        return []
    off = start - 1
    num_w = len(str(max(len(a), len(b), 1) + off))
    lang = lang_for_path(path)
    hl_old = LineHighlighter(lang, s)
    hl_new = LineHighlighter(lang, s)
    body_w = max(10, w - num_w - 4)
    out: list[str] = []
    for gi, group in enumerate(groups):
        if gi:
            out.append(s("faint", " " * num_w + "  ⋮" if s.unicode else " " * num_w + "  ..."))
        for op, i1, i2, j1, j2 in group:
            if op == "equal":
                for k in range(i2 - i1):
                    out.append(_line(s, " ", j1 + k + 1 + off, hl_new.line(b[j1 + k]), num_w, body_w, None))
                    hl_old.line(a[i1 + k])
                continue
            if op == "replace" and i2 - i1 == j2 - j1 and s.depth:
                # line-for-line change: show exactly which words changed
                olds, news = [], []
                for k in range(i2 - i1):
                    hl_old.line(a[i1 + k])
                    hl_new.line(b[j1 + k])
                    ob, nb = _word_marks(a[i1 + k], b[j1 + k], s)
                    olds.append(_line(s, "-", i1 + k + 1 + off, ob, num_w, body_w, "del"))
                    news.append(_line(s, "+", j1 + k + 1 + off, nb, num_w, body_w, "add"))
                out.extend(olds + news)
                continue
            for k in range(i1, i2):
                out.append(_line(s, "-", k + 1 + off, hl_old.line(a[k]), num_w, body_w, "del"))
            for k in range(j1, j2):
                out.append(_line(s, "+", k + 1 + off, hl_new.line(b[k]), num_w, body_w, "add"))
    if max_lines is not None and len(out) > max_lines:
        hidden = len(out) - max_lines
        out = out[:max_lines] + [s("faint", f"  … {hidden} more diff lines (ctrl+o to expand)")]
    return out


def _line(s: Style, sign: str, n: int, body: str, num_w: int, body_w: int,
          kind: str | None) -> str:
    num = s("line_no", str(n).rjust(num_w))
    body = cut(body.expandtabs(4), body_w)
    if kind is None:
        return f"{num}   {body}"
    role = "diff_add" if kind == "add" else "diff_del"
    if s.depth == 0:
        return f"{num} {sign} {body}"
    bg = s.bg(role + "_bg")
    mark = s(role, sign, bold=True)
    band = bg + " " + mark + bg + " " + body.replace("\x1b[0m", "\x1b[0m" + bg)
    return num + band + pad("", max(0, body_w - width(body))) + "\x1b[0m"


def _word_marks(old: str, new: str, s: Style) -> tuple[str, str]:
    """Both lines with the changed words on a brighter band. Changes are
    found word by word (letters/digits runs, then single symbols), so a
    one-character fix highlights its word, not the whole line."""
    import re
    tok = lambda t: re.findall(r"\w+|\s+|[^\w\s]", t.expandtabs(4))  # noqa: E731
    ta, tb = tok(old), tok(new)
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    oa, ob = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        sa, sb = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        if op == "equal":
            oa.append(s("text", sa))
            ob.append(s("text", sb))
            continue
        if sa:
            oa.append(s("strong", sa, bold=True, bg="diff_del_hl"))
        if sb:
            ob.append(s("strong", sb, bold=True, bg="diff_add_hl"))
    return "".join(oa), "".join(ob)


def summary(add: int, rem: int, s: Style) -> str:
    parts = []
    if add:
        parts.append(s("diff_add", f"+{add}"))
    if rem:
        parts.append(s("diff_del", f"−{rem}" if s.unicode else f"-{rem}"))
    return " ".join(parts) if parts else s("dim", "no change")

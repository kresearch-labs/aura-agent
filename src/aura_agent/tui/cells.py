"""cells.py — what the transcript is made of.

Each function returns finished lines for the scrollback: your message,
a tool call with its real result (lines read, exit code and output tail,
a coloured diff for edits, the page title for a web read…), notices,
the approval decision and the one-line footer at the end of a turn.
"""
from __future__ import annotations

import json
import os
import re

from .diff import render_diff, stats, summary as diff_summary
from .highlight import highlight, lang_for_path
from .style import Style
from .text import cut, width, wrap, wrap_text

BLAST_ROLE = {"read": "tool_read", "write": "tool_write", "execute": "tool_exec"}
# read_file's line prefix: `12|` or, with line tags on, `12:a3|`
_LINE_NO = re.compile(r"^\d+(?::[0-9a-f]{2})?\|")


# ------------------------------------------------------------------ titles --
def tool_title(name: str, args: dict) -> tuple[str, str]:
    """(verb, target) in plain words for a tool call."""
    a = args or {}
    p = str(a.get("path", "") or "")
    if name == "read_file":
        rng = ""
        if a.get("offset") or a.get("limit"):
            o = int(a.get("offset") or 1)
            rng = f"  lines {o}–{o + int(a.get('limit') or 2000) - 1}"
        return "Read", p + rng
    if name == "write_file":
        return "Write", p
    if name == "edit_file":
        return "Edit", p
    if name == "list_files":
        return "List", p or "."
    if name == "run_command":
        return "Run", str(a.get("cmd") or a.get("command") or "")
    if name == "run_checks":
        return "Check", str(a.get("kind", "tests"))
    if name in ("web_crawl", "web_fetch"):
        return "Fetch", str(a.get("url") or a.get("target") or "")
    if name == "browser_run":
        return "Browse", str(a.get("url") or "")
    if name == "delegate":
        return "Helper", str(a.get("task", ""))
    if name == "todo_write":
        return "Plan", str(a.get("text") or a.get("action") or "update")
    if name == "finding_add":
        return "Record result", str(a.get("title", ""))
    if name == "finding_list":
        return "Results", ""
    if name == "memory_write":
        return "Remember", str(a.get("text", ""))
    if name in ("skill_load", "skill_write"):
        return ("Load skill" if name == "skill_load" else "Save skill"), str(a.get("name", ""))
    if name.startswith("rules"):
        return "Rules", str(a.get("text") or a.get("rule") or "")
    if name == "symbols":
        return "Symbols", str(a.get("query") or a.get("path") or "")
    if name in ("notes", "notes_write", "state", "state_write"):
        return "Notes", str(a.get("section") or a.get("text") or "")[:60]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return parts[1] if len(parts) > 2 else "MCP", parts[-1]
    return name.replace("_", " ").capitalize(), ""


# ------------------------------------------------------------------- parse --
def _json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def command_output(result: str) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr) from a run_command result, which may be
    nested JSON (the shell's own result wrapped by the tool layer)."""
    d = _json(result)
    code, out, err = None, "", ""
    for _ in range(3):
        if not isinstance(d, dict):
            break
        if isinstance(d.get("exit_code"), int):
            code = d["exit_code"]
        if "stdout" in d or "stderr" in d:
            out, err = str(d.get("stdout") or ""), str(d.get("stderr") or "")
            break
        inner = d.get("result")
        d = _json(inner) if isinstance(inner, str) else inner
        if d is None and isinstance(inner, str):
            out = inner
            break
    if code is None and d is None and not out:
        out = result
    return code, out, err


# ------------------------------------------------------------------- cells --
def user_cell(text: str, s: Style, w: int) -> list[str]:
    mark = s("user", s.g("arrow") + " ", bold=True)
    body = wrap_text(text, w - 2, indent="  ", first_indent="")
    out = [""]
    for i, ln in enumerate(body):
        out.append((mark if i == 0 else "  ") + s("strong", ln) if ln else "")
    return out


def tool_cell(ev: dict, s: Style, w: int, verbose: bool = False,
              before: str | None = None, workspace=None) -> list[str]:
    """A finished tool call: title line + result lines."""
    name = ev.get("tool", "?")
    args = ev.get("args") or {}
    status = ev.get("status", "ok")
    result = str(ev.get("result") or ev.get("detail") or "")
    verb, target = tool_title(name, args)
    role = BLAST_ROLE.get(ev.get("blast", "read"), "tool_read")
    ok = status == "ok"
    dot = s(role if ok else ("warn" if status in ("refused", "blocked", "awaiting_approval") else "err"),
            s.g("bullet"))
    ms = ev.get("duration_ms") or 0
    timing = s("faint", f"  {ms / 1000:.1f}s") if ms >= 1000 else ""
    head = f"{dot} {s('strong', verb, bold=True)}"
    if target:
        tgt = target if name != "run_command" else target.replace("\n", " ⏎ ")
        shown = s(role, cut(tgt, max(10, w - width(verb) - 12)))
        path = str(args.get("path", "") or "")
        if workspace is not None and path and name in (
                "read_file", "write_file", "edit_file", "list_files"):
            # Ctrl/Cmd+click opens the file, in terminals that support links
            try:
                from pathlib import Path as _P
                shown = s.hyperlink((_P(workspace) / path).resolve().as_uri(), shown)
            except (ValueError, OSError):
                pass
        head += " " + shown
    out = [head + timing]
    branch = s("faint", "  " + s.g("branch") + " ")
    pad = "    "

    def detail(lines: list[str]):
        for i, ln in enumerate(lines):
            out.append((branch if i == 0 else pad) + ln)

    if not ok:
        reason = result.strip() or status
        reason = re.sub(r"^(error|refused):\s*", "", reason, flags=re.I)
        if status in ("refused", "blocked"):
            detail([s("warn", l) for l in wrap(cut(reason, 400), w - 6)][:4])
        elif status == "awaiting_approval":
            detail([s("warn", "waiting for your approval")])
        else:
            detail([s("err", l) for l in wrap(cut(reason, 600), w - 6)][:6])
        return out

    if name == "read_file":
        n = len([l for l in result.splitlines() if _LINE_NO.match(l)])
        trunc = " (more in the file)" if "TRUNCATED" in result else ""
        detail([s("dim", f"{n} line{'s' if n != 1 else ''}{trunc}")])
        if verbose:
            lang = lang_for_path(str(args.get("path", "")))
            body = [_LINE_NO.sub("", l) for l in result.splitlines()[:40]]
            for ln in highlight("\n".join(body), lang, s):
                out.append(pad + cut(ln, w - 6))
        return out
    if name == "list_files":
        items = _json(result)
        n = len(items) if isinstance(items, list) else result.count("\n") + 1
        detail([s("dim", f"{n} entr{'ies' if n != 1 else 'y'}")])
        return out
    if name == "run_command":
        code, so, se = command_output(result)
        text = (so + ("\n" + se if se and (code or not so) else "")).rstrip()
        lines = [l for l in text.splitlines()]
        keep = 40 if verbose else 5
        status_txt = (s("ok", "exit 0") if code == 0 else
                      s("err", f"exit {code}") if code is not None else "")
        shown = lines[-keep:]
        hidden = len(lines) - len(shown)
        body = []
        if hidden:
            body.append(s("faint", f"… {hidden} earlier line{'s' if hidden != 1 else ''}"))
        body += [s("dim", cut(l.expandtabs(4), w - 6)) for l in shown]
        if not body:
            body = [s("dim", "no output")]
        detail(([status_txt] if status_txt else []) + body)
        return out
    if name in ("edit_file", "write_file"):
        path = str(args.get("path", ""))
        if name == "edit_file":
            old, new = str(args.get("old_string") or args.get("old") or ""),                 str(args.get("new_string") or args.get("new") or "")
            after = _read(workspace, path) if workspace else None
            if after is not None and new and new in after:
                # the real file around the change: right line numbers,
                # indentation and context
                before_txt = after.replace(new, old, 1)
                a, r = stats(before_txt, after)
                lines = render_diff(before_txt, after, path, s, w - 4,
                                    max_lines=None if verbose else 14)
            else:
                a, r = stats(old, new)
                lines = render_diff(old, new, path, s, w - 4,
                                    max_lines=None if verbose else 14)
        else:
            new = str(args.get("content", ""))
            if before is None:
                n = new.count("\n") + (0 if new.endswith("\n") else 1)
                detail([s("dim", f"new file · {n} line{'s' if n != 1 else ''}")])
                lang = lang_for_path(path)
                preview = new.splitlines()[:(40 if verbose else 6)]
                for ln in highlight("\n".join(preview), lang, s):
                    out.append(pad + cut(ln, w - 6))
                if len(new.splitlines()) > len(preview):
                    out.append(pad + s("faint", f"… {len(new.splitlines()) - len(preview)} more lines"))
                return out
            a, r = stats(before, new)
            lines = render_diff(before, new, path, s, w - 4,
                                max_lines=None if verbose else 14)
        detail([diff_summary(a, r, s) + ("" if "[fuzzy-match]" not in result
                                          else s("faint", "  (matched loosely)"))])
        out.extend(pad + l for l in lines)
        note = _syntax_note(result)
        if note:
            out.append(pad + s("warn", note))
        return out
    if name in ("web_crawl", "web_fetch", "browser_run"):
        d = _json(result)
        if isinstance(d, dict):
            title = d.get("title") or ""
            chars = len(d.get("text") or "")
            st = d.get("status")
            bits = [b for b in (title and s("strong", cut(title, 70)),
                                st and s("dim", f"HTTP {st}"),
                                chars and s("dim", f"{chars:,} chars"),
                                d.get("pages_crawled", 1) > 1 and s("dim", f"{d['pages_crawled']} pages"))
                    if b]
            detail([" · ".join(bits) or s("dim", "done")])
            if d.get("error"):
                out.append(pad + s("warn", str(d["error"])[:200]))
        else:
            detail([s("dim", cut(result.strip().splitlines()[0] if result.strip() else "done", w - 6))])
        return out
    if name == "delegate":
        body = result.split("\n", 1)
        summary_line = body[1].strip().splitlines()[0] if len(body) > 1 and body[1].strip() else body[0]
        detail([s("dim", cut(summary_line, w - 6))])
        return out
    if name == "todo_write":
        return out          # The live plan panel shows the list itself
    if name == "finding_add":
        proven = "proven" in result.lower() and "not proven" not in result.lower()
        detail([s("ok" if proven else "warn", "proven" if proven else "recorded — not proven")])
        return out
    first = result.strip().splitlines()[0] if result.strip() else "done"
    detail([s("dim", cut(first, w - 6))])
    return out


def read_group_cell(batch: list[dict], s: Style, w: int, workspace=None) -> list[str]:
    """Several reads in a row as one row with their names."""
    files = [b for b in batch if b.get("tool") == "read_file"]
    other = [b for b in batch if b.get("tool") != "read_file"]
    parts = []
    if files:
        parts.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
    if other:
        parts.append(f"{len(other)} listing{'s' if len(other) != 1 else ''}")
    head = (s("tool_read", s.g("bullet")) + " " + s("strong", "Read", bold=True)
            + " " + s("tool_read", " and ".join(parts)))
    names = []
    for b in batch:
        args = b.get("args") or {}
        p = str(args.get("path") or args.get("query") or ".")
        if b.get("tool") == "read_file":
            n = len([l for l in str(b.get("result", "")).splitlines() if _LINE_NO.match(l)])
            label = s("text", p) + s("faint", f" {n}")
        else:
            label = s("text", p + ("/" if not p.endswith("/") else "")) if b.get("tool") == "list_files" \
                else s("text", p)
        if workspace is not None and b.get("tool") == "read_file":
            try:
                from pathlib import Path as _P
                label = s.hyperlink((_P(workspace) / p).resolve().as_uri(), label)
            except (ValueError, OSError):
                pass
        names.append(label)
    body = wrap(s("faint", " · ").join(names), w - 6)
    out = [head]
    for i, ln in enumerate(body[:4]):
        out.append((s("faint", "  " + s.g("branch") + " ") if i == 0 else "    ") + ln)
    if len(body) > 4:
        out.append("    " + s("faint", "…"))
    return out


def _read(workspace, path: str) -> str | None:
    try:
        with open(os.path.join(str(workspace), path), encoding="utf-8",
                  errors="replace", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return None


def _syntax_note(result: str) -> str:
    m = re.search(r"\[SYNTAX ERROR\][^\n]*", result)
    return m.group(0) if m else ""


def notice_cell(text: str, s: Style, w: int, kind: str = "warn") -> list[str]:
    glyph = {"warn": s.g("warn"), "info": s.g("info"), "err": s.g("cross"),
             "ok": s.g("tick")}.get(kind, s.g("info"))
    role = {"warn": "warn", "info": "info", "err": "err", "ok": "ok"}.get(kind, "info")
    lines = wrap_text(text, w - 2, indent="  ", first_indent="")
    return [(s(role, glyph) + " " if i == 0 else "") + s(role if kind != "info" else "dim", ln)
            for i, ln in enumerate(lines)]


# Teams after connecting: a team card, its own colour bar, the task in the
# header
_TEAM_LOOK = {   # Kind: (role of the bar, the mark)
    "started": ("accent", "▸"), "branch": ("faint", "↳"), "check": ("ok", "✓"),
    "collision": ("warn", "!"), "blocked": ("warn", "×"), "due": ("warn", "◷"),
    "done": ("ok", "✓"), "commit": ("faint", "●"), "push": ("ok", "↑"),
    "created": ("accent", "+"), "decision": ("accent", "◆"), "off": ("faint", "○"),
    "warn": ("warn", "!"), "note": ("faint", "·"),
}


def team_cell(d: dict, s: Style, w: int) -> list[str]:
    kind = str(d.get("kind") or "note")
    role, mark = _TEAM_LOOK.get(kind, ("faint", "·"))
    bar = s(role, "▌")
    task = d.get("task")
    head = bar + s(role, " TEAM", bold=True)
    if task:
        head += s("faint", " · ") + s("strong", f"#{task}") + (
            s("dim", " " + str(d.get("title") or "")[:max(10, w - 30)]) if d.get("title") else "")
    text = str(d.get("text") or "")
    if task and text.startswith(f"#{task} "):      # The header names the task already
        text = text[len(f"#{task} "):]
    if kind == "queued" or d.get("queued"):
        text += "" if "queued" in text else " (queued — the hub is off)"
    body = wrap_text(text, w - 6, indent="", first_indent="")
    out = [head] + [bar + " " + s(role if kind in ("collision", "blocked", "due", "warn") else "text",
                                  (mark + " " if i == 0 else "  ") + ln)
                    for i, ln in enumerate(body)]
    for ln in d.get("lines") or []:
        for j, part in enumerate(wrap_text(str(ln), w - 8, indent="", first_indent="")):
            out.append(bar + "   " + s("dim", ("• " if j == 0 else "  ") + part))
    if d.get("branch"):
        out.append(bar + "   " + s("faint", "branch: ") + s("dim", str(d["branch"])))
    if d.get("planned"):
        files = ", ".join(map(str, d["planned"]))
        for j, part in enumerate(wrap_text(files, w - 20, indent="", first_indent="")):
            out.append(bar + "   " + s("faint", "planned files: " if j == 0 else "               ")
                       + s("dim", part))
    return out


def footer_cell(parts: list[str], s: Style, ok: bool = True) -> list[str]:
    mark = s("ok", s.g("tick")) if ok else s("err", s.g("cross"))
    return [mark + " " + s("faint", f" {s.g('dot')} ").join(parts), ""]


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_secs(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s" if sec >= 10 else f"{sec:.1f}s"
    m, ss = divmod(int(sec), 60)
    return f"{m}m {ss:02d}s"

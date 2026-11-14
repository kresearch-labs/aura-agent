"""setup_ui.py — how setup looks and how it asks.

Now:

  - each step has a coloured header with "Step X of Y" and room around it;
  - icons with colour: ✓ worked · ⚠ needs you · ✗ failed · dim for details;
  - technical details are hidden (AURA_SETUP_DETAILS=1 shows them);
  - one answer: ↑↓ and Enter; "recommended" only on settings new to most people;
  - several answers: checkboxes, Space ticks, Enter is done;
  - typing only for names, keys and addresses;
  - an answered question folds into one line, so the screen stays calm.

Without a real terminal (a script, a pipe, tests) it falls back to typed
numbers, so nothing that drives setup breaks.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import ui


@dataclass
class Opt:
    value: str
    label: str
    hint: str = ""
    group: str = ""          # A heading the option sits under (shown once)


RECOMMENDED = "★ recommended"
MAX_ROWS = 12                # Options shown at once; more scroll (and filter)


# ------------------------------------------------------------- look --
def dim(s: str) -> str:
    """Secondary text: gray, so it steps back from the headers and choices."""
    return ui.paint(s, 244)


def _on() -> bool:
    try:
        ui.enable()
    except Exception:                                # noqa: BLE001
        pass
    return ui._ENABLED


def details() -> bool:
    return os.environ.get("AURA_SETUP_DETAILS", "") not in ("", "0")


def _width() -> int:
    try:
        return max(40, min(os.get_terminal_size().columns, 100))
    except OSError:
        return 80


def header(i: int, n: int, title: str, later: str = "") -> None:
    _on()
    w = _width() - 2
    head = f"━━ Step {i} of {n} · {title} "
    print()
    print()
    print("  " + ui.title(head + "━" * max(3, w - len(head) - 2)))
    if later:
        print("  " + dim(f"change it any time: {later}"))
    print()


def banner(title: str, sub: str = "") -> None:
    _on()
    w = _width() - 2
    head = f"━━ {title} "
    print()
    print("  " + ui.title(head + "━" * max(3, w - len(head) - 2)))
    if sub:
        print("  " + dim(sub))
    print()


ICON = {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "·", "detail": "·", "off": "·"}


def line(text: str, state: str = "info") -> None:
    """One finding. 'detail' lines show only with AURA_SETUP_DETAILS=1."""
    if state == "detail" and not details():
        return
    _on()
    icon = ICON.get(state, "·")
    if state == "ok":
        s = ui.ok(icon) + " " + text
    elif state == "warn":
        s = ui.paint(icon, ui.C["warn"]) + " " + text
    elif state == "fail":
        s = ui.err(icon) + " " + text
    else:
        s = dim(icon + " " + text)
    print("  " + s)


def note(text: str) -> None:
    print("  " + dim(text))


# ----------------------------------------------------------- asking --
def interactive() -> bool:
    if os.environ.get("AURA_SETUP_PLAIN"):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _fold(question: str, answer: str) -> None:
    """The answered question as one calm line."""
    print("  " + ui.bold(question) + "  " + ui.accent("› " + answer))


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


class _Picker:
    """Inline picker: draws below the cursor, redraws in place."""

    def __init__(self, question: str, opts: list[Opt], default: str = "",
                 multi: bool = False, checked: set | None = None, help_: str = "",
                 rec: str = ""):
        self.q, self.opts, self.multi = question, opts, multi
        self.default = default
        self.rec = rec       # Marked "recommended" — only on settings new to most people
        self.checked = set(checked or ())
        self.help = help_
        self.filter = ""
        self.cur = next((i for i, o in enumerate(opts) if o.value == default), 0)
        self.top = 0
        self.drawn = 0

    def shown(self) -> list[int]:
        if not self.filter:
            return list(range(len(self.opts)))
        words = self.filter.lower().split()
        return [i for i, o in enumerate(self.opts)
                if all(w in (o.label + " " + o.value).lower() for w in words)]

    def draw(self, out) -> None:
        w = _width()
        rows = []
        rows.append("  " + ui.bold(self.q))
        if self.help:
            rows.append("  " + dim(self.help))
        rows.append("")
        idx = self.shown()
        if self.cur not in idx and idx:
            self.cur = idx[0]
        pos = idx.index(self.cur) if self.cur in idx else 0
        if pos < self.top:
            self.top = pos
        if pos >= self.top + MAX_ROWS:
            self.top = pos - MAX_ROWS + 1
        group = None
        for j in idx[self.top:self.top + MAX_ROWS]:
            o = self.opts[j]
            if o.group and o.group != group:
                group = o.group
                rows.append("    " + dim(o.group))
            here = j == self.cur
            if self.multi:
                box = "☑" if o.value in self.checked else "☐"
                mark = ui.ok(box) if o.value in self.checked else dim(box)
            else:
                mark = ui.accent("●") if here else dim("○")
            arrow = ui.accent("❯") if here else " "
            label = ui.bold(o.label) if here else o.label
            tail = []
            if self.rec and o.value == self.rec and not self.multi:
                tail.append(ui.paint(RECOMMENDED, ui.C["warn"]))
            if o.hint:
                tail.append(dim(_cut(o.hint, max(10, w - len(o.label) - 24))))
            rows.append(f"  {arrow} {mark} {label}" + ("   " + "  ".join(tail) if tail else ""))
        more = len(idx) - (self.top + MAX_ROWS)
        if self.top > 0 or more > 0:
            rows.append("    " + dim(f"… {len(idx)} in all — keep pressing ↓, or type to search"))
        if not idx:
            rows.append("    " + dim(f"nothing matches '{self.filter}' — Backspace"))
        rows.append("")
        keys = ("↑↓ move · Space tick · Enter done" if self.multi
                else "↑↓ move · Enter choose")
        if self.filter:
            keys += f" · search: {self.filter}"
        rows.append("  " + dim(keys))
        if self.drawn:
            out.write(f"\x1b[{self.drawn}F\x1b[J")
        out.write("\n".join(rows) + "\n")
        out.flush()
        self.drawn = len(rows)

    def clear(self, out) -> None:
        if self.drawn:
            out.write(f"\x1b[{self.drawn}F\x1b[J")
            out.flush()
            self.drawn = 0

    def run(self):
        from .tui.term import Terminal
        term = Terminal()
        term.start()
        out = term
        out.write("\x1b[?25l")                         # Hide the cursor while picking
        try:
            self.draw(out)
            while True:
                k = term.keys.get()
                kid = k.id
                idx = self.shown()
                if kid == "ctrl+c":
                    raise KeyboardInterrupt
                if kid in ("up", "shift+tab") and idx:
                    p = idx.index(self.cur) if self.cur in idx else 0
                    self.cur = idx[(p - 1) % len(idx)]
                elif kid in ("down", "tab") and idx:
                    p = idx.index(self.cur) if self.cur in idx else 0
                    self.cur = idx[(p + 1) % len(idx)]
                elif kid in ("home", "pageup") and idx:
                    self.cur = idx[0]
                elif kid in ("end", "pagedown") and idx:
                    self.cur = idx[-1]
                elif kid == "escape":
                    if self.filter:
                        self.filter = ""
                    else:
                        self.clear(out)
                        return None
                elif kid == "backspace":
                    self.filter = self.filter[:-1]
                elif self.multi and (kid == "space" or (kid == "char" and k.text == " ")):
                    v = self.opts[self.cur].value
                    self.checked ^= {v}
                elif kid == "enter":
                    if not idx:
                        continue
                    self.clear(out)
                    if self.multi:
                        return [o.value for o in self.opts if o.value in self.checked]
                    return self.opts[self.cur].value
                elif kid == "char" and k.text and k.text.isprintable():
                    if not self.multi and k.text.isdigit() and not self.filter:
                        n = int(k.text)
                        if 1 <= n <= len(idx):
                            self.cur = idx[n - 1]
                    else:
                        self.filter += k.text
                elif kid == "paste" and k.text:
                    self.filter += k.text.strip()
                else:
                    continue
                self.draw(out)
        finally:
            out.write("\x1b[?25h")
            out.flush()
            term.stop()
            r = getattr(term, "_reader", None)
            if r is not None:
                r.join(0.5)


def _plain_one(question: str, opts: list[Opt], default: str, rec: str = "") -> str:
    print("  " + question)
    for i, o in enumerate(opts, 1):
        mark = f"  ({RECOMMENDED})" if rec and o.value == rec else ""
        print(f"    {i}. {o.label}" + (f" — {o.hint}" if o.hint else "") + mark)
    for _ in range(3):
        try:
            raw = input(f"  choose 1-{len(opts)} [Enter = {_label(opts, default)}]: ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            return opts[int(raw) - 1].value
        for o in opts:
            if raw.lower() in (o.value.lower(), o.label.lower()):
                return o.value
        print(f"    '{raw}' isn't one of the choices")
    return default


def _plain_many(question: str, opts: list[Opt], checked: set) -> list[str]:
    print("  " + question)
    for i, o in enumerate(opts, 1):
        box = "[x]" if o.value in checked else "[ ]"
        print(f"    {i}. {box} {o.label}" + (f" — {o.hint}" if o.hint else ""))
    try:
        raw = input("  numbers to switch, separated by spaces (Enter = as shown): ").strip()
    except EOFError:
        raw = ""
    out = set(checked)
    for w in raw.replace(",", " ").split():
        if w.isdigit() and 1 <= int(w) <= len(opts):
            out ^= {opts[int(w) - 1].value}
    return [o.value for o in opts if o.value in out]


def _label(opts: list[Opt], value: str) -> str:
    return next((o.label for o in opts if o.value == value), value)


def pick_one(question: str, opts: list[Opt], default: str = "", help_: str = "",
             rec: str = "") -> str | None:
    """One answer. Returns the chosen value (the default on a bare Enter); None if
    the user pressed Esc. The default is only where the cursor starts; `rec` is
    marked "recommended" — given only for settings most people don't know.
    """
    if not opts:
        return None
    if default not in [o.value for o in opts]:
        default = opts[0].value
    if not interactive():
        return _plain_one(question, opts, default, rec)
    v = _Picker(question, opts, default, help_=help_, rec=rec).run()
    if v is None:
        _fold(question, "skipped (Esc)")
        return None
    _fold(question, _label(opts, v))
    return v


def pick_many(question: str, opts: list[Opt], checked: set | None = None,
              help_: str = "") -> list[str]:
    """Checkboxes. Returns the ticked values (as they were on Esc)."""
    checked = set(checked or ())
    if not opts:
        return []
    if not interactive():
        return _plain_many(question, opts, checked)
    v = _Picker(question, opts, multi=True, checked=checked, help_=help_).run()
    if v is None:
        v = [o.value for o in opts if o.value in checked]
    names = [_label(opts, x) for x in v]
    _fold(question, ", ".join(names) if names else "none")
    return v


def yes_no(question: str, default: bool = True, yes: str = "Yes", no: str = "No",
           yes_hint: str = "", no_hint: str = "", rec: bool = False) -> bool:
    v = pick_one(question, [Opt("yes", yes, yes_hint), Opt("no", no, no_hint)],
                 "yes" if default else "no", rec=("yes" if default else "no") if rec else "")
    return v == "yes" if v is not None else default


def ask_text(question: str, default: str = "", help_: str = "") -> str:
    _on()
    print("  " + ui.bold(question))
    if help_:
        print("  " + dim(help_))
    suffix = f" [{default}]" if default else ""
    try:
        raw = input("  " + ui.accent("›") + suffix + " ").strip()
    except EOFError:
        raw = ""
    return raw or default


# ------------------------------------------------------ when it fails --
# The rule now: setup never crashes and never dead-ends — a failure says why in
# plain words and offers what can be done next.
TRY, CHANGE, SKIP = "try", "change", "skip"


def recover(problem: str, extra: list | None = None, try_label: str = "Try again",
            change_label: str = "Change my answer", skip_label: str = "Skip for now",
            skip_hint: str = "this part stays not ready — run aura setup again any time",
            default: str = "") -> str:
    """A failure: the reason, then what now. Returns the chosen value —
    TRY, CHANGE, SKIP, or one of the extra options' values."""
    line(problem, "fail")
    opts = list(extra or [])
    if try_label:
        opts.append(Opt(TRY, try_label, ""))
    if change_label:
        opts.append(Opt(CHANGE, change_label, ""))
    opts.append(Opt(SKIP, skip_label, skip_hint))
    got = pick_one("What now?", opts, default or opts[0].value)
    return got or SKIP


def run_command(what: str, cmd, shell: bool = False, timeout: float = 900,
                env: dict | None = None) -> bool:
    """'Do it for me': show exactly what runs, run it with its own output on
    the screen (a sign-in can open the browser, sudo can ask), and say how it
    ended. The caller has already had the user's yes."""
    import subprocess
    shown = cmd if isinstance(cmd, str) else " ".join(cmd)
    print()
    note(f"{what}:")
    print("    " + ui.accent(shown))
    print()
    try:
        r = subprocess.run(cmd, shell=shell, timeout=timeout,
                           env={**os.environ, **(env or {})})
        ok = r.returncode == 0
    except FileNotFoundError:
        line("that program isn't on this computer", "fail")
        return False
    except subprocess.TimeoutExpired:
        line(f"it didn't finish in {int(timeout // 60)} minutes — stopped", "fail")
        return False
    except KeyboardInterrupt:
        line("stopped", "warn")
        return False
    except OSError as e:
        line(f"it couldn't start: {e}", "fail")
        return False
    print()
    line(f"{what} — done" if ok else f"{what} — it ended with an error (code {r.returncode})",
         "ok" if ok else "fail")
    return ok


def plain_error(e: BaseException) -> str:
    """An exception as one plain line (no traceback, no class soup)."""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return f"the site answered with an error (HTTP {e.code})"
    if isinstance(e, (urllib.error.URLError, socket.gaierror, ConnectionError)):
        return "couldn't reach the site — it may be down, or this computer is offline"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "the site took too long to answer"
    s = " ".join(str(e).split())
    return s[:160] or e.__class__.__name__

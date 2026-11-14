"""board.py — the setup board.

Every setup step on one screen with its state (✓ proven /! needs you / – not
set), opened in any order. A step turns ✓ only after its real check on this
machine (setup_steps.py runs it and writes the setup receipt). Wide terminals:
the list on the left, the open step on the right; narrow ones: one at a time.
Slow checks (the sandbox test, opening a browser) run in the background and the
board says so.

Keys: ↑↓ step · Enter open · Space/←→ change a choice · Enter on "save and
check" saves · r re-check · u undo the last change · Esc back / close.
"""
from __future__ import annotations

import threading

from .text import cut, pad, width

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Board:
    def __init__(self, app):
        from .. import setup_steps as SS
        self.SS, self.app = SS, app
        self.steps = list(SS.STEPS)
        self.i = 0                      # The step under the cursor
        self.mode = "list"              # List | step
        self.ctx: dict | None = None
        self.looks: dict = {}           # Step id -> [Found]
        self.asks: dict = {}            # Step id -> [Ask]
        self.ans: dict = {}             # Step id -> {key: value}
        self.j = 0                      # The row inside an open step
        self.edit = None                # An Editor for a typed answer
        self.busy = ""
        self.note = ""
        self.closed = False
        self.after = None               # A chat command to run after closing
        self._tick = 0
        self.receipt = SS.receipt_state()
        self._bg(self._load, "looking at this machine")

    # ------------------------------------------------------------ work --
    def _bg(self, fn, says: str) -> None:
        if self.busy:
            return
        self.busy = says

        def run():
            try:
                fn()
            except Exception as e:                   # noqa: BLE001
                self.note = f"that didn't work: {str(e)[:160]}"
            finally:
                self.receipt = self.SS.receipt_state()
                self.busy = ""
        threading.Thread(target=run, daemon=True).start()

    def _load(self) -> None:
        self.ctx = self.SS.context()

    def _open_step(self) -> None:
        st = self.steps[self.i]
        if st.run is not None:          # The model has its own screens
            self.after = "/model"
            self.closed = True
            return
        self.mode, self.j, self.note = "step", 0, ""
        if st.id not in self.looks:
            def load():
                while self.ctx is None:
                    import time
                    time.sleep(0.05)
                self.looks[st.id] = st.look(self.ctx)
                self._fill_asks(st)
            self._bg(load, f"looking at {st.title.lower()}")

    def _expand(self, asks: list) -> list:
        """A checkbox question becomes one yes/no row per choice here."""
        out = []
        for a in asks:
            if not getattr(a, "multi", ""):
                out.append(a)
                continue
            on = set(str(a.default or "").replace(",", " ").split())
            off = a.flag_off if a.multi == "flags" else "off"
            for c in a.choices:
                v = c[0]
                label = c[1] if len(c) >= 3 else c[0]
                hint = (c[2] if len(c) >= 3 else c[1]) or ""
                key = v if a.multi == "flags" else f"{a.key}::{v}"
                out.append(self.SS.Ask(key, label, [("on", "yes", hint), (off, "no", "")],
                                       default="on" if v in on else off, when=a.when))
        return out

    def _fill_asks(self, st) -> None:
        self.asks[st.id] = self._expand(st.asks(self.ctx))
        cur = self.ans.setdefault(st.id, {})
        for a in self.asks[st.id]:
            cur.setdefault(a.key + "|" + a.when, a.default)

    def _visible(self, st) -> list:
        """The questions that apply now (a follow-up shows only after the
        answer it follows)."""
        cur = self.ans.get(st.id, {})
        out = []
        for a in self.asks.get(st.id, []):
            if a.when:
                wk, _, wv = a.when.partition("=")
                if str(self._val(cur, wk)) != wv:
                    continue
            out.append(a)
        return out

    @staticmethod
    def _val(cur: dict, key: str):
        for k, v in cur.items():
            if k.split("|", 1)[0] == key:
                return v
        return None

    def _save(self) -> None:
        st = self.steps[self.i]
        cur = self.ans.get(st.id, {})
        answers = {}
        for a in self._visible(st):
            v = cur.get(a.key + "|" + a.when)
            if v not in (None, ""):
                answers[a.key] = v
        for k in [k for k in answers if "::" in k]:     # A checkbox list, put back together
            base, _, name = k.partition("::")
            if answers.pop(k) == "on":
                answers[base] = (answers.get(base, "") + " " + name).strip()

        def run():
            changes = st.save(self.ctx, answers)
            proof = st.check(self.ctx)
            self.SS.record(st, changes, proof)
            self.looks.pop(st.id, None)               # Look again next time
            self.note = (("✓ " if proof.ok else "! ") + proof.evidence
                         + (f" — fix: {proof.fix.splitlines()[0]}" if proof.fix and not proof.ok
                            else ""))
            self.mode = "list"
        self._bg(run, f"saving and checking {st.title.lower()}")

    def _recheck(self) -> None:
        st = self.steps[self.i]
        if st.run is not None and self.ctx is None:
            return

        def run():
            while self.ctx is None:
                import time
                time.sleep(0.05)
            proof = st.check(self.ctx)
            self.SS.record(st, [], proof)
            self.note = ("✓ " if proof.ok else "! ") + proof.evidence
        self._bg(run, f"checking {st.title.lower()}")

    # ------------------------------------------------------------ keys --
    def key(self, k) -> None:
        if self.edit is not None:
            self._edit_key(k)
            return
        if k.id in ("ctrl+c",):
            self.closed = True
            return
        if self.mode == "list":
            self._list_key(k)
        else:
            self._step_key(k)

    def _list_key(self, k) -> None:
        ch = k.text if k.id == "char" else ""
        if k.id == "up":
            self.i = (self.i - 1) % len(self.steps)
        elif k.id in ("down", "tab"):
            self.i = (self.i + 1) % len(self.steps)
        elif k.id == "enter" or k.id == "right":
            self._open_step()
        elif k.id == "escape" or ch == "q":
            self.closed = True
        elif ch == "r":
            self._recheck()
        elif ch == "u":
            from ..options import undo
            self.note = undo()
        elif ch.isdigit():
            hit = next((n for n, s in enumerate(self.steps) if str(s.num) == ch), None)
            if hit is not None:
                self.i = hit

    def _step_key(self, k) -> None:
        st = self.steps[self.i]
        vis = self._visible(st)
        rows = len(vis) + 1                          # "save and check"
        ch = k.text if k.id == "char" else ""
        if self.busy and st.id not in self.asks:
            if k.id == "escape":
                self.mode = "list"
            return
        if k.id == "escape" or k.id == "left" and not vis:
            self.mode, self.note = "list", ""
            return
        if k.id == "up":
            self.j = (self.j - 1) % rows
        elif k.id in ("down", "tab"):
            self.j = (self.j + 1) % rows
        elif self.j == len(vis):
            if k.id == "enter":
                self._save()
        else:
            a = vis[self.j]
            cur = self.ans.setdefault(st.id, {})
            slot = a.key + "|" + a.when
            if not a.choices:
                if k.id == "enter" or ch:
                    from .editor import Editor
                    self.edit = (Editor(), a, slot)
                    if ch:                       # Typing starts the answer
                        from .app import _edit
                        _edit(self.edit[0], k)
                return
            vals = [c[0] for c in a.choices]
            now = cur.get(slot, a.default)
            n = vals.index(now) if now in vals else 0
            if k.id in ("right",) or ch == " ":
                cur[slot] = vals[(n + 1) % len(vals)]
            elif k.id == "left":
                cur[slot] = vals[(n - 1) % len(vals)]
            elif ch.isdigit() and 1 <= int(ch) <= len(vals):
                cur[slot] = vals[int(ch) - 1]
            elif k.id == "enter":
                self.j = min(rows - 1, self.j + 1)
            if (a.key == "tools.mode" and cur.get(slot) == "choose" and self.ctx
                    and not self.ctx.get("_tools_choose")):
                self.ctx["_tools_choose"] = True
                self._fill_asks(st)

    def _edit_key(self, k) -> None:
        ed, a, slot = self.edit
        if k.id == "enter":
            self.ans.setdefault(self.steps[self.i].id, {})[slot] = ed.text.strip()
            self.edit = None
            self.j += 1
        elif k.id in ("escape", "ctrl+c"):
            self.edit = None
        else:
            from .app import _edit
            _edit(ed, k)

    # ----------------------------------------------------------- lines --
    def _state(self, st) -> tuple[str, str, str]:
        """(mark, role, words) from the receipt."""
        r = self.receipt.get(st.id)
        if not r:
            return "–", "faint", "not set up"
        return ("✓", "ok", r.get("evidence", "")) if r.get("ok") else \
            ("!", "warn", (r.get("missing") and f"needs {r['missing']}") or r.get("evidence", ""))

    def lines(self, s, w: int, h: int) -> list[str]:
        self._tick += 1
        n = len(self.steps)
        marks = [self._state(st)[0] for st in self.steps]   # The record: one line per check
        ready, need, unset = marks.count("✓"), marks.count("!"), marks.count("–")
        busy = (s("accent", " " + _SPIN[self._tick % len(_SPIN)] + " " + self.busy + "…")
                if self.busy else "")
        out = ["  " + s("heading", "setup", bold=True)
               + s("faint", f"   {ready} of {n} ready · {need} need you · {unset} not set")
               + busy]
        out.append("")
        wide = w >= 96
        left_w = 44 if wide else w - 2
        left = self._list_lines(s, left_w, wide) if (wide or self.mode == "list") else []
        right = self._detail_lines(s, (w - left_w - 3) if wide else w - 2) \
            if (wide or self.mode == "step") else []
        if wide:
            for k in range(max(len(left), len(right))):
                lv = left[k] if k < len(left) else ""
                rv = right[k] if k < len(right) else ""
                out.append(pad(lv, left_w) + " " + s("faint", s.g("box_v")) + " " + rv)
        else:
            out += left or right
        if self.note:
            out.append("")
            out.append("  " + cut(self.note, w - 4))
        out.append("")
        keys = ("type, Enter keeps it · Esc cancels" if self.edit is not None else
                "↑↓ step · Enter open · r re-check · u undo · 1-9 jump · Esc close"
                if self.mode == "list" else
                "↑↓ question · Space/←→ change · Enter on 'save and check' saves · Esc back")
        out.append("  " + s("faint", keys))
        return out[:max(8, h - 1)]

    def _list_lines(self, s, w: int, wide: bool) -> list[str]:
        out = []
        for k, st in enumerate(self.steps):
            mark, role, words = self._state(st)
            label = f"{st.num:>2} {st.title}"
            if k == self.i:
                row = s("sel_fg", s.g("arrow") + " " + mark + " " + pad(cut(label, w - 6), w - 6),
                        bold=True, bg="sel_bg")
            else:
                row = "  " + s(role, mark) + " " + cut(label, w - 6)
            out.append(row)
            if not wide:
                out.append("      " + s("faint", cut(words, w - 8)))
        return out

    def _detail_lines(self, s, w: int) -> list[str]:
        st = self.steps[self.i]
        mark, role, words = self._state(st)
        out = [s("strong", f"{st.num} · {st.title}", bold=True)]
        if self.mode == "list":
            out.append(s(role, mark) + " " + cut(words, w - 3))
            out.append(s("faint", "Enter opens it · change it later: " + st.later))
            return out
        if st.id not in self.asks:
            out.append(s("faint", "looking…"))
            return out
        for f in self.looks.get(st.id, []):
            m = {"ok": ("✓", "ok"), "warn": ("!", "warn"), "off": ("–", "faint")}.get(
                f.state, ("·", "dim"))
            for n, ln in enumerate(_wrap(f.text, w - 3)):
                out.append((s(m[1], m[0]) if n == 0 else " ") + " " + s("dim", ln))
        cur = self.ans.get(st.id, {})
        vis = self._visible(st)
        if vis:
            out.append("")
        for n, a in enumerate(vis):
            here = n == self.j and self.edit is None
            slot = a.key + "|" + a.when
            q = cut(a.prompt, w - 2)
            out.append(s("sel_fg", s.g("arrow") + " " + q, bold=True, bg="sel_bg") if here
                       else "  " + s("text", q))
            if a.choices:
                val = cur.get(slot, a.default)
                row = "    "
                for c in a.choices:
                    tag = f"({'•' if c[0] == val else ' '}) {c[1] if len(c) >= 3 else c[0]}"
                    row += (s("accent", tag, bold=True) if c[0] == val else s("dim", tag)) + "  "
                out.append(cut(row, w))
                meaning = next(((c[2] if len(c) >= 3 else c[1]) for c in a.choices
                                if c[0] == val), "")
                if meaning:
                    for ln in _wrap(meaning, w - 6):
                        out.append("      " + s("faint", ln))
                if here and a.why:
                    for ln in _wrap(("suggested: " + a.default + " — " + a.why), w - 6):
                        out.append("      " + s("info", ln))
            else:
                if self.edit is not None and self.edit[2] == slot:
                    ed = self.edit[0]
                    shown = "•" * len(ed.text) if a.secret else ed.text
                    out.append("    " + s("accent", "> ") + shown + s("accent", "▏"))
                else:
                    v = str(cur.get(slot) or "")
                    out.append("    " + s("dim", ("•" * min(len(v), 12)) if (a.secret and v)
                                          else (v or "(Enter to type)")))
        save_here = self.j == len(vis) and self.edit is None
        out.append("")
        out.append(s("sel_fg", s.g("arrow") + " save and check", bold=True, bg="sel_bg")
                   if save_here else "  " + s("accent", "save and check"))
        out.append(s("faint", "change it later: " + st.later))
        return out


def _wrap(text: str, w: int) -> list[str]:
    words, out, line = str(text).split(), [], ""
    for wd in words:
        if line and width(line) + 1 + width(wd) > max(10, w):
            out.append(line)
            line = wd
        else:
            line = f"{line} {wd}".strip()
    if line:
        out.append(line)
    return out or [""]

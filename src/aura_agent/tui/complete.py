"""complete.py — suggestions while typing: / commands, @ files, and the
arguments of a few commands. Fuzzy: "/cmp" finds /compact, "@srvcli"
finds src/aura_agent/cli.py. Files are listed in the background and
cached; big folders (.git, node_modules, venvs, build output) are skipped.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
             "venv", "env", ".env", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             ".tox", "dist", "build", "target", ".next", ".nuxt", ".idea",
             ".vscode", ".gradle", ".terraform", "coverage", ".cache"}
MAX_FILES = 30000


@dataclass
class Item:
    insert: str          # Text that replaces the token
    label: str           # What the list shows
    hint: str = ""       # Dim text on the right
    kind: str = "cmd"    # Cmd | file | dir | arg


def fuzzy_score(query: str, text: str) -> int | None:
    """Higher is better; None = no match. Subsequence match with bonuses
    for a prefix, word starts and consecutive letters."""
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    if t.startswith(q):
        return 1000 - len(t)
    if q in t:
        return 700 - t.index(q) - len(t) // 4
    score, ti, prev = 0, 0, -2
    for ch in q:
        j = t.find(ch, ti)
        if j < 0:
            return None
        bonus = 10
        if j == prev + 1:
            bonus += 15
        if j == 0 or t[j - 1] in "/._- ":
            bonus += 20
        score += bonus - min(j - ti, 10)
        prev, ti = j, j + 1
    return score - len(t) // 3


def command_items(prefix: str, eng=None) -> list[Item]:
    from ..command_registry import COMMANDS
    items: list[tuple[int, Item]] = []
    q = prefix.lstrip("/")
    for c in COMMANDS:
        names = [c.name] + list(c.aliases)
        best = None
        for n in names:
            sc = fuzzy_score(q, n.lstrip("/"))
            if sc is not None and (best is None or sc > best):
                best = sc
        if best is not None:
            items.append((best, Item(c.name + " ", c.name, c.desc, "cmd")))
    if eng is not None:
        try:
            from ..custom_commands import discover
            for n, cc in discover(eng).items():
                sc = fuzzy_score(q, n)
                if sc is not None:
                    items.append((sc, Item("/" + n + " ", "/" + n,
                                           cc.description or "your command", "cmd")))
        except Exception:      # noqa: BLE001
            pass
    items.sort(key=lambda x: -x[0])
    return [i for _, i in items]


_ARGS = {
    "/assist": [("L1", "asks before every change"), ("L2", "asks before risky actions"),
                ("L3", "never asks")],
    "/mode": [("act", "build — full tools"), ("plan", "read and research only")],
    "/budget": [("preset", "pick a preset"), ("set", "change one limit")],
    "/memory": [("forget", "remove a remembered fact")],
    "/receipt": [("verify", "check the receipt chain")],
    "/scope": [("add", "allow a website / host"), ("remove", "remove one")],
    "/rules": [("add", "add a rule"), ("rm", "remove a rule")],
    "/resume": [("latest", "the newest chat"), ("search", "every message of every chat"),
                ("info", "what a chat did, what it used"), ("delete", "into the trash (restorable)"),
                ("restore", "out of the trash"), ("trash", "deleted chats · trash empty"),
                ("pin", "keep at the top"), ("unpin", ""), ("archive", "hide, don't delete"),
                ("unarchive", ""), ("archived", "the hidden ones"), ("rename", "a new title, same id"),
                ("export", "md or jsonl, keys scrubbed"), ("sort", "used · name · full · made")],
    "/model": [("ctx", "set or find the context size"), ("use", "switch model setup")],
    "/research": [("basic", "only the listed sources"), ("normal", "anything on the web, kept small"),
                  ("deep", "plan first, two sources each")],
    "/discuss": [("on", "talk only — no changes"), ("off", "back to work")],
    "/findaway": [("on", "offer a way to what's missing (asks first)"), ("off", "say what's missing")],
    "/team": [("status", "the hub and your connection"), ("connect", "a hub address · your name"),
              ("board", "your tasks · beside the chat in fullscreen"), ("link", "match a request to a task"),
              ("start", "this chat's task started"), ("done", "this chat's task done"),
              ("decision", "record a decision"), ("read", "a task: blocked, due, comments"),
              ("queue", "updates waiting for the hub"), ("sync", "send them now"),
              ("hub", "setup · start · stop · status"), ("chat", "the people chat page on/off"),
              ("wire", "Aura wires any app as the hub"), ("wiring", "the wiring in use")],
    "/think": [("auto", "the model's own default"), ("off", "no thinking — fastest"),
               ("low", "a little thinking"), ("medium", "more thinking"),
               ("high", "the most thinking — slowest")],
    "/proof": [("on", "re-check and test before answering"), ("off", "answer right away")],
    "/plan": [("on", "keep a step-by-step plan for this chat"), ("off", "no plan")],
    "/tui": [("fullscreen", "the whole window, sidebar"), ("inline", "your terminal's scrollback")],
    "/web": [("open", "the no-account sources on/off"), ("all", "any public page on/off")],
    "/cron": [("add", "schedule a job"), ("pause", "stop one"), ("resume", "restart one"),
              ("remove", "delete one"), ("run", "run one here now")],
    "/mcp": [("popular", "well-known servers"), ("search", "the official registry"),
             ("add", "add a server (shown, then asked)"), ("login", "sign in in the browser"),
             ("logout", "forget a sign-in")],
    "/add-dir": [("remove", "take a folder off")],
    "/tools": [("on", "send a tool group"), ("off", "stop sending it"), ("list", "every tool")],
}


def arg_items(cmd: str, partial: str) -> list[Item]:
    opts = list(_ARGS.get(cmd, []))
    if cmd == "/theme":
        from .style import THEMES
        opts = [(n, "") for n in THEMES]
    out = []
    for val, hint in opts:
        sc = fuzzy_score(partial, val)
        if sc is not None:
            out.append((sc, Item(val + " ", val, hint, "arg")))
    out.sort(key=lambda x: -x[0])
    return [i for _, i in out]


class FileIndex:
    """Files under the project, listed in a background thread."""

    def __init__(self, root):
        self.root = str(root)
        self.files: list[str] = []
        self.dirs: list[str] = []
        self._at = 0.0
        self._lock = threading.Lock()
        self._busy = False

    def refresh(self, force: bool = False) -> None:
        if self._busy or (not force and time.monotonic() - self._at < 20):
            return
        self._busy = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        files, dirs = [], []
        try:
            for base, dnames, fnames in os.walk(self.root):
                dnames[:] = sorted(d for d in dnames if d not in SKIP_DIRS
                                   and not d.endswith(".egg-info"))
                rel = os.path.relpath(base, self.root)
                rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
                if rel:
                    dirs.append(rel)
                for f in sorted(fnames):
                    files.append(rel + f)
                    if len(files) >= MAX_FILES:
                        raise StopIteration
        except StopIteration:
            pass
        except OSError:
            pass
        with self._lock:
            self.files, self.dirs = files, dirs
            self._at = time.monotonic()
        self._busy = False

    def match(self, query: str, limit: int = 40) -> list[Item]:
        with self._lock:
            files, dirs = list(self.files), list(self.dirs)
        scored = []
        for p in files:
            base = p.rsplit("/", 1)[-1]
            sc = fuzzy_score(query, base)
            sc2 = fuzzy_score(query, p)
            best = max([x for x in (None if sc is None else sc + 30, sc2) if x is not None],
                       default=None)
            if best is not None:
                scored.append((best, Item("@" + p + " ", p, "", "file")))
        for d in dirs:
            sc = fuzzy_score(query, d)
            if sc is not None:
                scored.append((sc - 5, Item("@" + d, d, "folder", "dir")))
        scored.sort(key=lambda x: (-x[0], len(x[1].label)))
        return [i for _, i in scored[:limit]]

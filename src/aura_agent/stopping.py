"""One stop for every tool.

The chat screen (Esc / Ctrl+C / /kill), the IDE link (ACP) and the web UI set
`sandbox_os.STOP` when the user stops a task, and commands already end on it
(`sandbox_os.run_tree`). Everything else that can take a while — walking
folders, ripgrep, reading a web page, training a persona — checks it through
this module, so a stop ends any tool within a moment, on every OS, keeping
what it had found so far where that makes sense.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator

from .mediator import Violation
from .sandbox_os import STOP, stop_requested

STOPPED_TEXT = "stopped — the user stopped the task"
FOR_MESSAGE_TEXT = ("stopped — the user sent a new message to read now (the step was cut "
                    "short for it; this is not a failure)")


def stopped_text() -> str:
    """What a stopped tool says: the task was stopped, or the step was cut
    short for the user's new message."""
    from .sandbox_os import STOP_WHY
    return FOR_MESSAGE_TEXT if STOP_WHY.get("why") == "message" else STOPPED_TEXT

# What the running tool is doing right now ("walked 4,210 folders", a
# command's latest output line), shown live under the chat's activity line so
# a long step never looks frozen. One slot: the tool the user is waiting on.
_PROGRESS = {"text": "", "t": 0.0}


def progress(text: str) -> None:
    """Say what the running tool is doing now (cheap; call as often as you like)."""
    from .sandbox_os import _DETACHED
    if _DETACHED.get():
        return                       # A background helper's work isn't what the user waits on
    _PROGRESS["text"] = str(text)[:300]
    _PROGRESS["t"] = time.monotonic()


def current_progress(max_age: float = 15.0) -> str:
    if _PROGRESS["text"] and time.monotonic() - _PROGRESS["t"] <= max_age:
        return _PROGRESS["text"]
    return ""


def clear_progress() -> None:
    _PROGRESS["text"] = ""


class UserStopped(Violation):
    """The user stopped the task while this tool ran."""


def stopped() -> bool:
    """The user stopped the task (never true inside a background helper)."""
    return stop_requested()


def check(what: str = "") -> None:
    """Raise UserStopped when the user has stopped the task."""
    if stop_requested():
        raise UserStopped(f"{what}: {stopped_text()}" if what else stopped_text())


def walk(top, prune: Callable[[str, list[str]], list[str]] | None = None,
         ) -> Iterator[tuple[str, list[str], list[str]]]:
    """os.walk that a stop ends (checked at every folder). prune(dirpath,
    names) returns the sub-folders to go into — skipped ones are never read."""
    for dp, dns, fns in os.walk(top):
        check()
        if prune is not None:
            dns[:] = prune(dp, dns)
        yield dp, dns, fns


def run(cmd, *, cwd: str | None = None, timeout: float = 30.0,
        env: dict | None = None, what: str = "") -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True) that a stop ends: the
    process is killed and UserStopped raised. A timeout kills it too and
    raises subprocess.TimeoutExpired, like subprocess.run."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    end = time.monotonic() + timeout
    while True:
        try:
            # communicate() may be called again after a timeout without
            # losing output, so a short wait in a loop never fills the pipes
            out, err = p.communicate(timeout=0.2)
            return subprocess.CompletedProcess(cmd, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            if stop_requested() or time.monotonic() >= end:
                p.kill()
                try:
                    p.communicate(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                check(what)
                raise subprocess.TimeoutExpired(cmd, timeout) from None


def read_body(resp, limit: int, chunk: int = 65536) -> bytes:
    """Read up to `limit` bytes of an HTTP response, a piece at a time, so a
    stop ends a slow download."""
    parts: list[bytes] = []
    got = 0
    while got < limit:
        check("reading the page")
        piece = resp.read(min(chunk, limit - got))
        if not piece:
            break
        parts.append(piece)
        got += len(piece)
    return b"".join(parts)

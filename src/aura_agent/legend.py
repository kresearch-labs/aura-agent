"""legend.py — the labels on messages, explained once."""
from __future__ import annotations

LEGEND = """Labels on messages (Aura adds them; the user doesn't type them):
[TASK] the user's instruction now: do it. Earlier tasks matter only if it refers to them.
[REPLY to: …] the user's short answer to the question you asked; it continues that task.
[ASK] a question: answer it and change nothing for it; if you are working on a task, carry on after.
[CHANGE] the user changes the task you are working on.
[LATER] a separate request: don't act on it now; Aura runs it after this task.
[TALK] discuss mode: talk only. Read or search to understand; never change anything or start the work.
[AURA NOTE] a note from Aura, the program running you, not from the user.
The user's own messages start with when they arrived [day date time]; don't write such stamps yourself."""

_MARK = "Labels on messages (Aura adds them"


def with_legend(prompt: str) -> str:
    """The base prompt with the legend once at its end."""
    p = prompt or ""
    return p if _MARK in p else (p.rstrip() + "\n\n" + LEGEND)

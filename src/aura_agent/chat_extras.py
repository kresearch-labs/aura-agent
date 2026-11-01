"""chat_extras.py — pure formatters for chat-only commands.

Receipt insights usage need small render helpers that live outside repl.py
(keeps the dispatcher thin) and are testable headless.
"""
from __future__ import annotations


def usage_line(prompt_tokens: int, completion_tokens: int, ctx: int,
               ledger: int, measured: bool) -> str:
    """One-line token ledger readout for /usage. Honest about the source:
    'measured' (provider usage objects) vs 'estimated' (local tokenizer)."""
    src = "measured" if measured else "estimated"
    window = f" of {ctx:,} window" if ctx else ""
    pct = f" ({100 * ledger // ctx}%)" if ctx and ledger else ""
    return (f"tokens: {ledger:,}{window}{pct} — {src} "
            f"({prompt_tokens:,} in / {completion_tokens:,} out this turn)")


def insights_text(eng) -> str:
    """/insights: reuse the CLI insights collect+render (audit tape)."""
    from pathlib import Path as _P
    from .insights import collect, render
    return render(collect(_P(eng)))

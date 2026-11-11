"""Quiet diagnostics for the pblock organs: nothing reaches the screen unless
AURA_PBLEARN_DEBUG is set.
"""
from __future__ import annotations

import builtins
import os
import sys


def dbg(*args, **kwargs) -> None:
    if not os.environ.get("AURA_PBLEARN_DEBUG"):
        return
    kwargs.pop("file", None)
    kwargs.pop("flush", None)
    builtins.print(*args, file=sys.stderr, flush=True, **kwargs)

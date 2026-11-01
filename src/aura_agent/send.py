"""send.py — `aura send` — scriptable one-shot message.

Reads a message from argv or stdin, writes it to a delivery target that exists
today: the engagement's outbox (picked up by cron jobs / other tooling) or a
plain file. Zero LLM calls, exit 0 on success.

  aura send "build finished" # engagement outbox
  aura send --to file:notify.md "hello"
  echo "long message" | aura send
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def deliver(eng: Path, message: str, to: str | None = None,
            sender: str = "operator") -> Path:
    """Write the message; returns the path written. Never calls the LLM."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if to:
        dest = Path(to.split(":", 1)[1] if ":" in to else to)
        if not dest.is_absolute():
            dest = Path(eng) / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(message, encoding="utf-8")
        return dest
    outbox = Path(eng) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    dest = outbox / f"send-{stamp}.json"
    dest.write_text(json.dumps(dict(
        sender=sender, message=message, created=stamp, channel="outbox"),
        ensure_ascii=False, indent=1), encoding="utf-8")
    return dest


def read_message(argv_msg: list[str]) -> str:
    if argv_msg:
        return " ".join(argv_msg)
    data = sys.stdin.read()
    return data.strip()

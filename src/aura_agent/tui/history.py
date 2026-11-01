"""history.py — what you typed, kept per project (the notebook), so Up and
Ctrl+R reach messages from earlier chats too. One JSON line per entry."""
from __future__ import annotations

import json
import time
from pathlib import Path

FILE = "prompt_history.jsonl"
KEEP = 1000


def load(eng: Path) -> list[str]:
    p = Path(eng) / FILE
    out: list[str] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(ln).get("text", "")
            except ValueError:
                continue
            if t and (not out or out[-1] != t):
                out.append(t)
    except OSError:
        return []
    return out[-KEEP:]


def append(eng: Path, text: str) -> None:
    if not text.strip():
        return
    p = Path(eng) / FILE
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text, "ts": round(time.time())}) + "\n")
        # keep the file bounded
        if p.stat().st_size > 2_000_000:
            lines = p.read_text(encoding="utf-8").splitlines()[-KEEP:]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass

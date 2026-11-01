"""thinking.py — How hard a reasoning model thinks: think off|low|medium|

So Aura sends reasoning_effort (+ enable_thinking for the template servers),
and a server that refuses a field has it dropped and remembered (wire_fit) — no
server is named in the code. Whether the model really thinks is learned from
its replies (model_facts "thinks"), not guessed.
"""
from __future__ import annotations

LEVELS = ("auto", "off", "low", "medium", "high")
WORDS = {"auto": "the model's own default", "off": "no thinking — fastest",
         "low": "a little thinking", "medium": "more thinking",
         "high": "the most thinking — slowest"}


def level(p) -> str:
    """This chat's level (kept on the model profile object, saved with the
    session); else the setting's default."""
    v = getattr(p, "thinking", None)
    if v in LEVELS:
        return v
    try:
        from .options import get
        v = get("model.thinking")
    except Exception:                                # noqa: BLE001
        v = "auto"
    return v if v in LEVELS else "auto"


def fields(lvl: str) -> dict:
    """The request fields for a level ({} for auto: nothing is sent)."""
    if lvl not in LEVELS or lvl == "auto":
        return {}
    return {"reasoning_effort": "none" if lvl == "off" else lvl,
            "chat_template_kwargs": {"enable_thinking": lvl != "off"}}


def seen(key: str, thought: bool) -> None:
    """Remember whether this model sends thinking (first answer it gives)."""
    try:
        from .model_facts import load, save_facts
        rec = load().get(key) or {}
        if thought and not rec.get("thinks"):
            save_facts(key, {"thinks": True})
    except Exception:                                # noqa: BLE001
        pass


def thinks(key: str) -> bool | None:
    """True: this model was seen thinking; None: not seen yet."""
    try:
        from .model_facts import load
        return True if (load().get(key) or {}).get("thinks") else None
    except Exception:                                # noqa: BLE001
        return None

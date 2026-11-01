"""prompt_profiles.py — which system prompt a model gets: Aura's own, for every
model.

So there is no per-model prompt any more: every model gets Aura's prompt
unchanged, and the model maker's advice goes into the request settings instead
(sampling.py — temperature, top_p … — the user chooses).

A tuned copy saved by an older Aura (~/.aura/prompts/<model>.json) is ignored;
`aura prompt reset` deletes it. AURA_PROMPT=generic still picks the research
candidate for `aura eval` comparisons only. Built once per session, so the
prompt stays byte-identical between calls (the backend's prompt cache keeps
hitting — see kv_cache.py).
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _dir() -> Path:
    from .paths import aura_home
    return aura_home() / "prompts"


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(model or "unknown"))[:80]


def copy_path(model: str) -> Path:
    """Where an older Aura kept a tuned copy for this model."""
    return _dir() / f"{_slug(model)}.json"


def prompt_for(model: str | None, core: str) -> tuple[str, str]:
    """(system prompt, where it came from): Aura's prompt, always."""
    if os.environ.get("AURA_PROMPT", "").lower() == "generic":
        from .engine.loop import GENERIC_SYSTEM
        return GENERIC_SYSTEM, "generic candidate (AURA_PROMPT=generic)"
    if model and copy_path(model).exists():
        return core, ("default — an old tuned copy for this model is ignored "
                      "(`aura prompt reset` deletes it)")
    return core, "default"


def remove_copy(model: str) -> bool:
    """`aura prompt reset`: delete an old tuned copy (and its candidate)."""
    gone = False
    for p in (copy_path(model), _dir() / f"{_slug(model)}.candidate.json"):
        try:
            p.unlink()
            gone = True
        except OSError:
            pass
    return gone

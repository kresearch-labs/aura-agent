"""session_settings.py — every session remembers everything it runs with.

They are saved in the session file (sessions.Session.record_settings) and
resume brings the chat back exactly as it was left.

A change in chat is for that session only; `permanent` also saves it as the
profile's default. A new session starts from the profile's defaults.

Keys are never saved here — only the model profile's NAME; its key stays in the
profile's store.
"""
from __future__ import annotations

from pathlib import Path

BUDGET_KEYS = ("preset", "tool_output", "prune_pct", "compact_pct",
               "web_depth", "token_ceiling")


def capture(loop, state: dict | None = None) -> dict:
    """What this loop runs with right now, as plain JSON values."""
    from . import features
    p = loop.llm.p
    out = {
        "provider": getattr(p, "name", None),
        "model": getattr(p, "model", None),
        "base_url": getattr(p, "base_url", None),
        "features": sorted(features.active()),
        "mode": getattr(loop.m, "mode", "act"),
        "assist": getattr(loop.m, "assist", "L2"),
        "thinking": getattr(p, "thinking", None),
    }
    if getattr(loop, "ctx_source", "") == "override":
        out["context_length"] = getattr(p, "context_length", None)
    s = getattr(loop, "settings", None)
    if s is not None:
        out["budget"] = {k: getattr(s, k, None) for k in BUDGET_KEYS}
    if state is not None:
        out["debug_raw"] = bool(state.get("debug_raw", False))
    return out


def switch_provider(loop, name: str, eng: Path) -> str:
    """Point the loop at another model profile (endpoint + model + key)
    for this session only — the profile's default is not changed."""
    from .cli import apply_model
    from .config import resolve
    from .ctx_probe import probe_context_length
    from .llm import LLMClient
    prof = resolve(name)
    loop.llm = LLMClient(prof)
    # Helpers and the reviewer follow the switch
    loop.m._main_llm = loop.llm
    if getattr(loop, "reviewer", None) is not None:
        loop.reviewer.llm = loop.llm
    src = apply_model(loop, prof, eng)
    ctx, csrc = probe_context_length(prof.base_url, prof.model,
                                     configured=prof.context_length)
    prof.context_length = ctx
    if loop.governor is not None:
        loop.governor.ctx = ctx
    loop.ctx_source = csrc
    return src


def apply(loop, saved: dict, eng: Path, state: dict | None = None
          ) -> list[str]:
    """Put a saved session's settings back on the loop. Returns plain
    notes for anything that could not be restored (never raises)."""
    notes: list[str] = []
    if not saved:
        return notes
    from . import features
    p = loop.llm.p
    # model profile (endpoint + key), then the exact model id
    want = saved.get("provider")
    if want and want != getattr(p, "name", None):
        try:
            from .profile_store import get_profile
            if get_profile(want) is None:
                notes.append(f"model profile '{want}' no longer exists — "
                             f"kept {p.name}")
            else:
                switch_provider(loop, want, eng)
                p = loop.llm.p
        except Exception as e:                     # noqa: BLE001
            notes.append(f"could not switch to '{want}': {e}")
    if saved.get("model") and saved["model"] != p.model \
            and getattr(p, "name", None) == want:
        p.model = saved["model"]
        try:
            from .ctx_probe import probe_context_length
            ctx, csrc = probe_context_length(p.base_url, p.model,
                                             configured=p.context_length)
            p.context_length = ctx
            if loop.governor is not None:
                loop.governor.ctx = ctx
            loop.ctx_source = csrc
        except Exception:                          # noqa: BLE001
            pass
    if saved.get("context_length"):
        p.context_length = int(saved["context_length"])
        if loop.governor is not None:
            loop.governor.ctx = p.context_length
        loop.ctx_source = "override"
    # budget
    b = saved.get("budget") or {}
    s = getattr(loop, "settings", None)
    if b and s is not None:
        for k in BUDGET_KEYS:
            if k in b:
                setattr(s, k, b[k])
        if loop.governor is not None:
            loop.governor.threshold = s.compact_pct
            loop.governor.prune_pct = s.prune_pct
        loop.cfg.max_tool_output = s.tool_output
        loop.m.budgets.token_ceiling = (s.token_ceiling
                                        if (s.token_ceiling or 0) > 0
                                        else None)
    if "features" in saved:
        features.set_session(set(saved["features"]))
    if saved.get("mode") in ("act", "plan", "discuss"):
        loop.m.mode = saved["mode"]
    if saved.get("assist") in ("L1", "L2", "L3"):
        loop.m.assist = saved["assist"]
    if saved.get("thinking"):
        loop.llm.p.thinking = saved["thinking"]
    if state is not None and "debug_raw" in saved:
        state["debug_raw"] = bool(saved["debug_raw"])
    return notes


def sync(loop, session, eng: Path, state: dict | None = None) -> list[str]:
    """Opening a session: a session that remembers settings gets them
    back; a new one records what it starts with."""
    notes = apply(loop, session.settings, eng, state) \
        if session.settings else []
    remember(loop, session, state)
    return notes


def remember(loop, session, state: dict | None = None) -> None:
    """Save the current settings into the session when they changed."""
    try:
        session.record_settings(capture(loop, state))
    except Exception as e:                         # noqa: BLE001
        from .eventlog import log_swallowed
        log_swallowed("session_settings.remember", e)

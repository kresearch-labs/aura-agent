"""fallback.py — Model fallback chains (claude-code pattern).

When the primary endpoint dies mid-work, the loop can transparently re-route to
a fallback provider instead of failing the whole session.

Chain construction (priority):
  1. AI_FALLBACKS env — comma-separated profile names
  2. profiles.json "fallbacks" list on the active profile
  3. heuristic: other SAVED profiles that are live right now
                (builtins excluded — pointing at dead defaults helps nobody)

Public surface:
  chain_for(profile_name) -> [names] — primary first, fallbacks after
  pick_fallback(primary) -> (name, ProviderProfile) | (None, None)
                              first LIVE fallback, primary excluded
"""
from __future__ import annotations

import os

from .config import ProviderProfile, resolve
from .profile_store import _load_file, get_profile, list_profiles
from .setup import _fetch_models


def chain_for(primary: str | None = None) -> list[str]:
    """Ordered fallback chain: primary first."""
    primary = primary or _active()
    chain: list[str] = [primary]
    # 1. env
    env = os.environ.get("AI_FALLBACKS", "")
    if not env:
        try:                       # Settings model.fallbacks
            from .options import get
            env = str(get("model.fallbacks") or "")
        except Exception:          # noqa: BLE001
            env = ""
    if env:
        chain += [p.strip() for p in env.split(",") if p.strip()]
    # 2. profiles.json: fallbacks list under the active profile or top-level
    data = _load_file()
    prof_fb = (data.get("profiles", {}).get(primary, {}) or {}).get(
        "fallbacks") or []
    top_fb = data.get("fallbacks") or []
    chain += [p for p in (list(prof_fb) + list(top_fb))]
    # dedupe, keep order, drop primary duplicates
    seen: set[str] = set()
    out = []
    for name in chain:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _active() -> str:
    env = os.environ.get("AI_PROVIDER")
    if env:
        return env
    return _load_file().get("active", "ollama")


def _is_live(name: str) -> bool:
    cfg = get_profile(name) or {}
    ok, _, _, _ = _fetch_models(cfg.get("base_url", ""),
                                cfg.get("api_key"))
    return ok


def pick_fallback(primary: str | None = None,
                  require_live: bool = True,
                  ) -> tuple[str | None, ProviderProfile | None]:
    """First fallback (after primary) that is configured — and live when
    require_live. Returns (None, None) when no fallback qualifies."""
    for name in chain_for(primary)[1:]:
        if name not in list_profiles():
            continue
        if require_live and not _is_live(name):
            continue
        return name, resolve(name)
    return None, None

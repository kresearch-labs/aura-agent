"""auth.py — `aura auth` — "what credentials do I have?"

Credentials visibility as a first-class command. aura auth list — env keys +
per-profile key presence (masked, never printed), plus which profiles lack a
key but need one. Never prints a full key: shows first 4 + last 4 chars.
"""
from __future__ import annotations

import os

from .profile_store import list_profiles
from .setup import _fetch_models


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def collect() -> dict:
    """Everything known about credentials, safe to print."""
    env_keys = {}
    for var in ("AI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "XAI_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY"):
        val = os.environ.get(var)
        if val:
            env_keys[var] = _mask(val)
    profiles = {}
    for name, cfg in list_profiles().items():
        key = cfg.get("api_key")
        url = cfg.get("base_url", "")
        # live-check tells whether the profile works without a key
        ok, _, _, code = _fetch_models(url, key)
        profiles[name] = dict(
            has_key=bool(key),
            key_masked=_mask(key) if key else None,
            works=ok,
            needs_key=(code in (401, 403)),
        )
    return dict(env=env_keys, profiles=profiles)


def render(info: dict) -> str:
    lines = ["credentials:"]
    if info["env"]:
        for var, masked in info["env"].items():
            lines.append(f"  env  {var:18s} {masked}")
    else:
        lines.append("  env  (none set)")
    lines.append("")
    for name, p in info["profiles"].items():
        if p["works"]:
            mark = "✓"
            detail = "works"
        elif p["needs_key"]:
            mark = "•"
            detail = "needs API key (HTTP 401/403)"
        else:
            mark = "✗"
            detail = "unreachable"
        keypart = f"key {p['key_masked']}" if p["has_key"] else "no key"
        lines.append(f"  {mark} {name:14s} {keypart:24s} {detail}")
    return "\n".join(lines)

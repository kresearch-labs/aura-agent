"""connect.py — In-chat provider switcher.

Bare `/connect` in chat: scans local endpoints, lists saved profiles with live
status, numbered picker → hot-swaps the provider (same path as `/profile
<name>`: LLMClient rebuild + ctx re-probe + persisted active).

`/connect <partial>` non-interactive: switches to the first profile whose name
contains <partial> (case-insensitive) — scriptable.
"""
from __future__ import annotations

from .profile_store import active_name, list_profiles
from .setup import _fetch_models


def list_connectable() -> list[tuple[str, dict, str]]:
    """[(name, cfg, status)] for every profile; status from a live ping."""
    out = []
    act = active_name()
    for name, cfg in list_profiles().items():
        ok, models, err, code = _fetch_models(cfg.get("base_url", ""),
                                              cfg.get("api_key"))
        if ok:
            status = f"live · {len(models)} model(s)"
        elif code in (401, 403):
            status = "needs API key"
        else:
            status = "unreachable"
        mark = "→" if name == act else " "
        out.append((f"{mark}{name}", cfg, status))
    return out


def pick_by_prefix(fragment: str) -> str | None:
    frag = fragment.lower()
    for name in list_profiles():
        if frag in name.lower():
            return name
    return None

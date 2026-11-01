"""model_picker.py — `aura model list`.

`aura model` (no action) uses the shared setup menu
(setup_quick.choose_endpoint). `aura model list` prints every profile with its
live status.
"""
from __future__ import annotations

from .profile_store import list_profiles
from .setup import _fetch_models


def _print_profiles() -> None:
    profiles = list_profiles()
    active = None
    try:
        from .profile_store import active_name
        active = active_name()
    except Exception:
        pass
    print()
    print("Profiles:")
    for name, cfg in profiles.items():
        mark = "*" if name == active else " "
        kind = "builtin" if cfg.get("builtin") else "saved"
        print(f" {mark}{name:12s} {cfg.get('base_url', '?'):36s} "
              f"model={cfg.get('model', '?')}  ({kind})")


def cmd_model_list() -> int:
    """`aura model list` — every profile + live reachability + model count."""
    _print_profiles()
    print("\nLive check:")
    profiles = list_profiles()
    for name, cfg in profiles.items():
        url = cfg.get("base_url", "")
        ok, models, err, code = _fetch_models(url, cfg.get("api_key"))
        if ok:
            print(f"  ✓ {name:12s} {len(models)} model(s)  e.g. "
                  f"{', '.join(models[:3])}")
        elif code in (401, 403):
            print(f"  • {name:12s} needs API key (HTTP {code})")
        else:
            print(f"  ✗ {name:12s} unreachable ({err})")
    return 0

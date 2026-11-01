"""profile_store.py — named profiles with model/endpoint/budgets.

Config lives at ~/.aura/profiles.yaml (or AURA_HOME). Ships with out-of-the-box
profiles (ollama/lm-studio/vllm/llamacpp) + user-defined ones. `aura profile
list|use|set|show` + in-chat model profile commands.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _profiles_file() -> Path:
    # per profile: each profile has its own models and keys
    from .paths import profile_path
    return profile_path("profiles.json")

# the usual local addresses only — no model names (picked in setup)
BUILTIN = {
    "ollama":    dict(base_url="http://localhost:11434/v1"),
    "lm-studio": dict(base_url="http://localhost:1234/v1"),
    "vllm":      dict(base_url="http://localhost:8000/v1"),
    "llamacpp":  dict(base_url="http://localhost:8080/v1"),
}


# keys never stay in this file: they go to the profile's key store and
# the file keeps "@keystore" in their place (keystore.py)
SECRET_FIELDS = {"api_key", "client_secret", "token", "access_token",
                 "refresh_token"}
_KS_PREFIX = "profiles.json"


def _load_file() -> dict:
    """The file as stored — keys appear as "@keystore". A key still in
    plain text (older Aura) is moved into the key store on the way."""
    pf = _profiles_file()
    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        from .keystore import seal
        if seal(data, _KS_PREFIX, SECRET_FIELDS):
            _write(pf, data)
            _scrub_backups(pf)
        return data
    return {}


def load_unsealed() -> dict:
    """The file with its keys filled in from the key store."""
    from .keystore import unseal
    return unseal(_load_file(), _KS_PREFIX)


def _write(pf: Path, data: dict) -> None:
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                  encoding="utf-8")


def _scrub_backups(pf: Path) -> None:
    """Setup's backups (profiles.json.pre-setup.*) were copies with the
    keys in them — now that the keys are stored safely, drop them there."""
    from .keystore import MARK
    for b in pf.parent.glob(pf.name + ".pre-setup.*"):
        try:
            d = json.loads(b.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def _strip(o):
            if isinstance(o, dict):
                return {k: (MARK if k in SECRET_FIELDS and isinstance(v, str)
                            and v and not v.startswith("${") else _strip(v))
                        for k, v in o.items()}
            if isinstance(o, list):
                return [_strip(v) for v in o]
            return o
        clean = _strip(d)
        if clean != d:
            _write(b, clean)


def _save_file(data: dict) -> None:
    from .keystore import seal
    data = json.loads(json.dumps(data))           # Don't touch the caller's
    seal(data, _KS_PREFIX, SECRET_FIELDS)
    _write(_profiles_file(), data)


def list_profiles() -> dict:
    """{name: {base_url, model, ...}} — builtins + user overrides + customs."""
    from .keystore import unseal
    saved = _load_file()
    out = {}
    for name, cfg in BUILTIN.items():
        out[name] = {**cfg, "builtin": True}
    for k, v in (saved.get("profiles") or {}).items():
        out[k] = {**unseal(v, f"{_KS_PREFIX}.profiles.{k}"), "builtin": False}
    return out


def get_profile(name: str) -> dict | None:
    return list_profiles().get(name)


def set_profile(name: str, activate: bool = True, **fields) -> dict:
    """Create/update a model setup (builtins can be shadowed). activate:
    also make it the profile's default — setup does; saving a detail of
    a setup (its context size, a fixed URL) must not."""
    data = _load_file()           # Keys stay "@keystore"; new ones get sealed
    profiles = data.setdefault("profiles", {})
    cur = profiles.get(name, BUILTIN.get(name, {}))
    cur.update({k: v for k, v in fields.items() if v is not None})
    profiles[name] = cur
    if activate:
        data["active"] = name
    _save_file(data)
    return cur


def active_name() -> str:
    env = os.environ.get("AI_PROVIDER")
    if env:
        return env
    data = _load_file()
    return data.get("active", "ollama")


def set_active(name: str) -> bool:
    if name not in list_profiles():
        return False
    data = _load_file()
    data["active"] = name
    _save_file(data)
    return True

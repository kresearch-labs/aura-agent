"""config.py — provider profiles + env overrides (generic-first).

Out-of-the-box profiles: ollama, lm-studio, vllm, llamacpp, or any
OpenAI-compatible endpoint. Env overrides win: AI_PROVIDER, AI_MODEL,
AI_BASE_URL, AI_API_KEY, AI_CONTEXT_LENGTH.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class ProviderProfile:
    name: str = "custom"
    base_url: str = "http://localhost:11434/v1"
    model: str = "local"
    api_key: str | None = None          # Local endpoints need none
    # None = ask the server (ctx_probe).
    context_length: int | None = None
    reasoning: bool = False             # reasoning_content passthrough
    thinking: str | None = None         # Think level for this chat (thinking.py)
    parallel_tools: bool = True
    first_token_timeout: float = 300.0  # Local models think 60–180s
    roles: dict | None = None           # Model lanes: {"worker": Profile}


# Where each local server usually listens — the ADDRESS only.
PROFILES: dict[str, dict] = {
    "ollama":    dict(base_url="http://localhost:11434/v1"),
    "lm-studio": dict(base_url="http://localhost:1234/v1"),
    "vllm":      dict(base_url="http://localhost:8000/v1"),
    "llamacpp":  dict(base_url="http://localhost:8080/v1"),
}


def resolve(name: str | None = None, **over) -> ProviderProfile:
    """Build profile from name + overrides + env. Precedence: explicit args > env
    > USER-SAVED profile (profile_store) > builtin PROFILES.

    Fix: user-saved profiles (profile_store) were silently ignored — a saved
    'glm' profile resolved to builtin defaults ('local' @:11434) because base =
    PROFILES.get(name) missed it. Saved layer now merges over builtins, env
    still wins over both.
    """
    name = (over.pop("env_name", None) or name
            or os.environ.get("AI_PROVIDER"))
    if not name:
        try:
            from .profile_store import active_name as _active
            name = _active()
        except Exception:
            name = "ollama"
    base = dict(PROFILES.get(name, {}))
    # user-saved layer (shadows builtins; source of /profile-saved endpoints)
    try:
        from .profile_store import get_profile as _saved
        saved = _saved(name) or {}
        base.update({k: v for k, v in saved.items()
                     if k != "builtin" and v is not None})
    except Exception:
        pass  # Profile store unreadable → builtin defaults stand
    base.setdefault("name", name)
    # env layer
    env_map = {"model": "AI_MODEL", "base_url": "AI_BASE_URL",
               "api_key": "AI_API_KEY", "context_length": "AI_CONTEXT_LENGTH"}
    for k, ev in env_map.items():
        if (v := os.environ.get(ev)):
            base[k] = v
    # explicit layer
    base.update({k: v for k, v in over.items() if v is not None})
    known = {f.name for f in fields(ProviderProfile)}
    p = ProviderProfile(**{k: v for k, v in base.items() if k in known})
    if isinstance(p.context_length, str):
        p.context_length = int(p.context_length)
    return p


# ===========================================================================
# RUN SETTINGS — the 4 presets (Full / Balanced / Economy / Budget).
# SETTINGS_DESIGN FINAL: exactly 3 user dials (tool-result inline cap,
# prune/compact pair, web research depth). Presets are DEFAULTS for silence,
# never limits over an explicit operator instruction. Scope rule maps onto the
# existing precedence chain: task flag > env > saved settings > preset.
# ===========================================================================
PRESET_TABLE: dict[str, dict] = {
    # "full" keeps 1MB.
    "none":      dict(tool_output=20_000,    prune_pct=0.50, compact_pct=0.80, web_depth=None),
    "full":      dict(tool_output=1_000_000, prune_pct=0.50, compact_pct=0.80, web_depth=None),
    "balanced":  dict(tool_output=50_000,     prune_pct=0.40, compact_pct=0.60, web_depth=100),
    "economy":   dict(tool_output=20_000,     prune_pct=0.35, compact_pct=0.55, web_depth=50),
    "budget":    dict(tool_output=8_000,      prune_pct=0.30, compact_pct=0.50, web_depth=10),
}
DEFAULT_PRESET = "none"
SETTINGS_KEYS = ("tool_output", "prune_pct", "compact_pct", "web_depth",
                 "token_ceiling")


@dataclass
class RunSettings:
    preset: str = DEFAULT_PRESET
    tool_output: int = 50_000
    prune_pct: float = 0.40
    compact_pct: float = 0.60
    web_depth: int | None = 100      # None = until scope exhausted
    token_ceiling: int | None = None  # Per-session cap; None = uncapped


def _settings_path() -> Path:
    from .paths import profile_path
    return profile_path("settings.json")      # The profile's defaults


def load_saved_settings() -> dict:
    try:
        return json.loads(Path(_settings_path()).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_saved_settings(d: dict) -> Path:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def resolve_settings(preset: str | None = None) -> RunSettings:
    """Resolve the active run settings.

    Precedence (scope rule): explicit preset arg > AURA_PRESET env > saved
    settings.preset > DEFAULT_PRESET. Individual keys: saved explicit values
    shadow the preset's; AURA_TOOL_OUTPUT / AURA_PRUNE_PCT / AURA_COMPACT_PCT /
    AURA_WEB_DEPTH env (power users) shadow everything.
    """
    name = (preset or os.environ.get("AURA_PRESET") or "").strip().lower()
    saved = load_saved_settings()
    if not name:
        name = str(saved.get("preset") or DEFAULT_PRESET).lower()
    if name not in PRESET_TABLE:
        name = DEFAULT_PRESET
    vals = dict(PRESET_TABLE[name])
    # saved explicit values shadow the preset's numbers (precedence chain:
    # saved layer sits above builtins). Per-key-vs-task-flag precedence
    # wrinkle = UNDISCUSSED — saved always applies for now.
    if not preset:
        for k in SETTINGS_KEYS:
            if k in saved and saved[k] is not None:
                vals[k] = saved[k]
    # power-user env shadows everything
    env_map = {"tool_output": "AURA_TOOL_OUTPUT", "prune_pct": "AURA_PRUNE_PCT",
               "compact_pct": "AURA_COMPACT_PCT", "web_depth": "AURA_WEB_DEPTH",
               "token_ceiling": "AURA_TOKEN_CEILING"}
    for k, ev in env_map.items():
        if os.environ.get(ev):
            try:
                v = float(os.environ[ev]) if k.endswith("pct") else int(os.environ[ev])
                vals[k] = v
            except ValueError:
                pass
    return RunSettings(preset=name, **vals)

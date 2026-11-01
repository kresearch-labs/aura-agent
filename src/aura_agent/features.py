"""features.py — 2.2: /features switches for behaviour features.

One place to turn the agent's behaviour features on or off, with presets.
Safety is NOT here and can't be switched off from a menu: approvals,
skill-pack limits, protected files, scope and the operator's hooks are
always on.

Precedence: AURA_FEATURES env (e.g. "-auto_lint,proof", "none", "all")
over this session's switches (/features without `permanent` — the
session file remembers them) over the profile's saved switches
(<profile>/settings.json "features") over the saved preset
("features_preset") over the default preset. Unknown names are ignored — a typo must never switch something else.
"""
from __future__ import annotations

import os

# name: (roadmap id, what it does)
FEATURES: dict[str, tuple[str, str]] = {
    "proof": ("1C.12", "before the final answer, re-check reads and run the "
                       "project's tests; one turn to fix failures"),
    "auto_lint": ("2.5", "after a step that changed code, run the linter and "
                         "report problems in the changed files"),
    "repro_first": ("2.6", "bug-fix tasks are asked to show the bug with a "
                           "failing test first"),
    "syntax_note": ("2.4", "warn at once when a write leaves a Python/JSON/"
                           "TOML file that no longer parses"),
    "drift_check": ("1C.16", "notice when tool calls wander from the task "
                             "and re-read it"),
    "reviewer": ("1.4", "after repeated failures, a fresh-context second "
                        "opinion on what is going wrong"),
}

PRESETS: dict[str, frozenset[str]] = {
    "recommended": frozenset(FEATURES),
    "minimal": frozenset({"syntax_note", "proof"}),
    "core": frozenset({"drift_check", "reviewer", "syntax_note"}),
    "off": frozenset(),
}
# The drift audit ran on every task in the original; the failure reviewer is
# the design's second opinion. syntax_note joined after `aura eval`: it is free
# (a local parse) and was the only setting that caught a broken edit — 7/7 vs
# 5/7. The rest stay off.
DEFAULT_PRESET = "core"


def parse(spec: str, base: set[str]) -> set[str]:
    """'a,-b' → base + {a} - {b}; 'all' / 'none' reset; preset names too."""
    out = set(base)
    for raw in (spec or "").split(","):
        tok = raw.strip().lower()
        if not tok:
            continue
        if tok == "all":
            out = set(FEATURES)
        elif tok == "none":
            out = set()
        elif tok in PRESETS:
            out = set(PRESETS[tok])
        elif tok.startswith("-") and tok[1:] in FEATURES:
            out.discard(tok[1:])
        elif tok.lstrip("+") in FEATURES:
            out.add(tok.lstrip("+"))
    return out


def _saved() -> dict:
    from .config import load_saved_settings
    return load_saved_settings()


# this session's switches; None = follow the profile's saved ones
_SESSION: set[str] | None = None


def set_session(on: set[str] | None) -> None:
    global _SESSION
    _SESSION = None if on is None else {n for n in on if n in FEATURES}


def active() -> set[str]:
    """The switched-on features right now. Never raises."""
    try:
        if _SESSION is not None:
            return parse(os.environ.get("AURA_FEATURES", ""), set(_SESSION))
        saved = _saved()
        preset = str(saved.get("features_preset") or DEFAULT_PRESET).lower()
        on = set(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))
        for name, val in (saved.get("features") or {}).items():
            if name in FEATURES:
                (on.add if val else on.discard)(name)
        return parse(os.environ.get("AURA_FEATURES", ""), on)
    except Exception:                                # noqa: BLE001
        return set(PRESETS[DEFAULT_PRESET])


def enabled(name: str) -> bool:
    return name in active()


def set_feature(name: str, on: bool, permanent: bool = True) -> str:
    from .config import save_saved_settings
    if name not in FEATURES:
        return f"unknown feature '{name}' — /features lists them"
    if _SESSION is not None or not permanent:
        cur = active() if _SESSION is None else set(_SESSION)
        (cur.add if on else cur.discard)(name)
        set_session(cur)
        if not permanent:
            return f"{name}: {'on' if on else 'off'} (this session)"
    saved = _saved()
    feats = dict(saved.get("features") or {})
    feats[name] = bool(on)
    saved["features"] = feats
    save_saved_settings(saved)
    return f"{name}: {'on' if on else 'off'} (saved)"


def apply_preset(name: str, permanent: bool = True) -> str:
    from .config import save_saved_settings
    name = name.lower()
    if name not in PRESETS:
        return f"unknown preset '{name}' — {', '.join(PRESETS)}"
    if _SESSION is not None or not permanent:
        set_session(set(PRESETS[name]))
        if not permanent:
            return f"features preset: {name} (this session)"
    saved = _saved()
    saved["features_preset"] = name
    saved["features"] = {}                 # A preset resets single switches
    save_saved_settings(saved)
    return f"features preset: {name} (saved)"


# Suggestions: proof / repro_first / auto_lint stay off but are suggested when
# the work needs them. Like VS Code's recommendations: remembered per project
# (not per chat — that nags), a "no" sticks (`/features never <name>`), and one
# switch stops them all (assist.suggest_checks).
SUGGEST_FILE = "suggested.json"
SUGGEST_EVERY = 7 * 86400              # Seconds: at most once a week per project


def _suggested(root) -> dict:
    import json
    from pathlib import Path
    try:
        return json.loads((Path(root) / SUGGEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_suggested(root, d: dict) -> None:
    import json
    from pathlib import Path
    try:
        (Path(root) / SUGGEST_FILE).write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def may_suggest(root, name: str, now: float | None = None) -> bool:
    import time
    d = _suggested(root)
    if name in d.get("never", []):
        return False
    return (now or time.time()) - float(d.get("at", {}).get(name, 0)) >= SUGGEST_EVERY


def mark_suggested(root, name: str, now: float | None = None) -> None:
    import time
    d = _suggested(root)
    d.setdefault("at", {})[name] = now or time.time()
    _save_suggested(root, d)


def never_suggest(root, name: str) -> str:
    d = _suggested(root)
    if name not in d.setdefault("never", []):
        d["never"].append(name)
    _save_suggested(root, d)
    return f"{name}: not suggested again in this project (/features on {name} still works)"


def command(parts: list[str], session: bool = False, root=None) -> list[str]:
    """/features                     list with on/off
    /features on|off <name>        switch one
    /features preset <name>        recommended | minimal | core | off
    /features never <name>         stop suggesting it in this project
    In chat (session=True) a change is for this session only; add
    `permanent` to also save it as the profile's default."""
    permanent = (not session) or "permanent" in [p.lower() for p in parts]
    parts = [p for p in parts if p.lower() != "permanent"]
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("on", "off") and len(parts) > 2:
        return [set_feature(parts[2].lower(), sub == "on", permanent)]
    if sub == "never" and len(parts) > 2 and root is not None:
        return [never_suggest(root, parts[2].lower())]
    if sub == "preset" and len(parts) > 2:
        return [apply_preset(parts[2], permanent)]
    on = active()
    env = os.environ.get("AURA_FEATURES")
    lines = ["features (behaviour only — safety is always on):"]
    for name, (rid, what) in FEATURES.items():
        lines.append(f"  [{'on ' if name in on else 'off'}] {name:12s} {what}")
    lines.append("/features on|off <name> · /features preset "
                 + " | ".join(PRESETS)
                 + (" · add `permanent` to save for the profile"
                    if session else "")
                 + (f" · AURA_FEATURES={env} is set" if env else ""))
    return lines

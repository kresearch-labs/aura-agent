"""agent_status.py — `aura status` agent-level HUD.

One screen, everything a user checks before/during work:
  agent version + uptime of install
  model active profile → endpoint → live model count (doctor-light)
  ctx probed context window + source
  doctor the 1.6 checks, one-line verdict
  sessions recent session files for the engagement (last 3)

Engagement section (existing render_status) is appended when a local engagement
dir exists.
"""
from __future__ import annotations

import json
from pathlib import Path

from .profile_store import active_name, get_profile
from .setup import _fetch_models


def _sessions_summary(eng: Path, limit: int = 3) -> list[str]:
    sdir = Path(eng) / "sessions"
    if not sdir.exists():
        return []
    files = sorted(sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:limit]
    out = []
    for f in files:
        try:
            n = sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                    if line.strip())
        except OSError:
            n = 0
        out.append(f"{f.name}  ({n} msgs)")
    return out


def render_agent_status(eng_dir: Path) -> str:
    from . import __version__
    lines = []
    lines.append("┌─────────────────────────────────────────────────────┐")
    lines.append("│  Aura status                                        │")
    lines.append("└─────────────────────────────────────────────────────┘")

    # --- version + profile ---------------------------------------------------
    lines.append(f"  version : {__version__}")
    pname = active_name()
    prof = get_profile(pname) or {}
    url = prof.get("base_url", "?")
    lines.append(f"  profile : {pname}")
    lines.append(f"  endpoint: {url}")
    lines.append(f"  model   : {prof.get('model', '?')}")

    # --- live endpoint check (doctor-light) -----------------------------------
    ok, models, err, code = _fetch_models(url, prof.get("api_key"))
    if ok:
        lines.append(f"  live    : ✓ endpoint answers ({len(models)} models)")
    elif code in (401, 403):
        lines.append(f"  live    : • endpoint needs an API key (HTTP {code})")
    else:
        lines.append(f"  live    : ✗ unreachable — run: aura model")

    # --- ctx ------------------------------------------------------------------
    try:
        from .ctx_probe import describe_source, probe_context_length
        ctx, src = probe_context_length(url, prof.get("model", "?"),
                                        configured=prof.get("context_length"))
        lines.append(f"  ctx     : {ctx} ({describe_source(src)})")
    except Exception as e:
        lines.append(f"  ctx     : ? ({e})")

    # --- doctor verdict (one line) ---------------------------------------------
    try:
        from .doctor_checks import run_checks
        checks, _ = run_checks(fix=False)
        bad = sum(1 for c in checks if not c["ok"])
        lines.append(f"  doctor  : {'OK' if bad == 0 else f'{bad} issue(s)'} "
                     f"(details: aura doctor)")
    except Exception:
        lines.append("  doctor  : ? (run aura doctor)")

    # --- engagement section ------------------------------------------------------
    eng = Path(eng_dir)
    lines.append("")
    if (eng / "scope.json").exists():
        try:
            from .engagement import engagement_status, render_status
            lines.append(render_status(engagement_status(eng)))
        except Exception as e:
            lines.append(f"  notebook:   error rendering ({e})")
    else:
        lines.append("  notebook:   none here — start one: aura init <name>")
    sess = _sessions_summary(eng)
    if sess:
        lines.append("  recent sessions:")
        for s in sess:
            lines.append(f"    • {s}")
    return "\n".join(lines)

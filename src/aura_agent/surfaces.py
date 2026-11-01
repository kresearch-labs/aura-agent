"""surfaces.py — Per-surface tool gating.

A tool surface = the context Aura runs in. Each surface declares which tool
blast-radii it may carry:

  interactive — everything (chat / run, operator present)
  cron — headless runs: no EXECUTE prompts (L1 approvals can't be
                 flagged for review; READ/WRITE allowed

Public: allowed_blast_radii(surface) -> set; gate_mediator(mediator, surface)
configures assist/mode accordingly + returns a summary line. The mediator keeps
final authority — this only sets surface policy.
"""
from __future__ import annotations

SURFACES: dict[str, dict] = {
    "interactive": dict(
        radii={"read", "write", "execute"},
        assist_default="L2",
        note="operator present — full toolset"),
    "chat": dict(
        radii={"read", "write", "execute"},
        assist_default="L2",
        note="interactive chat — full toolset"),
    "cron": dict(
        radii={"read", "write"},
        # Was L1 (ask for every write).
        assist_default="L2",
        note="unattended: no EXECUTE-blast tools, writes auto-approved"),
}

VALID = tuple(SURFACES)


def allowed_blast_radii(surface: str) -> set[str]:
    cap = SURFACES.get(surface)
    if not cap:
        raise ValueError(f"unknown surface '{surface}' — valid: {VALID}")
    return set(cap["radii"])


def gate_mediator(mediator, surface: str) -> str:
    """Apply surface policy to a live mediator; returns a summary line."""
    cap = SURFACES.get(surface)
    if not cap:
        raise ValueError(f"unknown surface '{surface}' — valid: {VALID}")
    radii = allowed_blast_radii(surface)
    # map blast radii -> tool-name allowlist from the registered specs
    allowed_names = set()
    for name, spec in mediator.tools.items():
        radius = getattr(spec, "blast_radius", None)
        radius_name = getattr(radius, "value", radius)
        if str(radius_name).lower() in radii:
            allowed_names.add(name)
    mediator.assist = cap["assist_default"]
    removed = sorted(set(mediator.tools) - allowed_names)
    for name in removed:
        mediator.tools.pop(name, None)
    return (f"surface {surface}: {len(allowed_names)} tools "
            f"({', '.join(sorted(radii))}) — {cap['note']}"
            + (f"; removed: {', '.join(removed)}" if removed else ""))

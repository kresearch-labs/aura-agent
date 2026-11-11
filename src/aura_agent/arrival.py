"""arrival.py —: the arrival router (detection + one-question flows).

Pure functions; the endpoint probe is injectable (`_probe`) so tests never
touch the network.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Arrival:
    endpoint_live: bool
    endpoint_url: str
    profile: str
    first_run: bool
    returning: bool
    findings_total: int = 0
    findings_proven: int = 0
    last_session: str | None = None
    last_age_min: float | None = None
    novice: bool = False


def _probe(base_url: str, api_key: str | None = None) -> tuple[bool, int]:
    """Live endpoint check → (ok, n_models). Mocked in tests."""
    from .setup import _fetch_models
    ok, models, _err, _code = _fetch_models(base_url, api_key)
    return ok, len(models or [])


def detect(eng) -> Arrival:
    """Read engagement + endpoint state. One probe max; failure = dead."""
    from pathlib import Path as _P
    from .profile_store import active_name
    from .config import resolve
    from .finding_card import findings_snapshot
    from .session_store import list_sessions

    eng = _P(eng)
    active = active_name()
    prof = None
    try:
        prof = resolve(active) if active else None
    except Exception:
        prof = None
    url = prof.base_url if prof else ""
    key = prof.api_key if prof else None
    live, _n = _probe(url, key) if url else (False, 0)

    sessions = [s for s in list_sessions(eng) if s.get("messages", 0) > 2]
    findings = findings_snapshot_safe(eng)
    proven = sum(1 for f in findings if f.get("proof"))
    last = sessions[0] if sessions else None

    return Arrival(
        endpoint_live=bool(live),
        endpoint_url=url,
        profile=active or "",
        first_run=not sessions,
        returning=bool(sessions),
        findings_total=len(findings),
        findings_proven=proven,
        last_session=(last or {}).get("file"),
        last_age_min=(last or {}).get("age_min"),
        novice=not sessions,
    )


def findings_snapshot_safe(eng) -> list[dict]:
    try:
        from .finding_card import findings_snapshot
        return findings_snapshot(eng)
    except Exception:
        return []


# ------------------------------------------------------------- renders ----

def render_first_arrival(a: Arrival) -> str:
    """FIRST arrival: bare card + ONE question. Novices get the 3-line
    guide; experts get the capability line. Hero copy throughout."""
    lines = []
    if a.novice:
        # Cao & Liu: novices benefit from ONE light explicit guide
        lines.append("new here — three things:")
        lines.append("  1. a local agent for your project — give it a task,")
        lines.append("     it does the work and proves the result")
        lines.append("  2. type a task in plain words")
        lines.append("  3. /help shows every command with descriptions")
    else:
        lines.append("give me a task and I'll prove the result — you own "
                     "the call, I do the verification work")
    lines.append("")
    lines.append("what do you want to do? (plain words)")
    return "\n".join(lines)


def render_returning_arrival(a: Arrival) -> str:
    """RETURNING arrival as a RAIL CARD: ┌─ resume ─┐ box with ┃ rows, findings
    inside the card, dead-end never silent.
    """
    from .contracts.visual2 import rail_top, rail_row, rail_bot, fg, _no_color
    if _no_color():
        # Structure survives, zero SGR — dead-endpoint warn INCLUDED
        # (no-dead-end: the warn + exact fix is information, not paint)
        lines = []
        if not a.endpoint_live:
            lines.append(f"endpoint DEAD ({a.endpoint_url})\n"
                         "  /connect to switch — you can still review "
                         "findings")
        if a.last_session:
            age = f", {a.last_age_min:.0f}m ago" if a.last_age_min else ""
            lines.append(f"last session: {a.last_session}{age}")
        if a.findings_total:
            lines.append(f"findings: {a.findings_total} findings · "
                         f"{a.findings_proven} proven")
        lines.append("type a task to continue, /resume <name> to switch, "
                     "/results to review")
        return "\n".join(lines)
    W = 64
    A = "#5c9cf5"   # Agent rail color
    out = [rail_top(W, " resume ", rail=A)]
    if not a.endpoint_live:
        out.append(rail_row(A, fg("✗ endpoint DEAD ", "#e06c75", bold=True)
                            + fg(a.endpoint_url or "", "#9aa8d4"), width=W))
        out.append(rail_row(A, fg("/connect to switch — your results are still "
                                  "here", "#9aa8d4"), width=W))
    if a.last_session:
        age = f"  ·  {a.last_age_min:.0f}m ago" if a.last_age_min else ""
        out.append(rail_row(A, fg("last session  ", "#9aa8d4")
                            + fg(str(a.last_session), "#dde3f0", bold=True)
                            + fg(age, "#9aa8d4"), width=W))
    if a.findings_total:
        out.append(rail_row(A, fg("results   ", "#9aa8d4")
                            + fg(f"{a.findings_total}", "#61ffca", bold=True)
                            + fg(f" · {a.findings_proven} proven "
                                 "", "#9aa8d4"),
                            width=W))
    out.append(rail_row(A, fg("type a task · /resume <name> · /results",
                              "#dde3f0"), width=W))
    out.append(rail_bot(W, rail=A))
    return "\n".join(out)

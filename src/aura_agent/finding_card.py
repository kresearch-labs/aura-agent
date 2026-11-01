"""finding_card.py — THE product surface. One renderer, two scales."""
from __future__ import annotations

import os
import time

_SEV_ORDER = ["critical", "high", "medium", "low", "info"]

# --- polish: terminal colorize, opt-in (report.md stays plain) ---------
# One renderer, two skins: finding_card() returns plain text (file-safe);
# colorize_card() paints the SAME text for the terminal. No drift: the
# color pass is purely additive over the plain renderer's output.
_SEV_COLOR = {"critical": 210, "high": 209, "medium": 215,
              "low": 111, "info": 249}


def _colors_wanted() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        from . import ui
        return ui._ENABLED
    except Exception:
        return False


def colorize_card(text: str) -> str:
    """Paint a rendered card for the terminal: section headers (MEANS/
    PROOF/NEXT/MORE) in accent, severity word tinted by level, evidence
    lines dimmed. Plain text in -> same text with ANSI. No reflow."""
    if not _colors_wanted():
        return text
    from . import ui
    out = []
    for ln in text.split("\n"):
        stripped = ln.lstrip()
        indent = ln[:len(ln) - len(stripped)]
        if stripped.split(" ", 1)[0] in ("MEANS", "PROOF", "NEXT", "MORE"):
            head = stripped.split(" ", 1)[0]
            rest = stripped[len(head):]
            out.append(indent + ui.paint(head, ui.C.get("banner_accent", 117))
                       + rest)
            continue
        for sev, col in _SEV_COLOR.items():
            if f"severity: {sev} " in ln:
                ln = ln.replace(f"severity: {sev} ",
                                ui.paint(f"severity: {sev} ", col))
                break
        if stripped.startswith("evidence:"):
            ln = ui.paint(ln, 249)
        out.append(ln)
    return "\n".join(out)

_SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


def finding_card(rec: dict, scale: str = "full") -> str:
    """Render ONE finding. scale: 'full' (in-stream / /findings <id> /
    report) or 'key' (exec-summary strip: MEANS+PROOF only)."""
    sev = rec.get("severity", "info")
    target = rec.get("target") or ""
    title = rec.get("title", rec.get("id", "result"))
    proof = rec.get("proof") or {}
    lines: list[str] = []

    # -- MEANS: the claim, in plain words -----------------------------------
    lines.append(f"MEANS  {title}" + (f" — {target}" if target else ""))
    kind = rec.get("vuln_type") or ""
    if kind or sev != "info":
        lines.append("       " + " · ".join(
            ([f"kind: {kind}"] if kind else [])
            + ([f"severity: {sev} (one input, not the verdict — the proof "
                "below is the weight)"] if sev != "info" else [])))

    # -- PROOF: boundary crossed, showed-language, self-verifying ----------
    lines.append("PROOF")
    if proof:
        cmd = proof.get("command") or proof.get("output_ref") or "?"
        lines.append(f"   showed: re-verify succeeded ({rec.get('status', 'validated')})")
        lines.append(f"   re-run: {cmd}")
        lines.append(f"   when: {_fmt_ts(proof.get('timestamp'))}")
        if proof.get("hash"):
            lines.append(f"   hash: {proof['hash']}")
        if proof.get("output_ref"):
            lines.append(f"   output: {proof['output_ref']}")
    else:
        lines.append("   not proven — this result stays out of the report")
        lines.append("   needs: the reads that show it, then record it again "
                     "(re-checked automatically)")

    # -- NEXT: routed action ------------------------------------------------
    if proof:
        lines.append("NEXT   aura report writes report.md · or /findings")
    else:
        lines.append("NEXT   ask the agent to show it, then record it again "
                     "— or /findings to review")
    lines.append("MORE")
    refs = rec.get("evidence_refs") or []
    if refs:
        for r in refs[:4]:
            lines.append(f"   evidence: {r}")
    if rec.get("id"):
        lines.append(f"   id: {rec['id']} · repro: see report methodology "
                     f"(re-run the PROOF command from a clean state)")
    if scale == "key":
        # exec-summary strip: MEANS+PROOF only
        cut = lines.index("NEXT")
        return "\n".join(lines[:cut])
    return "\n".join(lines)


# ------------------------------------------------------------- wiring ----
# Proof-landing detection + post-turn cards. findings.jsonl is append-only, so
# a cheap file re-read diffs the validated set — zero engine changes, zero
# model calls.

def findings_snapshot(eng) -> list[dict]:
    """Load current findings.jsonl ([] when absent)."""
    import json as _j
    from pathlib import Path as _P
    p = _P(eng) / "findings.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            r = _j_load(ln)
        except Exception:
            continue
        if r.get("id"):
            out.append(r)
    return out


def _j_load(line: str) -> dict:
    import json as _j
    return _j.loads(line)



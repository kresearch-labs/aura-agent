"""report.py — markdown report generator (`aura report` → report.md).

Rules (BUILD ):
  - summary + results (ONLY proven ones — enforced via
    FindingStore.reportable) + evidence links into artifacts/ +
    methodology (tools used, budget, timeline)
  - every evidence ref must resolve to a real file (exit criterion)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..tools.artifacts import load_artifact


def generate_report(engagement_root: Path, findings_store,
                    validator=None, title: str | None = None,
                    audit_path: Path | None = None) -> str:
    root = Path(engagement_root)
    findings = findings_store.reportable()
    excluded = [r for r in findings_store.records if r not in findings]

    # ---- methodology: derive from audit log -------------------------------
    tool_counts: dict[str, int] = {}
    refusals, first_ts, last_ts = 0, None, None
    audit = Path(audit_path) if audit_path else root / "audit.jsonl"
    if audit.exists():
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            tool_counts[r.get("call", "?")] = tool_counts.get(r.get("call", "?"), 0) + 1
            if str(r.get("status", "")).startswith(("REFUSED", "TOOL_ERROR")):
                refusals += 1
            ts = r.get("ts")
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts = ts if last_ts is None else max(last_ts or 0, ts)

    # graph stats
    graph_path = root / "graph.json"
    graph_line = ""
    if graph_path.exists():
        try:
            g = json.loads(graph_path.read_text(encoding="utf-8"))
            by: dict[str, int] = {}
            for n in g.get("nodes", []):
                by[n["status"]] = by.get(n["status"], 0) + 1
            graph_line = ", ".join(f"{k}: {v}" for k, v in sorted(by.items()))
        except (json.JSONDecodeError, KeyError):
            graph_line = "unreadable"

    out: list[str] = []
    out.append(f"# {title or 'Work report'}")
    out.append("")
    out.append(f"_generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}_")
    out.append("")

    # ---- summary -----------------------------------------------------------
    out.append("## Summary")
    sev_order = ["critical", "high", "medium", "low", "info"]
    sev = lambda r: r.get("severity") or "info"                  # noqa: E731
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[sev(f)] = by_sev.get(sev(f), 0) + 1
    summary = (", ".join(f"{s}: {by_sev[s]}" for s in sev_order
                         if by_sev.get(s))
               if set(by_sev) - {"info"} else "")
    out.append(f"{len(findings)} proven result(s)"
               + (f" — {summary}" if summary else "")
               + ".")
    if excluded:
        out.append(f"{len(excluded)} result(s) left out — not proven:")
        for r in excluded:
            out.append(f"  - {r['id']} {r['title']} [{r['status']}]")
    out.append("")

    # ---- results (: same MEANS/PROOF/NEXT/MORE sections as the chat
    # card — one renderer's vocabulary at report scale, zero drift) --------
    from ..finding_card import finding_card as _fc
    out.append("## Proven results")
    if not findings:
        out.append("_no proven results_")
    for f in sorted(findings, key=lambda r: sev_order.index(sev(r))):
        tag = f" [{sev(f).upper()}]" if sev(f) != "info" else ""
        out.append(f"### {f['id']} — {f['title']}{tag}")
        out.append("```")
        out.append(_fc(f))
        out.append("```")
        out.append("")

    # ---- methodology -------------------------------------------------------
    out.append("## Methodology")
    out.append(f"- tool calls: {sum(tool_counts.values())} total"
               + (f" ({refusals} refused/errored)" if refusals else ""))
    if tool_counts:
        top = ", ".join(f"{k}×{v}" for k, v in sorted(
            tool_counts.items(), key=lambda kv: -kv[1])[:8])
        out.append(f"- tool mix: {top}")
    if graph_line:
        out.append(f"- task graph: {graph_line}")
    if first_ts:
        span = (last_ts or first_ts) - first_ts
        out.append(f"- active window: {span/60:.0f} min")
    out.append("")

    # ---- evidence appendix with resolution check ---------------------------
    out.append("## Evidence Index")
    missing = []
    for f in findings:
        proof = f.get("proof") or {}
        if proof.get("output_ref"):
            try:
                load_artifact(root, proof["output_ref"])
                out.append(f"- {f['id']}: `{proof['output_ref']}` ✓")
            except (FileNotFoundError, ValueError) as e:
                missing.append((f["id"], proof["output_ref"], str(e)))
                out.append(f"- {f['id']}: `{proof['output_ref']}` ✗ ({e})")
        for ref in f.get("evidence_refs", []):
            p = root / ref
            if p.exists():
                out.append(f"- {f['id']}: `{ref}` ✓")
            else:
                missing.append((f["id"], ref, "not found"))
                out.append(f"- {f['id']}: `{ref}` ✗ (not found)")
    out.append("")
    if missing:
        out.append(f"**WARNING: {len(missing)} evidence ref(s) unresolved.**")
    return "\n".join(out)

"""insights.py — Session analytics.

Reads the engagement audit tape + sessions (NO LLM calls) and reports:
  tools — call counts per tool, OK vs refused/error split
  patterns — most-touched targets, top failure reasons
  sessions — activity by day (from audit timestamps)
  budget — audit lines per session-file (proxy for effort)

`aura insights` (engagement-scoped, read-only).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def _audit_lines(eng: Path) -> list[dict]:
    p = Path(eng) / "audit.jsonl"
    out = []
    if not p.exists():
        return out
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def collect(eng: Path) -> dict:
    recs = _audit_lines(eng)
    tools: Counter = Counter()
    ok: Counter = Counter()
    refused: Counter = Counter()
    targets: Counter = Counter()
    failures: Counter = Counter()
    days: Counter = Counter()
    for r in recs:
        call = r.get("call", "?")
        status = r.get("status", "?")
        tools[call] += 1
        if status == "OK":
            ok[call] += 1
            t = r.get("target")
            if t:
                targets[t.split("/")[0][:40]] += 1
        elif status.startswith("REFUSED") or status.startswith("TOOL_ERROR"):
            refused[call] += 1
            failures[status.split(":")[0]] += 1
        ts = r.get("ts")
        if ts:
            try:
                days[datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")] += 1
            except (ValueError, OSError, OverflowError):
                pass
    return dict(
        total_calls=len(recs),
        tools=tools.most_common(10),
        ok=dict(ok), refused=dict(refused),
        top_targets=targets.most_common(8),
        failures=failures.most_common(5),
        days=days.most_common(14),
    )


def render(info: dict) -> str:
    lines = [f"insights — {info['total_calls']} audited calls:"]
    if not info["total_calls"]:
        lines.append("  (no audit tape yet — run a task first)")
        return "\n".join(lines)
    lines.append("")
    lines.append("  tools (top 10):")
    for call, n in info["tools"]:
        o = info["ok"].get(call, 0)
        r = info["refused"].get(call, 0)
        lines.append(f"    {call:22s} {n:>5d}  (ok {o}, refused/err {r})")
    if info["top_targets"]:
        lines.append("  most-touched targets:")
        for t, n in info["top_targets"]:
            lines.append(f"    {t:40s} {n:>4d} calls")
    if info["failures"]:
        lines.append("  failure reasons:")
        for f, n in info["failures"]:
            lines.append(f"    {f:30s} {n:>4d}")
    if info["days"]:
        lines.append("  activity:")
        for d, n in info["days"]:
            bar = "▰" * min(n, 40)
            lines.append(f"    {d}  {bar} {n}")
    return "\n".join(lines)

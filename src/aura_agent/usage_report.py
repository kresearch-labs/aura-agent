"""usage_report.py — what Aura's runs used and cost, over days.

Aura keeps a receipt per run — this totals them: per day or per model, this
project or all of the profile's projects; cost stays "unknown" for a model
without a price (never guessed — `aura model price` sets one).
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _receipts(dirs: list[Path]) -> list[dict]:
    out = []
    for d in dirs:
        for p in sorted((Path(d) / "receipts").glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def _since(spec: str | None) -> float:
    if not spec:
        return 0.0
    from .history import parse_when       # "7d", "2w", "today", a date
    return parse_when(spec) or 0.0


def table(dirs: list[Path], since: str | None = None, by: str = "day") -> list[str]:
    start = _since(since)
    rows: dict = {}
    for r in _receipts(dirs):
        ts = float(r.get("started_ts") or 0)
        if ts < start:
            continue
        key = (time.strftime("%Y-%m-%d", time.localtime(ts)) if by == "day"
               else r.get("model") or "(model not recorded)")
        t = r.get("tokens") or {}
        row = rows.setdefault(key, {"runs": 0, "in": 0, "cached": 0, "out": 0,
                                    "cost": 0.0, "unknown": 0})
        row["runs"] += 1
        row["in"] += int(t.get("prompt") or 0)
        row["cached"] += int(t.get("cached") or 0)
        row["out"] += int(t.get("completion") or 0)
        c = r.get("cost_usd")
        if isinstance(c, (int, float)):
            row["cost"] += c
        else:
            row["unknown"] += 1
    if not rows:
        return ["no runs recorded" + (f" since {since}" if since else "")]
    head = f"{'day' if by == 'day' else 'model':<28}{'runs':>6}{'input':>12}{'cached':>12}{'output':>11}{'cost':>12}"
    lines = [head, "-" * len(head)]
    tot = {"runs": 0, "in": 0, "cached": 0, "out": 0, "cost": 0.0, "unknown": 0}
    for k in sorted(rows):
        r = rows[k]
        for f in tot:
            tot[f] += r[f]
        lines.append(_line(str(k)[:27], r))
    lines.append("-" * len(head))
    lines.append(_line("total", tot))
    if tot["unknown"]:
        lines.append(f"({tot['unknown']} run(s) on a model with no price — cost unknown, not counted; "
                     "set one: aura model price)")
    return lines


def _line(label: str, r: dict) -> str:
    cost = f"${r['cost']:.4f}" if r["runs"] > r["unknown"] else "unknown"
    return f"{label:<28}{r['runs']:>6}{r['in']:>12,}{r['cached']:>12,}{r['out']:>11,}{cost:>12}"


def project_dirs(all_projects: bool, here: Path) -> list[Path]:
    if not all_projects:
        return [here]
    from .paths import profile_dir
    root = profile_dir() / "projects"
    return [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []


def cache_lines(dirs: list[Path], since: str | None = None) -> list[str]:
    """`aura usage --cache`: the last run, then every run in the period —
    how much of each call was the same as the call before, and what broke it."""
    from .cache_inspect import report
    start = _since(since)
    runs = [r for r in _receipts(dirs) if r.get("cache") and float(r.get("started_ts") or 0) >= start]
    if not runs:
        return ["no runs with cache records" + (f" since {since}" if since else "")
                + " (recorded from this version on)"]
    runs.sort(key=lambda r: float(r.get("started_ts") or 0))
    last = runs[-1]
    out = [f"last run — {str(last.get('task') or '')[:60]}"] + ["  " + x for x in report(last["cache"])]
    if len(runs) > 1:
        tot: dict = {}
        for r in runs:
            c = r["cache"]
            for k in ("calls", "sent", "kept", "server_cached", "server_prompt", "server_reports"):
                tot[k] = tot.get(k, 0) + int(c.get(k) or 0)
            for why, b in (c.get("breaks") or {}).items():
                t = tot.setdefault("breaks", {}).setdefault(why, {"n": 0, "resent": 0})
                t["n"] += b["n"]
                t["resent"] += b["resent"]
        tot.setdefault("breaks", {})
        out += ["", f"all {len(runs)} runs" + (f" since {since}" if since else "")] + ["  " + x for x in report(tot)]
    return out

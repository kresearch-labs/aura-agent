"""audit_chain.py — the chain view. `aura audit` / chat audit renders the
audit.jsonl event chain as a narrative investigation timeline (action → reason
→ artifact → proof), SAME vocabulary the run view used (one event schema,
versioned — the investigation interface).

Pre-graded-autonomy question ANSWERED (open item): audit.jsonl already records
ts/call/args_hash/target/status — a fixed, versioned event schema. Sufficient
for the chain view; graded autonomy (G0-G3) will EXTEND the schema additively,
never break it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _load(eng) -> list[dict]:
    p = Path(eng) / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        out.append(r)
    return out


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "??:??"


def chain_summary(rows: list[dict]) -> list[dict]:
    """Per-target rollup: {target, ok, blocked, tools}."""
    rollup: dict[str, dict] = {}
    for r in rows:
        t = r.get("target") or "(none)"
        e = rollup.setdefault(t, {"target": t, "ok": 0, "blocked": 0,
                                  "tools": set()})
        if str(r.get("status", "")).startswith("OK"):
            e["ok"] += 1
        elif str(r.get("status", "")).startswith("REFUSED"):
            e["blocked"] = e.get("blocked", 0) + 1
        if r.get("call"):
            e["tools"] = e.get("tools", set()) | {r["call"]}
    for e in rollup.values():
        e["tools"] = sorted(e["tools"])
    return list(rollup.values())


def chain_view(eng, limit: int = 40) -> str:
    """Narrative timeline — what it did, to what, what survived."""
    rows = _load(eng)
    if not rows:
        return "no actions recorded yet — the audit chain fills as the agent works"
    ok_n = sum(1 for r in rows if str(r.get("status", "")).startswith("OK"))
    blocked_n = sum(1 for r in rows
                    if str(r.get("status", "")).startswith("REFUSED"))
    head = f"audit chain: {len(rows)} actions · {ok_n} ok · {blocked_n} blocked"
    lines = [head]
    for r in rows[-limit:]:
        ts = _fmt_ts(r.get("ts"))
        call = r.get("call", "?")
        target = r.get("target") or "-"
        status = str(r.get("status", ""))
        if status == "OK":
            lines.append(f"  ✔ {ts}  {call:14s} {target}")
        elif status.startswith("REFUSED_SCOPE"):
            lines.append(f"  ✘ {ts}  {call:14s} {target} — outside what "
                         "I'm allowed to touch")
        elif status.startswith("REFUSED"):
            reason = status.split(":", 1)[-1] if ":" in status else ""
            extra = f" — {reason[:48]}" if reason and reason != status else ""
            lines.append(f"  ✘ {ts}  {call:14s} {target} — blocked{extra}")
        else:
            lines.append(f"  · {ts}  {call:14s} {target} — {status[:40]}")
    if len(rows) > limit:
        lines.append(f"  … +{len(rows) - limit} earlier actions")
    lines.append("full detail: the notebook's audit.jsonl (fixed schema, "
                 "args hashed — secrets never stored)")
    return "\n".join(lines)

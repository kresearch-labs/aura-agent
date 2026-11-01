"""cache_inspect.py — where the prompt stops being the same between calls.

One changed byte near the start — a time stamp, a reordered tool list, a
rewritten summary — and everything after it is processed again (a measured 98.7
% hit rate fell to 0.7 % from a time stamp at the top). Aura compares each
request with the one before it on the client side — any server, cache reported
or not — and names the first thing that changed. kv_cache.py lays the prompt
out; this checks it held.
"""
from __future__ import annotations

import hashlib
import json


def fingerprint(messages: list[dict], tools: list[dict] | None) -> list[tuple]:
    """(label, chars, hash) for the tool list and each message, in send order
    (servers put tools before the messages)."""
    out = []
    t = json.dumps(tools or [], sort_keys=True, default=str)
    out.append(("tools", len(t), hashlib.blake2b(t.encode(), digest_size=8).hexdigest()))
    for m in messages:
        s = json.dumps({k: m.get(k) for k in ("role", "content", "tool_calls", "tool_call_id", "name")},
                       sort_keys=True, default=str)
        out.append((str(m.get("role") or "?"), len(s),
                    hashlib.blake2b(s.encode(), digest_size=8).hexdigest()))
    return out


def compare(prev: list[tuple] | None, cur: list[tuple]) -> dict:
    """{'sent': chars, 'kept': chars the same from the start, 'why': None when
    this call only added to the end, else what changed first}."""
    sent = sum(c for _, c, _ in cur)
    if not prev:
        return {"sent": sent, "kept": 0, "why": "first call"}
    # the per-call notes (journal, status) ride at the very end on purpose
    # (kv_cache rule 4): new messages go in before them, so only that tail
    # is read again — expected, not a break
    end = len(prev)
    while end > 1 and prev[end - 1][0] == "system":
        end -= 1
    if end < len(prev) and cur[:end] == prev[:end]:
        prev = prev[:end]
    kept, i = 0, 0
    while i < len(prev) and i < len(cur) and prev[i] == cur[i]:
        kept += cur[i][1]
        i += 1
    if i == len(prev):
        return {"sent": sent, "kept": kept, "why": None}      # Only appended
    if i == 0:
        why = "tool list changed"
    elif prev[i][0] == "system" or (i < len(cur) and cur[i][0] == "system"):
        why = "system prompt changed"
    elif len(cur) < len(prev):
        why = "older messages summarised or trimmed"
    else:
        why = f"an earlier {cur[i][0] if i < len(cur) else prev[i][0]} message changed"
    return {"sent": sent, "kept": kept, "why": why}


def fold(acc: dict | None, step: dict, server_cached: int | None, prompt_tokens: int = 0) -> dict:
    """Add one call to a run's totals (kept in the receipt)."""
    acc = acc or {"calls": 0, "sent": 0, "kept": 0, "breaks": {},
                  "server_cached": 0, "server_prompt": 0, "server_reports": 0}
    acc["calls"] += 1
    acc["sent"] += step["sent"]
    acc["kept"] += step["kept"]
    if step["why"] and step["why"] != "first call":
        b = acc["breaks"].setdefault(step["why"], {"n": 0, "resent": 0})
        b["n"] += 1
        b["resent"] += step["sent"] - step["kept"]
    if server_cached is not None:
        acc["server_reports"] += 1
        acc["server_cached"] += int(server_cached)
        acc["server_prompt"] += int(prompt_tokens or 0)
    return acc


def report(acc: dict | None) -> list[str]:
    """Plain lines for `aura usage --cache` and the run summary."""
    if not acc or not acc.get("calls"):
        return ["no calls recorded"]
    pct = 100 * acc["kept"] / max(1, acc["sent"])
    lines = [f"{acc['calls']} calls · {pct:.0f}% of what was sent was the same as the call before "
             f"(~{(acc['sent'] - acc['kept']) // 4:,} tokens had to be read again)"]
    if acc.get("server_reports"):
        sp = acc["server_prompt"]
        lines.append(f"server says: {acc['server_cached']:,} of {sp:,} prompt tokens came from its cache"
                     + (f" ({100 * acc['server_cached'] / sp:.0f}%)" if sp else ""))
    else:
        lines.append("server says: nothing (it doesn't report its cache) — the line above is Aura's own count")
    for why, b in sorted(acc["breaks"].items(), key=lambda kv: -kv[1]["resent"]):
        lines.append(f"  {why}: {b['n']}× — ~{b['resent'] // 4:,} tokens read again")
    if not acc["breaks"]:
        lines.append("  the start never changed — every call only added to the end")
    return lines

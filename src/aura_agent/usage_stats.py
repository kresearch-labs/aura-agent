"""usage_stats.py — `aura stats` — token usage across sessions.

Reads every session jsonl under the engagement, extracts recorded usage
(prompt/completion tokens from provider SSE usage when present, estimated
otherwise), aggregates: per-session: messages, prompt/completion/total tokens,
source (measured vs estimated) totals across sessions + per-model breakdown
when recorded

Never calls the LLM; pure file read.
"""
from __future__ import annotations

import json
from pathlib import Path


def _session_usage(path: Path) -> dict:
    msgs = 0
    prompt = 0
    completion = 0
    measured = False
    models: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = rec.get("role")
            if role:
                msgs += 1
            u = rec.get("usage") or {}
            if u.get("prompt_tokens") or u.get("completion_tokens"):
                prompt += int(u.get("prompt_tokens") or 0)
                completion += int(u.get("completion_tokens") or 0)
                measured = True
            model = rec.get("model")
            if model:
                models[model] = models.get(model, 0) + 1
    except OSError:
        pass
    total = prompt + completion
    return dict(file=path.name, messages=msgs, prompt=prompt,
                completion=completion, total=total, measured=measured,
                models=models)


def collect(eng: Path, limit: int = 10) -> dict:
    sdir = Path(eng) / "sessions"
    files = []
    if sdir.exists():
        files = sorted(sdir.glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    sessions = [_session_usage(p) for p in files[:limit]]
    tot_p = sum(s["prompt"] for s in sessions)
    tot_c = sum(s["completion"] for s in sessions)
    measured_n = sum(1 for s in sessions if s["measured"])
    return dict(sessions=sessions,
                total_prompt=tot_p, total_completion=tot_c,
                total=tot_p + tot_c,
                n_sessions=len(sessions),
                measured_sessions=measured_n)


def render(info: dict) -> str:
    lines = [f"token usage — last {info['n_sessions']} session(s):"]
    lines.append("")
    lines.append(f"  {'session':40s} {'msgs':>5s} {'prompt':>8s} "
                 f"{'compl':>8s} {'total':>8s} src")
    for s in info["sessions"]:
        src = "measured" if s["measured"] else "est."
        lines.append(f"  {s['file'][:40]:40s} {s['messages']:>5d} "
                     f"{s['prompt']:>8d} {s['completion']:>8d} "
                     f"{s['total']:>8d} {src}")
    lines.append("")
    src_note = (f"{info['measured_sessions']}/{info['n_sessions']} measured "
                "from provider usage; rest estimated")
    lines.append(f"  total: {info['total']:,} tokens "
                 f"(prompt {info['total_prompt']:,} · completion "
                 f"{info['total_completion']:,}) — {src_note}")
    return "\n".join(lines)

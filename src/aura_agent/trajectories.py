"""trajectories.py — trajectory export (SFT-ready run records).

`aura export trajectories` turns EVERYTHING the agent has ever done — operator
chat turns, sub-agent runs, mediator audit verdicts — into structured,
deterministic SFT records:

  {kind: "chat"|"role",
   task, messages: [{role, content?, tool_calls?, tool_call_id?, name}],
   tool_trace, meta: {engagement, refs, counts}}

Sources:
  engagement/sessions/*.jsonl (operator chat + role child sessions)
  engagement/delegates/*/sessions/ (role children, attributed by role)
  engagement/audit.jsonl + delegates/*/audit.jsonl (verdicts)

Dedup by content hash so re-exports never double-count. Privacy:
engagement-relative refs only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path



def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # Crash-torn tail — skip
    return out


def _session_messages(eng: Path, session_ref: str | None) -> list[dict]:
    """Replay a session jsonl into clean SFT messages."""
    if not session_ref:
        return []
    p = Path(session_ref)
    if not p.is_absolute():
        p = Path(eng) / p
    msgs = []
    for rec in _read_jsonl(p):
        msg = {k: rec[k] for k in ("role", "content", "tool_calls",
                                   "tool_call_id", "name")
               if rec.get(k) is not None}
        if msg.get("role"):
            msgs.append(msg)
    return msgs


def _audit_calls(eng: Path, audit_ref: str | None) -> list[dict]:
    if not audit_ref:
        return []
    p = Path(audit_ref)
    if not p.is_absolute():
        p = Path(eng) / p
    return [{"call": r.get("call"), "args_hash": r.get("args_hash"),
             "status": r.get("status"), "ts": r.get("ts")}
            for r in _read_jsonl(p)]


def _content_hash(record: dict) -> str:
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _chat_trajectories(eng: Path) -> list[dict]:
    """Top-level operator sessions → chat trajectories (skip role children;
    those are exported separately with role attribution)."""
    out = []
    for sp in sorted((eng / "sessions").glob("*.jsonl")):
        recs = _read_jsonl(sp)
        messages = [m for m in _session_messages(eng, str(sp))
                    if m.get("role") in ("user", "assistant", "tool",
                                         "system")]
        if len(messages) < 2:
            continue                       # empty/touched-only session
        task = next((str(m.get("content", ""))[:400] for m in messages
                     if m["role"] == "user"), "")
        record = {
            "kind": "chat",
            "task": task,
            "messages": messages,
            "tool_trace": _audit_calls(eng, str(eng / "audit.jsonl")),
            "meta": {"session": sp.name, "turns": len(messages)},
        }
        record["hash"] = _content_hash(record)
        out.append(record)
    return out


def _role_trajectories(eng: Path) -> list[dict]:
    """Delegate child sessions with their role + parent root attribution."""
    out = []
    for sp in sorted((eng / "delegates").glob("*/sessions/*.jsonl")):
        role_dir = sp.parent.parent
        role_name = role_dir.name.rsplit("-", 1)[0].replace("_", "-")
        messages = _session_messages(eng, str(sp))
        if len(messages) < 2:
            continue
        task = next((str(m.get("content", ""))[:400] for m in messages
                     if m["role"] == "user"), "")
        record = {
            "kind": "role",
            "role": role_name,
            "task": task,
            "messages": messages,
            "tool_trace": _audit_calls(eng, str(role_dir / "audit.jsonl")),
            "meta": {"session": sp.name, "delegate_dir": role_dir.name,
                     "turns": len(messages)},
        }
        record["hash"] = _content_hash(record)
        out.append(record)
    return out


def compile_trajectories(eng: Path) -> list[dict]:
    """All trajectories, deduped by content hash, deterministic order."""
    eng = Path(eng)
    seen: dict[str, dict] = {}
    for record in _chat_trajectories(eng) + _role_trajectories(eng):
        seen.setdefault(record["hash"], record)
    # remove the helper hash field from the payload? No — keep it: it IS
    # the reproducibility stamp. Deterministic order: kind, then hash.
    return sorted(seen.values(),
                  key=lambda r: (r["kind"], r.get("role", ""),
                                 r["hash"]))


def export_trajectories(eng: Path, dest: Path | None = None) -> Path:
    """Write trajectories.jsonl — the SFT dataset artifact."""
    eng = Path(eng)
    dest = dest or (eng / "trajectories.jsonl")
    dest.parent.mkdir(parents=True, exist_ok=True)
    records = compile_trajectories(eng)
    with dest.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return dest


def dataset_stats(path: Path) -> dict:
    """pass@k-era metrics about the exported dataset itself."""
    records = [json.loads(l) for l in
               Path(path).read_text(encoding="utf-8").splitlines()
               if l.strip()]
    by_kind: dict[str, int] = {}
    turns = 0
    tool_calls = 0
    for r in records:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        turns += len(r.get("messages", []))
        tool_calls += len(r.get("tool_trace", []))
    return {"records": len(records), "by_kind": by_kind,
            "message_turns": turns, "tool_calls": tool_calls}

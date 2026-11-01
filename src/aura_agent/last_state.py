"""last_state.py —: the shared read model + Previously-On card.

.last-state is ONE JSON object auto-maintained at every turn end + checkpoint;
rendered identically by chat startup, the arrival router, and the dashboard
(single-registry principle).

Previously-On card = SBAR shape: Situation → Background → Assessment (verified)
→ Recommendation. Cut from REAL state, 3-5 lines. Secrets scrubbed before disk
(invariant). Skippable per session.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_LAST_STATE = ".last-state"


def update_last_state(eng, data: dict) -> None:
    """Merge-write the state object; stamp last_ts; scrub secrets."""
    import time as _time
    from pathlib import Path as _P
    eng = _P(eng)
    data = dict(data)
    data.setdefault("last_ts", _time.time())   # R: age needs a stamp
    try:
        from .secrets_guard import sanitize_output
        blob = json.dumps(data, ensure_ascii=False, default=str)
        clean = sanitize_output(blob)
        payload = json.loads(clean)   # Re-parse the SCRUBBED text
    except Exception:
        payload = {k: v for k, v in data.items()
                   if not any(t in str(k).lower()
                              for t in ("key", "token", "secret"))}
    p = eng / _LAST_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                 encoding="utf-8")


def read_last_state(eng) -> dict | None:
    from pathlib import Path as _P
    p = _P(eng) / _LAST_STATE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def checkpoint_line(eng) -> str:
    st = read_last_state(eng) or {}
    n = st.get("findings_total", 0)
    proven = st.get("findings_proven", 0)
    sess = st.get("last_session", "?")
    cid = st.get("last_session_id") or ""
    # 76-col cap: the reply lands after the prompt bracket (~34 cols) —
    # two lines, findings get their own
    short = f"#{cid}" if cid else (sess.rsplit("-", 2)[-1] if "-" in sess else sess)
    return (f"saved · {short} — `aura resume {cid or 'latest'}` continues it"
            + (f"\n  results: {n} ({proven} proven)" if n else ""))


# ---------------------------------------------------------------------------
# Resume tradeoff, honest edges, named-session promotion.
# ---------------------------------------------------------------------------

ROT_THRESHOLD = 0.65   # Context-rot threshold governs the dialog


def resume_tradeoff(session_info: dict) -> str:
    """R: large-session tradeoff as ONE inline line (not a numbered dialog) —
    merges into the Previously-On card. '' for small sessions.
    """
    n = int(session_info.get("messages", 0))
    tok = int(session_info.get("tokens_est", 0))
    ctx = int(session_info.get("ctx", 0)) or 1
    if n < 100 and tok / ctx < ROT_THRESHOLD:
        return ""
    return (f"large session (~{tok // 1000}k tokens/turn) — "
            "resume compact-first? (c = yes, Enter = full)")


def resume_honest_limits() -> str:
    """Stated crash edges — trust through stated limits, not implied perfection.
    """
    return ("resume restores: the conversation, results and checkpoints. "
            "NOT restored: an in-flight tool call's output lost at the "
            "interruption point.")


def rewind_honest_limits() -> str:
    return ("rewind reverts conversation turns and shadow-git file "
            "snapshots. NOT reverted: shell/bash side-effects outside the "
            "workspace (installed packages, remote calls).")


def promotion_hint(session_name: str, n_messages: int,
                   threshold: int = 25) -> str:
    """One-time suggestion to name an unnamed growing session."""
    if session_name or n_messages < threshold:
        return ""
    return ("this session is getting long — worth naming? "
            "try: /rename login-bug-fix")


def previously_on(eng, age_days: float = 0.0, skip: bool = False) -> str:
    """Single-gate card from.last-state + findings on disk. '' when nothing to
    recap (first arrival) or the user skipped it.
    """
    if skip:
        return ""
    st = read_last_state(eng)
    if st is None:
        return ""
    from pathlib import Path as _P
    findings = []
    fp = _P(eng) / "findings.jsonl"
    if fp.exists():
        for ln in fp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("id") and not any(x["id"] == r["id"] for x in findings):
                findings.append(r)
    proven = sum(1 for f in findings if f.get("proof"))
    # R: human age — days over 1, hours under 1 day, minutes under an hour; "5d
    # ago" reads at a glance (the old card showed raw age_days only when >0, so
    # same-day returns showed nothing).
    age = ""
    if st.get("last_ts"):
        try:
            if age_days >= 1:
                age = f" ({int(age_days)}d ago)"
            elif age_days * 24 >= 1:
                age = f" ({int(age_days * 24)}h ago)"
            elif age_days > 0:
                age = f" ({max(1, int(age_days * 1440))}m ago)"
        except (TypeError, ValueError):
            age = ""
    # R: the card IS the single gate — plain-words recap, one choice line. No
    # SBAR jargon headers, no separate dialogs.
    s1 = (f"last time ({age.strip(' ()') or 'recently'})"
          if age else "last time")
    s2 = (f"you were: {st.get('last_task', '?')[:70]}"
          f"  ·  chat {st.get('last_session', '?')}")
    s3 = (f"results: {len(findings)} ({proven} proven)" if findings else "")
    s4 = "/resume picks a chat (each with what it worked on) · or just type"
    return "\n".join(x for x in (s1, s2, s3, s4) if x)

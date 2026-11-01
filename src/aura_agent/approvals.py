"""approvals.py — the request_approval primitive.

Durable approval requests with the full security stack: - OPAQUE random ids
(nothing executable in callback data) - args stored as DIGEST only (nothing
executable stored/travels) - durable file store (.approvals/pending/) —
survives restarts - ATOMIC exactly-once consume (read-verify-delete) - expiry
(default 300s countdown) — expired = never executes - TOCTOU revalidation:
agent still blocked + same args digest between tap and execution -
three-outcome metadata: outcome + reason + approver recorded
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_S = 300.0


def _digest_args(args: dict) -> str:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class PendingApproval:
    id: str = ""
    tool: str = "?"
    args_digest: str = ""
    grade: str = "G2"
    target: str = "?"
    intent: str = ""
    args: dict = field(default_factory=dict)   # test/direct construction only
    created_ts: float = field(default_factory=lambda: round(time.time(), 3))
    expires_ts: float = 0.0
    status: str = "pending"

    def __post_init__(self):
        if not self.args_digest and self.args:
            self.args_digest = _digest_args(self.args)
        if not self.id:
            self.id = secrets.token_hex(8)

    @classmethod
    def create(cls, eng, req, ttl_s: float = DEFAULT_TTL_S
               ) -> "PendingApproval":
        """Persist a new pending approval; returns it (id set).
        req: PendingApproval OR dict {tool, args|args_digest, grade,
        target, intent}."""
        now = time.time()
        if isinstance(req, PendingApproval):
            req = {"tool": req.tool, "args": req.args,
                   "grade": req.grade, "target": req.target,
                   "intent": req.intent}
        digest = req.get("args_digest") or _digest_args(req.get("args") or {})
        pa = cls(id=secrets.token_hex(8), tool=req.get("tool", "?"),
                 args_digest=digest, grade=req.get("grade", "G2"),
                 target=req.get("target", "?"), intent=req.get("intent", ""),
                 created_ts=round(now, 3),
                 expires_ts=round(now + ttl_s, 3))
        d = Path(eng) / "pending"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{pa.id}.json").write_text(
            json.dumps({k: getattr(pa, k) for k in (
                "id", "tool", "args_digest", "grade", "target", "intent",
                "created_ts", "expires_ts", "status")},
                ensure_ascii=False, indent=1), encoding="utf-8")
        return pa

    def card(self) -> str:
        """CONCLUSION-level disclosure: action + grade + target + countdown. Raw
        args NEVER render (digest-only storage).
        """
        left = max(0, int(self.expires_ts - time.time()))
        return (f"APPROVAL {self.grade} · {self.tool} → {self.target} "
                f"(expires in {left}s)")


def _p(eng, approval_id: str) -> Path:
    return Path(eng) / "pending" / f"{approval_id}.json"


def request_approval(eng, req: PendingApproval,
                     ttl_s: float = DEFAULT_TTL_S) -> PendingApproval:
    return PendingApproval.create(eng, req, ttl_s=ttl_s)


def load_pending(eng) -> list[dict]:
    d = Path(eng) / "pending"
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def consume_approval(eng, approval_id: str, outcome: str,
                     reason: str = "", approver: str = "") -> dict | None:
    """ATOMIC exactly-once consume: verify pending → delete → record. Expired
    approvals are consumed as EXPIRED (never execute).
    """
    p = _p(eng, approval_id)
    if not p.exists():
        return {"status": "already_consumed"}
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # atomic: delete first (rename = atomic on POSIX + Windows same-dir)
    p.unlink()
    if time.time() > rec.get("expires_ts", 0):
        rec["status"] = "expired"
        rec["outcome"] = "expired"
        return rec
    rec["status"] = "consumed"
    rec["outcome"] = {"approve": "approved", "reject": "rejected",
                      "escalate": "escalated"}.get(
        outcome.lower(), outcome.lower())
    rec["reason"] = reason
    rec["approver"] = approver
    rec["consumed_ts"] = round(time.time(), 3)
    d = Path(eng) / "decided"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{approval_id}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


def shown_args(args: dict, limit: int = 240) -> dict:
    """What the operator sees before approving: the real arguments (a
    command must be read to be approved), secret-scrubbed, long values
    cut with their size noted."""
    try:
        from .secrets_guard import scrub_secrets
    except Exception:                                # noqa: BLE001
        scrub_secrets = None
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        try:
            s = scrub_secrets(s) if scrub_secrets else "[hidden]"
        except Exception:                            # noqa: BLE001
            s = "[hidden — scrub failed]"
        cap = 1200 if k == "body" else limit     # A skill body is read, not skimmed
        if len(s) > cap:
            s = s[:cap] + f"… ({len(s)} chars)"
        out[k] = s
    return out


_GRADE_WORDS = {"G0": "read-only", "G1": "changes files here (undoable)",
                "G2": "runs a command", "G3": "can't be undone or reaches outside"}


def card_lines(req: dict) -> list[str]:
    """The question every chat screen shows: what, why, the real
    arguments, and the possible answers."""
    left = max(0, int(req.get("expires_ts", 0) - time.time()))
    head = req.get("heading") or (
        f"{_GRADE_WORDS.get(req.get('grade'), req.get('grade'))} "
        f"({req.get('grade')})")
    lines = [f"? approve {req.get('tool')} — {head}",
             f"  why asked: {req.get('why')}"]
    if req.get("detail"):
        lines.append(f"  {req['detail']}")
    for k, v in (req.get("args") or {}).items():
        v = str(v).replace("\r", "").replace("\n", "⏎ ")
        lines.append(f"  {k}: {v}")
    if req.get("hint"):
        lines.append(f"  note: {req['hint']}")
    opts = "y = yes, once"
    if req.get("can_always"):
        opts += f" · a = always for {req.get('target')}"
    opts += (f" · n = no (n <why> tells the agent why)   "
             f"(expires in {left // 60}m{left % 60:02d}s)")
    lines.append("  " + opts)
    return lines


def parse_answer(text: str, req: dict) -> str | None:
    """Operator reply → "once" | "always" | "deny"; None = not an answer.
    "n <why>" (or "no, <why>") denies WITH a reason — kept on the request
    as req["deny_reason"] so the model is told why, not just "no"."""
    raw = (text or "").strip().lstrip("/")
    t = raw.lower()
    if t in ("y", "yes", "approve", "ok"):
        return "once"
    if t in ("a", "always") and req.get("can_always"):
        return "always"
    if t in ("n", "no", "deny", "reject"):
        return "deny"
    word, _, rest = raw.partition(" ")
    if word.lower().rstrip(",:;-") in ("n", "no", "deny", "reject") \
            and rest.strip():
        req["deny_reason"] = rest.strip().lstrip(",:;- ")[:300]
        return "deny"
    return None


def ask_operator(eng, approver, tool: str, args: dict, grade: str,
                 target: str, why: str, allow_always: bool = True,
                 detail: str | None = None,
                 heading: str | None = None,
                 hint: str | None = None,
                 on_always=None) -> tuple[bool, str]:
    """Ask the operator about one call, using the primitives above — a durable
    pending record, exactly-once consume (expired = no), and the re-check that
    the call about to run is the one that was approved.

    `approver(request) -> "once" | "always" | "deny"` is supplied by the chat
    surface. Returns (run_it, note). "always" saves a permission for this tool
    + target when that is allowed (never for G3), or calls on_always instead
    when given (e.g. save a folder, outside.py).
    """
    import copy
    digest = _digest_args(args or {})
    # An approver that waits for someone keeps the question open as long as it
    # waits; an interactive one uses the normal countdown
    pa = PendingApproval.create(eng, {
        "tool": tool, "args_digest": digest, "grade": grade,
        "target": target, "intent": why},
        ttl_s=getattr(approver, "ttl_s", DEFAULT_TTL_S))
    can_always = (allow_always and grade in ("G1", "G2")
                  and target not in ("", "?", "*", None))
    request = {"id": pa.id, "tool": tool, "grade": grade, "target": target,
               "why": why, "args": shown_args(copy.deepcopy(args or {})),
               # long form for screens that preview the change (a diff);
               # still secret-scrubbed
               "preview_args": shown_args(copy.deepcopy(args or {}), limit=200_000),
               "can_always": can_always, "expires_ts": pa.expires_ts,
               "detail": detail, "heading": heading, "hint": hint}
    try:
        answer = str(approver(request) or "deny").lower()
    except Exception:                                # noqa: BLE001
        answer = "deny"                              # A broken prompt is a no
    if answer == "always" and not can_always:
        answer = "once"
    why_no = str(request.get("deny_reason") or "").strip()
    rec = consume_approval(eng, pa.id,
                           "approve" if answer in ("once", "always")
                           else "reject",
                           reason=(f"deny: {why_no}" if why_no and
                                   answer not in ("once", "always")
                                   else answer),
                           approver="operator")
    if not rec or rec.get("outcome") != "approved":
        why_not = "expired" if (rec or {}).get("outcome") == "expired" \
            else "declined"
        note = f"operator {why_not} (approval {pa.id})"
        if why_no and why_not == "declined":
            note += f' — their reason: "{why_no}"'
        return False, note
    # safety re-check: the call that runs is exactly the one approved
    if not revalidate(eng, pa.id, tool, _digest_args(args or {})):
        return False, f"call changed after approval {pa.id} — not run"
    note = f"approved once ({pa.id})"
    if answer == "always" and on_always is not None:
        try:
            note = f"approved; {on_always()}"
        except (ValueError, OSError) as e:
            note = f"approved once ({pa.id}); not saved: {e}"
    elif answer == "always":
        try:
            from .autonomy import persist_grant
            g = persist_grant(eng, grade, tool, str(target),
                              label=f"from approval {pa.id}")
            note = f"approved; saved permission {g['id']} for {tool} → {target}"
        except (ValueError, OSError) as e:
            note = f"approved once ({pa.id}); not saved: {e}"
    return True, note


def revalidate(eng, approval_id: str, tool: str, args_digest: str) -> bool:
    """TOCTOU: between tap and execution, confirm the SAME call is still
    the one approved. Any drift → False (never execute on mismatch)."""
    rec = None
    p = _p(eng, approval_id)
    if p.exists():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rec = None
    else:
        d = Path(eng) / "decided" / f"{approval_id}.json"
        if d.exists():
            try:
                rec = json.loads(d.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rec = None
    if rec is None:
        return False
    return (rec.get("tool") == tool
            and rec.get("args_digest") == args_digest)


# ------------------------------------------- unattended: stop and wait --
WAIT_TTL_S = 365 * 86400.0      # The question stays open while Aura waits


def answer_path(eng, approval_id: str) -> Path:
    return Path(eng) / "pending" / f"{approval_id}.answer"


def waiting_approver(eng, say=print, poll_s: float = 1.0):
    """The kill switch (.kill) ends the wait as a "no"."""
    def approver(request: dict) -> str:
        rid = request.get("id", "?")
        say(f"\n[waiting for approval] {request.get('tool')} → "
            f"{request.get('target')} ({request.get('grade')})\n"
            f"  approve:  aura approve {rid}\n"
            f"  reject:   aura reject {rid} [reason]\n"
            "  Nothing else runs until it is answered.")
        lst = None
        try:
            from . import gateway_approvals as GA
            from .notify import pick_tier
            if pick_tier("approval_needed") == "push" and GA.channels():
                for line in GA.push(eng, request):
                    say("  " + line)
                lst = GA.listen_for(request)
        except Exception as e:                       # noqa: BLE001
            say(f"  (not sent to your phone: {str(e)[:80]})")
        ans = answer_path(eng, rid)
        while True:
            if (Path(eng) / ".kill").exists():
                if lst is not None:
                    lst.done(rid)
                return "deny"
            if ans.exists():
                if lst is not None:
                    lst.done(rid)
                try:
                    rec = json.loads(ans.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    rec = {}
                ans.unlink(missing_ok=True)
                if rec.get("answer") == "approve":
                    say(f"  approved ({rid}) — continuing")
                    return "once"
                request["deny_reason"] = str(rec.get("reason") or "")[:300]
                say(f"  rejected ({rid}) — continuing without it")
                return "deny"
            time.sleep(poll_s)
    approver.ttl_s = WAIT_TTL_S
    return approver


def find_pending(approval_id: str, first=None) -> Path | None:
    """The notebook holding this pending approval: `first` (the current
    one), then every notebook of the active profile."""
    from .paths import no_project_dir, profile_dir
    cands = [Path(first)] if first else []
    cands += sorted((profile_dir() / "projects").glob("*"))
    cands.append(no_project_dir())
    for nb in cands:
        if _p(nb, approval_id).exists():
            return nb
    return None


def answer(approval_id: str, approve: bool, reason: str = "",
           first=None) -> str:
    """`aura approve|reject <id>`: hand the answer to the waiting run."""
    nb = find_pending(approval_id, first)
    if nb is None:
        return f"no open approval {approval_id}"
    answer_path(nb, approval_id).write_text(json.dumps(
        {"answer": "approve" if approve else "reject", "reason": reason}),
        encoding="utf-8")
    return (f"{'approved' if approve else 'rejected'} {approval_id} — the "
            "waiting run continues")


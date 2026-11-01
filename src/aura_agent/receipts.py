"""receipts.py — per-run receipt (the run's completion record).

  run-receipt spec: "Silence is not an operating state." Five-status
        vocabulary completed/no_work/blocked/failed/uncertain; sources and
        outputs hashed; secrets recorded as NAMES not values.
  DCS R+2: hash-chained receipts (prev_receipt hash), chain-break
  LEDGERMIND: the model's claim of completion is never the record;
        status comes from receipt CHECKS (anti fail-plausible).
  token spend attributed by surface (main loop vs delegate), cache
        split kept separate; JSONL-flat substrate, not OTel (still
        "Development" stability).

Zero-degradation: pure local file writes at run end; no model calls; no prompt
changes; independently revertible (delete module + 2 call sites).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


# Secret shapes scrubbed from receipt payloads (invariant: receipts carry
# secret NAMES, never values). Value-level scrubbing lives in secrets_guard.py;
# this is the receipt-side backstop.
_SECRET_SHAPES = (
    "AKIA[0-9A-Z]{16}",
    "sk-[A-Za-z0-9_-]{16,}",
    "Bearer [A-Za-z0-9._-]{8,}",
    "-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "xox[baprs]-[A-Za-z0-9-]{10,}",
    "gh[pousr]_[A-Za-z0-9]{20,}",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _scrub(text: str) -> str:
    """Replace secret-shaped substrings with a name-only marker."""
    import re
    out = text
    for pat in _SECRET_SHAPES:
        out = re.sub(pat, "[REDACTED]", out)
    return out


def _digest_obj(obj) -> str:
    return _sha(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                           default=str).encode("utf-8"))


class RunReceipt:
    """Accumulates per-run facts; written once at run end."""

    def __init__(self, eng: Path, run_id: str, task: str = ""):
        self.eng = Path(eng)
        self.run_id = run_id
        self.data: dict = {
            "schema": "aura.receipt/1",
            "run_id": run_id,
            "task": _scrub(str(task))[:200],
            "started_ts": round(time.time(), 3),
            "status": "uncertain",        # 5-status vocab; set by checks
            "steps": 0,
            "tool_calls": 0,
            "tool_errors": 0,
            "tokens": {"prompt": 0, "completion": 0, "cache_hits": 0,
                       "cache_misses": 0, "surface": "main", "source":
                       "none"},
            "cost_usd": None,             # Nullable, honest unknown
            "checks": {},                 # Name -> bool (status basis)
            "outputs": [],                # {path, sha}
            "notes": [],
        }

    # -------------------------------------------------------------- inputs
    def note(self, text: str) -> None:
        self.data["notes"].append(_scrub(str(text))[:200])

    def count_tool(self, ok: bool) -> None:
        self.data["tool_calls"] += 1
        if not ok:
            self.data["tool_errors"] += 1

    # ------------------------------------------------------------- outputs
    def record_output(self, path: Path) -> None:
        """Hash a file the run produced (content-addressed, Nettle-style)."""
        try:
            data = Path(path).read_bytes()
            self.data["outputs"].append(
                {"path": str(path), "sha": _sha(data),
                 "bytes": len(data)})
        except OSError:
            self.data["outputs"].append(
                {"path": str(path), "sha": None, "error": "unreadable"})

    # -------------------------------------------------------------- checks
    def add_check(self, name: str, ok: bool, detail: str = "") -> None:
        self.data["checks"][name] = {
            "ok": bool(ok), "detail": _scrub(detail)[:160]}

    # --------------------------------------------------------------- write
    def _derive_status(self) -> str:
        """Status from CHECKS, never from the model's claim."""
        checks = self.data["checks"]
        if not checks:
            # No checks registered: fall back to mechanical signals only.
            if self.data["tool_calls"] == 0 and self.data["steps"] <= 1:
                return "no_work"
            if self.data["tool_errors"] and \
                    self.data["tool_errors"] >= self.data["tool_calls"]:
                return "failed"
            return "uncertain"
        if all(c["ok"] for c in checks.values()):
            return "completed"
        if all(not c["ok"] for c in checks.values()):
            return "failed"
        return "uncertain"

    def finalize(self, steps: int, model_claim: str = "") -> dict:
        """Compute status, hash-link to the previous receipt, write JSON."""
        self.data["steps"] = int(steps)
        self.data["finished_ts"] = round(time.time(), 3)
        self.data["status"] = self._derive_status()
        if model_claim:
            self.data["model_claim"] = _scrub(str(model_claim))[:400]
        rdir = self.eng / "receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        # Hash chain: prev receipt's own hash (holdover: structural link,
        # tamper-EVIDENT not tamper-PROOF — no signatures in v1)
        prev = sorted(rdir.glob("*.json"))
        if prev:
            try:
                self.data["prev_receipt"] = prev[-1].name
                self.data["prev_sha"] = _sha(prev[-1].read_bytes())
            except OSError:
                pass
        # secrets: names only — scrub the whole payload once more
        blob = json.dumps(self.data, ensure_ascii=False, default=str)
        obj = json.loads(_scrub(blob))
        # A fingerprint of this receipt's own (scrubbed) content, so an edit to
        # the NEWEST receipt — which nothing links to yet — is caught
        obj["self_sha"] = _digest_obj(obj)
        (rdir / f"{self.run_id}.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return self.data


def latest_receipt(eng: Path) -> str | None:
    """Run-id of the newest receipt, or None."""
    rdir = Path(eng) / "receipts"
    files = sorted(rdir.glob("*.json")) if rdir.exists() else []
    return files[-1].stem if files else None


def render_receipt_card(eng: Path, run_id: str) -> str:
    """In-chat receipt card = receipt header + the 5-line summary. Missing receipt
    renders honestly, never fabricates.
    """
    rec = load_receipt(eng, run_id)
    if rec is None:
        return f"no receipt named {run_id} in receipts/ — /receipt (bare) shows the latest"
    rec = dict(rec)
    rec.setdefault("_eng", str(eng))
    head = f"receipt {run_id} · task: {rec.get('task', '?')[:60]}"
    return head + "\n" + render_summary(rec)


def load_receipt(eng: Path, run_id: str) -> dict | None:
    p = Path(eng) / "receipts" / f"{run_id}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_chain(eng: Path) -> dict:
    """Walk receipts/ in name order; verify each prev_sha link. Returns {ok,
    checked, breaks:[...]} — chain-break detection per.
    """
    rdir = Path(eng) / "receipts"
    files = sorted(rdir.glob("*.json")) if rdir.exists() else []
    breaks, checked, legacy = [], 0, 0
    prev_name = prev_sha = None
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            breaks.append({"receipt": f.name, "reason": "unreadable"})
            continue
        checked += 1
        own = rec.pop("self_sha", None)
        if own is None:
            legacy += 1                      # Written before self-checks
        elif own != _digest_obj(rec):
            breaks.append({"receipt": f.name, "reason": "content changed"})
        if prev_name is not None:
            got = rec.get("prev_receipt")
            want_sha = _sha_sha(prev_path := rdir / prev_name)
            if got != prev_name or rec.get("prev_sha") != want_sha:
                breaks.append({"receipt": f.name, "reason": "chain break",
                               "expected_prev": prev_name})
        prev_name, prev_path = f.name, f
    return {"ok": not breaks, "checked": checked, "breaks": breaks,
            "no_self_check": legacy}


def render_verify(result: dict) -> str:
    """Plain summary of verify_chain for chat and CLI."""
    n, b = result["checked"], result["breaks"]
    if n == 0:
        return "no receipts yet — every run writes one when it ends"
    lines = [f"receipts checked: {n} · "
             + ("chain intact ✓" if not b else f"{len(b)} problem(s) ✗")]
    for x in b:
        lines.append(f"  ✗ {x['receipt']}: {x['reason']}"
                     + (f" (expected link to {x['expected_prev']})"
                        if x.get("expected_prev") else ""))
    if result.get("no_self_check"):
        lines.append(f"  · {result['no_self_check']} older receipt(s) predate "
                     "self-checks: only their links are verified")
    lines.append("  tamper-evident, not tamper-proof: rewriting the whole "
                 "chain is not detected")
    return "\n".join(lines)


def _sha_sha(path: Path) -> str:
    try:
        return _sha(path.read_bytes())
    except OSError:
        return "?"


def render_summary(rec: dict) -> str:
    """The end-of-run summary — COMPACT. Two lines (status·findings /
    tokens·artifacts) + a next line only when actionable. Five separate lines
    was transcript noise.
    """
    status = rec.get("status", "uncertain")
    mark = {"completed": "OK", "no_work": "--", "blocked": "!!",
            "failed": "XX", "uncertain": "??"}.get(status, "??")
    checks = rec.get("checks", {})
    fpath = Path(rec.get("_eng", ".")) / "findings.jsonl"
    n_find, n_proof = 0, 0
    try:
        ids, proof = [], 0
        for ln in fpath.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("id") and r["id"] not in ids:
                ids.append(r["id"])
            if r.get("proof"):
                proof += 1
        n_find, n_proof = len(ids), min(proof, len(ids))
    except OSError:
        pass
    if checks:
        passed = sum(1 for c in checks.values() if c.get("ok"))
        l1 = (f"{status} · {passed}/{len(checks)} checks · "
              f"{n_find} findings ({n_proof} proven)")
    else:
        l1 = f"{status} · {n_find} findings ({n_proof} proven)"
    t = rec.get("tokens", {})
    outs = rec.get("outputs", [])
    art = (f"{len(outs)} artifact(s)" if outs else "no artifacts")
    cost = rec.get("cost_usd")
    money = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""   # When a price is known
    l2 = (f"tokens ↑{t.get('prompt', 0):,} ↓{t.get('completion', 0):,}{money} "
          f"· {art}")
    nxt = {
        "completed": "",
        "no_work": "next: task produced no actions — rephrase or /exit",
        "blocked": "next: resolve the block, then /resume",
        "failed": "next: inspect receipt + audit.jsonl, fix, re-run",
        "uncertain": "next: verify unproven checks before trusting output",
    }[status]
    bad = [k for k, c in checks.items() if not c.get("ok")]
    if bad:
        nxt = "next: unproven checks — " + ", ".join(bad[:3])
    out = [f"[{mark}] {l1}", l2]
    if nxt:
        out.append(nxt)
    return "\n".join(out)

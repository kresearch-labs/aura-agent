"""findings.py — state tool + findings with dedup.

The `state` tool is the ONLY write path to state.md and findings.jsonl.
Enforcement: fs.py consults the mediator's locked-files list before any write —
direct write_file to a state file is refused with an audit line.

Finding record (BLUEPRINT) — a claimed RESULT of any kind: {id FINDING-<hex6>,
title (the claim), vuln_type (kind, optional), target (optional), severity
(optional, default info), evidence_refs[], proof: null, status:
open|validated|rejected} When the model records one, the loop re-runs the
read-only steps behind it (engine/validator.py) and attaches the proof — or
says why not. Dedup key: (title_stem, vuln_type, target) — a duplicate burst
MERGES evidence into the existing record instead of creating a new one.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec

VALID_SEVERITIES = {"info", "low", "medium", "high", "critical"}


def title_stem(title: str) -> str:
    """Normalize a title for dedup comparison."""
    s = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return re.sub(r"\s+", " ", s)


def dedup_key(f: dict) -> tuple:
    return (title_stem(f.get("title", "")),
            (f.get("vuln_type") or "").lower(),
            (f.get("target") or "").lower())


class FindingStore:
    """Append-only jsonl of finding records + in-memory dedup index."""

    def __init__(self, path: Path, campaign: str = "default"):
        self.path = Path(path)
        self.campaign = campaign
        self.records: list[dict] = []
        self._load()

    def _load(self) -> None:
        """The file is append-only (every state change is a new line), so
        the LAST line per id is the record — earlier ones are history."""
        if not self.path.exists():
            return
        latest: dict[str, dict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # Crash-partial tolerated
            latest[rec.get("id") or f"_line{len(latest)}"] = rec
        self.records = list(latest.values())

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----------------------------------------------------------------- add
    def add(self, title: str, vuln_type: str = "", target: str = "",
            severity: str = "info",
            evidence_refs: list[str] | None = None) -> dict:
        """Create or MERGE a result. Only the claim (title) is required —
        kind (vuln_type), target and severity are optional for general
        work. Returns the record + action taken."""
        severity = (severity or "info").strip() or "info"
        if severity.lower() not in VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(VALID_SEVERITIES)}, got '{severity}'")
        rec = {
            "id": f"FINDING-{uuid.uuid4().hex[:6]}",
            "campaign": self.campaign,
            "title": title,
            "vuln_type": (vuln_type or "").lower(),
            "target": target,
            "severity": severity.lower(),
            "evidence_refs": list(evidence_refs or []),
            "proof": None,
            "status": "open",
            "created_ts": round(time.time(), 3),
        }
        key = dedup_key(rec)
        for existing in self.records:
            if dedup_key(existing) == key:
                # Duplicate burst → merge, not new record
                merged = sorted({*existing.get("evidence_refs", []),
                                 *rec.get("evidence_refs", [])})
                existing["evidence_refs"] = merged
                # severity escalates, never de-escalates
                order = ["info", "low", "medium", "high", "critical"]
                if order.index(rec["severity"]) > order.index(existing["severity"]):
                    existing["severity"] = rec["severity"]
                self._append(existing)  # State change is an append event
                return {**existing, "action": "merged", "id": existing["id"]}
        self.records.append(rec)
        self._append(rec)
        return {**rec, "action": "created"}

    # ---------------------------------------------------------------- proof
    def attach_proof(self, finding_id: str, proof: dict) -> dict:
        """Validator calls this. proof = {command, output_ref, timestamp, hash}."""
        for rec in self.records:
            if rec["id"] == finding_id:
                rec["proof"] = proof
                rec["status"] = "validated"
                self._append(rec)
                return rec
        raise KeyError(f"unknown finding {finding_id}")

    def reportable(self) -> list[dict]:
        """Contract: no proof → cannot enter the report (code-enforced)."""
        return [r for r in self.records
                if r["status"] == "validated" and r.get("proof")]


# -------------------------------------------------------------- state tool --
def register(mediator: Mediator, dispatch: dict, engagement_root: Path,
             campaign: str = "default") -> FindingStore:
    """Wire `state` tool + fs write-lock on state files."""
    store = FindingStore(Path(engagement_root) / "findings.jsonl", campaign)

    def state_read(_args) -> str:
        """Read state.md + findings summary (plan-safe)."""
        state_p = Path(engagement_root) / "state.md"
        content = state_p.read_text(encoding="utf-8") if state_p.exists() else ""
        summary = {"findings": len(store.records),
                   "validated": len(store.reportable()),
                   "open": sum(1 for r in store.records if r["status"] == "open")}
        return content + "\n[findings summary] " + json.dumps(summary)

    def state_write(args) -> str:
        """Append to state.md (operator-visible log)."""
        p = Path(engagement_root) / "state.md"
        entry = args.get("text", "").rstrip()
        section = (args.get("section") or "").strip()
        with p.open("a", encoding="utf-8") as f:
            f.write(("\n## " + section + "\n" if section else "\n") + entry + "\n")
        return f"state updated ({len(entry)} chars)"

    def finding_add(args) -> str:
        rec = store.add(
            title=args["title"],
            vuln_type=args.get("kind") or args.get("vuln_type") or "",
            target=args.get("target", ""),
            severity=args.get("severity") or "info",
            evidence_refs=args.get("evidence_refs"))
        return json.dumps({"id": rec["id"], "action": rec["action"]})

    def finding_list(_args) -> str:
        out = [{"id": r["id"], "title": r["title"], "severity": r["severity"],
                "status": r["status"], "proof": bool(r.get("proof"))}
               for r in store.records]
        return json.dumps(out, ensure_ascii=False)

    mediator.register(ToolSpec("state", BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    mediator.register(ToolSpec("state_write", BlastRadius.WRITE, lambda a: None))
    mediator.register(ToolSpec("finding_add", BlastRadius.WRITE, lambda a: None))
    mediator.register(ToolSpec("finding_list", BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    dispatch["state"] = state_read
    dispatch["state_write"] = state_write
    dispatch["finding_add"] = finding_add
    dispatch["finding_list"] = finding_list

    # Enforcement: fs tools must refuse writes to the locked state files. The
    # lock list lives on the mediator; fs.py checks it before writing.
    mediator.state_file_locks = set(STATE_FILES)
    mediator._findings = store
    return store


STATE_FILES = ("state.md", "findings.jsonl", "graph.json", "notes.md")

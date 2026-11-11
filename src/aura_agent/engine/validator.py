"""validator.py — re-verification proof engine (THE differentiator).

  validate(finding_id) → re-run the tool chain recorded in the audit log
  in a FRESH context; success = same observable result.
  Proof = {command, output_ref, timestamp, hash} attached to the result;
  status → validated. Results without proof CANNOT enter the report —
  enforced in FindingStore.reportable (code, not convention).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..mediator import BlastRadius, Mediator, Violation
from ..tools.artifacts import store_artifact


@dataclass
class ValidationResult:
    finding_id: str
    ok: bool
    tier: int = 1                    # 1=re-verified
    steps: list[dict] = field(default_factory=list)
    proof: dict | None = None
    error: str | None = None

    def summary(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"{self.finding_id}: {'validated' if self.ok else 'FAILED'} " \
               f"(tier-{self.tier}, {len(self.steps)} steps)"


class Validator:
    """Re-executes a result's verification chain through the mediator.

    The chain = the sequence of mediator-legal calls that produced the
    result, pulled from the audit log / graph clues. Fresh context =
    new tool dispatch, no memory of the original run; only the recorded
    commands replay.
    """

    def __init__(self, mediator: Mediator, findings_store,
                 engagement_root: Path):
        self.m = mediator
        self.store = findings_store
        self.root = Path(engagement_root)

    # ------------------------------------------------------------- chain io
    def save_chain(self, finding_id: str, chain: list[dict]) -> str:
        """Record the verification chain that produced a result. Called by
        the loop when the model claims a result: chain = [{tool, args}, ...]."""
        d = self.root / "chains"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{finding_id}.json"
        p.write_text(json.dumps(
            {"finding_id": finding_id, "chain": chain}, ensure_ascii=False,
            indent=1), encoding="utf-8")
        return str(p.relative_to(self.root))

    def load_chain(self, finding_id: str) -> list[dict]:
        p = self.root / "chains" / f"{finding_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))["chain"]
        raise FileNotFoundError(f"no verification chain for {finding_id}")

    # ------------------------------------------------------------ re-verify
    def validate(self, finding_id: str, dispatch: dict[str, Callable],
                 observable: Callable[[Any], str] | None = None) -> ValidationResult:
        """Re-run every step through the mediator (fresh dispatch). Success =
        all steps OK and the observable result matches the original."""
        try:
            chain = self.load_chain(finding_id)
        except FileNotFoundError as e:
            return ValidationResult(finding_id=finding_id, ok=False, error=str(e))
        rec = next((r for r in self.store.records
                    if r["id"] == finding_id), None)
        if rec is None:
            return ValidationResult(finding_id=finding_id,
                                    ok=False, error="unknown finding")

        steps, outputs = [], []
        for i, step in enumerate(chain):
            name, args = step["tool"], step.get("args", {})
            fn = dispatch.get(name)
            if fn is None:
                return ValidationResult(
                    finding_id=finding_id, ok=False, steps=steps,
                    error=f"chain step {i}: unknown tool '{name}'")
            # Never replay a step that changes things — re-running a recorded
            # `git commit`, `pip install` or write would repeat it
            spec = self.m.tools.get(name)
            if spec is None or spec.blast_radius != BlastRadius.READ:
                steps.append({"step": i, "tool": name, "status": "skipped",
                              "error": "not read-only — never replayed"})
                continue
            try:
                raw = self.m.execute(name, args, fn)
            except Violation as e:
                steps.append({"step": i, "tool": name, "status": "refused",
                              "error": str(e)})
                return ValidationResult(finding_id=finding_id, ok=False,
                                        steps=steps,
                                        error=f"step {i} refused: {e}")
            # The docstring always promised "same observable result"; now
            # enforced when the step recorded what it originally saw
            want = step.get("digest")
            if want and output_digest(raw) != want:
                steps.append({"step": i, "tool": name, "status": "changed",
                              "error": "result differs from the original"})
                return ValidationResult(finding_id=finding_id, ok=False,
                                        steps=steps,
                                        error=f"step {i}: result changed")
            outputs.append(raw)
            steps.append({"step": i, "tool": name, "status": "ok"})

        if observable is not None:
            obs = observable(outputs)
        else:
            obs = json.dumps(
                [o if isinstance(o, str) else str(o) for o in outputs],
                ensure_ascii=False)
        proof = self._make_proof(finding_id, chain, steps, obs)
        self.store.attach_proof(finding_id, proof)
        return ValidationResult(finding_id=finding_id, ok=True,
                                steps=steps, proof=proof)

    def _make_proof(self, finding_id: str, chain: list[dict],
                    steps: list[dict], observable: str) -> dict:
        """Proof artifact: the re-run record, content-addressed."""
        data = json.dumps({"finding_id": finding_id, "chain": chain,
                           "steps": steps, "observable": observable},
                          ensure_ascii=False, indent=1).encode()
        art = store_artifact(self.root, data, f"{finding_id}-proof.json")
        return {"command": json.dumps([s["tool"] for s in chain]),
                "output_ref": art["artifact_ref"],
                "timestamp": round(time.time(), 3),
                "hash": art["sha256_16"],
                "tier": 1}


# ------------------------------------------------- run proof engine
# Before the final answer (when the run changed something), the loop asks
# for a proof: re-run the read-only steps the answer rests on, run the
# project's tests, and store the result as a tamper-evident artifact that
# the run receipt cites. Nothing that changes state is ever replayed.

def output_digest(raw: Any) -> str:
    import hashlib
    data = raw if isinstance(raw, (bytes, bytearray)) else \
        (raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True,
                                                     default=str)).encode()
    return hashlib.sha256(bytes(data)).hexdigest()[:16]


def replay_reads(mediator: Mediator, dispatch: dict, reads: list[dict],
                 limit: int = 20) -> dict:
    """Re-run read-only calls and compare with what they returned during the
    run. `reads` should be the reads made AFTER the run's last change — the
    facts the final answer rests on — so expected changes caused by the
    agent's own edits are not flagged."""
    seen, checked, changed = set(), 0, []
    for step in reversed(reads):
        if checked >= limit:
            break
        key = (step["tool"], json.dumps(step.get("args", {}), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        from ..tool_names import no_replay
        spec = mediator.tools.get(step["tool"])
        fn = dispatch.get(step["tool"])
        if spec is None or fn is None or spec.blast_radius != BlastRadius.READ \
                or no_replay(step["tool"], step.get("args", {})):
            continue
        checked += 1
        try:
            raw = mediator.execute(step["tool"], step.get("args", {}), fn)
        except Exception as e:                       # noqa: BLE001
            changed.append({"tool": step["tool"], "why": f"now fails: {e}"})
            continue
        if output_digest(raw) != step.get("digest"):
            changed.append({"tool": step["tool"],
                            "args": step.get("args", {}),
                            "why": "result changed since it was read"})
    return {"checked": checked, "changed": changed}


def run_tests(mediator: Mediator, dispatch: dict, root: Path) -> dict:
    """Run the project's test command (project_checks) through the mediator
    — audited, sandboxed and gated like any other command."""
    return run_check(mediator, dispatch, root, "test")


_NO_TESTS = re.compile(r"\b(ran|collected) 0 (tests?|items?)\b|\bno tests (ran|found|to run)\b|"
                       r"\b0 passing\b|\bTests?:\s+0 total\b|\bno test files found\b", re.I)


def run_check(mediator: Mediator, dispatch: dict, root: Path,
              kind: str = "test", tail_chars: int = 800,
              tests: list[str] | None = None) -> dict:
    """2.5: run the project's test / lint / typecheck command through the
    mediator (as run_command — gated, audited, pack-limited). tests: only
    these test files / ids (the whole suite when None or when the tool
    can't be told which tests to run)."""
    from ..project_checks import check_for
    from ..tool_names import RUN_COMMAND
    check = check_for(Path(root), kind)
    if check is None:
        return {"ran": False, "why": f"no {kind} command found for this project"}
    scope = "all"
    if tests:
        narrow = check.only(list(tests))
        if narrow is not None:
            check, scope = narrow, "these tests: " + ", ".join(tests[:6]) + (
                f" (+{len(tests) - 6} more)" if len(tests) > 6 else "")
    if not check.available():
        return {"ran": False, "command": check.display(),
                "why": f"{check.display()} needs {', '.join(check.needs)}, "
                       "which is not installed"}
    fn = dispatch.get(RUN_COMMAND)
    if fn is None:
        return {"ran": False, "why": "no shell tool available"}
    try:
        # cwd = the folder the command was detected in, not wherever Aura
        # happened to be started from
        raw = mediator.execute(RUN_COMMAND, {"cmd": check.command_line(),
                                             "cwd": str(Path(root))}, fn)
    except Violation as e:
        return {"ran": False, "command": check.display(),
                "why": f"not run: {e}"}
    except Exception as e:                           # noqa: BLE001
        return {"ran": False, "command": check.display(),
                "why": f"could not run: {type(e).__name__}: {e}"}
    exit_code, tail = None, ""
    try:
        outer = json.loads(raw) if isinstance(raw, str) else dict(raw)
        exit_code = outer.get("exit_code")
        inner = outer.get("result") or outer.get("content") or ""
        try:
            d = json.loads(inner)
            tail = (str(d.get("stdout", "")) + str(d.get("stderr", "")))
        except (TypeError, ValueError):
            tail = str(inner)
    except (TypeError, ValueError):
        tail = str(raw)
    res = {"ran": True, "ok": exit_code == 0, "exit_code": exit_code,
           "command": check.display(), "why": check.why, "scope": scope,
           "tail": tail.strip()[-tail_chars:]}
    if kind == "test" and res["ok"] and _NO_TESTS.search(tail):
        res.update(ok=False, why="no tests ran — the command found nothing to run "
                                  "(a filter or path that matches no tests?)")
    return res


def prove_run(mediator: Mediator, dispatch: dict, root: Path,
              reads: list[dict]) -> dict:
    """The proof attached to a run before its final answer."""
    replay = replay_reads(mediator, dispatch, reads)
    tests = run_tests(mediator, dispatch, root)
    if tests.get("ran") and not tests.get("ok"):
        status = "tests_failed"
    elif replay["changed"]:
        status = "stale_reads"
    elif tests.get("ran"):
        status = "verified"
    else:
        status = "unverified"
    proof = {"status": status, "reads": replay, "tests": tests,
             "timestamp": round(time.time(), 3)}
    data = json.dumps(proof, ensure_ascii=False, indent=1,
                      default=str).encode()
    try:
        art = store_artifact(Path(root), data, "run-proof.json")
        proof["output_ref"] = art["artifact_ref"]
        proof["hash"] = art["sha256_16"]
    except Exception:                                # noqa: BLE001
        proof["hash"] = output_digest(data)
    return proof


def proof_summary(proof: dict) -> str:
    """One line for the receipt and the final message."""
    t, r = proof.get("tests", {}), proof.get("reads", {})
    reads = f"{r.get('checked', 0)} read(s) re-checked"
    if r.get("changed"):
        reads += f", {len(r['changed'])} changed"
    if t.get("ran"):
        tests = f"tests {'passed' if t.get('ok') else 'FAILED'} ({t.get('command')})"
    else:
        tests = f"tests not run — {t.get('why', '?')}"
    return f"{proof.get('status')}: {tests}; {reads}"

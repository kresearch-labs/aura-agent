"""selfrepair.py — the change-gate (agent self-mutation with a referee).

Trigger: any self-mutation the agent performs on ITSELF — tool registered, MCP
server added, config/profile changed, source file under src/ or scripts/
edited. NOT engagement files (those are already shadow-snapshotted by
tools/fs.py); THIS is about the agent's own body.

Flow on a failed post-change slice:
  1. reviewer diagnoses (the built engine/reviewer.py, separate context)
  2. optional web lookup of the EXACT traceback (blocked-page aware)
  3. reviewer proposes a patch → the gate:
     - TCB files (mediator, validator, selftest, tests/**): NEVER patched
       without explicit human approval — code-enforced, not convention
     - L1 assist mode: every patch asks; L2: TCB still asks
  4. approved → apply → re-run slice → green=keep / red=shadow-git
     auto-rollback (the pre-change bytes are restored, event recorded)

TCB rule (non-negotiable, enforced in `patch_allowed`):
  agent never patches mediator.py, validator.py, selftest.py, tests/**
  without explicit human approval — it must not be able to edit its own
  referee.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

TCB = (
    "src/aura_agent/mediator.py",
    "src/aura_agent/engine/validator.py",
    "src/aura_agent/engine/selftest.py",
)
TCB_PREFIXES = ("tests/",)
MAX_PATCH_BYTES = 256 * 1024         # Sanity cap, raised 64KB→256KB per
                                     # The REAL safety is the gate + slice
                                     # selftest + shadow-git rollback, not the
                                     # byte count
ROLLBACK_LOG = "selfrepair.jsonl"


# ------------------------------------------------------------------ TCB ----
def is_tcb(rel_path: str) -> bool:
    """The referee: mediator, validator, selftest, or anything under tests/."""
    p = rel_path.replace("\\", "/").lstrip("./")
    return p in TCB or p.startswith(TCB_PREFIXES)


def patch_allowed(rel_path: str, assist: str = "L2",
                  human_approved: bool = False) -> tuple[bool, str]:
    """The gate. Returns (allowed, reason).

    TCB + not human-approved → never (assist level irrelevant — the
    referee is above the autonomy dial). Non-TCB + L1 → ask (False with
    'needs approval'); L2 → allowed.
    """
    if not rel_path or not isinstance(rel_path, str):
        return False, "patch: empty path"
    if is_tcb(rel_path):
        if human_approved:
            return True, "TCB patch, human-approved"
        return False, ("TCB file — agent may not patch its own referee "
                       "without explicit human approval")
    if assist == "L1" and not human_approved:
        return False, "L1: every self-patch needs operator approval"
    return True, "ok"


# ----------------------------------------------------------------- slices --
# selftest slice map: what a change to X should re-verify (targeted, fast)
SLICE_MAP = {
    "tools/fs.py": ["fs jail + shadow", "mediator property suite"],
    "mediator.py": ["mediator property suite", "validator proof chain"],
    "tools/findings.py": ["state/findings lock + dedup", "validator proof chain"],
    "tools/artifacts.py": ["artifact integrity", "validator proof chain"],
    "sessions.py": ["session crash-tolerance"],
    "engine/governor.py": ["governor verbatim guarantee"],
    "engine/loop.py": ["full loop vs mock LLM", "burst detection"],
    "engine/loopwatch.py": ["burst detection"],
    "engine/graph.py": ["graph + failure registry"],
    "tools/shell.py": ["shell scope + banned"],
    "tools/web.py": ["web crawler same-origin"],
    "mcp_client.py": ["mcp client roundtrip"],
}
DEFAULT_SLICE = ["mediator property suite", "fs jail + shadow"]


def slice_for(rel_path: str) -> list[str]:
    """Targeted selftest checks for a changed file. Keys match by path
    suffix for files (tools/fs.py) and segment for dirs (engine/),
    then the default slice."""
    p = rel_path.replace("\\", "/").lstrip("./")
    # exact file keys (longest suffix first for nested overlap)
    for key in sorted((k for k in SLICE_MAP if not k.endswith("/")),
                      key=len, reverse=True):
        if p == f"src/aura_agent/{key}" or p.endswith("/" + key):
            return SLICE_MAP[key]
    # directory segment keys: any path segment matches (engine/, tools/)
    for key, checks in SLICE_MAP.items():
        if key.endswith("/") and f"/{key}" in f"/{p}" :
            return checks
    return DEFAULT_SLICE


def run_slice(project_root: Path, check_names: list[str]) -> dict:
    """Run ONLY the named selftest checks. Returns {ok, failed: [..]}.
    The checks import the INSTALLED package, so they verify real behavior;
    `project_root` only decides whether pytest-file-based checks (e.g.
    the mediator property suite) have sources to run."""
    from .engine.selftest import SelfTest
    st = SelfTest(project_root)
    for name in check_names:
        fn = getattr(st, _check_attr(name), None)
        if fn is None:
            continue
        st._check(name)(fn)
    failed = [r.name for r in st.results if not r.ok]
    return {"ok": not failed and bool(st.results), "ran": [r.name for r in st.results],
            "failed": failed}


def _check_attr(check_name: str) -> str:
    """check display name → SelfTest method name (static map, kept in sync
    with run_all's registration order)."""
    return {
        "mediator property suite": "_mediator_suite",
        "fs jail + shadow": "_fs_jail",
        "session crash-tolerance": "_sessions",
        "full loop vs mock LLM": "_loop_mock",
        "governor verbatim guarantee": "_governor",
        "burst detection": "_loopwatch",
        "graph + failure registry": "_graph",
        "state/findings lock + dedup": "_findings",
        "artifact integrity": "_artifacts",
        "validator proof chain": "_validator",
        "shell scope + banned": "_shell",
        "web crawler same-origin": "_web",
        "mcp client roundtrip": "_mediator_suite",   # Nearest proxy in-slice
    }.get(check_name, "")


class _Sandbox:
    """Hot-reload sandbox: after writing the patched bytes, replace the
    module in sys.modules with one loaded FROM THE PATCHED FILE, so the
    selftest slice exercises the patched code (in the real project the
    installed package IS this source tree — the file write is enough;
    the reload covers the same-process case)."""

    def __init__(self, project_root: Path, rel_path: str):
        self.root = Path(project_root)
        self.rel = rel_path
        self.target = self.root / rel_path
        self.mod_name = self._module_name(rel_path)

    @staticmethod
    def _module_name(rel_path: str) -> str | None:
        p = rel_path.replace("\\", "/")
        if not p.startswith("src/aura_agent/") or not p.endswith(".py"):
            return None
        mod = p[len("src/"):-len(".py")].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-len(".__init__")]
        return mod

    def reload_patched(self) -> None:
        """Hot-reload the patched module (in-process slice correctness)."""
        import importlib
        import sys
        if self.mod_name and self.mod_name in sys.modules:
            importlib.reload(sys.modules[self.mod_name])

    def restore_reload(self, original_bytes: bytes | None) -> None:
        """Write the original bytes back and hot-reload."""
        if original_bytes is not None and self.target.exists():
            self.target.write_bytes(original_bytes)
            self.reload_patched()


# ------------------------------------------------------------- shadow git --
def _live_package_root() -> Path | None:
    """The project root the INSTALLED package actually runs from —
    hot-reload is safe only against this root."""
    import aura_agent
    return Path(aura_agent.__file__).resolve().parents[2]


def _base(project_root: Path, store: Path | None) -> Path:
    """Where the gate keeps its copies: the notebook, else.selfrepair in the root
    (tests).
    """
    return Path(store) if store is not None else Path(project_root) / ".selfrepair"


def _snapshot_file(project_root: Path, rel_path: Path,
                   store: Path | None = None) -> str | None:
    """Pre-change bytes → <store>/<hash>/ (the rollback anchor)."""
    if not rel_path.exists():
        return None
    digest = hashlib.sha256(rel_path.read_bytes()).hexdigest()[:12]
    dest = _base(project_root, store) / digest
    if not dest.exists():
        dest.mkdir(parents=True)
        shutil.copy2(rel_path, dest / rel_path.name)
    return digest


def rollback(project_root: Path, rel_path: str, digest: str,
             store: Path | None = None) -> bool:
    """Shadow-git auto-rollback: restore pre-change bytes. Returns success."""
    src = _base(project_root, store) / digest / Path(rel_path).name
    dst = project_root / rel_path
    if not src.exists():
        return False
    shutil.copy2(src, dst)
    return True


# ------------------------------------------------------- copy-first flow ----
def _stage_package(project_root: Path, rel_path: str,
                   new_content: str, store: Path | None = None) -> tuple[Path, Path] | None:
    """COPY-FIRST: clone the LIVE package tree into.selfrepair/staging/<id>/src/,
    write the patched file into the clone. The clone is validated in a
    subprocess (staged tree first on PYTHONPATH) — the live tree is never
    touched until the staged copy proves green. Returns (stage_src,
    staged_file) or None for non-package paths.
    """
    p = rel_path.replace("\\", "/")
    if not p.startswith("src/aura_agent/"):
        return None
    try:
        import aura_agent
        live_pkg = Path(aura_agent.__file__).resolve().parent
    except Exception:
        return None
    sid = hashlib.sha256(f"{rel_path}{time.time()}".encode()).hexdigest()[:10]
    stage_src = (_base(project_root, store) / "staging" / sid / "src")
    shutil.copytree(live_pkg, stage_src / "aura_agent", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    dst = stage_src / p[len("src/"):]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new_content, encoding="utf-8", newline="")
    return stage_src, dst


def run_slice_staged(stage_src: Path, project_root: Path,
                     check_names: list[str], module: str | None = None) -> dict:
    """Run the selftest slice against the STAGED package copy in a subprocess
    (staged tree first on PYTHONPATH — the staged package shadows the live
    install for the slice). Live process stays clean. module: the changed
    module, imported first with Aura's entry point.
    """
    import subprocess as _sp
    import sys as _sys
    imports = [m for m in (module, "aura_agent.cli") if m]
    code = (
        "import json, sys, importlib\n"
        "sys.path.insert(0, sys.argv[1])\n"
        f"for _m in {json.dumps(imports)}:\n"
        "    try:\n"
        "        importlib.import_module(_m)\n"
        "    except BaseException as _e:\n"
        "        print(json.dumps({'ok': False, 'ran': ['import ' + _m],\n"
        "                          'failed': ['import ' + _m + ': ' + repr(_e)[:160]]}))\n"
        "        raise SystemExit(0)\n"
        "from aura_agent.engine.selftest import SelfTest\n"
        "from aura_agent.selfrepair import _check_attr\n"
        f"st = SelfTest({json.dumps(str(project_root))})\n"
        f"names = {json.dumps(check_names)}\n"
        "for name in names:\n"
        "    fn = getattr(st, _check_attr(name), None)\n"
        "    if fn is None:\n"
        "        continue\n"
        "    st._check(name)(fn)\n"
        "failed = [r.name for r in st.results if not r.ok]\n"
        "print(json.dumps({'ok': not failed and bool(st.results),\n"
        "                  'ran': [r.name for r in st.results],\n"
        "                  'failed': failed}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(stage_src)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    try:
        r = _sp.run([_sys.executable, "-c", code, str(stage_src)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, env=env)
    except Exception:
        return {"ok": False, "ran": [], "failed": ["staged slice error"]}
    for line in reversed((r.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                break
    return {"ok": False, "ran": [], "failed": ["staged slice error"]}


# ----------------------------------------------------------- the gate flow --
class SelfRepair:
    """One gated change: propose → check permission → apply → verify →
    keep or roll back. Every step lands in selfrepair.jsonl (auditable)."""

    def __init__(self, project_root: Path, assist: str = "L2",
                 reviewer=None, web_lookup=None, store: Path | None = None):
        self.root = Path(project_root)
        self.store = store                # The notebook's copy folder
        self.assist = assist
        self.reviewer = reviewer          # callable(diagnosis_ctx) -> dict
        self.web_lookup = web_lookup      # callable(query) -> str | None
        self.events: list[dict] = []

    def _log(self, event: dict) -> None:
        event = {"ts": round(time.time(), 3), **event}
        self.events.append(event)
        p = (_base(self.root, self.store).parent / ROLLBACK_LOG if self.store is not None
             else self.root / ROLLBACK_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def apply_change(self, rel_path: str, new_content: str,
                     human_approved: bool = False) -> dict:
        """The ONLY path an agent takes to mutate itself.

        Verification semantics: the selftest slice imports the INSTALLED
        package. When `root` IS the real project (the normal case) the
        file write IS the change and the slice tests it. On a sandbox
        root (tests), the slice still exercises the installed real code —
        so a sandbox apply verifies the GATE machinery (apply/verify/
        rollback/ledger), while real self-patches verify real behavior.
        """
        rel_path = rel_path.replace("\\", "/").lstrip("./")
        allowed, why = patch_allowed(rel_path, self.assist, human_approved)
        if not allowed:
            self._log({"event": "refused", "path": rel_path, "why": why})
            return {"ok": False, "refused": True, "why": why}
        target = self.root / rel_path
        # TOOL-TRUST RULE: an agent-proposed NEW tool needs EXPLICIT operator
        # approval — every time, no session-wide grants.
        if (not target.exists()) and rel_path.replace("\\", "/").startswith(
                "src/aura_agent/tools/") and not human_approved:
            self._log({"event": "refused", "path": rel_path,
                       "why": "tool-trust: new tool needs operator approval"})
            return {"ok": False, "refused": True,
                    "why": ("tool-trust: agent-proposed NEW tools require "
                            "explicit operator approval — pass "
                            "human_approved=true after the operator says yes")}
        if len(new_content.encode()) > MAX_PATCH_BYTES:
            self._log({"event": "refused", "path": rel_path,
                       "why": "patch too large"})
            return {"ok": False, "refused": True, "why": "patch too large"}
        digest = _snapshot_file(self.root, target, self.store)
        old_bytes = target.read_bytes() if target.exists() else None
        # COPY-FIRST: stage the patched package, verify the STAGED tree, merge
        # only on green. The live file is untouched until the copy proves
        # itself; a red slice discards the staging copy — no live rollback
        # needed, nothing was broken.
        staged = _stage_package(self.root, rel_path, new_content, self.store)
        if staged is not None:
            stage_src, _staged_file = staged
            checks = slice_for(rel_path)
            mod = rel_path[len("src/"):-3].replace("/", ".") if rel_path.endswith(".py") else None
            if mod and mod.endswith(".__init__"):
                mod = mod[:-len(".__init__")]
            res = run_slice_staged(stage_src, self.root, checks, mod)
            if not res["ok"]:
                shutil.rmtree(stage_src.parent, ignore_errors=True)
                diag = None
                if self.reviewer is not None:
                    try:
                        diag = self.reviewer(
                            f"staged self-patch of {rel_path} broke slice "
                            f"{res['failed'] or checks}")
                    except Exception as e:
                        diag = {"error": str(e)}
                web_hit = None
                if self.web_lookup is not None and diag:
                    try:
                        web_hit = self.web_lookup(
                            f"{res['failed'][0] if res['failed'] else 'selftest'} "
                            f"selftest failure after staged self-patch")
                    except Exception:
                        web_hit = None
                self._log({"event": "staged-discarded", "path": rel_path,
                           "failed_checks": res["failed"], "diagnosis": diag,
                           "web": web_hit,
                           "note": "live untouched (copy-first)"})
                return {"ok": False, "live_untouched": True,
                        "staged_discarded": True, "slice": checks,
                        "failed": res["failed"], "diagnosis": diag,
                        "web": web_hit}
            # green → merge into live
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content, encoding="utf-8", newline="")
            self._log({"event": "applied", "path": rel_path,
                       "snapshot": digest, "copy_first": True})
            sandbox = _Sandbox(self.root, rel_path)
            live_pkg_root = _live_package_root()
            if sandbox.mod_name and live_pkg_root is not None and \
                    self.root.resolve() == live_pkg_root:
                sandbox.reload_patched()
            shutil.rmtree(stage_src.parent, ignore_errors=True)
            self._log({"event": "keep", "path": rel_path,
                       "slice": checks, "copy_first": True})
            return {"ok": True, "kept": True, "slice": checks,
                    "copy_first": True, "snapshot": digest}
        # legacy in-place flow (non-package paths: scripts/, config, etc.)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8", newline="")
        self._log({"event": "applied", "path": rel_path,
                   "snapshot": digest})
        # targeted verification (hot-reload ONLY when this root IS the
        # live package — a sandbox root would otherwise replace real
        # modules with fake-file stubs and poison later checks)
        sandbox = _Sandbox(self.root, rel_path)
        live_pkg_root = _live_package_root()
        if sandbox.mod_name and live_pkg_root is not None and \
                self.root.resolve() == live_pkg_root:
            sandbox.reload_patched()
        checks = slice_for(rel_path)
        res = run_slice(self.root, checks)
        if res["ok"]:
            self._log({"event": "keep", "path": rel_path,
                       "slice": checks})
            return {"ok": True, "kept": True, "slice": checks,
                    "snapshot": digest}
        # red → diagnose (reviewer) → optional web lookup → rollback
        diag = None
        if self.reviewer is not None:
            try:
                diag = self.reviewer(
                    f"self-patch of {rel_path} broke selftest slice "
                    f"{res['failed'] or checks}")
            except Exception as e:            # Reviewer failure ≠ gate failure
                diag = {"error": str(e)}
        web_hit = None
        if self.web_lookup is not None and diag:
            try:
                web_hit = self.web_lookup(
                    f"{res['failed'][0] if res['failed'] else 'selftest'} "
                    f"selftest failure after self-patch")
            except Exception:
                web_hit = None
        if digest:
            rolled = rollback(self.root, rel_path, digest, self.store)
        else:                                     # It was a new file: remove it
            target.unlink(missing_ok=True)
            rolled = True
        if rolled and old_bytes is not None:
            sandbox.restore_reload(old_bytes)
        self._log({"event": "rollback", "path": rel_path,
                   "failed_checks": res["failed"],
                   "diagnosis": diag, "web": web_hit,
                   "restored": rolled})
        return {"ok": False, "rolled_back": rolled, "slice": checks,
                "failed": res["failed"], "diagnosis": diag,
                "web": web_hit, "snapshot": digest}

    def verify_referee_intact(self) -> bool:
        """TCB guarantee probe: the referee files exist and the full
        selftest still passes its mediator+validator slices."""
        for rel in TCB:
            if not (self.root / rel).exists():
                return False
        res = run_slice(self.root, ["mediator property suite",
                                    "validator proof chain"])
        return res["ok"]

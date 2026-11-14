"""selftest.py

Runs the full offline capability matrix against mocks — no lab, no network
beyond localhost. PASS/FAIL per subsystem. A release that doesn't pass selftest
doesn't ship.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SSE = lambda o: (b"data: " + json.dumps(o).encode() + b"\n\n")


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _python_executable_ok(path) -> bool:
    """True when `path` exists AND is executable ON THIS OS. A Linux uv
    venv's bin/python is a symlink into the WSL home — .exists() is True
    on Windows but spawn raises WinError 1920. Probe cheaply."""
    try:
        if not path or not Path(path).exists():
            return False
        r = subprocess.run([str(path), "-c", "1"], capture_output=True,
                           timeout=15)
        return r.returncode == 0
    except OSError:
        return False
    except Exception:
        return False


class SelfTest:
    def __init__(self, project_root: str | Path | None = None):
        # Parents: src/aura_agent/engine/selftest.py → project root
        self.project_root = (Path(project_root) if project_root
                             else Path(__file__).resolve().parents[3])
        self.results: list[CheckResult] = []

    # ------------------------------------------------------------- harness --
    def _check(self, name: str):
        def deco(fn):
            try:
                detail = fn() or ""
                self.results.append(CheckResult(name, True, detail))
            except Exception as e:  # noqa: BLE001 — selftest reports everything
                self.results.append(CheckResult(name, False, f"{type(e).__name__}: {e}"))
            return None
        return deco

    # -------------------------------------------------------------- checks --
    def run_all(self) -> list[CheckResult]:
        self.results = []
        self._check("mediator property suite")(self._mediator_suite)
        self._check("fs jail + shadow")(self._fs_jail)
        self._check("operator approvals")(self._approvals)
        self._check("hooks fire")(self._hooks)
        self._check("eval suite")(self._evals)
        self._check("session crash-tolerance")(self._sessions)
        self._check("full loop vs mock LLM")(self._loop_mock)
        self._check("governor verbatim guarantee")(self._governor)
        self._check("kv cache alignment")(self._kv_cache)
        self._check("model switch")(self._model_switch)
        self._check("generic prompt fits the code")(self._generic_prompt)
        self._check("burst detection")(self._loopwatch)
        self._check("graph + failure registry")(self._graph)
        self._check("state/findings lock + dedup")(self._findings)
        self._check("artifact integrity")(self._artifacts)
        self._check("validator proof chain")(self._validator)
        self._check("report gate")(self._report)
        self._check("shell scope + banned")(self._shell)
        self._check("web reader")(self._web)
        self._check("settings tiers follow profile")(self._settings_tier)
        self._check("tool-name drift guard")(self._tool_names)
        self._check("guardrail builds")(self._guardrail_wave1)
        self._check("guardrail sandbox")(self._guardrail_wave2)
        self._check("credential broker")(self._keybroker_wave3)
        return self.results

    # -- individual checks (each a compressed end-to-end scenario) ----------
    def _mediator_suite(self) -> str:
        tests = self.project_root / "tests" / "test_mediator.py"
        if not tests.exists():
            # installed-from-wheel: run the in-process property check instead
            return self._mediator_property_inline()
        # Fix: prefer the project's venv pytest — bare `uv run` fails when
        # pytest isn't in uv's managed env (repo moved hosts). Cross-platform +
        # symlink-safe: the repo's dev venv may be a Linux uv venv (bin/python
        # symlink → WSL home) that Windows cannot exec (WinError 1920). Prefer
        # an executable python; else run pytest in-process with the CURRENT
        # interpreter — selftest must never depend on which OS built the dev
        # venv.
        candidates = [
            self.project_root / ".venv" / "Scripts" / "python.exe",
            self.project_root / ".venv" / "bin" / "python",
        ]
        venv_py = next((c for c in candidates
                        if _python_executable_ok(c)), None)
        if venv_py is not None:
            cmd = [str(venv_py), "-m", "pytest"]
        else:
            cmd = [sys.executable, "-m", "pytest"]
        r = subprocess.run(
            cmd + ["tests/test_mediator.py", "-q", "--no-header"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            cwd=str(self.project_root))
        if r.returncode != 0:
            raise AssertionError((r.stdout + r.stderr)[-300:])
        return "property suite green"

    def _mediator_property_inline(self) -> str:
        from .inline_property import run_inline_property
        return run_inline_property()

    def _evals(self) -> str:
        """2.1: the suite's checkers are right (fail unsolved, pass the
        reference solution) and the runner scores a scripted agent."""
        from aura_agent.evals.runner import check_suite, run_suite, select
        from aura_agent.llm import StreamResult
        some = select(["bugfix-add", "config-json-edit", "read-count-failed",
                       "refactor-rename"])
        bad = [r["id"] for r in check_suite(some) if not r["ok"]]
        assert not bad, f"invalid tasks: {bad}"

        def solver(task):
            steps = [{"id": f"c{i}", "name": "write_file",
                      "arguments": json.dumps({"path": p, "content": c}),
                      "parsed_arguments": {"path": p, "content": c}}
                     for i, (p, c) in enumerate(task["solution"].items())]
            steps.append("done")

            class LLM:
                i = 0

                def chat(self, messages, tools=None, **k):
                    r = StreamResult()
                    if tools is None:
                        r.content = "x"
                        return r
                    st = steps[min(LLM.i, len(steps) - 1)]
                    LLM.i += 1
                    if isinstance(st, dict):
                        r.tool_calls = [st]
                    else:
                        r.content = st
                    return r
            return LLM()
        doc = run_suite(some[:2], llm_factory=solver, max_steps=10)
        assert doc["passed"] == 2, [t["why"] for t in doc["tasks"]]
        # every score records the model-specific setup it was measured with
        assert doc.get("prompt") and doc.get("cache_mode"), doc
        return f"{len(some)} tasks valid, scripted agent scored 2/2"

    def _generic_prompt(self) -> str:
        """The generic candidate must match how the code really behaves:
        shared pieces reused word for word, only tools that are offered,
        no promise the code doesn't keep, default untouched unless asked."""
        import os as _os
        import re as _re
        from aura_agent.engine.loop import DEFAULT_SYSTEM, GENERIC_SYSTEM
        from aura_agent.prompt_profiles import prompt_for
        from aura_agent.todo import TODO_INSTRUCTION
        from aura_agent.tool_schemas import MAIN_AGENT_TOOLS
        g = GENERIC_SYSTEM
        assert TODO_INSTRUCTION not in g and TODO_INSTRUCTION not in DEFAULT_SYSTEM, (
            "the plan instruction comes only with the plan switch, never in the fixed prompt")
        assert "[STATUS]" not in g and "[STATUS]" not in DEFAULT_SYSTEM, (
            "the [STATUS] block is gone (fix list #3: noise in every reply)")
        end = "only proven results enter the report."
        prove = DEFAULT_SYSTEM[DEFAULT_SYSTEM.index("- Prove what you claim"):]
        prove = prove[:prove.index(end) + len(end)]
        assert prove in g, "the 'prove what you claim' rule must be word for word"
        named = set(_re.findall(r"\b[a-z]+_[a-z_]+\b", g))
        status_words = {"in_progress"}         # Todo states, not tools
        missing = {t for t in named - status_words if t not in MAIN_AGENT_TOOLS}
        assert not missing, f"names tools that are not offered: {missing}"
        assert "user's OK" not in g and "needs your OK" not in g,             "must not promise approvals the assist level may not ask for"
        assert len(g) < 3000, "keep it short (instruction count lowers compliance)"
        prev = _os.environ.pop("AURA_PROMPT", None)
        try:
            assert prompt_for("any-model", DEFAULT_SYSTEM)[0] == DEFAULT_SYSTEM
            _os.environ["AURA_PROMPT"] = "generic"
            assert prompt_for("any-model", DEFAULT_SYSTEM)[0] == GENERIC_SYSTEM
        finally:
            _os.environ.pop("AURA_PROMPT", None)
            if prev is not None:
                _os.environ["AURA_PROMPT"] = prev
        return f"shared pieces reused, tools exist, no false promises ({len(g)} chars)"

    def _model_switch(self) -> str:
        """Everything model-specific follows a model switch together: the cache
        mode and the prompt — Aura's own for every model; an old tuned copy is
        ignored, never used.
        """
        import json as _json
        import os as _os
        from aura_agent import prompt_profiles as PP
        from aura_agent.cli import apply_model
        from aura_agent.config import ProviderProfile
        from aura_agent.engine.loop import DEFAULT_SYSTEM, AgentLoop, LoopConfig
        prev = _os.environ.get("AURA_HOME")
        _os.environ["AURA_HOME"] = tempfile.mkdtemp()
        try:
            old = PP.copy_path("qwen3-coder")
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text(_json.dumps({"style": "markdown", "notes": ["Keep it short."]}),
                           encoding="utf-8")
            loop = AgentLoop.__new__(AgentLoop)
            loop.cfg = LoopConfig()
            loop.core_prompt = DEFAULT_SYSTEM
            eng = Path(tempfile.mkdtemp())
            a = ProviderProfile(base_url="https://api.example.com/v1", model="glm-5.3")
            b = ProviderProfile(base_url="http://localhost:8000/v1", model="qwen3-coder")
            from aura_agent.legend import with_legend
            assert apply_model(loop, a, eng) == "default" and loop.cache_mode == "prefix"
            assert loop.cfg.system_prompt == with_legend(DEFAULT_SYSTEM)
            assert apply_model(loop, b, eng).startswith("default — an old tuned copy")
            assert loop.cache_mode == "block"
            assert loop.cfg.system_prompt == with_legend(DEFAULT_SYSTEM), "an old copy must not be used"
            assert PP.remove_copy("qwen3-coder") and not old.exists()
            assert apply_model(loop, b, eng) == "default"
        finally:
            if prev is None:
                _os.environ.pop("AURA_HOME", None)
            else:
                _os.environ["AURA_HOME"] = prev
        return "cache mode + prompt switch together; Aura's prompt for every model"

    def _kv_cache(self) -> str:
        """The backend's prompt cache needs the start of the prompt to stay
        identical between calls. Notes added mid-conversation must stay
        where they are (moving them to the top cut reuse from 97% to 15%)."""
        from aura_agent.kv_cache import align_messages, check_alignment
        S = lambda c: {"role": "system", "content": c}  # noqa: E731
        U = lambda c: {"role": "user", "content": c}    # noqa: E731
        A = lambda c: {"role": "assistant", "content": c}  # noqa: E731
        hist = []
        for i in range(30):
            hist += [A(f"step {i}"), U(f"tool result {i}: " + "x" * 400)]
        base = [S("SYSTEM PROMPT " * 200), U("task"), S("[SCOPE] task")] + hist
        one = base + [S("[TODO STATE] 3 pending")]
        two = base + [A("next"), U("result " + "y" * 400),
                      S("[TODO STATE] 2 pending")]

        def reuse(a, b):
            sa, sb = json.dumps(a), json.dumps(b)
            n = 0
            while n < min(len(sa), len(sb)) and sa[n] == sb[n]:
                n += 1
            return n / len(sb)
        for mode in ("prefix", "block"):
            r = reuse(align_messages(one, mode), align_messages(two, mode))
            assert r > 0.9, f"{mode}: only {r:.0%} of the prompt reusable"
            assert not check_alignment(align_messages(two, mode)), mode
        # a system note added mid-conversation keeps its place
        assert align_messages(two, "prefix")[2]["content"] == "[SCOPE] task"
        return "notes keep their place; >90% of the prompt reusable (prefix, block)"

    def _hooks(self) -> str:
        """2.9: tool_start can block (exit 2, with the hook's reason);
        tool_done fires for every outcome; .py hooks run on any OS."""
        from aura_agent.cronops import register_hook_points
        from aura_agent.mediator import Budgets, Mediator, Scope, Violation
        from aura_agent.tools.fs import register as reg
        tmp = Path(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        (eng / "hooks").mkdir()
        (eng / "hooks" / "tool_start.py").write_text(
            "import json, sys\n"
            "if json.load(sys.stdin)['args'].get('path') == 'no.txt':\n"
            "    print('not that file', file=sys.stderr); sys.exit(2)\n")
        (eng / "hooks" / "tool_done.py").write_text(
            "import json, sys\n"
            "e = json.load(sys.stdin)\n"
            "open('log.txt', 'a').write(e['status'] + ' ')\n")
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        reg(m, eng)
        register_hook_points(m, eng)
        run = lambda a: m.execute("write_file", a, m._fs["write_file"])  # noqa: SLF001
        run({"path": "ok.txt", "content": "1"})
        try:
            run({"path": "no.txt", "content": "1"})
            raise AssertionError("hook did not block")
        except Violation as e:
            assert "not that file" in str(e), e
        assert not (eng / "no.txt").exists()
        assert (eng / "log.txt").read_text().split() == ["ok"]
        return "tool_start blocks with reason, tool_done fires"

    def _approvals(self) -> str:
        """A gated call asks the operator; no = not run and the model"""
        from aura_agent.autonomy import list_grants
        from aura_agent.mediator import Budgets, Mediator, Scope, Violation
        from aura_agent.tools.fs import register as reg
        tmp = Path(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        reg(m, eng)
        m.assist = "L1"
        run = lambda a: m.execute("write_file", a, m._fs["write_file"])  # noqa: SLF001

        def refused(a) -> str:
            try:
                run(a)
            except Violation as e:
                return str(e)
            raise AssertionError("gated write ran")
        assert "needs operator approval" in refused({"path": "a", "content": "1"})
        m.approver = lambda req: "deny"
        assert "declined" in refused({"path": "a", "content": "0"})
        assert not (eng / "a").exists()
        m.approver = lambda req: "once"      # The same call is not re-asked
        assert "already said no" in refused({"path": "a", "content": "0"})

        def swap(req):                       # The card is a copy: editing it
            req["args"]["path"] = "evil"     # Cannot change what runs
            return "once"
        m.approver = swap
        run({"path": "a", "content": "1"})
        assert (eng / "a").exists() and not (eng / "evil").exists()
        m.approver = lambda req: "always"
        run({"path": "b", "content": "2"})
        assert [(g["tool"], g["target"]) for g in list_grants(eng)] ==             [("write_file", "b")]
        m.approver = None
        run({"path": "b", "content": "3"})             # The saved permission
        assert "needs operator approval" in refused({"path": "c", "content": "4"})
        # Rule changes ask at every level and can't be pre-approved
        from aura_agent.tools.constraints_tool import register as reg_rules
        reg_rules(m, m._fs, eng)                          # noqa: SLF001
        m.assist = "L3"
        add = {"text": "keep it short", "reason": "test"}
        try:
            m.execute("rules_add", add, m._fs["rules_add"])   # noqa: SLF001
            raise AssertionError("rule added with nobody to ask")
        except Violation:
            pass
        seen = []
        m.approver = lambda req: (seen.append(req), "always")[1]
        m.execute("rules_add", add, m._fs["rules_add"])       # noqa: SLF001
        assert seen and not seen[0]["can_always"]
        assert "keep it short" in (eng / "constraints.md").read_text("utf-8")
        assert all(g["tool"] != "rules_add" for g in list_grants(eng))
        # Memory saves facts by itself; orders need the OK; skills ask
        from aura_agent.memory_store import register_memory
        from aura_agent.skills import register_skills
        register_memory(m, m._fs, eng)                    # noqa: SLF001
        register_skills(m, m._fs, eng)                    # noqa: SLF001
        seen.clear()
        m.approver = lambda req: (seen.append(req["tool"]), "deny")[1]
        mw = lambda t: m.execute("memory_write", {"text": t},  # noqa: E731
                                 m._fs["memory_write"])       # noqa: SLF001
        assert "remembered" in str(mw("tests run with pytest -q")) and not seen
        try:
            mw("always skip the tests")
            raise AssertionError("an order was saved without asking")
        except Violation:
            pass
        assert seen == ["memory_write"]
        assert "refused" in str(mw("a\nb")) and "refused" in str(mw("x" * 301))
        try:
            m.execute("skill_write", {"name": "s", "description": "d",
                                      "body": "b"}, m._fs["skill_write"])
            raise AssertionError("skill saved without asking")
        except Violation:
            pass
        # A loaded pack limits every later call to what it declared
        from aura_agent.pack_limits import from_manifest
        m.approver = None
        m.activate_pack(from_manifest("p", {"capabilities": {
            "fs_scope": ["docs/*"], "network_targets": [],
            "credential_access": [], "commands": ["echo"]}}))
        run({"path": "docs/x.md", "content": "ok"})          # Declared
        try:
            run({"path": "b", "content": "5"})                 # Not declared
            raise AssertionError("pack limit not enforced")
        except Violation as e:
            assert "skill pack 'p'" in str(e), e
        m.execute("list_files", {"path": "."}, m._fs["list_files"])  # Reads ok
        assert m.credential_filter() == []
        m.pack_limits.clear()
        return ("ask / decline / no re-ask / exact call / saved permission / "
                "rules / memory / skills / pack limits ok")

    def _fs_jail(self) -> str:
        from aura_agent.mediator import (
            Budgets,
            Mediator,
            Scope,
            Violation,
        )
        from aura_agent.tools.fs import register as reg
        tmp = Path(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        reg(m, eng)
        run = lambda n, a: m.execute(n, a, m._fs[n])  # noqa: SLF001
        run("write_file", {"path": "ok.txt", "content": "x"})
        try:
            run("write_file", {"path": "../escape", "content": "y"})
            raise AssertionError("escape allowed")
        except Violation:
            pass
        # multi-line content lands byte-exact (Windows text mode once turned
        # every LF into CRLF and the read-back refused every such write),
        # and edit_file keeps a CRLF file's line endings
        run("write_file", {"path": "ml.py", "content": "a = 1\nb = 2\n"})
        assert (eng / "ml.py").read_bytes() == b"a = 1\nb = 2\n"
        (eng / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
        run("edit_file", {"path": "crlf.py", "old_string": "a = 1\nb = 2",
                          "new_string": "a = 1\nb = 3"})
        assert (eng / "crlf.py").read_bytes() == b"a = 1\r\nb = 3\r\n"
        # 2.4: a write that breaks the syntax says so at once (pinned to the
        # default switches so the operator's own /features can't fail this)
        import os as _os
        _prev = _os.environ.get("AURA_FEATURES")
        _os.environ["AURA_FEATURES"] = "recommended"
        try:
            bad = run("write_file", {"path": "bad.py", "content": "def f(:\n"})
            okw = run("write_file", {"path": "ok.py", "content": "x = 1\n"})
            badj = run("write_file", {"path": "c.json", "content": "{1:}"})
        finally:
            if _prev is None:
                _os.environ.pop("AURA_FEATURES", None)
            else:
                _os.environ["AURA_FEATURES"] = _prev
        # 2.2: the switch itself — off means no note
        from aura_agent.features import PRESETS, parse
        assert parse("-syntax_note,typo", set(PRESETS["recommended"])) == \
            set(PRESETS["recommended"]) - {"syntax_note"}
        assert parse("minimal", set()) == set(PRESETS["minimal"])
        assert "[SYNTAX ERROR] bad.py line 1" in bad, bad
        assert "SYNTAX" not in okw and "[SYNTAX ERROR]" in badj
        # and switched off (the default preset) there is no note
        _os.environ["AURA_FEATURES"] = "off"
        try:
            assert "SYNTAX" not in run("write_file", {"path": "bad2.py",
                                                      "content": "def f(:\n"})
        finally:
            if _prev is None:
                _os.environ.pop("AURA_FEATURES", None)
            else:
                _os.environ["AURA_FEATURES"] = _prev
        return "jail holds, line endings exact, syntax note (on/off)"

    def _sessions(self) -> str:
        from pathlib import Path as P

        from aura_agent.sessions import Session, new_session
        tmp = P(tempfile.mkdtemp())
        s = new_session(tmp, "crash test")
        s.append("user", content="t")
        with s.path.open("a") as f:
            f.write('{"seq": 99, "role": "ass')  # Crash tail
        s2 = Session(s.path)
        assert len(s2.messages) == 1
        return "partial tail skipped"

    def _loop_mock(self) -> str:
        from pathlib import Path as P

        from aura_agent.config import resolve
        from aura_agent.engine.loop import AgentLoop, LoopConfig
        from aura_agent.llm import LLMClient
        from aura_agent.mediator import Budgets, Mediator, Scope
        from aura_agent.sessions import new_session
        from aura_agent.tools.fs import register as reg

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(SSE({"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {
                        "name": "write_file",
                        "arguments": '{"path":"st.txt","content":"v"}'}}]}}]}))
                self.wfile.write(SSE({"choices": [{"delta": {"content": "st.txt written"}}]}))
                self.wfile.write(b"data: ")

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tmp = P(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        prof = resolve("custom", base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                       model="st", first_token_timeout=10)
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        reg(m, eng)
        calls = {"n": 0}
        real_chat = LLMClient.chat

        def chat_once(self, messages, **kw):
            if kw.get("side"):               # The message check (task_view.py): work
                from aura_agent.llm import StreamResult
                return StreamResult(content="<kind>task</kind><related>NONE</related>")
            calls["n"] += 1
            if calls["n"] == 1:
                return real_chat(self, messages, **kw)
            from aura_agent.llm import StreamResult
            return StreamResult(content="st.txt written")

        LLMClient.chat = chat_once
        try:
            loop = AgentLoop(LLMClient(prof), m, [], m._fs,
                             LoopConfig(max_steps=3), root=eng)
            rec = loop.run(new_session(eng / "sessions", "selftest"),
                           "write st")
        finally:
            LLMClient.chat = real_chat
            srv.shutdown()
        assert rec.error is None, rec.error
        assert (eng / "st.txt").read_text() == "v"
        return "E2E tool roundtrip ok"

    def _governor(self) -> str:
        from aura_agent.engine.governor import Governor, extract_verbatim
        g = Governor(100)
        msg = [{"role": "user", "content": "x" * 500 +
                " see CVE-2024-1234 at http://10.0.0.5/a"}]
        res = Governor.as_messages(g.compact(msg))
        blob = res[0]["content"]
        for tok in extract_verbatim(msg[0]["content"]):
            assert tok in blob, f"lost {tok}"
        # the compaction cliff: the user's standing rules survive word for
        # word, at the top, in both compaction paths
        rule = "Never touch the migrations folder."
        hist = ([{"role": "user", "content": f"Fix login. {rule}"}]
                + [{"role": "assistant" if i % 2 else "user",
                    "content": f"t{i}"} for i in range(14)])
        g2 = Governor(8000)
        for out in (g2.compact(hist), g2.compact_with_summary(hist, "s")):
            head = out.summary_msg["content"].split("[COMPACTED")[0]
            assert rule in head, "a user rule was lost in compaction"
        return "verbatim preserved; user rules kept through compaction"

    def _loopwatch(self) -> str:
        # WIRING check (the v05 lesson: standalone test passed while the
        # engine never called it) — loop.py must actually feed record_call
        import inspect
        from aura_agent.engine import loop as loop_mod
        src = inspect.getsource(loop_mod)
        assert "loopwatch" in src and "record_call" in src \
            and "record_result" in src \
            and "self._repeat_stop(name, args)" in src, (
                "loopwatch not wired into engine loop — dead code regression")
        from aura_agent.engine.loopwatch import BURST_LIMIT, LoopState
        st = LoopState()
        verdicts = [st.record_call("run_command", {"cmd": "scan"})
                    for _ in range(BURST_LIMIT)]
        assert verdicts[-1]["burst"] and not verdicts[-2]["burst"]
        # stall: ping-pong A/B (never repeats back-to-back) trips; reading
        # new files never does
        ok = lambda t: {"status": "ok", "result": t}            # noqa: E731
        st = LoopState()
        pong = [st.record_result("read_file", {"path": p}, ok(f"<{p}>"))
                for p in "abababab"]
        assert any(pong) and not any(pong[:6]), pong
        st = LoopState()
        assert not any(st.record_result("read_file", {"path": f"f{i}"},
                                        ok(f"<{i}>")) for i in range(12))
        return "burst trips at 5; ping-pong stall caught, new reads never"

    def _graph(self) -> str:
        from pathlib import Path as P

        from aura_agent.engine.graph import TaskGraph
        tmp = P(tempfile.mkdtemp())
        g = TaskGraph(tmp / "g.json")
        n = g.add("hypothesis")
        g.mark_dead(n.id, "nope")
        g2 = TaskGraph(tmp / "g.json")
        assert g2.check_retry(n.id)["allowed"] is False
        return "dead-end blocks retry"

    def _findings(self) -> str:
        from pathlib import Path as P

        from aura_agent.mediator import Budgets, Mediator, Scope
        from aura_agent.tools.findings import register as regf
        from aura_agent.tools.fs import register as reg
        tmp = P(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        reg(m, eng); regf(m, m._fs, eng)  # noqa: SLF001
        run = lambda n, a: m.execute(n, a, m._fs[n])  # noqa: SLF001
        run("finding_add", {"title": "SQLi Login", "vuln_type": "sqli",
                            "target": "t", "severity": "high"})
        r2 = json.loads(run("finding_add", {
            "title": "sqli login", "vuln_type": "SQLI",
            "target": "t", "severity": "critical"}))
        assert r2["action"] == "merged"
        try:
            run("write_file", {"path": "findings.jsonl", "content": "x"})
            raise AssertionError("lock failed")
        except Exception as e:
            assert "state file" in str(e)
        return "dedup + lock ok"

    def _artifacts(self) -> str:
        from pathlib import Path as P

        from aura_agent.tools.artifacts import (
            store_artifact,
            verify_manifest,
        )
        tmp = P(tempfile.mkdtemp())
        a = store_artifact(tmp, b"content", "f.bin")
        ref = a["artifact_ref"]
        (tmp / ref).write_bytes(b"tampered")
        report = verify_manifest(tmp)
        assert report["bad"], "corruption not detected"
        return "integrity sweep works"

    def _validator(self) -> str:
        from pathlib import Path as P

        # WIRING check: the Validator was built and tested for months while
        # nothing in the running agent ever called it
        import inspect
        from aura_agent.engine import loop as loop_mod
        src = inspect.getsource(loop_mod)
        assert "_prove_finding(raw)" in src and "Validator(" in src, (
            "validator not wired into the loop — recorded results are "
            "never proven")
        from aura_agent.engine.validator import Validator
        from aura_agent.mediator import (
            BlastRadius,
            Budgets,
            Mediator,
            Scope,
            ToolSpec,
        )
        from aura_agent.tools.findings import FindingStore
        tmp = P(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        m.register(ToolSpec("probe", BlastRadius.READ, lambda a: None),
                   plan_safe=True)
        store = FindingStore(eng / "f.jsonl")
        v = Validator(m, store, eng)
        f = store.add("canned", "misc", "t", "low")
        v.save_chain(f["id"], [{"tool": "probe", "args": {}}])
        res = v.validate(f["id"], {"probe": lambda a: "pwned"})
        assert res.ok and res.proof["hash"]
        assert len(store.reportable()) == 1
        # Aura never calls a result proven while a file it rests on does
        # not parse (eval: a re-read "proved" a claim about broken code)
        import types
        from aura_agent.engine.loop import AgentLoop
        (tmp / "bad.py").write_text("x = {'a': 1}}\n")
        (tmp / "ok.py").write_text("x = 1\n")
        fake = types.SimpleNamespace(
            work_dir=tmp, root=eng,
            m=types.SimpleNamespace(_findings=types.SimpleNamespace(
                records=[{"id": "F1", "target": "bad.py"}])))
        read = lambda p: {"tool": "read_file", "args": {"path": p}}
        why = AgentLoop._unparsable(fake, [read("ok.py"), read("bad.py")], {})
        assert why and why.startswith("bad.py ("), why
        assert AgentLoop._unparsable(fake, [read("ok.py")], {"id": "F1"}), \
            "a broken target must block the proof too"
        assert AgentLoop._unparsable(fake, [read("ok.py")], {}) is None
        # 2.6: bug-fix detection and what counts as a test file / failure
        from aura_agent.engine.loop import (_exit_code_of, _is_test_path,
                                            looks_like_bugfix)
        assert looks_like_bugfix("fix the crash in login")
        assert not looks_like_bugfix("add a README")
        assert _is_test_path("tests/test_a.py") and _is_test_path("a.spec.ts")
        assert not _is_test_path("src/app.py")
        assert _exit_code_of('{"exit_code": 1}') == 1
        return "re-verification proof attached; reproduce-first rules ok"

    def _report(self) -> str:
        from pathlib import Path as P

        from aura_agent.engine.report import generate_report
        from aura_agent.engine.validator import Validator
        from aura_agent.mediator import (
            BlastRadius,
            Budgets,
            Mediator,
            Scope,
            ToolSpec,
        )
        from aura_agent.tools.findings import FindingStore
        tmp = P(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({}), Budgets(), tmp / "a.jsonl", eng)
        m.register(ToolSpec("probe", BlastRadius.READ, lambda a: None),
                   plan_safe=True)
        store = FindingStore(eng / "f.jsonl")
        v = Validator(m, store, eng)
        f = store.add("timeout is 30 seconds")
        v.save_chain(f["id"], [{"tool": "probe", "args": {}}])
        v.validate(f["id"], {"probe": lambda a: "TIMEOUT = 30"})
        store.add("the app is fast")                    # Never proven
        md = generate_report(eng, store)
        proven = md.split("## Proven results")[1]
        assert "timeout is 30 seconds" in proven and "✗" not in md
        assert "the app is fast" not in proven, "unproven result reported"
        assert "1 result(s) left out" in md
        return "only proven results reported; unproven listed as left out"

    def _shell(self) -> str:
        from aura_agent.mediator import (
            Budgets,
            Mediator,
            Scope,
            Violation,
        )
        from aura_agent.tools.shell import register as regsh
        tmp = Path(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({"hosts": ["10.0.0.5"]}), Budgets(),
                     tmp / "a.jsonl", eng)
        from aura_agent.tools.fs import register as regfs
        regfs(m, eng); regsh(m, m._fs)  # noqa: SLF001
        run = lambda n, a: m.execute(n, a, m._fs[n])  # noqa: SLF001
        out = json.loads(run("run_command", {"cmd": "echo st", "host": "10.0.0.5"}))
        assert out["ok"]
        try:
            run("run_command", {"cmd": "echo x", "host": "10.9.9.9"})
            raise AssertionError("scope miss")
        except Violation:
            pass
        return "scope + exec ok"

    def _web(self) -> str:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from aura_agent.mediator import Budgets, Mediator, Scope
        from aura_agent.tools.web import register as regweb
        body = (b"<html><head><title>Home</title><script>var x=1</script>"
                b"</head><body><p>Hello reader</p><a href=/about>about</a>"
                b"</body></html>")
        asked: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                asked.append(self.path)
                # Redirects off scope: a private address (never followed unless
                # listed) and a public site
                if self.path in ("/away", "/away-public"):
                    self.send_response(302)
                    self.send_header("Location", "http://192.168.77.1/" if self.path == "/away"
                                     else "http://example.invalid/")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body if self.path == "/" else b"<html>x</html>")

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        tmp = Path(tempfile.mkdtemp()); eng = tmp / "e"; eng.mkdir()
        m = Mediator(Scope.from_dict({"hosts": ["127.0.0.1"]}), Budgets(),
                     tmp / "a.jsonl", eng)
        from aura_agent.tools.fs import register as regfs
        regfs(m, eng); regweb(m, m._fs)  # noqa: SLF001
        base = f"http://127.0.0.1:{srv.server_port}"
        crawl = lambda a: json.loads(m.execute(  # noqa: E731
            "web_crawl", a, m._fs["web_crawl"]))  # noqa: SLF001
        one = crawl({"url": base + "/"})
        # 1D.1: one page by default, readable text, and no path probes
        assert one["pages_crawled"] == 1 and asked == ["/"], asked
        assert one["title"] == "Home" and "Hello reader" in one["text"]
        assert "var x" not in one["text"] and one["artifact_ref"]
        two = crawl({"url": base + "/", "max_pages": 2, "delay": 0.0})
        assert two["pages_crawled"] == 2 and asked[-1] == "/about", asked
        away = crawl({"url": base + "/away"})
        assert "not followed" in away.get("error", ""), away
        m.scope = Scope.from_dict({"hosts": ["127.0.0.1"], "web_all": False})
        away = crawl({"url": base + "/away-public"})
        assert "not followed" in away.get("error", ""), away
        srv.shutdown()
        # 2.5: run_checks runs the project's own command (as run_command)
        import sys as _sys
        from aura_agent.tools.checks_tool import register as regchecks
        from aura_agent.tools.shell import register as regshell
        regshell(m, m._fs, allow_unbanned=True)               # noqa: SLF001
        regchecks(m, m._fs, eng)                              # noqa: SLF001
        (eng / ".aura").mkdir(exist_ok=True)
        (eng / ".aura" / "checks.json").write_text(json.dumps({
            "test": _sys.executable.replace("\\", "/") + " -c \"print(7)\""}))
        ran = json.loads(m.execute("run_checks", {"kind": "test"},
                                   m._fs["run_checks"]))      # noqa: SLF001
        assert ran.get("ok") and "7" in ran.get("tail", ""), ran
        return ("one page, readable text, no probes, off-scope redirect "
                "blocked; run_checks ok")

    def _tool_names(self) -> str:
        """Every tool a policy list refers to must really be registered — a rename
        that misses one of them fails here instead of silently disabling
        grading, flagging or the helper hints.
        """
        import os as _os
        from aura_agent import tool_names as TN
        from aura_agent.autonomy import _G3_VERBS
        from aura_agent.cli import build_agent
        from aura_agent.delegate import BIG_OUTPUT_TOOLS
        from aura_agent.engagement import init_engagement
        from aura_agent.engine.loop import _EXTERNAL_TOOLS
        from aura_agent.tool_schemas import MAIN_AGENT_TOOLS
        prev = _os.environ.get("AI_CONTEXT_LENGTH")
        _os.environ["AI_CONTEXT_LENGTH"] = "131072"   # No endpoint probe
        try:
            eng = Path(tempfile.mkdtemp()) / "e"
            init_engagement(eng, "drift")
            _p, m, _loop = build_agent(eng, None)
        finally:
            if prev is None:
                _os.environ.pop("AI_CONTEXT_LENGTH", None)
            else:
                _os.environ["AI_CONTEXT_LENGTH"] = prev
        known = set(m.tools) | set(TN.OPTIONAL_TOOLS)
        # verbs for outbound tools that don't exist yet (gateway delivery)
        future_verbs = {"send", "notify", "webhook"}
        constants = {v for k, v in vars(TN).items()
                     if k.isupper() and isinstance(v, str) and k != "MCP_PREFIX"}
        checks = {
            "tool_names constants": constants,
            "MAIN_AGENT_TOOLS": set(MAIN_AGENT_TOOLS),
            "_EXTERNAL_TOOLS": set(_EXTERNAL_TOOLS),
            "BIG_OUTPUT_TOOLS": set(BIG_OUTPUT_TOOLS),
            "_G3_VERBS": set(_G3_VERBS) - future_verbs,
        }
        bad = {k: sorted(v - known) for k, v in checks.items() if v - known}
        assert not bad, f"names not registered: {bad}"
        # Every registered tool must be REACHABLE — sent, or in the tool list
        # tools_find searches (a tool with no schema there is invisible to the
        # model) — and every tool sent must really be registered
        from aura_agent.tool_schemas import NOT_OFFERED
        from aura_agent.toolsets import CORE
        tl = _loop.tool_list
        sent = {s["function"]["name"] for s in _loop.schemas}
        reachable = set(tl.all)
        unseen = set(m.tools) - reachable - set(NOT_OFFERED)
        # use_tool is the loop's own wrapper: it unwraps into the real call
        unrun = (sent | reachable) - set(m.tools) - {"use_tool"}
        assert not unseen, f"registered but unreachable for the model: {sorted(unseen)}"
        assert not unrun, f"offered to the model but not registered: {sorted(unrun)}"
        assert set(CORE) <= sent, f"core tools not sent: {sorted(set(CORE) - sent)}"
        return (f"{sum(map(len, checks.values()))} references resolve; "
                f"{len(sent)} sent, {len(reachable)} reachable = registered")

    def _settings_tier(self) -> str:
        """SETTINGS_DESIGN final: the 4 presets resolve to their dials; the
        artifact wrapper honors the inline limit; Full's 1MB seatbelt and
        the loud truncation notice behave as designed."""
        import os as _os
        from ..config import PRESET_TABLE, resolve_settings
        from ..tools.artifacts import wrap_result as _wr
        keys = ("AURA_PRESET", "AURA_TOOL_OUTPUT", "AURA_PRUNE_PCT",
                "AURA_COMPACT_PCT", "AURA_WEB_DEPTH")
        old_env = {k: _os.environ.get(k) for k in keys}
        try:
            for k in keys:
                _os.environ.pop(k, None)
            got = {name: resolve_settings(name) for name in PRESET_TABLE}
            for name, s in got.items():
                t = PRESET_TABLE[name]
                assert s.tool_output == t["tool_output"], (name, s.tool_output)
                assert abs(s.prune_pct - t["prune_pct"]) < 1e-9
                assert abs(s.compact_pct - t["compact_pct"]) < 1e-9
            root = self.project_root
            big = b"P" * 120_000
            w_full = _wr(root, "t", big, inline_limit=1_000_000)
            assert w_full["truncated"] is False          # Full: 120K inline
            w_bal = _wr(root, "t", big, inline_limit=50_000)
            assert w_bal["truncated"] is True
            assert "TRUNCATED" in w_bal["result"]
            assert "artifact" in w_bal["result"]
            assert w_bal["total_bytes"] == 120_000
            return f"presets ok ({len(got)}), seatbelt + loud notice ok"
        finally:
            for k, v in old_env.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

    def _guardrail_wave1(self) -> str:
        """BUILD 1 read-after-write verify; BUILD 2 checkpoint/rollback (sidecar
        survives.git deletion); BUILD 3 grant semantics (G3 never grantable,
        wildcard-execute banned, expiry honored).
        """
        import os as _os
        import shutil as _sh
        import tempfile as _tf
        from pathlib import Path as _P
        sandbox = _P(_tf.mkdtemp(prefix="aura-guardrail-selftest-"))
        try:
            # -- BUILD 1: read-after-write --------------------------------
            from ..tools.fs import _readback_verify
            f = sandbox / "a.txt"
            f.write_text("hello world")
            v = _readback_verify(f, expect_bytes=11)
            assert not v.startswith("VERIFY-FAIL"), v
            v = _readback_verify(f, expect_bytes=99)
            assert v.startswith("VERIFY-FAIL"), v
            f.write_text("new content here")
            v = _readback_verify(f, contains="new content", absent="hello")
            assert not v.startswith("VERIFY-FAIL"), v
            v = _readback_verify(f, absent="new content")
            assert v.startswith("VERIFY-FAIL"), v

            # -- BUILD 2: checkpoint / rollback ----------------------------
            from ..checkpoints import create, list_checkpoints, restore
            f.write_text("version-1")
            rec = create(sandbox, label="v1")
            assert not rec.get("skipped") and rec.get("tar")
            f.write_text("version-2-broken")
            out = restore(sandbox)
            assert "restored" in out, out
            assert f.read_text() == "version-1", f.read_text()
            # sidecar survives .git deletion (tar lives outside .git)
            gitdir = sandbox / ".git"
            gitdir.mkdir(exist_ok=True)
            (gitdir / "x").write_text("x")
            _sh.rmtree(gitdir)
            f.write_text("version-3-broken")
            out = restore(sandbox)
            assert "restored" in out, out
            assert f.read_text() == "version-1", f.read_text()
            assert list_checkpoints(sandbox), "history.jsonl lost"

            # -- BUILD 3: grant semantics -----------------------------------
            from ..autonomy import match_grant, persist_grant
            rules = sandbox / ".aura"  # rules.json lives in the sandbox
            try:
                persist_grant(sandbox, "G3", "run_command", "localhost")
                raise AssertionError("G3 grant should be banned")
            except ValueError as e:
                assert "irreversible" in str(e), e
            try:
                persist_grant(sandbox, "G2", "run_command", "*")
                raise AssertionError("wildcard G2 grant should be banned")
            except ValueError as e:
                assert "allow-all" in str(e), e
            g = persist_grant(sandbox, "G2", "run_command", "localhost",
                              expires_s=3600)
            assert match_grant(sandbox, "G2", "run_command", "localhost")
            assert not match_grant(sandbox, "G2", "run_command", "other-host")
            # expiry honored
            expired = persist_grant(sandbox, "G1", "write_file", "*",
                                    expires_s=-10)
            assert not match_grant(sandbox, "G1", "write_file", "any.txt")
            return ("verify+checkpoint+grant ok "
                    f"({len(list_checkpoints(sandbox))} ckpts in sandbox)")
        finally:
            _sh.rmtree(sandbox, ignore_errors=True)

    def _guardrail_wave2(self) -> str:
        """BUILD 4+5: bubblewrap containment — write inside the notebook root
        succeeds, write outside it FAILS at the OS layer, network is unshared
        by default, sensitive env vars are scrubbed. When bwrap is missing the
        check asserts the honest fallback instead. Every path is a synthetic
        tmp sandbox root.
        """
        import os as _os
        import shutil as _sh
        import tempfile as _tf
        from pathlib import Path as _P
        from ..sandbox import bwrap_available, run_sandboxed, scrubbed_env
        sandbox = _P(_tf.mkdtemp(prefix="aura-sandbox-selftest-"))
        try:
            env = scrubbed_env()
            assert "GITHUB_TOKEN" not in env and "NPM_TOKEN" not in env
            if not bwrap_available():
                proc, note = run_sandboxed("echo hi", sandbox, cwd=str(sandbox),
                                           timeout=10)
                # Seatbelt / Landlock / Docker stand in for bubblewrap — those
                # go on to the containment probes below; with none, the note
                # must say so plainly (Windows: a job object only)
                if not any(e in note for e in ("docker", "landlock", "seatbelt")):
                    assert proc.returncode == 0 and ("UNSANDBOXED" in note or "sandbox: none" in note), note
                    return "no OS sandbox here — honest fallback verified"
            # write INSIDE the engagement root: must succeed
            proc, note = run_sandboxed(
                "echo inside > inside.txt && cat inside.txt",
                sandbox, cwd=str(sandbox), timeout=15)
            if "UNSANDBOXED" in note:
                # OS refused bwrap namespaces (e.g. AppArmor
                # apparmor_restrict_unprivileged_userns=1) — containment
                # probes are impossible; assert the fallback's honesty
                assert proc.returncode == 0 and "inside" in (proc.stdout or "")
                assert "setup FAILED" in note
                return ("OS blocks unprivileged userns — honest fallback "
                        "verified (containment activates once policy allows)")
            assert proc.returncode == 0, (proc.stderr or "")[:200]
            assert "inside" in (proc.stdout or ""), proc.stdout
            # Bubblewrap, Docker, Landlock or Seatbelt
            assert any(e in note for e in ("bwrap", "docker", "landlock", "seatbelt")), note
            # write OUTSIDE the engagement root: must FAIL at the OS layer
            outside = sandbox / ".." / "outside-write-probe.txt"
            proc, _ = run_sandboxed(
                f"echo pwned > {outside.resolve()}", sandbox,
                cwd=str(sandbox), timeout=15)
            assert proc.returncode != 0 or not outside.resolve().exists(), (
                "sandbox leak: write outside the notebook root succeeded")
            if outside.resolve().exists():
                outside.resolve().unlink()
            # network off by default: an outbound connection must fail.
            # Landlock limits TCP only (from v4), so it is probed with a TCP
            # connect; a kernel without it says "NOT limited" in the note
            if "landlock" in note:
                if "network=off" in note:
                    proc, _ = run_sandboxed(
                        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 5)\"",
                        sandbox, cwd=str(sandbox), timeout=15, network=False)
                    assert proc.returncode != 0, "sandbox leak: TCP out with network off"
                else:
                    assert "NOT limited" in note, note
            else:
                proc, _ = run_sandboxed(
                    "getent hosts example.com >/dev/null",
                    sandbox, cwd=str(sandbox), timeout=15, network=False)
                assert proc.returncode != 0, (
                    "sandbox leak: network reachable with --unshare-net")
            # network=True path accepts argv (semantics smoke — no real dial)
            proc, note_on = run_sandboxed(
                "true", sandbox, cwd=str(sandbox), timeout=15, network=True)
            assert proc.returncode == 0 and "network=on" in note_on, note_on
            engine = next(e for e in ("bwrap", "landlock", "seatbelt", "docker") if e in note)
            return f"fs+net containment verified via {engine} in sandbox root"
        finally:
            _sh.rmtree(sandbox, ignore_errors=True)

    def _keybroker_wave3(self) -> str:
        """BUILD 6: keybroker custody rules — notebook metadata map holds NO
        values; valet serves secrets only with the one-time run token (bad
        token / unknown name / loopback-only all fail closed); askpass script
        holds the token, not the secret; user store is 0600. Every path is a
        synthetic sandbox root.
        """
        import os as _os
        import shutil as _sh
        import tempfile as _tf
        import urllib.request as _req
        from pathlib import Path as _P
        sandbox = _P(_tf.mkdtemp(prefix="aura-broker-selftest-"))
        # Path.home() reads HOME on POSIX but USERPROFILE on Windows — both
        # are redirected, or the test would point at the REAL secrets file
        # (the assert below stops it before anything is written)
        old_env = {k: _os.environ.get(k) for k in
                   ("HOME", "USERPROFILE", "AURA_HOME", "AURA_KEYSTORE",
                    "AURA_PROFILE")}
        posix = _os.name != "nt"
        try:
            # isolate custody: user store under the sandbox HOME
            fake_home = sandbox / "home"
            (fake_home / ".config" / "aura").mkdir(parents=True)
            _os.environ["HOME"] = str(fake_home)
            _os.environ["USERPROFILE"] = str(fake_home)
            # the key store: a file store in a sandbox Aura home — never
            # the real OS keychain
            _os.environ["AURA_HOME"] = str(fake_home / ".aura")
            _os.environ["AURA_KEYSTORE"] = "file"
            _os.environ.pop("AURA_PROFILE", None)
            from .. import keybroker as kb
            from .. import keystore as ks
            assert str(kb.user_store_path()).startswith(str(fake_home)), \
                kb.user_store_path()
            # an old plain custody file is moved into the key store
            kb.user_store_path().write_text(json.dumps({"OLD_TOKEN": "t0"}))
            assert kb.resolve_value("OLD_TOKEN", env={}) == "t0"
            assert not kb.user_store_path().exists(), "old file not removed"

            # custody rule: metadata map carries names/hosts, never values
            meta_dir = sandbox / ".aura"
            meta_dir.mkdir()
            (meta_dir / "secrets.map").write_text(json.dumps({
                "git:github": {"hosts": ["github.com"],
                               "header": "Authorization"},
                "api:stripe": {"hosts": ["api.stripe.com"],
                               "env": ["STRIPE_KEY"]},
            }))
            meta = kb.load_meta(sandbox)
            assert "git:github" in meta and meta["api:stripe"]["env"] == ["STRIPE_KEY"]
            assert "sk_live" not in json.dumps(meta)

            # values live only in the key store (file store here: 0600)
            assert kb.set_user_secret("STRIPE_KEY", "sk_test_selftest_123")
            store_file = ks._pdir() / "secrets.json"
            assert str(store_file).startswith(str(fake_home)), store_file
            if posix:
                assert oct(store_file.stat().st_mode & 0o777) == "0o600"
            # the key-store index carries names, never values
            meta_txt = (ks._pdir() / "keystore.json").read_text()
            assert "cmd:STRIPE_KEY" in meta_txt
            assert "sk_test_selftest_123" not in meta_txt

            # valet: correct token serves, bad token fails closed,
            # unknown name fails closed
            v = kb.ValetServer(sandbox).start()
            try:
                base = f"http://127.0.0.1:{v.port}"
                ok = _req.urlopen(_req.Request(
                    f"{base}/secret/api:stripe",
                    headers={"X-Aura-Token": v.token}), timeout=5).read()
                assert ok == b"sk_test_selftest_123", ok
                def _get(url, token=None):
                    h = {"X-Aura-Token": token} if token else {}
                    return _req.urlopen(_req.Request(url, headers=h), timeout=5)
                # no token -> 403 (token gate fires first, by design)
                for url in (f"{base}/secret/api:stripe",
                            f"{base}/secret/git:github"):
                    try:
                        _get(url)
                        raise AssertionError(f"expected fail-closed: {url}")
                    except Exception as e:
                        assert getattr(e, "code", 0) == 403, (url, e)
                # valid token, wrong/unknown name -> 404 (no custody)
                for url in (f"{base}/secret/git:github",
                            f"{base}/secret/nope"):
                    try:
                        _get(url, token=v.token)
                        raise AssertionError(f"expected fail-closed: {url}")
                    except Exception as e:
                        assert getattr(e, "code", 0) == 404, (url, e)
            finally:
                v.stop()

            # askpass script holds the token, NOT the secret
            ap = kb.write_askpass(sandbox, "github", v)
            body = ap.read_text()
            assert "sk_test_selftest_123" not in body, body
            assert "X-Aura-Token" in body
            if posix:                      # Windows has no execute bit
                assert ap.stat().st_mode & 0o111

            # just-in-time env: resolved now, never written to sandbox fs
            env = kb.sandbox_env(sandbox, v)
            assert env.get("STRIPE_KEY") == "sk_test_selftest_123"
            fs_dump = "\n".join(str(p) for p in sandbox.rglob("*"))
            assert "sk_test_selftest_123" not in fs_dump, "secret hit sandbox fs"
            return "custody+valet+askpass+jit-env ok (fail-closed verified)"
        finally:
            for k, v in old_env.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v
            _sh.rmtree(sandbox, ignore_errors=True)

    # -------------------------------------------------------------- output --
    def summary(self) -> str:
        lines = ["aura selftest", "================="]
        fails = 0
        for r in self.results:
            mark = "PASS" if r.ok else "FAIL"
            if not r.ok:
                fails += 1
            lines.append(f"[{mark}] {r.name}" + (f" — {r.detail}" if r.detail else ""))
        lines.append("=================")
        lines.append(f"{len(self.results) - fails}/{len(self.results)} passed"
                     + (" — RELEASE OK" if fails == 0 else " — DO NOT SHIP"))
        return "\n".join(lines)


def run_selftest(project_root: str | None = None) -> str:
    import os as _os
    # keys written by checks go to a plain file store, never the real OS
    # keychain (the checks that write keys also use a sandbox Aura home)
    prev = _os.environ.get("AURA_KEYSTORE")
    _os.environ["AURA_KEYSTORE"] = "file"
    # the checks run tasks through the agent, and every task writes the pblock
    # diary and may learn — into a throwaway folder, never the user's real
    # pblocks (they had "bugfix-add" / "selftest" runs in their diary)
    prev_pb = _os.environ.get("AURA_PBSTORES")
    _os.environ["AURA_PBSTORES"] = tempfile.mkdtemp(prefix="aura-selftest-pblocks-")
    _reset_pblock_cache()
    try:
        st = SelfTest(project_root)
        st.run_all()
        return st.summary()
    finally:
        for k, v in (("AURA_KEYSTORE", prev), ("AURA_PBSTORES", prev_pb)):
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _reset_pblock_cache()


def _reset_pblock_cache() -> None:
    """The engine keeps its loaded pblocks per folder; drop them so the next
    briefing reads the folder AURA_PBSTORES names now."""
    try:
        from aura_agent.pblock_engine.ground import _CACHE
        _CACHE["root"] = _CACHE["orch"] = None
    except Exception:                                   # noqa: BLE001
        pass

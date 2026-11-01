"""run_code.py — a script that calls Aura's tools.

The saving: the data between the calls stays in the script, only the printed
result comes back.

Aura: every call the script makes — `aura.call("read_file", path="a.py")` —
comes back over localhost (a token per run) into the SAME mediator: scope,
approvals, audit and receipts apply as if the model had made it. The script
itself is local code: it is graded and approved like run_command (in the same
sandbox on Linux when available).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec

NOT_FROM_SCRIPTS = {"run_code", "delegate", "helper_apply", "process_input", "clarify",
                    "settings_set", "memory_forget"}
MAX_CALLS = 200

_CLIENT = '''"""aura — call Aura's tools from this script (they pass Aura's checks)."""
import json, os, urllib.request

def call(tool, **args):
    req = urllib.request.Request(os.environ["AURA_TOOLS_URL"],
        data=json.dumps({"tool": tool, "args": args}).encode(),
        headers={"Content-Type": "application/json", "X-Aura-Token": os.environ["AURA_TOOLS_TOKEN"]})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read())
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]
'''


def run_code(mediator: Mediator, dispatch: dict, root: Path, args: dict) -> str:
    code = str(args.get("code", ""))
    if not code.strip():
        return "error: give the Python code to run"
    timeout = max(5, min(int(args.get("timeout", 120) or 120), 600))
    token = secrets.token_hex(16)
    calls: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            def reply(obj):
                body = json.dumps(obj, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            if self.headers.get("X-Aura-Token") != token:
                self.send_response(403)
                self.end_headers()
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                tool, a = str(req.get("tool", "")), req.get("args") or {}
            except ValueError:
                return reply({"error": "bad request"})
            if len(calls) >= MAX_CALLS:
                return reply({"error": f"more than {MAX_CALLS} tool calls in one script"})
            if tool in NOT_FROM_SCRIPTS or tool not in dispatch or tool not in mediator.tools:
                calls.append(f"{tool} (refused)")
                return reply({"error": f"'{tool}' can't be called from a script"})
            calls.append(tool)
            try:
                # the same mediator: scope, approvals, audit
                res = mediator.execute(tool, a, dispatch[tool])
            except Exception as e:                       # noqa: BLE001
                return reply({"error": f"{type(e).__name__}: {e}"})
            reply({"result": res})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="aura-code-"))
    try:
        (tmp / "aura.py").write_text(_CLIENT, encoding="utf-8")
        (tmp / "script.py").write_text(code, encoding="utf-8")
        # The script runs like a command. Network stays on so it can reach
        # Aura's own tool server on localhost; scripts that reach out
        # themselves ask first (autonomy.run_code_is_g3).
        extra = {"PYTHONPATH": str(tmp) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                 "AURA_TOOLS_URL": f"http://127.0.0.1:{srv.server_address[1]}/",
                 "AURA_TOOLS_TOKEN": token, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            from ..sandbox import run_sandboxed
            p, _sb_note = run_sandboxed(f'"{sys.executable}" "{tmp / "script.py"}"', Path(root),
                                        cwd=str(root), timeout=timeout, network=True,
                                        extra_env=extra)
            out, err, code_ = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, code_ = (e.stdout or ""), f"stopped after {timeout} s", -1
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
    finally:
        srv.shutdown()
        try:
            for f in tmp.iterdir():
                f.unlink()
            tmp.rmdir()
        except OSError:
            pass
    try:
        from ..secrets_guard import scrub_secrets
        out, err = scrub_secrets(out or ""), scrub_secrets(err or "")
    except Exception:                                    # noqa: BLE001
        pass
    used = ", ".join(calls[:30]) + (f" … ({len(calls)} calls)" if len(calls) > 30 else "")
    return json.dumps({"exit_code": code_, "stdout": out, "stderr": err[-4000:],
                       "tool_calls": used or "none"}, ensure_ascii=False)


def register(mediator: Mediator, root: Path, dispatch: dict) -> None:
    mediator.register(ToolSpec("run_code", BlastRadius.EXECUTE, lambda a: None))
    dispatch["run_code"] = lambda a: run_code(
        mediator, dispatch, Path(getattr(mediator, "work_dir", None) or root), a)

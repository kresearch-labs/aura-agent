"""regress.py — "did Aura get worse after a change?".

Record once (live, costs model calls): a task runs on a copy of a project
folder; every model reply is saved with what happened (the tools run and their
outcome, the final files, the steps). Replay any time (free, no model): the
saved replies are served by a local fake model server to the CURRENT engine, on
a fresh copy, and the outcome is compared. Same model replies, different engine
behaviour = something in Aura changed — the report says what (a tool that now
fails, a file that differs, a run that ends sooner or later). Compared
structurally, never by prose.

Aura keeps, per model call, what was new since the model's last reply (tool
results, notes, the user's message) — the replay compares that call by call and
names the first one that differs, so a change is traced to its step.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _dir() -> Path:
    from .paths import aura_home
    return aura_home() / "regress"


def _snapshot(root: Path) -> dict:
    from .tools.search import SKIP_DIRS
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            out[p.relative_to(root).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()[:12]
    return out


def _copy(src: Path) -> Path:
    from .tools.search import SKIP_DIRS
    dest = Path(tempfile.mkdtemp(prefix="aura-regress-")) / Path(src).name
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    return dest


def _run(task: str, project: Path, llm_wrap=None, env: dict | None = None) -> dict:
    """One run on `project` (a copy); returns the outcome."""
    from .cli import build_agent, engagement_dir
    from .sessions import new_session
    old = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        nb = engagement_dir(None, project)
        _, m, loop = build_agent(nb, None)
        m.approver = lambda req: "once"            # Recorded runs approve like the original
        if llm_wrap:
            llm_wrap(loop)
        tools: list = []
        loop.on_event = lambda kd, d: tools.append([d.get("tool"), d.get("status")]) \
            if kd == "tool_done" else None
        t = time.time()
        rec = loop.run(new_session(nb / "sessions", "regress"), task)
        return {"tools": tools, "files": _snapshot(project), "steps": rec.step,
                "error": rec.error or "", "seconds": round(time.time() - t, 1)}
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seen(messages: list) -> list:
    """What was new for the model at this call: everything after its last
    reply, as (role, text) — ids and who-wrote-it left out."""
    from .notes import text_of
    last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    return [[m.get("role"), " ".join(text_of(m.get("content")).split())[:400]]
            for m in messages[last + 1:] if m.get("role") != "system"]


_STAMP = re.compile(r"\[\w{3} \d\d \w{3} \d\d:\d\d\] ")           # A message's time stamp
_IDS = re.compile(r"(?:\b|(?<=sha))(?=[0-9a-f]*\d)[0-9a-f]{6,40}\b|\b\d+(?:\.\d+)?\s?(?:ms|s)\b|"
                  r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?\b")


def _steady(text: str) -> str:
    """Leave out what differs on every run (ids, timings, timestamps) — a
    new finding id is not a change in Aura."""
    return _IDS.sub("#", _STAMP.sub("", text))


def _first_divergence(want: list, got: list) -> str:
    """The first model call whose input differs between the two runs."""
    want = [[[x[0], _steady(str(x[1]))] for x in call] for call in want]
    got = [[[x[0], _steady(str(x[1]))] for x in call] for call in got]
    for i, (a, b) in enumerate(zip(want, got)):
        if a != b:
            pick = next(((x, y) for x, y in zip(a, b) if x != y), (a[-1:] or [None], b[-1:] or [None]))
            return (f"first difference in what the model was sent: call {i + 1} — "
                    f"was {str(pick[0])[:160]!r}, now {str(pick[1])[:160]!r}")
    if len(want) != len(got):
        return f"the run made {len(got)} model calls (recorded: {len(want)})"
    return ""


def record(name: str, task: str, project: Path, say=print) -> dict:
    """Live run (the model in use), saving every model reply."""
    replies: list = []
    seen: list = []

    def wrap(loop):
        orig = loop.llm.chat

        def chat(*a, **kw):
            seen.append(_seen(a[0] if a else kw.get("messages") or []))
            res = orig(*a, **kw)
            replies.append({"content": res.content or "",
                            "tool_calls": [{"name": t["name"], "arguments": t["arguments"]}
                                           for t in res.tool_calls or []]})
            return res
        loop.llm.chat = chat
    base = _copy(project)
    files0 = _snapshot(base)
    out = _run(task, base, wrap)
    doc = {"name": name, "task": task, "recorded": round(time.time()), "start_files": files0,
           "project": str(Path(project).resolve()), "replies": replies, "seen": seen,
           "outcome": out}
    shutil.copytree(_copy(project), _dir() / name / "project", dirs_exist_ok=True)
    (_dir() / name / "golden.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    say(f"recorded '{name}': {len(replies)} model replies, {len(out['tools'])} tool calls, "
        f"{out['steps']} steps")
    return doc


class _Replay:
    """A local OpenAI-style server that answers with the recorded replies."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.served = 0
        self.seen: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"replay"}]}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    outer.seen.append(_seen(json.loads(body or b"{}").get("messages") or []))
                except ValueError:
                    outer.seen.append([])
                if outer.served < len(outer.replies):
                    r = outer.replies[outer.served]
                else:
                    r = {"content": "(the recording ended here)", "tool_calls": []}
                outer.served += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if r["tool_calls"]:
                    delta = {"tool_calls": [{"index": i, "id": f"r{outer.served}_{i}", "type": "function",
                                             "function": {"name": t["name"], "arguments": t["arguments"]}}
                                            for i, t in enumerate(r["tool_calls"])]}
                    if r["content"]:
                        delta["content"] = r["content"]
                    chunks = [{"choices": [{"delta": delta}]},
                              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
                else:
                    chunks = [{"choices": [{"delta": {"content": r["content"]}}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
                for c in chunks:
                    self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def close(self):
        self.srv.shutdown()


def replay(name: str) -> dict:
    """Free: the recorded replies against the current engine → differences."""
    g = json.loads((_dir() / name / "golden.json").read_text(encoding="utf-8"))
    srv = _Replay(g["replies"])
    try:
        out = _run(g["task"], _copy(_dir() / name / "project"),
                   env={"AI_BASE_URL": srv.url, "AI_MODEL": "replay", "AI_API_KEY": "x",
                        "AI_CONTEXT_LENGTH": "131072"})
    finally:
        srv.close()
    want = g["outcome"]
    diffs = []
    if [t[0] for t in out["tools"]] != [t[0] for t in want["tools"]]:
        diffs.append(f"tools ran differently: was {[t[0] for t in want['tools']]}, "
                     f"now {[t[0] for t in out['tools']]}")
    else:
        for i, (a, b) in enumerate(zip(want["tools"], out["tools"])):
            if a[1] != b[1]:
                diffs.append(f"call {i + 1} ({a[0]}): was {a[1]}, now {b[1]}")
    changed = sorted(k for k in set(want["files"]) | set(out["files"])
                     if want["files"].get(k) != out["files"].get(k))
    if changed:
        diffs.append("files differ: " + ", ".join(changed[:10]))
    if out["steps"] != want["steps"]:
        diffs.append(f"steps: was {want['steps']}, now {out['steps']}")
    if bool(out["error"]) != bool(want["error"]):
        diffs.append(f"ending: was {want['error'] or 'finished'}, now {out['error'] or 'finished'}")
    # Where it started
    where = _first_divergence(g["seen"], srv.seen) if g.get("seen") else ""
    return {"name": name, "same": not diffs, "diffs": diffs, "served": srv.served,
            "recorded": len(g["replies"]), "where": where}


def names() -> list[str]:
    d = _dir()
    return sorted(p.name for p in d.iterdir() if (p / "golden.json").is_file()) if d.is_dir() else []

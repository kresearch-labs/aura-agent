"""acp.py — Aura inside any editor, through the Agent Client Protocol (v1).

`aura acp` runs as the editor's agent: JSON-RPC 2.0 over stdin/stdout, one
message per line. Ours, no add-on (the official library would be a new
dependency).

The same engine as the chat screen.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import threading
from pathlib import Path

PROTOCOL_VERSION = 1
MODES = [
    {"id": "act", "name": "Act", "description": "does the work — asks before risky actions"},
    {"id": "plan", "name": "Plan", "description": "reads and plans, changes nothing"},
    {"id": "discuss", "name": "Discuss", "description": "talks it through, starts nothing"},
]
_KIND = {"read_file": "read", "list_files": "read", "locate": "search", "recall": "read", "decisions": "think", "view_image": "read", "notes": "read",
         "search_files": "search", "notebook_edit": "edit", "code_nav": "search", "history_search": "search",
         "symbols": "search", "write_file": "edit", "edit_file": "edit",
         "run_command": "execute", "run_code": "execute", "process_start": "execute",
         "run_checks": "execute", "web_crawl": "fetch", "web_search": "fetch",
         "todo_write": "think"}


def _title(tool: str, args: dict) -> str:
    a = args or {}
    p = a.get("path") or a.get("file") or ""
    if tool in ("read_file", "view_image"):
        return f"Read {p}"
    if tool in ("write_file", "edit_file"):
        return f"{'Write' if tool == 'write_file' else 'Edit'} {p}"
    if tool in ("run_command", "process_start"):
        return "Run: " + str(a.get("cmd") or "")[:80]
    if tool == "search_files":
        return "Search: " + str(a.get("pattern") or a.get("query") or "")[:60]
    if tool == "web_search":
        return "Web search: " + str(a.get("query") or "")[:60]
    if tool == "web_crawl":
        return "Read " + str(a.get("url") or "")[:80]
    plain = {"finding_add": "Record a result", "finding_list": "Look at results",
             "todo_write": "Update the plan", "notes_write": "Write a note", "notes": "Read the notes",
             "memory_write": "Remember", "memory_forget": "Forget", "clarify": "Ask you",
             "state": "Read the task state", "state_write": "Save the task state",
             "run_checks": "Run the project's checks", "delegate": "Start a helper",
             "tools_find": "Look for a tool", "skill_load": "Load a skill"}
    return plain.get(tool) or tool.replace("_", " ")


class Session:
    def __init__(self, server, eng: Path, profile, m, loop, chat):
        self.server, self.eng, self.profile, self.m, self.loop, self.chat = \
            server, eng, profile, m, loop, chat
        self.id = str(chat.id)
        self.cancelled = threading.Event()
        self.open_calls: list[tuple[str, str]] = []      # (tool, call id), oldest first
        self.before: dict[str, str | None] = {}
        self.sent_text = False
        self.ids = itertools.count(1)
        loop.on_event = self.on_event
        loop.stream_sink = self.on_stream
        m.approver = self.approve
        m.asker = lambda question, options, multi=False, free=True: ""   # Aura goes on its own

    # -- what the engine reports --------------------------------------------
    def update(self, upd: dict) -> None:
        self.server.notify("session/update", {"sessionId": self.id, "update": upd})

    def on_stream(self, kind: str, text) -> None:
        if not text:
            return
        if kind == "content":
            self.sent_text = True
            self.update({"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": str(text)}})
        elif kind == "reasoning":
            self.update({"sessionUpdate": "agent_thought_chunk",
                         "content": {"type": "text", "text": str(text)}})

    def _path(self, args: dict) -> Path | None:
        p = (args or {}).get("path")
        if not p:
            return None
        ws = Path(getattr(self.loop, "workspace", None) or self.eng)
        q = Path(p)
        return q if q.is_absolute() else ws / q

    def on_event(self, kind: str, d: dict) -> None:
        try:
            if kind == "tool_start":
                tool, args = d.get("tool", "?"), d.get("args") or {}
                cid = f"call_{next(self.ids)}"
                self.open_calls.append((tool, cid))
                upd = {"sessionUpdate": "tool_call", "toolCallId": cid, "name": tool,
                       "title": _title(tool, args), "kind": _KIND.get(tool, "other"),
                       "status": "in_progress", "rawInput": args}
                p = self._path(args)
                if p is not None:
                    upd["locations"] = [{"path": str(p)}]
                    if tool in ("write_file", "edit_file"):
                        try:
                            self.before[cid] = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            self.before[cid] = None
                self.update(upd)
            elif kind == "tool_done":
                tool = d.get("tool", "?")
                cid = next((c for t, c in self.open_calls if t == tool), None)
                if cid is None:
                    return
                self.open_calls.remove((tool, cid))
                ok = d.get("status") == "ok"
                content = []
                p = self._path(d.get("args") or {})
                if ok and p is not None and tool in ("write_file", "edit_file"):
                    try:
                        content.append({"type": "diff", "path": str(p),
                                        "oldText": self.before.pop(cid, None),
                                        "newText": p.read_text(encoding="utf-8", errors="replace")})
                    except OSError:
                        pass
                if not content and d.get("result"):
                    content.append({"type": "content", "content": {
                        "type": "text", "text": str(d.get("result"))[:4000]}})
                self.update({"sessionUpdate": "tool_call_update", "toolCallId": cid,
                             "status": "completed" if ok else "failed", "content": content})
                if tool == "todo_write":
                    self.send_plan()
        except Exception:                                    # noqa: BLE001
            pass

    def send_plan(self) -> None:
        try:
            from .todo import load_items
            items = load_items(self.eng, Path(self.chat.path).stem)
        except Exception:                                    # noqa: BLE001
            return
        status = {"done": "completed", "doing": "in_progress", "in_progress": "in_progress"}
        self.update({"sessionUpdate": "plan", "entries": [
            {"content": str(i.get("text") or i.get("content") or ""), "priority": "medium",
             "status": status.get(str(i.get("status") or "").lower(), "pending")}
            for i in items if isinstance(i, dict)]})

    # -- a yes card is the editor's own permission prompt --------------------
    def approve(self, req: dict) -> str:
        tool = str(req.get("tool") or "")
        cid = next((c for t, c in reversed(self.open_calls) if t == tool), None) \
            or f"call_{next(self.ids)}"
        options = [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}]
        if req.get("can_always"):
            options.append({"optionId": "always", "name": "Allow always", "kind": "allow_always"})
        options.append({"optionId": "deny", "name": "Reject", "kind": "reject_once"})
        title = str(req.get("heading") or "needs your OK") + " — " + str(req.get("why") or "")
        res = self.server.request("session/request_permission", {
            "sessionId": self.id, "options": options,
            "toolCall": {"toolCallId": cid, "title": title[:300],
                         "rawInput": req.get("args") or {}}})
        out = (res or {}).get("outcome") or {}
        if out.get("outcome") == "selected" and out.get("optionId") in ("once", "always"):
            return out["optionId"]
        return "deny"

    # -- one prompt turn ------------------------------------------------------
    def prompt(self, blocks: list) -> dict:
        text = _text_of(blocks)
        self.cancelled.clear()
        self.sent_text = False
        try:
            from .sandbox_os import STOP
            STOP.clear()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            self.loop.llm.cancel_event = threading.Event()
        except Exception:                                    # noqa: BLE001
            pass
        rec = self.loop.run(self.chat, text, kill_check=self.cancelled.is_set)
        if self.cancelled.is_set():
            return {"stopReason": "cancelled"}
        said = getattr(rec, "content", None)
        if said and not self.sent_text:
            self.on_stream("content", said)
        if getattr(rec, "error", None):
            self.on_stream("content", f"\n\n(stopped: {rec.error})")
        return {"stopReason": "end_turn"}

    def cancel(self) -> None:
        self.cancelled.set()
        try:
            self.loop.llm.cancel_event.set()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            from .sandbox_os import STOP
            STOP.set()
        except Exception:                                    # noqa: BLE001
            pass

    def replay(self) -> None:
        from .notes import text_of
        for msg in self.chat.messages:
            role, body = msg.get("role"), text_of(msg.get("content")).strip()
            if not body or role not in ("user", "assistant"):
                continue
            self.update({"sessionUpdate": "user_message_chunk" if role == "user"
                         else "agent_message_chunk", "content": {"type": "text", "text": body}})

    def modes(self) -> dict:
        cur = getattr(self.m, "mode", "act")
        return {"currentModeId": cur if cur in ("act", "plan", "discuss") else "act",
                "availableModes": MODES}


def _text_of(blocks: list) -> str:
    """The editor's message: text, plus files it attached (embedded or linked)."""
    out = []
    for b in blocks or []:
        t = b.get("type")
        if t == "text":
            out.append(str(b.get("text") or ""))
        elif t == "resource":
            r = b.get("resource") or {}
            path = str(r.get("uri") or "").replace("file://", "")
            if r.get("text") is not None:
                out.append(f"[file {path}]\n```\n{r['text']}\n```")
        elif t == "resource_link":
            out.append(f"[file {str(b.get('uri') or '').replace('file://', '')}]")
    return "\n\n".join(x for x in out if x)


class Server:
    def __init__(self, inp=None, out=None):
        self.inp = inp or sys.stdin.buffer
        self.out = out or sys.stdout.buffer
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.waiting: dict = {}
        self.sessions: dict[str, Session] = {}
        self.running = True

    # -- the wire --------------------------------------------------------------
    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, timeout: float | None = None):
        rid = f"aura-{next(self.ids)}"
        slot = {"done": threading.Event(), "result": None}
        self.waiting[rid] = slot
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        slot["done"].wait(timeout)
        self.waiting.pop(rid, None)
        return slot["result"]

    def reply(self, rid, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self.send(msg)

    # -- methods ---------------------------------------------------------------
    def _session(self, params) -> Session:
        s = self.sessions.get(str((params or {}).get("sessionId")))
        if s is None:
            raise KeyError("no such session")
        return s

    def _open(self, cwd: str, session_file: Path | None = None) -> Session:
        from .cli import build_agent, engagement_dir
        from .engine.loop import DEFAULT_SYSTEM
        from .sessions import Session as Chat, new_session
        eng = engagement_dir(str(cwd)) if cwd else engagement_dir(None)
        profile, m, loop = build_agent(eng, None)
        chat = Chat(session_file) if session_file else new_session(eng / "sessions", "editor")
        chat.ensure_meta()
        if not chat.messages:
            chat.append("system", content=loop.cfg.system_prompt or DEFAULT_SYSTEM)
        s = Session(self, eng, profile, m, loop, chat)
        self.sessions[s.id] = s
        return s

    def handle(self, msg: dict) -> None:
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        try:
            if method == "initialize":
                from . import __version__
                self.reply(rid, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {"loadSession": True,
                                          "promptCapabilities": {"image": False, "audio": False,
                                                                 "embeddedContext": True}},
                    "agentInfo": {"name": "aura", "title": "Aura", "version": __version__},
                    "authMethods": []})
            elif method == "session/new":
                s = self._open(params.get("cwd") or "")
                self.reply(rid, {"sessionId": s.id, "modes": s.modes()})
            elif method == "session/load":
                from .cli import engagement_dir
                eng = engagement_dir(str(params.get("cwd") or "")) if params.get("cwd") else None
                found = _find_chat(eng, str(params.get("sessionId")))
                if found is None:
                    self.reply(rid, error={"code": -32602, "message": "no such chat"})
                    return
                s = self._open(params.get("cwd") or "", found)
                s.replay()
                self.reply(rid, {"modes": s.modes()})
            elif method == "session/prompt":
                self.reply(rid, self._session(params).prompt(params.get("prompt") or []))
            elif method == "session/set_mode":
                s = self._session(params)
                mode = str(params.get("modeId") or "act")
                if mode not in ("act", "plan", "discuss"):
                    raise ValueError(f"unknown mode {mode}")
                s.m.mode = mode
                self.reply(rid, {})
            elif method == "session/cancel":
                self._session(params).cancel()
            elif rid is not None:
                self.reply(rid, error={"code": -32601, "message": f"method not found: {method}"})
        except (KeyError, ValueError) as e:
            if rid is not None:
                self.reply(rid, error={"code": -32602, "message": str(e)})
        except Exception as e:                                # noqa: BLE001
            if rid is not None:
                self.reply(rid, error={"code": -32603, "message": f"{type(e).__name__}: {e}"})

    def serve(self) -> int:
        for raw in self.inp:
            line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.reply(None, error={"code": -32700, "message": "parse error"})
                continue
            if "method" not in msg and "id" in msg:            # An answer to our request
                slot = self.waiting.get(msg["id"])
                if slot is not None:
                    slot["result"] = msg.get("result")
                    slot["done"].set()
                continue
            if msg.get("method") == "session/prompt":          # Long: its own thread
                threading.Thread(target=self.handle, args=(msg,), daemon=True).start()
            else:
                self.handle(msg)
        for s in self.sessions.values():
            s.cancel()
        return 0


def _find_chat(eng, chat_id: str) -> Path | None:
    from .paths import profile_dir
    roots = [Path(eng)] if eng else []
    roots += sorted((profile_dir() / "projects").glob("*"))
    for r in roots:
        for f in (Path(r) / "sessions").glob("*.jsonl"):
            try:
                first = f.open(encoding="utf-8").readline()
                if json.loads(first or "{}").get("id") == chat_id or f.stem == chat_id:
                    return f
            except (OSError, ValueError):
                continue
    return None


def main() -> int:
    """`aura acp`: the protocol gets private copies of stdin and stdout; the
    process's own stdin becomes empty and its stdout goes to stderr — so
    nothing Aura prints or reads, and no command it runs, can touch the
    editor's pipe (a stray read of stdin swallowed the editor's messages)."""
    sys.stdout.flush()
    inp = open(os.dup(0), "rb", buffering=0)
    out = open(os.dup(1), "wb", buffering=0)
    null = open(os.devnull, "rb")
    os.dup2(null.fileno(), 0)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    sys.stdout = sys.stderr
    os.environ.setdefault("AURA_ACP", "1")
    return Server(_Lines(inp), out).serve()


class _Lines:
    """Line reader on an unbuffered pipe (one protocol message per line)."""
    def __init__(self, f):
        self.f, self.buf = f, b""

    def __iter__(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                yield line
                continue
            chunk = self.f.read(65536)
            if not chunk:
                if self.buf:
                    yield self.buf
                return
            self.buf += chunk

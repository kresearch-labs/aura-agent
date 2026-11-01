"""wire_native.py — Anthropic's Messages API and OpenAI's Responses API, spoken
natively.

Why: through the OpenAI-compatible addresses both providers give less —
Anthropic's has no prompt caching and hides thinking; OpenAI's newest models
answer only on Responses, and only Responses carries a model's reasoning from
one step to the next (encrypted, with store=false).

How: Aura keeps one message format inside (chat-completions). The body
LLMClient builds is turned into the native request here; the native stream is
turned back into chat-shaped chunks, so the stall watch, thinking split and
tool-call assembly in llm.py run unchanged. What a native reply must get back
on the next call.

OpenAI — POST responses with `store: false` and `include:
["reasoning.encrypted_content"]`; reasoning items are sent back as they came;
tools are flat {type, name, description, parameters}; tool results are
`function_call_output` items. Standard library only.
"""
from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import urlparse

ANTHROPIC_VERSION = "2026-10-21"
THINKING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_MAX_TOKENS = 32000         # Only when the model list can't be read


def api_for(base_url: str) -> str:
    """chat | messages | responses — the option model.api, else by address."""
    try:
        from .options import get
        choice = str(get("model.api") or "auto")
    except Exception:                                    # noqa: BLE001
        choice = "auto"
    if choice in ("chat", "messages", "responses"):
        return choice
    host = (urlparse(base_url or "").hostname or "").lower()
    if host == "api.anthropic.com":
        return "messages"
    if host == "api.openai.com":
        return "responses"
    return "chat"


def strip_native(messages: list[dict]) -> list[dict]:
    """Chat-completions servers never see the saved native blocks."""
    return [{k: v for k, v in m.items() if k != "native"} if "native" in m else m
            for m in messages]


# ------------------------------------------------------------ shared bits --
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _data_url(url: str):
    m = re.match(r"data:([^;]+);base64,(.*)", url or "", re.S)
    return (m.group(1), m.group(2)) if m else None


def _args(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": str(raw)}


def _schema(tool: dict) -> tuple[str, str, dict]:
    fn = tool.get("function") or tool
    params = fn.get("parameters") or {"type": "object", "properties": {}}
    return str(fn.get("name", "")), str(fn.get("description", "") or ""), params


# --------------------------------------------------------------- Anthropic --
_INFO: dict = {}


def model_info(base_url: str, model: str, key: str | None) -> dict:
    """{"max_tokens": int|None, "adaptive": bool} from Anthropic's own model
    list — read once per model per run."""
    k = (base_url, model)
    if k not in _INFO:
        info: dict = {}
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/models/" + model,
                headers={"anthropic-version": ANTHROPIC_VERSION,
                         **({"x-api-key": key} if key else {})})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r) or {}
            info["max_tokens"] = int(d.get("max_tokens") or 0) or None
            types = ((d.get("capabilities") or {}).get("thinking") or {}).get("types") or {}
            info["adaptive"] = bool((types.get("adaptive") or {}).get("supported"))
        except Exception:                                # noqa: BLE001
            pass
        _INFO[k] = info
    return _INFO[k]


_ID_OK = re.compile(r"[^A-Za-z0-9_-]")


def _tid(v) -> str:
    return _ID_OK.sub("_", str(v or "")) or "call"


def to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """(system text, messages) — leading system messages become the system prompt;
    later ones go into the conversation as Aura's labelled notes.
    """
    from .notes import aura_note
    system: list[str] = []
    out: list[dict] = []
    started = False

    def put(role, blocks):
        blocks = [b for b in blocks if not (b.get("type") == "text" and not b.get("text"))]
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role, c = m.get("role"), m.get("content")
        if role == "system":
            if not started:
                system.append(_text(c))
            else:
                put("user", [{"type": "text", "text": aura_note(_text(c))}])
            continue
        started = True
        if role == "user":
            blocks = []
            for p in (c if isinstance(c, list) else [{"type": "text", "text": _text(c)}]):
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    blocks.append({"type": "text", "text": str(p.get("text", ""))})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    du = _data_url(url)
                    blocks.append({"type": "image", "source": (
                        {"type": "base64", "media_type": du[0], "data": du[1]} if du
                        else {"type": "url", "url": url})})
            put("user", blocks)
        elif role == "assistant":
            nat = m.get("native") or {}
            blocks = [b for b in (nat.get("blocks") or []) if nat.get("api") == "messages"
                      and b.get("type") in ("thinking", "redacted_thinking")]
            if _text(c):
                blocks.append({"type": "text", "text": _text(c)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                blocks.append({"type": "tool_use", "id": _tid(tc.get("id")),
                               "name": fn.get("name", ""), "input": _args(fn.get("arguments"))})
            put("assistant", blocks)
        elif role == "tool":
            put("user", [{"type": "tool_result", "tool_use_id": _tid(m.get("tool_call_id")),
                          "content": _text(c) or "(no output)"}])
    # tool results must open the user message that answers the tool calls
    for msg in out:
        if msg["role"] == "user":
            msg["content"].sort(key=lambda b: b.get("type") != "tool_result")
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    if out[-1]["role"] == "assistant":
        # Ending on the model's own turn is a "prefill".
        out.append({"role": "user", "content": [{"type": "text", "text": aura_note("(continue)")}]})
    return "\n\n".join(s for s in system if s), out


def anthropic_request(base_url: str, key: str | None, body: dict) -> tuple[str, dict, dict]:
    info = model_info(base_url, body["model"], key)
    system, msgs = to_anthropic(body.get("messages") or [])
    out: dict = {"model": body["model"], "messages": msgs, "stream": True,
                 "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens")
                                   or info.get("max_tokens") or FALLBACK_MAX_TOKENS),
                 "cache_control": {"type": "ephemeral"}}
    if system:
        out["system"] = system
    if body.get("tools"):
        out["tools"] = [{"name": n, "description": d, "input_schema": p}
                        for n, d, p in map(_schema, body["tools"])]
        out["tool_choice"] = {"type": "auto"}
    headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION,
               **({"x-api-key": key} if key else {})}
    if info.get("adaptive"):
        out["thinking"] = {"type": "adaptive", "display": "summarized",
                           "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        headers["anthropic-beta"] = THINKING_BETA
    return base_url.rstrip("/") + "/messages", out, headers


class AnthropicStream:
    """Anthropic SSE events → chat-shaped chunks; keeps the thinking blocks."""
    _STOP = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length",
             "stop_sequence": "stop", "refusal": "content_filter", "pause_turn": "stop"}

    def __init__(self):
        self.blocks: dict[int, dict] = {}
        self.tool_index: dict[int, int] = {}
        self.usage: dict = {}

    def native(self) -> dict | None:
        keep = [self.blocks[i] for i in sorted(self.blocks)
                if self.blocks[i].get("type") in ("thinking", "redacted_thinking")]
        return {"api": "messages", "blocks": keep} if keep else None

    def _usage(self) -> dict:
        u = self.usage
        cached = int(u.get("cache_read_input_tokens") or 0)
        prompt = int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": int(u.get("output_tokens") or 0),
                "total_tokens": prompt + int(u.get("output_tokens") or 0),
                "cache_read_input_tokens": cached,
                "prompt_tokens_details": {"cached_tokens": cached}}

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        delta: dict = {}
        if t == "message_start":
            self.usage.update((ev.get("message") or {}).get("usage") or {})
        elif t == "content_block_start":
            i, b = ev.get("index", 0), dict(ev.get("content_block") or {})
            self.blocks[i] = b
            if b.get("type") == "tool_use":
                n = self.tool_index[i] = len(self.tool_index)
                b["_json"] = ""
                delta = {"tool_calls": [{"index": n, "id": b.get("id", ""),
                                         "function": {"name": b.get("name", ""), "arguments": ""}}]}
            elif b.get("type") == "text" and b.get("text"):
                delta = {"content": b["text"]}
        elif t == "content_block_delta":
            i, d = ev.get("index", 0), ev.get("delta") or {}
            b = self.blocks.setdefault(i, {})
            k = d.get("type")
            if k == "text_delta":
                delta = {"content": d.get("text", "")}
            elif k == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                delta = {"reasoning_content": d.get("thinking", "")}
            elif k == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
            elif k == "input_json_delta" and i in self.tool_index:
                b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                delta = {"tool_calls": [{"index": self.tool_index[i],
                                         "function": {"arguments": d.get("partial_json", "")}}]}
        elif t == "content_block_stop":
            b = self.blocks.get(ev.get("index", 0)) or {}
            if b.get("type") == "tool_use" and not b.get("_json"):
                # no input streamed: the call takes no arguments
                delta = {"tool_calls": [{"index": self.tool_index[ev.get("index", 0)],
                                         "function": {"arguments": "{}"}}]}
        elif t == "message_delta":
            self.usage.update(ev.get("usage") or {})
            reason = self._STOP.get(((ev.get("delta") or {}).get("stop_reason")) or "", None)
            return [{"choices": [{"delta": {}, "finish_reason": reason}]},
                    {"choices": [], "usage": self._usage()}]
        elif t == "error":
            err = ev.get("error") or {}
            raise ConnectionError(f"{err.get('type', 'error')}: {err.get('message', '')}"[:300])
        return [{"choices": [{"delta": delta}]}] if delta else []


# ------------------------------------------------------------ OpenAI Responses --
def to_responses(messages: list[dict], model: str) -> tuple[str, list[dict]]:
    """(instructions, input items)."""
    lead: list[str] = []
    items: list[dict] = []
    started = False
    for m in messages:
        role, c = m.get("role"), m.get("content")
        if role == "system":
            if not started:
                lead.append(_text(c))
            else:
                items.append({"role": "developer", "content": _text(c)})
            continue
        started = True
        if role == "user":
            parts = []
            for p in (c if isinstance(c, list) else [{"type": "text", "text": _text(c)}]):
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"type": "input_text", "text": str(p.get("text", ""))})
                elif p.get("type") == "image_url":
                    parts.append({"type": "input_image",
                                  "image_url": (p.get("image_url") or {}).get("url", "")})
            if parts:
                items.append({"role": "user", "content": parts})
        elif role == "assistant":
            nat = m.get("native") or {}
            if nat.get("api") == "responses" and nat.get("model") == model:
                items.extend(nat.get("items") or [])
            if _text(c):
                items.append({"role": "assistant", "content": _text(c)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({"type": "function_call", "call_id": str(tc.get("id", "")),
                              "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": str(m.get("tool_call_id", "")),
                          "output": _text(c) or "(no output)"})
    return "\n\n".join(s for s in lead if s), items


def responses_request(base_url: str, key: str | None, body: dict) -> tuple[str, dict, dict]:
    instructions, items = to_responses(body.get("messages") or [], body["model"])
    out: dict = {"model": body["model"], "input": items, "stream": True, "store": False,
                 "include": ["reasoning.encrypted_content"],
                 "reasoning": {"summary": "auto"}}
    if instructions:
        out["instructions"] = instructions
    if body.get("tools"):
        out["tools"] = [{"type": "function", "name": n, "description": d, "parameters": p}
                        for n, d, p in map(_schema, body["tools"])]
        out["tool_choice"] = "auto"
    mx = body.get("max_tokens") or body.get("max_completion_tokens")
    if mx:
        out["max_output_tokens"] = int(mx)
    for k in ("temperature", "top_p"):  # wire_fit drops one a model refuses
        if k in body:
            out[k] = body[k]
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_object":
        out["text"] = {"format": {"type": "json_object"}}
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    return base_url.rstrip("/") + "/responses", out, headers


class ResponsesStream:
    """Responses SSE events → chat-shaped chunks; keeps the reasoning items."""

    def __init__(self, model: str):
        self.model = model
        self.reasoning: list[dict] = []
        self.tool_index: dict[str, int] = {}
        self.with_args: set[str] = set()
        self.summary_open: set[str] = set()

    def native(self) -> dict | None:
        return ({"api": "responses", "model": self.model, "items": self.reasoning}
                if self.reasoning else None)

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type", "")
        delta: dict = {}
        if t == "response.output_text.delta":
            delta = {"content": ev.get("delta", "")}
        elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = {"reasoning_content": ev.get("delta", "")}
        elif t == "response.reasoning_summary_part.added":
            iid = ev.get("item_id", "")
            if iid in self.summary_open:
                delta = {"reasoning_content": "\n\n"}
            self.summary_open.add(iid)
        elif t == "response.output_item.added":
            it = ev.get("item") or {}
            if it.get("type") == "function_call":
                n = self.tool_index[it.get("id") or it.get("call_id")] = len(self.tool_index)
                delta = {"tool_calls": [{"index": n, "id": it.get("call_id", ""),
                                         "function": {"name": it.get("name", ""), "arguments": ""}}]}
        elif t == "response.function_call_arguments.delta":
            iid = ev.get("item_id", "")
            if iid in self.tool_index:
                self.with_args.add(iid)
                delta = {"tool_calls": [{"index": self.tool_index[iid],
                                         "function": {"arguments": ev.get("delta", "")}}]}
        elif t == "response.output_item.done":
            it = ev.get("item") or {}
            if it.get("type") == "reasoning":
                keep = {k: it[k] for k in ("type", "id", "summary", "encrypted_content") if k in it}
                keep.setdefault("summary", [])
                self.reasoning.append(keep)
            elif it.get("type") == "function_call":
                iid = it.get("id") or it.get("call_id")
                if iid in self.tool_index and iid not in self.with_args:
                    delta = {"tool_calls": [{"index": self.tool_index[iid],
                                             "function": {"arguments": it.get("arguments") or "{}"}}]}
        elif t in ("response.completed", "response.incomplete"):
            r = ev.get("response") or {}
            u = r.get("usage") or {}
            cached = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
            usage = {"prompt_tokens": int(u.get("input_tokens") or 0),
                     "completion_tokens": int(u.get("output_tokens") or 0),
                     "total_tokens": int(u.get("total_tokens") or 0),
                     "prompt_tokens_details": {"cached_tokens": cached}}
            reason = "tool_calls" if self.tool_index else "stop"
            if t == "response.incomplete":
                why = (r.get("incomplete_details") or {}).get("reason")
                reason = "length" if why == "max_output_tokens" else "content_filter"
            return [{"choices": [{"delta": {}, "finish_reason": reason}]},
                    {"choices": [], "usage": usage}]
        elif t in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev
            raise ConnectionError(f"{err.get('code') or 'error'}: {err.get('message', '')}"[:300])
        return [{"choices": [{"delta": delta}]}] if delta else []

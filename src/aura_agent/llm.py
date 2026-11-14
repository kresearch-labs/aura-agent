"""llm.py — OpenAI-compatible streaming client with tool-call delta assembly.

Uses stdlib urllib (zero mandatory deps at this layer). Handles:
  - SSE streaming + timeout handling for slow local first tokens
  - tool_call deltas accumulated by index (the upgrade.py 422-430 pattern)
  - reasoning_content passthrough (vLLM/DeepSeek reasoning models)
  - retry/backoff on transient failures
  - quirk-flags from the provider profile

Contract: this module is transport only. Sessions/governor live elsewhere.
"""
from __future__ import annotations

import json
import re
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


def _try_fallback(primary_name: str):
    """2.8: first live fallback after the primary. Returns (name, profile)
    or (None, None). Lazy import to keep transport-only layering."""
    try:
        from .fallback import pick_fallback
        return pick_fallback(primary_name, require_live=True)
    except Exception:
        return None, None

from .config import ProviderProfile

TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError)


class ContextOverflow(RuntimeError):
    """The provider rejected the request because the prompt does not fit
    the model's context window. Retrying the same request cannot succeed;
    the caller must shrink the context first (loop: compact + retry)."""


class ProviderRejected(RuntimeError):
    """A non-retryable client error (bad request, unknown model, ...).
    Raised immediately with the provider's own message instead of being
    retried as a network fault and reported as 'unreachable'."""


# These client codes are retryable or have their own handling; every other 4xx
# is not.
_RETRYABLE_4XX = (401, 403, 408, 409, 429)
_OVERFLOW_MARKERS = (
    "context_length_exceeded", "maximum context length", "context length",
    "context window", "too many tokens", "prompt is too long",
    "input is too long", "reduce the length", "exceeds the model",
    "context size", "exceeds the available context", "exceeds context",
)


def context_limit_in(message: str) -> int | None:
    """The real context size a server names when it refuses a prompt as
    too long ("... available context size (4096 tokens)", "maximum
    context length is 8192 tokens"), or None."""
    import re
    m = re.search(r"(?:context (?:size|length|window)|max(?:imum)? "
                  r"(?:context|tokens?))\D{0,40}?(\d{3,8})\s*tokens",
                  message or "", re.I)
    if not m:       # "prompt is too long: 9000 tokens > 200000 maximum"
        m = re.search(r">\s*(\d{3,8})\s*(?:tokens?\s*)?max(?:imum)?",
                      message or "", re.I)
    return int(m.group(1)) if m else None


def _http_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def unreachable_message(e: BaseException, profile, tries: int) -> str:
    """Plain words for a model server that kept failing: what happened, the error
    code it gave, and what to try.
    """
    where = (f"{getattr(profile, 'model', '?')} at "
             f"{getattr(profile, 'base_url', '?')}")
    code = getattr(e, "code", None)
    reason = str(getattr(e, "reason", "") or e)
    low = reason.lower()
    if isinstance(code, int) and code == 429:
        why = ("the server says too many requests — it is limiting how "
               "fast you can use it")
    elif isinstance(code, int) and code >= 500:
        why = "the model server had a problem of its own"
    elif isinstance(code, int) and code == 408:
        why = "the server gave up waiting for the request"
    elif isinstance(code, int):
        why = "the server refused the request"
    elif "refused" in low:
        why = "nothing is answering at that address — is the model server running?"
    elif "timed out" in low or isinstance(e, TimeoutError):
        why = "the server didn't answer in time"
    elif any(k in low for k in ("getaddrinfo", "name or service", "nodename")):
        why = "that address can't be found — check the URL"
    else:
        why = "the connection failed"
    err = f"HTTP {code}" if isinstance(code, int) else reason
    end = "" if why.endswith("?") else "."
    return (f"The model is unreachable ({where}) — tried {tries} times.\n"
            f"  Why: {why}{end}\n  Error: {err}\n"
            "  Try: check the server, then run the task again — or switch "
            "model (aura model / /model).")


@dataclass
class ToolCallAccumulator:
    """Assemble streamed tool_call deltas into complete calls."""
    calls: dict[int, dict] = field(default_factory=dict)

    def add(self, delta_calls: list[dict]) -> list[dict]:
        for tc in delta_calls or []:
            idx = tc.get("index", 0)
            slot = self.calls.setdefault(
                idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            slot["name"] += fn.get("name") or ""
            slot["arguments"] += fn.get("arguments") or ""
        return [self.calls[i] for i in sorted(self.calls)
                if self._complete(self.calls[i])]

    @staticmethod
    def _complete(c: dict) -> bool:
        return bool(c["id"]) and bool(c["name"]) and _valid_json(c["arguments"])


def _valid_json(s: str) -> bool:
    try:
        json.loads(s or "{}")
        return True
    except json.JSONDecodeError:
        return False


def parse_tool_args(raw: str | None) -> tuple[dict, str | None]:
    """ADD-2: tool-call arguments → (args, error).

    Weak/flash models emit malformed argument JSON; a bare json.loads used
    to raise and kill the step. Repair only the malformations that cannot
    change meaning — code fences, trailing commas, double-encoded JSON,
    Python literals — and on failure return ({}, error) so the loop hands
    the error back to the model as a tool result instead of crashing.

    Truncated arguments are deliberately NOT repaired: closing a cut-off
    string or object would execute a different call than intended (a
    shortened shell command, a dropped "dry_run" key). Resending is the
    only safe fix.
    """
    import ast
    import re
    s = (raw or "").strip()
    if not s:
        return {}, None
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    for c in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            v = json.loads(c)
        except json.JSONDecodeError:
            try:
                v = ast.literal_eval(c)       # True/None/'single quotes'
            except (ValueError, SyntaxError):
                continue
        if isinstance(v, str):                # Double-encoded: "{\"a\": 1}"
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                continue
        if isinstance(v, dict):
            return v, None
    return {}, (f"could not parse tool arguments as a JSON object: "
                f"{(raw or '')[:200]!r} — re-send the call with valid JSON "
                f"arguments")


@dataclass
class StreamResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    first_token_ms: int = 0
    duration_s: float = 0.0
    # What a native API needs back on the next call (wire_native.py).
    native: dict | None = None


def _stall_limits() -> tuple[int, int]:
    """(gap seconds after the first word, total seconds per reply) — the options
    model.stall_seconds / model.reply_minutes.
    """
    try:
        from .options import get
        return int(get("model.stall_seconds")), int(get("model.reply_minutes")) * 60
    except Exception:                                # noqa: BLE001
        return 90, 30 * 60


def _shorten_wait(resp, seconds: int) -> None:
    """After the first word, a silent socket ends the read after `seconds`
    instead of the long first-word wait. Best effort (standard library
    response internals); without it the gap check still runs per line."""
    try:
        sock = resp.fp.raw._sock
        sock.settimeout(max(1, seconds))
    except Exception:                                # noqa: BLE001
        pass


_SYSROLE = re.compile(r"system (role|message|prompt)s? (is |are )?(not supported|unsupported|"
                      r"not allowed)|unsupported role:? ?.?system|role .?system.? (is )?not",
                      re.I)


def merge_system(messages: list[dict]) -> list[dict]:
    """System messages folded into user text, for models that reject the
    system role: leading ones go at the start of the first user message,
    later ones become user notes. Never mutates the input."""
    # labelled: folded-in text is Aura's, never the user's words
    from .notes import aura_note
    out: list[dict] = []
    lead: list[str] = []
    for m in messages:
        if m.get("role") == "system":
            text = str(m.get("content", "") or "")
            if any(x.get("role") == "user" for x in out):
                out.append({"role": "user", "content": aura_note(text)})
            else:
                lead.append(text)
            continue
        if lead and m.get("role") == "user":
            body = m.get("content", "")
            head = aura_note("\n\n".join(lead)) + "\n\n" + _USER_SAYS
            if isinstance(body, list):
                body = [{"type": "text", "text": head}] + body
            else:
                body = head + str(body or "")
            m = {**m, "content": body}
            lead = []
        out.append(m)
    if lead:
        out.insert(0, {"role": "user", "content": aura_note("\n\n".join(lead))})
    return out


_USER_SAYS = "[THE USER'S MESSAGE]\n"


class LLMClient:
    def __init__(self, profile: ProviderProfile):
        self.p = profile
        # set when a fallback model took over during a call; the loop
        # reads it, tells the user, and clears it
        self.switched: str | None = None

    # ------------------------------------------------------------- streaming
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             on_delta=None, max_retries: int = 3,
             extra: dict | None = None, side: bool = False) -> StreamResult:
        # The server's own default applies; a limit is sent only when the user
        # sets AI_MAX_TOKENS or a caller asks for one.
        if max_tokens is None:
            try:                   # Settings model.max_reply_tokens; AI_MAX_TOKENS overrides
                from .options import get as _oget
                max_tokens = int(_oget("model.max_reply_tokens") or 0) or None
            except Exception:      # noqa: BLE001
                max_tokens = None
        # who wrote a message stays in Aura; pasted images are read from
        # their files only for the current turn (notes.py)
        from .notes import for_wire
        messages = for_wire(messages)
        # A model measured to reject the system role gets that text at the
        # start of the first user message instead (no odd failures)
        try:
            from .model_facts import fact as _fact, model_key as _mkey
            if _fact("system_role", _mkey(self.p.base_url, self.p.model)) == "no":
                messages = merge_system(messages)
        except Exception:                               # noqa: BLE001
            pass
        maker: dict = {}
        if temperature is None:
            try:
                from .model_facts import model_key as _skey
                from .sampling import in_use
                maker = in_use(_skey(self.p.base_url, self.p.model))
            except Exception:                           # noqa: BLE001
                maker = {}
        body = {
            "model": self.p.model,
            "messages": messages,
            "temperature": (temperature if temperature is not None
                            else maker.get("temperature", 0.4)),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        for _k, _v in maker.items():
            if _k != "temperature":
                body[_k] = _v
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.p.reasoning:
            # ask for reasoning tokens on providers that support it; vLLM
            # reasoning parsers emit reasoning_content in deltas automatically
            body["chat_template_kwargs"] = {"enable_thinking": True}
        if not side:
            # /think: how hard the model thinks (thinking.py); a side call
            # (the message check, summaries) keeps the model's default
            from .thinking import fields, level
            for _k, _v in fields(level(self.p)).items():
                body[_k] = _v
        if extra:
            body.update(extra)          # e.g. response_format (checks it works)
        # only what this model accepts (wire_fit: learned from its refusals)
        from . import wire_fit
        from .model_facts import model_key as _wkey
        keep = frozenset(extra or ())
        wkey = _wkey(self.p.base_url, self.p.model)
        wire_fit.apply(body, wkey, keep)
        # where the prompt stopped being the same as the last call (cache_inspect)
        try:
            if not side:                # A side call isn't the main chat's cache story
                from .cache_inspect import fingerprint, compare
                fp = fingerprint(messages, tools)
                self.last_cache = compare(getattr(self, "_cache_fp", None), fp)
                self._cache_fp = fp
                self.calls_n = getattr(self, "calls_n", 0) + 1
        except Exception:                               # noqa: BLE001
            self.last_cache = None

        attempt, backoff = 0, 2.0
        pool = self._key_pool()
        kidx = 0
        # a retry after part of the reply was already shown starts the reply
        # from scratch — say so on screen instead of silently printing it a
        # second time underneath the cut-off copy
        shown = {"any": False}
        caller_delta = on_delta
        if caller_delta is not None:
            def on_delta(kind, text):
                if kind in ("content", "reasoning", "tool_call"):
                    shown["any"] = True
                caller_delta(kind, text)
        while True:
            if shown["any"]:
                shown["any"] = False
                caller_delta("restart", "connection dropped — the reply "
                             "starts again")
            try:
                res = self._stream(body, on_delta, api_key=pool[kidx % len(pool)])
                if not side:
                    # does this server reuse the start of a call? (the loop
                    # sends side calls the cache-friendly way only then)
                    try:
                        from .model_facts import _cached_tokens
                        if (_cached_tokens(res.usage) or 0) > 0:
                            self.server_caches = True
                    except Exception:                   # noqa: BLE001
                        pass
                return res
            except TRANSIENT as e:
                # HTTPError subclasses URLError: classify before retrying.
                # Non-retryable client errors fail fast with the provider's
                # message; 401/403/408/409/429 and 5xx keep the retry path.
                code = getattr(e, "code", 0) or 0
                if isinstance(e, urllib.error.HTTPError) \
                        and 400 <= code < 500 and code not in _RETRYABLE_4XX:
                    detail = _http_body(e)
                    # one of Aura's own optional fields refused → leave it
                    # out (or rename it), remember, send again
                    if code in (400, 422) and wire_fit.heal(
                            body, detail, _wkey(self.p.base_url, body.get("model", "")),
                            keep):
                        continue
                    # A server that refuses the "system" role: learned the
                    # first time it says so, remembered for this model, and the
                    # same call sent again with Aura's text at the start of the
                    # user's
                    if code in (400, 422) and _SYSROLE.search(detail)                             and any(m.get("role") == "system" for m in body.get("messages") or []):
                        try:
                            from .model_facts import model_key as _mk, save_facts as _sf
                            from .model_facts import load as _ld
                            _key = _mk(self.p.base_url, self.p.model)
                            _caps = dict((_ld().get(_key) or {}).get("caps") or {})
                            _caps["system_role"] = {"value": "no", "how": "measured",
                                                    "note": "the server refused it"}
                            _sf(_key, {"caps": _caps})
                        except Exception:               # noqa: BLE001
                            pass
                        body["messages"] = merge_system(body["messages"])
                        continue
                    if code in (400, 413) and any(
                            m in detail.lower() for m in _OVERFLOW_MARKERS):
                        raise ContextOverflow(
                            f"context window full (HTTP {code}): "
                            f"{detail[:300]}") from e
                    raise ProviderRejected(
                        f"provider rejected the request (HTTP {code}): "
                        f"{detail[:300] or e.reason}") from e
                # Credential rotation on rate limits — a 401/403/429 with a
                # multi-key pool rotates to the next key (free retry), instead
                # of burning the retry budget on a key-shaped problem.
                rate = isinstance(e, urllib.error.HTTPError) and \
                    getattr(e, "code", 0) in (401, 403, 429)
                if (isinstance(e, urllib.error.HTTPError)
                        and getattr(e, "code", 0) in (401, 403)
                        and kidx >= len(pool) - 1):
                    raise ProviderRejected(
                        f"the provider refused the API key (HTTP {e.code}) "
                        f"for '{self.p.name}' — check or replace it: "
                        f"aura setup / aura keys. {_http_body(e)[:200]}"
                    ) from e
                if rate and len(pool) > 1 and kidx < len(pool) - 1:
                    kidx += 1
                    attempt += 1
                    if attempt > max_retries:
                        fb_name, fb_profile = _try_fallback(self.p.name)
                        if fb_profile is not None:
                            self.switched = (f"'{self.p.name}' kept failing "
                                             f"— now using fallback "
                                             f"'{fb_name}' ({fb_profile.model})")
                            self.p = fb_profile
                            body["model"] = fb_profile.model
                            pool = self._key_pool()
                            kidx = 0
                            attempt = 0
                            continue
                        raise RuntimeError(unreachable_message(
                            e, self.p, max_retries + 1)) from e
                    time.sleep(backoff * attempt)
                    continue
                attempt += 1
                if attempt > max_retries:
                    # 2.8: fallback chain — transparent re-route when the
                    # primary is dead and a fallback is configured+live
                    fb_name, fb_profile = _try_fallback(self.p.name)
                    if fb_profile is not None:
                        self.switched = (f"'{self.p.name}' is unreachable — "
                                         f"now using fallback '{fb_name}' "
                                         f"({fb_profile.model})")
                        self.p = fb_profile
                        body["model"] = fb_profile.model
                        attempt = 0        # Fresh retry budget on fallback
                        continue
                    # plain words: what happened, the code, what to try
                    raise RuntimeError(unreachable_message(
                        e, self.p, max_retries + 1)) from e
                time.sleep(backoff * attempt)

    def _key_pool(self) -> list:
        """A pool of one = rotation disabled (current behavior)."""
        raw = os.environ.get("AURA_API_KEYS") or os.environ.get("AI_API_KEYS") or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if self.p.api_key and self.p.api_key not in keys:
            keys.insert(0, self.p.api_key)
        return keys or [None]

    def _stream(self, body: dict, on_delta, api_key=None) -> StreamResult:
        key = api_key if api_key is not None else self.p.api_key
        # Anthropic Messages / OpenAI Responses spoken natively; their events
        # become chat-shaped chunks below (wire_native.py)
        from . import wire_native as WN
        api = WN.api_for(self.p.base_url)
        native = None
        if api == "messages":
            url, wire, headers = WN.anthropic_request(self.p.base_url, key, body)
            native = WN.AnthropicStream()
        elif api == "responses":
            url, wire, headers = WN.responses_request(self.p.base_url, key, body)
            native = WN.ResponsesStream(body["model"])
        else:
            url = self.p.base_url.rstrip("/") + "/chat/completions"
            wire = dict(body, messages=WN.strip_native(body.get("messages") or []))
            headers = {"Content-Type": "application/json",
                       **({"Authorization": f"Bearer {key}"} if key else {})}
        req = urllib.request.Request(url, data=json.dumps(wire).encode(),
                                     headers=headers, method="POST")
        t0 = time.monotonic()
        acc = ToolCallAccumulator()
        out = StreamResult()
        timings: dict = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        got_first = False
        from .stream_guard import ProgressWatch, ThinkSplit, split_closed
        watch = ProgressWatch()             # Thinking in circles inside this reply
        splitter = ThinkSplit(assume_open=getattr(self, "_bare_close", False))  # <think>…</think> in the answer

        cancel = getattr(self, "cancel_event", None)
        # The first word may take first_token_timeout; after it, a gap with no
        # progress ends the stream, and so does a total limit. Keep-alive lines
        # (".
        gap_s, total_s = _stall_limits()
        last_progress = t0

        def _stalled(now: float) -> str | None:
            if now - t0 > total_s:
                return f"the reply took longer than {total_s // 60} minutes"
            wait = gap_s if got_first else self.p.first_token_timeout
            if now - last_progress > wait:
                return (f"no progress for {int(now - last_progress)} s"
                        + ("" if got_first else " waiting for the first word"))
            return None

        with urllib.request.urlopen(req, timeout=self.p.first_token_timeout) as r:
            for raw in r:
                # the chat's Esc: stop reading; what arrived so far is kept
                if cancel is not None and cancel.is_set():
                    out.finish_reason = "cancelled"
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                why = _stalled(time.monotonic())
                if why:
                    raise TimeoutError(f"stalled: {why}")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for chunk in (native.feed(chunk) if native is not None else [chunk]):
                    if self._chunk(chunk, out, acc, splitter, watch, on_delta,
                                   content_parts, reasoning_parts, timings):
                        last_progress = time.monotonic()
                        if not got_first:
                            out.first_token_ms = int((time.monotonic() - t0) * 1000)
                            got_first = True
                            _shorten_wait(r, gap_s)   # Silence now ends sooner
                    if out.finish_reason == "cycle_guard":
                        break
                if out.finish_reason == "cycle_guard":
                    break                   # Stop reading: the reply is thrown away
        if native is not None:
            out.native = native.native()
        return self._finish(out, acc, splitter, content_parts, reasoning_parts, timings, t0)

    def _chunk(self, chunk, out, acc, splitter, watch, on_delta, content_parts,
               reasoning_parts, timings) -> bool:
        """One chat-shaped chunk into the reply. True when it carried
        progress (text, thinking or a tool call)."""
        progressed = False
        if usage := chunk.get("usage"):
            out.usage = usage
            if on_delta:
                on_delta("usage", usage)   # Real-time usage event
        if isinstance(chunk.get("timings"), dict):     # llama.cpp server
            timings.update(chunk["timings"])
        for ch in chunk.get("choices") or []:
            out.finish_reason = ch.get("finish_reason") or out.finish_reason
            d = ch.get("delta") or {}
            # vLLM / DeepSeek send "reasoning_content"; Ollama
            # (and others) send "reasoning"
            rc = d.get("reasoning_content") or d.get("reasoning")
            if d.get("content") or d.get("tool_calls") or rc:
                progressed = True
            # thinking sent inside the answer (<think>…</think>) is
            # moved to the thinking stream, so it is watched and shown
            # as thinking (servers without a reasoning parser)
            parts = [("reasoning", rc)] if rc else []
            if c := d.get("content"):
                parts += splitter.feed(c)
            for kind, text in parts:
                if kind == "reasoning":
                    reasoning_parts.append(text)
                    if on_delta:
                        on_delta("reasoning", text)
                    if watch.feed(text):
                        out.finish_reason = "cycle_guard"
                        break
                else:
                    content_parts.append(text)
                    if on_delta:
                        on_delta("content", text)
            if out.finish_reason == "cycle_guard":
                break
            if dc := d.get("tool_calls"):
                for done in acc.add(dc):
                    if on_delta:
                        on_delta("tool_call", done)
        return progressed

    def _finish(self, out, acc, splitter, content_parts, reasoning_parts, timings, t0):
        from .stream_guard import split_closed
        # llama.cpp reports its prompt cache only as timings.cache_n: give
        # it the usual shape when the usage said nothing about the cache
        if timings and "cache_n" in timings and isinstance(out.usage, dict) and not any(
                k in out.usage for k in ("prompt_tokens_details", "prompt_cache_hit_tokens",
                                         "cache_read_input_tokens", "cached_tokens")):
            out.usage = dict(out.usage, prompt_tokens_details={"cached_tokens": int(timings["cache_n"] or 0)})
        for kind, text in splitter.finish():
            (reasoning_parts if kind == "reasoning" else content_parts).append(text)
        out.content = "".join(content_parts)
        out.reasoning = "".join(reasoning_parts)
        if out.reasoning:
            try:
                from .model_facts import model_key as _tkey
                from .thinking import seen
                seen(_tkey(self.p.base_url, self.p.model), True)
            except Exception:                           # noqa: BLE001
                pass
        if splitter.assumed and not splitter.closed:
            # started as thinking on this model's habit, but no "</think>"
            # came: it was the answer after all
            out.content, out.reasoning = out.reasoning + out.content, ""
        elif not out.reasoning:              # Only "</think>" arrived (opening tag in the prompt)
            out.reasoning, out.content = split_closed(out.content)
            if out.reasoning:
                self._bare_close = True      # Its next replies start as thinking
        out.tool_calls = [acc.calls[i] for i in sorted(acc.calls)]
        for tc in out.tool_calls:
            tc["parsed_arguments"], err = parse_tool_args(tc["arguments"])
            if err:
                tc["parse_error"] = err
        out.duration_s = round(time.monotonic() - t0, 2)
        return out

    # ------------------------------------------------------------- utilities
    def ping(self) -> dict:
        """x doctor: endpoint reachable + models list."""
        req = urllib.request.Request(
            self.p.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {self.p.api_key}"}
            if self.p.api_key else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.load(r) or {}
                models = [m.get("id", "?") for m in body.get("data", [])
                          if isinstance(m, dict)]
            return {"ok": True, "models": models}
        except Exception as e:
            return {"ok": False, "error": str(e)}

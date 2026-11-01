"""wire_fit.py — send only what the model accepts.

Aura adds a few fields of its own to every request: temperature, a reply size
limit, stream_options, chat_template_kwargs (vLLM-style servers), tool_choice.
Providers disagree about them. So no provider is named here: when a request
comes back 400 naming one of OUR optional fields, that field is dropped (or
renamed), the request is sent again, and the model's record keeps it so later
calls are right the first time. Fields a caller asked for on purpose (extra=,
e.g. response_format / tool_choice checks) are never touched — a check must see
the real answer.
"""
from __future__ import annotations

OPTIONAL = ("temperature", "top_p", "stream_options", "chat_template_kwargs",
            "tool_choice", "parallel_tool_calls", "max_tokens", "reasoning_effort",
            # the maker's settings (sampling.py): left out where refused
            "top_k", "min_p", "repetition_penalty", "presence_penalty",
            "frequency_penalty")
_REFUSED = ("unsupported", "not supported", "does not support", "unknown",
            "unrecognized", "unrecognised", "not allowed", "not permitted",
            "extra inputs", "extra fields", "invalid", "only the default",
            "not available", "is not valid", "no such")


def _rec(key: str) -> dict:
    from .model_facts import load
    return (load().get(key) or {}).get("wire") or {}


def apply(body: dict, key: str, keep: set | frozenset = frozenset()) -> dict:
    """Shape body for the model from what it refused before."""
    w = _rec(key)
    for f in w.get("drop") or []:
        if f not in keep:
            body.pop(f, None)
    if w.get("token_field") == "max_completion_tokens" and "max_tokens" in body \
            and "max_tokens" not in keep:
        body["max_completion_tokens"] = body.pop("max_tokens")
    return body


def heal(body: dict, detail: str, key: str,
         keep: set | frozenset = frozenset()) -> str | None:
    """A 400 came back with this text. If it names one of Aura's own
    optional fields, fix body in place, remember it, and say what changed.
    None = nothing of ours to fix (the error is real)."""
    d = (detail or "").lower()
    w = dict(_rec(key))
    note = None
    if "max_completion_tokens" in d and "max_tokens" in body and "max_tokens" not in keep:
        body["max_completion_tokens"] = body.pop("max_tokens")
        w["token_field"] = "max_completion_tokens"
        note = "max_tokens → max_completion_tokens"
    else:
        for f in OPTIONAL:
            if f in body and f not in keep and f in d and any(r in d for r in _REFUSED):
                body.pop(f)
                w["drop"] = sorted(set(w.get("drop") or []) | {f})
                note = f"{f} left out"
                break
    if note:
        try:
            from .model_facts import save_facts
            save_facts(key, {"wire": w})
        except Exception:                            # noqa: BLE001
            pass
    return note

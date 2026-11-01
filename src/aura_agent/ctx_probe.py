"""ctx_probe.py —.1: true context length via fallback chain.

Chain (best source first, cached, source-shown): 1. user override —
AI_CONTEXT_LENGTH / profile context_length field WINS 2. saved — a size the
server reported, or one found by testing 3. live probe — ask the SERVER (never
the model; servers know, weights hallucinate): Ollama api/show, vLLM v1/models
max_model_len, LM Studio api/v0/models, llama.cpp props 4. default — 131072
(128k), shown as GUESSED When the server doesn't say, setup offers three
choices: type the size, use 128k, or find it by testing (find_by_testing: send
bigger and bigger prompts until the server refuses or stops answering). No
guessing from the model's name any more.

Cache: ctx_cache.json keyed by base_url+model. A size the server reported is
kept until the user changes it (/model ctx <n>) — no expiry. urllib only — zero
deps, 1 GET, hard timeout, never raises.
"""
from __future__ import annotations

import json
import os
import time

DEFAULT_CTX = 131072       # 128k when nothing better is known
PROBE_TIMEOUT = 3.0

def _cache_path() -> "os.PathLike[str] | str":
    from .paths import aura_home
    return str(aura_home() / "ctx_cache.json")


def _cache_get(key: str) -> tuple[int, str] | None:
    """(ctx, source): "measured" when found by testing, else "cache"."""
    try:
        data = json.loads(open(_cache_path(), encoding="utf-8").read())
        hit = data.get(key)
        if hit:
            return (int(hit["ctx"]),
                    "measured" if hit.get("how") == "measured" else "cache")
    except Exception:
        pass
    return None


def _cache_put(key: str, ctx: int, how: str = "probed") -> None:
    try:
        path = _cache_path()
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            data = {}
        data[key] = {"ctx": ctx, "ts": time.time(), "how": how}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(json.dumps(data))
    except Exception:
        pass


def _get_json(url: str, api_key: str | None = None) -> dict | None:
    """One GET, hard timeout, never raises. Honors AI_API_KEY when set
    (auth-gated proxies require a bearer even for /models)."""
    try:
        import urllib.request
        headers = {"Accept": "application/json"}
        key = api_key or os.environ.get("AI_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _probe_ollama_post(base_url: str, model: str) -> int | None:
    """POST /api/show {"name": model} → model_info["<fam>.context_length"]."""
    try:
        import urllib.request
        root = base_url.split("/v1")[0] if "/v1" in base_url else base_url.rstrip("/")
        req = urllib.request.Request(
            f"{root}/api/show",
            data=json.dumps({"name": model}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        mi = data.get("model_info") or {}
        for k, v in mi.items():
            if k.endswith("context_length"):
                return int(v)
    except Exception:
        pass
    return None


def running_window(base_url: str, model: str) -> int | None:
    """The window the server ACTUALLY runs this model with, when it says. Only a
    LOADED model has one.
    """
    from .first_run import _is_local
    if not _is_local(base_url):                            # Cloud APIs: not theirs
        return None
    root = base_url.rstrip("/").removesuffix("/v1")
    ps = _get_json(f"{root}/api/ps")                       # Ollama
    if isinstance(ps, dict):
        for m in ps.get("models") or []:
            if model in (m.get("name"), m.get("model")) and m.get("context_length"):
                return int(m["context_length"])
    lm = _get_json(f"{root}/api/v0/models")                # LM Studio
    if isinstance(lm, dict):
        for e in lm.get("data") or []:
            if e.get("id") == model and e.get("loaded_context_length"):
                return int(e["loaded_context_length"])
    return None


def _probe_openai_models(base_url: str, model: str) -> int | None:
    """vLLM-style /v1/models entries may carry max_model_len."""
    data = _get_json(base_url.rstrip("/") + "/models")
    if not isinstance(data, dict):
        return None
    for entry in data.get("data") or []:
        if entry.get("id") == model and entry.get("max_model_len"):
            return int(entry["max_model_len"])
    return None


def _probe_lmstudio(base_url: str, model: str) -> int | None:
    data = _get_json(base_url.rstrip("/").removesuffix("/v1")
                     + "/api/v0/models")
    if not isinstance(data, dict):
        return None
    for entry in data.get("data") or []:
        if entry.get("id") == model:
            for field in ("max_context_length", "context_length", "ctx"):
                if entry.get(field):
                    return int(entry[field])
    return None


def _probe_llamacpp(base_url: str, model: str) -> int | None:
    root = base_url.rstrip("/").removesuffix("/v1")
    data = _get_json(f"{root}/props")
    if isinstance(data, dict):
        gen = (data.get("default_generation_settings") or {})
        n_ctx = (gen.get("n_ctx") or data.get("n_ctx"))
        if n_ctx := (gen.get("n_ctx") or data.get("n_ctx")):
            return int(n_ctx)
    return None


def is_guess(src: str | None) -> bool:
    """True when the size was not reported by the server or set by the user
    (the fixed default)."""
    return src == "default"


def describe_source(src: str | None) -> str:
    """Plain words for where a context size came from."""
    src = src or "?"
    if src == "override":
        return "set by you"
    if src == "cache":
        return "reported by the server earlier"
    if src == "measured":
        return "found by testing"
    if src == "probed:running":
        return "what the server runs the model with now"
    if src.endswith("-max"):
        return ("the model's maximum — the server may run it with less (Ollama: 4,096 "
                "unless OLLAMA_CONTEXT_LENGTH is set); checked again once it's loaded")
    if src.startswith("probed"):
        return "reported by the server"
    if src == "default":
        return ("GUESSED (default) — set it: /model ctx <n>, or find it: "
                "/model ctx find")
    return src


def probe_context_length(base_url: str, model: str,
                         configured: int | None = None
                         ) -> tuple[int, str]:
    """Returns (ctx, source) where source ∈ {override, cache, measured,
    probed:<kind>, default}. Never raises. configured (explicit user value)
    wins."""
    key = f"{base_url}|{model}"
    if configured:
        return int(configured), "override"
    # the running window wins over any maximum (a loaded Ollama / LM Studio
    # model); it is not cached — it changes when the model is reloaded
    run = running_window(base_url, model)
    if run and run > 256:
        return run, "probed:running"
    cached = _cache_get(key)
    if cached:
        return cached
    # probes, best-first
    for kind, fn in (("ollama", _probe_ollama_post),
                     ("models-endpoint", _probe_openai_models),
                     ("lmstudio", _probe_lmstudio),
                     ("llamacpp", _probe_llamacpp)):
        ctx = fn(base_url, model)
        if ctx and ctx > 256:
            if kind in ("ollama", "lmstudio"):
                # the model's MAXIMUM, not what the server runs it with —
                # not kept as fact; the running window is read once loaded
                return ctx, f"probed:{kind}-max"
            _cache_put(key, ctx)
            return ctx, f"probed:{kind}"
    return DEFAULT_CTX, "default"


def server_reports(base_url: str, model: str) -> int | None:
    """Only what the server itself says (no default): for setup's question."""
    ctx, src = probe_context_length(base_url, model)
    return ctx if src != "default" else None


def remember_measured(base_url: str, model: str, ctx: int) -> None:
    _cache_put(f"{base_url}|{model}", int(ctx), how="measured")


def find_by_testing(base_url: str, model: str, api_key: str | None = None,
                    say=print, timeout: float = 180.0,
                    ceiling: int = 2_097_152, memory_check=None) -> int | None:
    """Find the real window by trying: send prompts of growing size until the
    server refuses ("too long") or stops answering, then narrow down. Generic —
    any OpenAI-compatible server. Returns tokens, or None when not even a small
    prompt works. Each try asks for a 1-token reply (this probe only) so it
    costs prompt tokens, not answers.

    Memory is watched between tries: on a local server, before each bigger
    prompt the free memory is checked again — low → stop at the biggest size
    that worked and say so, instead of pushing the machine into a freeze.
    """
    if memory_check is None:
        from .machine import heavy_job_risk

        def memory_check():
            return heavy_job_risk(base_url)
    import urllib.error
    import urllib.request
    from .llm import context_limit_in
    url = base_url.rstrip("/") + "/chat/completions"
    key = api_key or os.environ.get("AI_API_KEY")
    per_word = [1.0]                    # Tokens per filler word, learned

    def attempt(n_tokens: int):
        """'ok' | int (the size the server names) | 'too_big'."""
        words = max(1, int(n_tokens / per_word[0]))
        body = json.dumps({"model": model, "max_tokens": 1, "stream": False,
                           "messages": [{"role": "user", "content":
                                         "Reply OK. " + "word " * words}]}
                          ).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = e.read().decode("utf-8", "replace")
            except Exception:
                msg = ""
            named = context_limit_in(msg)
            return named if named else "too_big"
        except Exception:                 # No answer = too big for it
            return "too_big"
        used = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        if used:
            per_word[0] = max(0.2, used / words)
            if used < n_tokens * 0.8:     # The server cut the prompt short
                return used
        return "ok"

    say("  finding the context size by testing (bigger and bigger prompts)…")
    lo, hi, n = 0, None, 4096
    while n <= ceiling:
        why = memory_check() if lo else None
        if why:
            say(f"    stopped at {lo:,} tokens to protect this machine: {why}")
            return lo
        say(f"    trying {n:,} tokens…")
        r = attempt(n)
        if isinstance(r, int):
            say(f"    the server says its limit is {r:,}")
            return r
        if r == "ok":
            lo, n = n, n * 2
        else:
            hi = n
            break
    if lo == 0:
        return None
    if hi is None:                        # Everything fit up to the ceiling
        return lo
    for _ in range(6):                    # Narrow down to a few percent
        if hi - lo <= max(1024, lo // 20):
            break
        mid = (lo + hi) // 2
        why = memory_check()
        if why:
            say(f"    stopped at {lo:,} tokens to protect this machine: {why}")
            break
        say(f"    trying {mid:,} tokens…")
        r = attempt(mid)
        if isinstance(r, int):
            return r
        if r == "ok":
            lo = mid
        else:
            hi = mid
    return lo // 1024 * 1024 or lo

"""sampling.py — the model maker's own settings (temperature, top_p …) instead of
Aura's one-size 0.4.

Its OpenAI-style endpoint takes temperature and top_p; what isn't sent keeps
the model's own value. - llama.cpp: the GGUF file's general.sampling.* keys
(temp, top_p, top_k, min_p, penalty_repeat) — read from the file when props
names one Aura can open. - OpenRouter-style model lists: each entry's
"default_parameters" and "hugging_face_id". - Hugging Face: the model's
generation_config.json. Nothing found: Aura's own settings stay. The choice is
per model and the user's (setup asks; `aura model settings maker|aura`);
nothing changes until they say so. Standard library only.
"""
from __future__ import annotations

import json
import struct
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

KEYS = ("temperature", "top_p", "top_k", "min_p", "repetition_penalty",
        "presence_penalty", "frequency_penalty")
_OLLAMA = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k",
           "min_p": "min_p", "repeat_penalty": "repetition_penalty",
           "presence_penalty": "presence_penalty", "frequency_penalty": "frequency_penalty"}
_GGUF = {"general.sampling.temp": "temperature", "general.sampling.top_p": "top_p",
         "general.sampling.top_k": "top_k", "general.sampling.min_p": "min_p",
         "general.sampling.penalty_repeat": "repetition_penalty"}


def _root(base_url: str) -> str:
    u = (base_url or "").rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def _get(url: str, data: dict | None = None, headers: dict | None = None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _clean(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if k in KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v) if k == "top_k" else round(float(v), 4)
    return out


def from_ollama_parameters(text: str) -> dict:
    vals = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in _OLLAMA:
            try:
                vals[_OLLAMA[parts[0]]] = float(parts[1])
            except ValueError:
                pass
    return _clean(vals)


def gguf_sampling(path: Path) -> dict:
    """The general.sampling.* keys of a GGUF file (header only is read)."""
    vals: dict = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return {}
        version = struct.unpack("<I", f.read(4))[0]
        if version < 2:
            return {}
        _tensors, kvs = struct.unpack("<QQ", f.read(16))
        sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
        fmt = {4: "<I", 5: "<i", 6: "<f", 10: "<Q", 11: "<q", 12: "<d", 0: "<B", 1: "<b",
               2: "<H", 3: "<h", 7: "<?"}

        def string() -> str:
            n = struct.unpack("<Q", f.read(8))[0]
            return f.read(n).decode("utf-8", "replace")

        def skip(t: int):
            if t == 8:
                f.seek(struct.unpack("<Q", f.read(8))[0], 1)
            elif t == 9:
                it, n = struct.unpack("<IQ", f.read(12))
                if it in sizes:
                    f.seek(sizes[it] * n, 1)
                else:
                    for _ in range(n):
                        skip(it)
            else:
                f.seek(sizes[t], 1)

        for _ in range(kvs):
            key = string()
            t = struct.unpack("<I", f.read(4))[0]
            if key in _GGUF and t in fmt:
                vals[_GGUF[key]] = struct.unpack(fmt[t], f.read(sizes[t]))[0]
            else:
                skip(t)
            if len(vals) == len(_GGUF):
                break
    return _clean(vals)


def _hf(repo: str) -> dict:
    d = _get(f"https://huggingface.co/{repo}/resolve/main/generation_config.json")
    return _clean(d if isinstance(d, dict) else {})


def find(base_url: str, model: str, api_key: str | None = None) -> tuple[dict, str]:
    """(the maker's settings, where they came from) — ({}, why not)."""
    root = _root(base_url)
    auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    tried = []
    try:                                                    # Ollama
        d = _get(root + "/api/show", {"model": model}, auth)
        vals = from_ollama_parameters(d.get("parameters") or "")
        if vals:
            return vals, "the model's own settings (Ollama model info)"
        tried.append("Ollama model info: none set")
    except Exception:                                       # noqa: BLE001
        pass
    try:                                                    # llama.cpp
        d = _get(root + "/props", headers=auth)
        mp = d.get("model_path")
        if mp and Path(mp).is_file():
            vals = gguf_sampling(Path(mp))
            if vals:
                return vals, "the model file's own settings (GGUF general.sampling)"
            tried.append("the model file sets none")
    except Exception:                                       # noqa: BLE001
        pass
    hf = None
    try:                                                    # OpenRouter-style list
        d = _get(base_url.rstrip("/") + "/models", headers=auth)
        for m in d.get("data") or []:
            if isinstance(m, dict) and m.get("id") == model:
                vals = _clean(m.get("default_parameters") or {})
                if vals:
                    return vals, "the publisher's default parameters (the server's model list)"
                hf = m.get("hugging_face_id") or None
    except Exception:                                       # noqa: BLE001
        pass
    host = (urlparse(base_url).hostname or "").lower()
    if not hf and "/" in model and host not in ("localhost", "127.0.0.1", "::1"):
        hf = model.split(":")[0]
    if hf:
        try:
            vals = _hf(hf)
            if vals:
                return vals, f"the model's generation_config.json on Hugging Face ({hf})"
        except Exception:                                   # noqa: BLE001
            tried.append(f"Hugging Face {hf}: not found")
    return {}, "; ".join(tried) or "the server and the model don't publish any"


# ------------------------------------------------------- the user's choice --
def choice(key: str) -> dict:
    try:
        from .model_facts import load
        rec = (load().get(key) or {}).get("sampling")
        return rec if isinstance(rec, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def save(key: str, use: str, values: dict, source: str) -> None:
    from .model_facts import save_facts
    save_facts(key, {"sampling": {"choice": use, "values": _clean(values), "source": source}})


def in_use(key: str) -> dict:
    """The maker's settings when the user chose them for this model, else {}."""
    c = choice(key)
    return dict(c.get("values") or {}) if c.get("choice") == "maker" else {}


def describe(values: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in values.items()) or "none"

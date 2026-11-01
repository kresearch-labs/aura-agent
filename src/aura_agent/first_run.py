"""first_run.py — N: inference-first onboarding, REWRITTEN.

User corrections: 1. NO endpoint scanning. Default lane is API-key-first; local
probing only when the user explicitly picks the local lane. 2. NO
profile-naming question. More profiles come later via profile in chat. 3. Keys
are MASKED AS TYPED (getpass showed nothing on conhost). 4. The model is ALWAYS
chosen by the user from the live list — no auto-pick ("endpoint lists 6 models
— using X" is gone). The interactive menu itself lives in
setup_quick.choose_endpoint (shared with `aura setup`, so there is one menu and
one model picker).

Public:
  ensure_working_profile(eng, interactive=True) -> ProviderProfile | None
  live_completion_test(profile) -> (ok, detail)
  find_working_profile(interactive, verbose) # kept for aura doctor
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .config import ProviderProfile, resolve
from .profile_store import active_name, get_profile, set_active, set_profile

DEFAULT_PROFILE = "default"


# --------------------------------------------------------------------------
# The live-completion gate — models is not proof
# --------------------------------------------------------------------------

def _is_local(base_url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(base_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") \
        or host.endswith(".local") or host.startswith(("192.168.", "10."))


def _why(code: int, text: str) -> str:
    """The provider's answer in plain words (install audit: a 404 is as
    often a dead model name as a wrong address; a 429 is not always money)."""
    t = text.lower()
    if code == 401:
        return "the key was refused — check it, or make a new one"
    if code == 403:
        return "the key works but isn't allowed this model — check the plan or the model's access"
    if code == 402 or any(w in t for w in ("insufficient_quota", "billing", "credit", "payment")):
        return "the account has no credit or billing set up for this"
    if code == 404 and "model" in t:
        return "this model name isn't available there (it may be retired) — pick it from the list"
    if code == 404:
        return ("nothing at that address — check the base URL "
                "(Aura adds /chat/completions to it)")
    if code == 429:
        return "rate limited — wait a moment and try again"
    if code in (500, 502, 503, 529):
        return "the provider is having trouble right now — try again shortly"
    short = " ".join(text.split())[:160]
    return f"HTTP {code}: {short}" if short else f"HTTP {code}"


def live_completion_test(profile: ProviderProfile,
                         timeout: float | None = None) -> tuple[bool, str]:
    """Send a real tiny completion. Returns (ok, plain-language reason).
    A local server may need a while to load the model the first time, so
    it gets longer; a field the model refuses is fixed the way the chat
    client fixes it (wire_fit) and remembered."""
    from . import wire_fit
    from .model_facts import model_key
    local = _is_local(profile.base_url)
    if timeout is None:
        timeout = 180.0 if local else 45.0
    from .wire_native import api_for
    if api_for(profile.base_url) != "chat":
        # a native API (wire_native.py): test the path Aura will really use
        from .llm import LLMClient
        try:
            res = LLMClient(profile).chat(
                [{"role": "user", "content": "Reply with the word: OK"}], max_retries=1)
        except Exception as e:                          # noqa: BLE001
            return False, " ".join(str(e).split())[:200]
        if res.content or res.reasoning:
            return True, "replied"
        return False, "the server answered, but with no reply in it"
    key = model_key(profile.base_url, profile.model)
    body = wire_fit.apply({
        "model": profile.model,
        "messages": [{"role": "user", "content": "Reply with the word: OK"}],
        "max_tokens": 16,
        "stream": False,
    }, key)
    for _ in range(3):
        req = urllib.request.Request(
            profile.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {profile.api_key}"}
                        if profile.api_key else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read().decode("utf-8", errors="replace")
                if "text/event-stream" in ctype:
                    for line in raw.splitlines():
                        if line.startswith("data:") and line[5:].strip():
                            return True, "replied"
                    return False, "the server streamed nothing back"
                body_json = json.loads(raw)
                if body_json.get("choices"):
                    return True, "replied"
                return False, "the server answered, but with no reply in it"
        except urllib.error.HTTPError as e:
            try:
                text = (e.read() or b"").decode("utf-8", errors="replace")
            except Exception:                           # noqa: BLE001
                text = ""
            if e.code in (400, 422) and wire_fit.heal(body, text, key):
                continue                                # A field it refused — once more
            return False, _why(e.code, text)
        except Exception as e:                          # noqa: BLE001
            msg = str(e)
            if "timed out" in msg:
                return False, (f"no answer in {int(timeout)} s"
                               + (" — a local model may still be loading; try again"
                                  if local else ""))
            if "refused" in msg or "Errno" in msg or "getaddrinfo" in msg:
                return False, "nothing answered (the server is off, or the address/port is wrong)"
            return False, msg[:120]
    return False, "the server kept refusing the request's fields"


def was_verified(name: str, prof: ProviderProfile) -> bool:
    """This setup answered a real message before, with this same address
    and model."""
    try:
        v = (get_profile(name) or {}).get("verified") or {}
        return v.get("model") == prof.model and v.get("base_url") == prof.base_url
    except Exception:                                   # noqa: BLE001
        return False


def mark_verified(name: str, prof: ProviderProfile) -> None:
    import time
    try:
        if get_profile(name):
            set_profile(name, activate=False, verified={
                "model": prof.model, "base_url": prof.base_url,
                "at": round(time.time())})
    except Exception:                                   # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# The gate (N)
# --------------------------------------------------------------------------

def ensure_working_profile(eng, interactive: bool = True) -> ProviderProfile | None:
    """Call at chat/run startup. Returns a working profile, or None when
    the user skipped / non-interactive found nothing."""
    # Install audit: a setup that answered once is trusted — testing on every
    # start cost money on paid APIs, added a wait, and a slow local model that
    # timed out dropped a working user into setup. A real failure still shows
    # on the first call, in plain words.
    active = active_name()
    prof = resolve(active) if active else None
    if prof is not None:
        if was_verified(active, prof):
            return prof
        ok, why = live_completion_test(prof)
        if ok:
            mark_verified(active, prof)
            return prof
    else:
        why = "no model setup is chosen"

    if not interactive:
        # Never switch to another saved setup on our own — say what failed and
        # how to choose
        print(f"  x model setup '{active}' didn't answer: {why}")
        try:
            from .profile_store import list_profiles
            others = [n for n in list_profiles() if n != active]
        except Exception:                               # noqa: BLE001
            others = []
        if others:
            print("    saved setups: " + ", ".join(others[:8]))
            print("    choose one: aura model use <name>   (or aura setup)")
        else:
            print("    set one up: aura setup   (or set AI_BASE_URL / AI_MODEL / AI_API_KEY)")
        return None

    # the same intent-first menu as `aura setup` (one menu, one picker)
    print()
    print("Aura needs one AI endpoint (any OpenAI-compatible API).")
    from .setup_quick import choose_endpoint
    return choose_endpoint()


# --------------------------------------------------------------------------
# kept for `aura doctor` / legacy callers: the silent ladder
# --------------------------------------------------------------------------

def find_working_profile(interactive: bool = True,
                         verbose: bool = True
                         ) -> tuple[ProviderProfile | None, str]:
    """Walk saved/env candidates; return (working_profile, name). A candidate
    passes only after a live completion. N: reads ALL saved profiles (not a
    fixed name list) — the ladder must find a working profile whatever it's
    called.
    """
    seen: set[str] = set()
    out: list[tuple[str, ProviderProfile]] = []

    def add(name: str):
        if name in seen:
            return
        cfg = get_profile(name)
        if cfg:
            seen.add(name)
            try:
                out.append((name, resolve(name)))
            except Exception:
                pass

    if os.environ.get("AI_PROVIDER"):
        add(os.environ["AI_PROVIDER"])
    add(active_name())
    add(DEFAULT_PROFILE)
    # Every saved profile — the ladder is name-blind (N)
    try:
        from .profile_store import _load_file
        for name in _load_file().get("profiles", {}):
            add(name)
    except Exception:
        pass
    for n in ("ollama", "lm-studio", "vllm", "llamacpp"):
        add(n)
    for name, prof in out:
        ok, detail = live_completion_test(prof)
        if ok:
            if verbose:
                print(f"  ✓ {name}: {detail} ({prof.model} @ "
                      f"{prof.base_url})")
            return prof, name
        if verbose:
            print(f"  • {name}: {detail}")
    return None, ""

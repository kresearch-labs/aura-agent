"""setup.py — Aura guided setup wizard.

  quick — scan local endpoints, auto-pick the first live one, zero questions
  full — every option: scan, then interactive provider/model picker
  minimal — no network scans; explicit provider + URL + model prompts

Wizard contract (from the study):
  - every screen shows what will happen next
  - every choice shows WHY/WHEN to pick it (descriptions, not bare names)
  - config auto-backup before mutation + printed restore command
  - ends in a VERIFIED successful action (live model fetch), never a promise
  - Tool Availability Summary at the end (X/Y ready, missing keys named)
"""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from datetime import datetime

from .profile_store import (_profiles_file, list_profiles, set_active,
                            set_profile)

# scan targets: (name, base_url, hint)
SCAN_TARGETS = [
    ("ollama",    "http://localhost:11434/v1", "Ollama — free, local, no key"),
    ("lm-studio", "http://localhost:1234/v1",  "LM Studio — free, local"),
    ("llamacpp",  "http://localhost:8080/v1",  "llama.cpp server — free, local"),
    ("vllm",      "http://localhost:8000/v1",  "vLLM — free, local, long ctx"),
]

TIMEOUT = 3.0


def restore_hint(bak: str) -> str:
    """The copy-back command for this machine's shell."""
    import os
    if os.name == "nt":
        return f'copy "{bak}" "{_profiles_file()}"   (PowerShell: Copy-Item)'
    return f'cp "{bak}" "{_profiles_file()}"'


def _backup_profiles() -> str | None:
    """Auto-backup before wizard mutation; returns the backup path."""
    pf = _profiles_file()
    if not pf.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = pf.with_name(f"profiles.json.pre-setup.{stamp}")
    shutil.copy2(pf, bak)
    return str(bak)


# words in a model id that mean it isn't a chat model — shown last, not hidden
_NOT_CHAT = ("embed", "tts", "whisper", "dall-e", "moderation", "transcribe",
             "rerank", "image-", "-image", "audio", "speech")


def _order(entries: list[dict]) -> list[str]:
    """Newest first when the server dates them; models that look like they
    aren't for chat (embeddings, speech, images) go last."""
    def when(m):
        c = m.get("created") or m.get("created_at") or 0
        if isinstance(c, str):                     # Anthropic: an ISO date
            c = c[:10].replace("-", "")
        try:
            return float(c)
        except (TypeError, ValueError):
            return 0.0
    ents = sorted(entries, key=when, reverse=True)
    ids = [str(m.get("id", "?")) for m in ents]
    chat = [i for i in ids if not any(w in i.lower() for w in _NOT_CHAT)]
    return chat + [i for i in ids if i not in chat]


def _fetch_models(base_url: str, api_key: str | None = None,
                  timeout: float | None = None) -> tuple[bool, list[str], str, int]:
    """GET {base}/models -> (ok, model_ids, error, http_code). Never raises.
    http_code 401/403 => endpoint EXISTS but is auth-walled (reportable).
    A listed provider that wants its key another way (Anthropic: x-api-key)
    gets it that way too."""
    from .providers import CLOUD_PROVIDERS, provider_for_url
    spec = CLOUD_PROVIDERS.get(provider_for_url(base_url) or "", {})
    if timeout is None:
        timeout = 15.0 if spec else TIMEOUT
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if api_key:
        headers.update({k: v.replace("{key}", api_key)
                        for k, v in (spec.get("list_headers") or {}).items()})
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models" + (spec.get("list_query") or ""),
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r) or {}
            ents = [m for m in body.get("data", []) if isinstance(m, dict)]
            return (True, _order(ents), "", 200)
    except urllib.error.HTTPError as e:
        return (False, [], f"HTTP {e.code}", e.code)
    except Exception as e:
        return (False, [], str(e), 0)


def scan_endpoints(api_key: str | None = None) -> tuple[list[dict], list[dict]]:
    """Probe known local endpoints.
    Returns (live, auth_walled): live = usable now; auth_walled = endpoint
    answered 401/403 (exists, needs a key) — reported, not hidden."""
    live: list[dict] = []
    walled: list[dict] = []
    for name, url, hint in SCAN_TARGETS:
        ok, models, _, code = _fetch_models(url, api_key)
        if ok:
            live.append(dict(name=name, base_url=url, hint=hint,
                             models=models, live=True))
        elif code in (401, 403):
            walled.append(dict(name=name, base_url=url, hint=hint, code=code))
    saved_names = {t[0] for t in SCAN_TARGETS}
    for pname, cfg in list_profiles().items():
        if pname in saved_names:
            continue
        url = cfg.get("base_url", "")
        if "localhost" in url or "127.0.0.1" in url:
            ok, models, _, code = _fetch_models(url, cfg.get("api_key"))
            if ok:
                live.append(dict(name=pname, base_url=url, hint="saved profile",
                                 models=models, live=True))
            elif code in (401, 403):
                walled.append(dict(name=pname, base_url=url,
                                   hint="saved profile", code=code))
    return live, walled


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def _choose(title: str, options: list[tuple[str, str]], default_idx: int = 0) -> str:
    """Picker: arrow keys on a real terminal, numbers otherwise.
    options = [(label, description)]. Returns label."""
    from . import setup_ui as SU
    if SU.interactive():
        got = SU.pick_one(title.strip(), [SU.Opt(lab, lab[:1].upper() + lab[1:], d)
                                          for lab, d in options],
                          options[default_idx][0])
        return got if got is not None else options[default_idx][0]
    print(f"\n{title}")
    for i, (label, desc) in enumerate(options, 1):
        mark = "*" if i == default_idx + 1 else " "
        print(f"  {mark}{i}. {label}")
        if desc:
            print(f"     {desc}")
    while True:
        raw = _ask("Choose 1-{}".format(len(options)),
                   str(default_idx + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  ? enter a number from the list")


def _pick_model(base_url: str, api_key: str | None, fallback: str) -> str:
    """Live model list from the endpoint; manual fallback."""
    ok, models, err, code = _fetch_models(base_url, api_key)
    if ok and models:
        print(f"  endpoint reports {len(models)} model(s) — live list:")
        if len(models) <= 12:
            for i, m in enumerate(models, 1):
                print(f"    {i}. {m}")
            pick = _ask("Model number or exact id", "1")
            if pick.isdigit() and 1 <= int(pick) <= len(models):
                return models[int(pick) - 1]
            if pick in models:
                return pick
        else:
            # long list: show first 12, allow free-text
            for m in models[:12]:
                print(f"    - {m}")
            print(f"    ... and {len(models) - 12} more")
            pick = _ask("model id (exact)")
            if pick in models:
                return pick
        return fallback
    print(f"  (could not list models: {err})")
    return _ask("model id", fallback)


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def run_setup(mode: str = "quick") -> int:
    """Entry: aura setup [quick|full|minimal]. Returns exit code."""
    if mode == "quick":
        # remade quick: intent-first (cloud key / local / custom / skip),
        # live-completion gate — see setup_quick.py
        from .setup_quick import run_quick
        return run_quick()
    print()
    print("┌─────────────────────────────────────────────────────────┐")
    print("│              ☤ Aura Setup — {} mode".format(mode).ljust(58) + "│")
    print("└─────────────────────────────────────────────────────────┘")
    print()
    if mode == "full":
        print("Full mode: scan, then interactive provider + model pickers,")
        print("then a live verification. You confirm every step.")
    else:  # Minimal
        print("Minimal mode: no network scanning. You type the provider name,")
        print("URL and model. Use this on locked-down/air-gapped machines.")
    print()

    bak = _backup_profiles()
    if bak:
        print(f"  (auto-backup: {bak}")
        print(f"   restore: {restore_hint(bak)})")

    # ---- find or ask the endpoint ------------------------------------------
    chosen: dict | None = None
    if mode == "full":
        print("Scanning local endpoints...")
        found, walled = scan_endpoints()
        for w in walled:
            print(f"  • {w['name']} @ {w['base_url']} — needs an API key "
                  f"(HTTP {w['code']})")
        if not found:
            print("  ✗ no live local endpoints found")
            if walled:
                print("    (endpoints above exist but need keys — "
                      "re-run with the key set, or use minimal mode)")
            print("    Options: install Ollama (ollama.com), start LM Studio,")
            print("    or choose minimal mode and enter a remote endpoint.")
            mode = "minimal-handoff"
        else:
            opts = [(f"{f['name']} — {f['base_url']} ({len(f['models'])} models)",
                     f["hint"]) for f in found]
            pick = _choose("Select provider:", opts)
            chosen = next(f for f in found
                          if f["base_url"] in pick or f['name'] in pick)

    if chosen is None:
        from .onboard import profile_name_looks_like_key
        name = _ask("Profile name", "custom")
        while profile_name_looks_like_key(str(name)):
            # a key typed here would be saved — and shown — as a name
            print("  ✗ that looks like an API key, not a name — the key "
                  "is asked for next")
            name = _ask("Profile name", "custom")
        url = _ask("Base URL (OpenAI-compatible, incl. /v1)",
                   "http://localhost:11434/v1")
        from .setup_quick import _secret
        key = _secret("API key (blank for local)")       # Hidden as it's typed
        chosen = dict(name=str(name), base_url=str(url), models=[],
                      live=False, api_key=(key or None))

    # ---- model -------------------------------------------------------------
    c_url = str(chosen["base_url"])
    c_key_raw = chosen.get("api_key")
    c_key: str | None = str(c_key_raw) if c_key_raw else None
    raw_models = chosen.get("models")
    c_models: list = list(raw_models) if isinstance(raw_models, list) else []
    if mode == "full" or not c_models:
        model = _pick_model(c_url, c_key, c_models[0] if c_models else "local")
    else:
        model = c_models[0]
    print(f"\nModel: {model}")

    # ---- context length --------------------------------
    # Ask the server first. When it doesn't say, three choices: type it,
    # use 128k, or let Aura find it by testing. Whatever is chosen is kept
    # until changed (/model ctx <n>).
    from .ctx_probe import (DEFAULT_CTX, find_by_testing, remember_measured,
                            server_reports)
    ctx_len = None
    reported = server_reports(c_url, str(model))
    if reported:
        print(f"Context size: {reported:,} tokens (the server says so)")
    else:
        how = _choose(
            "The server doesn't say how much text this model can take in "
            "at once (its context size). What should Aura do?",
            [("type it", "you enter the number, e.g. 131072"),
             ("use 128k", f"the default ({DEFAULT_CTX:,} tokens), shown "
                          "as a guess"),
             ("find it by testing", "Aura sends bigger and bigger prompts "
                                    "until the server refuses — a few "
                                    "minutes; on a paid API it costs "
                                    "prompt tokens")], default_idx=1)
        if how == "type it":
            raw = _ask("Context size (tokens)", str(DEFAULT_CTX)).strip()
            try:
                ctx_len = int(raw.replace(",", "").replace("_", ""))
            except ValueError:
                print(f"  (not a number — using {DEFAULT_CTX:,})")
        elif how == "find it by testing":
            from .machine import ok_to_run
            go = ok_to_run("finding the context size", c_url)
            found = find_by_testing(c_url, str(model), c_key) if go else None
            if found:
                remember_measured(c_url, str(model), found)
                print(f"Context size: {found:,} tokens (found by testing)")
            elif not go:
                print(f"  not tested — using {DEFAULT_CTX:,} for now (aura setup again, "
                      "or /model ctx <n>)")
            else:
                print("  couldn't find it (even a small prompt failed) — "
                      f"using {DEFAULT_CTX:,} for now")

    # ---- VERIFIED first: a real message answered (a model list is not
    # proof — the model may be retired or the key not allowed it); only a
    # setup that answered is saved, as in quick setup -------------------------
    from .config import ProviderProfile
    from .first_run import live_completion_test, mark_verified
    c_name: str = chosen["name"]
    cand = ProviderProfile(name=c_name, base_url=c_url, model=str(model), api_key=c_key)
    print(f"\nlive-testing {model} @ {c_url} ...")
    ok, detail = live_completion_test(cand)
    if not ok:
        print(f"  ✗ the model didn't answer: {detail}")
        print("    nothing was saved — fix it, then run again: aura setup")
        return 1
    print(f"  ✓ VERIFIED: {model} answered a real message")

    # ---- save + activate ---------------------------------------------------
    fields = dict(base_url=c_url, model=model, context_length=ctx_len)
    if c_key:
        fields["api_key"] = c_key
    set_profile(c_name, **fields)
    set_active(c_name)
    mark_verified(c_name, cand)
    print(f"Profile saved: {c_name} -> {c_url}")
    print(f"Active profile: {c_name}")

    # ---- 3.2: per-capability provider pickers + availability summary -------
    # (bug 1 fix: ONE availability summary — the legacy static block is gone;
    #  bug 2/4 fix: the llm capability row is preselected/skipped in favour
    #  of the provider the user just verified in the scan step)
    try:
        from .setup_pickers import (pick_capabilities, render_summary,
                                    save_capabilities)
        if mode == "full":
            chosen_caps = pick_capabilities(
                assume_yes=False, prefer_llm=c_name)
            save_capabilities(chosen_caps)
            print("\nCapability choices saved (profiles.json "
                  "'capabilities').")
            print()
            print(render_summary())
        else:
            # minimal mode: capabilities follow the profile just saved
            from .setup_pickers import load_capabilities, save_capabilities
            caps = load_capabilities()
            caps["llm"] = c_name if c_name in {t[0] for t in
                                               SCAN_TARGETS} else "custom"
            save_capabilities(caps)
    except Exception:
        pass  # Summary is best-effort; setup already succeeded

    # ---- closing summary (single block) --------------------------------------
    print()
    print("Next steps:")
    print("  aura chat                  — start working (in your project folder)")
    print('  aura run "your task"       — one task, start to finish')
    print("  aura doctor                — health check")
    return 0

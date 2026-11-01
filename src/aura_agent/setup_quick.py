"""setup_quick.py — the REMADE quick setup: intent first, one key, verified.

Replaces the scan-first quick mode after the operator's criticism

New quick flow:
  1. INTENT QUESTION: cloud provider / local model / custom / skip
  2. Lane cloud: paste key -> Aura identifies the provider from the
                    key's documented prefix -> canonical base URL filled
                    in -> model (starter or from live list) -> LIVE
                    completion gate -> save only on pass
  3. Lane local: port scan runs HERE (after the user says local);
                    nothing found -> exact Ollama install commands + rescan
  4. Lane custom: URL + key -> live gate
  Skip: exit, nothing saved

The same menu is the first-run gate (first_run.ensure_working_profile): one
intent question, one model picker, for `aura setup` and for the first `aura
chat` / `aura run`. No scanning outside the local lane, and the model is always
chosen from the live list.
"""
from __future__ import annotations

import getpass

from .config import ProviderProfile
from .first_run import live_completion_test
from .profile_store import _profiles_file, set_active, set_profile
from .providers import CLOUD_PROVIDERS, detect_provider_from_key
from .setup import _backup_profiles, _fetch_models, scan_endpoints


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def _secret(prompt: str) -> str:
    """Masked entry: '*' per char so the user SEES typing is happening
    (silent getpass reads as 'nothing works' — idiot-proof law: the user
    must get visible feedback for every keystroke). Delegates to the
    onboard masked reader (per-char '*', backspace works, paste works)."""
    try:
        from .onboard import ask_secret
        return ask_secret(f"{prompt} (input hidden, shows *): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        # no console (CI, pipes, test harness) — silent fallback is fine
        try:
            return getpass.getpass(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


def _choose_model(models: list[str], where: str,
                  starter: str = "") -> str | None:
    """The model is chosen by the user from the live list; free text only
    when the endpoint can't list its models. None = nothing chosen."""
    if len(models) == 1:
        print(f"  • {where} has one model: {models[0]}")
        return models[0]
    if models:
        print(f"  • models on {where} ({len(models)}, newest first):")
        shown = models
        for _ in range(6):                 # Narrow down by typing words
            page = shown[:12]
            for i, m in enumerate(page, 1):
                print(f"    {i:>2}. {m}")
            if len(shown) > 12:
                print(f"    ... and {len(shown) - 12} more — type a word to narrow the list")
            pick = _ask(f"model 1-{len(page)}, exact id, or a word to search", "1")
            if pick.isdigit() and 1 <= int(pick) <= len(page):
                return page[int(pick) - 1]
            if pick in models:
                return pick
            words = pick.lower().split()
            found = [m for m in models if all(w in m.lower() for w in words)]
            if not found:
                print(f"  ✗ nothing matches '{pick}'")
                continue
            shown = found
            print(f"  • {len(found)} match '{pick}':")
        print("  ✗ no model chosen — nothing saved")
        return None
    model = _ask("model name (press ENTER for the provider default)"
                 if starter else "model name (as your provider calls it)",
                 starter)
    if not model:
        print("  ✗ need a model name")
    return model or None


# --------------------------------------------------------------------------
# lanes — each returns (name, url, key, model) or None
# --------------------------------------------------------------------------

def _lane_cloud() -> tuple[str, str, str | None, str] | None:
    print()
    key = _secret("Paste your API key")
    if not key:
        print("  ✗ no key entered")
        return None
    from .providers import unusable_key
    bad = unusable_key(key)
    if bad:
        print(f"  ✗ {bad}")
        return None
    provider = detect_provider_from_key(key)
    if provider:
        print(f"  • the key looks like {CLOUD_PROVIDERS[provider]['label']}")
    else:
        keys = list(CLOUD_PROVIDERS)
        print("  • which provider is this key from?")
        for i, k in enumerate(keys, 1):
            print(f"    {i:>2}. {CLOUD_PROVIDERS[k]['label']}")
        raw = _ask("choose number")
        if not (raw.isdigit() and 1 <= int(raw) <= len(keys)):
            return None
        provider = keys[int(raw) - 1]
    p = CLOUD_PROVIDERS[provider]
    if p.get("note"):
        print(f"  • {p['label']}: {p['note']}")
    url = p.get("base_url") or _ask("base URL (incl. /v1)")
    if not url:
        print("  ✗ URL required for this provider")
        return None
    # The model always comes from the provider's own list
    ok, models, err, code = _fetch_models(url, key)
    if not ok:
        from .first_run import _why
        print(f"  (couldn't list the models: {_why(code, err) if code else err})")
        if code == 401:
            return None
    model = _choose_model(models if ok else [], p["label"])
    return (provider, url, key or None, model) if model else None


def _lane_local() -> tuple[str, str, str | None, str] | None:
    print()
    # What this computer can run — said first, so a model that fits is picked,
    # not one that crawls or won't start
    from . import local_fit as LF
    try:
        hw = LF.hardware()
        print(f"This computer: {LF.describe(hw)}")
        print(f"  what fits: {LF.size_classes(hw)}")
    except Exception:                                   # noqa: BLE001
        hw = None
    print("Scanning local endpoints (11434/1234/8080/8000)...")
    found, walled = scan_endpoints()
    for w in walled:
        print(f"  • {w['name']} @ {w['base_url']} — needs an API key")
    if not found:
        # ollama.com/download. The installed app runs the server itself —
        # `ollama serve` after it fails with "address already in use", so it
        # isn't suggested.
        print("  ✗ no local model server answered. Ollama (free, local) installs with:")
        print("    Windows (PowerShell): irm https://ollama.com/install.ps1 | iex")
        print("    macOS / Linux       : curl -fsSL https://ollama.com/install.sh | sh")
        print("  then download a model that fits this computer's memory:")
        print("    ollama pull <model>     (models: ollama.com/search)")
        print("  LM Studio, llama.cpp (llama-server) and vLLM work too — start one, then rescan.")
        try:
            input("press ENTER when running to rescan (blank=cancel): ")
        except (EOFError, KeyboardInterrupt):
            return None
        found, _w = scan_endpoints()
        if not found:
            print("  ✗ still nothing live")
            return None
    if len(found) == 1:
        f = found[0]
    else:
        for i, f in enumerate(found, 1):
            print(f"  {i}. {f['name']} — {f['base_url']} "
                  f"({len(f['models'])} models)")
        raw = _ask(f"choose 1-{len(found)}", "1")
        if not (raw.isdigit() and 1 <= int(raw) <= len(found)):
            return None
        f = found[int(raw) - 1]
    if f["name"] == "ollama" and hw is not None:
        f["models"] = _ollama_fit(f["base_url"], hw, f["models"])
    if not f["models"]:
        print("  ✗ endpoint lists no models — is a model loaded?")
        return None
    model = _choose_model(f["models"], f["name"])
    return (f["name"], f["base_url"], None, model) if model else None


def _ollama_fit(url: str, hw: dict, models: list[str]) -> list[str]:
    """Each installed model marked fast / slower / too big for this
    computer, and a model can be fetched from Ollama's library — its size
    is checked BEFORE downloading, and nothing downloads without a yes."""
    from . import local_fit as LF
    sizes = dict(LF.installed(url))
    if models:
        print("  models already here:")
        for m in models:
            if sizes.get(m):
                kind, words = LF.verdict(LF.need_gb(sizes[m]), hw)
                mark = {"fast": "✓", "slow": "~", "too big": "✗"}[kind]
                print(f"    {mark} {m} — {words}")
    while True:
        try:
            name = input("  download another model? its name from ollama.com/search?c=tools "
                         "(e.g. name:tag), blank = no: ").strip()
        except (EOFError, KeyboardInterrupt):
            return models
        if not name:
            return models
        try:
            size = LF.registry_size(name)
        except Exception as e:                          # noqa: BLE001
            print(f"    couldn't reach Ollama's library: {str(e)[:100]}")
            continue
        if size is None:
            print(f"    '{name}' isn't in Ollama's library — check the name and tag")
            continue
        kind, words = LF.verdict(LF.need_gb(size), hw)
        print(f"    {name}: a {size / LF.GB:.1f} GB download — {words}")
        if kind == "too big":
            print("    it won't run well here; pick a smaller size (a lower number before 'b')")
        try:
            go = input(f"    download {size / LF.GB:.1f} GB now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            go = ""
        if go not in ("y", "yes"):
            continue
        ok, why = LF.pull(url, name)
        if not ok:
            print(f"    ✗ the download failed: {why}")
            continue
        print(f"    ✓ {name} downloaded")
        full = name if ":" in name else name + ":latest"
        return [full] + [m for m in models if m != full]


def _lane_custom() -> tuple[str, str, str | None, str] | None:
    url = _ask("base URL (incl. /v1)")
    if not url:
        print("  ✗ URL required")
        return None
    key = _secret("API key (blank for none)") or None
    # Idiot-proof law: live list FIRST (numbered picker), free-text last.
    ok, models, err, _c = _fetch_models(url, key)
    if not ok:
        print(f"  (couldn't list models: {err})")
    model = _choose_model(models if ok else [], url)
    if not model:
        return None
    # name from the URL host (custom.proxy.dev -> custom-proxy-dev)
    from urllib.parse import urlparse
    host = urlparse(url).hostname or "custom"
    name = "custom-" + host.replace(".", "-")[:24]
    return name, url, key, model


_LANES = {"1": ("cloud", "_lane_cloud"),
          "2": ("local", "_lane_local"),
          "3": ("custom", "_lane_custom")}


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def choose_endpoint() -> ProviderProfile | None:
    """The intent question → lane → live completion gate → save + activate.
    Returns the working profile, or None (skipped / failed — nothing
    saved). Shared by `aura setup` and the first-run gate."""
    print()
    print("What do you want to use Aura with?")
    print("  1. A cloud provider (paste an API key — I'll identify it)")
    print("  2. A local model on this machine (Ollama / LM Studio / ...)")
    print("  3. A custom endpoint (any OpenAI-compatible URL + key)")
    print("  4. Skip for now")
    try:
        lane = input("choose 1-4 [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("cancelled — nothing saved")
        return None

    if lane not in _LANES:
        print("skipped — nothing saved. Run 'aura setup' when ready.")
        return None

    lane_name, fn_name = _LANES[lane]
    picked = globals()[fn_name]()
    if not picked:
        print("cancelled — nothing saved")
        return None
    name, url, key, model = picked

    # LIVE COMPLETION GATE — /models 200 is not proof
    cand = ProviderProfile(name=name, base_url=url, model=model, api_key=key)
    print(f"\nlive-testing {model} @ {url} ...")
    ok, detail = live_completion_test(cand)
    if not ok:
        print(f"  ✗ live test failed: {detail}")
        print("  nothing was saved — fix the endpoint and re-run 'aura setup'")
        return None
    print(f"  ✓ live test passed ({detail})")

    fields = dict(base_url=url, model=model)
    if key:
        fields["api_key"] = key
    set_profile(name, **fields)
    set_active(name)
    from .first_run import mark_verified
    mark_verified(name, cand)
    # a local server can answer and still cut Aura's instructions (Ollama
    # runs with 4,096 tokens by default) or never call a tool — say so now
    try:
        from .setup_steps import local_checks
        found, problems = local_checks(cand)
        for f in found:
            print(f"  {'✓' if f.state == 'ok' else '!' if f.state == 'warn' else '·'} {f.text}")
        for what, fix in problems:
            print(f"  ! {what}")
            print(f"    fix: {fix}")
    except Exception:                                   # noqa: BLE001
        pass
    print(f"  ✓ active profile: {name} -> {url} ({model})")
    print("    more profiles later: /profile in chat")
    return cand


def run_quick() -> int:
    print()
    print("┌─────────────────────────────────────────────────────────┐")
    print("│              ☤ Aura Setup — quick                       │")
    print("└─────────────────────────────────────────────────────────┘")
    print()
    bak = _backup_profiles()
    if bak:
        print(f"  (auto-backup: {bak}")
        from .setup import restore_hint
        print(f"   restore: {restore_hint(bak)})")

    if choose_endpoint() is None:
        return 1

    print()
    print("Next:")
    print("  aura chat              — start working (in your project folder)")
    print('  aura run "task"        — one task, start to finish')
    print("  aura setup full        — the other choices (tools, safety, web, …)")
    return 0

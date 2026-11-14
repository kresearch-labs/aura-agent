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
    from . import setup_ui as SU
    if len(models) == 1:
        SU.line(f"{where} has one model: {models[0]}", "ok")
        return models[0]
    if models and SU.interactive():
        return SU.pick_one("Which model should Aura use?",
                           [SU.Opt(m, m) for m in models], models[0],
                           f"{len(models)} on {where}, newest first — type to search")
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
    """Pick the provider first: Aura shows how to get its key, opens the
    page, and tests the key by listing the provider's models. Or paste a key
    and Aura recognises the provider."""
    from . import setup_journeys as J
    from . import setup_ui as SU
    from .providers import unusable_key
    while True:
        names = list(CLOUD_PROVIDERS)
        prov = SU.pick_one("Which provider?",
                           [SU.Opt("paste", "I have a key — Aura recognises it", "")]
                           + [SU.Opt(k, CLOUD_PROVIDERS[k]["label"],
                                     "needs an account with credit") for k in names
                              if CLOUD_PROVIDERS[k].get("base_url")], "paste")
        if prov is None:
            return None
        if prov == "paste":
            return _lane_cloud_paste()
        p = CLOUD_PROVIDERS[prov]
        url = p["base_url"]
        got: dict = {}

        def check(key: str):
            bad = unusable_key(key)
            if bad:
                return bad
            ok, models, err, code = _fetch_models(url, key)
            if not ok:
                return (J.http_why(code, err) if code else
                        f"couldn't reach {p['label']} — it may be down, or this computer "
                        "is offline")
            got["models"] = models
            return None
        key = J.get_value(J.provider_guide(prov), f"To use {p['label']} you need an API key:",
                          check, back_label="Choose another provider")
        if key == J.AGAIN:
            continue
        if not key:
            return None
        model = _choose_model(got.get("models") or [], p["label"])
        return (prov, url, key, model) if model else None


def _lane_cloud_paste() -> tuple[str, str, str | None, str] | None:
    from . import setup_ui as SU
    from .first_run import _why
    from .providers import unusable_key
    while True:
        print()
        key = _secret("Paste your API key")
        if not key:
            SU.line("no key entered", "warn")
            return None
        bad = unusable_key(key)
        if bad:
            SU.line(bad, "fail")
            if SU.yes_no("Paste a different key?", True, "Yes", "Go back"):
                continue
            return None
        provider = detect_provider_from_key(key)
        if provider:
            SU.line(f"the key looks like {CLOUD_PROVIDERS[provider]['label']}", "ok")
        else:
            provider = SU.pick_one("Which provider is this key from?",
                                   [SU.Opt(k, CLOUD_PROVIDERS[k]["label"])
                                    for k in CLOUD_PROVIDERS], next(iter(CLOUD_PROVIDERS)))
            if provider is None:
                return None
        p = CLOUD_PROVIDERS[provider]
        if p.get("note"):
            SU.line(f"{p['label']}: {p['note']}", "detail")
        url = p.get("base_url") or SU.ask_text("The provider's address (with /v1)")
        if not url:
            return None
        while True:                       # The model list comes from the provider
            ok, models, err, code = _fetch_models(url, key)
            if ok:
                break
            why = _why(code, err) if code else ("couldn't reach " + p["label"]
                                                + " — it may be down, or this computer is offline")
            what = SU.recover(f"{p['label']}: {why}",
                              extra=[SU.Opt("key", "Paste a different key", "")],
                              change_label="", default="key" if code in (401, 403) else SU.TRY)
            if what == SU.TRY:
                continue
            break
        if not ok:
            if what == "key":
                continue
            return None
        model = _choose_model(models, p["label"])
        return (provider, url, key or None, model) if model else None


def _lane_local() -> tuple[str, str, str | None, str] | None:
    from . import setup_ui as SU
    # What this computer can run — said first, so a model that fits is picked,
    # not one that crawls or won't start
    from . import local_fit as LF
    try:
        hw = LF.hardware()
        SU.note(f"This computer: {LF.describe(hw)}")
        SU.line(f"what fits: {LF.size_classes(hw)}", "detail")
    except Exception:                                   # noqa: BLE001
        hw = None
    SU.note("Looking for models on this computer…")
    found, walled = scan_endpoints()
    for w in walled:
        SU.line(f"{w['name']} at {w['base_url']} needs an API key", "detail")
    while not found:
        exe = ollama_exe()
        if exe:
            SU.line("Ollama is installed, but it isn't running.", "warn")
            what = SU.pick_one("What now?", [
                SU.Opt("start", "Start Ollama for me", ""),
                SU.Opt("again", "Look again", "if you started it yourself"),
                SU.Opt("skip", "Go back", "choose another way, or skip")], "start")
        else:
            SU.line("No model program is running on this computer.", "warn")
            SU.note("Ollama is free and runs models on your computer (LM Studio, "
                    "llama.cpp and vLLM work too).")
            what = SU.pick_one("What now?", [
                SU.Opt("install", "Install Ollama for me", "from ollama.com, its own installer"),
                SU.Opt("again", "Look again", "if you started a model program yourself"),
                SU.Opt("skip", "Go back", "choose another way, or skip")], "install")
        if what in (None, "skip"):
            return None
        if what == "install":
            if not install_ollama():
                continue
        elif what == "start":
            start_ollama(exe)
        SU.note("Looking again…")
        found = wait_for_local(30 if what in ("start", "install") else 3)
        if not found:
            SU.line("still nothing answering", "fail")
    if len(found) == 1:
        f = found[0]
    else:
        v = SU.pick_one("Which program should Aura use?",
                        [SU.Opt(str(i), f["name"], f"{len(f['models'])} models")
                         for i, f in enumerate(found)], "0")
        if v is None:
            return None
        f = found[int(v)]
    if f["name"] == "ollama" and hw is not None:
        model = _pick_ollama(f["base_url"], hw, f["models"])
        return (f["name"], f["base_url"], None, model) if model else None
    while not f["models"]:
        what = SU.recover(f"{f['name']} has no model loaded — load one in it",
                          change_label="", try_label="Look again (after loading one)")
        if what != SU.TRY:
            return None
        again = [x for x in scan_endpoints()[0] if x["base_url"] == f["base_url"]]
        f = again[0] if again else f
    model = _choose_model(f["models"], f["name"])
    return (f["name"], f["base_url"], None, model) if model else None


def ollama_exe() -> str | None:
    """Where Ollama is, when it is installed (running or not)."""
    import os
    import shutil
    exe = shutil.which("ollama")
    if exe:
        return exe
    for c in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
              "/Applications/Ollama.app/Contents/Resources/ollama",
              "/usr/local/bin/ollama", "/usr/bin/ollama"):
        if c and os.path.isfile(c):
            return c
    return None


def start_ollama(exe: str | None = None) -> None:
    """Start the Ollama server in the background (its app on Windows/macOS)."""
    import os
    import subprocess
    import sys as _sys
    from . import setup_ui as SU
    exe = exe or ollama_exe()
    SU.note("starting Ollama…")
    try:
        if _sys.platform == "darwin" and os.path.isdir("/Applications/Ollama.app"):
            subprocess.Popen(["open", "-a", "Ollama"])
            return
        if os.name == "nt":
            app = os.path.join(os.path.dirname(exe or ""), "ollama app.exe")
            argv = [app] if os.path.isfile(app) else [exe, "serve"]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | \
                getattr(subprocess, "DETACHED_PROCESS", 0x8)
            subprocess.Popen(argv, creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        subprocess.Popen([exe, "serve"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as e:
        SU.line(f"couldn't start Ollama: {e}", "fail")


def install_ollama() -> bool:
    """Ollama's own installer (ollama.com/download), on a yes."""
    import os
    from . import setup_ui as SU
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               "irm https://ollama.com/install.ps1 | iex"]
        shell = False
    else:
        cmd, shell = "curl -fsSL https://ollama.com/install.sh | sh", True
    ok = SU.run_command("installing Ollama", cmd, shell=shell)
    if ok and ollama_exe():
        start_ollama()
    return ok


def wait_for_local(seconds: float) -> list:
    import time
    end = time.time() + seconds
    while True:
        found, _w = scan_endpoints()
        if found or time.time() >= end:
            return found
        time.sleep(2)


def _ollama_fit(url: str, hw: dict, models: list[str]) -> list[str]:
    """Kept for callers outside setup: the models, unchanged."""
    return models


def _pick_ollama(url: str, hw: dict, models: list[str]) -> str | None:
    """Ollama's models in one list — on Ollama's cloud, and on this computer
    marked by how well each runs here — with "download a new model" as one
    of the choices. Its size is checked BEFORE downloading, and nothing
    downloads without a yes."""
    from . import local_fit as LF
    from . import setup_ui as SU
    sizes = dict(LF.installed(url))
    DL = "+download"
    while True:
        cloud = [m for m in models if m.endswith(":cloud") or m.endswith("-cloud")]
        here = [m for m in models if m not in cloud]
        opts, first_fit = [], ""
        for m in cloud:
            opts.append(SU.Opt(m, m, "", "On Ollama's cloud — fast, needs your Ollama sign-in"))
        for m in here:
            hint = ""
            if sizes.get(m):
                kind, words = LF.verdict(LF.need_gb(sizes[m]), hw)
                hint = {"fast": "runs well here", "slow": "runs, but slowly",
                        "too big": "too big for this computer"}.get(kind, words)
                if kind == "fast" and not first_fit:
                    first_fit = m
            opts.append(SU.Opt(m, m, hint, "On this computer — private, works offline"))
        opts.append(SU.Opt(DL, "+ Download a new model", "its size is checked first",
                           "More"))
        default = (cloud[0] if cloud else first_fit or (here[0] if here else DL))
        v = SU.pick_one("Which model should Aura use?", opts, default)
        if v is None:
            return None
        if v != DL:
            return v
        name = SU.ask_text("The model's name", "",
                           "from ollama.com/search?c=tools — for example qwen3:8b")
        if not name:
            continue
        try:
            size = LF.registry_size(name)
        except Exception as e:                          # noqa: BLE001
            SU.line(f"couldn't reach Ollama's library: {str(e)[:100]}", "fail")
            continue
        if size is None:
            SU.line(f"'{name}' isn't in Ollama's library — check the name", "fail")
            continue
        kind, words = LF.verdict(LF.need_gb(size), hw)
        SU.line(f"{name}: a {size / LF.GB:.1f} GB download — {words}",
                "warn" if kind == "too big" else "info")
        if not SU.yes_no(f"Download {size / LF.GB:.1f} GB now?", kind != "too big",
                         "Download", "Not this one"):
            continue
        ok, why = LF.pull(url, name)
        if not ok:
            SU.line(f"the download failed: {why}", "fail")
            continue
        SU.line(f"{name} downloaded", "ok")
        return name if ":" in name else name + ":latest"


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

def _local_running() -> bool:
    """Only to pick the suggested answer: is a model program listening on
    this computer's usual ports? A connect, nothing is asked of it."""
    import socket
    for port in (11434, 1234, 8080, 8000):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            continue
    return False


def choose_endpoint() -> ProviderProfile | None:
    """The intent question → lane → live completion gate → save + activate.
    Returns the working profile, or None (skipped — nothing saved). Shared by
    `aura setup` and the first-run gate. Never dead-ends: a failure is said
    plainly with what can be done next.
    """
    from . import setup_ui as SU
    lane = None
    picked = None
    while True:
        if picked is None:
            if lane is None:
                lane = SU.pick_one("Where is the AI model Aura should use?", [
                    SU.Opt("1", "A cloud provider", "paste an API key — Aura recognises it"),
                    SU.Opt("2", "On this computer", "Ollama, LM Studio, llama.cpp, vLLM"),
                    SU.Opt("3", "Another address", "any OpenAI-compatible URL and key"),
                    SU.Opt("4", "Skip for now", "")], "2" if _local_running() else "1")
                if lane is None or lane not in _LANES:
                    print("  skipped — nothing saved. Run 'aura setup model' when ready.")
                    return None
            picked = globals()[_LANES[lane][1]]()
            if not picked:
                what = SU.pick_one("No model chosen — what now?", [
                    SU.Opt("other", "Choose another way", "cloud, this computer, an address"),
                    SU.Opt("again", "Try that again", ""),
                    SU.Opt("skip", "Skip for now", "Aura can't work until a model is set")],
                    "other")
                if what == "again":
                    continue
                if what == "other":
                    lane = None
                    continue
                print("  skipped — nothing saved. Run 'aura setup model' when ready.")
                return None
        name, url, key, model = picked
        cand = ProviderProfile(name=name, base_url=url, model=model, api_key=key)
        print()
        SU.note(f"Testing {model}…")
        ok, detail = live_completion_test(cand)
        if ok:
            break
        problem, fixes, default = _diagnose(cand, detail)
        opts = fixes + [SU.Opt("model", "Pick another model", ""),
                        SU.Opt("other", "Choose another way", "cloud, this computer, an address")]
        what = SU.recover(problem, extra=opts, change_label="", default=default or SU.TRY)
        if what == SU.SKIP:
            print("  nothing saved — run 'aura setup model' when ready.")
            return None
        if what == "model":
            picked = None
            continue
        if what == "other":
            picked, lane = None, None
            continue
        if what == "signin":
            exe = ollama_exe()
            SU.note("Ollama opens a sign-in page in your browser — sign in there.")
            SU.run_command("signing in to Ollama", [exe or "ollama", "signin"])
        elif what == "key":
            new = _secret("Paste a different API key")
            if new:
                picked = (name, url, new, model)
        # TRY, or after a fix: test again

    fields = dict(base_url=url, model=model)
    if cand.api_key:
        fields["api_key"] = cand.api_key
    set_profile(name, **fields)
    set_active(name)
    from .first_run import mark_verified
    mark_verified(name, cand)
    # a local server can answer and still cut Aura's instructions (Ollama
    # runs with 4,096 tokens by default) or never call a tool — say so now,
    # and fix what Aura can fix
    tools_ok = True
    try:
        from .setup_steps import local_checks
        found, problems = local_checks(cand)
        tools_ok = not any("tool" in w for w, _ in problems)
        for f in found:
            SU.line(f.text, {"ok": "detail", "warn": "warn"}.get(f.state, "detail"))
        for what, fix in problems:
            if name == "ollama" and "window" in what and fix_ollama_window():
                continue
            SU.line(what, "warn")
            SU.note(f"    fix: {fix}")
    except KeyboardInterrupt:
        raise
    except Exception:                                   # noqa: BLE001
        pass
    SU.line(f"{model} works" + (" and can use tools" if tools_ok else ""), "ok")
    SU.line(f"saved as model setup '{name}' ({url})", "detail")
    return cand


def _diagnose(cand, detail: str) -> tuple[str, list, str]:
    """A failed model test in plain words, and the fixes that fit it."""
    from . import setup_ui as SU
    d = (detail or "").lower()
    local = any(h in (cand.base_url or "") for h in ("localhost", "127.0.0.1", "[::1]"))
    cloud_model = (cand.model or "").endswith((":cloud", "-cloud"))
    if local and cloud_model and any(w in d for w in ("refused", "401", "403",
                                                       "unauthorized", "sign")):
        return ("Ollama isn't signed in — its cloud models need an ollama.com account",
                [SU.Opt("signin", "Sign in to Ollama for me", "opens ollama.com in your browser")],
                "signin")
    if any(w in d for w in ("timed out", "timeout", "time out")):
        return (f"{cand.model} didn't answer in time — the first load of a big model can "
                "be slow, or it may be too big for this computer", [], SU.TRY)
    if any(w in d for w in ("memory", "out of memory", "oom", "requires more")):
        return (f"{cand.model} needs more memory than this computer has free — pick a "
                "smaller one", [], "model")
    if "key was refused" in d or "401" in d:
        return ("the API key was refused", [SU.Opt("key", "Paste a different key", "")], "key")
    if "credit" in d or "billing" in d or "402" in d:
        return ("the account has no credit or billing set up for this — add it on the "
                "provider's site, or choose another way", [], "other")
    if "isn't available" in d or "retired" in d:
        return (f"{cand.model} isn't available there", [], "model")
    if any(w in d for w in ("unreachable", "connection", "refused", "getaddrinfo",
                            "offline", "urlopen", "errno")):
        return ("couldn't reach the model — the server may be down, or this computer "
                "is offline", [], SU.TRY)
    return (f"{cand.model} didn't answer: {detail}", [], SU.TRY)


def fix_ollama_window(size: int = 32768) -> bool:
    """Ollama runs every model with 4,096 tokens unless OLLAMA_CONTEXT_LENGTH is
    set — too small for Aura. Set it and restart Ollama, on a yes."""
    import os
    import sys as _sys
    from . import setup_ui as SU
    SU.line("Ollama gives models only 4,096 tokens of room — too small for Aura's "
            "instructions and your work", "warn")
    if not SU.yes_no(f"Raise it to {size:,} and restart Ollama for you?", True,
                     "Yes, fix it", "Not now", yes_hint="sets OLLAMA_CONTEXT_LENGTH",
                     rec=True):
        return False
    if os.name == "nt":
        ok = SU.run_command("saving the setting", ["setx", "OLLAMA_CONTEXT_LENGTH", str(size)])
        if ok:
            import subprocess
            for im in ("ollama app.exe", "ollama.exe"):
                subprocess.run(["taskkill", "/IM", im, "/F"], capture_output=True)
            os.environ["OLLAMA_CONTEXT_LENGTH"] = str(size)
            start_ollama()
    elif _sys.platform == "darwin":
        ok = SU.run_command("saving the setting and restarting Ollama",
                            f"launchctl setenv OLLAMA_CONTEXT_LENGTH {size} && "
                            "osascript -e 'quit app \"Ollama\"'; sleep 2; open -a Ollama",
                            shell=True)
    else:
        ok = SU.run_command(
            "saving the setting and restarting Ollama (asks for your password)",
            "sudo mkdir -p /etc/systemd/system/ollama.service.d && "
            f"printf '[Service]\\nEnvironment=\"OLLAMA_CONTEXT_LENGTH={size}\"\\n' | "
            "sudo tee /etc/systemd/system/ollama.service.d/aura-context.conf >/dev/null && "
            "sudo systemctl daemon-reload && sudo systemctl restart ollama", shell=True)
    if ok:
        wait_for_local(20)
        SU.line(f"Ollama now gives models {size:,} tokens", "ok")
    return ok


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

"""setup_journeys.py — every setup option works end to end.

Choosing an option is only the start. Each one goes:

  guide what it is, what it costs, the steps — in plain words
  open Aura opens the page (or the app) for the user
  get the key / address / sign-in, typed hidden where it is secret
  check a REAL test (a search, a message, the server's tool list)
  use say what Aura now does with it

And a failing test is diagnosed (wrong key, plan not active, credits used up,
site down, a program missing) with what fixes it — Aura does the fix on a yes
(installs a missing program, opens the page again).
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import setup_ui as SU

AGAIN = "__again__"          # A journey's "choose another" — the question is asked again


@dataclass
class Guide:
    what: str                  # "a Brave Search API key"
    url: str                   # The page that gives it
    steps: list = field(default_factory=list)
    cost: str = ""             # Plain words, said before anything is opened
    prefix: tuple = ()         # What a real one starts with (a format check)
    secret: bool = True        # Typed hidden
    optional: bool = False     # It works without one too


# ------------------------------------------------------------ helpers --
def open_page(url: str) -> None:
    """Open a page in the user's browser; the address is shown too, in case
    no browser opens (SSH, a server)."""
    SU.note(f"opening {url}")
    try:
        import webbrowser
        webbrowser.open(url, new=2)
    except Exception:                                # noqa: BLE001
        pass


def show_guide(g: Guide, title: str) -> None:
    print()
    print("  " + SU.ui.bold(title))
    if g.cost:
        SU.note(g.cost)
    for i, s in enumerate(g.steps, 1):
        print(f"    {i}. {s}")


def get_value(g: Guide, title: str, check, back_label: str = "Choose another") -> str | None:
    """Guide → open the page → paste → check. Returns the working value, AGAIN
    (choose another option), or None (not now)."""
    show_guide(g, title)
    opened = False
    while True:
        from urllib.parse import urlparse
        host = (urlparse(g.url).hostname or g.url).removeprefix("www.")
        opts = [SU.Opt("open", (f"Open {host} in my browser" if not opened
                                else f"Open {host} again"), ""),
                SU.Opt("have", "I have it — paste it", ""),
                SU.Opt("back", back_label, ""),
                SU.Opt("skip", "Not now", "")]
        what = SU.pick_one(f"Get {g.what}", opts, "have" if opened else "open")
        if what in (None, "skip"):
            return None
        if what == "back":
            return AGAIN
        if what == "open":
            open_page(g.url)
            opened = True
            continue
        if g.secret:
            from .setup_quick import _secret
            val = _secret(f"Paste {g.what}").strip()
        else:
            val = SU.ask_text(f"Paste {g.what}").strip()
        if not val:
            continue
        if g.prefix and not val.startswith(g.prefix):
            SU.line(f"that doesn't look like {g.what} — it starts with "
                    f"{' or '.join(g.prefix)}", "warn")
            if not SU.yes_no("Use it anyway?", False, "Yes, test it", "No, paste again"):
                continue
        SU.note("testing…")
        try:
            problem = check(val)
        except KeyboardInterrupt:
            raise
        except Exception as e:                       # noqa: BLE001
            problem = SU.plain_error(e)
        if not problem:
            return val
        SU.line(problem, "fail")


def http_why(code: int | None, body: str = "", plan_hint: str = "") -> str:
    """A refused request in plain words."""
    b = (body or "").lower()
    if code in (401, 403):
        if "plan" in b or "subscri" in b or plan_hint:
            return ("the key wasn't accepted — " + (plan_hint or "the plan isn't active "
                                                    "on the account yet"))
        return "the key wasn't accepted — copy it again from the page (all of it)"
    if code == 402 or "credit" in b or "quota" in b or "billing" in b:
        return "the account has no credit left — add credit on the page, or pick another"
    if code == 429:
        return "too many requests — the free amount may be used up; wait, or add credit"
    if code and code >= 500:
        return "the service is having trouble right now — try again in a minute"
    return f"the service answered with an error (HTTP {code})"


def _err_code(e: BaseException) -> tuple[int | None, str]:
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:                            # noqa: BLE001
            body = ""
        return e.code, body
    s = str(e)
    import re
    m = re.search(r"\b(40[0-9]|42[0-9]|5[0-9]{2})\b", s)
    return (int(m.group(1)) if m else None), s


# ------------------------------------------------- programs Aura adds --
# Each with the package manager this computer has (winget / Homebrew / apt).
PROGRAMS = {
    "node": {"why": "npm and npx come with Node.js",
             "winget": "OpenJS.NodeJS.LTS", "brew": "node", "apt": "nodejs npm"},
    "go": {"why": "Go's tools", "winget": "GoLang.Go", "brew": "go", "apt": "golang"},
    "rustup": {"why": "Rust's tools", "winget": "Rustlang.Rustup", "brew": "rustup",
               "sh": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"},
    "clangd": {"why": "from LLVM · a large download (about 400 MB)",
               "winget": "LLVM.LLVM", "brew": "llvm", "apt": "clangd"},
    "docker": {"why": "Docker runs the app in a container",
               "winget": "Docker.DockerDesktop", "brew_cask": "docker", "apt": "docker.io"},
}
NEEDS = {"npm": "node", "npx": "node", "go": "go", "rustup": "rustup", "docker": "docker"}


def install_program(prog: str) -> bool:
    """Install a program the user doesn't have, on a yes, with the package
    manager this computer has."""
    p = PROGRAMS.get(prog) or {}
    if os.name == "nt" and shutil.which("winget") and p.get("winget"):
        cmd, sh = ["winget", "install", "-e", "--id", p["winget"],
                   "--accept-package-agreements", "--accept-source-agreements"], False
    elif sys.platform == "darwin" and shutil.which("brew") and (p.get("brew") or p.get("brew_cask")):
        cmd, sh = (["brew", "install", "--cask", p["brew_cask"]] if p.get("brew_cask")
                   else ["brew", "install", p["brew"]]), False
    elif p.get("sh"):
        cmd, sh = p["sh"], True
    elif shutil.which("apt-get") and p.get("apt"):
        cmd, sh = f"sudo apt-get install -y {p['apt']}", True
    else:
        SU.line(f"Aura can't install {prog} on this computer by itself — get it from its "
                "own site, then run aura setup again", "fail")
        return False
    if not SU.yes_no(f"Install {prog} for you?", True, "Yes, install it", "Not now",
                     yes_hint=p.get("why", "")):
        return False
    ok = SU.run_command(f"installing {prog}", cmd, shell=sh)
    _refresh_path()
    if prog == "clangd":
        from .lsp import find_server
        return ok and bool(find_server("c"))
    return ok and bool(shutil.which(prog) or shutil.which(prog + ".exe")
                       or prog == "docker")


def _refresh_path() -> None:
    """A program just installed is on the user PATH, not yet in this process's."""
    if os.name != "nt":
        return
    try:
        import winreg
        parts = []
        for root, sub in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
            except OSError:
                pass
        if parts:
            os.environ["PATH"] = ";".join(parts + [os.environ.get("PATH", "")])
    except Exception:                                # noqa: BLE001
        pass


def ensure_program(cmd: str) -> bool:
    """The program a fix needs: there already, or installed now on a yes."""
    if shutil.which(cmd):
        return True
    prog = NEEDS.get(cmd, cmd)
    SU.line(f"this needs {prog}, which isn't on this computer", "warn")
    return install_program(prog) and bool(shutil.which(cmd))


# ------------------------------------------------------ web search --
SEARCH_GUIDES = {
    "brave": Guide("a Brave Search API key", "https://api-dashboard.search.brave.com/register",
                   ["Make an account and sign in", "Add a card (Brave asks for one even "
                    "for the free credit)", "Subscribe to the Search plan",
                    "Open 'API keys' → 'Add API key', and copy it"],
                   "$5 of free credit a month (about 1,000 searches), then $5 per 1,000"),
    "tavily": Guide("a Tavily API key", "https://app.tavily.com",
                    ["Sign in (Google works)", "Copy the API key on the first page"],
                    "1,000 free searches a month, no card", prefix=("tvly-",)),
    "exa": Guide("an Exa API key", "https://dashboard.exa.ai/api-keys",
                 ["Sign in", "Under 'API keys' (not 'Service keys'), copy or create a key"],
                 "free credit to start, then pay per search"),
    "serper": Guide("a Serper API key", "https://serper.dev/api-key",
                    ["Sign up", "Copy the API key from the dashboard"],
                    "2,500 free searches to start, no card; Google's results"),
    "firecrawl": Guide("a Firecrawl API key", "https://www.firecrawl.dev/app/api-keys",
                       ["Sign up", "Copy the key on the API keys page"],
                       "a free amount each month, then pay per search", prefix=("fc-",)),
    "marginalia": Guide("a Marginalia API key", "https://about.marginalia-search.com/article/api",
                        ["To try it: type public (a shared key, often busy)",
                         "For your own key: buy one on the page"],
                        "a small, independent web index", secret=False),
    "searxng": Guide("your SearXNG address", "https://docs.searxng.org",
                     ["The address of a SearXNG server you run (https://…)",
                      "In its settings.yml, 'json' must be in search → formats"],
                     "no cost — your own server", secret=False, prefix=("http://", "https://")),
    "openalex": Guide("an OpenAlex API key", "https://openalex.org/settings/api",
                      ["Make a free account", "Copy the key from Settings → API"],
                      "research papers; free (a key is needed since February 2026)"),
}


def search_service(svc: str) -> str | None:
    """Connect a search service end to end. Returns the service name when
    connected, AGAIN to choose another, None for not now."""
    from . import websearch as W
    from .keystore import put, remove
    eng = W.BY_NAME.get(svc)
    g = SEARCH_GUIDES.get(svc)
    if eng is None or g is None:
        return None

    def check(val: str) -> str | None:
        if eng.needs:
            put(eng.needs, val)
        try:
            hits = eng.run("open source licence", 1)
        except Exception as e:                       # noqa: BLE001
            if eng.needs:
                remove(eng.needs)
            code, body = _err_code(e)
            if code is None and isinstance(e, (urllib.error.URLError, OSError)):
                return f"couldn't reach {eng.label} — it may be down, or this computer is offline"
            return http_why(code, body, "subscribe to the Search plan on the dashboard, then "
                                        "try again" if svc == "brave" else "")
        SU.line(f"{eng.label} works — a test search came back ({len(hits)} result"
                f"{'' if len(hits) == 1 else 's'})", "ok")
        return None

    got = get_value(g, f"To search with {eng.label} you need {g.what}:", check,
                    back_label="Choose another search service")
    if got in (None, AGAIN):
        return got
    SU.line(f"Aura now searches with {eng.label} first; the free sources stay as a backup",
            "ok")
    return svc


# --------------------------------------------------------- browser --
def browser_part(engine: str) -> str | None:
    """Pages that need JavaScript: Aura's browser part (Playwright) is added to
    this copy of Aura now, and a page is really opened. Returns a problem or
    None."""
    from .setup_steps import _browser_check, _have_playwright, installed_browsers
    if engine == "off":
        return None
    if not _have_playwright():              # The yes above was the yes to Playwright
        if not add_python_part("playwright>=1.62.0"):
            return "the browser part didn't install — see the lines above"
        mark_extra("web")
    if engine == "download" or not installed_browsers():
        ok = SU.run_command("downloading Chromium (about 150 MB)",
                            [sys.executable, "-m", "playwright", "install", "chromium"])
        if not ok:
            return "Chromium didn't download — the site may be down, or this computer is offline"
    ok, says = _browser_check()
    if not ok:
        return says
    SU.line(f"{says} — Aura can now use pages that need JavaScript", "ok")
    return None


def python_part_cmd(spec: str) -> list:
    """The command that adds a package to the Python Aura runs in — uv's own
    installer when the installer placed Aura, else pip."""
    from .lifecycle import layout
    lay = layout() or {}
    uv = lay.get("uv")
    if lay.get("method") == "uv-tool" and uv and os.path.isfile(uv):
        return [uv, "pip", "install", "--python", sys.executable, spec]
    return [sys.executable, "-m", "pip", "install", spec]


def add_python_part(spec: str) -> bool:
    """Add a package to the Python Aura runs in. New files only, so it works
    while Aura runs."""
    cmd = python_part_cmd(spec)
    ok = SU.run_command(f"adding {spec.split('>')[0].split('=')[0]}", cmd)
    import importlib
    importlib.invalidate_caches()
    return ok


def mark_extra(extra: str) -> None:
    """Keep the part on updates: the installer's record says which parts."""
    try:
        import json
        from .lifecycle import _aura_home
        f = _aura_home() / "install.json"
        lay = json.loads(f.read_text(encoding="utf-8"))
        have = [e for e in str(lay.get("extras") or "").split(",") if e]
        if extra not in have:
            lay["extras"] = ",".join(have + [extra])
            f.write_text(json.dumps(lay, indent=1), encoding="utf-8")
    except Exception:                                # noqa: BLE001
        pass


# -------------------------------------------------------- messages --
TELEGRAM = Guide("the bot token", "https://t.me/BotFather",
                 ["In Telegram, open @BotFather (Aura opens it)", "Send /newbot",
                  "Give it a name, then a username ending in 'bot'",
                  "BotFather replies with a token like 123456:ABC-… — copy it"],
                 "free")
DISCORD = Guide("the webhook address", "https://discord.com/app",
                ["Open the channel's settings (the gear next to its name)",
                 "Integrations → Webhooks → New Webhook", "Copy Webhook URL"],
                "free", prefix=("https://discord.com/api/webhooks/",
                                "https://discordapp.com/api/webhooks/"))
SLACK = Guide("the webhook address", "https://api.slack.com/apps?new_app=1",
              ["Create an app 'From scratch' and pick your workspace",
               "Incoming Webhooks → turn it on", "Add New Webhook → pick the channel",
               "Copy the Webhook URL"],
              "free", prefix=("https://hooks.slack.com/",))
WEBHOOK = Guide("the address", "https://webhook.site",
                ["Any address that takes a JSON POST (https://…)"],
                "", secret=True, prefix=("http://", "https://"))


def telegram() -> dict | None:
    """Bot token checked, the user's chat found (Aura waits for their
    message), a test message delivered. Returns {token, chat_id} / AGAIN / None."""
    import json

    def check(tok: str) -> str | None:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getMe",
                                        timeout=15) as r:
                me = json.load(r).get("result") or {}
        except urllib.error.HTTPError as e:
            return ("Telegram refused that token — copy it again from @BotFather"
                    if e.code in (401, 404) else http_why(e.code))
        except Exception as e:                       # noqa: BLE001
            return f"couldn't reach Telegram — {SU.plain_error(e)}"
        check.bot = me.get("username") or ""
        SU.line(f"the token works — your bot is @{check.bot}", "ok")
        return None
    check.bot = ""
    tok = get_value(TELEGRAM, "To get Aura's messages on Telegram you need a bot:", check,
                    back_label="Choose another way")
    if tok in (None, AGAIN):
        return tok
    from .setup_steps import telegram_chat_id
    while True:
        if check.bot:
            open_page(f"https://t.me/{check.bot}")
        SU.note(f"Send your bot @{check.bot} any message in Telegram — Aura is waiting…")
        chat, who = None, ""
        for _ in range(60):                           # Up to 2 minutes
            chat, who = telegram_chat_id(tok)
            if chat:
                break
            time.sleep(2)
        if chat:
            break
        what = SU.recover("no message reached the bot in 2 minutes",
                          try_label="Wait again", change_label="")
        if what != SU.TRY:
            return None
    SU.line(f"found your chat: {who or chat}", "ok")
    res = _send_test("telegram", {"token": tok, "chat_id": chat})
    if res:
        SU.line(res, "warn")
    return {"token": tok, "chat_id": chat}


def webhook(ch: str) -> str | None:
    g = {"discord": DISCORD, "slack": SLACK}.get(ch, WEBHOOK)
    label = {"discord": "Discord", "slack": "Slack"}.get(ch, "a webhook")

    def check(url: str) -> str | None:
        return _send_test(ch, {"url": url}, ok_line=True)
    return get_value(g, f"To get Aura's messages in {label}:", check,
                     back_label="Choose another way")


def _send_test(ch: str, cfg: dict, ok_line: bool = False) -> str | None:
    from .gateway import _send
    try:
        r = _send(ch, "Aura: messages will arrive here (setup test).", cfg)
    except Exception as e:                           # noqa: BLE001
        return f"the test message didn't go through — {SU.plain_error(e)}"
    if not r.get("ok"):
        d = str(r.get("detail", ""))
        code, _ = _err_code(Exception(d))
        if code == 404:
            return "that webhook doesn't exist (any more) — make a new one and paste it"
        return f"the test message didn't go through — {d[:140]}"
    if ok_line:
        SU.line(f"a test message was sent to {ch} — check it arrived", "ok")
    return None


# ------------------------------------------------------------ apps --
# Token pages
APP_TOKENS = {
    "github": Guide("a GitHub token", "https://github.com/settings/personal-access-tokens/new",
                    ["Name it 'Aura', pick an expiry", "Repository access: the repositories "
                     "Aura may use", "Permissions: what Aura may do (Contents, Issues, "
                     "Pull requests…)", "Generate token, and copy it"],
                    "free", prefix=("github_pat_", "ghp_")),
    "sentry": Guide("a Sentry auth token", "https://sentry.io/settings/account/api/auth-tokens/",
                    ["Create New Token", "Give it the scopes Aura needs (read is enough "
                     "to look at errors)", "Copy it"], "free with your Sentry account"),
    "supabase": Guide("a Supabase access token",
                      "https://supabase.com/dashboard/account/tokens",
                      ["Generate new token, name it 'Aura'", "Copy it"],
                      "free with your Supabase account", prefix=("sbp_",)),
}


def app(name: str, mcp_path) -> str | None:
    """Add one app and make it really work: its program (npx / uvx / Docker)
    is there, its token or sign-in is done, and it starts and lists its
    tools. Returns a problem, or None when the app works."""
    from . import mcp_registry as R
    p = R.POPULAR.get(name) or {}
    try:
        h = R.exact(name)
    except Exception as e:                           # noqa: BLE001
        return (f"couldn't look up {name} — the app list (MCP registry) may be down, or "
                f"this computer is offline ({SU.plain_error(e)})")
    if not h or not h.get("spec"):
        return f"{name} has no version Aura can start on this computer"
    spec = h["spec"]
    run = spec.get("command")
    if run and not shutil.which(run):
        if run == "uvx":
            from .lifecycle import layout
            uv = (layout() or {}).get("uv")
            if uv and os.path.isfile(uv):           # Aura's own uv has uvx's job
                spec = {**spec, "command": uv, "args": ["tool", "run", *spec.get("args", [])]}
            elif not ensure_program("uvx"):
                return f"{name} needs uvx, which isn't on this computer"
        elif not ensure_program(run):
            return f"{name} needs {run}, which isn't on this computer"
    if run == "docker" and not _docker_running():
        if not _start_docker():
            return "Docker is installed but not running — start Docker Desktop, then try again"
    g = APP_TOKENS.get(name)
    if g:
        from .keystore import put
        env_name = next(iter(spec.get("env") or {}), "") or (h.get("env") or [""])[0]

        def check(tok: str) -> str | None:
            return None
        tok = get_value(g, f"{p.get('by', name)} needs {g.what}:", check,
                        back_label="Don't add this app")
        if tok in (None, AGAIN):
            return "not added — no token"
        if env_name:
            put(env_name, tok)
    R.add(name, spec, mcp_path)
    if spec.get("url") and "sign-in" in str(p.get("account", "")):
        SU.note(f"{p.get('by', name)}: sign in in your browser (Aura opens it)…")
        try:
            from .mcp_oauth import login
            login(spec["url"], say=lambda s: SU.note(str(s)))
        except Exception as e:                       # noqa: BLE001
            return f"the {p.get('by', name)} sign-in didn't finish — {SU.plain_error(e)}"
    n, problem = tools_of(name, spec)
    if problem:
        return f"{name} was added but didn't start — {problem}"
    SU.line(f"{p.get('by', name)} works — {n} tools; Aura uses them when a task needs them",
            "ok")
    return None


def tools_of(name: str, spec: dict) -> tuple[int, str | None]:
    """Start the app's server for real and count its tools."""
    try:
        if spec.get("url"):
            from .mcp_http import RemoteMCPServer
            s = RemoteMCPServer(name, spec["url"], timeout=60)
        else:
            from .keystore import get
            from .mcp_client import MCPServer
            env = {k: (get(v[2:-1]) or "") if str(v).startswith("${") else v
                   for k, v in (spec.get("env") or {}).items()}
            s = MCPServer(name, spec["command"], list(spec.get("args") or []), env, timeout=120)
            s.start()
        try:
            tools = s.list_tools()
        finally:
            try:
                s.stop()
            except Exception:                        # noqa: BLE001
                pass
        return len(tools), None
    except Exception as e:                           # noqa: BLE001
        return 0, SU.plain_error(e)


def _docker_running() -> bool:
    import subprocess
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_docker() -> bool:
    import subprocess
    if not SU.yes_no("Start Docker Desktop for you?", True, "Yes, start it", "Not now"):
        return False
    try:
        if os.name == "nt":
            exe = os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe")
            subprocess.Popen([exe])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Docker"])
        else:
            SU.run_command("starting Docker", "sudo systemctl start docker", shell=True)
    except OSError:
        return False
    SU.note("waiting for Docker to start (it can take a minute)…")
    for _ in range(45):
        if _docker_running():
            SU.line("Docker is running", "ok")
            return True
        time.sleep(2)
    return False


# ----------------------------------------------------- model keys --
def provider_guide(provider: str) -> Guide:
    from .providers import CLOUD_PROVIDERS
    p = CLOUD_PROVIDERS[provider]
    page = p.get("key_page") or ""
    url = page if page.startswith("http") else f"https://{page}"
    return Guide(f"your {p['label']} API key", url,
                 ["Sign in (or make an account)", "Add a payment method or credit — "
                  "API use is paid per use", "Create an API key and copy it"],
                 "paid per use on your own account",
                 prefix=tuple(p.get("key_prefixes") or ()))

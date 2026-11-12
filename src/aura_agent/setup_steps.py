"""setup_steps.py — the steps of Aura's setup, as data.

Each step: look(ctx) what was found on this machine -> [Found] asks(ctx) the
choices, each with a suggestion + why -> [Ask] save(ctx, ans) write the choices
(only via Aura's own settings) check(ctx) a REAL check afterwards -> Proof
later the command that changes it later Every check goes into the setup record
(a plain list, secrets scrubbed), so `aura doctor` and the user can see what
was proven and when.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Found:
    text: str
    state: str = "info"          # Ok | warn | off | info


@dataclass
class Ask:
    key: str
    prompt: str
    choices: list                # [(value, what it means)]; [] = free text
    default: str = ""
    why: str = ""                # Why the suggestion
    secret: bool = False         # Typed hidden; saved to the key store only
    when: str = ""               # Ask only if this earlier answer key=value holds


@dataclass
class Proof:
    ok: bool
    evidence: str                # What was checked and what it showed
    missing: str = ""            # What isn't there (when not ok)
    fix: str = ""                # The command / step that fixes it


@dataclass
class Step:
    id: str
    num: int
    title: str
    quick: bool                  # Asked in quick setup too
    later: str
    look: Callable[[dict], list] = lambda ctx: []
    asks: Callable[[dict], list] = lambda ctx: []
    save: Callable[[dict, dict], list] = lambda ctx, ans: []
    check: Callable[[dict], Proof] = lambda ctx: Proof(True, "nothing to check")
    run: Callable | None = None  # A step with its own screens (the model)
    means: Callable[[dict], str] = lambda ans: ""


# ------------------------------------------------------------ context --
def context() -> dict:
    """What every step may need to know about this machine and setup."""
    ctx: dict = {"os": platform.system(), "nt": os.name == "nt",
                 "tty": sys.stdin.isatty() and sys.stdout.isatty()}
    try:
        from .config import resolve
        from .profile_store import active_name
        ctx["setup_name"] = active_name()
        ctx["profile"] = resolve(ctx["setup_name"])
    except Exception:                                # noqa: BLE001
        ctx["profile"] = None
    ctx["ctx_len"] = _context_len(ctx.get("profile"))
    return ctx


def _context_len(prof) -> int | None:
    if prof is None:
        return None
    if getattr(prof, "context_length", None):
        return int(prof.context_length)
    try:
        from .ctx_probe import server_reports
        return server_reports(prof.base_url, prof.model)
    except Exception:                                # noqa: BLE001
        return None


def _opt(key: str):
    from .options import get
    return get(key)


def _set(key: str, value) -> str | None:
    """Change one setting; a line saying what changed, or None if same."""
    from .options import fmt, set_
    old, new = set_(key, value, who="setup")
    return None if old == new else f"{key}: {fmt(old)} -> {fmt(new)}"


# --------------------------------------------- local servers (step 1) --
# A server can look healthy and still drop Aura's instructions or never call a
# tool — so both are checked for real here.
ROOM = 16384          # Aura works well from here; 32,768 is better


def _start_need(ctx: dict | None = None) -> int:
    """Tokens Aura sends before any of the user's work: its instructions,
    memory and notes (a few thousand) plus the starting tool list."""
    try:
        return _start_tokens(ctx or {}) + 3000
    except Exception:                                # noqa: BLE001
        return 5000


def _server_kind(base_url: str) -> str:
    from .ctx_probe import _get_json
    root = base_url.rstrip("/").removesuffix("/v1")
    if isinstance(_get_json(f"{root}/api/version"), dict):
        return "ollama"
    if isinstance(_get_json(f"{root}/props"), dict):
        return "llamacpp"
    if isinstance(_get_json(f"{root}/api/v0/models"), dict):
        return "lmstudio"
    data = _get_json(base_url.rstrip("/") + "/models")
    if isinstance(data, dict) and any("max_model_len" in (e or {}) for e in data.get("data") or []):
        return "vllm"
    return "other"


CTX_FIX = {
    "ollama": ("set OLLAMA_CONTEXT_LENGTH=32768 for the Ollama server and restart it — "
               "Windows: your user environment variables, then start Ollama again; "
               "macOS: launchctl setenv OLLAMA_CONTEXT_LENGTH 32768, restart the app; "
               "Linux: systemctl edit ollama.service → Environment=\"OLLAMA_CONTEXT_LENGTH=32768\", "
               "then systemctl daemon-reload && systemctl restart ollama"),
    "lmstudio": "load the model again with a larger context length (32768) in LM Studio",
    "llamacpp": "start llama-server with -c 32768",
    "vllm": "start vLLM with --max-model-len 32768",
    "other": "start the server with a larger context window (32768)",
}
TOOL_FIX = {
    "llamacpp": "start llama-server with --jinja (tool calls need the model's chat template)",
    "vllm": "start vLLM with --enable-auto-tool-choice --tool-call-parser <the parser for "
            "this model> (docs.vllm.ai: Tool Calling)",
    "ollama": "this model may not support tools in Ollama — pick one tagged 'tools' on "
              "ollama.com/search",
    "lmstudio": "pick a model LM Studio marks as tool-capable",
    "other": "check the server's tool-calling settings",
}


def tool_call_works(prof) -> tuple[bool | None, str]:
    """One tiny request with one tool: did a tool call come back?"""
    import urllib.error
    import urllib.request
    body = {"model": prof.model, "stream": False,
            "messages": [{"role": "user", "content":
                          "What time is it? Use the get_time tool."}],
            "tools": [{"type": "function", "function": {
                "name": "get_time", "description": "the current time",
                "parameters": {"type": "object", "properties": {}}}}]}
    req = urllib.request.Request(
        prof.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {prof.api_key}"} if prof.api_key else {})})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:                            # noqa: BLE001
            text = ""
        return False, f"the server refused tools (HTTP {e.code}): {' '.join(text.split())[:140]}"
    except Exception as e:                           # noqa: BLE001
        return None, f"couldn't check tools: {str(e)[:100]}"
    msg = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    if msg.get("tool_calls"):
        return True, "a test tool call came back"
    return False, "the model answered in text instead of calling the tool"


def local_checks(prof) -> tuple[list, list]:
    """For a local server: [Found] and the problems [(what, fix)]."""
    from .ctx_probe import running_window
    from .first_run import _is_local
    if prof is None or not _is_local(prof.base_url):
        return [], []
    kind = _server_kind(prof.base_url)
    found, problems = [], []
    need = _start_need()
    win = running_window(prof.base_url, prof.model)
    if win is None and kind in ("llamacpp", "vllm"):
        from .ctx_probe import probe_context_length
        win, _ = probe_context_length(prof.base_url, prof.model)
    if win:
        room = win - need
        ok = win >= ROOM
        found.append(Found(f"the server runs {prof.model} with a {win:,}-token window; Aura "
                           f"starts each chat with about {need:,} — {max(room, 0):,} left for "
                           "your work", "ok" if ok else "warn"))
        if not ok:
            problems.append((f"a {win:,}-token window is too small (Aura works well from "
                             f"{ROOM:,})", CTX_FIX.get(kind, CTX_FIX["other"])))
    else:
        found.append(Found("the server didn't say the window it runs the model with "
                           "(it may not be loaded yet)", "info"))
    works, says = tool_call_works(prof)
    if works is None:
        found.append(Found(says, "info"))
    else:
        found.append(Found(says, "ok" if works else "warn"))
        if not works:
            problems.append(("tool calls don't work", TOOL_FIX.get(kind, TOOL_FIX["other"])))
    return found, problems


# ----------------------------------------------------------- 1 model --
def _model_look(ctx) -> list:
    p = ctx.get("profile")
    if p is None:
        return [Found("no model chosen yet", "warn")]
    from .first_run import was_verified
    ok = was_verified(ctx.get("setup_name", ""), p)
    return [Found(f"{p.model} at {p.base_url}", "ok" if ok else "warn"),
            Found("answered a real message" if ok else "not tested yet",
                  "ok" if ok else "warn")]


def _model_run(ctx) -> list:
    from .setup_quick import choose_endpoint
    prof = choose_endpoint()
    if prof is None:
        return []
    ctx["profile"], ctx["setup_name"] = prof, prof.name
    ctx["ctx_len"] = _context_len(prof) or _ask_context(prof)
    return [f"model: {prof.model} at {prof.base_url}"]


def _ask_context(prof) -> int | None:
    """The server didn't say the model's context size: ask once — type it, find
    it by testing, or use 128k. The answer is kept for this model until changed
    (/model ctx <n>), so a chat never guesses quietly."""
    from .ctx_probe import DEFAULT_CTX, find_by_testing, remember_measured
    from .profile_store import set_profile
    from .setup import _ask, _choose
    how = _choose(f"The server doesn't say how much text {prof.model} can take in at once "
                  "(its context size). What should Aura do?",
                  [("type it", "you enter the number, e.g. 262144"),
                   ("find it by testing", "Aura sends bigger and bigger prompts until the "
                                          "server refuses — a few minutes; on a paid API it "
                                          "costs prompt tokens"),
                   ("use 128k", f"{DEFAULT_CTX:,} tokens, kept as your choice")], default_idx=0)
    n = None
    if how == "type it":
        raw = _ask("Context size (tokens)", str(DEFAULT_CTX)).strip()
        try:
            n = int(raw.replace(",", "").replace("_", "").replace(" ", ""))
        except ValueError:
            print(f"  (not a number — using {DEFAULT_CTX:,})")
    elif how == "find it by testing":
        from .machine import ok_to_run
        if ok_to_run("finding the context size", prof.base_url):
            found = find_by_testing(prof.base_url, prof.model, getattr(prof, "api_key", None))
            if found:
                remember_measured(prof.base_url, prof.model, found)
                print(f"  context size: {found:,} tokens (found by testing)")
                return found
        print("  not found — you can set it any time: /model ctx <tokens>")
    n = n or DEFAULT_CTX
    try:
        set_profile(prof.name, context_length=int(n))
        prof.context_length = int(n)
    except Exception:                                # noqa: BLE001
        pass
    print(f"  context size: {n:,} tokens (your choice — change it: /model ctx <tokens>)")
    return n


def _model_check(ctx) -> Proof:
    p = ctx.get("profile")
    if p is None:
        return Proof(False, "no model chosen", "a model", "aura setup model")
    from .first_run import live_completion_test, was_verified, mark_verified
    if was_verified(ctx.get("setup_name", ""), p):
        said = f"{p.model} answered a real message (checked when it was set up)"
    else:
        ok, why = live_completion_test(p)
        if not ok:
            return Proof(False, f"{p.model} didn't answer: {why}", "a model that answers",
                         "aura setup model")
        mark_verified(ctx.get("setup_name", ""), p)
        said = f"{p.model} answered a real message just now"
    # a local server: the window it really runs with, and a real tool call
    found, problems = local_checks(p)
    if found:
        said += " · " + " · ".join(f.text for f in found)
    if problems:
        return Proof(False, said, "; ".join(w for w, _ in problems),
                     "\n".join(f for _, f in problems))
    return Proof(True, said)


# ---------------------------------------------------------- 2 asking --
def _asking_asks(ctx) -> list:
    sb = _sandbox_status()
    lvl = Ask("assist.level", "How much should Aura ask before acting?",
              [("L1", "before every change — you see each step"),
               ("L2", "only before risky steps: what can't be undone, or reaches "
                      "outside the project"),
               ("L3", "never — it works alone" + ("" if sb["contained"] else
                      "  (no sandbox works on this machine — commands would run "
                      "with your own rights)"))],
              default=str(_opt("assist.level")),
              why="L2 lets it work without stopping you for every edit, and still "
                  "asks before anything that can't be taken back")
    start = Ask("assist.start", "The default for every new chat — act or plan? "
                "(one chat: /mode act|plan)",
                [("act", "Aura works on what you ask"),
                 ("plan", "it looks and plans first, and changes nothing until you "
                          "switch with /mode")],
                default=str(_opt("assist.start")),
                why="act is quicker; plan suits a project you don't want touched yet")
    return [lvl, start]


def _asking_save(ctx, ans) -> list:
    out = []
    for k in ("assist.level", "assist.start"):
        if k in ans:
            line = _set(k, ans[k])
            if line:
                out.append(line)
    return out


def _asking_check(ctx) -> Proof:
    lvl, start = _opt("assist.level"), _opt("assist.start")
    if lvl == "L3" and not _sandbox_status()["contained"]:
        return Proof(False, f"new chats start at {lvl}, in {start} mode — but no sandbox "
                     "works here, so nothing stands between a bad command and your files",
                     "a working sandbox (or L2)", "aura setup safety")
    return Proof(True, f"new chats start at {lvl}, in {start} mode (read back from "
                 "your profile's settings — a project's own files can't change it)")


# ---------------------------------------------------------- 3 safety --
_SANDBOX: dict = {}


def _sandbox_status(refresh: bool = False) -> dict:
    """Run a real command in the sandbox: it must write inside the project
    folder and must NOT write outside it. Cached for one setup run."""
    if _SANDBOX and not refresh:
        return _SANDBOX
    from .paths import aura_home
    from .sandbox import run_sandboxed
    root = Path(tempfile.mkdtemp(prefix="aura-sbx-"))
    away = aura_home() / "tmp" / f"sbx-{uuid.uuid4().hex[:8]}"
    away.parent.mkdir(parents=True, exist_ok=True)
    inside = root / "inside.txt"
    note = ""
    try:
        for target in (inside, away):
            proc, note = run_sandboxed(f'echo x > "{target}"', root, cwd=str(root),
                                       timeout=30)
        res = {"method": note, "wrote_inside": inside.exists(),
               "escaped": away.exists()}
    except Exception as e:                           # noqa: BLE001
        res = {"method": f"the check couldn't run: {e}", "wrote_inside": False,
               "escaped": None}
    finally:
        try:
            away.unlink()
        except OSError:
            pass
        import shutil
        shutil.rmtree(root, ignore_errors=True)
    res["contained"] = bool(res["wrote_inside"] and res["escaped"] is False)
    _SANDBOX.clear()
    _SANDBOX.update(res)
    return res


def _apparmor_blocks_bwrap() -> bool:
    """Ubuntu 24.04+: AppArmor stops bubblewrap making its namespaces."""
    try:
        from .sandbox import bwrap_available
        flag = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        return bwrap_available() and flag.read_text().strip() == "1" \
            and not Path("/etc/apparmor.d/bwrap").exists()
    except OSError:
        return False


BWRAP_FIX = """sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
EOF
sudo apparmor_parser -r /etc/apparmor.d/bwrap"""


def _safety_look(ctx) -> list:
    from .sandbox_os import describe
    sb = _sandbox_status()
    out = [Found("on this machine: " + describe(), "info")]
    if sb["contained"]:
        out.append(Found("a test command could write in the project and could NOT "
                         "write outside it", "ok"))
    elif sb["escaped"]:
        out.append(Found("a test command could write OUTSIDE the project — no file "
                         "boundary here", "warn"))
    else:
        out.append(Found(f"the sandbox test didn't work: {sb['method']}", "warn"))
    if _apparmor_blocks_bwrap():
        out.append(Found("bubblewrap is blocked by Ubuntu's AppArmor setting "
                         "(Landlock is used instead). The narrow fix allows bwrap "
                         "only, not every program — shown at the end", "warn"))
    if ctx.get("nt") and not sb["contained"]:
        from . import sandbox_win as SW
        if SW.installed() and not SW.ready():
            out.append(Found("Microsoft's process container (MXC) is downloaded, but this "
                             "Windows can't run it yet — it needs Windows 11 24H2 or newer "
                             "with the August 2026 update", "warn"))
        elif SW.offered():
            out.append(Found("Microsoft's process container (MXC) can give commands a "
                             f"file/network boundary here, no admin needed — a "
                             f"{SW.PACKAGE_SIZE / 1e6:.0f} MB download, asked below", "info"))
        else:
            out.append(Found("this Windows has no file/network sandbox Aura can use without "
                             "admin rights; WSL2 or Docker Desktop gives one", "warn"))
    if sb["contained"]:
        out.append(Found("network for commands: off, except the sites you allow "
                         "(aura scope add <site>); a command that needs more asks first",
                         "info"))
    else:
        out.append(Found("network for commands: sent through Aura's filter for the "
                         "sites you allow — but with no sandbox a program can ignore "
                         "the filter, so it isn't enforced here", "warn"))
    return out


def _mxc_offer(ctx) -> bool:
    if not ctx.get("nt"):
        return False
    from . import sandbox_win as SW
    return SW.offered() and SW.installed() is None


def _safety_asks(ctx) -> list:
    sb = _sandbox_status()
    get = []
    if _mxc_offer(ctx):
        from . import sandbox_win as SW
        # downloads only on a yes; the default never downloads
        get = [Ask("sandbox.mxc", f"Download Microsoft's sandbox for commands (MXC "
                   f"{SW.VERSION}, {SW.PACKAGE_SIZE / 1e6:.0f} MB from nuget.org)?",
                   [("yes", "download it now — checked against nuget.org's published "
                            "SHA-512, kept in Aura's folder, nothing installed into Windows"),
                    ("no", "not now — commands run with your own rights; 'aura setup safety' asks again")],
                   default="yes",
                   why="commands then write only in the project and temp folders, and "
                       "reach only the sites you allow")]
    return get + [Ask("sandbox.required", "Should commands run only inside the sandbox?",
                [("on", "yes — a command that can't be sandboxed is refused"),
                 ("off", "no — it runs, and Aura says plainly it wasn't sandboxed")],
                default="on" if sb["contained"] else "off",
                why=("the sandbox works here, so nothing is lost" if sb["contained"]
                     else "no sandbox works here yet, so 'on' would refuse every command"
                     + (" — with MXC downloaded above, 'on' is safe to pick" if get else "")))]


def _safety_save(ctx, ans) -> list:
    out = []
    if str(ans.get("sandbox.mxc", "")).lower() in ("no", "n", "false"):
        ctx["_sandbox_declined"] = True
    if str(ans.get("sandbox.mxc", "")).lower() in ("yes", "y", "true"):
        from . import sandbox_win as SW
        try:
            out.append(SW.get())
            if not SW.ready():
                out.append("MXC is saved, but this Windows can't run it yet (it needs "
                           "Windows 11 24H2+ with the August 2026 update) — commands "
                           "keep running as before")
        except Exception as e:                       # noqa: BLE001
            out.append(f"MXC wasn't downloaded: {e}")
        _SANDBOX.clear()
    if "sandbox.required" in ans:
        line = _set("sandbox.required", ans["sandbox.required"] in ("on", "yes", "true", True))
        if line:
            out.append(line)
    return out


def _safety_check(ctx) -> Proof:
    sb = _sandbox_status(refresh=True)
    req = bool(_opt("sandbox.required"))
    if sb["contained"]:
        return Proof(True, f"{sb['method']}: wrote inside the project, couldn't write "
                     "outside it" + (" · commands must be sandboxed" if req else ""))
    fix = BWRAP_FIX if _apparmor_blocks_bwrap() else (
        ("run 'aura setup safety' and say yes to Microsoft's process container (MXC)"
         if _mxc_offer(ctx) else "use WSL2 or start Docker Desktop")
        if ctx.get("nt") else "install bubblewrap, "
        "or use a kernel with Landlock (Linux 5.13+)")
    if req:
        return Proof(False, "commands must be sandboxed, but no sandbox works here — "
                     "every command would be refused", "a working sandbox", fix)
    if ctx.get("_sandbox_declined"):
        return Proof(True, "commands run with your own rights — you chose no sandbox for now "
                     "(Aura says so on each command · add one later: aura setup safety)")
    return Proof(False, "commands run with your own rights (no sandbox works here; "
                 "Aura says so on each one)", "a sandbox", fix)


# ----------------------------------------------------------- 4 tools --
def _tool_cost() -> dict:
    """Tokens each group's descriptions take, and what a new chat sends."""
    from .tool_schemas import SCHEMAS
    from .toolsets import CORE, LARGE_CONTEXT, TOOLSETS, user_switches
    schemas = SCHEMAS if isinstance(SCHEMAS, dict) else {}

    def cost(names):
        return sum(len(json.dumps(schemas[n])) for n in names if n in schemas) // 4
    return {"core": cost(CORE), "groups": {t.name: (cost(t.tools), t) for t in TOOLSETS},
            "switches": user_switches(), "large": LARGE_CONTEXT}


def _start_tokens(ctx) -> int:
    c = _tool_cost()
    on, off, everything = c["switches"]
    total = c["core"]
    for name, (tok, t) in c["groups"].items():
        if everything or name in on or (name not in off and (
                t.default or (t.big_model and (ctx.get("ctx_len") or 0) >= c["large"]))):
            total += tok
    return total


def _tools_look(ctx) -> list:
    n = ctx.get("ctx_len")
    start = _start_tokens(ctx)
    out = [Found(f"a new chat sends about {start:,} tokens of tool descriptions"
                 + (f" — {start * 100 // n}% of this model's {n:,}-token window" if n
                    else " (this model's window isn't known yet)"),
                 "warn" if n and start * 100 // n > 25 else "info"),
           Found("other groups come when a task needs them (tools_find) and are put "
                 "away again at a summary", "info")]
    return out


def _tools_asks(ctx) -> list:
    c = _tool_cost()
    n = ctx.get("ctx_len")
    asks = [Ask("tools.mode", "Tool groups:",
                [("auto", "Aura sends what each task needs (suggested)"),
                 ("choose", "set each group on / auto / off yourself")],
                default="auto",
                why="every tool description is paid for on every call; past ~30–50 "
                    "tools models pick worse — so only what's needed is sent")]
    if ctx.get("_tools_choose"):
        for name, (tok, t) in sorted(c["groups"].items()):
            share = f", {tok * 100 / n:.1f}% of the window" if n else ""
            asks.append(Ask(f"tools.{name}", f"{name} — {t.about} (~{tok:,} tokens{share})",
                            [("auto", "when a task needs it"), ("on", "always"),
                             ("off", "never")], default=str(_opt(f"tools.{name}"))))
    return asks


def _tools_save(ctx, ans) -> list:
    out = []
    for k, v in ans.items():
        if k.startswith("tools.") and k != "tools.mode":
            line = _set(k, v)
            if line:
                out.append(line)
    return out


def _tools_check(ctx) -> Proof:
    n = ctx.get("ctx_len")
    start = _start_tokens(ctx)
    if not n:
        return Proof(True, f"about {start:,} tokens of tools at the start (the window "
                     "size is found on the first chat)")
    pct = start * 100 / n
    if pct > 25:
        return Proof(False, f"the tools take {pct:.0f}% of the window at the start",
                     "room for your work", "aura setup tools  (switch big groups to auto)")
    return Proof(True, f"the tools take {pct:.1f}% of {n:,} tokens at the start")


# ------------------------------------------------------------- 9 you --
def _you_asks(ctx) -> list:
    return [Ask("you.name", "What should Aura call you? (blank = skip)", [], default=""),
            Ask("you.style", "How do you like answers?",
                [("short", "short and to the point"),
                 ("detailed", "with the reasoning and details"),
                 ("skip", "don't say")], default="skip"),
            Ask("history.auto_recall", "When you mention an earlier chat, look it up?",
                [("on", "yes — searched on this computer, no model calls"),
                 ("off", "no")], default="on" if _opt("history.auto_recall") else "off",
                why="'we decided last time…' then finds what was decided")]


def _you_save(ctx, ans) -> list:
    out = []
    from .memory_store import user_file, write_memory
    from .paths import no_project_dir
    root = no_project_dir()

    def keep(text: str, old_start: str, said: str) -> bool:
        """Save the user's own answer (kept, not decayed like a guess);
        nothing if it's already there; an earlier different one replaced."""
        try:
            cur = user_file().read_text(encoding="utf-8")
        except OSError:
            cur = ""
        if text in cur:
            return False
        write_memory(root, "user", "[PROVEN] " + text, section="About you",
                     source="setup", quote=said,
                     replaces=old_start if old_start in cur else "")
        return True
    name = str(ans.get("you.name") or "").strip()
    if name and keep(f"The user's name is {name}", "The user's name is", name):
        out.append(f"USER.md: name {name}")
    style = ans.get("you.style")
    if style in ("short", "detailed"):
        text = ("Prefers short, to-the-point answers" if style == "short"
                else "Prefers detailed answers with the reasoning")
        if keep(text, "Prefers ", style):
            out.append(f"USER.md: {style} answers")
    if "history.auto_recall" in ans:
        line = _set("history.auto_recall", ans["history.auto_recall"] in ("on", True))
        if line:
            out.append(line)
    return out


def _you_check(ctx) -> Proof:
    from .memory_store import user_file
    p = user_file()
    has = p.exists() and "about you" in p.read_text(encoding="utf-8", errors="replace").lower()
    return Proof(True, ("USER.md has what you said" if has else "nothing about you saved "
                        "(that's fine)") + f" · earlier chats looked up: "
                 + ("on" if _opt("history.auto_recall") else "off"))


# ------------------------------------------------------------ team ----
# Aura Teams: the username is asked at install — one question. It is the
# person's name on the board's work and their git branch name.
def _team_asks(ctx) -> list:
    from .teams import load_config
    return [Ask("teams.username", "Your username for team work (blank = later)", [],
                default=load_config().get("username", ""),
                why="Aura Teams: your name on the team board's work and your git "
                    "branch name — letters, digits and dashes")]


def _team_save(ctx, ans) -> list:
    from . import teams
    name = str(ans.get("teams.username") or "").strip()
    if not name:
        return []
    try:
        name = teams.check_username(name)
    except ValueError as e:
        ctx["team.refused"] = f"'{name}' not saved: {e}"
        return [f"team username not saved: {e}"]
    cfg = teams.load_config()
    if cfg.get("username") == name:
        return []
    cfg["username"] = name
    teams.save_config(cfg)
    return [f"team username: {name} (your branch name)"]


def _team_check(ctx) -> Proof:
    from .teams import load_config
    cfg = load_config()
    name = cfg.get("username")
    if ctx.get("team.refused"):
        return Proof(False, f"team username: {name or 'none'}", missing=ctx["team.refused"],
                     fix="aura setup team")
    return Proof(True, (f"team username: {name}" if name else "no team username "
                        "(that's fine until you join a team)")
                 + (f" · hub: {cfg['hub_url']}" if cfg.get("hub_url") else ""))


# ------------------------------------------------------------ 11 look --
def terminal_background(timeout: float = 0.3) -> str | None:
    """Ask the terminal for its background colour (OSC 11) -> 'dark' /
    'light', or None. Terminals that don't support it never answer, so the
    wait is short; the old Windows console ignores it."""
    if os.name == "nt" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import re
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            sys.stdout.write("\033]11;?\033\\")
            sys.stdout.flush()
            buf = b""
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
                if not r:
                    break
                buf += os.read(fd, 64)
                if buf.endswith((b"\a", b"\033\\")):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        m = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf)
        if not m:
            return None
        r, g, b = (int(x[:2], 16) for x in m.groups())
        return "light" if (0.299 * r + 0.587 * g + 0.114 * b) > 128 else "dark"
    except Exception:                                # noqa: BLE001
        return None


def _look_look(ctx) -> list:
    bg = ctx.setdefault("_bg", terminal_background())
    return [Found(f"the terminal says its background is {bg}" if bg else
                  "the terminal didn't say whether it's dark or light", "info")]


def _look_asks(ctx) -> list:
    from .themes import available
    bg = ctx.get("_bg")
    names = available()
    sugg = bg if bg in names else str(_opt("look.theme"))
    return [Ask("look.theme", "Colour theme:", [(n, "") for n in names], default=sugg,
                why=("matches what the terminal reported" if bg else "your current one"))]


def _look_save(ctx, ans) -> list:
    if "look.theme" not in ans:
        return []
    line = _set("look.theme", ans["look.theme"])
    return [line] if line else []


def _look_check(ctx) -> Proof:
    from .themes import current
    return Proof(True, f"theme {current()} (read back)")


# ------------------------------------------------------------- 5 web --
# So no service is called free here, and where a card is needed it is said.
# Prices are the services' own, rounded; the site has the current ones.
SEARCH_NOTES = {
    "brave": "about $5 per 1,000 searches, $5 of credit a month (≈1,000 searches); "
             "a card is needed — brave.com/search/api",
    "tavily": "pay per search (credits) — tavily.com",
    "exa": "pay per search — exa.ai",
    "serper": "Google's results, about $1 per 1,000 searches — serper.dev",
    "firecrawl": "pay per search — firecrawl.dev",
    "marginalia": "a small, independent web index — marginalia.nu",
    "searxng": "your own SearXNG server: no cost, you run it — type its address",
    "openalex": "research papers; works without a key, a key raises the limit — openalex.org",
}


def _own_engines() -> list:
    from . import websearch as W
    return [e for e in W.ENGINES if e.category == W.OWN]


def installed_browsers() -> list[str]:
    """Chrome / Edge already on this machine (Playwright can drive them,
    so nothing has to be downloaded)."""
    import shutil
    found = []
    if os.name == "nt":
        roots = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)",
                                                 "LOCALAPPDATA")]
        for name, rel in (("Edge", r"Microsoft\Edge\Application\msedge.exe"),
                          ("Chrome", r"Google\Chrome\Application\chrome.exe")):
            if any(r and Path(r, rel).exists() for r in roots):
                found.append(name)
    elif sys.platform == "darwin":
        for name, app in (("Edge", "Microsoft Edge"), ("Chrome", "Google Chrome")):
            if Path(f"/Applications/{app}.app").exists():
                found.append(name)
    else:
        for name, exes in (("Edge", ("microsoft-edge", "microsoft-edge-stable")),
                           ("Chrome", ("google-chrome", "google-chrome-stable"))):
            if any(shutil.which(x) for x in exes):
                found.append(name)
    return found


def _have_playwright() -> bool:
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


def _web_look(ctx) -> list:
    from . import websearch as W
    own = [e.label for e in _own_engines() if e.available()]
    out = [Found("search services connected: " + (", ".join(own) if own else "none"),
                 "ok" if own else "info"),
           Found("open public sources (Wikipedia, arXiv, PubMed, Stack Overflow, "
                 "GitHub): " + {"on": "allowed", "off": "not used"}.get(
                     W.consent() or "", "not decided yet"), "info")]
    br = installed_browsers()
    out.append(Found("browsers on this machine: " + (", ".join(br) if br else "none found")
                     + (" · Aura's browser part is added" if _have_playwright() else
                        " · Aura's browser part isn't added yet (optional — for pages that "
                        "need JavaScript)"),
                     "ok" if br and _have_playwright() else "info"))
    return out


def _web_asks(ctx) -> list:
    from . import websearch as W
    own = _own_engines()
    have = [e.name for e in own if e.available()]
    asks = [Ask("web.service", "Connect a search service?",
                [("keep", "keep what's connected" + (f" ({', '.join(have)})" if have else
                                                     " (none)"))]
                + [(e.name, SEARCH_NOTES.get(e.name, e.label)) for e in own],
                default="keep",
                why="open sources answer many questions without one; a service is "
                    "better for general web search")]
    for e in own:
        if e.name == "searxng":
            asks.append(Ask("web.key", "Your SearXNG address (https://…):", [],
                            when="web.service=searxng"))
        else:
            asks.append(Ask("web.key", f"Paste your {e.label} key (hidden):", [],
                            secret=True, when=f"web.service={e.name}"))
    asks.append(Ask("web.open_sources", "Search the web with no account (DuckDuckGo, "
                    "Wikipedia, arXiv, PubMed, Stack Overflow, GitHub)?",
                    [("on", "yes — the search words go to those sites"),
                     ("off", "no — only services I connect")],
                    default=W.consent() or "on",
                    why="they need no key; what you search is sent to them"))
    br = installed_browsers()
    asks.append(Ask("browser.engine", "Which browser should Aura use for web pages?",
                    [("installed", "the one on this machine"
                      + (f" ({' / '.join(br)})" if br else " (none found)")
                      + ("" if _have_playwright() else " — driven by Aura's browser part, "
                         "added with one command shown at the end")),
                     ("download", "Playwright's own Chromium (a ~150 MB download, "
                                  "when you run the command shown)")],
                    default="installed" if br else "download",
                    why=("no browser download — it uses the one you have" if br else
                         "no Chrome or Edge was found")))
    return asks


def _web_save(ctx, ans) -> list:
    from . import websearch as W
    out = []
    svc = ans.get("web.service") or "keep"
    key = str(ans.get("web.key") or "").strip()
    if svc not in ("keep", "") and key:
        eng = next((e for e in _own_engines() if e.name == svc), None)
        if eng is not None and eng.needs:
            from .keystore import put
            put(eng.needs, key)
            out.append(f"{eng.needs}: saved to the key store")
            ctx["_web_test"] = svc
    if ans.get("web.open_sources") in ("on", "off"):
        if W.consent() != ans["web.open_sources"]:
            W.set_consent(ans["web.open_sources"])
            out.append(f"open public sources: {ans['web.open_sources']}")
    if ans.get("browser.engine") in ("installed", "download"):
        line = _set("browser.engine", ans["browser.engine"])
        if line:
            out.append(line)
    return out


def _browser_check() -> tuple[bool, str]:
    """Open a page in the chosen browser, headless — the real thing."""
    if not _have_playwright():
        return False, "Playwright isn't installed"
    try:
        from .tools.browser import PlaywrightEngine
        eng = PlaywrightEngine(headless=True)
        try:
            eng.page.goto("about:blank")
            used = getattr(eng, "channel", None) or "Playwright's Chromium"
        finally:
            eng.close()
        return True, f"opened a page in {used}"
    except Exception as e:                           # noqa: BLE001
        return False, f"couldn't open a browser: {str(e).splitlines()[0][:140]}"


def _web_check(ctx) -> Proof:
    from . import websearch as W
    parts, ok, missing, fix = [], True, [], []
    svc = ctx.pop("_web_test", None)
    if svc:
        eng = next(e for e in _own_engines() if e.name == svc)
        try:
            hits = eng.run("open source licence", 1)
            parts.append(f"{eng.label} answered a test search ({len(hits)} result"
                         f"{'' if len(hits) == 1 else 's'}; one search from your plan)")
        except Exception as e:                       # noqa: BLE001
            ok = False
            parts.append(f"{eng.label} refused the test search: {str(e)[:120]}")
            missing.append(f"a working {eng.label} key")
            fix.append(f"aura setup web  (key page: {SEARCH_NOTES.get(svc, '')})")
    else:
        own = [e.label for e in _own_engines() if e.available()]
        parts.append("search: " + (", ".join(own) if own else
                                   "open sources only" if W.consent() == "on" else
                                   "nothing connected"))
    if not _have_playwright():
        # optional: search and reading pages work without it
        from .lifecycle import add_extra_command
        parts.append("pages that need JavaScript: add Aura's browser part when you want it — "
                     + add_extra_command("web"))
        return Proof(ok, " · ".join(parts), "; ".join(missing), "\n".join(fix))
    b_ok, b_says = _browser_check()
    parts.append(b_says)
    if not b_ok:
        ok = False
        missing.append("a browser Aura can drive")
        fix.append("aura setup web" + ("" if installed_browsers() and
                                         _opt("browser.engine") == "installed"
                                         else "  (then: playwright install chromium)"))
    return Proof(ok, " · ".join(parts), "; ".join(missing), "\n".join(fix))


# ------------------------------------------------------------ 7 away --
def _gw_file() -> Path:
    from .paths import profile_path
    return profile_path("gateway.json")


def _gw_load() -> dict:
    try:
        return json.loads(_gw_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _away_look(ctx) -> list:
    from .gateway import _config
    cfg = _config(None)
    on = [c for c in ("telegram", "discord", "slack", "webhook") if cfg.get(c)]
    return [Found("messages go to: " + (", ".join(on) if on else
                                         "nowhere yet (the outbox file only)"),
                  "ok" if on else "info"),
            Found("used for: scheduled jobs' results, and approvals while you're away",
                  "info")]


def _away_asks(ctx) -> list:
    return [
        Ask("away.channel", "Where should Aura send messages when you're away?",
            [("keep", "keep what's set"), ("telegram", "a Telegram bot (made with @BotFather)"),
             ("discord", "a Discord channel webhook"),
             ("slack", "a Slack app's incoming webhook"),
             ("webhook", "any address that takes a JSON POST")],
            default="keep"),
        Ask("away.token", "Paste the bot token from @BotFather (hidden):", [],
            secret=True, when="away.channel=telegram"),
        Ask("away.ready", "Now send any message to your bot in Telegram, then press "
            "Enter (Aura reads the chat it came from):", [], when="away.channel=telegram"),
        Ask("away.url", "Paste the channel's webhook URL — Discord: channel settings → "
            "Integrations → Webhooks (hidden):", [], secret=True, when="away.channel=discord"),
        Ask("away.url", "Slack: make an app at api.slack.com/apps, turn on Incoming "
            "Webhooks, add one to a channel, paste its URL (hidden):", [], secret=True,
            when="away.channel=slack"),
        Ask("away.url", "The address to POST to (hidden):", [], secret=True,
            when="away.channel=webhook"),
    ]


def telegram_chat_id(token: str) -> tuple[str | None, str]:
    """The chat of the newest message the bot got (getUpdates)."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return None, ("the token was refused" if e.code in (401, 404) else
                      "the bot has a webhook set — remove it, or type the chat id"
                      if e.code == 409 else f"HTTP {e.code}")
    except Exception as e:                           # noqa: BLE001
        return None, str(e)[:100]
    for upd in reversed(data.get("result") or []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"]), chat.get("title") or chat.get("first_name") or ""
    return None, "no message has reached the bot yet"


def _away_save(ctx, ans) -> list:
    ch = ans.get("away.channel") or "keep"
    if ch == "keep":
        return []
    data = _gw_load()
    out = []
    if ch == "telegram":
        token = str(ans.get("away.token") or "").strip()
        if not token:
            return ["telegram: no token given — nothing changed"]
        chat, who = telegram_chat_id(token)
        data["telegram"] = {"token": token, **({"chat_id": chat} if chat else {})}
        out.append("telegram: token saved to the key store" +
                   (f", chat {who or chat}" if chat else f" — chat not found: {who}"))
    else:
        url = str(ans.get("away.url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return [f"{ch}: that isn't a web address — nothing changed"]
        data[ch] = {"url": url}
        out.append(f"{ch}: address saved to the key store")
    p = _gw_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    from .gateway import _config
    _config(None)                     # Seals tokens/URLs into the key store now
    ctx["_away_test"] = ch
    return out


def _away_check(ctx) -> Proof:
    from .gateway import _config, deliver_message
    ch = ctx.pop("_away_test", None)
    cfg = _config(None)
    chans = [ch] if ch else [c for c in ("telegram", "discord", "slack", "webhook")
                             if cfg.get(c)]
    if not chans:
        return Proof(True, "no channel set — results stay in the outbox file "
                     "(that's fine)")
    res = [(c, deliver_message("Aura: messages will arrive here (setup test).", c,
                               _force=True)) for c in chans]
    bad = [(c, r) for c, r in res if not r.get("ok")]
    if bad:
        c, r = bad[0]
        return Proof(False, f"the test message to {c} wasn't delivered: "
                     f"{r.get('detail', '')[:140]}", f"a working {c} setting",
                     "aura setup away")
    return Proof(True, "a test message was delivered to " + ", ".join(chans)
                 + " — check it arrived")


# ------------------------------------------------------------ 10 code --
# the servers' own install commands (pyright: Microsoft's npm package;
# typescript-language-server's README; gopls: go.dev; rust-analyzer: rustup)
LSP_INSTALL = {
    "python": "npm install -g pyright   (or: pip install basedpyright)",
    "typescript": "npm install -g typescript-language-server typescript",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rust": "rustup component add rust-analyzer",
    "c": "clangd — clangd.llvm.org/installation",
}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
              ".aura", "target", ".tox"}


def project_languages(root: Path | None = None, limit: int = 4000) -> list[str]:
    """Languages with files in this folder (looked at quickly, not deep)."""
    from .lsp import language_of
    root = Path(root or os.getcwd())
    seen: dict[str, int] = {}
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            n += 1
            lang = language_of(f)
            if lang:
                seen[lang] = seen.get(lang, 0) + 1
            if n >= limit:
                break
        if n >= limit:
            break
    return sorted(seen, key=lambda k: -seen[k])


def _code_look(ctx) -> list:
    from .lsp import installed
    langs = ctx.setdefault("_langs", project_languages())
    have = installed()
    if not langs:
        return [Found("no code files in this folder — run `aura setup code` inside a "
                      "project to check its languages", "info")]
    return [Found(f"{lang}: " + (f"language server {Path(have[lang]).stem}" if have.get(lang)
                                 else "no language server installed"),
                  "ok" if have.get(lang) else "warn") for lang in langs]


def _code_asks(ctx) -> list:
    return [Ask("code.lsp", "After an edit, show the language server's errors to Aura?",
                [("on", "yes — mistakes are caught right after the edit"), ("off", "no")],
                default="on" if _opt("code.lsp") else "off",
                why="it catches a broken edit before the next step builds on it"),
            Ask("assist.suggest_checks", "Suggest extra checks (tests, a repro) when the "
                "work needs them?", [("on", "yes"), ("off", "no")],
                default="on" if _opt("assist.suggest_checks") else "off")]


def _code_save(ctx, ans) -> list:
    out = []
    for k in ("code.lsp", "assist.suggest_checks"):
        if k in ans:
            line = _set(k, ans[k] in ("on", True))
            if line:
                out.append(line)
    return out


def _code_check(ctx) -> Proof:
    from .lsp import installed
    if not _opt("code.lsp"):
        return Proof(True, "language-server checks are off")
    langs = ctx.get("_langs") or project_languages()
    have = installed()
    missing = [lang for lang in langs if not have.get(lang)]
    if not langs:
        return Proof(True, "no code here to check; servers found: "
                     + (", ".join(k for k, v in have.items() if v) or "none"))
    if missing:
        return Proof(False, "language servers found for: "
                     + (", ".join(lang for lang in langs if have.get(lang)) or "none"),
                     "a server for " + ", ".join(missing),
                     "\n".join(f"{lang}: {LSP_INSTALL.get(lang, '')}" for lang in missing))
    return Proof(True, "a language server is installed for every language here ("
                 + ", ".join(langs) + ")")


# ----------------------------------------------------- 6 connections --
def _mcp_path() -> Path:
    from .mcp_registry import config_file
    return config_file(None, False)


def _builtin_skills() -> int:
    d = Path(__file__).parent / "skills_builtin"
    return len([p for p in d.iterdir() if p.is_dir()]) if d.is_dir() else 0


def _conn_look(ctx) -> list:
    from .mcp_registry import POPULAR, listing
    have = listing(_mcp_path())
    out = [Found("MCP servers added: " + (", ".join(have) if have else "none"),
                 "ok" if have else "info"),
           Found(f"{_builtin_skills()} built-in skills (Aura's own, matched to each task)",
                 "ok")]
    out += [Found(f"{short} — {p['what']} (by {p['by']}; needs {p['account']})", "info")
            for short, p in POPULAR.items() if short not in have]
    out.append(Found("skills from elsewhere aren't added by setup (2026's skill-store "
                     "attacks hid harm in install steps) — review one, then "
                     "aura skills add --local <folder>", "info"))
    return out


def _conn_asks(ctx) -> list:
    return [Ask("mcp.add", "Add servers from the list above? Type their short names, "
                "separated by spaces (blank = none):", [], default="")]


def _conn_save(ctx, ans) -> list:
    from . import mcp_registry as R
    names = [n for n in str(ans.get("mcp.add") or "").replace(",", " ").split() if n]
    out, added = [], []
    have = R.listing(_mcp_path())
    for n in names:
        n = n.lower()
        if n in have:
            continue
        if n not in R.POPULAR:
            out.append(f"{n}: not on the well-known list — aura mcp add {n} shows "
                       "what it runs and asks")
            continue
        try:
            h = R.exact(n)
        except Exception as e:                       # noqa: BLE001
            out.append(f"{n}: the registry couldn't be reached ({str(e)[:80]})")
            continue
        if not h or not h.get("spec"):
            out.append(f"{n}: no entry Aura can start")
            continue
        R.add(n, h["spec"], _mcp_path())
        added.append(n)
        out.append(f"{n}: added ({h['spec'].get('url') or h['spec'].get('command')})")
    ctx["_mcp_added"] = added
    return out


def _conn_check(ctx) -> Proof:
    from . import mcp_registry as R
    servers = R.listing(_mcp_path())
    if not servers:
        return Proof(True, f"no MCP servers; {_builtin_skills()} built-in skills ready")
    ready, todo = [], []
    for name, sp in servers.items():
        if sp.get("url"):
            try:
                from .mcp_oauth import token_for
                signed = bool(token_for(sp["url"]))
            except Exception:                        # noqa: BLE001
                signed = False
            needs = (R.POPULAR.get(name) or {}).get("account", "")
            if signed or "sign-in" not in needs:
                ready.append(name)
            else:
                todo.append((name, f"aura mcp login {name}"))
            continue
        miss = R.runner_missing(sp)
        envs = [v[2:-1] for v in (sp.get("env") or {}).values()
                if isinstance(v, str) and v.startswith("${") and v.endswith("}")]
        from .keystore import get as kget
        nokey = [e for e in envs if not (os.environ.get(e) or kget(e))]
        if miss:
            todo.append((name, f"install {miss}"))
        elif nokey:
            todo.append((name, "aura keys set " + nokey[0]))
        else:
            ready.append(name)
    ev = (("ready: " + ", ".join(ready)) if ready else "") + (
        (" · " if ready else "") + "need a step: " + ", ".join(n for n, _ in todo)
        if todo else "")
    ev += " (a server's code is fetched the first time a chat starts it)"
    if todo:
        return Proof(False, ev, ", ".join(n for n, _ in todo),
                     "\n".join(f"{n}: {f}" for n, f in todo))
    return Proof(True, ev)


# ----------------------------------------------------- 8 background --
# Containers and WSL without systemd get the plain way (a terminal, or crontab
# + `cron run`).
RUN_ARGV = ["-m", "aura_agent", "cron", "watch"]


def _runner_env() -> dict:
    from .paths import aura_home, profile_name
    src = str(Path(__file__).resolve().parent.parent)
    return {"AURA_HOME": str(aura_home()), "AURA_PROFILE": profile_name(),
            "PYTHONPATH": src}


def runner_kind() -> str:
    import shutil
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if shutil.which("systemctl") and (os.environ.get("XDG_RUNTIME_DIR")
                                      or Path("/run/systemd/system").exists()):
        return "systemd"
    return "none"


def runner_file(kind: str | None = None) -> Path | None:
    from .paths import profile_name
    kind = kind or runner_kind()
    tag = profile_name()
    if kind == "systemd":
        return Path.home() / ".config" / "systemd" / "user" / f"aura-cron-{tag}.service"
    if kind == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"ai.aura.cron.{tag}.plist"
    if kind == "windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / \
            "Start Menu" / "Programs" / "Startup" / f"aura-cron-{tag}.cmd"
    return None


def runner_text(kind: str) -> str:
    env = _runner_env()
    py = sys.executable
    if kind == "systemd":
        envs = " ".join(f'"{k}={v}"' for k, v in env.items())
        return ("[Unit]\nDescription=Aura scheduled jobs (aura cron watch)\n\n"
                f"[Service]\nExecStart={py} {' '.join(RUN_ARGV)}\nEnvironment={envs}\n"
                "Restart=on-failure\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n")
    if kind == "macos":
        from xml.sax.saxutils import escape as x
        args = "".join(f"<string>{x(a)}</string>" for a in [py, *RUN_ARGV])
        envs = "".join(f"<key>{x(k)}</key><string>{x(v)}</string>" for k, v in env.items())
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC '
                '"-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict><key>Label</key><string>{x(runner_file(kind).stem)}'
                f'</string><key>ProgramArguments</key><array>{args}</array>'
                f'<key>EnvironmentVariables</key><dict>{envs}</dict>'
                '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n')
    if kind == "windows":
        pyw = Path(py).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else py)
        sets = "".join(f'set "{k}={v}"\r\n' for k, v in env.items())
        return f'@echo off\r\n{sets}start "" /min "{exe}" {" ".join(RUN_ARGV)}\r\n'
    return ""


def start_commands(kind: str) -> list[list[str]]:
    f = runner_file(kind)
    if kind == "systemd":
        return [["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", f.name]]
    if kind == "macos":
        return [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(f)]]
    return []


def stop_commands(kind: str) -> list[list[str]]:
    f = runner_file(kind)
    if kind == "systemd":
        return [["systemctl", "--user", "disable", "--now", f.name]]
    if kind == "macos":
        return [["launchctl", "bootout", f"gui/{os.getuid()}", str(f)]]
    return []


def _exec(argv: list[str]):
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


EXEC = _exec            # Tests swap this; nothing else runs OS commands here


def _bg_look(ctx) -> list:
    from .cronops import cron_notebooks, load_jobs, watcher_alive
    jobs = sum(len(load_jobs(nb)) for nb in cron_notebooks())
    kind = runner_kind()
    f = runner_file(kind)
    alive = watcher_alive()
    out = [Found(f"scheduled jobs: {jobs}", "info"),
           Found("a runner is running (it checked in "
                 f"{int(time.time() - alive['at'])} s ago)" if alive else
                 "nothing is running them now — scheduled jobs only run while "
                 "`aura cron watch` runs", "ok" if alive else ("warn" if jobs else "info"))]
    if kind == "none":
        out.append(Found("this system has no per-user service manager (a container, "
                         "or WSL without systemd)", "info"))
    elif f and f.exists():
        out.append(Found(f"runner installed: {f}", "ok"))
    return out


def _bg_save(ctx, ans) -> list:
    v = ans.get("background.runner")
    kind = runner_kind()
    f = runner_file(kind)
    if v not in ("on", "off") or f is None:
        return []
    out = []
    if v == "off":
        if not f.exists():
            return []
        for argv in stop_commands(kind):
            EXEC(argv)
        f.unlink()
        return [f"runner removed: {f}"]
    f.parent.mkdir(parents=True, exist_ok=True)
    text = runner_text(kind)
    if not f.exists() or f.read_text(encoding="utf-8") != text:
        f.write_text(text, encoding="utf-8", newline="")
        out.append(f"runner written: {f}")
    if kind == "windows":
        import subprocess
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0)
        if EXEC is _exec:
            subprocess.Popen(["cmd", "/c", str(f)], creationflags=flags,
                             close_fds=True)
        else:
            EXEC(["cmd", "/c", str(f)])
        out.append("runner started")
    for argv in start_commands(kind):
        r = EXEC(argv)
        if getattr(r, "returncode", 0) != 0:
            out.append(f"{' '.join(argv[:3])} said: {(r.stderr or r.stdout or '').strip()[:120]}")
    ctx["_bg_started"] = True
    return out


def _bg_check(ctx) -> Proof:
    from .cronops import watcher_alive
    kind = runner_kind()
    f = runner_file(kind)
    if ctx.pop("_bg_started", False):
        for _ in range(20):                          # The runner checks in at start
            if watcher_alive(30):
                break
            time.sleep(0.5)
    alive = watcher_alive()
    if alive:
        tail = (" · after logout too: loginctl enable-linger" if kind == "systemd" else "")
        return Proof(True, f"a runner is running (process {alive.get('pid')}, checked in "
                     f"{int(time.time() - alive['at'])} s ago){tail}")
    if f and f.exists():
        return Proof(False, f"the runner is installed ({f.name}) but hasn't checked in",
                     "a running runner", "aura cron watch  (run it once to see the error)")
    return Proof(True, "no background runner — scheduled jobs run while `aura cron watch` "
                 "runs" + (" (or add `aura cron run` to crontab)" if kind == "none" else ""))


# ---------------------------------------------------------- 0 import --
# Never imported: skills (outside code — 2026's skill-store attacks), personas
# (Aura has none).
_KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
              "MISTRAL_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
              "BRAVE_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY", "SERPER_API_KEY",
              "FIRECRAWL_API_KEY", "GITHUB_PERSONAL_ACCESS_TOKEN", "SENTRY_ACCESS_TOKEN",
              "SUPABASE_ACCESS_TOKEN")


_AGENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "hermes": "Hermes",
                "openclaw": "OpenClaw"}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _json5(text: str):
    """JSON, forgiving the comments and trailing commas JSON5 allows."""
    import re
    try:
        return json.loads(text)
    except ValueError:
        pass
    out, i, n, quote = [], 0, len(text), None
    while i < n:                          # Drop // and /* */ outside strings
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    t = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(t)
    except ValueError:
        return {}


def _env_file(p: Path) -> dict:
    out = {}
    for line in _read(p).splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in _KEY_NAMES and v:
                out[k] = v
    return out


def _mcp_entry(sp: dict) -> dict | None:
    """An agent's server entry in Aura's form. A remote server's
    `Authorization: Bearer …` header becomes Aura's token (moved to the key
    store on import); other headers are listed in "_dropped" (Aura sends
    only a bearer token) — never silently lost."""
    if not isinstance(sp, dict):
        return None
    if sp.get("url"):
        out: dict = {"url": str(sp["url"])}
        hdrs = sp.get("headers") or sp.get("http_headers") or {}
        dropped = []
        for k, v in (hdrs.items() if isinstance(hdrs, dict) else []):
            if str(k).lower() == "authorization" and str(v).lower().startswith("bearer "):
                out["token"] = str(v)[7:].strip()
            else:
                dropped.append(str(k))
        if sp.get("bearer_token_env_var"):
            out["token"] = "${" + str(sp["bearer_token_env_var"]) + "}"
        if dropped:
            out["_dropped"] = dropped
        return out
    if sp.get("command"):
        return {"command": str(sp["command"]), "args": [str(a) for a in sp.get("args") or []],
                **({"env": {str(k): str(v) for k, v in (sp.get("env") or {}).items()}}
                   if sp.get("env") else {})}
    return None


def find_agents(home: Path | None = None, env: dict | None = None) -> dict:
    """What each other agent on this machine has that Aura can bring over:
    {agent: {"where", "instructions": [(file, text)], "memory": [(file, text)],
             "mcp": {name: spec}, "project_mcp": {folder: {name: spec}},
             "keys": {NAME: value}, "not": [what is skipped]}}
    env: where CLAUDE_CONFIG_DIR / CODEX_HOME come from (the real
    environment unless a home folder is given, e.g. in tests)."""
    h = Path(home or Path.home())
    out: dict = {}
    if env is None:
        env = os.environ if home is None else {}
    cdir = Path(env.get("CLAUDE_CONFIG_DIR") or (h / ".claude"))
    cl = {"where": str(cdir), "instructions": [], "memory": [], "mcp": {},
          "project_mcp": {}, "keys": {}, "not": []}
    if (cdir / "CLAUDE.md").exists():
        cl["instructions"].append((str(cdir / "CLAUDE.md"), _read(cdir / "CLAUDE.md")))
    cfiles = list(dict.fromkeys(p for p in (cdir / ".claude.json", h / ".claude.json",
                                            h / ".claude" / ".claude.json") if p.exists()))
    for cf in cfiles:
        cj = _json5(_read(cf)) or {}
        for n, sp in (cj.get("mcpServers") or {}).items():
            e = _mcp_entry(sp)
            if e and n not in cl["mcp"]:
                cl["mcp"][n] = e
        for folder, pc in (cj.get("projects") or {}).items():
            for n, sp in ((pc or {}).get("mcpServers") or {}).items() if isinstance(pc, dict) else ():
                e = _mcp_entry(sp)
                if e:
                    cl["project_mcp"].setdefault(folder, {}).setdefault(n, e)
    if cl["instructions"] or cl["mcp"] or cl["project_mcp"] or cdir.is_dir() or cfiles:
        out["claude-code"] = cl
    xdir = Path(env.get("CODEX_HOME") or (h / ".codex"))
    cx = {"where": str(xdir), "instructions": [], "memory": [], "mcp": {},
          "project_mcp": {}, "keys": {}, "not": []}
    for name in ("AGENTS.override.md", "AGENTS.md"):
        if (xdir / name).exists():
            cx["instructions"].append((str(xdir / name), _read(xdir / name)))
            break
    if (xdir / "config.toml").exists():
        try:
            import tomllib
            conf = tomllib.loads(_read(xdir / "config.toml"))
        except Exception:                            # noqa: BLE001
            conf = {}
        for n, sp in (conf.get("mcp_servers") or {}).items():
            e = _mcp_entry(sp)
            if e:
                cx["mcp"][n] = e
    if cx["instructions"] or cx["mcp"] or xdir.is_dir():
        out["codex"] = cx
    hd = h / ".hermes"
    if hd.is_dir():
        he = {"where": str(hd), "instructions": [], "memory": [], "mcp": {}, "keys": {},
              "not": []}
        if (hd / "memories" / "USER.md").exists():
            he["memory"].append(("~/.hermes/memories/USER.md", _read(hd / "memories" / "USER.md")))
        he["keys"] = _env_file(hd / ".env")
        if "mcp_servers" in _read(hd / "config.yaml"):
            he["not"].append("MCP servers in config.yaml (YAML — add them with aura mcp add)")
        if (hd / "skills").is_dir():
            he["not"].append("skills (outside code — review one, then aura skills add --local)")
        if (hd / "SOUL.md").exists():
            he["not"].append("SOUL.md (a persona — Aura has none)")
        he["unchecked"] = True        # Built from its docs; no real install seen yet
        out["hermes"] = he
    od = h / ".openclaw"
    if od.is_dir():
        oc = {"where": str(od), "instructions": [], "memory": [], "mcp": {}, "keys": {},
              "not": []}
        conf = _json5(_read(od / "openclaw.json"))
        ws = Path(((conf.get("agents") or {}).get("defaults") or {}).get("workspace")
                  or (conf.get("agent") or {}).get("workspace") or (od / "workspace"))
        ws = Path(os.path.expanduser(str(ws)))
        if (ws / "AGENTS.md").exists():
            oc["instructions"].append(("OpenClaw AGENTS.md", _read(ws / "AGENTS.md")))
        if (ws / "USER.md").exists():
            oc["memory"].append(("OpenClaw USER.md", _read(ws / "USER.md")))
        for n, sp in (((conf.get("mcp") or {}).get("servers")) or {}).items():
            e = _mcp_entry(sp)
            if e:
                oc["mcp"][n] = e
        oc["keys"] = _env_file(od / ".env")
        if (ws / "skills").is_dir() or (od / "skills").is_dir():
            oc["not"].append("skills (outside code — review one, then aura skills add --local)")
        if (ws / "SOUL.md").exists():
            oc["not"].append("SOUL.md (a persona — Aura has none)")
        oc["unchecked"] = True
        out["openclaw"] = oc
    return out


def _lines(text: str, limit: int = 60) -> list[str]:
    """The instruction/fact lines of a markdown file (headings, blank and
    code lines left out), at most `limit`."""
    out, code = [], False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            code = not code
            continue
        if code or not s or s.startswith(("#", "<!--", "---")):
            continue
        s = s.lstrip("-*• ").strip()
        if len(s) > 3:
            out.append(s[:300])
        if len(out) >= limit:
            break
    return out


def _import_look(ctx) -> list:
    found = ctx.setdefault("_agents", find_agents())
    if not found:
        return [Found("no other agent's settings found (Claude Code, Codex, Hermes, "
                      "OpenClaw)", "info")]
    out = []
    for agent, d in found.items():
        bits = []
        if d["instructions"]:
            bits.append(f"instructions ({sum(len(_lines(t)) for _, t in d['instructions'])} lines)")
        if d["memory"]:
            bits.append(f"about you ({sum(len(_lines(t)) for _, t in d['memory'])} lines)")
        if d["mcp"]:
            bits.append("MCP servers: " + ", ".join(d["mcp"]))
        pm = d.get("project_mcp") or {}
        if pm:
            bits.append(f"MCP servers in {len(pm)} project(s): "
                        + ", ".join(sorted({n for v in pm.values() for n in v})))
        if d["keys"]:
            bits.append("keys: " + ", ".join(d["keys"]))           # Names only
        note = "  (read from its docs — not yet checked on a real install)" if d.get("unchecked") else ""
        out.append(Found(f"{agent} ({d['where']}): " + ("; ".join(bits) or "nothing to bring") + note,
                         "info"))
        for n in d["not"]:
            out.append(Found(f"{agent}: not brought over — {n}", "off"))
        for n, sp in list(d["mcp"].items()) + [(n, sp) for v in pm.values() for n, sp in v.items()]:
            for hdr in sp.get("_dropped") or []:
                out.append(Found(f"{agent}: server {n}'s header {hdr} isn't brought over "
                                 "(Aura sends only a bearer token)", "off"))
    return out


def _import_asks(ctx) -> list:
    found = ctx.get("_agents") or find_agents()
    asks = []
    for agent, d in found.items():
        if d["instructions"] or d["memory"]:
            asks.append(Ask(f"import.{agent}.text", f"{agent}: bring its instructions and "
                            "what it knows about you into Aura's USER.md?",
                            [("on", "yes"), ("off", "no")], default="on",
                            why="they're yours; lines written for that agent's own tools "
                                "may not fit Aura — look at USER.md afterwards"))
        if d["mcp"] or d.get("project_mcp"):
            names = list(d["mcp"]) + sorted({n for v in (d.get("project_mcp") or {}).values() for n in v})
            asks.append(Ask(f"import.{agent}.mcp", f"{agent}: add its MCP servers "
                            f"({', '.join(names)})?", [("on", "yes"), ("off", "no")],
                            default="on", why="the same servers, so the same tools"))
        if d["keys"]:
            asks.append(Ask(f"import.{agent}.keys", f"{agent}: copy its keys "
                            f"({', '.join(d['keys'])}) into Aura's key store?",
                            [("off", "no"), ("on", "yes")], default="off",
                            why="only if you say so — a key is copied, never shown"))
    return asks


def _sealed(name: str, sp: dict) -> dict:
    """The server entry with every literal secret moved to the key store
    and referred to as ${NAME} (Aura passes it in, never as text)."""
    import re as _re
    from .keystore import put
    sp = {k: v for k, v in sp.items() if k != "_dropped"}
    env = dict(sp.get("env") or {})
    for k, v in list(env.items()):
        if not (v.startswith("${") and v.endswith("}")):
            put(k, v)
            env[k] = "${" + k + "}"
    if env:
        sp["env"] = env
    tok = sp.get("token")
    if tok and not (tok.startswith("${") and tok.endswith("}")):
        key = "MCP_" + _re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_TOKEN"
        put(key, tok)
        sp["token"] = "${" + key + "}"
    return sp


def _project_mcp_file(folder: str) -> Path:
    """Aura's MCP list for that project folder (its notebook)."""
    from .cli import engagement_dir
    from .mcp_registry import config_file
    return config_file(engagement_dir(folder), True)


def _backup_before_import() -> Path:
    import zipfile
    from .memory_store import user_file
    from .paths import profile_dir
    dest = profile_dir() / "backups" / f"pre-import-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as z:
        for p in (user_file(), _mcp_path()):
            if p.exists():
                z.write(p, p.name)
    return dest


def _import_save(ctx, ans) -> list:
    from . import mcp_registry as R
    from .keystore import put
    found = ctx.get("_agents") or find_agents()
    picked = [(a, d) for a, d in found.items()
              if any(ans.get(f"import.{a}.{w}") == "on" for w in ("text", "mcp", "keys"))]
    if not picked:
        return []
    out = [f"backup first: {_backup_before_import()}"]
    from .memory_store import user_file, write_memory
    from .paths import no_project_dir
    for agent, d in picked:
        if ans.get(f"import.{agent}.text") == "on":
            cur = _read(user_file())
            n = 0
            for label, text in d["instructions"] + d["memory"]:
                for line in _lines(text):
                    if line in cur:
                        continue
                    write_memory(no_project_dir(), "user", "[PROVEN] " + line,
                                 section="From " + _AGENT_NAMES.get(agent, agent),
                                 source=f"import:{label}",
                                 quote=line)
                    n += 1
            out.append(f"{agent}: {n} line(s) into USER.md")
        if ans.get(f"import.{agent}.mcp") == "on":
            have = R.listing(_mcp_path())
            for name, sp in d["mcp"].items():
                if name not in have:
                    R.add(name, _sealed(name, sp), _mcp_path())
                    out.append(f"{agent}: MCP server {name} added")
            # servers that agent keeps per project go to the same project in Aura
            for folder, servers in (d.get("project_mcp") or {}).items():
                if not Path(folder).is_dir():
                    out.append(f"{agent}: {folder} no longer exists — its servers skipped")
                    continue
                pf = _project_mcp_file(folder)
                have_p = R.listing(pf)
                for name, sp in servers.items():
                    if name not in have_p:
                        R.add(name, _sealed(name, sp), pf)
                        out.append(f"{agent}: MCP server {name} added for {folder}")
        if ans.get(f"import.{agent}.keys") == "on":
            for k, v in d["keys"].items():
                put(k, v)
            out.append(f"{agent}: keys copied — " + ", ".join(d["keys"]))
    ctx["_imported"] = [(a, d, {w for w in ("text", "mcp", "keys")
                                 if ans.get(f"import.{a}.{w}") == "on"}) for a, d in picked]
    return out


def _import_check(ctx) -> Proof:
    """Read back everything that was brought over."""
    from . import mcp_registry as R
    from .keystore import get as kget
    from .memory_store import user_file
    picked = ctx.pop("_imported", None)
    if not picked:
        return Proof(True, "nothing brought over")
    have = R.listing(_mcp_path())
    usr = _read(user_file())
    bad, said = [], []
    for agent, d, parts in picked:
        got = []
        if "text" in parts:
            lines = [ln for _, t in d["instructions"] + d["memory"] for ln in _lines(t)]
            gone = [ln for ln in lines if ln not in usr]
            bad += [f"{agent}: {len(gone)} USER.md line(s)"] if gone else []
            got.append(f"{len(lines) - len(gone)} line(s) in USER.md")
        if "mcp" in parts:
            gone = [n for n in d["mcp"] if n not in have]
            for folder, servers in (d.get("project_mcp") or {}).items():
                if Path(folder).is_dir():
                    hp = R.listing(_project_mcp_file(folder))
                    gone += [f"{n} ({folder})" for n in servers if n not in hp]
            bad += [f"{agent}: MCP {n}" for n in gone]
            total = len(d["mcp"]) + sum(len(v) for f, v in (d.get("project_mcp") or {}).items()
                                        if Path(f).is_dir())
            got.append(f"{total - len(gone)} MCP server(s)")
        if "keys" in parts:
            gone = [k for k in d["keys"] if kget(k) != d["keys"][k]]
            bad += [f"{agent}: key {k}" for k in gone]
            got.append(f"{len(d['keys']) - len(gone)} key(s) in the key store")
        said.append(f"{agent}: " + ", ".join(got))
    if bad:
        return Proof(False, "some of it isn't there after the import: " + "; ".join(said),
                     ", ".join(bad), "aura setup import  (the backup is in profiles/…/backups)")
    return Proof(True, "read back — " + "; ".join(said))


# ------------------------------------------------------------ the list --
def _merged(sid: str, num: int, title: str, later: str, *parts) -> Step:
    """One step made of several small ones: their findings and questions
    in order, each saved, the checks combined (ready only if all are)."""
    def look(ctx):
        return [f for lk, _, _, _ in parts for f in lk(ctx)]

    def asks(ctx):
        return [a for _, ak, _, _ in parts for a in ak(ctx)]

    def save(ctx, ans):
        return [c for _, _, sv, _ in parts for c in sv(ctx, ans)]

    def check(ctx):
        proofs = [ck(ctx) for _, _, _, ck in parts]
        return Proof(all(p.ok for p in proofs), " · ".join(p.evidence for p in proofs),
                     "; ".join(p.missing for p in proofs if p.missing),
                     "\n".join(p.fix for p in proofs if p.fix))
    return Step(sid, num, title, False, later, look=look, asks=asks, save=save, check=check)


STEPS: list[Step] = [
    Step("import", 0, "Bring over from another agent", False, "aura setup import",
         look=_import_look, asks=_import_asks, save=_import_save, check=_import_check),
    Step("model", 1, "Model", True, "aura setup model", look=_model_look,
         run=_model_run, check=_model_check),
    Step("asking", 2, "How much Aura asks", True, "aura setup asking  ·  /assist in a chat",
         asks=_asking_asks, save=_asking_save, check=_asking_check),
    Step("safety", 3, "Safety — where commands run", False, "aura setup safety",
         look=_safety_look, asks=_safety_asks, save=_safety_save, check=_safety_check),
    _merged("tools", 4, "Tools and code", "aura setup tools  ·  /tools in a chat",
            (_tools_look, _tools_asks, _tools_save, _tools_check),
            (_code_look, _code_asks, _code_save, _code_check)),
    Step("web", 5, "Web and browser", False, "aura setup web",
         look=_web_look, asks=_web_asks, save=_web_save, check=_web_check),
    Step("connect", 6, "Connections (MCP servers, skills)", False, "aura setup connect  ·  aura mcp",
         look=_conn_look, asks=_conn_asks, save=_conn_save, check=_conn_check),
    Step("away", 7, "Messages when you're away", False, "aura setup away  ·  aura gateway",
         look=_away_look, asks=_away_asks, save=_away_save, check=_away_check),
    _merged("you", 8, "You and the look", "aura setup you  ·  /theme in a chat",
            (lambda ctx: [], _you_asks, _you_save, _you_check),
            (_look_look, _look_asks, _look_save, _look_check)),
    Step("team", 9, "Team work", True, "aura setup team  ·  aura teams",
         asks=_team_asks, save=_team_save, check=_team_check),
]
from .editions import teams_present as _teams_present  # noqa: E402
if not _teams_present():                    # Shipped without Aura Teams
    STEPS = [s for s in STEPS if s.id != "team"]
BY_ID = {s.id: s for s in STEPS}
# the old step names still open the step they were folded into
ALIASES = {"code": "tools", "look": "you"}


# ------------------------------------------------------------ receipt --
def _receipt_path() -> Path:
    from .paths import profile_path
    return profile_path("setup_receipt.jsonl")


def _scrub(text: str) -> str:
    try:
        from .receipts import _scrub as scrub
        return scrub(text)
    except Exception:                                # noqa: BLE001
        return text


def record(step: Step, changes: list, proof: Proof) -> dict:
    """Add one line to the setup record: which step, what the check showed, what
    changed. A plain list.
    """
    p = _receipt_path()
    rec = {"at": round(time.time()), "step": step.id, "ok": proof.ok,
           "evidence": _scrub(proof.evidence), "missing": proof.missing,
           "changes": [_scrub(c) for c in changes]}
    try:
        from .sessions import locked_append
        locked_append(p, json.dumps(rec) + "\n")
    except Exception:                                # noqa: BLE001
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


def receipt_state() -> dict:
    """step id -> its latest line in the setup record."""
    out: dict = {}
    try:
        for line in _receipt_path().read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                out[rec["step"]] = rec
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return out


# ------------------------------------------------------------- answers --
def answers_from(file: str | None) -> dict:
    """Unattended answers: a JSON file {"assist.level": "L2", ...} and/or
    environment variables AURA_SETUP_<KEY> (dots as _, e.g.
    AURA_SETUP_ASSIST_LEVEL=L2). The environment wins."""
    ans: dict = {}
    if file:
        ans.update(json.loads(Path(file).read_text(encoding="utf-8")))
    for k, v in os.environ.items():
        if k.startswith("AURA_SETUP_") and k not in ("AURA_SETUP_LANE",):
            ans[k[len("AURA_SETUP_"):].lower().replace("_", ".", 1)] = v
    return ans


# ------------------------------------------------------- plain runner --
def _ask_plain(a: Ask, inp=input, say=print):
    if a.why:
        say(f"    suggested: {a.default} — {a.why}")
    if not a.choices:
        try:
            return inp(f"  {a.prompt} ").strip()
        except EOFError:
            return a.default
    say(f"  {a.prompt}")
    for i, (val, meaning) in enumerate(a.choices, 1):
        mark = "  (suggested)" if val == a.default else ""
        say(f"    {i}. {val}" + (f" — {meaning}" if meaning else "") + mark)
    for _ in range(3):
        try:
            raw = inp(f"  choose 1-{len(a.choices)} [{a.default}]: ").strip()
        except EOFError:
            raw = ""
        if not raw:
            return a.default
        if raw.isdigit() and 1 <= int(raw) <= len(a.choices):
            return a.choices[int(raw) - 1][0]
        if raw in [c[0] for c in a.choices]:
            return raw
        say(f"    '{raw}' isn't one of the choices")
    return a.default


def run(mode: str = "full", only: list | None = None, answers: dict | None = None,
        interactive: bool = True, dry_run: bool = False, inp=input, say=print) -> int:
    """Returns an exit code."""
    ctx = context()
    try:                                     # The machine, read once
        from .machine import profile
        profile(refresh=True)
    except Exception:                        # noqa: BLE001
        pass
    answers = dict(answers or {})
    steps = [s for s in STEPS if (only and s.id in only)
             or (not only and (mode == "full" or s.quick))]
    plan: list[tuple[Step, dict]] = []
    hidden: set = set()                  # Answers never printed or recorded
    for i, st in enumerate(steps, 1):
        say("")
        say(f"[{i}/{len(steps)}] {st.title}   (change it any time: {st.later})")
        for f in st.look(ctx):
            say(f"  {'✓' if f.state == 'ok' else '!' if f.state == 'warn' else '·'} {f.text}")
        if st.run is not None:
            if not interactive or dry_run:
                say("  (kept as it is — set it with: " + st.later + ")")
                continue
            st.run(ctx)
            plan.append((st, {}))
            continue
        ans: dict = {}
        asks = st.asks(ctx)
        k = 0
        while k < len(asks):
            a = asks[k]
            if a.when:                       # Only after the answer it follows
                wk, _, wv = a.when.partition("=")
                if str(ans.get(wk, "")) != wv:
                    k += 1
                    continue
            if a.secret:
                hidden.add(a.key)
            if a.key in answers:
                ans[a.key] = answers[a.key]
            elif interactive and a.secret:
                from .setup_quick import _secret
                ans[a.key] = _secret("  " + a.prompt.rstrip(": "))
            elif interactive:
                ans[a.key] = _ask_plain(a, inp, say)
            else:
                ans[a.key] = a.default
            if a.key == "tools.mode" and ans[a.key] == "choose":
                ctx["_tools_choose"] = True
                asks = st.asks(ctx)
            k += 1
        plan.append((st, ans))
    # the summary, then one confirm
    say("")
    say("what will be saved:")
    for st, ans in plan:
        shown = {k: ("(hidden)" if k in hidden else v) for k, v in ans.items()
                 if k not in ("tools.mode", "away.ready") and v not in ("", None, "skip")}
        say(f"  {st.title}: " + (", ".join(f"{k}={v}" for k, v in shown.items())
                                  if shown else "as it is"))
    if dry_run:
        say("")
        say("dry run — nothing was saved")
        return 0
    if interactive:
        try:
            go = inp("save these? [Y/n] ").strip().lower()
        except EOFError:
            go = ""
        if go in ("n", "no"):
            say("nothing saved.")
            return 1
    ready, total = 0, 0
    say("")
    for st, ans in plan:
        changes = st.save(ctx, ans) if st.run is None else []
        proof = st.check(ctx)
        record(st, changes, proof)
        total += 1
        ready += proof.ok
        say(f"  {'✓' if proof.ok else '!'} {st.title}: {proof.evidence}")
        if not proof.ok:
            if proof.missing:
                say(f"      missing: {proof.missing}")
            if proof.fix:
                for line in proof.fix.splitlines():
                    say(f"      fix: {line}" if line == proof.fix.splitlines()[0]
                        else f"           {line}")
        say(f"      change it later: {st.later}")
    say("")
    say(f"{ready}/{total} ready — each one checked on this machine "
        "(the setup receipt keeps what was shown: aura setup status)")
    return 0 if ready == total else 2


def status_lines() -> list[str]:
    """The board's state in plain lines (aura setup status)."""
    rec = receipt_state()
    out = []
    for st in STEPS:
        r = rec.get(st.id)
        if not r:
            out.append(f"  – {st.num:>2} {st.title}: not set up  ({st.later})")
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"]))
        out.append(f"  {'✓' if r['ok'] else '!'} {st.num:>2} {st.title}: {r['evidence']}"
                   f"  [{when}]")
    return out

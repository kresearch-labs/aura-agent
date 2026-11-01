"""model_facts.py — measure per-model choices once, on the user's own model.

Nobody measures on the user's own model. FFTW's "wisdom": measure once per
machine, save, reuse, redo when the machine changes.

So: small probes, run once per model with the user's OK (the cost is said
first), saved per model; Aura uses them on every run; the user's explicit
setting always wins; `aura eval model --again` re-measures.

  edit form — the same 6 small edits with line tags and with plain lines,
                through Aura's real edit tool on scratch files (one retry,
                as in real use); tags unless plain is clearly better.
  early check — 4 vague and 2 clear tasks, with and without a reminder to
                ask first; kept only if it clearly improves right asking.

Small samples, so a choice changes only on a clear win (>= 2 of 6).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

ACTIVE: str = ""             # The model key in use (set by build_agent / model switch)
CLEAR_WIN = 2

EDIT_CASES = [
    ("area.py", "def area(w, h):\n    return w + h\n",
     "The function should multiply w and h, not add them.",
     "def area(w, h):\n    return w * h\n"),
    ("config.js", "module.exports = {\n  host: 'localhost',\n  port: 3000,\n  debug: false,\n};\n",
     "Change the port to 8080.",
     "module.exports = {\n  host: 'localhost',\n  port: 8080,\n  debug: false,\n};\n"),
    ("greet.py", "def greet(name):\n    message = f'Hello, {name}'\n    return message\n",
     "Before returning, also print the message.",
     "def greet(name):\n    message = f'Hello, {name}'\n    print(message)\n    return message\n"),
    ("limits.py", "MAX_USERS = 10\nMAX_ITEMS = 10\nMAX_USERS_PER_TEAM = 10\n",
     "Set MAX_ITEMS to 50 (leave the other two as they are).",
     "MAX_USERS = 10\nMAX_ITEMS = 50\nMAX_USERS_PER_TEAM = 10\n"),
    ("app.yaml", "server:\n  name: api\n  replicas: 2\ndatabase:\n  name: main\n  replicas: 1\n",
     "Give the database 3 replicas (the server keeps 2).",
     "server:\n  name: api\n  replicas: 2\ndatabase:\n  name: main\n  replicas: 3\n"),
    ("README.md", "# Instalation\n\nRun `make`.\n",
     "Fix the spelling of the heading to Installation.",
     "# Installation\n\nRun `make`.\n"),
]

VAGUE = ["Export the data.", "Make the button bigger.", "Fix the bug.",
         "Send the report to the team."]
CLEAR = ["In math.py, add a one-line docstring to the function add saying it adds two numbers.",
         "In calc.py, rename the variable x to total on line 3."]
EARLY_CHECK = ("[CHECK FIRST] Before acting: if the request can be read more than one "
               "way and the choice matters, or only the user has the information, "
               "ask with the clarify tool first. Otherwise just do it.")


# ---------------------------------------------------------------- store --
def model_key(base_url: str, model: str) -> str:
    host = urllib.parse.urlsplit(base_url or "").netloc or base_url or "?"
    return f"{host}|{model}"


def _path() -> Path:
    from .paths import aura_home
    return aura_home() / "model_facts.json"


_CACHE: dict = {"mtime": None, "data": {}}


def load() -> dict:
    """The saved facts (cached; re-read when the file changes — the model
    client looks facts up on every call)."""
    try:
        p = _path()
        mt = p.stat().st_mtime
        if _CACHE["mtime"] != mt:
            _CACHE.update(mtime=mt, data=json.loads(p.read_text(encoding="utf-8")))
        return dict(_CACHE["data"])
    except Exception:                                # noqa: BLE001
        return {}


def save_facts(key: str, facts: dict) -> None:
    d = load()
    stamp = {"measured": round(time.time())} if any(k in ALL for k in facts) else {}
    d[key] = {**d.get(key, {}), **facts, **stamp}
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")
    _CACHE["mtime"] = None


PROBING = False       # True while checks run: the client applies no facts


def fact(name: str, key: str | None = None):
    """A measured choice / capability for the model (default: the active
    one), or None (not measured)."""
    if PROBING:
        return None
    rec = load().get(key or ACTIVE) or {}
    v = rec.get(name)
    if isinstance(v, dict):
        return v.get("choice", v.get("value"))
    caps = rec.get("caps") or {}
    c = caps.get(name)
    return c.get("value") if isinstance(c, dict) else None


# ---------------------------------------------------------------- price --
def set_price(key: str, inp: float, out: float, cached: float | None = None) -> dict:
    """What this model costs, in dollars per million tokens (the user's
    own numbers — Aura never guesses a price)."""
    p = {"in": float(inp), "out": float(out),
         "cached": float(cached) if cached is not None else float(inp)}
    save_facts(key, {"price": p})
    return p


def price(key: str) -> dict | None:
    """The model's price per million tokens, $0 for a local server, None
    when unknown."""
    from .machine import is_local
    p = (load().get(key) or {}).get("price")
    if isinstance(p, dict):
        return p          # A price you set wins (a local proxy to a paid cloud model)
    if is_local("http://" + key.split("|", 1)[0]):
        return {"in": 0.0, "out": 0.0, "cached": 0.0, "local": True}
    return None


def call_cost(key: str, usage: dict) -> float | None:
    """Dollars for one call from the server's own counts; None = unknown."""
    p = price(key)
    if p is None or not usage:
        return None
    prompt = usage.get("prompt_tokens") or 0
    cached = _cached_tokens(usage) or 0
    done = usage.get("completion_tokens") or 0
    return ((prompt - cached) * p["in"] + cached * p["cached"] + done * p["out"]) / 1_000_000


# --------------------------------------------------------------- probes --
def _first_edit(res) -> dict | None:
    for tc in res.tool_calls or []:
        if tc.get("name") == "edit_file" and not tc.get("parse_error"):
            return tc.get("parsed_arguments") or {}
    return None


def probe_edit_form(llm, log=print) -> dict:
    """Same edits, two forms; counts right edits (one retry with the error,
    as in a real run)."""
    from .tool_schemas import SCHEMAS
    from .tools import fs as F
    tools = [SCHEMAS["edit_file"]]
    score = {"tags": 0, "plain": 0}
    first = {"tags": 0, "plain": 0}
    prev = os.environ.get("AURA_LINE_TAGS")
    try:
        for form in ("tags", "plain"):
            os.environ["AURA_LINE_TAGS"] = "1" if form == "tags" else "0"
            for name, before, change, after in EDIT_CASES:
                root = Path(tempfile.mkdtemp())
                shadow = root / ".sh"
                shadow.mkdir()
                (root / name).write_text(before, encoding="utf-8", newline="")
                seen: dict = {}
                view = F.read_file(root, {"path": name}, None, seen)
                msgs = [{"role": "system", "content": "You edit files with the edit_file tool. "
                         "Make exactly the requested change and nothing else."},
                        {"role": "user", "content": f"read_file {name}:\n{view}\n\nChange: {change}"}]
                ok = False
                for attempt in (1, 2):
                    try:
                        res = llm.chat(msgs, tools=tools, temperature=0)
                    except Exception as e:           # noqa: BLE001
                        log(f"  {form} {name}: model error {str(e)[:80]}")
                        break
                    args = _first_edit(res)
                    if args is None:
                        msgs += [{"role": "assistant", "content": res.content or ""},
                                 {"role": "user", "content": "Use the edit_file tool to make the change."}]
                        continue
                    args["path"] = name
                    try:
                        F.edit_file(root, shadow, args, None, seen)
                        ok = (root / name).read_text(encoding="utf-8") == after
                        err = "" if ok else "the file isn't as asked yet"
                    except Exception as e:           # noqa: BLE001
                        err = str(e)[:300]
                    if ok:
                        score[form] += 1
                        first[form] += attempt == 1
                        break
                    (root / name).write_text(before, encoding="utf-8", newline="")
                    seen.clear()
                    view = F.read_file(root, {"path": name}, None, seen)
                    msgs += [{"role": "assistant", "content": res.content or "",
                              "tool_calls": [{"id": "t", "type": "function", "function": {
                                  "name": "edit_file", "arguments": json.dumps(args)}}]},
                             {"role": "tool", "tool_call_id": "t",
                              "content": f"error: {err}. The file is unchanged:\n{view}"}]
                log(f"  {form:5s} {name:10s} {'right' if ok else 'wrong'}")
    finally:
        if prev is None:
            os.environ.pop("AURA_LINE_TAGS", None)
        else:
            os.environ["AURA_LINE_TAGS"] = prev
    choice = "plain" if score["plain"] - score["tags"] >= CLEAR_WIN else "tags"
    return {"choice": choice, "right": score, "right_first_try": first, "of": len(EDIT_CASES)}


def _asked(res) -> bool:
    return any(tc.get("name") == "clarify" for tc in res.tool_calls or [])


def probe_early_check(llm, log=print) -> dict:
    from .tool_schemas import SCHEMAS
    tools = [SCHEMAS["clarify"], SCHEMAS["read_file"], SCHEMAS["edit_file"]]
    score = {"without": 0, "with": 0}
    for variant in ("without", "with"):
        for task in VAGUE + CLEAR:
            msgs = [{"role": "system", "content": "You are a coding agent working in the user's project."}]
            if variant == "with":
                msgs.append({"role": "user", "content": EARLY_CHECK})
            msgs.append({"role": "user", "content": task})
            try:
                res = llm.chat(msgs, tools=tools, temperature=0)
            except Exception as e:                   # noqa: BLE001
                log(f"  {variant} model error {str(e)[:80]}")
                continue
            asked = _asked(res)
            right = asked if task in VAGUE else not asked
            score[variant] += right
            log(f"  {variant:7s} {'asked' if asked else 'acted':5s} {'right' if right else 'wrong'}  {task[:50]}")
    keep = score["with"] - score["without"] >= CLEAR_WIN
    return {"choice": "on" if keep else "off", "right": score, "of": len(VAGUE) + len(CLEAR)}


# ----------------------------------------------------- capabilities --
def _red_png(size: int = 8) -> str:
    """A tiny solid red PNG as a data URL (built here, no image library)."""
    import base64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


_PING = {"type": "function", "function": {
    "name": "ping", "description": "Answer a ping.",
    "parameters": {"type": "object", "properties": {"n": {"type": "integer"}},
                   "required": ["n"]}}}


def _cached_tokens(usage: dict):
    """Cached prompt tokens, in any provider's shape; None = not reported."""
    if not usage:
        return None
    d = usage.get("prompt_tokens_details")
    if isinstance(d, dict) and "cached_tokens" in d:
        return d.get("cached_tokens") or 0
    for k in ("prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"):
        if k in usage:
            return usage.get(k) or 0
    return None


def _server_info(base_url: str, model: str, api_key: str | None) -> dict:
    """What an OpenAI-style server says about the model in /v1/models —
    the fields any server may add (no server-specific calls)."""
    try:
        from .ctx_probe import _get_json
        d = _get_json(base_url.rstrip("/") + "/models", api_key) or {}
    except Exception:                                # noqa: BLE001
        return {}
    for e in d.get("data") or []:
        if isinstance(e, dict) and e.get("id") == model:
            keep = ("owned_by", "max_model_len", "context_length", "context_window",
                    "max_context_length", "capabilities", "max_output_tokens", "created")
            return {k: e[k] for k in keep if k in e}
    return {}


def _probe_tool_choice(ask) -> str:
    """'good' (both right) / 'weak' — two tasks, Aura's full tool list."""
    from .tool_schemas import SCHEMAS
    tools = list(SCHEMAS.values()) if isinstance(SCHEMAS, dict) else list(SCHEMAS)
    tasks = [("Which files are in the folder 'src'? Use one tool.", ("list_files",), "path"),
             ("Find where the text 'def load_config' appears in this project. Use one tool.",
              ("search_files", "code_nav"), None)]
    right = 0
    for text, names, need in tasks:
        r = ask([{"role": "user", "content": text}], tools=tools)
        tc = next(iter(r.tool_calls or []), None)
        if tc and tc.get("name") in names and not tc.get("parse_error") and \
                (need is None or (tc.get("parsed_arguments") or {}).get(need)):
            right += 1
    return "good" if right == len(tasks) else "weak"


# what real runs show (no extra calls): tool calls that were malformed, named
# a tool that doesn't exist, or left out a required argument
def record_tool_calls(key: str, calls: int, bad: int) -> None:
    """Once per run (not per call): the run's tool calls and the bad ones."""
    rec = load().get(key) or {}
    st = dict(rec.get("tool_use") or {"calls": 0, "bad": 0})
    st["calls"] = int(st.get("calls", 0)) + int(calls)
    st["bad"] = int(st.get("bad", 0)) + int(bad)
    if st["calls"] > 400:                       # Recent behaviour counts most
        st = {"calls": st["calls"] // 2, "bad": st["bad"] // 2}
    save_facts(key, {"tool_use": st})


def tool_skill(key: str | None = None) -> str | None:
    """'weak' / 'good' / None (not known). Real use (20+ calls) first, else the
    measurement.
    """
    rec = load().get(key or ACTIVE) or {}
    st = rec.get("tool_use") or {}
    n, bad = int(st.get("calls", 0)), int(st.get("bad", 0))
    if n >= 20:
        return "weak" if bad / n >= 0.2 else "good"
    v = fact("tool_choice", key)
    return v if v in ("good", "weak") else None


def probe_capabilities(llm, log=print, full: bool = False) -> dict:
    """What this endpoint + model can do — a few tiny calls. Every value says how
    it was learned: measured, server, or unknown (never a guess).
    """
    from .llm import ProviderRejected
    global PROBING
    caps: dict = {}
    times: list[tuple[int, float, int]] = []          # First-token ms, seconds, tokens
    thinks = []

    def put(name, value, how="measured", note=""):
        caps[name] = {"value": value, "how": how, **({"note": note} if note else {})}
        log(f"  {name:14s} {value}{'  (' + note + ')' if note else ''}")

    def ask(msgs, **kw):
        res = llm.chat(msgs, temperature=0, **kw)
        times.append((res.first_token_ms or 0, res.duration_s or 0.0,
                      (res.usage or {}).get("completion_tokens") or 0))
        thinks.append(bool(res.reasoning))
        return res

    PROBING = True
    try:
        # tools, and several in one reply
        try:
            r = ask([{"role": "user", "content": "Call the ping tool with n=1."}], tools=[_PING])
            put("tools", "yes" if any(t.get("name") == "ping" for t in r.tool_calls or []) else "no")
            usage = r.usage or {}
            put("usage", "yes" if usage.get("prompt_tokens") else "no")
            put("cached_report", "yes" if _cached_tokens(usage) is not None else "no",
                note="the server says how many prompt tokens came from its cache")
        except ProviderRejected as e:
            put("tools", "no", note=str(e)[:80])
        except Exception as e:                       # noqa: BLE001
            put("tools", "unknown", "unknown", str(e)[:80])
        if caps.get("tools", {}).get("value") == "yes":
            try:
                r = ask([{"role": "user", "content": "Call the ping tool twice in this one "
                          "reply: once with n=1 and once with n=2."}], tools=[_PING])
                put("parallel_tools", "yes" if len(r.tool_calls or []) >= 2 else "no")
            except Exception as e:                   # noqa: BLE001
                put("parallel_tools", "unknown", "unknown", str(e)[:80])
            try:
                put("tool_choice", _probe_tool_choice(ask),
                    note="right tool + arguments out of the full list, 2 tries")
            except Exception as e:                   # noqa: BLE001
                put("tool_choice", "unknown", "unknown", str(e)[:80])
        # the system role
        try:
            ask([{"role": "system", "content": "Answer in one word."},
                 {"role": "user", "content": "Say ok."}])
            put("system_role", "yes")
        except ProviderRejected as e:
            put("system_role", "no", note=str(e)[:80])
        except Exception as e:                       # noqa: BLE001
            put("system_role", "unknown", "unknown", str(e)[:80])
        # images
        try:
            r = ask([{"role": "user", "content": [
                {"type": "text", "text": "What colour is this image? Answer with one word."},
                {"type": "image_url", "image_url": {"url": _red_png()}}]}])
            put("vision", "yes" if "red" in (r.content or "").lower() else "no",
                note="" if "red" in (r.content or "").lower() else f"said: {(r.content or '')[:40]}")
        except ProviderRejected as e:
            put("vision", "no", note=str(e)[:80])
        except Exception as e:                       # noqa: BLE001
            put("vision", "unknown", "unknown", str(e)[:80])
        # JSON output
        try:
            r = ask([{"role": "user", "content": 'Reply with the JSON object {"a": 1} and nothing else.'}],
                    extra={"response_format": {"type": "json_object"}})
            try:
                ok = isinstance(json.loads((r.content or "").strip().strip("`").removeprefix("json")), dict)
            except ValueError:
                ok = False
            put("json", "yes" if ok else "no")
        except ProviderRejected as e:
            put("json", "no", note=str(e)[:80])
        except Exception as e:                       # noqa: BLE001
            put("json", "unknown", "unknown", str(e)[:80])
        # caching: the same long start twice
        if caps.get("cached_report", {}).get("value") == "yes":
            try:
                big = "Reference text. " + " ".join(f"item-{i}" for i in range(1500))
                msgs = [{"role": "system", "content": big}, {"role": "user", "content": "Say ok."}]
                ask(msgs)
                r = ask(msgs)
                put("caching", "yes" if (_cached_tokens(r.usage) or 0) > 0 else "no")
            except Exception as e:                   # noqa: BLE001
                put("caching", "unknown", "unknown", str(e)[:80])
        else:
            put("caching", "unknown", "unknown", "the server doesn't report cached tokens")
        # speed
        try:
            r = ask([{"role": "user", "content": "Write the numbers 1 to 60, separated by spaces."}])
            toks = (r.usage or {}).get("completion_tokens") or 0
            gen = max(0.001, (r.duration_s or 0) - (r.first_token_ms or 0) / 1000)
            ftt = sorted(t[0] for t in times if t[0])
            put("first_token_ms", ftt[len(ftt) // 2] if ftt else None,
                note="middle of the calls above")
            put("tokens_per_s", round(toks / gen, 1) if toks else None)
        except Exception as e:                       # noqa: BLE001
            put("tokens_per_s", "unknown", "unknown", str(e)[:80])
        put("thinking", "yes" if any(thinks) else "no",
            note="thinking text seen in replies" if any(thinks) else "")
        if full:
            try:
                r = ask([{"role": "user", "content": "Write the word go, 20000 times, separated by spaces."}])
                n = (r.usage or {}).get("completion_tokens") or 0
                put("max_reply", n if r.finish_reason == "length" else f">={n}",
                    note=f"stopped: {r.finish_reason}")
            except Exception as e:                   # noqa: BLE001
                put("max_reply", "unknown", "unknown", str(e)[:80])
    finally:
        PROBING = False
    info = _server_info(llm.p.base_url, llm.p.model, getattr(llm.p, "api_key", None))
    if info:
        caps["server_info"] = {"value": info, "how": "server"}
        log(f"  server says    {info}")
    return caps


ALL = ("caps", "edit_form", "early_check")


def hint(base_url: str, model: str) -> str | None:
    """One line for a model not checked yet (or None)."""
    rec = load().get(model_key(base_url, model)) or {}
    if rec.get("caps"):
        return None
    return f"{model} isn't checked yet — aura eval model ({cost_note()})"


def offer(llm, key: str, ask=input, say=print) -> dict | None:
    """After a model is picked: ONE offer to learn it (it spends tokens, so
    it always asks; a "no" is remembered). `aura eval model` any time."""
    rec = load().get(key) or {}
    if rec.get("caps") or rec.get("declined"):
        return None
    say(f"\n{key.split('|', 1)[-1]} hasn't been checked yet. Aura can find out what it can "
        "do (tools, images, JSON, speed, …) and which edit form and ask-first habit "
        "suit it — so it never has to guess.")
    say("  " + cost_note())
    try:
        yes = str(ask("  check it now? [y/N] ")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        yes = False
    if not yes:
        save_facts(key, {"declined": round(time.time())})
        say("  ok — not asking again for this model (later: aura eval model).")
        return None
    out = measure(llm, key, log=lambda s: say(s))
    for ln in show(key):
        say(ln)
    return out


def measure(llm, key: str, which: tuple = ALL, log=print,
            full: bool = False) -> dict:
    out = {}
    if "caps" in which:
        log("what the model can do (a few tiny calls)")
        out["caps"] = probe_capabilities(llm, log, full=full)
    if "edit_form" in which:
        log("edit form: line tags vs plain lines")
        out["edit_form"] = probe_edit_form(llm, log)
    if "early_check" in which:
        log("early check: ask first on vague tasks, act on clear ones")
        out["early_check"] = probe_early_check(llm, log)
    save_facts(key, out)
    return out


COSTS = {"caps": 10, "edit_form": 18, "early_check": 12}


def cost_note(which: tuple = ALL, full: bool = False) -> str:
    n = sum(COSTS[w] for w in which) + (1 if full and "caps" in which else 0)
    extra = " plus one long reply (--full)" if full and "caps" in which else ""
    return (f"about {n} short model calls{extra} — a few thousand tokens; a minute "
            "or two on a local model")


COST_NOTE = cost_note()


def show(key: str) -> list[str]:
    rec = load().get(key)
    if not rec:
        return [f"{key}: not measured yet — aura eval model"]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("measured", 0)))
    out = [f"{key} (measured {when}):"]
    e = rec.get("edit_form")
    if e:
        out.append(f"  edit form: {e['choice']} — right with tags {e['right']['tags']}/{e['of']}, "
                   f"plain {e['right']['plain']}/{e['of']}")
    c = rec.get("early_check")
    if c:
        out.append(f"  early check: {c['choice']} — right without {c['right']['without']}/{c['of']}, "
                   f"with {c['right']['with']}/{c['of']}")
    caps = rec.get("caps") or {}
    if caps:
        out.append("  can do (measured = checked on this server; unknown = not told, never guessed):")
        for name, v in caps.items():
            if name == "server_info":
                out.append(f"    {'server says':16s} {v.get('value')}")
                continue
            note = f" — {v['note']}" if v.get("note") else ""
            out.append(f"    {name:16s} {v.get('value')} ({v.get('how')}){note}")
    return out

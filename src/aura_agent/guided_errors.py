"""guided_errors.py — Symptom → cause → fix for every failure.

The market contract: users never see a raw traceback. Every failure prints:
what happened (symptom) why (most likely cause, specific to the error) the
exact command to fix it

`explain_error(exc)` returns [lines]; CLI wraps every command dispatch.
"""
from __future__ import annotations

import urllib.error


def _endpoint_fixes(base_url: str | None, code: int | None) -> list[str]:
    fixes = []
    if code in (401, 403):
        fixes.append("why     : the endpoint answered but rejected the key")
        fixes.append("fix     : set the key, then retry:")
        fixes.append("            export AI_API_KEY=<key>        # or")
        fixes.append("            aura model                      # pick a keyless provider")
    elif code == 404:
        fixes.append("why     : endpoint answered but the path is wrong — "
                     "base_url must include /v1 for OpenAI-compatible servers")
        fixes.append("fix     : check the URL, then: aura model")
    elif code == 0 or code is None:
        fixes.append("why     : nothing is listening on that port")
        fixes.append("fix     : start a local server, or switch profile:")
        fixes.append("            ollama serve                    # if installed")
        fixes.append("            aura model                      # scan + pick a live one")
    else:
        fixes.append(f"why     : server answered HTTP {code}")
        fixes.append("fix     : aura doctor  (health check) · aura model (switch)")
    return fixes


def explain_error(exc: BaseException, base_url: str | None = None) -> list[str]:
    s = str(exc)
    out = [f"error   : {type(exc).__name__}: {s}"]

    code = None
    url = base_url
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        url = url or getattr(exc, "url", None)
    if "HTTP Error" in s:
        try:
            code = int(s.split("HTTP Error ")[1].split()[0])
        except Exception:
            pass
    if "unreachable" in s.lower() or "connection" in s.lower() or code in (401, 403, 404):
        if url:
            out.append(f"endpoint: {url}")
        out.extend(_endpoint_fixes(url, code))
        out.append("or      : aura setup quick   # full re-detection")
        return out

    if "scope" in s.lower() and ("missing" in s.lower() or "not found" in s.lower()):
        out.append("why     : this notebook has no scope.json")
        out.append("fix     : aura init <name>     # creates scope + dirs")
        return out

    if "No module named" in s:
        mod = s.split("No module named")[-1].strip().strip("'\"")
        kind, pkg = dep_kind(mod)
        out.append(f"why     : {kind} part '{mod}' is not installed")
        out.append(f"fix     : python -m pip install {pkg}")
        return out

    out.append("fix     : run 'aura doctor' for a health check, or re-run setup:")
    out.append("            aura setup quick")
    return out


# ---------------------------------------------------------------------------
# Card contract v2 — five fixed sections, fairness-consistent shape. Tone lint
# rules (Microsoft voice): takes responsibility, carries a solution, leads with
# the important line, no blame phrases.
# ---------------------------------------------------------------------------

_TONE_FIXES = (  # Blame-phrase → responsibility-phrase
    ("invalid input", "I couldn't read that"),
    ("bad input", "I couldn't read that"),
    ("wrong input", "that didn't read as expected"),
    ("you must", "the next step is"),
    ("you failed", "the call didn't land"),
    ("illegal", "not allowed by the safety scope"),
)


def _plain_what(exc: BaseException) -> str:
    """Plain-language WHAT HAPPENED — no exception codes."""
    s = str(exc)
    low = s.lower()
    if "connection" in low or "unreachable" in low:
        return "I couldn't reach the AI endpoint"
    if "timeout" in low or "timed out" in low:
        return "the endpoint took too long to answer"
    if "http error" in low:
        code = None
        try:
            code = int(s.split("HTTP Error ")[1].split()[0])
        except Exception:
            pass
        reasons = {401: "the endpoint rejected the API key",
                   403: "the endpoint denied access (key or plan)",
                   404: "the endpoint path is wrong (needs /v1)",
                   429: "the endpoint is rate-limiting us"}
        if code is not None and code in reasons:
            return reasons[code]
        return f"the endpoint answered HTTP {code}"
    if "No module named" in s:
        mod = s.split("No module named")[-1].strip().strip("'\"")
        kind, _pkg = dep_kind(mod)
        return f"{'a' if kind == 'required' else 'an'} {kind} part of Aura ({mod}) isn't installed"
    if "scope" in low and ("missing" in low or "not found" in low):
        return "this project has no safety-scope file yet"
    return f"the {type(exc).__name__} step failed"


# What pyproject.toml lists (import name → package): required vs optional
_REQUIRED = {"nacl": "pynacl", "wcwidth": "wcwidth", "keyring": "keyring"}
_OPTIONAL = {"playwright": "'aura-agent[web]'", "pypdf": "'aura-agent[pdf]'",
             "cryptography": "'aura-agent[vault]'", "pytest": "'aura-agent[test]'",
             "hypothesis": "'aura-agent[test]'"}


def dep_kind(mod: str) -> tuple[str, str]:
    """('required' | 'optional', what to pip install) for a missing module."""
    top = mod.split(".")[0]
    if top in _REQUIRED:
        return "required", _REQUIRED[top]
    return "optional", _OPTIONAL.get(top, top)


def _plain_why(exc: BaseException) -> str:
    low = str(exc).lower()
    if "connection" in low or "unreachable" in low:
        return "nothing is listening on that port, or the server stopped"
    if "http error 401" in low or "http error 403" in low:
        return "the key is missing, wrong, or expired"
    if "http error 404" in low:
        return "base_url must include /v1 for OpenAI-compatible servers"
    if "http error 429" in low:
        return "too many requests in a short window"
    if "No module named" in str(exc):
        mod = str(exc).split("No module named")[-1].strip().strip("'\"")
        return ("Aura needs it — the install is incomplete" if dep_kind(mod)[0] == "required"
                else "optional extras install separately")
    if "scope" in low:
        return "the notebook was created without its scope walk"
    return "the failure class isn't one I know yet"


def _card_fix(exc: BaseException) -> str:
    s = str(exc)
    low = s.lower()
    if "http error 401" in low or "http error 403" in low:
        return "FIX    set the key: export AI_API_KEY=<key>  ·  or /connect"
    if "http error 404" in low:
        return "FIX    fix the URL, then: aura model"
    if "connection" in low or "unreachable" in low:
        return ("FIX    start the server (ollama serve)  ·  or switch: "
                "aura model")
    if "http error 429" in low:
        return "FIX    wait a minute, then retry the last command"
    if "No module named" in s:
        mod = s.split("No module named")[-1].strip().strip("'\"")
        return f"FIX    pip install {mod}"
    if "scope" in low and ("missing" in low or "not found" in low):
        return "FIX    aura init <name>   # creates the safety scope"
    return "FIX    aura doctor  # health check + safe fixes"


def _card_or(exc: BaseException) -> str:
    low = str(exc).lower()
    if "connection" in low or "http error" in low:
        return "OR     /model — pick any other live endpoint"
    return "OR     aura setup quick   # full re-detection"


# ---------------------------------------------------------------------------
# The five install death modes → exact fixes (install error map).
# Symptom-anchored: what pipx/pip actually prints, mapped to the fix.
# ---------------------------------------------------------------------------

INSTALL_DEATH_MODES: tuple[tuple[str, str, str], ...] = (
    # (symptom-fragment, what-happened, fix-command)
    ("No module named pip",
     "pip itself is missing from this Python",
     "python3 -m ensurepip --upgrade"),
    ("externally-managed-environment",
     "this Python is system-managed — direct pip installs are blocked",
     "pipx install aura-agent   # or: python3 -m venv .venv first"),
    ("pipx: command not found",
     "pipx isn't installed yet",
     "pipx installs isolated CLIs: pip install --user pipx, then pipx "
     "ensurepath"),
    ("Permission denied",
     "the install location isn't writable by your user",
     "pipx install aura-agent   # user-scoped, no sudo needed"),
    ("No matching distribution found",
     "the package index is unreachable (offline or proxy)",
     "check network/proxy, then retry; or pip install --index-url <mirror>"),
)


def error_card(exc: BaseException, survives: str = "session intact",
               stress: bool = False) -> str:
    """The 5-line card (stress mode: condition→action pairs only). Same shape for
    EVERY failure class — fairness doctrine.
    """
    what = _plain_what(exc)
    for bad, good in _TONE_FIXES:
        what = what.replace(bad, good)
    if stress:
        return (f"SURVIVES  {survives}\n"
                f"HAPPENED  {what}\n"
                f"FIX    {_card_fix(exc).replace('FIX    ', '', 1)}")
    return (
        f"SURVIVES  {survives}\n"
        f"HAPPENED  {what}\n"
        f"WHY    {_plain_why(exc)}\n"
        f"{_card_fix(exc)}\n"
        f"{_card_or(exc)}"
    )


# ---------------------------------------------------------------------------
# Blameless postmortem (SRE culture) — run-ending failures become education:
# what happened / causes / ONE tracked action.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Failure-class taxonomy BEFORE any retry. 14.4% of failures are gates —
# retrying a gate is wrong; config failures need a SWITCH, not a retry; only
# transient classes may retry.
# ---------------------------------------------------------------------------

def run_guarded(fn, *args, base_url: str | None = None, **kwargs) -> int:
    """Wrap a cmd_* function: exceptions become guided error CARDS, exit 1.
    Tracebacks only behind AURA_DEBUG=1 (uninvited-tracebacks ban).
    """
    try:
        return fn(*args, **kwargs) or 0
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 — the whole point is catching all
        import os as _os
        print()
        print(error_card(exc, survives="notebook data on disk is intact"))
        if _os.environ.get("AURA_DEBUG", "") == "1":
            import traceback
            traceback.print_exc()
        else:
            print("  (full traceback: set AURA_DEBUG=1)")
        return 1

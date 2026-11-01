"""helper.py — the optional cheap second model.

None of them checks the small model is good enough before handing it work.

Aura: the option helper.model names a model setup (aura model list) or a model
on the same server. Before it gets any job, a check: a short summary must keep
every name and number word for word (a summary that loses
"src/auth/login.py:42" is worse than none). Passed → it writes chat titles and
summaries; failed or not set → the main model does them. Every job says which
model did it.
"""
from __future__ import annotations

import re
import time

CHECK_TEXT = ("While deploying build 7f3a9c1, src/auth/login.py line 42 raised "
              "KeyError: 'session_id'; the health check on port 8443 failed 3 times "
              "and the rollback to v2.14.0 took 95 seconds.")
MUST_KEEP = ("7f3a9c1", "src/auth/login.py", "42", "KeyError", "session_id", "8443",
             "v2.14.0", "95")
_KEY = "helper_check"


def name() -> str:
    try:
        from .options import get
        return str(get("helper.model") or "").strip()
    except Exception:                                # noqa: BLE001
        return ""


def profile(main_profile=None):
    """The helper's model setup, or None when none is set."""
    n = name()
    if not n:
        return None
    from dataclasses import replace
    from .config import resolve
    try:
        from .profile_store import get_profile
        saved = get_profile(n)
    except Exception:                                # noqa: BLE001
        saved = None
    if saved:
        # explicit values beat the environment (AI_MODEL is the MAIN model)
        return resolve(n, base_url=saved.get("base_url"), model=saved.get("model"),
                       api_key=saved.get("api_key"))
    main = main_profile or resolve(None)
    return replace(main, model=n, name=f"helper:{n}")   # A model on the same server


def _facts_key(p) -> str:
    from .model_facts import model_key
    return model_key(p.base_url, p.model)


def checked(p) -> str | None:
    """'yes' / 'no' for this helper, or None (not checked yet)."""
    from .model_facts import load
    rec = (load().get(_facts_key(p)) or {}).get(_KEY) or {}
    return rec.get("value")


def check(p, log=print) -> dict:
    """One short call: does the helper keep names and numbers exact?"""
    from .llm import LLMClient
    from .model_facts import save_facts
    t0 = time.time()
    try:
        res = LLMClient(p).chat([{"role": "user", "content": (
            "Summarize this in one sentence. Keep every file name, number and "
            "identifier exactly as written.\n\n" + CHECK_TEXT)}], temperature=0)
        text = res.content or ""
    except Exception as e:                           # noqa: BLE001
        out = {"value": "no", "why": f"unreachable: {str(e)[:80]}"}
        save_facts(_facts_key(p), {_KEY: out})
        log(f"helper {p.model}: not usable — {out['why']}")
        return out
    lost = [w for w in MUST_KEEP if w not in text]
    out = {"value": "no" if lost else "yes", "kept": len(MUST_KEEP) - len(lost),
           "of": len(MUST_KEEP), "seconds": round(time.time() - t0, 1)}
    if lost:
        out["lost"] = lost
    save_facts(_facts_key(p), {_KEY: out})
    log(f"helper {p.model}: {'passed' if not lost else 'failed'} — kept "
        f"{out['kept']}/{out['of']} names and numbers"
        + (f" (lost: {', '.join(lost)})" if lost else ""))
    return out


def client(main_llm=None):
    """(LLMClient, who) for a side job: the helper when set and checked
    good, else the main model. who = the model's name, for the record."""
    p = profile(getattr(main_llm, "p", None))
    if p is not None and checked(p) == "yes":
        from .llm import LLMClient
        return LLMClient(p), f"helper {p.model}"
    if main_llm is not None:
        return main_llm, f"main {main_llm.p.model}"
    return None, ""


def state() -> str:
    """One line for /settings explain."""
    p = profile()
    if p is None:
        return "not set — the main model does titles and summaries"
    c = checked(p)
    return {"yes": f"{p.model}: checked, in use",
            "no": f"{p.model}: failed the check — the main model does its jobs",
            None: f"{p.model}: not checked yet — aura eval helper"}[c]


def title_for(text: str, main_llm=None) -> tuple[str, str]:
    """A 3–6 word chat title from the first message. ("", "") if no model."""
    llm, who = client(main_llm)
    if llm is None:
        return "", ""
    try:
        res = llm.chat([{"role": "user", "content": (
            "Give a 3 to 6 word title for a chat that starts with the message "
            "below. Reply with the title only, no quotes.\n\n" + text[:1500])}],
            temperature=0, side=True)
    except Exception:                                # noqa: BLE001
        return "", ""
    t = re.sub(r"\s+", " ", (res.content or "").strip().strip('"\'`*#')).strip()
    return (t[:60], who) if t else ("", "")

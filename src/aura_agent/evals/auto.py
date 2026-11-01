"""auto.py — offer the eval when a model has not been measured yet.

When the operator picks a model (aura setup / aura model / /model in chat)
and Aura has no eval result for it, it OFFERS to run the suite — it never
runs on its own, because the tasks spend the operator's tokens. Asked once
per model: a "no" is remembered. `aura eval run` works any time.

Results live in ~/.aura/evals/ (AURA_HOME), named by model, so the offer
knows which models are measured and /features can point at the latest
score.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def _dir() -> Path:
    from ..paths import aura_home
    return aura_home() / "evals"


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(model or "unknown"))[:80]


def latest(model: str) -> Path | None:
    """The newest saved result for this model, or None."""
    d = _dir()
    if not d.is_dir():
        return None
    found = sorted(d.glob(f"{_slug(model)}--*.json"))
    return found[-1] if found else None


def save(doc: dict) -> Path:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_slug(doc.get('model'))}--{time.strftime('%Y%m%d-%H%M%S')}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def _declined() -> set[str]:
    from ..config import load_saved_settings
    return set(load_saved_settings().get("eval_declined") or [])


def _remember_no(model: str) -> None:
    from ..config import load_saved_settings, save_saved_settings
    saved = load_saved_settings()
    saved["eval_declined"] = sorted(_declined() | {str(model)})
    save_saved_settings(saved)


def should_offer(model: str | None) -> bool:
    return bool(model) and latest(model) is None and model not in _declined()


def hint(model: str | None) -> str | None:
    """One line for places where running now would be in the way (chat)."""
    if not should_offer(model):
        return None
    return (f"{model} hasn't been measured yet — `aura eval run` scores it "
            "on 20 small tasks (uses tokens)")


def estimate(n_tasks: int) -> str:
    """Cost of a run in plain words, from the eval results saved on this machine
    (any model): average tokens and minutes per task. Says so when there is
    nothing to go on yet.
    """
    toks, secs = [], []
    d = _dir()
    for f in (sorted(d.glob("*.json")) if d.is_dir() else []):
        try:
            for t in json.loads(f.read_text(encoding="utf-8")).get("tasks", []):
                if t.get("tokens"):
                    toks.append(int(t["tokens"]))
                    secs.append(float(t.get("seconds") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if not toks:
        return ("cost: not known yet — no earlier eval on this machine to "
                "estimate from (on one large model a full run used about "
                "1–1.5 million tokens)")
    per_t = sum(toks) / len(toks)
    per_s = sum(secs) / len(secs)
    return (f"cost: about {per_t * n_tasks / 1e6:.1f} million tokens and "
            f"{max(1, round(per_s * n_tasks / 60))} minutes (from "
            f"{len(toks)} earlier tasks on this machine; your model may "
            "differ)")


def offer(model: str | None, profile_name: str | None = None,
          ask=input, say=print) -> dict | None:
    """Ask once per unmeasured model; on yes run the suite and save it."""
    if not should_offer(model):
        return None
    say(f"\n{model} hasn't been measured on this agent yet. Run the eval now?")
    from .runner import select as _select
    say("  20 small tasks, each in a temporary folder — "
        "you can stop it with Ctrl+C. Later: `aura eval run`.")
    say("  " + estimate(len(_select())))
    try:
        yes = str(ask("  run it now? [y/N] ")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        yes = False
    if not yes:
        _remember_no(model)
        say("  ok — not asking again for this model.")
        return None
    from .runner import run_suite, select

    def show(r):
        say(f"  {'PASS' if r['passed'] else 'fail'} {r['id']}")
    try:
        doc = run_suite(select(), profile_name, on_result=show)
    except KeyboardInterrupt:
        say("  stopped — nothing saved. Run it later with `aura eval run`.")
        return None
    if doc.get("aborted"):
        say(f"  stopped: {doc['aborted']}")
        return doc
    p = save(doc)
    say(f"  score {doc['passed']}/{len(doc['tasks'])} — saved {p}")
    return doc

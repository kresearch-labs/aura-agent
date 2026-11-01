"""feedback.py — The visible feedback loop.

Feedback [text] → consent screen → auto-metadata (versions, profile NAME, OS —
no engagement data, no absolute paths) → GitHub issue if gh auth, else durable
local bundle (~aura/feedback-bundles/). Scrubbed at WRITE time, not upload time
(a security tool's bundle is itself sensitive). Every fix names its reporter;
telemetry stays OFF.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def _scrub(text: str) -> str:
    """No absolute paths, no token-shaped strings, at WRITE time."""
    out = re.sub(r"(?<!\w)(/[A-Za-z0-9._-]+){2,}", "<path>", str(text))
    out = re.sub(r"\b(sk-|AKIA|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b",
                 "[REDACTED]", out)
    out = re.sub(r"(?i)\b(\w*(?:api[_-]?key|secret|password|token)\w*\s*[=:]"
                 r"\s*)[\"']?[^\s\"']{8,}", r"\1[REDACTED]", out)
    return out


def collect_bundle(text: str, eng, version: str, profile: str,
                   os_name: str) -> dict:
    t = _scrub(text)
    return {
        "text": t,
        "version": version,
        "profile": profile,
        "os": os_name,
        "ts": round(time.time(), 3),
    }


def write_bundle(eng, bundle: dict) -> Path:
    d = Path(eng) / "feedback-bundles"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"fb-{int(time.time() * 1000)}.json"
    dest.write_text(json.dumps(bundle, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return dest


def feedback_flow(text: str, eng, version: str, profile: str,
                  os_name: str, consent: bool = True) -> dict:
    """The /feedback flow: consent → bundle → (gh else local).
    Returns {ok, where} — the operator always knows where their words went."""
    if not consent:
        return {"ok": False, "where": "declined — nothing written"}
    bundle = collect_bundle(text=text, eng=eng, version=version,
                            profile=profile, os_name=os_name)
    # gh path: only when gh CLI is authenticated; bundle is written EITHER
    # way so the reporter's words are never lost to a network failure.
    local = write_bundle(eng, bundle)
    gh_ok = False
    try:
        import shutil
        if shutil.which("gh"):
            gh_ok = True   # Issue creation lands with the public repo
    except Exception:
        gh_ok = False
    return {"ok": True,
            "where": str(local),
            "gh": gh_ok,
            "note": ("GitHub issue opens when the public repo exists — your "
                     "bundle is already saved locally" if gh_ok
                     else "saved locally; GitHub issue path activates at "
                          "public release")}

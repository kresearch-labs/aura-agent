"""setup_pickers.py — Per-capability provider pickers.

Each capability shows its providers with availability checks and what each
needs (free/paid/key), the operator picks, and the choice is persisted to
profiles.json under "capabilities" — consumed at tool registration time.

Capabilities (real, in-codebase):
  llm — any OpenAI-compatible endpoint (scan: 11434/1234/8080/8000)
  browser — playwright chromium [web] extra / stdlib-no-browser fallback
  artifacts — sha256 store (always available, no provider)
  web-crawl — stdlib crawler (always available, no key)

Tool Availability Summary at the end: X/Y ready + exact missing piece per
disabled capability.
"""
from __future__ import annotations

import importlib.util

from .profile_store import _load_file, _save_file
from .setup import _fetch_models


# --------------------------------------------------------------------------
# capability definitions: id -> (label, providers [(name, desc, ready)])
# --------------------------------------------------------------------------

def capability_table() -> dict[str, dict]:
    """id -> {label, providers: [(name, detail, ready)], default}"""
    return {
        "llm": dict(
            label="LLM endpoint",
            providers=[
                ("ollama", "free · local · no key",
                 _llm_ready("http://localhost:11434/v1")),
                ("lm-studio", "free · local",
                 _llm_ready("http://localhost:1234/v1")),
                ("vllm", "free · local · long context",
                 _llm_ready("http://localhost:8000/v1")),
                ("llamacpp", "free · local",
                 _llm_ready("http://localhost:8080/v1")),
                ("custom", "any OpenAI-compatible endpoint (+key)", True),
            ],
            default="ollama"),
        "browser": dict(
            label="Browser engine (DOM proofs)",
            providers=[
                ("playwright", "chromium [web] extra — full DOM proof",
                 importlib.util.find_spec("playwright") is not None),
                ("off", "skip browser proof (tier-1 re-verify only)", True),
            ],
            default="playwright"),
        "web-crawl": dict(
            label="Web crawler",
            providers=[("stdlib", "built-in polite crawler — no deps", True)],
            default="stdlib"),
        "artifacts": dict(
            label="Artifact store",
            providers=[("sha256", "built-in — no deps", True)],
            default="sha256"),
    }


def _llm_ready(url: str) -> bool:
    ok, _, _, _ = _fetch_models(url)
    return ok


def availability() -> dict[str, tuple[bool, str]]:
    """(ready, missing_piece) per capability id — for the summary."""
    caps = capability_table()
    out = {}
    for cid, cap in caps.items():
        any_ready = any(r for _, _, r in cap["providers"])
        missing = ""
        if not any_ready:
            if cid == "llm":
                missing = "no local endpoint — install Ollama (ollama.com) " \
                          "or run 'aura setup minimal'"
            elif cid == "browser":
                missing = "pip install 'aura-agent[web]' && " \
                          "playwright install chromium"
        out[cid] = (any_ready, missing)
    return out


def render_summary() -> str:
    avail = availability()
    lines = ["Tool availability:"]
    caps = capability_table()
    n_ok = 0
    for cid, cap in caps.items():
        ready, missing = avail[cid]
        n_ok += 1 if ready else 0
        if ready:
            lines.append(f"  ✓ {cap['label']:32s} ready")
        else:
            lines.append(f"  ✗ {cap['label']:32s} {missing}")
    lines.append(f"  ({n_ok}/{len(caps)} capabilities ready)")
    return "\n".join(lines)


def pick_capabilities(assume_yes: bool = False,
                      prefer_llm: str | None = None) -> dict[str, str]:
    """Interactive per-capability picker (only when >1 provider).
    EOF/blank/invalid → default (never raises).
    prefer_llm: preselect/skip the llm row when the scan already verified
    that provider (bugs 2/4 — no overlapping decision)."""
    caps = capability_table()
    chosen: dict[str, str] = {}
    for cid, cap in caps.items():
        if len(cap["providers"]) == 1 or assume_yes:
            chosen[cid] = cap["default"]
            continue
        if cid == "llm" and prefer_llm:
            known = {n for n, _, _ in cap["providers"]}
            chosen[cid] = prefer_llm if prefer_llm in known else "custom"
            continue
        print(f"\n{cap['label']}:")
        for i, (name, detail, ready) in enumerate(cap["providers"], 1):
            mark = "✓" if ready else "✗"
            star = "*" if name == cap["default"] else " "
            print(f"  {star}{i}. {name:12s} [{mark}] {detail}")
        try:
            raw = input(f"  choose 1-{len(cap['providers'])} "
                        f"[{cap['default']}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if raw.isdigit() and 1 <= int(raw) <= len(cap["providers"]):
            chosen[cid] = cap["providers"][int(raw) - 1][0]
        else:
            chosen[cid] = cap["default"]
    return chosen


def save_capabilities(chosen: dict[str, str]) -> None:
    data = _load_file()
    data["capabilities"] = chosen
    _save_file(data)


def load_capabilities() -> dict[str, str]:
    data = _load_file()
    caps = capability_table()
    saved = data.get("capabilities") or {}
    return {cid: saved.get(cid, cap["default"])
            for cid, cap in caps.items()}

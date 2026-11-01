"""webcmd.py — /web in chat (shared by any screen): the search connectors
and their health, the open public sources switch, the all-websites switch."""
from __future__ import annotations

from pathlib import Path


def web_command(eng: Path, args: list[str]) -> list[str]:
    from . import websearch as W
    from .engagement import load_scope, set_web_all
    if args[:1] in (["open"], ["engines"]) and len(args) >= 2:
        v = args[1].lower()
        v = {"all": "on", "knowledge": "on"}.get(v, v)    # Earlier wording
        if v not in ("on", "off"):
            return ["/web open on | off"]
        W.set_consent(v)
        return [f"open public sources (Wikipedia, arXiv, PubMed, Stack Overflow, "
                f"GitHub): {v}"]
    if args[:1] == ["all"] and len(args) >= 2 and args[1] in ("on", "off"):
        return [set_web_all(eng, args[1] == "on")]
    out = ["web search — services you connect with your own key, and the open "
           "public sources if you switch them on:"]
    out += W.status_lines()
    out.append("all websites: " + ("ON — any public website may be read"
                                   if load_scope(eng).get("web_all", True) else
                                   "off — only websites in /scope (turn on: /web all on)"))
    out.append("connect a service: aura keys set BRAVE_API_KEY (or TAVILY_API_KEY, "
               "EXA_API_KEY, SERPER_API_KEY, FIRECRAWL_API_KEY, MARGINALIA_API_KEY, "
               "OPENALEX_API_KEY; SEARXNG_URL for your own server) · /web open on|off · "
               "/web all on|off")
    return out

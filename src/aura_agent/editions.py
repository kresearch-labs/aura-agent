"""editions.py — which optional parts this Aura was shipped with.

Aura Teams lives in its own modules (teams*.py, tools/team_tool.py). A copy
of Aura can be shipped without them; every place that offers Teams asks
here first, so a copy without them simply has no Teams — no command, no
tool, no sidebar section, no setup step, no error.
"""
from __future__ import annotations

import importlib.util
from functools import lru_cache


@lru_cache(maxsize=1)
def teams_present() -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in (
            "aura_agent.teams", "aura_agent.teams_ui", "aura_agent.teams_run",
            "aura_agent.tools.team_tool"))
    except (ImportError, ValueError):
        return False

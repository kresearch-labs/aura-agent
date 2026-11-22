"""Aura team server. PROTOCOL = 1; team_dir() is its data folder."""
from __future__ import annotations

PROTOCOL = 1

# Routes whose shape changed per protocol version. A client OLDER than the
# server gets 426 update_aura on these; a NEWER client is served while it can
# and told once on its side.
CHANGED_ROUTES: dict[int, list[str]] = {}


def team_dir():
    """The team server's data folder inside Aura's own."""
    from ..paths import aura_home
    d = aura_home() / "teamserver"
    d.mkdir(parents=True, exist_ok=True)
    return d

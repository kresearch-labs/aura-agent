"""config_line.py —: THE canonical config annunciation.

One line, identical everywhere it renders (banner, status, model header,
arrival): `profile · model · ctx · key · source`. Every config change
re-announces it. Provenance display prints source-of-truth, never secret
values; empty-vs-absent are distinct visible states [ doctrine].
"""
from __future__ import annotations


def config_line(profile: str, model: str, ctx: int | str,
                key_status: str, source: str) -> str:
    ks = f"key: {key_status}" if key_status else "key: unknown"
    # Visual: two-line form — the reply lands after the prompt bracket (~34
    # cols), so one line overflows 76. Caller splits on \n.
    return (f"{profile} · {model} · ctx {ctx}\n"
            f"{ks} · from {source}")



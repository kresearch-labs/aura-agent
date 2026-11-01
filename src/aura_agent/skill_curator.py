"""skill_curator.py — the person's own hand on their skills. Nothing here moves by
itself: the time-based "put aside when unused" pass was dropped — a skill
hidden from the model is never used, so it looks unused and gets put aside (it
fed itself), and a skill needed once a year would be gone the week it's needed.
Picking the right skills for the work is skill_pick.py; this is only what the
person does:

  - pin: a skill always offered to the model (listed every turn);
  - archive: put one aside in <skills>/.archive (never deleted);
  - restore: bring it back;
  - status: each skill, pinned or not, how it did, what is put aside;
  - every change is written to <skills>/.curator.jsonl.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

USAGE = ".usage.json"          # Per skills folder: {name: {"pinned": bool}}
LEDGER = ".curator.jsonl"
ARCHIVE = ".archive"


def roots(eng: Path | None) -> list[Path]:
    """The person's skill folders: the project's and the profile's."""
    from .skills import _global_home
    out = []
    if eng is not None:
        out.append(Path(eng) / "skills")
    out.append(_global_home(None))
    return [r for r in out if r.exists()]


def _units(root: Path) -> list[str]:
    from .skills import SKILL_FILE
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and ((d / SKILL_FILE).exists() or (d / "manifest.json").exists()))


def _usage(root: Path) -> dict:
    try:
        return json.loads((root / USAGE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_usage(root: Path, u: dict) -> None:
    tmp = root / (USAGE + ".tmp")
    tmp.write_text(json.dumps(u, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(root / USAGE)


def _ledger(root: Path, action: str, name: str) -> None:
    with open(root / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": round(time.time()), "action": action, "skill": name}) + "\n")


def pinned(eng: Path | None) -> set[str]:
    out: set[str] = set()
    for root in roots(eng):
        out |= {n for n, r in _usage(root).items() if isinstance(r, dict) and r.get("pinned")}
    return out


def _find(eng: Path | None, name: str, archived: bool) -> Path | None:
    for root in roots(eng):
        if (root / ARCHIVE / name if archived else root / name).is_dir():
            return root
    return None


def archive(eng: Path | None, name: str) -> str:
    root = _find(eng, name, archived=False)
    if not root:
        return f"no skill '{name}' of yours here (built-in skills can't be put aside)"
    dest = root / ARCHIVE / name
    if dest.exists():
        dest = root / ARCHIVE / f"{name}-{int(time.time())}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root / name), str(dest))
    _ledger(root, "archive", name)
    return f"'{name}' put aside — `aura skills restore {name}` brings it back"


def restore(eng: Path | None, name: str) -> str:
    root = _find(eng, name, archived=True)
    if not root:
        return f"no put-aside skill '{name}' — `aura skills curator` lists them"
    if (root / name).exists():
        return f"a skill '{name}' is already in use — rename one first"
    shutil.move(str(root / ARCHIVE / name), str(root / name))
    _ledger(root, "restore", name)
    return f"'{name}' is back"


def pin(eng: Path | None, name: str, on: bool = True) -> str:
    root = _find(eng, name, archived=False)
    if not root:
        return f"no skill '{name}' of yours here"
    u = _usage(root)
    rec = u.setdefault(name, {})
    rec["pinned"] = on
    _save_usage(root, u)
    _ledger(root, "pin" if on else "unpin", name)
    return (f"'{name}' pinned — offered to the model on every turn" if on
            else f"'{name}' unpinned")


def status(eng: Path | None) -> str:
    from .skill_pick import outcomes
    out = outcomes()
    pins = pinned(eng)
    lines = []
    for root in roots(eng):
        names = _units(root)
        arch = sorted(d.name for d in (root / ARCHIVE).iterdir()) \
            if (root / ARCHIVE).is_dir() else []
        lines.append(f"{root}: {len(names)} in use, {len(arch)} put aside")
        for n in names:
            r = out.get(n) or {}
            how = f"worked {r['ok']}/{r['runs']} times" if r.get("runs") else "not picked yet"
            lines.append(f"  {n}: {how}" + ("  [pinned]" if n in pins else ""))
        for n in arch:
            lines.append(f"  (put aside) {n}")
    return "\n".join(lines) or "no skills of yours yet"

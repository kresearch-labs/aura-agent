"""work_folder.py — where Aura makes things when the user didn't say.

Started where no project is (the home folder, a drive root), the first message
that is real work makes Aura ask where — a new folder under ~/Aura named after
the task, or any folder the user names (made only with their yes). Aura then
works only inside it; its own records stay in Aura's home, never mixed with the
user's files. Questions and chat need no folder.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# what Aura itself keeps in a notebook — anything else in the no-project
# notebook is the user's work from before this change
NOTEBOOK_ITEMS = {
    "artifacts", "audit.jsonl", "chains", "decided", "findings.jsonl", "memories",
    "notes.md", "pblocks", "pending", "prompt_history.jsonl", "receipts", "scope.json",
    "sessions", "sources", "tasks.json", "todos.jsonl", "workspace.json", "delegates",
    "processes", "graph.json", "changes.json", "constraints.md", "resolution.jsonl",
    "scorecard.json", "trajectories.jsonl", "ui_fallback.log", "actions.log", "salvage",
    "state.md", "report.md", "events.jsonl", "history.jsonl", "swallowed.jsonl",
    "cron.jsonl", "cron_folders.json", "hooks", "logs",
}
_FILLER = {
    "a", "an", "the", "me", "my", "make", "build", "create", "write", "code", "please",
    "can", "could", "would", "you", "i", "want", "need", "to", "for", "of", "and", "in",
    "with", "using", "use", "some", "new", "simple", "small", "let", "lets", "let's", "us",
    "help", "do", "that", "this", "it", "on", "start", "set", "up", "just", "now", "again",
}
_MOVED_FLAG = ".files_moved_offer"


def work_root() -> Path:
    """The parent of the folders Aura makes: ~/Aura (AURA_WORK_ROOT for tests)."""
    return Path(os.environ.get("AURA_WORK_ROOT") or (Path.home() / "Aura"))


def needs_folder(eng) -> bool:
    """This chat runs in the no-project notebook: real work needs a folder first."""
    try:
        from .paths import no_project_dir
        return Path(eng).resolve() == no_project_dir().resolve()
    except Exception:                                    # noqa: BLE001
        return False


def name_for(task: str) -> str:
    """A short folder name from the task: "make a snake game" → snake-game."""
    words = re.findall(r"[a-z0-9]+", str(task or "").lower())
    keep = [w for w in words if w not in _FILLER and len(w) > 1][:3]
    return "-".join(keep) or "my-project"


def suggest(task: str) -> Path:
    """~/Aura/<name>; a name already taken gets -2, -3 …"""
    base = work_root() / name_for(task)
    p, n = base, 2
    while p.exists() and any(p.iterdir()):
        p = base.with_name(f"{base.name}-{n}")
        n += 1
    return p


def from_answer(answer: str) -> Path | None:
    """What the user typed: a path (anywhere) or a name (a folder under ~/Aura)."""
    a = str(answer or "").strip().strip('"').strip("'")
    if not a:
        return None
    if a.startswith("~") or "/" in a or "\\" in a or re.match(r"^[A-Za-z]:", a):
        return Path(os.path.expanduser(a)).resolve()
    return work_root() / (re.sub(r"[^A-Za-z0-9._ -]+", "", a).strip().replace(" ", "-") or "my-project")


def not_usable(p: Path) -> str | None:
    """Why this folder can't be the work folder, or None."""
    from .paths import unsafe_workspace
    if p.exists() and not p.is_dir():
        return "a file, not a folder"
    return unsafe_workspace(p) if p.exists() else None


def move_chat(old_eng, new_eng, session_path) -> Path:
    """The chat (and what belongs to it: its images, its settings) moves to
    the new project's notebook. Returns the chat's new path."""
    src = Path(session_path)
    dst_dir = Path(new_eng) / "sessions"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in list(src.parent.glob(src.stem + "*")):
        target = dst_dir / item.name
        if not target.exists():
            shutil.move(str(item), str(target))
    return dst_dir / src.name


def users_items(eng) -> list[Path]:
    """The user's files in the no-project notebook (made before Aura asked
    where to work): everything that isn't Aura's own, hidden ones left out."""
    try:
        return sorted(p for p in Path(eng).iterdir()
                      if p.name not in NOTEBOOK_ITEMS and not p.name.startswith("."))
    except OSError:
        return []


def offer_due(eng) -> bool:
    return needs_folder(eng) and not (Path(eng) / _MOVED_FLAG).exists() and bool(users_items(eng))


def offered(eng, answer: str) -> None:
    try:
        (Path(eng) / _MOVED_FLAG).write_text(answer, encoding="utf-8")
    except OSError:
        pass


def move_items(eng, dest: Path) -> list[str]:
    """Move the user's files out of Aura's folder into `dest`. Names that
    are already there are left where they were (never overwritten)."""
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in users_items(eng):
        target = dest / p.name
        if target.exists():
            continue
        try:
            shutil.move(str(p), str(target))
            moved.append(p.name)
        except OSError:
            pass
    return moved

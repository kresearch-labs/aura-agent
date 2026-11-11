"""session_store.py — named sessions with list/switch/resume/delete.

Sessions already persist as jsonl per engagement; this adds the operator
surface: list with previews, resume by index or name, delete, rename.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _candidate_files(engagement_root: Path) -> list[Path]:
    """sessions/ subdir first; root-level *.jsonl as legacy fallback.: internal
    scratch sessions (machine state, not user history) never surface —
    RESERVED_NAMES filtered here.
    """
    root = Path(engagement_root)
    out = [p for p in (root / "sessions").glob("*.jsonl")
           if p.stem.rsplit("-", 2)[-1] not in RESERVED_NAMES
           and not any(r in p.stem for r in RESERVED_NAMES)]
    # root-level legacy fallback ALSO filters reserved names (the ledger
    # events.jsonl lives at root — user catch: it surfaced as 'last session')
    out += [p for p in root.glob("*.jsonl")
            if p.stem not in RESERVED_NAMES
            and not any(r in p.stem for r in RESERVED_NAMES)
            and p not in out
            and _looks_like_session(p)]
    return out


_SESSION_ROLES = {"system", "user", "assistant", "tool"}


def _looks_like_session(p: Path, probe: int = 5) -> bool:
    """A root-level *.jsonl is a legacy chat session only if its records look like
    chat messages. The name denylist below kept missing data files —
    findings.jsonl surfaced as a session, and sessions or resume could switch
    the chat INTO it, appending chat turns to the findings store. Content beats
    names: findings / todos / audit / event records never carry a chat 'role'.
    """
    try:
        with p.open(encoding="utf-8") as f:
            for _ in range(probe):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("role") in _SESSION_ROLES:
                    return True
    except OSError:
        return False
    return False


# Machine-internal session name stems — never user history. 'events' added
# (user catch): events.jsonl is the LEDGER, not a chat session — it must never
# appear as 'last session' in the arrival card.
RESERVED_NAMES = {"twoask", "referee", "selftest", "smoke", "va",
                  "events", "audit"}   # audit.jsonl = the ledger, not a chat


LIST_CACHE = ".list-cache.json"   # Per chat: how far it was read, and what it said
_LIST_CACHE_V = 1
_TAIL = 1024


def _new_scan() -> dict:
    return {"count": 0, "first_user": "", "last_ts": None, "first_ts": None, "title": "",
            "chars": 0, "meta": {}, "flags": {},
            "use": {"in": 0, "out": 0, "calls": 0, "models": []},   # What the chat used
            "tasks": [],
            "files": []}


def _scan_record(st: dict, r: dict) -> None:
    from .notes import is_users_words, users_words
    use = st["use"]
    if r.get("record") == "title":
        st["title"] = str(r.get("title") or "")
        return
    if r.get("record") == "meta":
        st["meta"] = st["meta"] or {k: r.get(k) for k in ("id", "parent")}
        return
    if r.get("record") == "flags":
        st["flags"].update(r.get("flags") or {})
        return
    if r.get("record") == "settings":
        mdl = (r.get("settings") or {}).get("model")
        if mdl and mdl not in use["models"]:
            use["models"].append(mdl)
        return
    u = r.get("usage") or {}
    if isinstance(u, dict):
        use["in"] += int(u.get("prompt_tokens") or 0)
        use["out"] += int(u.get("completion_tokens") or 0)
    use["calls"] += len(r.get("tool_calls") or [])
    if r.get("record") == "compact":
        st["chars"] = len(str(r.get("summary", "")))     # The summary replaced the rest
        return
    st["count"] += 1
    if is_users_words(r):
        said = " ".join(users_words(r).split())
        if not st["first_user"]:
            st["first_user"] = said[:60]
        if said and not _small_talk(said) and said not in st["tasks"]:
            st["tasks"].append(said)
    for c in r.get("tool_calls") or []:
        fn = (c or {}).get("function") or {}
        if fn.get("name") in ("write_file", "edit_file"):
            try:
                path = str(json.loads(fn.get("arguments") or "{}").get("path") or "")
            except (json.JSONDecodeError, AttributeError):
                path = ""
            if path and path not in st["files"]:
                st["files"].append(path)
    if r.get("ts"):
        st["first_ts"] = st["first_ts"] or r["ts"]
        st["last_ts"] = r["ts"]
    st["chars"] += len(str(r.get("content") or "")) + len(str(r.get("tool_calls") or ""))


def _tail_of(f, offset: int) -> str:
    import hashlib
    n = min(_TAIL, offset)
    f.seek(offset - n)
    return hashlib.sha256(f.read(n)).hexdigest()


def _scan_file(path: Path, cached: dict | None) -> dict:
    """What a chat file says, reading only what was added since the last
    listing (a chat file only grows; one that was rewritten — a rewind — is
    read again from the start)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        start, st = 0, _new_scan()
        if cached and 0 < cached.get("offset", 0) <= size \
                and _tail_of(f, cached["offset"]) == cached.get("tail"):
            start, st = cached["offset"], cached["state"]
        f.seek(start)
        end = start
        for raw in f:
            if not raw.endswith(b"\n"):
                break                        # A line still being written: next time
            end += len(raw)
            try:
                r = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                _scan_record(st, r)
        tail = _tail_of(f, end) if end else ""
    return {"offset": end, "tail": tail, "state": st}


def list_sessions(engagement_root: Path) -> list[dict]:
    """Last used first (the time of the last message), with message count,
    the user's first words, when it was made and how big it is."""
    root = Path(engagement_root)
    cache_path = root / "sessions" / LIST_CACHE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("v") != _LIST_CACHE_V:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    old = cache.get("files") or {}
    new: dict = {}
    out = []
    for f in _candidate_files(engagement_root):
        try:
            scan = _scan_file(f, old.get(f.name))
        except OSError:
            continue
        new[f.name] = scan
        st = scan["state"]
        meta, flags, last_ts, first_ts = st["meta"], st["flags"], st["last_ts"], st["first_ts"]
        used = last_ts or f.stat().st_mtime
        out.append({"file": f.name, "id": str(meta.get("id") or chat_id(f.name)),
                    "parent": meta.get("parent"), "pinned": bool(flags.get("pinned")),
                    "archived": bool(flags.get("archived")), "usage": st["use"],
                    "active_s": round((last_ts or 0) - (first_ts or 0)) if first_ts and last_ts else 0,
                    "messages": st["count"], "preview": st["first_user"],
                    "tasks": st["tasks"], "files": st["files"],
                    "title": st["title"], "mtime": f.stat().st_mtime,
                    "made_ts": first_ts or f.stat().st_mtime, "used_ts": used,
                    "tokens": st["chars"] // 4,
                    "age_min": round((time.time() - used) / 60, 1)})
    if new != old and (root / "sessions").is_dir():
        try:
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": _LIST_CACHE_V, "files": new}, ensure_ascii=False),
                           encoding="utf-8")
            import os
            os.replace(tmp, cache_path)
        except OSError:
            pass                              # The cache is only a shortcut
    out.sort(key=lambda s: s["used_ts"], reverse=True)
    return out


# ------------------------------------------------------------------ lookup --
def resolve(engagement_root: Path, ref: str, include_archived: bool = True
            ) -> tuple[Path | None, list[dict]]:
    """A chat from what the user typed: its id (#a1b2c3, or a unique start of it,
    like git), "latest", a number from the list, its title, or its file name.
    (path, []) found; (None, candidates) when several match; (None, []) when
    none does.
    """
    ref = (ref or "").strip()
    root = Path(engagement_root)
    chats = [s for s in list_sessions(root) if include_archived or not s.get("archived")]
    if not ref or not chats:
        return None, []

    def path_of(s):
        for base in (root / "sessions", root):
            if (base / s["file"]).exists():
                return base / s["file"]
        return None

    low = ref.lower()
    if low in ("latest", "last"):          # The newest you work in: not an archived one
        live = [s for s in chats if not s.get("archived")] or chats
        return path_of(live[0]), []
    if ref.isdigit() and int(ref) < len(chats) and not low.startswith("#"):
        return path_of(chats[int(ref)]), []
    want = low.lstrip("#")
    exact = [s for s in chats if s.get("id") == want]
    if exact:
        return path_of(exact[0]), []
    if len(want) >= 3:
        pre = [s for s in chats if str(s.get("id", "")).startswith(want)]
        if len(pre) == 1:
            return path_of(pre[0]), []
        if len(pre) > 1:
            return None, pre
    for s in chats:
        if s["file"] == ref or Path(s["file"]).stem == ref or \
                (s.get("title") or "").lower() == low:
            return path_of(s), []
    part = [s for s in chats if low in Path(s["file"]).stem.lower()
            or low in (s.get("title") or "").lower()]
    if len(part) == 1:
        return path_of(part[0]), []
    return None, part


# ------------------------------------------------------------------- trash --
TRASH = ".trash"


def trash_dir(engagement_root: Path) -> Path:
    return Path(engagement_root) / "sessions" / TRASH


def trash_chat(engagement_root: Path, path: Path) -> Path:
    """Delete = into the project's chat trash."""
    import os
    d = trash_dir(engagement_root)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / path.name
    n = 1
    while dest.exists():
        dest = d / f"{path.stem}-{n}{path.suffix}"
        n += 1
    path.replace(dest)
    os.utime(dest, None)                   # The time it was deleted
    try:                                   # Its view (a shortcut, rebuilt when needed)
        path.with_name(path.stem + ".view.json").unlink()
    except OSError:
        pass
    return dest


def trashed(engagement_root: Path) -> list[dict]:
    """The chats in the trash, newest first: file, id, title, deleted ts."""
    d = trash_dir(engagement_root)
    out = []
    for f in d.glob("*.jsonl") if d.exists() else []:
        cid, title, first = "", "", ""
        try:
            from .notes import is_users_words, users_words
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("record") == "meta" and not cid:
                    cid = str(r.get("id") or "")
                elif r.get("record") == "title":
                    title = str(r.get("title") or "")
                elif not first and is_users_words(r):
                    first = users_words(r)[:60]
        except OSError:
            continue
        out.append({"file": f.name, "path": f, "id": cid or chat_id(f.name),
                    "title": title or first, "deleted_ts": f.stat().st_mtime})
    out.sort(key=lambda s: s["deleted_ts"], reverse=True)
    return out


def restore_chat(engagement_root: Path, ref: str) -> Path | None:
    """Out of the trash, back in the list (by id, start of it, or title)."""
    want = (ref or "").lower().lstrip("#")
    hits = [s for s in trashed(engagement_root)
            if s["id"] == want or (len(want) >= 3 and s["id"].startswith(want))
            or (s["title"] or "").lower() == want]
    if len(hits) != 1:
        return None
    src = hits[0]["path"]
    dest = Path(engagement_root) / "sessions" / src.name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}-restored{dest.suffix}")
    src.replace(dest)
    return dest


def empty_trash(engagement_root: Path, older_than_days: float | None = None) -> int:
    """Remove trashed chats for good (older_than_days: only those deleted
    longer ago). Returns how many."""
    n = 0
    cut = time.time() - older_than_days * 86400 if older_than_days is not None else None
    for s in trashed(engagement_root):
        if cut is None or s["deleted_ts"] < cut:
            try:
                s["path"].unlink()
                n += 1
            except OSError:
                pass
    return n


def chat_id(filename: str) -> str:
    """The id a chat file has (its meta record; else from its file name)."""
    from .sessions import legacy_id
    return legacy_id(filename)


def _small_talk(text: str) -> bool:
    """'continue', 'hi', 'thanks', 'yes' — not a task of their own."""
    w = text.lower().strip(" .!?")
    return len(w) < 3 or w in {"continue", "go", "go on", "next", "yes", "no", "ok", "okay",
                               "thanks", "thank you", "hi", "hello", "hey", "bye", "stop",
                               "what now", "what to do", "done", "sure", "y", "n"}


def resume_by_index(engagement_root: Path, index: int) -> Path | None:
    sessions = list_sessions(engagement_root)
    if 0 <= index < len(sessions):
        root = Path(engagement_root)
        name = sessions[index]["file"]
        for base in (root / "sessions", root):
            p = base / name
            if p.exists():
                return p
    return None


def resume_by_name(engagement_root: Path, name: str) -> Path | None:
    """Resume by exact filename, exact stem, or unique partial stem match.
    Ambiguous partial → None (caller lists candidates)."""
    root = Path(engagement_root)
    candidates = [p for p in _candidate_files(root)]
    # exact filename / stem, or the chat's id (#a1b2c3)
    want_id = name.lower().lstrip("#")
    for p in candidates:
        if p.name == name or p.stem == name or chat_id(p.name) == want_id:
            return p
    # unique partial stem
    hits = [p for p in candidates if name.lower() in p.stem.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


def search_sessions(engagement_root: Path, query: str) -> list[dict]:
    """Filter list_sessions() by title (filename stem) OR first-user-line
    preview match. Most recent first. Plain substring, case-insensitive."""
    q = query.lower()
    return [s for s in list_sessions(engagement_root)
            if q in s["file"].lower() or q in s["preview"].lower()]


def delete_session(engagement_root: Path, name: str) -> bool:
    """Into the trash (restorable) — never removed for good here."""
    root = Path(engagement_root)
    for base in (root / "sessions", root):
        p = base / name
        if p.exists():
            trash_chat(root, p)
            return True
    return False

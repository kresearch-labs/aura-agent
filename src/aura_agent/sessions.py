"""sessions.py — jsonl session persistence + tail-replay resume.

One file per session: engagement/sessions/<ts>-<slug>.jsonl
Each line: {seq, ts, role, content?, tool_calls?, tool_call_id?, name?, meta?}
plus records: {"record": "compact", ...} and {"record": "settings",
"settings": {...}} — everything this session runs with (model, budget,
features, mode, ...); the last one wins, so /resume brings it back.
Resume = rebuild the message list → continue. Crash-safe: every line is
self-contained, partial trailing line is ignored on load.

The whole chat stays in the jsonl. Opening a chat reads only the recent
part: each compaction also writes <name>.view.json — the message list right
after it, and where in the jsonl that point is — so a chat of weeks opens
from its last compaction instead of from its first line. A view that does
not match the jsonl is ignored and the chat is read from the start.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

_IS_WIN = os.name == "nt"

VIEW_VERSION = 1
VIEW_TAIL = 4096               # Bytes before the view's offset that must still match
VIEW_FROM_BYTES = 256 * 1024   # A chat read in full this big gets a view for next time


def _compact_brief(rec: dict) -> dict:
    """What is kept in memory about one compaction (the summary itself is
    in the message list and in the jsonl)."""
    return {k: rec.get(k) for k in ("seq", "ts", "kept", "dropped")}


def locked_append(path: Path, line: str, wait: float = 2.0) -> int:
    """Append one line under a lock every Aura writer takes. Returns the file size
    after.
    """
    data = line.encode("utf-8")
    with open(path, "ab") as f:
        got = False
        t0 = time.monotonic()
        try:
            if _IS_WIN:
                import msvcrt
                while True:
                    try:
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)   # Byte 0 = the chat's lock
                        got = True
                        break
                    except OSError:
                        if time.monotonic() - t0 > wait:
                            break
                        time.sleep(0.02)
                f.seek(0, os.SEEK_END)
            else:
                import fcntl
                while True:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except OSError:
                        if time.monotonic() - t0 > wait:
                            break
                        time.sleep(0.02)
            f.write(data)
            f.flush()
            return f.tell()
        finally:
            if got:
                try:
                    if _IS_WIN:
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def legacy_id(filename: str) -> str:
    """The id of a chat made before ids were written in the file: from its
    first file name (6 hex characters)."""
    import hashlib
    return hashlib.sha1(Path(filename).stem.encode("utf-8")).hexdigest()[:6]


def new_id(sessions_dir: Path | None = None) -> str:
    """A new chat's id: 6 random hex characters, not one already in use in
    this project (a 7th when the 6 ever run out of room)."""
    import secrets
    used: set[str] = set()
    try:
        if sessions_dir is not None and Path(sessions_dir).exists():
            from .session_store import list_sessions
            used = {s.get("id", "") for s in list_sessions(Path(sessions_dir).parent)}
    except Exception:                                # noqa: BLE001
        used = set()
    for n in (6, 6, 6, 7, 8):
        cid = secrets.token_hex(4)[:n]
        if not any(u.startswith(cid) or cid.startswith(u) for u in used if u):
            return cid
    return secrets.token_hex(6)


def slugify(s: str, n: int = 24) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "session"


# what a message keeps in memory; `by` (user | aura) is stripped before
# anything is sent to the model (notes.for_wire)
_MSG_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name", "by",
             "native",      # wire_native: thinking / reasoning to send back
             "talk")        # Said in discuss mode — sent with the [TALK] label # wire_native: thinking / reasoning to send back


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.messages: list[dict] = []
        self.compacts: list[dict] = []   # Compaction history (append-only)
        self.settings: dict = {}         # What this session runs with
        self.title: str = ""              # Set by a title record
        # The chat's own record.
        self.meta: dict = {}
        self.flags: dict = {}
        # a new chat holds its lines until the user's first message: opening
        # Aura and leaving writes nothing (hold())
        self._held: list[str] | None = None
        # another Aura writing this same chat (a second terminal on it):
        # noticed by the file growing between our own writes
        self.other_writer = False
        self._size = None
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        """Rebuild the message list; tolerate a partial tail. `compact` records
        rewrite the message list in place (: compaction persists across resume
        — no resurrection of compacted turns). Starts from the view of the last
        compaction when it matches the file; else from the first line.
        """
        if not self.path.exists():
            return
        start = self._open_view()
        with self.path.open("rb") as f:
            f.seek(start)
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Partial write from a crash — skip
                self._take(rec)
        if not start and self.compacts and self.path.stat().st_size > VIEW_FROM_BYTES:
            self._save_view()               # The next open starts from here

    def _take(self, rec: dict) -> None:
        """One jsonl record into the in-memory state."""
        self.seq = max(self.seq, rec.get("seq", 0))
        if rec.get("record") == "compact":
            self.compacts.append(_compact_brief(rec))
            self._apply_compact_record(rec)
            return
        if rec.get("record") == "settings":
            if isinstance(rec.get("settings"), dict):
                self.settings = rec["settings"]
            return
        if rec.get("record") == "title":
            self.title = str(rec.get("title") or "")
            return
        if rec.get("record") == "meta":
            if not self.meta:              # The first one is the chat's
                self.meta = {k: rec.get(k) for k in ("id", "created", "parent", "forked_at")}
            return
        if rec.get("record") == "flags":
            self.flags.update({k: bool(v) for k, v in (rec.get("flags") or {}).items()})
            return
        msg = {k: v for k, v in rec.items()
               if k in _MSG_KEYS and v is not None}
        if rec.get("ts") and msg.get("role") == "user":
            from .notes import is_users_words
            if is_users_words(rec):
                msg["ts"] = rec["ts"]
        if msg.get("role"):
            self.messages.append(msg)

    # ------------------------------------------------------------- the view
    @property
    def view_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".view.json")

    def _tail_hash(self, offset: int) -> str:
        """A fingerprint of the bytes just before `offset` (proves the jsonl
        is still the file the view was made from)."""
        n = min(VIEW_TAIL, offset)
        with self.path.open("rb") as f:
            f.seek(offset - n)
            return hashlib.sha256(f.read(n)).hexdigest()

    def _open_view(self) -> int:
        """Take the state from the view; the byte offset to read on from
        (0 = no usable view: read the whole file)."""
        try:
            v = json.loads(self.view_path.read_text(encoding="utf-8"))
            off = int(v["offset"])
            if (v.get("v") != VIEW_VERSION or off <= 0
                    or off > self.path.stat().st_size or v.get("tail") != self._tail_hash(off)):
                return 0
            self.seq = int(v["seq"])
            self.messages = list(v["messages"])
            self.compacts = list(v.get("compacts") or [])
            self.settings = dict(v.get("settings") or {})
            self.title = str(v.get("title") or "")
            self.meta = dict(v.get("meta") or {})
            self.flags = dict(v.get("flags") or {})
            return off
        except (OSError, ValueError, KeyError, TypeError):
            self.seq, self.messages, self.compacts = 0, [], []
            self.settings, self.title, self.meta, self.flags = {}, "", {}, {}
            return 0

    def _save_view(self) -> None:
        """Write the view: the state as a full read would rebuild it now,
        and the jsonl's size at this moment."""
        try:
            off = self.path.stat().st_size
            v = {"v": VIEW_VERSION, "offset": off, "tail": self._tail_hash(off),
                 "seq": self.seq, "messages": self.messages, "compacts": self.compacts,
                 "settings": self.settings, "title": self.title, "meta": self.meta,
                 "flags": self.flags}
            tmp = self.view_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.view_path)
        except (OSError, TypeError, ValueError):
            pass                            # The view is only a shortcut

    def drop_view(self) -> None:
        try:
            self.view_path.unlink()
        except OSError:
            pass

    def in_view(self) -> dict:
        """How much of this chat the model has in front of it now: the
        messages since the last compaction, from when, and how many earlier
        parts were summarized (the whole chat stays on disk)."""
        first = next((m.get("ts") for m in self.messages if m.get("ts")), None)
        return {"messages": sum(1 for m in self.messages if m.get("role") != "system"),
                "since": first, "summarized": len(self.compacts)}

    def _apply_compact_record(self, rec: dict) -> None:
        """Replay a persisted compaction: [summary] + kept non-system tail
        after the last system prompt. Records the exact kept-count, so
        replay is deterministic without trusting a stored message list."""
        keep_n = int(rec.get("kept", 0))
        msgs = self.messages
        system = [m for m in msgs if m["role"] == "system"]
        rest = [m for m in msgs if m["role"] != "system"]
        rest = rest[-keep_n:] if keep_n else rest
        summary = {"role": "system", "content": rec.get("summary", "")}
        self.messages = ([summary] if summary["content"] else []) \
            + system + rest

    def record_compact(self, summary_content: str, kept: int,
                       dropped: int) -> dict:
        """Persist a compaction as an append-only record. The summary block
        itself is the record — replay rebuilds deterministically."""
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3),
               "record": "compact", "summary": summary_content,
               "kept": kept, "dropped": dropped}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self._held is None:
            # the view holds what a full read would rebuild right here (the
            # caller then sets its own live list; replay must not depend on it)
            live = self.messages
            self.messages = self._replay_compact(list(live), rec)
            self.compacts.append(_compact_brief(rec))
            self._save_view()
            self.messages = live
        return rec

    def record_title(self, title: str, by: str = "") -> None:
        """A chat's title (written by the helper model, or the main one)."""
        title = " ".join(str(title).split())[:60]
        if not title or title == getattr(self, "title", ""):
            return
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "record": "title",
               "title": title, "by": by}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.title = title

    @property
    def id(self) -> str:
        """The chat's id: written in the file when it was made; a chat from
        before ids has the one its first file name gives (chat_id), written
        the first time it is opened (ensure_meta)."""
        return str(self.meta.get("id") or legacy_id(self.path.name))

    def ensure_meta(self, parent: str = "", forked_at: int = 0) -> None:
        """Write the chat's own record if it has none yet."""
        if self.meta:
            return
        has = self._held is None and self.path.exists() and self.path.stat().st_size > 0
        cid = legacy_id(self.path.name) if has else new_id(self.path.parent)
        self.meta = {"id": cid, "created": round(time.time(), 3),
                     "parent": parent or None, "forked_at": forked_at or None}
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "record": "meta", **self.meta}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")

    def record_flags(self, **flags) -> None:
        """pinned / archived (the newest record wins on load)."""
        flags = {k: bool(v) for k, v in flags.items()}
        if all(self.flags.get(k, False) == v for k, v in flags.items()):
            return
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "record": "flags", "flags": flags}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.flags.update(flags)

    def record_settings(self, settings: dict) -> None:
        """Remember what this session now runs with (append-only; the
        newest record wins on load). No-op when nothing changed."""
        if settings == self.settings:
            return
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3),
               "record": "settings", "settings": settings}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.settings = dict(settings)

    # ------------------------------------------------------------------ write
    def hold(self) -> None:
        """Keep this chat's lines in memory until the user says something."""
        if self._held is None:
            self._held = []

    @property
    def held(self) -> bool:
        return self._held is not None

    def _flush(self) -> None:
        lines, self._held = self._held or [], None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for ln in lines:
            self._write(ln)

    def _write(self, line: str) -> None:
        if self._held is not None:
            self._held.append(line)
            return
        try:
            before = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            before = None
        if self._size is not None and before is not None and before != self._size:
            self.other_writer = True        # Someone else appended since our last write
        self._size = locked_append(self.path, line)

    def append(self, role: str, **kw) -> dict:
        if self._held is not None and role == "user" and kw.get("by") != "aura":
            self._flush()                    # The first real message: the chat exists now
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "role": role, **kw}
        self._write(json.dumps(rec, ensure_ascii=False) + "\n")
        msg = {k: v for k, v in rec.items()
               if k in _MSG_KEYS and v is not None}
        if role == "user" and kw.get("by") == "user":
            msg["ts"] = rec["ts"]           # When it arrived (notes.for_wire stamps it)
        self.messages.append(msg)
        return rec

    def note(self, content, **kw) -> dict:
        """Aura's own note to the model: labelled, and stored as Aura's — never
        mixed up with what the user typed.
        """
        from .notes import aura_note
        return self.append("user", content=aura_note(content), by="aura", **kw)

    # ------------------------------------------------------------------ meta
    def last_user_task(self) -> str:
        # The user's own words — never one of Aura's notes
        from .notes import is_users_words, users_words
        for m in reversed(self.messages):
            if is_users_words(m):
                c = m.get("content")
                if isinstance(c, list):          # Text + pasted images
                    return " ".join(p.get("text", "") for p in c
                                    if isinstance(p, dict) and p.get("type") == "text")
                return users_words(m) if str(c).startswith(
                    ("[STEER]", "[ASK]", "[CHANGE]", "[LATER]", "(typed by the user)")) else str(c)
        return ""

    def stats(self) -> dict:
        roles = {}
        for m in self.messages:
            roles[m["role"]] = roles.get(m["role"], 0) + 1
        return {"file": self.path.name, "messages": len(self.messages),
                "seq": self.seq, **roles}

    # ------------------------------------------------- time travel ----
    def _records(self) -> list[dict]:
        out = []
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def state_at_seq(self, seq: int) -> dict:
        """Full-state restore: rebuild (messages, seq, compacts) from the jsonl
        prefix up to and including `seq`. Pure replay — identical logic to
        _load, bounded by seq.
        """
        messages: list[dict] = []
        compacts: list[dict] = []
        max_seq = 0
        settings: dict = {}
        for rec in self._records():
            r_seq = rec.get("seq", 0)
            if r_seq > seq:
                break
            max_seq = max(max_seq, r_seq)
            if rec.get("record") == "settings":
                settings = rec.get("settings") or {}
                continue
            if rec.get("record") == "compact":
                compacts.append(rec)
                messages = self._replay_compact(messages, rec)
                continue
            # the same keys a normal load keeps — `by` too, so after a rewind
            # Aura's notes are still told apart from the user's words
            msg = {k: v for k, v in rec.items() if k in _MSG_KEYS and v is not None}
            if rec.get("ts") and msg.get("role") == "user":
                from .notes import is_users_words
                if is_users_words(rec):
                    msg["ts"] = rec["ts"]
            if msg.get("role"):
                messages.append(msg)
        return {"messages": messages, "seq": max_seq, "compacts": compacts,
                "settings": settings}

    @staticmethod
    def _replay_compact(messages: list[dict], rec: dict) -> list[dict]:
        keep_n = int(rec.get("kept", 0))
        system = [m for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]
        rest = rest[-keep_n:] if keep_n else rest
        summary = {"role": "system", "content": rec.get("summary", "")}
        return ([summary] if summary["content"] else []) + system + rest

    def restore(self, seq: int) -> str:
        """Rewind the LIVE session to `seq`. The truncated tail is kept as a
        snapshot file (never destroyed); the jsonl is rewritten with the
        prefix; in-memory state is rebuilt. Returns the snapshot ref.
        """
        seq = max(0, int(seq))
        # snapshot the full current file first (the tail is never destroyed)
        snap_dir = self.path.parent / ".checkpoints"
        snap_dir.mkdir(exist_ok=True)
        snap = snap_dir / (f"{self.path.stem}-pre-rewind-"
                           f"{time.strftime('%H%M%S')}.jsonl")
        records = self._records()
        if self.path.exists():
            shutil.copy2(self.path, snap)
        state = self.state_at_seq(seq)
        self.drop_view()                    # The file is rewritten.
        with self.path.open("w", encoding="utf-8") as f:
            for rec in records:
                if rec.get("seq", 0) > seq:
                    continue
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            for rec in records:             # The chat's own records stay
                if rec.get("seq", 0) > seq and rec.get("record") in ("meta", "flags", "title"):
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.messages = state["messages"]
        self.seq = state["seq"]
        self.compacts = state["compacts"]
        self.settings = state["settings"]
        return str(snap)

    def fork(self, seq: int, sessions_dir: Path | None = None,
             label: str = "fork") -> "Session":
        """Branch this session at `seq` into a NEW session file. The original is
        untouched; the branch starts with the prefix state.
        """
        state = self.state_at_seq(seq)
        dest_dir = sessions_dir or self.path.parent
        branch = Session(dest_dir / (
            f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(label)}.jsonl"))
        branch.path.touch()
        with branch.path.open("w", encoding="utf-8") as f:
            for rec in self._records():
                if rec.get("seq", 0) > seq:
                    break
                if rec.get("record") in ("meta", "flags", "title"):
                    continue               # The parent's id, pins and title stay its own
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        branch.messages = state["messages"]
        branch.seq = state["seq"]
        branch.compacts = state["compacts"]
        branch.settings = state["settings"]
        branch.ensure_meta(parent=self.id, forked_at=seq)
        if self.title:
            branch.record_title(f"{self.title} (fork)", by="fork")
        return branch


def new_session(sessions_dir: Path, task: str, lazy: bool = False) -> Session:
    ts = time.strftime("%Y%m%d-%H%M%S")
    s = Session(sessions_dir / f"{ts}-{slugify(task)}.jsonl")
    n = 1
    while s.path.exists():
        s = Session(sessions_dir / f"{ts}-{n:02d}-{slugify(task)}.jsonl")
        n += 1
    if lazy:
        s.hold()                    # Written at the first message
    else:
        s.path.touch()              # Touch: empty session still listable
    s.ensure_meta()                 # Its id, from the first line on
    return s


def latest_session(sessions_dir: Path) -> Session | None:
    if not sessions_dir.exists():
        return None
    files = sorted(sessions_dir.glob("*.jsonl"))
    return Session(files[-1]) if files else None

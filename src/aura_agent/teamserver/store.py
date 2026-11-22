"""teamserver/store.py — the SQLite store (plan).

WAL + busy_timeout + BEGIN IMMEDIATE so many threads write safely; one
connection per thread; every write inside `write`; migrations forward only;
`now` is the one clock.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import schema

_local = threading.local()


def connect(path) -> sqlite3.Connection:
    """Open the store: WAL, bounded lock waits, foreign keys on, explicit
    transactions (we issue BEGIN ourselves), Row rows."""
    conn = sqlite3.connect(str(path), check_same_thread=False,
                           isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect_thread(path) -> sqlite3.Connection:
    """One connection per thread, all to the same file (plan)."""
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = str(path)
    if key not in conns:
        conns[key] = connect(path)
    return conns[key]


def _deliver_after_commit(callbacks) -> None:
    # Notification is best effort, never a second database transaction and
    # never a reason to turn a successful write into an HTTP 500.
    for callback in callbacks:
        try:
            callback()
        except Exception:                            # noqa: BLE001
            pass


def after_commit(conn: sqlite3.Connection, callback) -> None:
    """Run callback after THIS store.write commits, or now outside a writer.

    The thread-local per-connection frame prevents a rolled-back insert from
    leaking a stream event. Every database mutation still owns its own write;
    this is only a notification seam, with no new nested-write semantics.
    """
    frames = getattr(_local, "notification_frames", None)
    pending = frames.get(id(conn)) if frames is not None else None
    if pending is not None:
        pending.append(callback)
    else:
        _deliver_after_commit((callback,))


class write:
    """`with store.write(conn):` — BEGIN IMMEDIATE … COMMIT, ROLLBACK on an
    exception. Every write in the server goes through this."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.callbacks = []

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        frames = getattr(_local, "notification_frames", None)
        if frames is None:
            frames = {}
            _local.notification_frames = frames
        frames[id(self.conn)] = self.callbacks
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        frames = getattr(_local, "notification_frames", None)
        if frames is not None:
            frames.pop(id(self.conn), None)
        if exc_type is None:
            try:
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                # The write DIDN'T happen either way — a failed COMMIT must
                # roll back too, or the thread's connection sits mid-
                # transaction and every later read/write on it lies
                try:
                    self.conn.execute("ROLLBACK")
                finally:
                    raise
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        if exc_type is None:
            _deliver_after_commit(self.callbacks)
        return False


_ADD_COLUMN = re.compile(
    r"^[ \t]*ALTER TABLE[ \t]+(\w+)[ \t]+ADD COLUMN[ \t]+(\w+)\b[^;\n]*;[ \t]*$",
    re.IGNORECASE | re.MULTILINE)


def _without_present_columns(conn: sqlite3.Connection, script: str) -> str:
    """A step that adds a column runs safely twice: an ADD COLUMN line whose
    column is already there is left out (SQLite has no IF NOT EXISTS). A
    line for a table this store doesn't have (and the step doesn't create)
    is left out too: there is nothing to add the column to."""
    created = {t.lower() for t in re.findall(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", script, re.IGNORECASE)}

    def keep(m):
        table = m.group(1)
        if table.lower() in created:
            return m.group(0)                    # Made earlier in this step
        cols = {r[1].lower() for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            return ""                            # No such table here
        return "" if m.group(2).lower() in cols else m.group(0)
    return _ADD_COLUMN.sub(keep, script)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply schema.VERSIONS in order; forward only; sets user_version.
    Returns the version now in place. (executescript commits any pending
    transaction, so the version runs as ONE atomic script of its own.)"""
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    for n in sorted(schema.VERSIONS):
        if n <= v:
            continue
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + _without_present_columns(conn, schema.VERSIONS[n]) +
            f"\nPRAGMA user_version = {int(n)};\nCOMMIT;")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def now() -> float:
    """The one clock — tests freeze by monkeypatching this."""
    import time
    return time.time()


# ------------------------------------------------ server-side secrets
# The team server has no OS key store of its own (devices' keys persist as rows
# here, by the same convention). Real webhook URLs / tokens / signing secrets
# live in the server_secrets table; projects.settings and every API answer
# carry only the pointer key and the sha256 fingerprint — a value is written
# here and read at send/verify time, never shown again.
def secret_fp(value: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _has_secrets(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND"
        " name = 'server_secrets'").fetchone() is not None


def secret_put(conn: sqlite3.Connection, key: str, value: str) -> str:
    """Store the real value under `key` (inside the caller's store.write());
    returns the fingerprint — the only thing a UI or log may carry."""
    fp = secret_fp(value)
    conn.execute(
        "INSERT INTO server_secrets (key, value, fingerprint, created)"
        " VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " value = excluded.value, fingerprint = excluded.fingerprint",
        (key, str(value), fp, now()))
    return fp


def secret_get(conn: sqlite3.Connection, key: str) -> str:
    if not key or not _has_secrets(conn):
        return ""
    row = conn.execute("SELECT value FROM server_secrets WHERE key = ?",
                       (key,)).fetchone()
    return str(row["value"]) if row else ""


def secret_drop(conn: sqlite3.Connection, key: str) -> None:
    if key and _has_secrets(conn):
        conn.execute("DELETE FROM server_secrets WHERE key = ?", (key,))


def bootstrap(conn: sqlite3.Connection, team_name: str, owner_name: str,
              owner_pubkey: bytes, device_name: str, cert_fp: str) -> dict:
    """."""
    from . import log as logmod
    ts = now()
    with write(conn):
        conn.execute(
            "INSERT INTO team (id, name, created, cert_fp) VALUES (1,?,?,?)",
            (team_name, ts, cert_fp))
        cur = conn.execute(
            "INSERT INTO people (name, display, role, created) VALUES (?,?,?,?)",
            (owner_name, owner_name, "owner", ts))
        owner = cur.lastrowid
        # 's 'linked-app' sentinel is NOT created here: inserting it at
        # bootstrap would renumber every later person (name→id stability is
        # load-bearing for tests and audits). Existing teams get it from the
        # migration; fresh teams get it lazily on first ingest via
        # chatlinks.link_person_id — it is never person id 1 either way.
        device_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO devices (id, person, name, pubkey, created, last_seen)"
            " VALUES (?,?,?,?,?,?)",
            (device_id, owner, device_name, owner_pubkey, ts, ts))
        cur = conn.execute(
            "INSERT INTO projects (slug, name, created, created_by)"
            " VALUES ('general', 'general', ?, ?)", (ts, owner))
        project = cur.lastrowid
        conn.execute(
            "INSERT INTO project_members (project, person, role) VALUES (?,?,?)",
            (project, owner, "manager"))
        # Every project has its chat room, made with the project
        conn.execute(
            "INSERT INTO rooms (kind, name, project, made_by, ts)"
            " VALUES ('project', '#general', ?, ?, ?)", (project, owner, ts))
        for key, type, name, pos in schema.DEFAULT_STATUSES:
            conn.execute(
                "INSERT INTO statuses (project, key, type, name, position)"
                " VALUES (?,?,?,?,?)", (project, key, type, name, pos))
        source, ev_id = logmod.server_event()
        logmod.append(conn, None, source, ev_id, "team.created", team_name,
                      owner, {"team": team_name, "owner": owner_name}, ts)
    return {"owner": owner, "device": device_id, "project": project}

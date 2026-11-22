"""teamserver/backup.py — online backups and restore (plan).

`backup_now` copies the live database page by page (the server stays up,
writers keep writing), writes the chain heads beside it as a witness, and keeps
the last 14 — older ones are MOVED to backups/old/, never deleted: nothing is
cleaned automatically, the admin decides.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

KEEP = 14


def backup_now(conn: sqlite3.Connection, dest_dir) -> Path:
    """One online backup + head file; returns the backup's path."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = dest / f"team-{stamp}.db"
    n = 2
    while out.exists():
        out = dest / f"team-{stamp}-{n}.db"
        n += 1
    dst = sqlite3.connect(str(out))
    try:
        conn.backup(dst)
        dst.row_factory = sqlite3.Row
        rows = dst.execute("SELECT chain, seq, hash FROM log_heads").fetchall()
    finally:
        dst.close()
    heads = {r["chain"]: {"seq": r["seq"], "hash": r["hash"]} for r in rows}
    (dest / (out.name + ".head.json")).write_text(
        json.dumps(heads, indent=1, sort_keys=True), encoding="utf-8")
    _prune(dest)
    return out


def _prune(dest: Path) -> None:
    """Keep the newest KEEP backups; move older ones to backups/old/."""
    olds = dest / "old"
    backups = sorted(dest.glob("team-*.db"))
    for old in backups[:-KEEP] if len(backups) > KEEP else []:
        olddir = dest.parent / "backups" if dest.name != "backups" else dest
        olddir = olddir / "old"
        olddir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(olddir / old.name))
        head = old.with_suffix(".db.head.json")
        if head.exists():
            shutil.move(str(head), str(olddir / head.name))


def restore(backup_file, team_dir, live_db) -> Path:
    """Only with the server stopped. Keeps the current database as a
    before-restore copy, then puts the backup in its place. Returns the
    before-restore copy's path."""
    backup_file = Path(backup_file)
    team_dir = Path(team_dir)
    live_db = Path(live_db)
    bdir = team_dir / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    keep = bdir / f"before-restore-{stamp}.db"
    if live_db.exists():
        shutil.copy2(live_db, keep)
    for aux in (live_db.with_suffix(".db-wal"), live_db.with_suffix(".db-shm"),
                live_db.parent / (live_db.name + "-wal"),
                live_db.parent / (live_db.name + "-shm")):
        try:
            aux.unlink()
        except FileNotFoundError:
            pass
    shutil.copy2(backup_file, live_db)
    head = backup_file.with_suffix(".db.head.json")
    if head.exists():
        shutil.copy2(head, live_db.with_suffix(".db.head.json"))
    return keep


def due(dest_dir) -> bool:
    """True when the newest backup is older than a day (or none exists) — the
    server backs up at start and then daily.
    """
    backups = sorted(Path(dest_dir).glob("team-*.db"))
    if not backups:
        return True
    age = time.time() - backups[-1].stat().st_mtime
    return age > 86400

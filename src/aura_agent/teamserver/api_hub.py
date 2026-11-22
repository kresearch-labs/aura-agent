"""teamserver/api_hub.py — hand over, move, new address.

The move bundle = an online backup of the database + the certificate + its key
+ the team settings, SEALED for the receiving person's device (Ed25519 public
key converted to Curve25519, nacl SealedBox). Because the certificate moves
with it, the fingerprint stays the same — nobody needs a new invite.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile

from . import auth
from . import log as logmod
from . import store
from .api_people import _actor


def _db():
    from . import teamserver_db
    return teamserver_db.open_team()


def hub_bundle(handler, match, body) -> None:
    """GET api/v1/hub/bundle?device=<id> — signed, hub.handover."""
    conn = _db()
    actor = _actor(conn, handler, body, need="any")
    if actor is None:
        return
    from .perms import can
    if not can(conn, actor, "hub.handover"):
        handler._error(403, "only the owner hands the hub over")
        return
    import re as _re
    qs = handler.path.split("?", 1)
    params = dict(p.split("=", 1) for p in qs[1].split("&") if "=" in p) \
        if len(qs) > 1 else {}
    device_id = params.get("device", "")
    dev = conn.execute(
        "SELECT d.pubkey, d.revoked_at, p.name AS person_name FROM devices d"
        " JOIN people p ON p.id = d.person WHERE d.id = ?",
        (device_id,)).fetchone()
    if dev is None or dev["revoked_at"]:
        handler._error(404, "no such open device to seal the bundle for")
        return
    from .teamserver_db import team_dir
    d = team_dir()
    # the parts: online backup, certificate, key, team settings
    from . import backup as B
    import tempfile
    # the team row may not exist yet (a hub set up before it had a name);
    # read it the way every other reader here does — no row, no name
    team_row = conn.execute("SELECT name FROM team WHERE id = 1").fetchone()
    with tempfile.TemporaryDirectory() as td:
        bak = B.backup_now(conn, td)
        files = {"team.db": bak.read_bytes(),
                 "team-cert.pem": (d / "team-cert.pem").read_bytes(),
                 "team-key.pem": (d / "team-key.pem").read_bytes(),
                 "team.settings.json": json.dumps(
                     {"team": team_row["name"] if team_row else "",
                      "fp": handler.server.server_fp},
                     sort_keys=True).encode()}
    # Seal for the receiver's device: Ed25519 -> Curve25519
    import nacl.public
    import nacl.bindings as _nb
    curve_pk = nacl.public.PublicKey(
        _nb.crypto_sign_ed25519_pk_to_curve25519(bytes(dev["pubkey"])))
    box = nacl.public.SealedBox(curve_pk)
    # the WHOLE bundle is sealed (H-3's fix: team.db carries every DM,
    # every session row, every secret — "secrets only with the key" was
    # untrue; only team.settings.json and the receiver's name ride in
    # clear, both non-secret)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("team.db.sealed",
                   base64.b64encode(box.encrypt(files["team.db"])))
        z.writestr("team-cert.pem.sealed",
                   base64.b64encode(box.encrypt(files["team-cert.pem"])))
        z.writestr("team.settings.json", files["team.settings.json"])
        z.writestr("team-key.pem.sealed", base64.b64encode(box.encrypt(files["team-key.pem"])))
        z.writestr("sealed-for", device_id)
    blob = buf.getvalue()
    with store.write(conn):
        src, ev = logmod.server_event()
        logmod.append(conn, None, src, ev, "hub.handover", dev["person_name"],
                      None, {"by": actor["name"], "device": device_id})
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value)"
            " VALUES ('hub.moved_at', ?)",
            (str(store.now()),))
    handler._json(200, {"bundle": base64.b64encode(buf.getvalue()).decode()})


def hub_address(handler, match, body) -> None:
    """GET api/v1/hub/address — signed, team.manage: an address notice signed by
    the hub's TLS key.
    """
    conn = _db()
    actor = auth.verify(conn, handler, body)
    if actor is None:
        return
    from .perms import can
    if not can(conn, actor, "team.manage"):
        handler._error(403, "only the owner or an admin publishes the "
                            "address")
        return
    from .teamserver_db import team_dir
    key_p = team_dir() / "team-key.pem"
    host = handler.headers.get("Host") or ""
    notice = address_notice(f"https://{host}", key_p.read_bytes())
    handler._json(200, {"notice": notice})


def address_notice(url: str, key_pem: bytes) -> str:
    """`aura-address:v1:<b64url(json {u, fp, time})>.<b64url(sig)>` — signed by
    the hub's TLS private key.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = serialization.load_pem_private_key(key_pem, password=None)
    fp = tls_fp_of(key_pem)
    payload = json.dumps({"u": url, "fp": fp,
                          "time": round(store.now(), 3)},
                         sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    return ("aura-address:v1:"
            + base64.urlsafe_b64encode(payload).decode().rstrip("=")
            + "." + base64.urlsafe_b64encode(sig).decode().rstrip("="))


def tls_fp_of(key_pem: bytes) -> str:
    """The fingerprint of the cert this key made — read from the store's team row
    (it never changes).
    """
    conn = _db()
    row = conn.execute("SELECT cert_fp FROM team WHERE id = 1").fetchone()
    return row["cert_fp"] if row else ""

def _backups_actor(conn, handler, body):
    """Signed OR session — never both. A request carrying device headers is
    verified as signed; a failed verify has ALREADY sent its 401 and nothing
    may run after it (no session fall-through). Without device headers the
    browser session applies, with the same-origin Origin check on state changes
    (need='session' semantics).
    """
    if handler.headers.get("Aura-Device"):
        return _actor(conn, handler, body, need="signed")
    from .api_people import _actor
    return _actor(conn, handler, body or b"", need="session")


def backups_list(handler, match, body) -> None:
    """GET api/v1/backups — the backups with dates and sizes."""
    conn = _db()
    actor = _backups_actor(conn, handler, body)
    if actor is None:
        return
    from .perms import can
    if not can(conn, actor, "team.manage"):
        handler._error(403, "only the owner or an admin sees the backups")
        return
    from .teamserver_db import backups_dir
    out = []
    d = backups_dir()
    if d.exists():
        for f in sorted(d.glob("*.db"), key=lambda x: -x.stat().st_mtime):
            out.append({"name": f.name, "size": f.stat().st_size,
                        "ts": f.stat().st_mtime})
    handler._json(200, {"backups": out})


def backups_now(handler, match, body) -> None:
    """POST api/v1/backups — "Back up now": the server stays up (sqlite online
    backup); the heads witness the copy.
    """
    conn = _db()
    actor = _backups_actor(conn, handler, body)
    if actor is None:
        return                     # ONE error already sent — no backup
    from .perms import can
    if not can(conn, actor, "team.manage"):
        handler._error(403, "only the owner or an admin runs a backup")
        return
    from .backup import backup_now
    from .teamserver_db import backups_dir
    res = backup_now(conn, backups_dir())
    from . import log as _log
    # one history entry per backup file: an id from the clock alone made two
    # backups in the same second share one entry (the second went unwitnessed)
    _log.append(conn, None, "server", "bk-" + res.name,
                "backup.now", "manual", actor.get("person_id"),
                {"detail": str(res)[:120]}, store.now())
    handler._json(200, {"ok": True, "detail": str(res)[:200]})

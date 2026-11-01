"""packsign.py —: Ed25519 pack signing + verification.

A pack's manifest.json carries a digest of EVERY file in the pack and an
Ed25519 signature over that digest list. Verification:

  1. the manifest uses the current scheme (2). Scheme 1 signed only the
     pack's .json files, so SKILL.md — the instructions the agent reads —
     could change without breaking the signature; such packs are refused
     with a "re-sign" message.
  2. every file matches the digest; added, removed or edited files fail.
  3. the signature was made by a PUBLISHER KEY THE OPERATOR TRUSTS
     (~/.aura/publisher_keys.json). The key inside the pack is never
     trusted on its own — anyone can sign a pack with their own key.

Commands (cli.py): `aura skills keygen`, `aura skills sign`, `aura skills
trust-key`, `aura skills keys`. Private keys stay with the publisher; only the
64-hex-character public key is shared.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

SCHEME = 2
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------ manifest ---
def _canonical_manifest(pack_dir: Path) -> bytes:
    """Digest of every file in the pack except manifest.json itself."""
    pack_dir = Path(pack_dir)
    entries = []
    for f in sorted(pack_dir.rglob("*")):
        if f.is_file() and f.name != "manifest.json" \
                and "__pycache__" not in f.parts:
            entries.append({"path": f.relative_to(pack_dir).as_posix(),
                            "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
    return json.dumps({"files": sorted(entries, key=lambda e: e["path"])},
                      sort_keys=True, indent=1).encode()


# ---------------------------------------------------------------- keys ---
def generate_keypair() -> tuple[bytes, str]:
    """Returns (private_key_bytes, public_key_hex). Requires pynacl."""
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    return sk.encode(), sk.verify_key.encode().hex()


def _keys_path() -> Path:
    from ..paths import aura_home
    return aura_home() / "publisher_keys.json"


def trusted_publishers() -> dict[str, dict]:
    """{public_key_hex: {name, added_ts}} — keys the operator trusts."""
    try:
        data = json.loads(_keys_path().read_text(encoding="utf-8"))
        keys = data.get("keys") or {}
        return {k: v for k, v in keys.items() if _HEX_KEY.match(k)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def trust_publisher(public_key_hex: str, name: str = "") -> dict:
    key = (public_key_hex or "").strip().lower()
    if not _HEX_KEY.match(key):
        return {"ok": False, "error": "a public key is 64 hex characters"}
    keys = trusted_publishers()
    keys[key] = {"name": name or key[:12], "added_ts": round(time.time(), 3)}
    p = _keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"keys": keys}, indent=1), encoding="utf-8")
    return {"ok": True, "key": key, "name": keys[key]["name"]}


# --------------------------------------------------------- sign/verify ---
def sign_pack(pack_dir: Path, private_key: bytes) -> Path:
    """Write manifest.json (digest of every file + Ed25519 signature)."""
    from nacl.signing import SigningKey
    sk = SigningKey(private_key)
    manifest_bytes = _canonical_manifest(pack_dir)
    # The pack's own fields (name, description, its declared capabilities = its
    # run-time limits) survive signing. The trust fingerprint covers
    # manifest.json, so editing them later drops trust.
    keep = {}
    try:
        old = json.loads((Path(pack_dir) / "manifest.json").read_text(encoding="utf-8"))
        keep = {k: old[k] for k in ("name", "version", "description", "source",
                                    "capabilities") if k in old}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    out = {
        **keep,
        "scheme": SCHEME,
        "manifest": json.loads(manifest_bytes),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "algorithm": "ed25519",
        "public_key": sk.verify_key.encode().hex(),
        "signature": sk.sign(manifest_bytes).signature.hex(),
    }
    p = Path(pack_dir) / "manifest.json"
    p.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return p


def verify_pack(pack_dir: Path, public_key_hex: str | None = None) -> dict:
    """Verify scheme, file digests and the signature. With no explicit key,
    the signer must be a publisher the operator trusts."""
    pack_dir = Path(pack_dir)
    mf = pack_dir / "manifest.json"
    if not mf.exists():
        return {"ok": False, "reason": "unsigned pack (no manifest.json)"}
    try:
        doc = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "reason": "manifest.json corrupt"}
    if doc.get("scheme") != SCHEME:
        return {"ok": False, "reason": "signed with the old scheme that did "
                "not cover SKILL.md — ask the publisher to re-sign it"}
    current = _canonical_manifest(pack_dir)
    if hashlib.sha256(current).hexdigest() != doc.get("manifest_sha256"):
        return {"ok": False, "reason": "pack files changed, added or removed "
                "since signing"}
    for entry in (doc.get("manifest") or {}).get("files", []):
        f = pack_dir / entry["path"]
        if not f.exists():
            return {"ok": False, "reason": f"missing file: {entry['path']}"}
        if hashlib.sha256(f.read_bytes()).hexdigest() != entry["sha256"]:
            return {"ok": False, "reason": f"tampered: {entry['path']}"}
    pub = (public_key_hex or doc.get("public_key") or "").lower()
    if not pub:
        return {"ok": False, "reason": "no public key to verify against"}
    publisher = None
    if public_key_hex is None:
        publisher = trusted_publishers().get(pub)
        if publisher is None:
            return {"ok": False, "reason": (
                f"signed by an unknown publisher (key {pub[:16]}…) — if you "
                f"trust them: aura skills trust-key {pub}")}
    try:
        from nacl.signing import VerifyKey
        VerifyKey(bytes.fromhex(pub)).verify(current,
                                             bytes.fromhex(doc["signature"]))
    except ImportError:
        return {"ok": False, "reason": "pynacl is not installed — cannot "
                "check signatures (pip install pynacl)"}
    except Exception as e:
        return {"ok": False, "reason": f"signature invalid: {e}"}
    return {"ok": True, "signed_by": pub[:16],
            "publisher": (publisher or {}).get("name", pub[:12]),
            "files": len((doc.get("manifest") or {}).get("files", []))}

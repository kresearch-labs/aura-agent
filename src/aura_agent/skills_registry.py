"""skills_registry.py — Local skill-pack marketplace

Aura skills = skill packs (packs/<name> with Ed25519-signed manifest.json,
verify_pack in tools/packsign.py). The registry adds the marketplace layer:

  aura skills list — installed packs + trust state
  aura skills install <src> — copy a pack into the registry (staged,
                                  UNTRUSTED until verified+trusted)
  aura skills verify <name> — signature + manifest digest check
  aura skills trust <name> — mark verified pack trusted (records
                                  trust decision + fingerprint)
  aura skills audit — re-verify every trusted pack (tamper
                                  detection), list drift

Trust rules: - untrusted packs are VISIBLE but flagged; the loader refuses them
- trust is bound to the manifest fingerprint — any content change invalidates
trust (audit shows 'DRIFT') Registry: ~/.aura/profiles/<profile>/skills/<name>
(the profile's) or <project notebook>/skills/<name> (--local: this project
only).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .tools.packsign import verify_pack


def _registry(global_: bool) -> Path:
    if global_:
        from .paths import profile_path
        return profile_path("skills")         # The profile's skills
    # This project only: its notebook's skills— where load_skills reads them
    from .cli import engagement_dir
    return engagement_dir(None) / "skills"




def _trust_file(registry: Path) -> Path:
    return registry / ".trust.json"


def _fingerprint(pack_dir: Path) -> str:
    """Content hash over ALL pack files sorted by relative path — any
    tampering moves it (manifest.json alone wouldn't catch file edits)."""
    pack_dir = Path(pack_dir)
    if not (pack_dir / "manifest.json").exists():
        return ""
    h = hashlib.sha256()
    for f in sorted(p for p in pack_dir.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(pack_dir)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:32]


def list_packs(registry: Path | None = None) -> list[dict]:
    reg = registry or _registry(global_=True)
    out = []
    if not reg.exists():
        return out
    trust = _load_trust(reg)
    for d in sorted(reg.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        fp = _fingerprint(d)
        t = trust.get(d.name, {})
        trusted = bool(t.get("trusted")) and t.get("fingerprint") == fp
        out.append(dict(name=d.name, trusted=trusted,
                        fingerprint=fp,
                        verified=t.get("verified", False)))
    return out


def _load_trust(registry: Path) -> dict:
    p = _trust_file(registry)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_trust(registry: Path, trust: dict) -> None:
    p = _trust_file(registry)
    p.write_text(json.dumps(trust, indent=1), encoding="utf-8")


def install(src: Path, registry: Path | None = None,
            global_: bool = True) -> dict:
    """Stage a pack into the registry as UNTRUSTED. Never overwrites an
    existing trusted pack without explicit trust re-grant."""
    reg = registry or _registry(global_=global_)
    src = Path(src)
    mf = src / "manifest.json"
    if not mf.exists():
        return dict(ok=False, error=f"not a pack (no manifest.json): {src}")
    reg.mkdir(parents=True, exist_ok=True)
    dest = reg / src.name
    if dest.exists():
        return dict(ok=False, error=f"'{src.name}' already installed "
                                    "(remove it first)")
    shutil.copytree(src, dest)
    # stage as untrusted + record verification result at install time
    ver = verify_pack(dest)
    trust = _load_trust(reg)
    trust[src.name] = dict(verified=bool(ver.get("ok")),
                           fingerprint=_fingerprint(dest),
                           installed=time.time())
    _save_trust(reg, trust)
    return dict(ok=True, name=src.name, verified=bool(ver.get("ok")),
                detail=ver)


def verify(name: str, registry: Path | None = None,
           global_: bool = True) -> dict:
    reg = registry or _registry(global_=global_)
    pack = reg / name
    if not pack.exists():
        return dict(ok=False, error=f"no such pack: {name}")
    ver = verify_pack(pack)
    trust = _load_trust(reg)
    entry = trust.setdefault(name, {})
    entry["verified"] = bool(ver.get("ok"))
    entry["fingerprint"] = _fingerprint(pack)
    _save_trust(reg, trust)
    return ver


def trust(name: str, registry: Path | None = None,
          global_: bool = True) -> dict:
    """Trust = verified NOW + fingerprint recorded. Refuses unverified."""
    reg = registry or _registry(global_=global_)
    pack = reg / name
    if not pack.exists():
        return dict(ok=False, error=f"no such pack: {name}")
    ver = verify(name, registry=reg)
    if not ver.get("ok"):
        return dict(ok=False,
                    error=f"pack '{name}' fails verification — refusing "
                          f"to trust: {ver.get('reason') or ver.get('detail', '')}")
    fp = _fingerprint(pack)
    t = _load_trust(reg)
    t[name] = dict(verified=True, trusted=True, fingerprint=fp,
                   trusted_at=time.time())
    _save_trust(reg, t)
    return dict(ok=True, name=name, fingerprint=fp)


def audit(registry: Path | None = None, global_: bool = True) -> list[dict]:
    """Re-verify every known pack; trusted packs whose fingerprint changed
    report DRIFT (content changed after trust was granted)."""
    reg = registry or _registry(global_=global_)
    trust = _load_trust(reg)
    rows = []
    for name, entry in sorted(trust.items()):
        pack = reg / name
        if not pack.exists():
            rows.append(dict(name=name, status="MISSING"))
            continue
        fp = _fingerprint(pack)
        if entry.get("trusted") and entry.get("fingerprint") != fp:
            # fingerprint first: any content change after trust is DRIFT,
            # whether or not the (old) signature still verifies
            rows.append(dict(name=name, status="DRIFT",
                             detail="content changed after trust — "
                                    "re-trust required"))
        elif not verify_pack(pack).get("ok"):
            rows.append(dict(name=name, status="BROKEN"))
        else:
            rows.append(dict(name=name, status="OK"))
    return rows



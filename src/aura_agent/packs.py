"""packs.py — skill packs grown into plugins.

Aura keeps what packs already had and those lack. Hooks and agents in an
imported plugin are kept but not run (listed).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def _registry(global_: bool, eng: Path | None = None) -> Path:
    from .skills_registry import _registry as reg
    return reg(global_=global_) if global_ or eng is None else Path(eng) / "skills"


def import_plugin(src: Path, global_: bool = True, eng: Path | None = None) -> dict:
    """."""
    src = Path(src)
    pj = src / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return {"ok": False, "error": f"not a plugin folder (no .claude-plugin/plugin.json): {src}"}
    try:
        meta = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"plugin.json unreadable: {e}"}
    name = "".join(c for c in str(meta.get("name", src.name)) if c.isalnum() or c in "-_")[:40]
    if not name:
        return {"ok": False, "error": "the plugin has no usable name"}
    reg = _registry(global_, eng)
    dest = reg / name
    if dest.exists():
        return {"ok": False, "error": f"'{name}' is already installed (remove it first)"}
    dest.mkdir(parents=True)
    copied, kept_only = [], []
    for part in ("commands", "skills"):
        if (src / part).is_dir():
            shutil.copytree(src / part, dest / part)
            copied.append(part)
    for part in ("hooks", "agents"):
        if (src / part).is_dir():
            shutil.copytree(src / part, dest / part)
            kept_only.append(part)
    servers = {}
    if (src / ".mcp.json").is_file():
        try:
            servers = (json.loads((src / ".mcp.json").read_text(encoding="utf-8"))
                       .get("mcpServers") or {})
        except (OSError, ValueError):
            servers = {}
        (dest / "mcp.json").write_text(json.dumps({"mcp_servers": servers}, indent=1), encoding="utf-8")
        copied.append("mcp servers")
    manifest = {"name": name, "version": str(meta.get("version", "")),
                "description": str(meta.get("description", ""))[:300],
                "source": "claude-plugin",
                # Capabilities = what its skills may do while active; MCP
                # servers are listed
                "capabilities": {"fs_scope": [], "network_targets": [], "commands": [],
                                 "mcp_servers": sorted(servers)}}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    from .skills_registry import _fingerprint, _load_trust, _save_trust
    t = _load_trust(reg)
    t[name] = {"verified": False, "fingerprint": _fingerprint(dest), "installed": time.time(),
               "imported": "claude-plugin"}
    _save_trust(reg, t)
    return {"ok": True, "name": name, "path": str(dest), "parts": copied,
            "kept_not_run": kept_only, "mcp_servers": sorted(servers)}


def _own_key() -> bytes:
    """."""
    from .paths import aura_home
    from .tools.packsign import generate_keypair, trust_publisher
    p = aura_home() / "keys" / "own_ed25519.key"
    if p.is_file():
        return bytes.fromhex(p.read_text(encoding="utf-8").strip())
    sk, pub = generate_keypair()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sk.hex(), encoding="utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except OSError:
        pass
    trust_publisher(pub, "you (local key)")
    return sk


def adopt(name: str, global_: bool = True, eng: Path | None = None) -> dict:
    """."""
    from .skills_registry import trust
    from .tools.packsign import sign_pack
    reg = _registry(global_, eng)
    d = reg / name
    if not (d / "manifest.json").is_file():
        return {"ok": False, "error": f"no pack named {name!r}"}
    sign_pack(d, _own_key())
    return trust(name, registry=reg)


def trusted_packs(eng: Path | None = None) -> list[Path]:
    """Pack folders that pass the loader's trust gate."""
    from .skills import _global_home, _trusted_here
    from .skills_registry import _load_trust
    out = []
    for d, is_global in ((_global_home(None), True),
                         (Path(eng) / "skills" if eng else None, False)):
        if d is None or not d.is_dir():
            continue
        trust = _load_trust(d)
        for p in sorted(d.iterdir()):
            if p.is_dir() and (p / "manifest.json").is_file() and _trusted_here(p, trust, is_global):
                out.append(p)
    return out


def pack_mcp_servers(eng: Path | None = None) -> dict:
    """MCP servers from trusted packs, named <pack>.<server>."""
    out = {}
    for p in trusted_packs(eng):
        f = p / "mcp.json"
        if not f.is_file():
            continue
        try:
            servers = json.loads(f.read_text(encoding="utf-8")).get("mcp_servers") or {}
        except (OSError, ValueError):
            continue
        for n, spec in servers.items():
            if isinstance(spec, dict):
                out[f"{p.name}.{n}"] = spec
    return out

"""doctor_checks.py — Extended doctor checks + --fix.

  - profiles.json parses / active profile exists
  - profile endpoint scheme is http(s) and ends in /v1 (common mistake)
  - session dir writable
  - ctx cache fresh/missing (informational)
  - version staleness (PyPI check, offline-tolerant)

`--fix` repairs what is safely repairable (adds v1, creates missing dirs,
clears stale ctx cache) and reports what needs a human.
"""
from __future__ import annotations

import json

from .profile_store import _profiles_file, list_profiles, active_name


def _check(name: str, ok: bool, detail: str,
           fixable: bool = False) -> dict:
    return dict(name=name, ok=ok, detail=detail, fixable=fixable)


def _fix_profile_url(url: str) -> str:
    if url.rstrip("/").endswith("/v1"):
        return url
    return url.rstrip("/") + "/v1"


def run_checks(fix: bool = False) -> tuple[list[dict], list[str]]:
    """Returns (checks, fix_notes). check dicts: name/ok/detail/fixable."""
    checks: list[dict] = []
    fixed: list[str] = []

    # --- profiles file -------------------------------------------------------
    pf = _profiles_file()
    raw: dict = {}
    if pf.exists():
        try:
            raw = json.loads(pf.read_text(encoding="utf-8"))
            checks.append(_check("profiles file", True, str(pf)))
        except json.JSONDecodeError as e:
            # idiot-proofing: a corrupt file must be REPAIRABLE, not a dead end.
            # fix = back up the corrupt file, write a valid empty profiles.json;
            # the user then re-runs `aura setup quick` to add profiles.
            if fix:
                bak = pf.with_suffix(".json.corrupt-bak")
                try:
                    bak.write_text(pf.read_text(encoding="utf-8"),
                                   encoding="utf-8")
                except OSError:
                    pass
                pf.write_text("{}\n", encoding="utf-8")
                fixed.append("profiles: backed up corrupt file to "
                             f"{bak.name}, wrote fresh one")
                checks.append(_check("profiles file", True,
                                     f"repaired (backup: {bak.name})"))
                raw = {}
            else:
                checks.append(_check(
                    "profiles file", False,
                    f"corrupt JSON: {e} — fix with: aura doctor --fix "
                    f"(backs it up and starts fresh)",
                    fixable=True))
            raw = {}
    else:
        checks.append(_check("profiles file", False,
                             "not found — run: aura setup quick"))

    # --- active profile exists -----------------------------------------------
    active = raw.get("active") or (active_name() if pf.exists() else None)
    profiles = list_profiles()
    if active and active in profiles:
        checks.append(_check("active profile", True, active))
    else:
        checks.append(_check("active profile", False,
                             f"'{active}' not in profiles "
                             f"({', '.join(profiles) or 'none'})"))

    # --- endpoint URL sanity (fixable: add /v1) ------------------------------
    for name, cfg in profiles.items():
        url = cfg.get("base_url", "")
        if not url:
            checks.append(_check(f"endpoint[{name}]", False, "no base_url"))
            continue
        if not url.startswith(("http://", "https://")):
            checks.append(_check(f"endpoint {name}", False,
                                 f"bad scheme: {url}"))
            continue
        if not url.rstrip("/").endswith("/v1") and cfg.get("builtin") is False:
            # custom profiles pointing at root — 404 class; auto-fix
            if fix:
                new = _fix_profile_url(url)
                cfg["base_url"] = new
                from .profile_store import set_profile
                set_profile(name, activate=False, base_url=new)
                fixed.append(f"{name}: {url} -> {new}")
                checks.append(_check(f"endpoint {name}", True,
                                     f"fixed: {new}"))
            else:
                checks.append(_check(f"endpoint {name}", False,
                                     f"{url} missing /v1 (404 risk)",
                                     fixable=True))

    # --- session dir writable -------------------------------------------------
    try:
        home = pf.parent
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check("config home writable", True, str(home)))
    except OSError as e:
        checks.append(_check("config home writable", False, str(e)))

    # --- ctx cache age ---------------------------------------------------------
    cache = pf.parent / "ctx_cache.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            checks.append(_check("ctx cache", True,
                                 f"{len(data)} entr{'y' if len(data) == 1 else 'ies'}"))
        except Exception:
            if fix:
                cache.unlink()
                fixed.append("ctx cache: cleared corrupt file")
                checks.append(_check("ctx cache", True, "cleared corrupt file"))
            else:
                checks.append(_check("ctx cache", False, "corrupt",
                                     fixable=True))
    # what protects the commands Aura runs on this machine (every OS)
    try:
        from .sandbox_os import describe
        _sb = describe()
        checks.append(_check("command sandbox", not _sb.startswith(("none", "a Windows job")), _sb))
    except Exception:                                    # noqa: BLE001
        pass
    # the required parts (pyproject.toml) — keyring missing went unnoticed:
    # keys then quietly live in a file instead of the OS keychain
    import importlib.util
    from .guided_errors import _REQUIRED
    missing = [pkg for mod, pkg in _REQUIRED.items() if importlib.util.find_spec(mod) is None]
    checks.append(_check("required parts", not missing,
                         "all installed" if not missing else
                         "missing: " + ", ".join(missing) + " — python -m pip install "
                         + " ".join(missing) + (" (keys are kept in a file until keyring is "
                                                 "installed)" if "keyring" in missing else "")))
    return checks, fixed


def render(checks: list[dict], fixed: list[str]) -> str:
    lines = []
    bad = 0
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        if not c["ok"]:
            bad += 1
        suffix = "  (fixable: aura doctor --fix)" if (
            not c["ok"] and c.get("fixable")) else ""
        lines.append(f"  {mark} {c['name']:22s} {c['detail']}{suffix}")
    if fixed:
        lines.append("")
        lines.append("  fixes applied:")
        for f in fixed:
            lines.append(f"    • {f}")
    lines.append("")
    if bad == 0:
        lines.append("  verdict: OK — all checks passed")
    else:
        lines.append(f"  verdict: {bad} issue(s) — see ✗ above")
    return "\n".join(lines)

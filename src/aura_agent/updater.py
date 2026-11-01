"""updater.py — updating Aura from its releases.

  check → what is installed, what the release has (no change)
  download(info) → the new package into ~/.aura/releases/<ver>/, its
                       SHA-256 checked against the release's SHA256SUMS
  install(path) → uv installs it (Windows: after Aura closes)
  rollback_target → the newest kept package older than this version

Where the release comes from: AURA_RELEASE_URL (a mirror or a test), else the
repository install.json names (GitHub's releases/latest/download). Only the
stable channel for now (GitHub's "latest" skips pre-releases).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import __version__

WHEEL_RE = re.compile(r"^([0-9a-f]{64})\s+\*?(aura_agent-([0-9][^\s]*)-py3-none-any\.whl)\s*$")
DEFAULT_REPO = "kresearch-labs/aura-agent"


def _home() -> Path:
    from .paths import aura_home
    return aura_home()


def layout() -> dict | None:
    from .lifecycle import layout as _l
    return _l()


def _vkey(v: str) -> tuple:
    """Version order: 0.10.0 > 0.9.2; a pre-release (0.2.0rc1) < 0.2.0."""
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v.strip().lstrip("v"))
    if not m:
        return ((0,), 0, v)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (4 - len(nums))
    return (nums, 0 if m.group(2) else 1, m.group(2))


def newer(a: str, b: str) -> bool:
    """Is version a newer than version b?"""
    return _vkey(a) > _vkey(b)


def base_url(lay: dict | None = None) -> str:
    env = os.environ.get("AURA_RELEASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    lay = lay if lay is not None else (layout() or {})
    repo = (lay.get("repo") or DEFAULT_REPO).strip("/")
    return f"https://github.com/{repo}/releases/latest/download"


def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"aura-agent/{__version__} (update)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - https/our mirror only
        return r.read()


def parse_sums(text: str) -> dict | None:
    """The one Aura package a SHA256SUMS names: {wheel, sum, version}."""
    hits = [m for m in (WHEEL_RE.match(ln.strip()) for ln in text.splitlines()) if m]
    if len(hits) != 1:
        return None
    m = hits[0]
    return {"sum": m.group(1), "wheel": m.group(2), "version": m.group(3)}


def check(lay: dict | None = None) -> dict:
    """{ok, current, latest, newer, base, wheel, sum, kind} or {ok: False, error}.
    Reads one small file; changes nothing."""
    lay = lay if lay is not None else (layout() or {})
    base = base_url(lay)
    out = {"current": __version__, "base": base, "kind": lay.get("kind") or ("folder" if lay else "")}
    try:
        info = parse_sums(_get(f"{base}/SHA256SUMS").decode("utf-8", "replace"))
    except Exception as e:                               # noqa: BLE001
        return {**out, "ok": False, "error": f"couldn't read the release ({base}/SHA256SUMS): {e}"}
    if not info:
        return {**out, "ok": False, "error": "the release's SHA256SUMS doesn't name exactly one Aura package"}
    return {**out, "ok": True, "latest": info["version"], "wheel": info["wheel"], "sum": info["sum"],
            "newer": newer(info["version"], __version__)}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(info: dict) -> Path:
    """The release's package into releases/<ver>/, checked; raises on a
    mismatch (and deletes the bad file). A kept, still-matching copy is
    reused."""
    keep = _home() / "releases" / info["latest"]
    keep.mkdir(parents=True, exist_ok=True)
    dest = keep / info["wheel"]
    if dest.exists() and _sha256(dest) == info["sum"]:
        return dest
    data = _get(f"{info['base']}/{info['wheel']}", timeout=120)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    got = _sha256(tmp)
    if got != info["sum"]:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"the package doesn't match the release's checksum (expected {info['sum']}, "
                         f"got {got}) — nothing was installed")
    tmp.replace(dest)
    (keep / (info["wheel"] + ".sha256")).write_text(info["sum"] + "\n", encoding="utf-8")
    return dest


def kept() -> list[tuple[str, Path]]:
    """Packages kept from earlier installs and updates, newest first —
    only those that still match the checksum saved beside them."""
    out = []
    root = _home() / "releases"
    if not root.is_dir():
        return out
    for d in root.iterdir():
        for w in d.glob("aura_agent-*-py3-none-any.whl"):
            s = d / (w.name + ".sha256")
            m = re.match(r"aura_agent-(.+)-py3-none-any\.whl$", w.name)
            if not m:
                continue
            if s.exists() and s.read_text(encoding="utf-8").strip() != _sha256(w):
                continue
            out.append((m.group(1), w))
    out.sort(key=lambda t: _vkey(t[0]), reverse=True)
    return out


def rollback_target() -> tuple[str, Path] | None:
    """The newest kept package older than the version running now."""
    for v, w in kept():
        if newer(__version__, v):
            return v, w
    return None


def _spec(lay: dict, wheel: Path) -> str:
    url = wheel.resolve().as_uri()
    extras = lay.get("extras") or ""
    return f"aura-agent[{extras}] @ {url}" if extras else f"aura-agent @ {url}"


def _uv_argv(lay: dict, wheel: Path) -> list[str]:
    return [lay.get("uv", ""), "tool", "install", "--force", "--managed-python",
            "--python", str(lay.get("python") or "3.12"), _spec(lay, wheel)]


def _record(lay: dict, wheel: Path, version: str) -> None:
    lay = dict(lay)
    lay.update({"kind": "release", "source": str(wheel), "version": version, "at": int(time.time())})
    (_home() / "install.json").write_text(json.dumps(lay, indent=1), encoding="utf-8")


def result_file() -> Path:
    return _home() / "update-result.json"


def install(wheel: Path, version: str, lay: dict | None = None, verb: str = "updated") -> dict:
    """Install a checked package. {ok, now: bool, message}.
    Windows can't replace the Python Aura is running from, so there a small
    helper waits for Aura to close, installs, and leaves update-result.json
    (told at the next start). Elsewhere it is installed now."""
    from .lifecycle import _uv_env
    lay = lay if lay is not None else (layout() or {})
    if lay.get("method") != "uv-tool" or not Path(lay.get("uv", "")).exists():
        return {"ok": False, "now": True,
                "message": "this Aura wasn't installed by Aura's installer, so it can't update itself"}
    if os.name == "nt":
        _install_after_exit(lay, wheel, version)
        return {"ok": True, "now": False,
                "message": f"Aura {version} is checked and ready — it goes in as soon as Aura closes "
                           "(nothing to run; the next start says how it went)"}
    r = subprocess.run(_uv_argv(lay, wheel), env=_uv_env(lay), capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "now": True,
                "message": "the install failed: " + (r.stderr or r.stdout).strip()[-400:]}
    exe = Path(lay.get("bin_dir", "")) / "aura"
    v = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    if v.returncode != 0:
        return {"ok": False, "now": True, "message": "the new aura command doesn't run: "
                + (v.stderr or "").strip()[:200]}
    _record(lay, wheel, version)
    return {"ok": True, "now": True, "message": f"{verb} {__version__} → {version} — restart Aura to use it"}


def _install_after_exit(lay: dict, wheel: Path, version: str) -> None:
    """Windows: a hidden helper waits for this Aura to exit, then installs
    the checked package and writes update-result.json."""
    from .lifecycle import _uv_env
    import tempfile
    home = _home()
    res = result_file()
    res.unlink(missing_ok=True)
    plan = {"argv": _uv_argv(lay, wheel), "env": {k: v for k, v in _uv_env(lay).items()
                                                  if k.startswith("UV_")},
            "pid": os.getpid(), "from": __version__, "to": version, "wheel": str(wheel),
            "exe": str(Path(lay.get("bin_dir", "")) / "aura.exe"), "result": str(res),
            "layout": str(home / "install.json")}
    pf = Path(tempfile.gettempdir()) / f"aura-update-{os.getpid()}.json"
    pf.write_text(json.dumps(plan), encoding="utf-8")
    # the helper runs on the uv-managed Python? no — that is the one being
    # replaced. It runs on PowerShell, which every Windows has.
    ps = r"""
$p = Get-Content -Raw -Path $args[0] | ConvertFrom-Json
while (Get-Process -Id $p.pid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 500 }
Start-Sleep -Seconds 1
foreach ($k in $p.env.PSObject.Properties) { Set-Item -Path ("env:" + $k.Name) -Value $k.Value }
$a = @($p.argv)
$out = & $a[0] $a[1..($a.Count - 1)] 2>&1 | Out-String
$ok = ($LASTEXITCODE -eq 0)
$ver = ""
if ($ok) { try { $ver = (& $p.exe --version 2>$null | Out-String).Trim() } catch { $ok = $false } }
if ($ok -and $ver) {
  $l = Get-Content -Raw -Path $p.layout | ConvertFrom-Json
  $l | Add-Member -Force kind "release"; $l | Add-Member -Force source $p.wheel
  $l | Add-Member -Force version $p.to
  $l | ConvertTo-Json | Set-Content -Path $p.layout -Encoding UTF8
}
$r = [ordered]@{ ok = [bool]($ok -and $ver); from = $p.from; to = $p.to; version = $ver
                 detail = $out.Substring([Math]::Max(0, $out.Length - 600)); at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
$r | ConvertTo-Json | Set-Content -Path $p.result -Encoding UTF8
Remove-Item -Force $args[0] -ErrorAction SilentlyContinue
"""
    sf = Path(tempfile.gettempdir()) / f"aura-update-{os.getpid()}.ps1"
    sf.write_text(ps, encoding="utf-8")
    detach(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(sf), str(pf)])


def detach(argv: list[str]) -> None:
    """Windows: start a helper that outlives Aura. The `aura.exe` launcher runs
    Aura inside a job that is closed — every process in it killed — when Aura
    exits, so the helper leaves the job (CREATE_BREAKAWAY_FROM_JOB); where a
    job doesn't allow that, it starts without it. Hidden with CREATE_NO_WINDOW,
    not DETACHED_PROCESS: PowerShell given no console at all quits at once
    without running anything.
    """
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    quiet = {"close_fds": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
             "stderr": subprocess.DEVNULL}
    try:
        subprocess.Popen(argv, creationflags=base | breakaway, **quiet)
    except OSError:
        subprocess.Popen(argv, creationflags=base, **quiet)


CHECK_EVERY_S = 24 * 3600


def daily_notice(now: float | None = None) -> str | None:
    """With the `updates.check` setting on: at most once a day, one quiet line
    when a newer release is out. Never with AURA_NO_UPDATE_CHECK=1, never for
    an install that can't update itself; any failure stays silent (it is only a
    hint).
    """
    if os.environ.get("AURA_NO_UPDATE_CHECK", "").strip() in ("1", "true", "yes"):
        return None
    try:
        from .options import get
        if not get("updates.check"):
            return None
    except Exception:                                    # noqa: BLE001
        return None
    lay = layout() or {}
    if lay.get("method") != "uv-tool":
        return None
    now = time.time() if now is None else now
    rec_f = _home() / "update-check.json"
    try:
        rec = json.loads(rec_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = {}
    if now - float(rec.get("at") or 0) < CHECK_EVERY_S:
        latest = rec.get("latest") or ""
    else:
        info = check(lay)
        latest = info.get("latest", "") if info.get("ok") else ""
        try:
            rec_f.write_text(json.dumps({"at": now, "latest": latest}), encoding="utf-8")
        except OSError:
            pass
    if latest and newer(latest, __version__):
        return f"Aura {latest} is out (this is {__version__}) — say 'update' to get it"
    return None


def last_result() -> str | None:
    """Once, at the next start: how a Windows after-exit update went."""
    f = result_file()
    try:
        r = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    try:
        f.unlink()
    except OSError:
        pass
    if r.get("ok"):
        return f"updated {r.get('from')} → {r.get('to')} ✓"
    return (f"the update to {r.get('to')} didn't finish — Aura {r.get('from')} is still installed. "
            f"Ask to update again. ({str(r.get('detail') or '').strip()[-200:]})")

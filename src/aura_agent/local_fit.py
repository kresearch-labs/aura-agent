"""local_fit.py — will this model run well on this computer?.

Verdicts are estimates and say so.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

GB = 1024 ** 3
REGISTRY = "https://registry.ollama.ai/v2"
CTX = 32768                       # The window Aura recommends (setup_steps.ROOM × 2)
HEADROOM_GB = 4.0                 # Left for the system and other programs


# ------------------------------------------------------------- hardware --
def _ram_gb() -> float:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("ae", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / GB
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5).stdout
            return int(out.strip()) / GB
        for line in open("/proc/meminfo", encoding="utf-8"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024 / GB
    except Exception:                                # noqa: BLE001
        pass
    return 0.0


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                                # noqa: BLE001
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[1].isdigit():
            gpus.append({"name": parts[0], "vram_gb": int(parts[1]) / 1024})
    return gpus


def _windows_gpus() -> list[dict]:
    """Dedicated memory from the 64-bit registry value (WMI stops at 4 GB)."""
    out = []
    try:
        import winreg
        key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(k, sub) as s:
                        name = winreg.QueryValueEx(s, "DriverDesc")[0]
                        size = winreg.QueryValueEx(s, "HardwareInformation.qwMemorySize")[0]
                    if isinstance(size, bytes):
                        size = int.from_bytes(size, "little")
                    if int(size) > 0:
                        out.append({"name": str(name), "vram_gb": int(size) / GB})
                except OSError:
                    continue
    except Exception:                                # noqa: BLE001
        pass
    return out


def _linux_amd() -> list[dict]:
    out = []
    try:
        import glob
        for f in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
            size = int(open(f, encoding="utf-8").read().strip())
            if size > GB:
                out.append({"name": "AMD GPU", "vram_gb": size / GB})
    except Exception:                                # noqa: BLE001
        pass
    return out


def hardware() -> dict:
    """{ram_gb, gpus:[{name, vram_gb}], unified, gpu_gb (what a model may use
    on the GPU)}."""
    ram = _ram_gb()
    gpus = _nvidia()
    if os.name == "nt":
        seen = {g["name"] for g in gpus}
        gpus += [g for g in _windows_gpus() if g["name"] not in seen]
    elif sys.platform.startswith("linux"):
        gpus += _linux_amd()
    unified = sys.platform == "darwin" and platform.machine() == "arm64"
    if unified:
        gpu_gb = ram * 0.70                         # Metal's share of shared memory
    else:
        big = [g for g in gpus if g["vram_gb"] >= 2]  # Small integrated parts don't count
        gpu_gb = sum(g["vram_gb"] for g in big)
    return {"ram_gb": round(ram, 1), "gpus": gpus, "unified": unified,
            "gpu_gb": round(gpu_gb, 1)}


def describe(hw: dict) -> str:
    if hw["unified"]:
        return f"Apple silicon, {hw['ram_gb']:.0f} GB shared memory (about {hw['gpu_gb']:.0f} GB for a model)"
    gpus = ", ".join(f"{g['name']} ({g['vram_gb']:.0f} GB)" for g in hw["gpus"] if g["vram_gb"] >= 2)
    return f"{hw['ram_gb']:.0f} GB memory" + (f", graphics: {gpus}" if gpus else ", no graphics card a model can use")


# ---------------------------------------------------------------- the fit --
def need_gb(size_bytes: int, ctx: int = CTX) -> float:
    """Memory to run a model file of this size with a `ctx` window (estimate):
    the file, the conversation's working memory, the overhead."""
    size = size_bytes / GB
    kv = size * (0.30 if ctx >= 32768 else 0.15 if ctx >= 16384 else 0.08)
    return size + kv + 1.0


def verdict(need: float, hw: dict) -> tuple[str, str]:
    """('fast' | 'slow' | 'too big', plain words)."""
    usable_ram = max(0.0, hw["ram_gb"] - HEADROOM_GB)
    if hw["gpu_gb"] and need <= hw["gpu_gb"]:
        return "fast", f"fits in the graphics memory (needs about {need:.1f} GB)"
    if hw["unified"]:
        return ("too big", f"needs about {need:.1f} GB; about {hw['gpu_gb']:.0f} GB is free for a model")
    if need <= hw["gpu_gb"] + usable_ram:
        part = "partly on the graphics card, the rest on the processor" if hw["gpu_gb"] else "on the processor"
        return "slow", f"runs {part} — slower (needs about {need:.1f} GB)"
    return "too big", f"needs about {need:.1f} GB; this computer has about {hw['gpu_gb'] + usable_ram:.0f} GB to give it"


def size_classes(hw: dict) -> str:
    """What fits, in parameters, at 4-bit (≈0.6 GB per billion)."""
    def billions(gb: float) -> int:
        return max(0, int(((gb - 1.0) / 1.3) / 0.6))
    fast = billions(hw["gpu_gb"]) if hw["gpu_gb"] else 0
    slow = billions(hw["gpu_gb"] + max(0.0, hw["ram_gb"] - HEADROOM_GB))
    parts = []
    if fast:
        parts.append(f"fast: models up to about {fast}B parameters")
    parts.append(f"{'slower' if fast else 'on the processor (slow)'}: up to about {slow}B")
    return " · ".join(parts) + "  (4-bit models, a 32k-token window — estimates)"


# ----------------------------------------------------------- the models --
def _split(name: str) -> tuple[str, str]:
    name = name.strip()
    repo, _, tag = name.partition(":")
    if "/" not in repo:
        repo = "library/" + repo
    return repo, tag or "latest"


def registry_size(name: str) -> int | None:
    """A model's download size from Ollama's registry (no download).
    None = no such model there."""
    repo, tag = _split(name)
    req = urllib.request.Request(f"{REGISTRY}/{repo}/manifests/{tag}", headers={
        "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return sum(int(layer.get("size", 0)) for layer in d.get("layers") or [])


def _root(base_url: str) -> str:
    return re.sub(r"/v1/?$", "", base_url.rstrip("/"))


def installed(base_url: str) -> list[tuple[str, int]]:
    """Models already in Ollama: [(name, size bytes)]."""
    try:
        with urllib.request.urlopen(_root(base_url) + "/api/tags", timeout=5) as r:
            d = json.load(r)
    except Exception:                                # noqa: BLE001
        return []
    return [(m.get("name") or m.get("model"), int(m.get("size") or 0))
            for m in d.get("models") or [] if m.get("name") or m.get("model")]


def pull(base_url: str, name: str, say=print) -> tuple[bool, str]:
    """Download a model into Ollama (POST /api/pull), showing progress."""
    req = urllib.request.Request(_root(base_url) + "/api/pull",
                                 data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    last, shown = "", -10
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            for raw in r:
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if ev.get("error"):
                    return False, str(ev["error"])[:200]
                st = str(ev.get("status") or "")
                total, done = ev.get("total"), ev.get("completed")
                if total and done is not None:
                    pct = int(done * 100 / total)
                    if pct >= shown + 10 or pct == 100:
                        say(f"    {st[:30]} {pct}% of {total / GB:.1f} GB")
                        shown = pct
                elif st and st != last:
                    say(f"    {st}")
                last = st
                if st == "success":
                    return True, "downloaded"
    except Exception as e:                           # noqa: BLE001
        return False, str(e)[:200]
    return last == "success", last or "the download stopped"

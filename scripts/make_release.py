"""make_release.py — build the files of one Aura release, exactly as a GitHub
release holds them:

    aura_agent-X.Y.Z-py3-none-any.whl the package the installers install
    aura_agent-X.Y.Z.tar.gz the source archive
    install.sh, install.ps1 with the release's tag written in
    SHA256SUMS one line per file above

Python scripts/make_release.py [--out DIR] [--uv PATH]

The version comes from src/aura_agent/__init__.py (the one source). The release
workflow runs this same script, so what is tested here is what ships. Nothing
is uploaded — this only writes DIR (default dist/release).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    m = re.search(r'^__version__ = "([^"]+)"', (ROOT / "src/aura_agent/__init__.py").read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit("✗ no __version__ in src/aura_agent/__init__.py")
    return m.group(1)


def stamp(text: str, tag: str, var: str) -> str:
    """Write the tag into the installer's `<var> = ""` line (exactly one)."""
    pat = re.compile(rf'^(\$?{var}\s*=\s*)""', re.M)
    if len(pat.findall(text)) != 1:
        sys.exit(f"✗ the installer has no single {var} line to stamp")
    return pat.sub(rf'\g<1>"{tag}"', text, count=1)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="build one Aura release's files")
    ap.add_argument("--out", default=str(ROOT / "dist" / "release"))
    ap.add_argument("--uv", default=shutil.which("uv") or "uv", help="the uv to build with")
    a = ap.parse_args()
    ver = version()
    tag = f"v{ver}"
    out = Path(a.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    r = subprocess.run([a.uv, "build", "--out-dir", str(out)], cwd=ROOT)
    if r.returncode != 0:
        sys.exit("✗ the build failed")
    for name in ("install.sh", "install.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        text = stamp(text, tag, "AURA_TAG" if name.endswith(".sh") else "AuraTag")
        # .sh keeps LF (sh breaks on CRLF); .ps1 keeps what it has
        (out / name).write_bytes(text.encode("utf-8"))
    for junk in out.glob(".gitignore"):
        junk.unlink()
    files = sorted(p for p in out.iterdir() if p.is_file())
    want = {f"aura_agent-{ver}-py3-none-any.whl", f"aura_agent-{ver}.tar.gz", "install.sh", "install.ps1"}
    if {p.name for p in files} != want:
        sys.exit(f"✗ unexpected release files: {sorted(p.name for p in files)}")
    (out / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), encoding="utf-8", newline="\n")
    print(f"\nrelease {tag} in {out}")
    for p in files + [out / "SHA256SUMS"]:
        print(f"  {p.name:42} {p.stat().st_size:>10,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

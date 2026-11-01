"""image_paste.py — images in a chat message.

Aura does both: image paths in a message are attached, and `/image` reads the
clipboard on every OS (PowerShell on Windows, osascript on macOS, wl-paste or
xclip on Linux — the ones that exist; nothing is installed). The image goes to
the model only when it was checked to see images; otherwise Aura says why.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_PATHS = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')


def image_paths(text: str, workspace: Path | None = None) -> list[str]:
    """Existing image files named in the message (dragged-in paths come
    quoted when they have spaces)."""
    out = []
    for m in _PATHS.finditer(text or ""):
        cand = (m.group(1) or m.group(2) or m.group(3) or "").strip().rstrip(",;")
        if not cand.lower().endswith(IMAGE_EXT):
            continue
        p = Path(os.path.expanduser(cand))
        if not p.is_absolute() and workspace is not None:
            p = Path(workspace) / p
        try:
            if p.is_file() and str(p) not in out:
                out.append(str(p))
        except OSError:
            continue
    return out[:5]


def clipboard_image(dest_dir: Path) -> tuple[str | None, str]:
    """Save the clipboard's image as a PNG. (path, '') or (None, why)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"clip-{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        if os.name == "nt":
            ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                  "$i=[Windows.Forms.Clipboard]::GetImage(); if($i -eq $null){exit 3}; "
                  f"$i.Save('{dest}', [Drawing.Imaging.ImageFormat]::Png)")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                               capture_output=True, timeout=15)
        elif sys.platform == "darwin":
            script = (f'set f to open for access POSIX file "{dest}" with write permission\n'
                      'write (the clipboard as «class PNGf») to f\nclose access f')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        else:
            tool = ("wl-paste", "--type", "image/png") if shutil.which("wl-paste") else \
                   ("xclip", "-selection", "clipboard", "-t", "image/png", "-o") if shutil.which("xclip") \
                   else None
            if tool is None:
                return None, "no clipboard tool here (wl-paste or xclip) — drag the image file in instead"
            with open(dest, "wb") as f:
                r = subprocess.run(list(tool), stdout=f, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"couldn't read the clipboard: {e}"
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        try:
            dest.unlink()
        except OSError:
            pass
        return None, "there is no image on the clipboard"
    return str(dest), ""


def vision_note(loop) -> tuple[bool, str]:
    """(send it?, what to tell the user) — from the check of this model."""
    try:
        from .model_facts import fact, model_key
        v = fact("vision", model_key(loop.llm.p.base_url, loop.llm.p.model))
    except Exception:                                    # noqa: BLE001
        v = None
    if v is None:
        v = vision_check(loop)              # Aura finds out itself, once per model
    if v == "yes":
        return True, "image attached — this model sees images (checked)"
    if v == "no":
        return False, ("this model can't see images (checked) — switch to one that can "
                       "(/model), or say what's in it")
    return False, "couldn't tell whether this model sees images (the check failed) — not sent"


def vision_check(loop) -> str | None:
    """One tiny call (a small red square: "what colour?") — the same check as
    `aura eval model`, run by Aura the first time an image is attached. The
    answer is remembered for the model.
    """
    try:
        from .llm import ProviderRejected
        from .model_facts import _red_png, model_key, save_facts
        p = loop.llm.p
        try:
            r = loop.llm.chat([{"role": "user", "content": [
                {"type": "text", "text": "What colour is this image? Answer with one word."},
                {"type": "image_url", "image_url": {"url": _red_png()}}]}],
                temperature=0, side=True)
            v = "yes" if "red" in (r.content or "").lower() else "no"
        except ProviderRejected:
            v = "no"
        save_facts(model_key(p.base_url, p.model), {"vision": {"value": v, "how": "measured",
                                                                "note": "checked on first image"}})
        return v
    except Exception:                                    # noqa: BLE001
        return None

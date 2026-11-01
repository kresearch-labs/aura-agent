"""attach.py — anything you give Aura to look at, and pastes too big to send.

  - a terminal pastes text only; agents take images by a key that reads the
    audio, video and images to a model that can use them;
  - pasting a large file whole "burns fast" — the advice is a file the agent
    reads in parts, not the whole text in the message;
  - Office files are zip archives of XML: their text comes out with the
    standard library (word/document.xml, ppt/slides/*.xml,
    xl/sharedStrings.xml + xl/worksheets/*.xml), with a guard against zip
    bombs;
  - files copied in a file manager: Windows `Get-Clipboard -Format
    FileDropList`, macOS the clipboard's «class furl», Linux text/uri-list.

What each kind becomes (describe): text / code / csv / json → its text
(csv/json with a short summary) image → attached when the model sees images
(checked once, by Aura itself — vision_check) pdf → its text, page by page
(pypdf when installed) docx / xlsx / pptx → their text and tables (standard
library) zip / tar → the list of what is inside (never unpacked here) audio /
video / program → its type and size, plainly: the model can't use it Too big
for the room left in the context → saved with the chat; the message names the
file and its size, and the model reads the parts it needs.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

BIG_SHARE = 0.25                 # Of the free context: past this, a file the model reads
MAX_MEMBER = 50 * 1024 * 1024    # Zip bomb guard: one part of an Office file
MAX_TOTAL = 200 * 1024 * 1024
CHARS_PER_TOKEN = 3.5

TEXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".csv", ".tsv", ".html", ".htm", ".css", ".xml", ".sql", ".sh", ".ps1",
        ".bat", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".kt", ".swift",
        ".log", ".env.example", ".r", ".lua", ".pl", ".cs", ".vue", ".svelte", ".gradle"}
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
OFFICE = {".docx", ".xlsx", ".pptx"}
ARCHIVE = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}
MEDIA = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".avi", ".webm"}
PROGRAM = {".exe", ".dll", ".msi", ".so", ".dylib", ".bin", ".iso", ".apk"}


def tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN)


def kind_of(path: Path) -> str:
    s = "".join(Path(path).suffixes[-2:]).lower() if Path(path).name.endswith(".tar.gz") else Path(path).suffix.lower()
    if s in IMAGE:
        return "image"
    if s == ".pdf":
        return "pdf"
    if s in OFFICE:
        return "office"
    if s in ARCHIVE or s == ".tar.gz":
        return "archive"
    if s in MEDIA:
        return "media"
    if s in PROGRAM:
        return "program"
    if s in TEXT:
        return "text"
    try:                                          # An unknown ending: text if it reads as text
        head = Path(path).read_bytes()[:4096]
        head.decode("utf-8")
        return "text" if b"\x00" not in head else "binary"
    except (OSError, UnicodeDecodeError):
        return "binary"


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


# --------------------------------------------------------------- extract ---
def _xml_text(data: bytes, para_tag: str) -> list[str]:
    """Text of an Office XML part, one line per paragraph."""
    root = ET.fromstring(data)                    # Expat refuses entity bombs (3.7.1+)
    out, cur = [], []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "t" and el.text:
            cur.append(el.text)
        elif tag == "tab":
            cur.append("\t")
        elif tag in ("br", "cr"):
            cur.append("\n")
        if tag == para_tag and cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _zip_parts(z: zipfile.ZipFile, names: list[str]) -> list[tuple[str, bytes]]:
    total, out = 0, []
    for n in names:
        info = z.getinfo(n)
        if info.file_size > MAX_MEMBER:
            raise ValueError(f"{n} is {_size(info.file_size)} unpacked — too big to read safely")
        total += info.file_size
        if total > MAX_TOTAL:
            raise ValueError("the file unpacks to more than 200 MB — not read (a zip bomb?)")
        out.append((n, z.read(n)))
    return out


def office_text(path: Path) -> tuple[str, str]:
    """(text, what it is) for a .docx / .xlsx / .pptx."""
    s = Path(path).suffix.lower()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if s == ".docx":
            parts = [n for n in names if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes)\.xml", n)]
            lines = []
            for _, data in _zip_parts(z, sorted(parts, key=lambda n: n != "word/document.xml")):
                lines += _xml_text(data, "p")
            return "\n".join(lines), f"{len(lines)} paragraphs"
        if s == ".pptx":
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            out = []
            for i, (_, data) in enumerate(_zip_parts(z, slides), 1):
                out.append(f"--- slide {i} ---\n" + "\n".join(_xml_text(data, "p")))
            return "\n".join(out), f"{len(slides)} slides"
        if s == ".xlsx":
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                (_, data), = _zip_parts(z, ["xl/sharedStrings.xml"])
                root = ET.fromstring(data)
                for si in root.iter():
                    if si.tag.endswith("}si"):
                        shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")))
            sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            out, rows_total = [], 0
            for i, (_, data) in enumerate(_zip_parts(z, sheets), 1):
                root = ET.fromstring(data)
                rows = []
                for row in root.iter():
                    if not row.tag.endswith("}row"):
                        continue
                    cells = []
                    for c in row:
                        if not c.tag.endswith("}c"):
                            continue
                        v = next((x.text for x in c if x.tag.endswith("}v")), None)
                        if v is None:
                            v = "".join(t.text or "" for t in c.iter() if t.tag.endswith("}t"))
                        elif c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                            v = shared[int(v)]
                        cells.append(v or "")
                    rows.append("\t".join(cells))
                rows_total += len(rows)
                out.append(f"--- sheet {i} ({len(rows)} rows) ---\n" + "\n".join(rows))
            return "\n".join(out), f"{len(sheets)} sheet(s), {rows_total} rows"
    raise ValueError("not an Office file Aura reads")


def pdf_text(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("reading a PDF's text needs the pypdf package (not installed)")
    r = PdfReader(str(path))
    pages = [f"--- page {i} ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(r.pages, 1)]
    text = "\n".join(pages)
    if len(text.replace("-", "").strip()) < 20 * len(pages):
        return text, f"{len(pages)} pages — little text (a scanned PDF? its pages are images)"
    return text, f"{len(pages)} pages"


def archive_list(path: Path) -> tuple[str, str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            lines = [f"{i.filename}  ({_size(i.file_size)})" for i in infos[:300]]
            more = f"\n… {len(infos) - 300} more" if len(infos) > 300 else ""
            return "\n".join(lines) + more, f"{len(infos)} files inside (not unpacked)"
    import tarfile
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            ms = t.getmembers()
            return "\n".join(f"{m.name}  ({_size(m.size)})" for m in ms[:300]), f"{len(ms)} files inside (not unpacked)"
    return "", "an archive Aura can't list here (7z / rar)"


def table_summary(path: Path, text: str) -> str:
    s = Path(path).suffix.lower()
    try:
        if s in (".csv", ".tsv"):
            import csv
            import io
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if s == ".tsv" else ","))
            if rows:
                return f"{len(rows) - 1} rows × {len(rows[0])} columns: {', '.join(rows[0][:12])}"
        if s == ".json":
            d = json.loads(text)
            if isinstance(d, list):
                return f"a list of {len(d)} items"
            if isinstance(d, dict):
                return f"an object with keys: {', '.join(list(d)[:12])}"
    except Exception:                                # noqa: BLE001
        return ""
    return ""


def describe(path: Path) -> dict:
    """What a file becomes for the model: {kind, label, text, note, ok}."""
    p = Path(path)
    k = kind_of(p)
    size = p.stat().st_size if p.exists() else 0
    base = {"kind": k, "name": p.name, "size": _size(size), "path": str(p)}
    try:
        if k == "text":
            text = p.read_text(encoding="utf-8", errors="replace")
            summ = table_summary(p, text)
            return {**base, "text": text, "note": summ or f"{text.count(chr(10)) + 1} lines", "ok": True}
        if k == "office":
            text, what = office_text(p)
            return {**base, "text": text, "note": what, "ok": True}
        if k == "pdf":
            text, what = pdf_text(p)
            return {**base, "text": text, "note": what, "ok": True}
        if k == "archive":
            text, what = archive_list(p)
            return {**base, "text": text, "note": what, "ok": bool(text)}
        if k == "image":
            return {**base, "text": "", "note": "an image", "ok": True}
    except Exception as e:                           # noqa: BLE001
        return {**base, "text": "", "note": f"couldn't be read: {e}", "ok": False}
    what = {"media": "audio/video — the model can't hear or watch it",
            "program": "a program — it isn't run or read",
            "binary": "a binary file — not text the model can read"}.get(k, k)
    return {**base, "text": "", "note": what, "ok": False}


# ------------------------------------------------------- too big to send ---
def free_tokens(loop) -> int:
    g = getattr(loop, "governor", None)
    if not g or not g.ctx:
        return 0
    used = 0
    try:
        used = g.count(loop._last_sent or []) if getattr(loop, "_last_sent", None) else 0
    except Exception:                                # noqa: BLE001
        used = 0
    return max(0, g._usable() - used)


def too_big(text: str, loop) -> bool:
    free = free_tokens(loop)
    return bool(free) and tokens(text) > free * BIG_SHARE


def save_aside(eng: Path, chat_id: str, name: str, text: str) -> Path:
    """A big paste / attachment kept with the chat for the model to read in parts."""
    d = Path(eng) / "pastes"
    d.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]+", "-", name)[:60] or "paste"
    p = d / f"{chat_id}-{stem}.txt"
    n = 2
    while p.exists():
        p = d / f"{chat_id}-{stem}-{n}.txt"
        n += 1
    p.write_text(text, encoding="utf-8")
    return p


def aside_ref(p: Path, what: str, text: str) -> str:
    lines = text.count("\n") + 1
    return (f"[{what} — {lines:,} lines, ~{tokens(text):,} tokens — too big to put in this "
            f"message; saved as {p}: read the parts you need with read_file (offset/limit) "
            f"or search_files]")


# ------------------------------------------------------------ clipboard ---
def clipboard_files() -> list[str]:
    """Files copied in the file manager (Explorer, Finder, a Linux one)."""
    import os
    import shutil
    import subprocess
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-Clipboard -Format FileDropList | ForEach-Object { $_.FullName }"],
                               capture_output=True, text=True, timeout=10)
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        import sys
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e",
                                "POSIX path of (the clipboard as «class furl»)"],
                               capture_output=True, text=True, timeout=10)
            return [r.stdout.strip()] if r.returncode == 0 and r.stdout.strip() else []
        for cmd in (["wl-paste", "--no-newline", "--type", "text/uri-list"],
                    ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-o"]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    from urllib.parse import unquote, urlparse
                    return [unquote(urlparse(u).path) for u in r.stdout.split()
                            if u.startswith("file://")]
    except Exception:                                # noqa: BLE001
        return []
    return []

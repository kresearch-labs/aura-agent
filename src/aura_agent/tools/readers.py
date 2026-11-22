"""readers.py — read_file for files that are not plain UTF-8 text.

Aura's read_file did UTF-8 text only (a PDF came back as noise). Here, with
Python's own libraries only (zipfile, xml, json) — PDFs use pypdf, the same
optional add-on web_crawl uses:

  - text in any common encoding (UTF-8, UTF-8/16 with a byte-order mark,
    UTF-16 without one, Windows-1252), said when it isn't UTF-8;
  - PDF pages ("1-5"), notebooks (cells with their text output), Word
    (.docx), Excel (.xlsx, sheet by sheet), PowerPoint (.pptx, slide by slide);
  - images and other binary files: what they are and which tool fits, never
    a page of noise.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".heic"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".xlsm", ".docm", ".pptm"}
TEXT_CAP = 200_000                 # Characters one read gives back
PDF_PAGES_MAX = 20                 # Pages one read gives back


def size_words(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ------------------------------------------------------------------ text --
def decode(raw: bytes) -> tuple[str, str]:
    """(text, encoding note) — '' note for plain UTF-8."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
        return raw[2:].decode(enc, errors="replace"), f"(read as {enc.upper()})"
    head = raw[:4096]
    if len(head) >= 8 and head.count(b"\x00") > len(head) // 4 and not head.startswith(_MAGIC):
        # UTF-16 without a byte-order mark: every other byte is zero and the
        # others are ordinary text (a binary file has zeros too)
        even, odd = head[0::2], head[1::2]
        zeros, chars = (odd, even) if odd.count(0) >= even.count(0) else (even, odd)
        texty = sum(1 for b in chars if 32 <= b < 127 or b in (9, 10, 13))
        if zeros.count(0) > len(zeros) * 0.9 and texty > len(chars) * 0.9:
            enc = "utf-16-le" if zeros is odd else "utf-16-be"
            return raw.decode(enc, errors="replace"), f"(read as {enc.upper()})"
    text = raw.decode("utf-8", errors="replace")
    bad = text.count("�")
    if bad > 5 and bad > len(text) // 200:
        try:
            return raw.decode("cp1252"), "(not UTF-8 — read as Windows-1252)"
        except UnicodeDecodeError:
            pass
    return text, ""


_MAGIC = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"SQLite format 3", b"\x1f\x8b", b"%PDF",
          b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")


def is_binary(raw: bytes) -> bool:
    head = raw[:8192]
    if head.startswith(_MAGIC):
        return True
    if not head or b"\x00" not in head:
        return False
    _t, note = decode(head)
    return not note.startswith("(read as UTF-16")


# ---------------------------------------------------------------- special --
def special(p: Path, args: dict) -> str | None:
    """The answer for a file that is not plain text, or None to read it as
    text."""
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return (f"{p.name} is an image ({size_words(p.stat().st_size)}). Look at it with "
                f"view_image(path='{args.get('path')}').")
    if ext == ".pdf":
        return pdf(p, str(args.get("pages") or ""))
    if ext == ".ipynb":
        return notebook(p)
    if ext in OFFICE_EXT:
        try:
            if ext in (".docx", ".docm"):
                return docx(p)
            if ext in (".xlsx", ".xlsm"):
                return xlsx(p, str(args.get("sheet") or ""))
            return pptx(p)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            return f"{p.name} could not be read as an Office file: {e}"
    return None


def _pages(spec: str, n: int) -> list[int]:
    """'3' / '1-5' / '2,4-6' → 0-based page numbers (at most PDF_PAGES_MAX)."""
    if not spec:
        return list(range(min(n, PDF_PAGES_MAX)))
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError("pages is like 3, 1-5 or 2,4-6")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        out += [i - 1 for i in range(a, b + 1) if 1 <= i <= n]
    return sorted(set(out))[:PDF_PAGES_MAX]


def pdf(p: Path, spec: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return (f"{p.name} is a PDF and the PDF reader add-on (pypdf, aura-agent[pdf]) "
                "isn't installed — ask the user whether to add it; nothing else reads PDFs")
    try:
        rd = PdfReader(str(p))
        n = len(rd.pages)
        want = _pages(spec, n)
        parts = []
        for i in want:
            txt = (rd.pages[i].extract_text() or "").strip()
            parts.append(f"--- page {i + 1} of {n} ---\n" + (txt or "(no text on this page — "
                                                               "it may be a scan or a picture)"))
        body = "\n\n".join(parts)[:TEXT_CAP]
        more = ""
        if want and want[-1] + 1 < n:
            more = (f"\n[pages {want[0] + 1}-{want[-1] + 1} of {n} shown; more with "
                    f"pages='{want[-1] + 2}-{min(n, want[-1] + 1 + PDF_PAGES_MAX)}']")
        return body + more
    except ValueError:
        raise
    except Exception as e:                           # noqa: BLE001
        return f"{p.name}: the PDF could not be read ({type(e).__name__}: {e})"


def notebook(p: Path) -> str:
    nb = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    out = []
    for i, c in enumerate(nb.get("cells") or [], 1):
        kind = c.get("cell_type", "code")
        src = "".join(c.get("source") or [])
        cid = f" id={c['id']}" if c.get("id") else ""
        out.append(f"# cell {i} [{kind}]{cid}")
        out.append(src.rstrip())
        outputs = []
        for o in c.get("outputs") or []:
            if o.get("output_type") == "stream":
                outputs.append("".join(o.get("text") or []))
            elif "data" in o:
                d = o["data"]
                if "text/plain" in d:
                    outputs.append("".join(d["text/plain"]))
                if any(k.startswith("image/") for k in d):
                    outputs.append("[image output]")
            elif o.get("output_type") == "error":
                outputs.append(f"{o.get('ename')}: {o.get('evalue')}")
        if outputs:
            text = "\n".join(x.rstrip() for x in outputs)
            if len(text) > 2000:
                text = text[:1500] + f"\n[… {len(text) - 1500} characters of output left out]"
            out.append("# output:\n" + text)
    body = "\n".join(out)
    return (body[:TEXT_CAP] + ("\n[cut at the read limit]" if len(body) > TEXT_CAP else "")
            + "\n[edit cells with notebook_edit(path, cell=N, ...)]")


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def docx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for para in root.iter(_W + "p"):
        style = para.find(f"{_W}pPr/{_W}pStyle")
        text = "".join(t.text or "" for t in para.iter(_W + "t"))
        if not text.strip():
            continue
        lvl = (style.get(_W + "val") or "") if style is not None else ""
        if lvl.lower().startswith("heading"):
            num = re.sub(r"\D", "", lvl) or "1"
            text = "#" * min(6, int(num)) + " " + text
        paras.append(text)
    tables = sum(1 for _ in root.iter(_W + "tbl"))
    body = "\n\n".join(paras)
    return (body[:TEXT_CAP] + ("\n[cut at the read limit]" if len(body) > TEXT_CAP else "")
            + (f"\n[{tables} table(s) — their text is included above]" if tables else ""))


def xlsx(p: Path, sheet: str = "") -> str:
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels}
        rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        sheets = [(s.get("name"), target.get(s.get(rid), "")) for s in wb.iter(_S + "sheet")]
        out = [f"sheets: {', '.join(n for n, _ in sheets)}"]
        for name, tgt in sheets:
            if sheet and name.lower() != sheet.lower():
                continue
            path = "xl/" + tgt.lstrip("/").removeprefix("xl/")
            if path not in names:
                continue
            rows = []
            for row in ET.fromstring(z.read(path)).iter(_S + "row"):
                cells = []
                for c in row.iter(_S + "c"):
                    v = c.find(_S + "v")
                    t = c.get("t")
                    if t == "s" and v is not None:
                        val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(_S + "t"))
                    else:
                        val = v.text if v is not None and v.text else ""
                    cells.append(f"{c.get('r')}={val}" if val else "")
                line = " | ".join(x for x in cells if x)
                if line:
                    rows.append(line)
                if len(rows) >= 500:
                    rows.append("[… more rows; read one sheet with sheet='name']")
                    break
            out.append(f"--- sheet {name} ({len(rows)} rows shown) ---")
            out += rows
    body = "\n".join(out)
    return body[:TEXT_CAP] + ("\n[cut at the read limit]" if len(body) > TEXT_CAP else "")


def pptx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.sub(r"\D", "", n.rsplit("/", 1)[1])))
        out = []
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            paras = []
            for para in root.iter(_A + "p"):
                t = "".join(x.text or "" for x in para.iter(_A + "t"))
                if t.strip():
                    paras.append(t)
            out.append(f"--- slide {i} ---\n" + "\n".join(paras))
    body = "\n\n".join(out)
    return body[:TEXT_CAP] + ("\n[cut at the read limit]" if len(body) > TEXT_CAP else "")


def binary_note(p: Path, raw: bytes) -> str:
    kind = {b"PK\x03\x04": "a zip archive (or a file packed like one)",
            b"MZ": "a Windows program", b"\x7fELF": "a Linux program",
            b"SQLite format 3": "an SQLite database", b"\x1f\x8b": "a gzip archive",
            b"%PDF": "a PDF"}
    what = next((v for k, v in kind.items() if raw.startswith(k)), "a binary file")
    return (f"{p.name} is {what} ({size_words(p.stat().st_size)}), not text — "
            "read_file shows text, PDFs, notebooks and Office files. A tool or command "
            "made for this kind of file can look inside it.")

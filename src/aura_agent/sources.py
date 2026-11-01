"""sources.py — the project's sources list and the citation check.

Aura keeps every page it read — address, title, a hash and the text — in the
notebook, and before the final answer checks what the answer cites:

  - a link the answer gives was really opened (or at least came up in a
    search) in this project;
  - a quoted sentence is really in one of the pages read.

  - a number in a sentence that cites a page is really in that page
    Bench's FACT finds 6–22 % of cited claims unsupported by their own
    damaging part — the numbers — exactly and for free).

The run gets one turn to fix what doesn't hold; the receipt records the result
either way.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from pathlib import Path

TEXT_CAP = 400_000          # Characters of a page kept for checking
MIN_QUOTE_WORDS = 6


def _dir(root: Path) -> Path:
    return Path(root) / "sources"


def norm_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    q = "&".join(x for x in p.query.split("&") if x and not x.startswith(("utm_", "ref=")))
    return f"{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def record(root: Path, url: str, title: str = "", text: str = "",
           kind: str = "read") -> None:
    """kind: 'read' (opened, text kept) or 'seen' (a search result)."""
    try:
        d = _dir(root)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "kind": kind, "url": url,
               "key": norm_url(url), "title": (title or "")[:200]}
        if kind == "read" and text:
            body = text[:TEXT_CAP]
            sha = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
            (d / f"{sha}.txt").write_text(body, encoding="utf-8")
            rec.update(sha=sha, chars=len(body))
        with (d / "sources.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(root: Path) -> list[dict]:
    p = _dir(root) / "sources.jsonl"
    out = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _text_of(root: Path, rec: dict) -> str:
    try:
        return (_dir(root) / f"{rec['sha']}.txt").read_text(encoding="utf-8")
    except (OSError, KeyError):
        return ""


_URL = re.compile(r"https?://[^\s<>\"'`\]\)]+")
_QUOTE = re.compile(r"[\"“]([^\"“”\n]{20,600})[\"”]")


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def cited_urls(answer: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL.findall(answer or "")))


def quotes(answer: str) -> list[str]:
    return [q.strip() for q in _QUOTE.findall(answer or "")
            if len(_words(q)) >= MIN_QUOTE_WORDS]


def _in_text(quote: str, text_words: str) -> bool:
    w = _words(quote)
    if " ".join(w) in text_words:
        return True
    # small differences (a word changed or dropped): most 5-word runs present
    runs = [" ".join(w[i:i + 5]) for i in range(0, max(1, len(w) - 4))]
    hit = sum(1 for r in runs if r in text_words)
    return bool(runs) and hit / len(runs) >= 0.8


_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")


def _numbers(sentence: str) -> list[str]:
    """Numbers worth checking: 2+ digits, a decimal or a percent (not "3 ways",
    not a list marker); the sentence's links are left out first."""
    text = _URL.sub(" ", sentence)
    out = []
    for n in _NUM.findall(text):
        bare = n.replace(",", "").rstrip("%")
        if "." in bare or n.endswith("%") or len(bare.replace(".", "")) >= 2:
            out.append(n)
    return list(dict.fromkeys(out))


def _number_in(n: str, text: str) -> bool:
    bare = n.replace(",", "").rstrip("%")
    flat = text.replace(",", "")
    return re.search(rf"(?<![\d.]){re.escape(bare)}(?![\d])", flat) is not None


def _sentences(answer: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])|\n+", answer or "") if x.strip()]


def _public(url: str) -> bool:
    from .mediator import is_public_host
    return is_public_host(urllib.parse.urlsplit(url).hostname or "")


def check(root: Path, answer: str, context: str = "") -> dict | None:
    """None when the answer cites nothing checkable. Else a summary:
    {urls, opened, only_seen[], never_seen[], quotes, quotes_found,
     quotes_missing[], ok}.

    Only public web addresses count (localhost, private addresses and the
    like are the project's own). A link or quote that already appears in
    `context` — the conversation so far: the user's words, tool output —
    is backed by it."""
    urls = [u for u in cited_urls(answer) if _public(u)]
    qs = quotes(answer)
    if not urls and not qs:
        return None
    ctx_keys = {norm_url(u) for u in cited_urls(context)}
    ctx_words = " ".join(_words(context))
    srcs = load(root)
    read = {s["key"]: s for s in srcs if s.get("kind") == "read"}
    seen = {s["key"] for s in srcs}
    opened, only_seen, never = [], [], []
    for u in urls:
        k = norm_url(u)
        if k in read or k in ctx_keys:
            opened.append(u)
        elif k in seen:
            only_seen.append(u)
        else:
            never.append(u)
    found, missing = 0, []
    if qs:
        texts = [" ".join(_words(_text_of(root, s))) for s in read.values()]
        for q in qs:
            if any(_in_text(q, t) for t in texts if t) or (ctx_words and _in_text(q, ctx_words)):
                found += 1
            else:
                missing.append(q)
    # quotes only count when Aura read pages (a quote of the user's own
    # words or of a file isn't a web citation)
    if not read:
        qs, found, missing = [], 0, []
        if not urls:
            return None     # Nothing left to check ("sources: — all checked" was shown)
    # numbers in a sentence that cites a page read → must be in that page
    numbers_bad = []
    for sent in _sentences(answer):
        cited = [read[norm_url(u)] for u in cited_urls(sent) if norm_url(u) in read]
        if not cited:
            continue
        texts = [_text_of(root, r) for r in cited]
        for n in _numbers(sent):
            if not any(_number_in(n, t) for t in texts if t):
                numbers_bad.append({"number": n, "url": cited[0].get("url", ""),
                                    "sentence": sent.strip()[:160]})
    return {"urls": len(urls), "opened": len(opened), "only_seen": only_seen,
            "never_seen": never, "quotes": len(qs), "quotes_found": found,
            "quotes_missing": missing, "numbers_unsupported": numbers_bad,
            "ok": not only_seen and not never and not missing and not numbers_bad}


def summary(c: dict) -> str:
    bits = []
    if c["urls"]:
        bits.append(f"{c['opened']}/{c['urls']} links opened")
    if c["quotes"]:
        bits.append(f"{c['quotes_found']}/{c['quotes']} quotes found in the pages")
    if c.get("numbers_unsupported"):
        bits.append(f"{len(c['numbers_unsupported'])} number(s) not in the page cited")
    return "sources: " + " · ".join(bits) + (" — all checked" if c["ok"] else "")


def fix_message(c: dict) -> str:
    lines = ["[SOURCES] Before you finish — your answer cites things the "
             "pages you read don't back:"]
    for u in c["never_seen"]:
        lines.append(f"- {u}: never opened or found in this project — open it "
                     "with web_crawl, or remove it")
    for u in c["only_seen"]:
        lines.append(f"- {u}: only seen in search results — open it before citing it")
    for q in c["quotes_missing"]:
        lines.append(f'- the quote "{q[:120]}" is in none of the pages you read — '
                     "quote exactly, or say it in your own words without quote marks")
    for b in c.get("numbers_unsupported", []):
        lines.append(f"- {b['number']} is not in {b['url']} (cited for: \"{b['sentence']}\") — "
                     "use the page's own figure, cite where the number really comes from, "
                     "or drop it")
    lines.append("Your answer above already reached the user — don't repeat it. "
                 "Reply with a short correction only: start with 'Correction:', give "
                 "the sentences that change with their sources, or say plainly which "
                 "claims you could not check.")
    return "\n".join(lines)

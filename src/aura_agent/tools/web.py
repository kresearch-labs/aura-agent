"""web.py — read web pages (stdlib only: urllib + html.parser).

1D.1: this was a security crawler — every call first probed admin/,.git/HEAD,
wp-admin/, backup/… on the site, crawled up to 40 pages and inventoried forms
and cookies. For a general agent that is slow, noisy and can get the user
blocked by a site's firewall. Now it is a plain reader:

  - default: fetch ONE page; return its readable text (scripts/styles
    dropped), title and links
  - max_pages > 1: also follow same-site links, breadth first, politely
    (delay between requests), capped by the settings' web_depth
  - no path probes, no form inventory, no cookie jar

The tool keeps its name (web_crawl) so packs, settings and history that refer
to it keep working. Full page texts land in an artifact; the reply carries the
first page inline.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from ..tool_names import WEB_CRAWL, WEB_SEARCH
from ..tools.artifacts import store_artifact

UA = "aura-agent/0.2 (+reader)"
MAX_BYTES = 2_000_000          # Per page read from the network
MAX_PDF_BYTES = 40_000_000     # A PDF is useless when cut short
PAGE_TEXT_CAP = 50_000         # Per page kept in the artifact
INLINE_CHARS = 12_000          # Page text returned inline (head + tail beyond)
MAX_LINKS = 50                 # Links listed in the reply

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
          "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
          "pre", "blockquote", "hr", "dt", "dd"}
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}


class _Reader(HTMLParser):
    """HTML → readable text + title + links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r" *\n *", "\n", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


class _Blocked(Exception):
    pass


def _opener(allow):
    """urllib follows redirects silently — to another site too, which would
    skip the scope (and any skill pack's limits) checked for the first
    URL. Every redirect target is checked with `allow(url) -> reason|None`
    before it is followed."""
    class _Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            why = allow(newurl) if allow else None
            if why:
                raise _Blocked(f"redirected to {newurl} — not followed: {why}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    return urllib.request.build_opener(_Guard)


def fetch(url: str, timeout: float = 15.0, allow=None) -> dict:
    """One GET. Returns {url, status, content_type, title, text, links}."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,text/plain,"
                                                         "application/json,*/*;q=0.5",
                                               "Accept-Encoding": "gzip, deflate"})
    try:
        with _opener(allow).open(req, timeout=timeout) as r:
            final = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            enc = (r.headers.get("Content-Encoding") or "").lower()
            limit = MAX_PDF_BYTES if ("pdf" in ctype.lower()
                                      or final.lower().split("?")[0].endswith(".pdf")) else MAX_BYTES
            raw = r.read(limit)
            status = r.status
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "content_type": "",
                "title": "", "text": "", "links": [],
                "error": f"HTTP {e.code} {e.reason}"}
    except _Blocked as e:
        return {"url": url, "status": 0, "content_type": "", "title": "",
                "text": "", "links": [], "error": str(e)}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"url": url, "status": 0, "content_type": "", "title": "",
                "text": "", "links": [], "error": str(e)}
    # Some servers compress even when not asked (python.org does).
    try:
        if "gzip" in enc or raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            import zlib
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:                           # noqa: BLE001
        return {"url": final, "status": status, "content_type": ctype, "title": "",
                "text": "", "links": [], "error": f"compressed page could not be unpacked: {e}"}
    if enc in ("br", "zstd"):
        return {"url": final, "status": status, "content_type": ctype, "title": "",
                "text": "", "links": [], "error": f"the site sent '{enc}' compression, which can't be read"}
    if "pdf" in ctype.lower() or raw[:5] == b"%PDF-":
        return _pdf_page(final, status, ctype, raw)
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    page = {"url": final, "status": status, "content_type": ctype,
            "title": "", "text": body, "links": [], "html_chars": len(body)}
    if "html" in ctype.lower() or (not ctype and "<html" in body[:500].lower()):
        rd = _Reader()
        try:
            rd.feed(body)
        except Exception:                            # noqa: BLE001
            pass
        page["title"] = " ".join(rd.title.split())
        page["text"] = rd.text()
        page["links"] = list(dict.fromkeys(
            urllib.parse.urljoin(final, h).split("#")[0] for h in rd.links
            if not h.startswith(("javascript:", "mailto:", "#"))))
    page["text"] = page["text"][:PAGE_TEXT_CAP]
    return page


def _pdf_page(url: str, status: int, ctype: str, raw: bytes) -> dict:
    """PDFs are read with pypdf when it is installed."""
    page = {"url": url, "status": status, "content_type": ctype or "application/pdf",
            "title": "", "text": "", "links": []}
    try:
        import io

        from pypdf import PdfReader
    except ImportError:
        page["error"] = ("this is a PDF and the PDF reader isn't installed "
                         "(pip install 'aura-agent[pdf]' or pip install pypdf)")
        return page
    try:
        rd = PdfReader(io.BytesIO(raw))
        page["title"] = str((rd.metadata or {}).get("/Title") or "")
        page["text"] = "\n\n".join((pg.extract_text() or "") for pg in rd.pages)[:PAGE_TEXT_CAP]
        page["pages_in_pdf"] = len(rd.pages)
    except Exception as e:                           # noqa: BLE001
        page["error"] = f"PDF could not be read: {e}"
    return page


def _reader_service(url: str) -> dict | None:
    """A page that is all JavaScript has no text for a plain reader. With
    the user's own Jina key connected, Jina's reader renders it."""
    try:
        import os

        from ..websearch import _secret, user_agent
        if not _secret("JINA_API_KEY"):
            return None
        base = os.environ.get("AURA_WEB_BASE_JINA", "https://r.jina.ai").rstrip("/")
        head = {"User-Agent": user_agent(), "Accept": "text/plain",
                "Authorization": f"Bearer {_secret('JINA_API_KEY')}"}
        req = urllib.request.Request(f"{base}/{url}", headers=head)
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read(MAX_BYTES).decode("utf-8", errors="replace")
        return {"text": text[:PAGE_TEXT_CAP], "via": "reader service (r.jina.ai)"}
    except Exception:                                # noqa: BLE001
        return None


def head_tail(text: str, budget: int) -> tuple[str, bool]:
    """Whole text when it fits; else the first 3/4 and last 1/4 of the
    budget (the end of a page often holds the conclusion)."""
    if len(text) <= budget:
        return text, False
    head = text[:int(budget * 0.75)]
    tail = text[-int(budget * 0.25):]
    left = len(text) - len(head) - len(tail)
    return f"{head}\n\n[... {left:,} characters left out ...]\n\n{tail}", True


def read_site(url: str, max_pages: int = 1, delay: float = 0.3,
              allow=None) -> list[dict]:
    """The start page, then same-site links breadth first, up to max_pages."""
    host = urllib.parse.urlparse(url).netloc
    queue, seen, pages = [url], set(), []
    while queue and len(pages) < max_pages:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        if pages:
            time.sleep(delay)
        pg = fetch(u, allow=allow)
        pages.append(pg)
        for link in pg["links"]:
            if urllib.parse.urlparse(link).netloc == host and link not in seen:
                queue.append(link)
    return pages


def register(mediator: Mediator, dispatch: dict) -> None:
    def allow(url: str) -> str | None:
        host = urllib.parse.urlparse(url).hostname or ""
        if not mediator.scope.allows(WEB_CRAWL, host):
            if mediator._ask_scope(WEB_CRAWL, {"url": url}, host):
                return None                          # The user allowed it (the site card)
            return (f"'{host}' is not in this project's allowed list and the user didn't "
                    "allow it — continue another way")
        from ..pack_limits import check
        for lim in list(getattr(mediator, "pack_limits", {}).values()):
            why = check(lim, WEB_CRAWL, "read", {"url": url})
            if why:
                return f"skill pack '{lim.name}': {why}"
        return None

    def _one(target: str, want: int, delay: float) -> list[dict]:
        why = allow(target)
        if why:
            return [{"url": target, "status": 0, "content_type": "", "title": "",
                     "text": "", "links": [], "error": f"not opened: {why}"}]
        pages = read_site(target, max_pages=want, delay=delay, allow=allow)
        first = pages[0]
        # only a page that is big but almost text-free is a JavaScript
        # page; a page that is simply short (example.com) is left alone
        if (not first.get("error") and "html" in first.get("content_type", "")
                and len(first.get("text", "")) < 300
                and first.get("html_chars", 0) > 10_000):
            alt = _reader_service(first["url"])
            if alt and len(alt["text"]) > len(first["text"]):
                first["text"], first["via"] = alt["text"], alt["via"]
        return pages

    def web_crawl(args) -> str:
        urls = args.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if args.get("url"):
            urls = [str(args["url"])] + [u for u in urls if u != args["url"]]
        urls = [str(u).strip() for u in urls if str(u).strip()][:6]
        if not urls:
            raise Violation("web_crawl: give url (or urls, up to 6)")
        for u in urls:
            if not u.startswith(("http://", "https://")):
                raise Violation(f"web_crawl: url must be http(s): {u}")
        # one page per address unless asked; the settings' web_depth caps
        # how many pages a single address may follow
        try:
            want = max(1, int(args.get("max_pages") or 1))
        except (TypeError, ValueError):
            want = 1
        try:
            from ..config import resolve_settings
            cap = resolve_settings().web_depth
        except Exception:                            # noqa: BLE001
            cap = 100
        if cap:
            want = min(want, int(cap))
        delay = float(args.get("delay", 0.3))
        if len(urls) == 1:
            groups = [_one(urls[0], want, delay)]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                groups = list(pool.map(lambda u: _one(u, want, delay), urls))
        pages = [p for g in groups for p in g]
        art = store_artifact(Path(mediator.root), json.dumps(
            pages, ensure_ascii=False, indent=1).encode(), "web-pages.json")
        # the sources list: every page really read, with its text, so the
        # answer's citations can be checked (sources.py)
        from ..sources import record
        mon = getattr(mediator, "_research", None)
        for p in pages:
            if p.get("text") and not p.get("error"):
                record(Path(mediator.root), p["url"], p.get("title", ""), p["text"])
                if mon is not None:
                    mon.saw_read(p["url"])
        budget = INLINE_CHARS if len(groups) == 1 else max(2_000, INLINE_CHARS // len(groups))
        replies = []
        for g in groups:
            first = g[0]
            reply = {"url": first["url"], "status": first["status"],
                     "title": first["title"],
                     "content_type": first["content_type"],
                     "pages_crawled": len(g)}
            for k in ("error", "via", "pages_in_pdf"):
                if first.get(k):
                    reply[k] = first[k]
            text, cut = head_tail(first.get("text", ""), budget)
            reply["text"] = text + (
                f"\n[full text: {len(first['text']):,} chars in artifact "
                f"'{art['artifact_ref']}' — read it with read_file]" if cut else "")
            reply["links"] = first["links"][:MAX_LINKS if len(groups) == 1 else 10]
            if len(g) > 1:
                reply["other_pages"] = [
                    {"url": p["url"], "status": p["status"], "title": p["title"],
                     "chars": len(p["text"])} for p in g[1:]]
            replies.append(reply)
        out = replies[0] if len(replies) == 1 else {"pages": replies}
        out["artifact_ref"] = art["artifact_ref"]
        return json.dumps(out, ensure_ascii=False)

    def _target(a: dict):
        u = a.get("url")
        if not u:
            us = a.get("urls")
            u = (us[0] if us else "") if isinstance(us, list) else (us or "")
        return urllib.parse.urlparse(str(u)).hostname

    mediator.register(ToolSpec(WEB_CRAWL, BlastRadius.READ, _target))
    dispatch[WEB_CRAWL] = web_crawl

    # web_search (websearch.py)
    def web_search(args) -> str:
        from .. import websearch as W
        query = str(args.get("query") or "").strip()
        if not query:
            raise Violation("web_search: give a query")
        try:
            limit = max(1, min(20, int(args.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        cons = W.consent() or "on"
        note = ""
        level = getattr(getattr(mediator, "_research", None), "level", None) or "normal"
        res = W.search(query, kind=str(args.get("kind") or "auto"), limit=limit,
                       deep=bool(args.get("deep")), cons=cons, level=level)
        from ..sources import record
        for x in res.hits:
            record(Path(mediator.root), x["url"], x.get("title", ""), kind="seen")
        mon = getattr(mediator, "_research", None)
        if mon is not None:                  # Novelty of this search
            mon.saw_search([x["url"] for x in res.hits])
        return W.render(res) + note

    mediator.register(ToolSpec(WEB_SEARCH, BlastRadius.READ, lambda a: None),
                      plan_safe=True)
    dispatch[WEB_SEARCH] = web_search

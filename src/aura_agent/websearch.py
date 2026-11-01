"""websearch.py — web search for the agent.

  - Connectors, no favourite: each service works with the user's own key
    or server (public release: the service's terms are between the user
    Overflow, GitHub) only when the user switches them on.
  - Health-aware: every engine's results are remembered (on disk). An
    error, a timeout, a captcha page — and an EMPTY answer — count as
    failures; a failing engine rests for a while and the next one answers.
  - The right engine for the question (deterministic rules): papers →
    facts → Wikipedia, the rest → general engines.
    duplicates and ranks up what several engines agree on.

Stdlib only; every engine's address can be overridden (tests, mirrors) with
AURA_WEB_BASE_<NAME>. No user identifiers are sent.
"""
from __future__ import annotations

import gzip
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TIMEOUT = 12.0


def user_agent() -> str:
    """Wikimedia and others block clients that don't say who they are. Aura
    names itself; AURA_CONTACT (an email or URL — your choice) is added
    when set, as their policies ask for."""
    contact = os.environ.get("AURA_CONTACT", "").strip()
    return "aura-agent/0.2 (self-hosted AI agent" + (f"; {contact}" if contact else "") + ")"


# Minimum seconds between requests to one service, from its own rules
_PACE = {"export.arxiv.org": 3.0, "eutils.ncbi.nlm.nih.gov": 0.34,
         "html.duckduckgo.com": 1.0}
_last_call: dict[str, float] = {}
_pace_lock = __import__("threading").Lock()


def _pace(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    gap = _PACE.get(host)
    if not gap:
        return
    with _pace_lock:
        wait = _last_call.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()

OWN = "own"                # Your key or your own server
OPEN = "open"              # Official open public API, no account needed
WEB = "web"                # General web search with no account


class EngineError(Exception):
    pass


class Captcha(EngineError):
    pass


# ------------------------------------------------------------------ http --
def _base(name: str, default: str) -> str:
    return os.environ.get(f"AURA_WEB_BASE_{name.upper()}", default).rstrip("/")


def _http(url: str, data: dict | None = None, headers: dict | None = None,
          timeout: float = TIMEOUT, raw: bool = False):
    _pace(url)
    h = {"User-Agent": user_agent(), "Accept": "application/json, text/html;q=0.8",
         "Accept-Encoding": "gzip"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read(3_000_000)
            if r.headers.get("Content-Encoding") == "gzip" or payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)
            text = payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise EngineError(f"HTTP {e.code} (key refused or blocked)")
        if e.code in (429, 503):
            raise EngineError(f"HTTP {e.code} (rate limit or busy)")
        raise EngineError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EngineError(f"unreachable: {getattr(e, 'reason', e)}")
    if _looks_like_captcha(text):
        raise Captcha("captcha / bot-check page")
    if raw:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise EngineError("answer was not JSON")


_CAPTCHA = re.compile(r"captcha|unusual traffic|are you a robot|anomaly-modal|"
                      r"verify you are human|challenge-form", re.I)


def _looks_like_captcha(text: str) -> bool:
    head = text[:20_000]
    return bool(_CAPTCHA.search(head)) and "<html" in head.lower()


def _clean(s) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", str(s or "")))
    return " ".join(s.split())


def _hit(title, url, snippet="") -> dict:
    return {"title": _clean(title)[:200], "url": str(url or "").strip(),
            "snippet": _clean(snippet)[:300]}


# --------------------------------------------------------------- engines --
@dataclass
class Engine:
    name: str
    label: str
    category: str                    # OWN | OPEN
    kinds: tuple[str, ...]           # General / papers / code / facts
    run: Callable[[str, int], list[dict]]
    needs: str = ""                  # env/keystore name this engine needs

    def available(self) -> bool:
        return not self.needs or bool(_secret(self.needs))


def _secret(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        from .keystore import get
        return get(name) or ""
    except Exception:                                # noqa: BLE001
        return ""


def _brave(q, n):
    d = _http(_base("brave", "https://api.search.brave.com") + "/res/v1/web/search?"
              + urllib.parse.urlencode({"q": q, "count": n}),
              headers={"X-Subscription-Token": _secret("BRAVE_API_KEY")})
    return [_hit(r.get("title"), r.get("url"), r.get("description"))
            for r in (d.get("web") or {}).get("results", [])]


def _tavily(q, n):
    head = {"Authorization": f"Bearer {_secret('TAVILY_API_KEY')}"}
    d = _http(_base("tavily", "https://api.tavily.com") + "/search",
              {"query": q, "max_results": n}, headers=head)
    return [_hit(r.get("title"), r.get("url"), r.get("content")) for r in d.get("results", [])]


def _exa(q, n):
    d = _http(_base("exa", "https://api.exa.ai") + "/search",
              {"query": q, "numResults": n, "type": "auto",
               "contents": {"text": {"maxCharacters": 300}}},
              headers={"x-api-key": _secret("EXA_API_KEY")})
    return [_hit(r.get("title"), r.get("url"), r.get("text") or r.get("summary"))
            for r in d.get("results", [])]


def _serper(q, n):
    d = _http(_base("serper", "https://google.serper.dev") + "/search",
              {"q": q, "num": n}, headers={"X-API-KEY": _secret("SERPER_API_KEY")})
    return [_hit(r.get("title"), r.get("link"), r.get("snippet")) for r in d.get("organic", [])]


def _firecrawl(q, n):
    head = {"Authorization": f"Bearer {_secret('FIRECRAWL_API_KEY')}"}
    d = _http(_base("firecrawl", "https://api.firecrawl.dev") + "/v2/search",
              {"query": q, "limit": n}, headers=head)
    data = d.get("data", [])
    items = data.get("web", []) if isinstance(data, dict) else data   # V2 groups by source
    return [_hit(r.get("title"), r.get("url"), r.get("description")) for r in items]


def _searxng(q, n):
    d = _http(_secret("SEARXNG_URL").rstrip("/") + "/search?"
              + urllib.parse.urlencode({"q": q, "format": "json"}))
    return [_hit(r.get("title"), r.get("url"), r.get("content"))
            for r in d.get("results", [])][:n]


def _marginalia(q, n):
    # Its data is CC-BY-NC-SA by default.
    key = _secret("MARGINALIA_API_KEY")
    text = _http(_base("marginalia", "https://api2.marginalia-search.com") + "/search?"
                 + urllib.parse.urlencode({"query": q, "count": n}),
                 headers={"API-Key": key}, raw=True)
    if "limit exceeded" in text[:200].lower():
        raise EngineError("the key's daily limit is reached")
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        raise EngineError("answer was not JSON")
    return [_hit(r.get("title"), r.get("url"), r.get("description"))
            for r in d.get("results", [])][:n]


def _duckduckgo(q, n):
    """Its HTML page; a bot-check page rests it (_pace keeps it to one search a
    second).
    """
    page = _http(_base("duckduckgo", "https://html.duckduckgo.com") + "/html/?"
                 + urllib.parse.urlencode({"q": q}), raw=True)
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
                         r'(?:.*?class="result__snippet"[^>]*>(.*?)</a>)?', page, re.S):
        href = html.unescape(m.group(1))
        if "uddg=" in href:
            href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
        out.append(_hit(m.group(2), href, m.group(3) or ""))
        if len(out) >= n:
            break
    return out


def _wikipedia(q, n):
    base = _base("wikipedia", "https://en.wikipedia.org")
    d = _http(base + "/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": q, "format": "json",
         "srlimit": n}))
    return [_hit(r.get("title"),
                 base + "/wiki/" + urllib.parse.quote(str(r.get("title", "")).replace(" ", "_")),
                 r.get("snippet")) for r in (d.get("query") or {}).get("search", [])]


_PAPER_NOISE = {"paper", "papers", "study", "studies", "research", "on", "the", "of",
                "a", "an", "and", "for", "in", "recent", "latest", "about", "arxiv",
                "is", "are", "to", "with", "preprint", "article"}


def _arxiv(q, n):
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", q)
             if w.lower() not in _PAPER_NOISE] or [q]
    query = " AND ".join(f"all:{w}" for w in words[:8])
    page = _http(_base("arxiv", "https://export.arxiv.org") + "/api/query?"
                 + urllib.parse.urlencode({"search_query": query, "max_results": n,
                                           "sortBy": "relevance"}),
                 raw=True)
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", page, re.S):
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        u = re.search(r"<id>(.*?)</id>", e, re.S)
        s = re.search(r"<summary>(.*?)</summary>", e, re.S)
        if u:
            out.append(_hit(t.group(1) if t else "", u.group(1).strip(), s.group(1) if s else ""))
    return out[:n]


def _openalex(q, n):
    d = _http(_base("openalex", "https://api.openalex.org") + "/works?"
              + urllib.parse.urlencode({"search": q, "per-page": n,
                                        "api_key": _secret("OPENALEX_API_KEY")}))
    out = []
    for r in d.get("results", []):
        url = r.get("doi") or r.get("id")
        venue = ((r.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        out.append(_hit(r.get("display_name"), url,
                        f"{r.get('publication_year', '')} {venue} · cited {r.get('cited_by_count', 0)}"))
    return out


def _pubmed(q, n):
    base = _base("pubmed", "https://eutils.ncbi.nlm.nih.gov") + "/entrez/eutils"
    key = {"api_key": _secret("NCBI_API_KEY")} if _secret("NCBI_API_KEY") else {}
    ids = (_http(base + "/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": q, "retmode": "json", "retmax": n, **key}))
           .get("esearchresult", {}).get("idlist", []))
    if not ids:
        return []
    d = _http(base + "/esummary.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json", **key})).get("result", {})
    return [_hit(d.get(i, {}).get("title"), f"https://pubmed.ncbi.nlm.nih.gov/{i}/",
                 f"{d.get(i, {}).get('pubdate', '')} {d.get(i, {}).get('source', '')}")
            for i in ids]


def _github(q, n):
    tok = _secret("GITHUB_TOKEN")
    head = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        head["Authorization"] = f"Bearer {tok}"
    d = _http(_base("github", "https://api.github.com") + "/search/repositories?"
              + urllib.parse.urlencode({"q": q, "per_page": n}), headers=head)
    return [_hit(r.get("full_name"), r.get("html_url"),
                 f"{r.get('description') or ''} · ★{r.get('stargazers_count', 0)}")
            for r in d.get("items", [])]


def _stackexchange(q, n):
    d = _http(_base("stackexchange", "https://api.stackexchange.com") + "/2.3/search/advanced?"
              + urllib.parse.urlencode({"order": "desc", "sort": "relevance", "q": q,
                                        "site": "stackoverflow", "pagesize": n,
                                        **({"key": _secret("STACKEXCHANGE_KEY")}
                                           if _secret("STACKEXCHANGE_KEY") else {})}))
    return [_hit(r.get("title"), r.get("link"),
                 f"{'answered' if r.get('is_answered') else 'no accepted answer'} · score {r.get('score', 0)}")
            for r in d.get("items", [])]


ENGINES: tuple[Engine, ...] = (
    # your own account or server — the service's terms are between you and it
    Engine("brave", "Brave Search", OWN, ("general",), _brave, "BRAVE_API_KEY"),
    Engine("tavily", "Tavily", OWN, ("general",), _tavily, "TAVILY_API_KEY"),
    Engine("exa", "Exa", OWN, ("general", "papers"), _exa, "EXA_API_KEY"),
    Engine("serper", "Serper (Google results)", OWN, ("general",), _serper, "SERPER_API_KEY"),
    Engine("firecrawl", "Firecrawl", OWN, ("general",), _firecrawl, "FIRECRAWL_API_KEY"),
    Engine("marginalia", "Marginalia", OWN, ("general",), _marginalia, "MARGINALIA_API_KEY"),
    Engine("searxng", "your SearXNG", OWN, ("general",), _searxng, "SEARXNG_URL"),
    Engine("openalex", "OpenAlex", OWN, ("papers",), _openalex, "OPENALEX_API_KEY"),
    # general web search with no account — the everyday level (normal)
    Engine("duckduckgo", "DuckDuckGo", WEB, ("general",), _duckduckgo),
    # official open public APIs (no accounts exist, or an optional key) —
    # only when you switch them on
    Engine("wikipedia", "Wikipedia", OPEN, ("facts",), _wikipedia),
    Engine("arxiv", "arXiv", OPEN, ("papers",), _arxiv),
    Engine("pubmed", "PubMed", OPEN, ("medical",), _pubmed),
    Engine("stackexchange", "Stack Overflow", OPEN, ("code",), _stackexchange),
    Engine("github", "GitHub", OPEN, ("code",), _github),
)
BY_NAME = {e.name: e for e in ENGINES}

# where each one's terms are (README "Services Aura can contact" lists them)
TERMS = {
    "brave": "https://api-dashboard.search.brave.com/terms-of-service",
    "tavily": "https://tavily.com/terms",
    "exa": "https://exa.ai/assets/Exa_Labs_Terms_of_Service.pdf",
    "serper": "https://serper.dev/terms",
    "firecrawl": "https://www.firecrawl.dev/terms-of-service",
    "marginalia": "https://about.marginalia-search.com/article/api/",
    "openalex": "https://openalex.org/terms",
    "wikipedia": "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
    "arxiv": "https://info.arxiv.org/help/api/tou.html",
    "pubmed": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    "stackexchange": "https://stackoverflow.com/legal/api-terms-of-use",
    "github": "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
    "duckduckgo": "https://duckduckgo.com/terms",
}


# ---------------------------------------------------------------- consent --
CONSENT_KEY = "web_open_sources"     # "on" | "off" | missing = on


def consent() -> str | None:
    """'on', 'off', or None."""
    v = os.environ.get("AURA_WEB_OPEN_SOURCES")
    if not v:
        try:
            from .config import load_saved_settings
            d = load_saved_settings()
            v = d.get(CONSENT_KEY)
            if v is None and d.get("web_free_engines") in ("all", "knowledge"):
                v = "on"                        # The earlier setting name
            elif v is None and d.get("web_free_engines") == "off":
                v = "off"
        except Exception:                            # noqa: BLE001
            v = None
    if v in ("all", "knowledge", "1", "yes", "true"):
        v = "on"
    if v in ("0", "no", "false"):
        v = "off"
    return v if v in ("on", "off") else None


def set_consent(value: str) -> None:
    from .config import load_saved_settings, save_saved_settings
    d = load_saved_settings()
    d[CONSENT_KEY] = "on" if value in ("on", "all", "knowledge") else "off"
    save_saved_settings(d)


def allowed(e: Engine, cons: str | None, level: str = "normal") -> bool:
    if e.category == OWN:
        return e.available()
    if e.category == WEB and level == "basic":
        return False                       # Basic: only the listed sources
    return cons == "on"


# ----------------------------------------------------------------- health --
def _health_path() -> Path:
    from .paths import aura_home
    return aura_home() / "web_engines.json"


def load_health() -> dict:
    try:
        return json.loads(_health_path().read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        return {}


def _save_health(h: dict) -> None:
    try:
        p = _health_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(h, indent=1), encoding="utf-8")
    except OSError:
        pass


def resting(name: str, h: dict | None = None) -> float:
    """Seconds this engine still rests (0 = ready)."""
    rec = (h if h is not None else load_health()).get(name) or {}
    return max(0.0, float(rec.get("rest_until", 0)) - time.time())


import threading as _threading
_HEALTH_LOCK = _threading.Lock()


def _record(name: str, outcome: str, ms: float, why: str = "") -> None:
    with _HEALTH_LOCK:
        _record_locked(name, outcome, ms, why)


def _record_locked(name: str, outcome: str, ms: float, why: str = "") -> None:
    h = load_health()
    r = h.setdefault(name, {"ok": 0, "empty": 0, "fail": 0, "streak": 0})
    if outcome == "ok":
        r["ok"] += 1
        r["streak"] = 0
        r["rest_until"] = 0
    else:
        r["empty" if outcome == "empty" else "fail"] += 1
        r["streak"] = r.get("streak", 0) + 1
        r["last_problem"] = (why or outcome)[:120]
        # an empty answer rests only after a few in a row; a failure at once,
        # longer each time (5 min, 10, 20 … at most an hour)
        if outcome != "empty" or r["streak"] >= 3:
            r["rest_until"] = time.time() + min(3600, 300 * 2 ** (r["streak"] - 1))
    r["ms"] = round(ms if not r.get("ms") else 0.7 * r["ms"] + 0.3 * ms)
    r["last_used"] = round(time.time())
    h[name] = r
    _save_health(h)


# ---------------------------------------------------------------- routing --
_KIND_WORDS = {
    "papers": r"\b(paper|papers|study|studies|arxiv|research on|journal|doi|"
              r"preprint|clinical|trial|meta-analysis|survey of|citation)\b",
    "code": r"\b(error|exception|traceback|stack trace|segfault|undefined|"
            r"npm|pip|cargo|gradle|maven|library|package|api|sdk|framework|"
            r"python|javascript|typescript|rust|golang|java|c\+\+|regex|sql|"
            r"github|repo|repository|stackoverflow|compile|build fails)\b",
    "medical": r"\b(clinical|trial|patient|patients|disease|drug|dose|gene|genes|"
               r"protein|cancer|tumou?r|therapy|treatment|diagnosis|vaccine|virus|"
               r"medical|medicine|health|pubmed|symptom|syndrome|infection)\b",
    "facts": r"^(who|what|when|where) (is|was|are|were|did)\b|\b(history of|"
             r"biography|born|capital of|population of|definition of|wikipedia)\b",
}


def kind_of(query: str) -> str:
    q = query.lower()
    if re.search(_KIND_WORDS["papers"], q) and re.search(_KIND_WORDS["medical"], q):
        return "medical"
    for kind in ("papers", "code", "facts"):
        if re.search(_KIND_WORDS[kind], q):
            return kind
    return "general"


def plan(kind: str, cons: str | None, h: dict | None = None,
         level: str = "normal") -> list[Engine]:
    """Engines to try, best first: the specialists for this kind, then your
    own general engines, then the free general ones — resting ones last."""
    h = h if h is not None else load_health()
    spec = [e for e in ENGINES if kind in e.kinds and kind != "general"]
    if kind == "medical":           # Medical papers: PubMed first, then the paper engines
        spec += [e for e in ENGINES if "papers" in e.kinds]
    general = [e for e in ENGINES if "general" in e.kinds]
    order = []
    for e in spec + [e for e in general if e.category == OWN] + \
            [e for e in general if e.category != OWN]:
        if e not in order and allowed(e, cons, level):
            order.append(e)
    if not order and cons == "on":
        # They cover facts, code and papers; try those
        order = [e for e in ENGINES if e.category == OPEN and allowed(e, cons, level)
                 and e.kinds[0] in ("facts", "code", "papers")]
    return sorted(order, key=lambda e: resting(e.name, h) > 0)


def _norm(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    q = "&".join(x for x in p.query.split("&") if x and not x.startswith(("utm_", "ref=")))
    return f"{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def _try(e: Engine, query: str, n: int) -> tuple[list[dict], str]:
    t0 = time.monotonic()
    try:
        hits = [x for x in e.run(query, n) if x.get("url", "").startswith("http")]
    except Captcha as ex:
        _record(e.name, "captcha", (time.monotonic() - t0) * 1000, str(ex))
        return [], f"{e.label}: {ex}"
    except Exception as ex:                          # noqa: BLE001
        _record(e.name, "fail", (time.monotonic() - t0) * 1000, str(ex))
        return [], f"{e.label}: {ex}"
    ms = (time.monotonic() - t0) * 1000
    if not hits:
        _record(e.name, "empty", ms)
        return [], f"{e.label}: no results"
    _record(e.name, "ok", ms)
    return hits, ""


@dataclass
class SearchResult:
    query: str
    kind: str
    hits: list[dict] = field(default_factory=list)      # Merged, ranked
    used: list[str] = field(default_factory=list)       # Engines that answered
    problems: list[str] = field(default_factory=list)   # Engines that didn't
    none_allowed: bool = False
    open_only: bool = False      # A general question, only the open sources on


def merge(results: dict[str, list[dict]]) -> list[dict]:
    """Reciprocal-rank merge; duplicates folded, engines listed."""
    by: dict[str, dict] = {}
    for eng, hits in results.items():
        for rank, x in enumerate(hits):
            k = _norm(x["url"])
            cur = by.get(k)
            if cur is None:
                cur = dict(x, engines=[], score=0.0)
                by[k] = cur
            cur["engines"].append(eng)
            cur["score"] += 1.0 / (rank + 2)
            if len(x.get("snippet", "")) > len(cur.get("snippet", "")):
                cur["snippet"] = x["snippet"]
    return sorted(by.values(), key=lambda r: (-len(r["engines"]), -r["score"]))


def search(query: str, kind: str = "auto", limit: int = 8, deep: bool = False,
           cons: str | None = None, level: str = "normal") -> SearchResult:
    kind = kind_of(query) if kind in ("", "auto") else kind
    res = SearchResult(query=query, kind=kind)
    order = plan(kind, cons, level=level)
    if not order:
        res.none_allowed = True
        return res
    res.open_only = kind == "general" and all(e.category == OPEN for e in order)
    want = 3 if deep else (2 if kind != "general" else 1)
    got: dict[str, list[dict]] = {}
    if deep:
        # research mode: ask several at once
        ready = [e for e in order if resting(e.name) == 0][:4] or order[:2]
        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
            outs = list(pool.map(lambda e: (e, _try(e, query, limit)), ready))
        for e, (hits, prob) in outs:
            if hits:
                got[e.label] = hits
            elif prob:
                res.problems.append(prob)
    # one by one until enough engines answered (a specialist + a general)
    for e in order:
        if len(got) >= want:
            break
        if e.label in got or any(p.startswith(e.label + ":") for p in res.problems):
            continue
        hits, prob = _try(e, query, limit)
        if hits:
            got[e.label] = hits
        else:
            res.problems.append(prob)
    res.used = list(got)
    res.hits = merge(got)[:limit]
    return res


def render(res: SearchResult) -> str:
    if res.none_allowed:
        return ("web_search: no search service is connected. The user can "
                "connect one with their own key (`/web` lists them: Brave, Tavily, "
                "Exa, Serper, Firecrawl, Marginalia, OpenAlex, or their own "
                "SearXNG), or switch on the open public sources (Wikipedia, "
                "arXiv, PubMed, Stack Overflow, GitHub) with `/web open on`. "
                "Tell the user; don't guess an answer.")
    narrow = ("\nOnly the open public sources are on (Wikipedia, arXiv, PubMed, Stack "
              "Overflow, GitHub) — they cover facts, papers and code, not the whole web. "
              "For a general web search the user can connect a service with their own "
              "key (`/web` lists them)." if res.open_only else "")
    if not res.hits:
        why = "; ".join(res.problems) or "no results"
        return (f"web_search: nothing found for {res.query!r} ({why}). Try other "
                "words, or say you couldn't find it." + narrow)
    lines = [f"search {res.query!r} [{res.kind}] — {len(res.hits)} results via "
             f"{', '.join(res.used)}"]
    for i, x in enumerate(res.hits, 1):
        lines.append(f"{i}. {x['title'] or x['url']}\n   {x['url']}")
        if x.get("snippet"):
            lines.append(f"   {x['snippet'][:240]}")
        if len(x.get("engines", [])) > 1:
            lines.append(f"   (found by {len(x['engines'])}: {', '.join(x['engines'])})")
    if res.problems:
        lines.append("not answering: " + "; ".join(res.problems))
    lines.append("Before answering, read the 2-3 most relevant pages with web_crawl "
                 "(several at once: urls=[...]); cite only what you read." + narrow)
    return "\n".join(lines)


def status_lines() -> list[str]:
    """For /web: every connector — connected / switched on / health."""
    cons = consent()
    h = load_health()
    out = ["no-account sources (DuckDuckGo, Wikipedia, arXiv, PubMed, Stack Overflow, "
           "GitHub): " + ("off (/web open on)" if cons == "off" else "on (/web open off)")]
    cons = cons or "on"
    for e in ENGINES:
        if e.category == OWN:
            state = "connected" if e.available() else f"not connected ({e.needs})"
        else:
            state = "on" if allowed(e, cons) else "off (no account)"
        r = h.get(e.name) or {}
        rest = resting(e.name, h)
        health = (f"ok {r.get('ok', 0)} · empty {r.get('empty', 0)} · failed {r.get('fail', 0)}"
                  + (f" · ~{r['ms']} ms" if r.get("ms") else "")
                  + (f" · resting {int(rest // 60) + 1} min ({r.get('last_problem', '')})"
                     if rest else "")) if r else "not used yet"
        out.append(f"  {e.label:26s} {', '.join(e.kinds):14s} {state:28s} {health}")
    return out

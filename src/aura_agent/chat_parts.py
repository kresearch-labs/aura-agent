"""chat_parts.py — a long chat as a chain of parts, indexed as it is written.

A chat never gets re-squashed into summaries of summaries (that is how long
chats get poisoned: old, half-true lines pile up and look current). Instead:

  - the chat's file is the CURRENT part. When it holds about PART_TOKENS of
    chat, at the end of a finished exchange, it is moved into the chat's parts
    folder (<chat>.parts/part-0001.jsonl, …) unchanged, and a fresh current
    part starts with the chat's own records, a short code-made "where we are"
    note and the last exchange word for word. The model works only from the
    current part; older parts are opened only when something is needed.
  - every exchange gets ONE index line, written by code as it happens (no
    model call): when, what was asked, what was done (tools, files), errors,
    a decision mark, and where it is (part + seq). Short and specific: file
    names, error names and commands are what find the right spot.
  - every finished part gets ONE master line (dates, topics, decisions).
  - a small DECISIONS list per chat holds what is true now: a new decision
    REPLACES an old one (the old one stays, marked "replaced on <date> by"),
    and decisions that depended on a replaced one are flagged to check. It
    goes with every model call; each entry points to where it was said.
  - recall: master index → one part's index → the exact spot in the files.

Sizes and shapes, from the long-context and chat-memory research the design
was checked against: parts of ~24k tokens split by size at a finished
exchange — few coarse parts beat many topic-split ones, because every extra
bookmark makes the right one harder to pick; the always-visible part of the
index is KEYWORD-ONLY bookmarks (a bookmark with text in it makes the model
think it already knows and stop looking — it then guesses); keywords are the
SPECIFIC ones (file and error names, numbers, dates, proper names), which
decide whether the right part is found; and "don't guess — recall first".
Outdated facts are what that work leaves open: the decisions list (newest
wins, replaced ones marked, dependents flagged) covers it.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path

PART_TOKENS = 24_000          # A part holds about this much chat
CHARS_PER_TOKEN = 4
CARRY_TOKENS = 5_000          # The end of a closed part the next one starts with
DECISIONS_MAX_CHARS = 8_000   # The list sent with every call (~2k tokens)
RECALL_MAX_CHARS = 12_000     # What one recall of the exact spot gives back (~3k tokens)

_DECISION = re.compile(
    r"\b(from now on|never|always|don'?t|do not|stop (?:doing|making|using)|no more|"
    r"we decided|decided|instead of|use .{1,30} instead|must not|must|keep it|"
    r"switch to|change (?:it|that) to|from today)\b", re.I)
_STOP = set("""a an the and or but if then to of in on at for with from by is are was were be
been it its this that these those i you we they he she me my our your us them as so do does
did not no yes can could would should will just also into out up down over than there here what
which who how why when where all any some more most other such only own same too very s t now
let lets please ok okay""".split())


# ------------------------------------------------------------------ files --
def parts_dir(chat_path: Path) -> Path:
    p = Path(chat_path)
    return p.with_name(p.stem + ".parts")


def part_files(chat_path: Path) -> list[Path]:
    d = parts_dir(chat_path)
    return sorted(d.glob("part-*.jsonl")) if d.is_dir() else []


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def current_part(chat_path: Path) -> int:
    return len(part_files(chat_path)) + 1


def head_tokens(chat_path: Path) -> int:
    """About how much chat the current part holds."""
    try:
        return Path(chat_path).stat().st_size // CHARS_PER_TOKEN
    except OSError:
        return 0


# ---------------------------------------------------------- index lines --
def _text(rec: dict) -> str:
    c = rec.get("content")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def _words(s: str, n: int) -> str:
    w = " ".join(str(s).split()).split(" ")
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def _tool_bit(call: dict) -> tuple[str, list[str]]:
    """'edit fs.py' / 'ran pytest' and the specific words it carries."""
    fn = call.get("function") or call
    name = str(fn.get("name") or call.get("name") or "")
    args = fn.get("arguments") or call.get("parsed_arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    keys: list[str] = []
    what = ""
    if isinstance(args, dict):
        if args.get("path"):
            what = os.path.basename(str(args["path"]).rstrip("/\\")) or str(args["path"])
            keys.append(what)
        elif args.get("cmd"):
            parts = str(args["cmd"]).split()
            what = " ".join(os.path.basename(p.strip('"')) for p in parts[:2])
            keys += [os.path.basename(p.strip('"')) for p in parts[:3]]
        elif args.get("pattern"):
            what = repr(str(args["pattern"])[:30])
            keys.append(str(args["pattern"])[:30])
        elif args.get("url"):
            what = re.sub(r"^https?://", "", str(args["url"]))[:40]
        elif args.get("name"):
            what = str(args["name"])[:30]
            keys.append(what)
    short = {"read_file": "read", "write_file": "wrote", "edit_file": "edit",
             "run_command": "ran", "search_files": "search", "list_files": "list",
             "web_crawl": "web", "web_search": "websearch", "process_start": "start"}.get(name, name)
    return (f"{short} {what}".strip(), keys)


_NAMEY = re.compile(r"\b(?:[A-Z][\w\-]*(?:\+[A-Z][\w\-]*)+|[A-Z][\w\-]{2,}|"
                    r"[\w\-]+\.(?:py|js|ts|tsx|md|json|toml|yml|yaml|txt|"
                    r"sh|ps1|go|rs|java|c|cpp|h|css|html|svelte|zip|pdf|db|jsonl)|"
                    r"\d+(?:[.,:/\-]\d+)*[a-zA-Z%]{0,3}|[a-z]+_[a-z_]+|--?[a-z][\w\-]+)\b")


def _specific(text: str, n: int = 8) -> list[str]:
    """The words that tell this moment apart: names, file names, numbers and
    dates, snake_case names, flags — not common words."""
    out = []
    for w in _NAMEY.findall(text or ""):
        if w.lower() in _STOP or len(w) < 2 or w in out:
            continue
        out.append(w)
        if len(out) >= n:
            break
    return out


_COMMON = set("""want need make made build built using used work works working change changed
going thing things there those these about after again could would should still really maybe
first later never always every think tell said says look looks help keep from tests test
please through instead going doing until while without within""".split())


def _content_words(text: str, n: int = 3) -> list[str]:
    """The plain words that carry a decision ('zips', 'repos') when it has no
    names or numbers."""
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", text or ""):
        lw = w.lower()
        if lw in _STOP or lw in _COMMON or lw in out:
            continue
        out.append(lw)
        if len(out) >= n:
            break
    return out


_ERR = re.compile(r"\b([A-Z][A-Za-z]+(?:Error|Exception))\b|\bexit(?:ed with)?(?: code)? ([1-9]\d*)\b")


def exchange_line(part: int, recs: list[dict]) -> dict | None:
    """One exchange (a user message to Aura's answer) → its index entry."""
    from .notes import is_users_words, users_words
    user = next((users_words(r) for r in recs if is_users_words(r)), "")
    if not user and not any(r.get("role") == "assistant" for r in recs):
        return None
    did: list[str] = []
    keys: list[str] = []
    errs: list[str] = []
    answer = ""
    for r in recs:
        if r.get("role") == "assistant":
            for c in r.get("tool_calls") or []:
                bit, k = _tool_bit(c)
                if bit and bit not in did:
                    did.append(bit)
                keys += k
            t = _text(r)
            if t.strip() and not r.get("tool_calls"):
                answer = t
        elif r.get("role") == "tool":
            t = _text(r)[:3000]
            for m in _ERR.finditer(t):
                e = m.group(1) or f"exit {m.group(2)}"
                if e not in errs:
                    errs.append(e)
    seqs = [int(r.get("seq") or 0) for r in recs if r.get("seq")]
    ts = next((float(r["ts"]) for r in recs if r.get("ts")), time.time())
    decision = bool(_DECISION.search(user))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    line = (f"p{part} #{min(seqs) if seqs else 0} {when}"
            + (" DECISION" if decision else "")
            + f" | you: {_words(user, 12)}"
            + (f" | did: {', '.join(did[:5])}" + (f" +{len(did) - 5}" if len(did) > 5 else "")
               if did else "")
            + (f" | ✗ {', '.join(errs[:3])}" if errs else "")
            + (f" | → {_words(answer, 10)}" if answer else ""))
    said = _specific(user) or _content_words(user)
    keys = (said + keys) if decision else (keys + said)   # A decision: its own words first
    keys += errs
    return {"part": part, "from": min(seqs) if seqs else 0, "to": max(seqs) if seqs else 0,
            "ts": round(ts, 3), "decision": decision, "line": line,
            "keys": list(dict.fromkeys(keys))[:16], "user": user[:400]}


def write_exchange(chat_path: Path, recs: list[dict]) -> dict | None:
    e = exchange_line(current_part(chat_path), recs)
    if e:
        _append(parts_dir(chat_path) / "index.jsonl", e)
    return e


def index_entries(chat_path: Path, part: int | None = None) -> list[dict]:
    rows = _read_jsonl(parts_dir(chat_path) / "index.jsonl")
    return [r for r in rows if part is None or r.get("part") == part]


def master_entries(chat_path: Path) -> list[dict]:
    return _read_jsonl(parts_dir(chat_path) / "master.jsonl")


def _master_line(chat_path: Path, part: int, recs: list[dict]) -> dict:
    rows = index_entries(chat_path, part)
    words: Counter = Counter()
    for r in rows:
        for k in r.get("keys") or []:
            if k.lower() not in _STOP:
                words[k] += 1
    ts = [float(r["ts"]) for r in recs if r.get("ts")]
    seqs = [int(r["seq"]) for r in recs if r.get("seq")]
    decisions = [_words(r.get("user", ""), 10) for r in rows if r.get("decision")]
    a = time.strftime("%Y-%m-%d", time.localtime(min(ts))) if ts else "?"
    b = time.strftime("%Y-%m-%d", time.localtime(max(ts))) if ts else "?"
    topics = [w for w, _ in words.most_common(7)]
    dec = list(dict.fromkeys(k for r in rows if r.get("decision")
                             for k in (_specific(r.get("user", "")) or _content_words(r.get("user", "")))[:2]))[:3]
    # keyword-only: no text from the part (a bookmark that reads like an
    # answer stops the model from looking it up)
    line = (f"[p{part} {a}" + (f"→{b}" if b != a else "") + ": "
            + ", ".join(topics or ["(no keywords)"])
            + (f" · DECISION {', '.join(dec)}" if dec else "") + "]")
    return {"part": part, "file": f"part-{part:04d}.jsonl",
            "from": min(seqs) if seqs else 0, "to": max(seqs) if seqs else 0,
            "from_ts": min(ts) if ts else 0, "to_ts": max(ts) if ts else 0,
            "exchanges": len(rows), "topics": topics, "line": line}


# --------------------------------------------------------------- rolling --
def should_roll(chat_path: Path, part_tokens: int | None = None) -> bool:
    return head_tokens(chat_path) >= (part_tokens or PART_TOKENS)


def roll(chat_path: Path) -> dict:
    """Move the current part into the parts folder, unchanged, and write its
    master line. Returns {part, file, master} — the caller (Session.roll_part)
    starts the new current part."""
    p = Path(chat_path)
    n = current_part(p)
    d = parts_dir(p)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"part-{n:04d}.jsonl"
    recs = _read_jsonl(p)
    os.replace(p, dest)
    m = _master_line(p, n, recs)
    _append(d / "master.jsonl", m)
    return {"part": n, "file": dest, "master": m}


def carry_records(chat_path: Path, budget: int = CARRY_TOKENS) -> list[dict]:
    """The end of the part that is about to close, word for word, to start the
    next one with: the user's messages and Aura's answers (no tool traffic),
    newest back to about `budget` tokens, always whole exchanges and at least
    the last one — so a new part never starts with the model knowing nothing."""
    from .notes import is_users_words
    recs = [r for r in _read_jsonl(Path(chat_path)) if not r.get("record")]
    talk = [r for r in recs if is_users_words(r) or (
        r.get("role") == "assistant" and not r.get("tool_calls") and _text(r).strip())]
    out: list[dict] = []
    used = 0
    for r in reversed(talk):
        cost = len(_text(r)) // CHARS_PER_TOKEN + 4
        if out and used + cost > budget and is_users_words(out[0]):
            break
        out.insert(0, r)
        used += cost
    while out and not is_users_words(out[0]):
        out.pop(0)                       # Start on the user's words
    return out


def start_note(chat_path: Path, finished_part: int) -> str:
    """The code-made note a new part starts with: what this is, where the
    rest is, and the end of the last part's index — no summary."""
    rows = index_entries(chat_path, finished_part)
    tail = rows[-4:]
    masters = master_entries(chat_path)
    return ("[CHAT PART %d — this chat continues] Parts 1-%d are on disk, unchanged and "
            "searchable; these bookmarks list what is in them. Everything the user told you "
            "earlier in this chat — about themselves, their plans, choices and work — is in "
            "those parts, not in the project's files. Do not guess a detail you do not have "
            "here, and do not assume an older fact still holds: recall it first (recall "
            "query=words searches every exchange word for word; seq=N opens the exact spot). "
            "When two things disagree, the newest wins.\n"
            % (finished_part + 1, finished_part)
            + ("\n".join(m.get("line", "") for m in masters[-40:]) + "\n" if masters else "")
            + (f"Last moments of part {finished_part}:\n" + "\n".join("  " + r["line"] for r in tail)
               if tail else ""))


# ---------------------------------------------------------------- recall --
def _render(recs: list[dict]) -> str:
    from .notes import is_note
    out = []
    for r in recs:
        role = r.get("role")
        if r.get("record") or not role:
            continue
        if role == "user" and is_note(r):
            continue
        t = _text(r)
        tag = f"#{r.get('seq')} {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(r.get('ts') or 0)))}"
        if role == "assistant" and r.get("tool_calls"):
            calls = ", ".join(_tool_bit(c)[0] for c in r["tool_calls"])
            out.append(f"{tag} aura → {calls}" + (f": {t[:300]}" if t.strip() else ""))
        elif role == "tool":
            out.append(f"{tag} result: {t[:600]}")
        else:
            who = "you" if role == "user" else "aura"
            out.append(f"{tag} {who}: {t[:1500]}")
    return "\n".join(out)


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"[\w.\-]+", text.lower()))


def _has_word(words: set[str], w: str) -> bool:
    """A whole word, with the plain plural either way (cup / cups)."""
    return (w in words or w + "s" in words or w + "es" in words
            or (len(w) > 3 and w.endswith("s") and w[:-1] in words))


def _snippet(text: str, got: list[str], width: int = 110) -> str:
    """The words around the first hit, so the line shows why it matched."""
    low = text.lower()
    found = [m for m in (re.search(r"\b" + re.escape(w), low) for w in got) if m]
    if not found:
        return ""
    at = min(found, key=lambda m: m.start())
    a, b = max(0, at.start() - width), min(len(text), at.end() + width)
    return " ".join(text[a:b].split())


def _exchange_text(p: Path, rows: list[dict], roles=("user", "assistant")) -> dict[int, str]:
    """Each indexed exchange's own words (by default the user's and the answers,
    never tool output), read from the part files: {from_seq: text}."""
    spans = sorted((int(r.get("from") or 0), int(r.get("to") or 0)) for r in rows)
    if not spans:
        return {}
    starts = [a for a, _b in spans]
    out: dict[int, list[str]] = {}
    for f in part_files(p) + [p]:
        for rec in _read_jsonl(f):
            if rec.get("record") or rec.get("carried") or rec.get("role") not in roles:
                continue
            if rec.get("role") == "user" and rec.get("by") not in (None, "user"):
                continue                       # Aura's own notes are not the user's words
            s = int(rec.get("seq") or 0)
            i = bisect.bisect_right(starts, s) - 1
            if i >= 0 and s <= spans[i][1]:
                t = _text(rec)
                if t:
                    out.setdefault(spans[i][0], []).append(t)
    return {k: "\n".join(v) for k, v in out.items()}


def _hidden_words(chat_path: Path) -> list[str]:
    """The specific words of what the user asked to forget (names, numbers),
    kept only so quotes from the chat can hide them."""
    out: list[str] = []
    for ev in _read_jsonl(_decisions_file(Path(chat_path))):
        out += [w for w in ev.get("hide") or [] if len(w) > 2]
    return list(dict.fromkeys(out))


def hide_forgotten(chat_path: Path, text: str) -> str:
    for w in _hidden_words(chat_path):
        text = re.sub(r"(?i)\b" + re.escape(w) + r"\b", "[forgotten]", text)
    return text


def recall(chat_path: Path, args: dict) -> str:
    """No args: the master index and the current part's latest index lines.
    part=N: that part's index. seq=X: the exact spot (window messages around
    it) from the files. query=: index lines that match, newest first."""
    body = _recall(chat_path, args)
    gone = [i for i in decisions_state(Path(chat_path))
            if i.get("kind") == "forget" and i["status"] == "active"]
    if gone and (args.get("seq") not in (None, "") or args.get("query")):
        # the chat still holds what the user asked to forget, word for word:
        # its names are hidden, and the model is told why
        body = ("The user asked to forget: " + "; ".join(_words(i["text"].split(" — ")[0], 12)
                                                         for i in gone)
                + " — do not use or repeat it; its names show as [forgotten] below.\n"
                + hide_forgotten(chat_path, body))
    return body


def _recall(chat_path: Path, args: dict) -> str:
    p = Path(chat_path)
    cur = current_part(p)
    if args.get("seq") not in (None, ""):
        seq = int(args["seq"])
        window = max(1, min(int(args.get("window") or 6), 30))
        recs = []
        for f in part_files(p) + [p]:
            rows = [r for r in _read_jsonl(f) if r.get("seq")]
            if rows and rows[0]["seq"] - window <= seq <= rows[-1]["seq"] + window:
                recs += rows
        recs = [r for r in recs if seq - window <= int(r.get("seq") or 0) <= seq + window]
        body = _render(recs)
        if len(body) > RECALL_MAX_CHARS:
            body = body[:RECALL_MAX_CHARS] + "\n[… cut — ask a smaller window]"
        return body or f"nothing at #{seq} in this chat"
    if args.get("query"):
        q = list(dict.fromkeys(w.lower() for w in re.findall(r"[\w.\-]+", str(args["query"]))
                               if w.lower() not in _STOP))
        # every exchange is matched on its index line AND its full words in
        # the part files (a fact deep in a long message has no index word);
        # a word few exchanges have counts for more than a common one
        rows = index_entries(p)
        text = _exchange_text(p, rows)
        hits = []
        for r in rows:
            hay = (r.get("line", "") + " " + " ".join(r.get("keys") or []) + " "
                   + r.get("user", "")).lower()
            full = text.get(int(r.get("from") or 0), "")
            words = _word_set(full)
            got = [w for w in q if w in hay or _has_word(words, w)]
            if got:
                hits.append((r, got, full))
        df = Counter(w for _r, got, _f in hits for w in got)
        n = max(1, len(rows))
        scored = [(sum(math.log(1 + n / df[w]) for w in got), r.get("ts", 0), r, _snippet(full, got))
                  for r, got, full in hits]
        scored.sort(key=lambda x: (-round(x[0], 3), -x[1]))
        # the exchange in progress has no index line yet: its messages are
        # searched as they are, so what was just said is found too
        last = max((int(r.get("to") or 0) for r in index_entries(p, cur)), default=0)
        live = []
        for r in _read_jsonl(p):
            if r.get("record") or int(r.get("seq") or 0) <= last or r.get("carried"):
                continue
            t = _text(r).lower()
            if t and sum(1 for w in q if w in t):
                live.append(f"#{r.get('seq')} (now, {r.get('role')}): {_words(_text(r), 18)}")
        if not scored and not live:
            return (f"nothing in this chat matches {args['query']!r} — try other words "
                    "(a synonym, a name, a number) before saying it was never mentioned")
        return (("exchanges that match, best first (recall seq=N for the exact spot; when "
                 "they disagree, the newest wins):\n"
                 + "\n".join(r["line"] + (f"\n    …{snip}…" if snip else "")
                             for _s, _t, r, snip in scored[:10]) if scored else "")
                + (("\n" if scored else "") + "in this exchange so far:\n" + "\n".join(live[-8:])
                   if live else ""))
    if args.get("part") not in (None, ""):
        n = int(args["part"])
        rows = index_entries(p, n)
        return (f"part {n}" + (" (current)" if n == cur else "") + f" — {len(rows)} exchanges:\n"
                + "\n".join(r["line"] for r in rows)) if rows else f"no index for part {n}"
    masters = master_entries(p)
    rows = index_entries(p, cur)[-15:]
    return ((f"this chat has {cur} part{'s' if cur != 1 else ''}; you work from part {cur}.\n")
            + ("\n".join(m.get("line", "") for m in masters) + "\n" if masters else "")
            + (f"part {cur} so far (latest):\n" + "\n".join(r["line"] for r in rows)
               if rows else ""))


# ----------------------------------------------------------------- tools --
def register(mediator, dispatch: dict) -> None:
    """recall + decisions: both work on the chat the loop is running
    (mediator._history_session)."""
    from .mediator import BlastRadius, ToolSpec, Violation

    def _chat():
        s = getattr(mediator, "_history_session", None)
        if s is None or not getattr(s, "path", None):
            raise Violation("no chat is open — nothing to recall")
        return s

    def _recall(a):
        return recall(Path(_chat().path), a or {})

    def _decisions(a):
        s = _chat()
        return decide(Path(s.path), a or {}, seq=int(getattr(s, "seq", 0) or 0))

    mediator.register(ToolSpec("recall", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["recall"] = _recall
    mediator.register(ToolSpec("decisions", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["decisions"] = _decisions


# ------------------------------------------------------------- decisions --
def _decisions_file(chat_path: Path) -> Path:
    return parts_dir(chat_path) / "decisions.jsonl"


def decisions_state(chat_path: Path) -> list[dict]:
    """Every decision with its state: active, or replaced (by, when)."""
    items: dict[str, dict] = {}
    for ev in _read_jsonl(_decisions_file(chat_path)):
        if ev.get("op") == "add":
            items[ev["id"]] = {**ev, "status": "active"}
        elif ev.get("op") == "replace" and ev.get("old") in items:
            old = items[ev["old"]]
            old.update(status="replaced", replaced_by=ev["id"], replaced_ts=ev.get("ts"))
            items[ev["id"]] = {**ev, "status": "active", "replaces": ev["old"]}
        elif ev.get("op") == "drop" and ev.get("id") in items:
            items[ev["id"]].update(status="dropped", replaced_ts=ev.get("ts"))
        elif ev.get("op") == "doubt" and ev.get("id") in items:
            # a later change may have made this untrue (knock-on); it stays,
            # marked, until a pass replaces or keeps it
            items[ev["id"]].setdefault("doubt", []).append(ev.get("because"))
    replaced = {k for k, v in items.items() if v["status"] != "active"}
    for v in items.values():
        v["check"] = [d for d in v.get("depends") or [] if d in replaced]
        v["doubt"] = [d for d in v.get("doubt") or [] if d in items and items[d]["status"] == "active"]
    return list(items.values())


def decide(chat_path: Path, args: dict, seq: int = 0) -> str:
    action = str(args.get("action") or "list").lower()
    items = decisions_state(chat_path)
    if action == "list":
        return decisions_block(chat_path, all_=True) or "no decisions recorded in this chat yet"
    if action not in ("add", "replace", "drop"):
        raise ValueError("decisions: action is list, add, replace or drop")
    known = {i["id"] for i in items}
    if action == "drop":
        did = str(args.get("id") or "")
        if did not in known:
            raise ValueError(f"no decision {did!r} — decisions(action='list') shows them")
        _append(_decisions_file(chat_path), {"op": "drop", "id": did, "ts": round(time.time(), 3)})
        return f"{did} dropped (kept in the record)"
    text = " ".join(str(args.get("text") or "").split())
    if not text:
        raise ValueError("decisions: give text — the decision in one line")
    deps = [d for d in (args.get("depends_on") or []) if d in known]
    nid = f"d{len(items) + 1}"
    ev = {"op": "add" if action == "add" else "replace", "id": nid, "text": text[:300],
          "ts": round(float(args.get("_ts") or time.time()), 3), "seq": seq, "depends": deps}
    if args.get("_kind"):
        ev["kind"] = args["_kind"]       # "fact" / "forget": written by the fact pass
    if args.get("_hide"):
        ev["hide"] = list(args["_hide"])  # Names to hide in quotes (never shown)
    if action == "replace":
        old = str(args.get("replaces") or "")
        if old not in known:
            raise ValueError(f"replace needs replaces=<id> of the decision it replaces "
                             f"(one of {', '.join(sorted(known)) or 'none yet'})")
        ev["old"] = old
    _append(_decisions_file(chat_path), ev)
    after = decisions_state(chat_path)
    flagged = [i["id"] for i in after if i["status"] == "active" and i["check"]]
    return (f"done: {nid} is recorded and active" + (f"; {ev['old']} is now marked replaced"
                                                     if action == "replace" else "")
            + ". It goes with every message from now on — nothing more to save or prove "
              "(no file, no finding needed)."
            + (f" Check these — they depend on a replaced decision: {', '.join(flagged)}"
               if flagged else ""))


def decisions_block(chat_path: Path, all_: bool = False,
                    max_chars: int | None = DECISIONS_MAX_CHARS) -> str:
    """The list as the model sees it, newest first (max_chars=None: all of it)."""
    items = decisions_state(chat_path)
    if not items:
        return ""
    show = items if all_ else [i for i in items if i["status"] == "active"]
    if not show:
        return ""
    # what the user asked to forget comes first: it overrides anything below
    # and anything found later in the chat
    show.sort(key=lambda i: (i.get("kind") != "forget", -float(i.get("ts") or 0)))
    by_id = {i["id"]: i for i in items}
    lines = []
    for i in show:
        when = time.strftime("%Y-%m-%d", time.localtime(float(i.get("ts") or 0)))
        if i.get("kind") == "forget" and i["status"] == "active":
            lines.append(f"- {i['id']} ({when}) FORGOTTEN: {i['text']}")
            continue
        text = i["text"]
        if i.get("doubt") and i["status"] == "active":
            # the warning comes first and the fact reads as the past: a model
            # skims the end of a line, not its tail notes
            why = "; ".join(_words(by_id[d]["text"], 12) for d in i["doubt"][:2])
            text = f"MAY BE OUTDATED (since: {why}) — was: {text}"
        s = (f"- {i['id']} ({when}{', #' + str(i['seq']) if i.get('seq') else ''}"
             f"{', fact' if i.get('kind') == 'fact' else ''}): {text}")
        if i.get("replaces"):
            s += f" [replaces {i['replaces']}]"
        if i["status"] != "active":
            s += (f" — REPLACED by {i.get('replaced_by')}" if i.get("replaced_by")
                  else " — dropped")
        if i.get("check"):
            s += f" ⚠ depends on {', '.join(i['check'])}, which was replaced — check it"
        lines.append(s)
    body = "\n".join(lines)
    if max_chars and len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n- … older ones: decisions(action='list')"
    upto = facts_through(chat_path)
    age = (f" as of message #{upto['seq']} ({time.strftime('%Y-%m-%d', time.localtime(upto['ts']))})"
           if upto.get("seq") else "")
    return (f"[NOTED IN THIS CHAT{age} — decisions and facts about the user, newest first. A "
            "summary, not proof: " + ("what was said after that message is not in it yet, and "
                                      if age else "") +
            "any value may have changed since. When an answer depends on one of these, check the "
            "chat first (recall query=words, then seq=N); the newest thing said wins. An entry "
            "marked MAY BE OUTDATED is never stated as current: say what was true and that "
            "the change since may have affected it, or ask. Never use or repeat what is "
            "marked FORGOTTEN, even if the chat still has it]\n" + body)


# ------------------------------------------------------ facts cursor --
def _facts_state_file(chat_path: Path) -> Path:
    return parts_dir(chat_path) / "facts_state.json"


def facts_through(chat_path: Path) -> dict:
    """{seq, ts}: the last message the fact pass has read (0 = none yet)."""
    try:
        return json.loads(_facts_state_file(chat_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seq": 0, "ts": 0}


def set_facts_through(chat_path: Path, seq: int, ts: float) -> None:
    f = _facts_state_file(chat_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seq": int(seq), "ts": float(ts)}), encoding="utf-8")
    os.replace(tmp, f)


# A user message that says something changed: the list is brought up to date
# now, not when the part closes (plain words for change, removal and forgetting)
_CHANGE = re.compile(
    r"\b(?:(?:i|we|i've|we've|i'm|we're|just|finally|have|has|had)\s+(?:\w+\s+)?"
    r"(?:switched|moved|relocated|quit|replaced|cancel+ed|changed|stopped|left|dropped|"
    r"swapped|migrated|upgraded|downgraded)|no longer|not any ?more|"
    r"(?:please\s+)?forget|don'?t (?:remember|keep|mention|bring up)|"
    r"(?:delete|remove|erase) (?:that|this|it|my|our|the)\b|"
    r"diagnosed|new (?:job|role|team|home|apartment|house|place|address|doctor|car|phone|"
    r"manager|position|school|partner)|from now on)\b", re.I)


def says_change(text: str) -> bool:
    return bool(_CHANGE.search(text or ""))


# ----------------------------------------------------------------- facts --
FACTS_INSTRUCTIONS = (
    "You keep the 'what is true now' list of a long chat between a user and their "
    "assistant. Read the user's messages that came after the list was last updated "
    "(dated, with #seq) and the current list, and say what to change in the list.\n"
    "Record lasting facts about the user and their work that a later answer could depend "
    "on: where they live and work, people, things they own or use, tools and settings, "
    "health, routines, schedules, plans, preferences, and decisions. Only the user's own "
    "situation: skip topics they merely asked about or researched, general knowledge, "
    "and what the assistant said.\n"
    "Above all catch CHANGES. When something new makes a listed entry false or outdated, "
    "replace that entry with the new state.\n"
    "Knock-on effects: for EVERY add or replace that changes the user's situation (job, "
    "home, health, team, tool, system, plan, relationship), go through the WHOLE current "
    "list and put in \"affects\" the ids of every entry the change could make untrue — "
    "routines, places, schedules, people, tools, settings and arrangements that depended "
    "on the old state — even though the user never mentioned them. When unsure, include "
    "it: it is only marked 'may be outdated', not removed. If a later message gives the "
    "new value of such an entry, replace it.\n"
    "Forgetting: EVERY message that asks to forget, delete, remove or stop keeping "
    "something gets a \"forget\" change — also when no current entry holds it (the chat "
    "itself still does). Give the id of the entry if one is listed, and a short topic that "
    "does NOT contain the value (\"the user's former employer\", not the company name). "
    "Never write the forgotten value into another entry.\n"
    "Keep the list short and lasting — about 40 entries at most: drop entries that were "
    "only passing (a trip that is over, a one-time purchase) or too small to matter. "
    "Each entry is one short plain line that stands on its own, with the specific names "
    "and numbers.")
FACTS_JSON = ('Answer with JSON only: {"changes": ['
              '{"action": "add", "text": "...", "seq": N, "affects": ["d4"]}, '
              '{"action": "replace", "replaces": "d3", "text": "...", "seq": N, "affects": []}, '
              '{"action": "forget", "id": "d7", "topic": "...", "seq": N}, '
              '{"action": "drop", "id": "d9"}]} — "seq" is the #seq of the message it comes '
              'from. {"changes": []} when nothing lasting was said.')
FACTS_MSG_CHARS = 1_500       # Each user message, as much as goes to the fact pass
FACTS_INPUT_CHARS = 60_000    # ~15k tokens: the most one fact pass reads
FACTS_MAX_CHANGES = 24


def forget_requests(chat_path: Path, when: dict[int, float]) -> list[tuple[int, str]]:
    """[(seq, words)] of the user's messages in this pass that ask to forget."""
    from .notes import is_users_words, users_words
    p = Path(chat_path)
    out = []
    lo = min(when) if when else 0
    for f in part_files(p) + [p]:
        for r in _read_jsonl(f):
            s = int(r.get("seq") or 0)
            if s in when and s >= lo and is_users_words(r):
                t = str(users_words(r) or "")
                if _FORGET.search(t):
                    out.append((s, t))
    return out


def facts_request(chat_path: Path) -> tuple[str, dict[int, float]]:
    """The text for the fact pass: the whole current list, then the user's
    messages after the last one a pass read (from the parts and the current
    part), dated. Returns (text, {seq: ts}); the caller moves the cursor to the
    last seq in it once the answer is applied."""
    from .notes import is_users_words, users_words
    p = Path(chat_path)
    after = int(facts_through(p).get("seq") or 0)
    lines, when, used = [], {}, 0
    for f in part_files(p) + [p]:
        rows = _read_jsonl(f)
        if rows and max((int(r.get("seq") or 0) for r in rows), default=0) <= after:
            continue
        for r in rows:
            s = int(r.get("seq") or 0)
            if s <= after or r.get("record") or r.get("carried") or not is_users_words(r):
                continue
            t = " ".join(str(users_words(r) or "").split())[:FACTS_MSG_CHARS]
            if not t:
                continue
            ts = float(r.get("ts") or 0)
            ln = f"#{s} {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}: {t}"
            if used + len(ln) > FACTS_INPUT_CHARS:
                break                      # The rest goes in the next pass
            lines.append(ln)
            when[s] = ts
            used += len(ln)
    current = decisions_block(p, all_=False, max_chars=None) or "(empty)"   # The whole list
    return ("CURRENT LIST:\n" + current + "\n\nTHE USER'S MESSAGES SINCE THE LIST WAS UPDATED:\n"
            + "\n".join(lines)), when


def apply_facts(chat_path: Path, reply: str, when: dict[int, float],
                asks_forget: list | None = None) -> list[str]:
    """Apply the fact pass's answer to the list; returns what was done.
    asks_forget: [(seq, words)] of the messages that ask to forget something
    (forget_requests), so none is lost if the model skips it."""
    asks_forget = asks_forget or []
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not data.get("changes") and not m:
        # a model that answered with a plain list instead of JSON: each
        # "- line" is a fact to add (nothing replaced without an id)
        data = {"changes": [{"action": "add", "text": ln.strip()[1:].strip()}
                            for ln in (reply or "").splitlines()
                            if ln.strip().startswith(("-", "*", "•"))]}
    done = []
    last_ts = max(when.values()) if when else time.time()
    for c in (data.get("changes") or [])[:FACTS_MAX_CHANGES]:
        if not isinstance(c, dict):
            continue
        try:
            seq = int(c.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        ts = when.get(seq) or last_ts
        known = {i["id"]: i for i in decisions_state(chat_path)}
        act = str(c.get("action") or "add").lower()
        if act == "drop":
            try:
                decide(chat_path, {"action": "drop", "id": str(c.get("id") or "")})
                done.append("drop")
            except ValueError:
                pass
            continue
        if act == "forget":
            # the entry goes, and a FORGOTTEN line (topic only, never the
            # value) stays at the top of the list so it is not used again
            did = str(c.get("id") or "")
            hide: list[str] = []
            if did in known:
                # the names it held, to hide in quotes — not plain words that
                # start a sentence ("Works", "User")
                hide = [w for w in _specific(known[did]["text"], 8)
                        if w.lower() not in _STOP and w.lower() not in _COMMON
                        and w.lower() not in _PLAIN_CAPS][:6]
                if known[did]["status"] == "active":
                    _append(_decisions_file(chat_path), {"op": "drop", "id": did, "ts": round(ts, 3)})
            topic = " ".join(str(c.get("topic") or c.get("text") or "something the user named").split())
            decide(chat_path, {"action": "add", "text": f"{topic[:120]} — the user asked to forget "
                               "it: do not use, repeat or look it up", "_kind": "forget", "_ts": ts,
                               "_hide": hide}, seq if seq in when else 0)
            done.append("forget")
            continue
        if not str(c.get("text") or "").strip():
            continue
        args = {"action": "replace" if act == "replace" else "add",
                "text": c["text"], "replaces": c.get("replaces"), "_kind": "fact", "_ts": ts}
        try:
            decide(chat_path, args, seq if seq in when else 0)
        except ValueError:
            if args["action"] != "replace":
                continue
            args["action"] = "add"                    # An unknown id: keep the fact
            decide(chat_path, args, seq if seq in when else 0)
        done.append(args["action"])
        # knock-on: every entry this change could make untrue is marked
        nid = f"d{len(known) + 1}"
        for a in (c.get("affects") or [])[:12]:
            a = str(a)
            if a in known and a != c.get("replaces") and known[a]["status"] == "active":
                _append(_decisions_file(chat_path), {"op": "doubt", "id": a, "because": nid,
                                                     "ts": round(ts, 3)})
                done.append("doubt")
    # a request to forget is never lost: if the model gave no forget change
    # for a message that asks for one, the message itself is recorded as one
    if asks_forget and "forget" not in done:
        for s, words in asks_forget:
            decide(chat_path, {"action": "add", "text": f"what the user asked in #{s} "
                               f"(\"{_words(words, 16)}\") — the user asked to forget it: do not "
                               "use, repeat or look it up", "_kind": "forget",
                               "_ts": when.get(s) or last_ts}, s)
            done.append("forget")
    if when:
        s = max(when)
        set_facts_through(chat_path, s, when[s])
    return done


_FORGET = re.compile(r"\b(?:forget|don'?t (?:remember|keep|mention|bring up)|"
                     r"(?:delete|remove|erase) (?:that|this|it|my|our|the)\b|stop (?:keeping|remembering))",
                     re.I)


# ---------------------------------------------------------- auto recall --
AUTO_RECALL_HITS = 3
AUTO_RECALL_WIDTH = 220       # Characters each side of the hit (~1k tokens for 3 hits)


def _score_exchanges(p: Path, q: list[str], rows: list[dict],
                     roles=("user", "assistant")) -> list[tuple]:
    """(score, ts, row, text, words) for every exchange that has a query word in
    its words; a word few exchanges have counts more."""
    text = _exchange_text(p, rows, roles)
    hits = []
    for r in rows:
        hay = (r.get("line", "") if "assistant" in roles else "") + " " + r.get("user", "")
        hay = hay.lower()
        full = text.get(int(r.get("from") or 0), "")
        words = _word_set(full)
        got = [w for w in q if w in hay or _has_word(words, w)]
        if got:
            hits.append((r, got, full))
    df = Counter(w for _r, got, _f in hits for w in got)
    n = max(1, len(rows))
    out = [(sum(math.log(1 + n / df[w]) for w in got), r.get("ts", 0), r, full, got)
           for r, got, full in hits]
    out.sort(key=lambda x: (-round(x[0], 3), -x[1]))
    return out


def auto_recall(chat_path: Path, message: str) -> str:
    """Before Aura answers a message in a chat with closed parts: the few
    earlier exchanges (outside the part in view) that share the most specific
    words with it, quoted — so the model sees what was said before without
    having to think of searching. Code only, no model call."""
    p = Path(chat_path)
    cur = current_part(p)
    if cur <= 1:
        return ""
    q = list(dict.fromkeys(w.lower() for w in re.findall(r"[\w.\-]+", message or "")
                           if len(w) > 2 and w.lower() not in _STOP and w.lower() not in _COMMON))
    if not q:
        return ""
    rows = [r for r in index_entries(p) if int(r.get("part") or 0) < cur]
    if not rows:
        return ""
    # the user's own words, not Aura's past answers: an old answer that was
    # wrong (or stale) would be quoted back as evidence and repeat itself.
    # Only a question about what Aura itself said looks at its answers.
    about_aura = bool(_ABOUT_AURA.search(message or ""))
    roles = ("user", "assistant") if about_aura else ("user",)
    scored = _score_exchanges(p, q, rows, roles)
    if not about_aura:
        # an earlier asking of the same question is not evidence either
        scored = [s for s in scored if not _just_a_question(s[3])]
    # only real matches: more than one common-ish word, or one rare one
    floor = math.log(1 + len(rows) / max(1, len(rows) // 10))
    keep = [s for s in scored if s[0] >= floor][:AUTO_RECALL_HITS]
    if not keep:
        return ""
    lines = []
    for _s, _ts, r, full, got in sorted(keep, key=lambda x: x[1]):
        snip = hide_forgotten(p, _snippet(full, got, AUTO_RECALL_WIDTH))
        when = time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0)))
        lines.append(f"- p{r.get('part')} #{r.get('from')} {when}, "
                     f"{'the exchange' if about_aura else 'the user said'}: …{snip}…")
    return ("[EARLIER IN THIS CHAT — " + ("exchanges" if about_aura else "the user's own words")
            + " from closed parts that share words with the user's message, found by code "
            "(quotes, not a summary; oldest first). They may be outdated: the newest thing "
            "said wins, and recall seq=N opens the full spot. Use them only if they bear on "
            "the message]\n" + "\n".join(lines))


_PLAIN_CAPS = set("""user users works worked goes went uses used lives lived has had owns
team plans likes prefers takes drives sees meets wants needs keeps says said currently now
recently previously formerly old new former""".split())

_ABOUT_AURA = re.compile(r"\byou\s+(?:\w+\s+)?(?:told|said|recommend\w*|suggest\w*|mention\w*|"
                         r"gave|listed|wrote|explain\w*|describ\w*|propos\w*|answer\w*)\b|"
                         r"\byour\s+(?:answer|advice|suggestion|recommendation|list)", re.I)


def _just_a_question(text: str) -> bool:
    """A message that only asks (short, with a '?'): repeating it tells nothing."""
    t = " ".join((text or "").split())
    return "?" in t and len(t) < 160

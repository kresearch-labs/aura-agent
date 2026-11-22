"""teams_wiring.py — any app as a Teams hub, through a wiring file Aura writes.

A wiring is DATA, never code: for each contract operation (teams_hub.OPS) one
HTTP request to the hub, or one MCP tool call, or a constant — and where the
answer's fields are. One fixed interpreter (this file) runs it; nothing the
model writes is ever executed.

    {"version": 1, "hub": "github", "base": "https://api.github.com",
     "auth": {"header": "Authorization", "value": "Bearer {token}"},
     "headers": {"Accept": "application/vnd.github+json"},
     "vars": {"repo": "acme/web"},
     "made_by": "asha", "from": "research: docs.github.com/rest/issues",
     "ops": {
       "whoami": {"http": "GET /user", "pick": "login"},
       "get_task": {"http": "GET /repos/{repo}/issues/{task}", "task": {
                      "id": "number", "title": "title", "description": "body",
                      "done": "state=closed", "assignees": "assignees[].login",
                      "labels": "labels[].name", "due_date": "milestone.due_on"}},
       "add_comment": {"http": "POST /repos/{repo}/issues/{task}/comments",
                       "body": {"body": "{text}"}},
       ...}}

Rules the loader enforces (a file that breaks one is refused whole):
  - requests go only to `base` (a path starting with "/"; no other host,
    no scheme); `base` is the hub address the user connected to;
  - MCP calls only to an MCP server the user added;
  - only the contract's operations (+ `blocked_by`, read-only), only
    GET/POST/PUT/PATCH/DELETE; writes only in the write operations;
  - the token is never in the file: `{token}` is filled from the key store
    in the auth header and nowhere else.

Field rules ("where in the answer"): "a.b" a path · "a[].b" every item's b ·
"a=x" true when a equals x · "@x" the constant x · "" the whole answer. Value
maps for a request: {"from": "done", "map": {"true": "closed", "false": "open",
"none": "all"}}. A value that is None leaves its query parameter out.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .teams_hub import OPS, Hub

READ_ONLY = {"whoami", "list_tasks", "get_task", "projects", "comments", "labels", "blocked_by", "people"}
WRITES = {"create_task", "add_comment", "mark", "unmark", "set_done", "due"}
KNOWN = set(OPS) | {"blocked_by", "due"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
# per-op the method the contract op means (H-15): writes stay their own
# call; nothing in a contract is a DELETE
METHODS_BY_OP = {
    "whoami": {"GET"}, "list_tasks": {"GET"}, "get_task": {"GET"},
    "projects": {"GET"}, "comments": {"GET"}, "labels": {"GET"},
    "blocked_by": {"GET"}, "people": {"GET"},
    "create_task": {"POST"}, "add_comment": {"POST"},
    "mark": {"PUT", "PATCH", "POST"}, "unmark": {"PUT", "PATCH", "POST"},
    "set_done": {"PUT", "PATCH", "POST"}, "due": {"PUT", "PATCH", "POST"},
}
_OP_KEYS = {"http", "mcp", "const", "query", "body", "items", "task", "pick",
            "text", "skip_if", "paginate", "project", "note", "author"}
_TOP_KEYS = {"version", "hub", "base", "auth", "headers", "vars", "ops", "made_by",
             "from", "checked", "check", "note"}
TOKEN_KEY_PREFIX = "teams.hub."


class WiringError(ValueError):
    """Why a wiring file was refused — said to the user plainly."""


# ------------------------------------------------- the file holds no keys ---
# "the token stays in the key store; the file never holds it."
# validate/use/publish all refuse a wiring whose strings carry a real
# credential — the stored hub token verbatim, or a generic secret shape (the
# same shapes secrets_guard scrubs). The {token} placeholder is the one right
# form.
def _keystore_token() -> str:
    try:
        from . import keystore, teams
        return keystore.get(teams.TOKEN_KEY) or ""
    except Exception:                                # noqa: BLE001 - a locked
        return ""                                    # Store never blocks checks


def _string_leaves(node, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _string_leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _string_leaves(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _screen_credentials(w: dict) -> None:
    """Refuse credential-shaped literals anywhere in the wiring."""
    token = _keystore_token()
    try:
        from .secrets_guard import SECRET_SHAPES
        shapes = [re.compile(p) for p, _ in SECRET_SHAPES]
    except Exception:                                # noqa: BLE001
        shapes = []
    for path, leaf in _string_leaves(w):
        if token and token in leaf:
            raise WiringError(
                f"the wiring carries the hub's real token ({path}) — the "
                "file never holds it; write {token} in the auth header and "
                "Aura fills it from the key store")
        if "{token}" in leaf:
            continue                                 # The placeholder is right
        for rx in shapes:
            if rx.search(leaf):
                raise WiringError(
                    f"the wiring carries a credential-shaped value ({path}) — "
                    "the file never holds secrets; the token goes in the "
                    "key store, written as {token} in the auth header")


# ----------------------------------------------------------- the loader ---
def validate(w: dict, hub_url: str | None = None) -> dict:
    """The wiring as the interpreter will run it, or WiringError."""
    if not isinstance(w, dict):
        raise WiringError("a wiring is a JSON object")
    extra = set(w) - _TOP_KEYS
    if extra:
        raise WiringError(f"unknown parts in the wiring: {', '.join(sorted(extra))}")
    base = str(w.get("base") or "").rstrip("/")
    if not re.match(r"^https?://[^/\s]+(/[^\s?#]*)?$", base):
        raise WiringError("the wiring needs `base`: the hub's address (http(s)://host[/path])")
    if hub_url and _host(base) != _host(hub_url):
        raise WiringError(f"the wiring talks to {_host(base)}, but the hub you connected to "
                          f"is {_host(hub_url)} — refused")
    if "{token}" in json.dumps({k: v for k, v in w.items() if k != "auth"}):
        raise WiringError("the token may appear only in the auth header")
    auth = w.get("auth") or {}
    if auth and (set(auth) - {"header", "value"} or "{token}" not in str(auth.get("value", ""))):
        raise WiringError("auth is {\"header\": name, \"value\": \"… {token} …\"}")
    ops = w.get("ops")
    if not isinstance(ops, dict) or not ops:
        raise WiringError("the wiring has no operations")
    unknown = set(ops) - KNOWN
    if unknown:
        raise WiringError(f"not contract operations: {', '.join(sorted(unknown))}")
    for name, op in ops.items():
        if not isinstance(op, dict) or set(op) - _OP_KEYS:
            raise WiringError(f"{name}: unknown keys {sorted(set(op) - _OP_KEYS) if isinstance(op, dict) else ''}")
        kinds = [k for k in ("http", "mcp", "const") if k in op]
        if len(kinds) != 1:
            raise WiringError(f"{name}: one of http, mcp or const")
        if "http" in op:
            m = re.match(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$", str(op["http"]))
            if not m:
                raise WiringError(f"{name}: http is \"METHOD /path\" (a path on the hub only)")
            if m.group(1) != "GET" and name in READ_ONLY:
                raise WiringError(f"{name} only reads — {m.group(1)} refused")
            # (H-15): the op's method is what the op MEANS — a comment is
            # never a DELETE, a create only POSTs; the wiring's own say on
            # how the op mutates reflects this contract, not free methods
            ok_methods = METHODS_BY_OP.get(name)
            if ok_methods and m.group(1) not in ok_methods:
                raise WiringError(
                    f"{name} runs as {sorted(ok_methods)} — {m.group(1)} refused "
                    "(the op's method is what the op means)")
            if "//" in m.group(2) or ".." in m.group(2):
                raise WiringError(f"{name}: the path may not leave the hub")
        if "mcp" in op:
            mc = op["mcp"]
            if not isinstance(mc, dict) or not mc.get("server") or not mc.get("tool"):
                raise WiringError(f"{name}: mcp is {{\"server\", \"tool\", \"args\"}}")
    missing = [o for o in OPS if OPS[o][0] and o not in ops]
    if missing:
        raise WiringError("a hub needs at least: " + ", ".join(missing))
    _screen_credentials(w)
    return w


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def sha(w: dict) -> str:
    return hashlib.sha256(json.dumps(w, sort_keys=True).encode()).hexdigest()[:12]


def wiring_path() -> Path:
    from .paths import aura_home
    d = aura_home() / "teams"
    d.mkdir(parents=True, exist_ok=True)
    return d / "wiring.json"


def load_in_use() -> dict:
    """The approved wiring this machine uses (checked again on load)."""
    from . import teams
    p = wiring_path()
    if not p.exists():
        raise WiringError("no wiring in use — connect to the hub again")
    return validate(json.loads(p.read_text(encoding="utf-8")), teams.load_config().get("hub_url"))


def use(w: dict) -> None:
    """Put an approved wiring in use (the user said yes on the check card)."""
    from . import teams
    cfg = teams.load_config()
    validate(w, cfg.get("hub_url"))
    wiring_path().write_text(json.dumps(w, indent=1), encoding="utf-8")
    cfg.update(hub_kind="wired", wiring_sha=sha(w), hub_app=w.get("hub", ""))
    teams.save_config(cfg)


# ------------------------------------------------------ when it breaks ---
# the app changes (a renamed field, a moved address) and an operation that
# worked starts failing. It is recorded — not the hub being off, not a refused
# token — told in the sidebar and to the model, and Aura offers a fixed wiring
# through the same research → check → yes.
def _health_path() -> Path:
    return wiring_path().with_name("wiring_health.json")


def health() -> dict:
    try:
        return json.loads(_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _note_health(op: str, error: str | None, immediate: bool = False,
                 what: str = "op") -> None:
    """One health record per failing operation (or per 'op.field' when the hub's
    answer lost a field — a renamed field is told at once).

    Immediate: — "a failing operation is told at once". Only a transient 5xx is
    squelched until it repeats (one blip is not the app changing); a 404, a
    changed answer shape, a refused redirect and a missing field surface on the
    FIRST failure. HubOff/TokenRefused are never recorded here (the hub off /
    the token: not the wiring).
    """
    import time
    h = health()
    if error is None:
        if op not in h:
            return
        h.pop(op, None)
    else:
        cur = h.get(op) or {"count": 0}
        h[op] = {"count": cur["count"] + 1, "error": error[:200],
                 "ts": round(time.time()), "immediate": bool(immediate),
                 "what": what}
    try:
        _health_path().write_text(json.dumps(h, indent=1), encoding="utf-8")
    except OSError:
        pass


def broken() -> dict:
    """Operations to tell about NOW: first-failure ones (immediate) plus repeated
    transient ones (one 5xx can be a blip — keeps the squelch there only).
    """
    return {k: v for k, v in health().items()
            if v.get("immediate") or v.get("count", 0) >= 2}


def repair(op: str, new: dict | None = None, token: str | None = None) -> dict:
    """One wiring operation that started failing, looked at again right now. Reads
    only; never writes. Returns: {"op", "known", "recorded" (the health entry),
    "still_failing", "error" (the live re-check), "valid_new", "new_error",
    "diffs"} With `new` `diffs` is the real difference the card shows —
    differences(the wiring in use, new) — so a re-check always produces a diff
    instead of the old dead 'no difference' path.
    """
    out = {"op": op, "known": op in KNOWN, "recorded": None,
           "still_failing": None, "error": "", "valid_new": None,
           "new_error": "", "diffs": []}
    h = health()
    out["recorded"] = h.get(op) or next(           # A field of the op counts
        (v for k, v in h.items() if k.startswith(op + ".")), None)
    from . import teams
    try:
        cur = load_in_use()
    except WiringError as e:
        out["error"] = str(e)
        return out
    if not op:
        out["error"] = "name the operation that fails"
        return out
    out["entry"] = (cur.get("ops") or {}).get(op)
    if op not in (cur.get("ops") or {}):
        out["error"] = (f"the wiring in use has no '{op}'" +
                        ("" if out["known"] else " (not a contract operation)"))
    # re-run the ops that need no arguments — the live part of the re-check
    probe = {"whoami": "whoami", "projects": "projects", "people": "people"}
    if op in probe:
        try:
            got = getattr(WiredHub(cur, token=token), probe[op])()
            out["still_failing"] = got in (None, "", [])
            out["error"] = "" if not out["still_failing"] else "the answer is empty"
        except Exception as e:                       # noqa: BLE001
            out["still_failing"], out["error"] = True, str(e)[:160]
    elif op == "list_tasks":
        try:
            WiredHub(cur, token=token).list_tasks(done=None)
            out["still_failing"] = False
        except Exception as e:                       # noqa: BLE001
            out["still_failing"], out["error"] = True, str(e)[:160]
    if new is not None:
        try:
            validate(new, teams.load_config().get("hub_url"))
            out["valid_new"] = True
        except WiringError as e:
            out["valid_new"], out["new_error"] = False, str(e)
        out["diffs"] = differences(cur, new) if isinstance(new, dict) else []
    return out


def differences(old: dict, new: dict) -> list[str]:
    """What a new wiring changes, one line per operation (for the card)."""
    out = []
    oo, no = old.get("ops") or {}, new.get("ops") or {}
    for k in sorted(set(oo) | set(no)):
        if k not in no:
            out.append(f"- {k}: removed")
        elif k not in oo:
            out.append(f"+ {k}: added ({no[k].get('http') or 'mcp/const'})")
        elif oo[k] != no[k]:
            a = oo[k].get("http") or "mcp/const"
            b = no[k].get("http") or "mcp/const"
            out.append(f"~ {k}: " + (f"{a} → {b}" if a != b else "fields/arguments changed"))
    for k in ("base", "auth", "headers", "vars"):
        if old.get(k) != new.get(k):
            out.append(f"~ {k} changed")
    return out


# ------------------------------------------------- one wiring per team ---
WIRING_TITLE = "Aura wiring — keep open (how every Aura talks to this board)"


def _json_blocks(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"```json\s*(\{.*?\})\s*```", text or "", re.S):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def find_published(hub) -> tuple[dict | None, int | None]:
    """(the team's wiring, its task) — read with a draft wiring's own reads.

    The match is the EXACT title: a leftover "Aura wiring check — safe to
    delete …" task also starts with "Aura wiring" and must never be taken
    for the team's wiring (the old startswith did).

    Trust lane (H-15): a JSON block in a comment counts only when its
    author is a PERSON ON THE TEAM (the wiring's `people` op when it has
    one; else the roster the board itself shows). An anonymous / outside
    commenter's wiring never outranks the task's own description block.
    """
    try:
        for t in hub.list_tasks(done=False):
            if str(t.get("title", "")).strip() == WIRING_TITLE:
                found = _json_blocks(t.get("description", ""))
                # Plan tightened: only an ADMIN's wiring rides — and since no
                # op ever proves an author is an admin through this law's
                # tools, comments never adopt anything at all; the task's own
                # description block is authored by the person who created the
                # task (publish is the only writer of it)
                return (found[-1] if found else None), t["id"]
    except Exception:                                # noqa: BLE001
        pass
    return None, None


def publish(hub, w: dict) -> str:
    """Put the wiring on the hub for the team (a new version = a comment).

    The file never holds a credential — publish validates again, so a
    hand-edited or model-made wiring with a real token in it is refused here
    too, not only at use.
    """
    w = validate(w)
    body = "```json\n" + json.dumps(w, indent=1) + "\n```"
    _, tid = find_published(hub)
    if tid is not None:
        hub.add_comment(tid, f"[AURA] wiring updated by {w.get('made_by', '?')}\n{body}")
        return f"the team's wiring task #{tid} has the new version"
    if not hub.has("create_task"):
        return "this hub's wiring can't create tasks — the wiring stays on this machine only"
    pid = None
    if hub.has("projects"):
        ps = hub.projects()
        pid = ps[0]["id"] if ps else None
    t = hub.create_task(pid, WIRING_TITLE,
                        "Every Aura on the team reads this to talk to the board. Keep it "
                        "open; a new version is added as a comment.\n\n" + body)
    return f"put on the board as #{t.get('id')} so every Aura on the team uses the same one"


# ------------------------------------------------------- field picking ---
def pick(data, rule: str):
    """One value out of an answer by a field rule (see the top)."""
    if rule is None:
        return None
    rule = str(rule)
    if rule == "":
        return data
    if rule.startswith("@"):
        c = rule[1:]
        return {"true": True, "false": False, "null": None}.get(c, c)
    if "=" in rule:
        path, want = rule.split("=", 1)
        v = pick(data, path)
        return str(v).lower() == want.lower() if v is not None else False
    if "[]" in rule:
        head, rest = rule.split("[]", 1)
        items = pick(data, head) if head else data
        rest = rest.lstrip(".")
        return [pick(i, rest) if rest else i for i in (items or []) if isinstance(i, (dict, list, str, int))]
    cur = data
    for part in rule.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _fill(tpl, vals: dict, in_path: bool = False):
    """{name} placeholders → values (URL-quoted inside a path). A value map
    {"from", "map"} picks by a value. Returns None for a missing value."""
    if isinstance(tpl, dict) and "from" in tpl and "map" in tpl:
        v = vals.get(tpl["from"])
        key = "none" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
        return tpl["map"].get(key)
    if isinstance(tpl, dict):
        out = {k: _fill(v, vals) for k, v in tpl.items()}
        return {k: v for k, v in out.items() if v is not None}
    if isinstance(tpl, list):
        return [x for x in (_fill(v, vals) for v in tpl) if x is not None]
    if not isinstance(tpl, str):
        return tpl
    whole = re.fullmatch(r"\{(\w+)\}", tpl)
    if whole and not in_path:
        return vals.get(whole.group(1))         # Keeps the type (a list, a number)
    missing = [n for n in re.findall(r"\{(\w+)\}", tpl) if vals.get(n) is None]
    if missing:
        return None
    return re.sub(r"\{(\w+)\}", lambda m: (urllib.parse.quote(str(vals[m.group(1)]), safe="/")
                                            if in_path else str(vals[m.group(1)])), tpl)


def _mcp_server(name: str):
    """One of the user's own MCP servers (the profile's, added with /mcp add)
    — never one the wiring itself describes."""
    try:
        from .mcp_client import load_servers_from_config
        from .mcp_tools import load_config
        for s in load_servers_from_config(load_config(Path.cwd())):
            if s.name == name:
                return s
    except Exception:                               # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------- the runner ---
class _OffHubRedirect(Exception):
    """The hub answered a redirect to a DIFFERENT authority — a redirect
    never carries the hub's Authorization there (the audit's token-leak fix)."""


def _authority(url: str) -> str:
    """scheme://host:port — the redirect may move only WITHIN it."""
    u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    return f"{u.scheme or 'http'}://{(u.hostname or '').lower()}:{u.port or (443 if u.scheme == 'https' else 80)}"


class _SameHostOnly(urllib.request.HTTPRedirectHandler):
    """Redirect policy for wired hubs: follow on the hub's own authority
    (scheme, host, port), refuse anything else. urllib's default handler
    re-sends every header — the Authorization included — to wherever a 302
    points, so this guard is installed on every op's request."""

    def __init__(self, authority: str):
        self._authority = authority

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _authority(newurl) != self._authority:
            raise _OffHubRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WiredHub(Hub):
    """Runs a validated wiring against the hub."""
    kind = "wired"

    def __init__(self, wiring: dict, token: str | None = None):
        self.w = wiring
        self.ops = wiring["ops"]
        self.base = wiring["base"].rstrip("/")
        self._token = token
        self.calls: list[tuple[str, str]] = []        # (op, what) — for the check card
        self.track = token is None                    # The wiring in use: record what breaks
        self._opener = urllib.request.build_opener(_SameHostOnly(_authority(self.base)))

    def has(self, op: str) -> bool:
        return op in self.ops

    # -- plumbing
    def _tok(self) -> str:
        if self._token is not None:
            return self._token
        from . import keystore, teams
        return keystore.get(teams.TOKEN_KEY) or ""

    def _vals(self, **kw) -> dict:
        v = dict(self.w.get("vars") or {})
        v.update({k: x for k, x in kw.items()})
        return v

    def _run(self, name: str, **kw):
        from .teams_board import BoardError, HubOff, TokenRefused
        try:
            ans = self._run_op(name, **kw)
        except (HubOff, TokenRefused):
            raise                                   # The hub off / the token: not the wiring
        except BoardError as e:
            if self.track:
                # Told at once — but a transient 5xx is a blip until it repeats
                # (broken keeps the squelch for those only)
                _note_health(name, str(e), immediate=not getattr(e, "blip", False))
            raise
        if self.track:
            _note_health(name, None)
        return ans

    def _run_op(self, name: str, **kw):
        from .teams_board import BoardError, HubOff, TokenRefused
        op = self.ops.get(name)
        if op is None:
            raise BoardError(f"this hub's wiring has no '{name}'")
        vals = self._vals(**kw)
        if "const" in op:
            return _fill(op["const"], vals)
        if "mcp" in op:
            return self._mcp(name, op, vals)
        method, path = op["http"].split(" ", 1)
        path = _fill(path, vals, in_path=True)
        if path is None:
            raise BoardError(f"{name}: a value its address needs is missing")
        query = _fill(op.get("query") or {}, vals)
        body = _fill(op["body"], vals) if "body" in op else None
        out, page = [], 1
        pg = op.get("paginate") or {}
        while True:
            q = dict(query)
            if pg:
                q[pg.get("param", "page")] = page
                if pg.get("per_page_param"):
                    q[pg["per_page_param"]] = pg.get("per_page", 100)
            url = self.base + path + (("?" + urllib.parse.urlencode(q, doseq=True)) if q else "")
            headers = {"User-Agent": "aura-teams", "Content-Type": "application/json"}
            headers.update({k: str(v) for k, v in (self.w.get("headers") or {}).items()})
            auth = self.w.get("auth") or {}
            if auth:
                headers[auth["header"]] = auth["value"].replace("{token}", self._tok())
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            self.calls.append((name, f"{method} {path}"))
            try:
                with self._opener.open(req, timeout=20) as r:
                    raw = r.read().decode("utf-8")
                ans = json.loads(raw) if raw.strip() else {}
            except _OffHubRedirect as e:
                err = BoardError(f"{name}: the hub sent the request to another "
                                 f"server ({_host(str(e)) or str(e)}) — refused: the token "
                                 "would go with it")
                raise err from e
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise TokenRefused(f"the hub refused the token for {name} ({e.code}) — it "
                                       "may lack a permission, or have expired") from e
                err = BoardError(f"{name}: the hub said {e.code}: "
                                 f"{e.read().decode('utf-8', 'replace')[:150]}")
                err.blip = 500 <= e.code < 600      # Transient — squelched once
                raise err from e
            except (urllib.error.URLError, OSError) as e:
                raise HubOff(f"the hub is not reachable ({getattr(e, 'reason', e)})") from e
            except ValueError as e:                 # The answer isn't JSON
                raise BoardError(f"{name}: the hub's answer changed shape (not "
                                 "JSON the wiring understands) — the app may "
                                 "have changed") from e
            if not pg:
                return ans
            items = pick(ans, op.get("items", "")) or []
            out += items if isinstance(items, list) else []
            if len(items) < pg.get("per_page", 100) or page >= 50:
                return out
            page += 1

    def _mcp(self, name: str, op: dict, vals: dict):
        from .teams_board import BoardError
        mc = op["mcp"]
        self.calls.append((name, f"mcp {mc['server']}.{mc['tool']}"))
        srv = _mcp_server(mc["server"])
        if srv is None:
            raise BoardError(f"{name}: the MCP server '{mc['server']}' isn't one you added")
        try:
            srv.start()
            text = srv.call_tool(mc["tool"], _fill(mc.get("args") or {}, vals))
        except Exception as e:                      # noqa: BLE001
            raise BoardError(f"{name}: MCP {mc['server']}.{mc['tool']}: {e}") from e
        finally:
            try:
                srv.stop()
            except Exception:                       # noqa: BLE001
                pass
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text

    def _items(self, name: str, ans) -> list:
        op = self.ops[name]
        items = ans if op.get("paginate") else pick(ans, op.get("items", ""))
        items = items if isinstance(items, list) else []
        skip = op.get("skip_if")
        return [i for i in items if not (skip and isinstance(i, dict) and pick(i, skip))]

    @staticmethod
    def _rule_root(rule) -> str | None:
        """The first path segment of a field rule ('assignees[].login' →
        'assignees', 'state=closed' → 'state'); None for constants/@/''."""
        rule = str(rule or "")
        if not rule or rule.startswith("@"):
            return None
        if "=" in rule:
            rule = rule.split("=", 1)[0]
        if "[]" in rule:
            rule = rule.split("[]", 1)[0]
        return rule.split(".", 1)[0] or None

    def _field_tells(self, name: str, raws: list, only: tuple | None = None) -> None:
        """A field the wiring declares that NO answer carries any more means the
        app renamed it: told at once (immediate), as 'op.field', distinct from
        the op itself failing. A field present but empty is normal (a null
        milestone), never told.
        """
        if not self.track:
            return
        rules = self.ops.get(name, {}).get("task") or \
                self.ops.get("get_task", {}).get("task") or {}
        dicts = [r for r in raws if isinstance(r, dict)]
        for field, rule in rules.items():
            root = self._rule_root(rule)
            if root is None or (only and field not in only):
                continue
            key = f"{name}.{field}"
            if dicts and all(root not in r for r in dicts):
                _note_health(key, f"the hub's {name} answer no longer has "
                                  f"'{field}' ('{root}' is gone) — renamed? "
                                  "the wiring needs a fix",
                             immediate=True, what="field")
            else:
                _note_health(key, None)             # Present: clear a past tell

    def _task(self, name: str, raw) -> dict:
        rules = self.ops[name].get("task") or self.ops.get("get_task", {}).get("task") or {}
        t = {k: pick(raw, r) for k, r in rules.items()}
        t["assignees"] = [{"username": a} for a in (t.get("assignees") or []) if a]
        t["labels"] = [{"title": a} for a in (t.get("labels") or []) if a]
        t["done"] = bool(t.get("done"))
        t["description"] = t.get("description") or ""
        if t.get("due_date") and str(t["due_date"])[:4].isdigit():
            t["due_date"] = str(t["due_date"])[:10]
        else:
            t["due_date"] = None
        t.setdefault("project_id", (self.ops.get("projects") and 1) or None)
        t.setdefault("related_tasks", {})
        return t

    # -- the contract
    def whoami(self) -> str:
        return str(pick(self._run("whoami"), self.ops["whoami"].get("pick", "")) or "")

    def list_tasks(self, assignee=None, project=None, done=None) -> list[dict]:
        ans = self._run("list_tasks", assignee=assignee, project=project, done=done)
        items = self._items("list_tasks", ans)
        # lists often trim fields — only the two Teams can't function
        # without raise the rename tell here (get_task checks all fields)
        self._field_tells("list_tasks", items, only=("id", "title"))
        tasks = [self._task("list_tasks", i) for i in items]
        # a hub that can't filter on its side: filter here
        if assignee:
            tasks = [t for t in tasks if any(a["username"] == assignee for a in t["assignees"])]
        if done is not None:
            tasks = [t for t in tasks if t["done"] == done]
        return tasks

    def get_task(self, task_id) -> dict:
        raw = self._run("get_task", task=task_id)
        self._field_tells("get_task", [raw])
        t = self._task("get_task", raw)
        # Row 9: blocked-by degrades IN THE OPEN — "not known on this hub" when
        # the wiring has no op, "failed to read" when it errors — never
        # indistinguishable from not-blocked (an empty list).
        if "blocked_by" in self.ops:
            try:
                ans = self._run("blocked_by", task=task_id)
                t["related_tasks"] = {"blocked": [self._task("blocked_by", i)
                                                  for i in self._items("blocked_by", ans)]}
            except Exception as e:                   # noqa: BLE001 - optional
                t["related_tasks"] = {"blocked": [],
                                      "blocked_error":
                                          f"blocked-by failed to read ({str(e)[:100]})"}
        else:
            t["related_tasks"] = {"blocked": [],
                                  "blocked_unknown": "not known on this hub"}
        return t

    def projects(self) -> list[dict]:
        ans = self._run("projects")
        out = []
        for i in self._items("projects", ans) if not isinstance(ans, list) or self.ops["projects"].get("items") \
                else ans:
            rules = self.ops["projects"].get("project") or {"id": "id", "title": "title"}
            out.append({k: pick(i, r) for k, r in rules.items()})
        return out

    def create_task(self, project_id, title, description="") -> dict:
        me = self.whoami()
        ans = self._run("create_task", project=project_id, title=title,
                        description=description, me=me, assignees=[me])
        return self._task("create_task", ans)

    def comments(self, task_id) -> list[str]:
        return [t for _, t in self.comment_authors(task_id)]

    def comment_authors(self, task_id) -> list[tuple[str, str]]:
        """(author, text) — the author by the op's `author` rule ("" without one)."""
        ans = self._run("comments", task=task_id)
        op = self.ops["comments"]
        rule, who = op.get("text", ""), op.get("author")
        return [(str(pick(i, who) or "") if who else "", str(pick(i, rule) or ""))
                for i in self._items("comments", ans)]

    def people(self) -> list[str]:
        """The team: the wiring's `people` op, else who appears on the board."""
        if "people" in self.ops:
            ans = self._run("people")
            rule = self.ops["people"].get("text", "")
            return sorted({str(pick(i, rule)) for i in self._items("people", ans) if pick(i, rule)})
        names = set()
        for t in self.list_tasks(done=None)[:200]:
            for a in t.get("assignees") or []:
                names.add(a["username"])
        return sorted(n for n in names if n)

    def add_comment(self, task_id, text) -> dict:
        self._run("add_comment", task=task_id, text=text)
        return {"comment": text}

    def labels(self, task_id) -> list[str]:
        ans = self._run("labels", task=task_id)
        rule = self.ops["labels"].get("text", "")
        return [str(pick(i, rule)) for i in self._items("labels", ans)]

    def mark(self, task_id, label) -> None:
        self._run("mark", task=task_id, label=label, labels=[label])

    def unmark(self, task_id, label) -> None:
        self._run("unmark", task=task_id, label=label)

    def set_done(self, task_id, task) -> dict:
        self._run("set_done", task=task_id)
        return {**task, "done": True}

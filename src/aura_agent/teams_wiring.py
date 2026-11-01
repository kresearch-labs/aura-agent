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
WRITES = {"create_task", "add_comment", "mark", "unmark", "set_done"}
KNOWN = set(OPS) | {"blocked_by"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_OP_KEYS = {"http", "mcp", "const", "query", "body", "items", "task", "pick",
            "text", "skip_if", "paginate", "project", "note", "author"}
_TOP_KEYS = {"version", "hub", "base", "auth", "headers", "vars", "ops", "made_by",
             "from", "checked", "check", "note"}
TOKEN_KEY_PREFIX = "teams.hub."


class WiringError(ValueError):
    """Why a wiring file was refused — said to the user plainly."""


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
            if "//" in m.group(2) or ".." in m.group(2):
                raise WiringError(f"{name}: the path may not leave the hub")
        if "mcp" in op:
            mc = op["mcp"]
            if not isinstance(mc, dict) or not mc.get("server") or not mc.get("tool"):
                raise WiringError(f"{name}: mcp is {{\"server\", \"tool\", \"args\"}}")
    missing = [o for o in OPS if OPS[o][0] and o not in ops]
    if missing:
        raise WiringError("a hub needs at least: " + ", ".join(missing))
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


def _note_health(op: str, error: str | None) -> None:
    import time
    h = health()
    if error is None:
        if op not in h:
            return
        h.pop(op, None)
    else:
        cur = h.get(op) or {"count": 0}
        h[op] = {"count": cur["count"] + 1, "error": error[:200], "ts": round(time.time())}
    try:
        _health_path().write_text(json.dumps(h, indent=1), encoding="utf-8")
    except OSError:
        pass


def broken() -> dict:
    """Operations failing twice or more in a row (one failure can be a blip)."""
    return {k: v for k, v in health().items() if v.get("count", 0) >= 2}


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
    """(the team's wiring, its task) — read with a draft wiring's own reads."""
    try:
        for t in hub.list_tasks(done=False):
            if str(t.get("title", "")).startswith("Aura wiring"):
                found = _json_blocks(t.get("description", ""))
                for c in hub.comments(t["id"]):
                    found += _json_blocks(c)
                return (found[-1] if found else None), t["id"]
    except Exception:                                # noqa: BLE001
        pass
    return None, None


def publish(hub, w: dict) -> str:
    """Put the wiring on the hub for the team (a new version = a comment)."""
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
                _note_health(name, str(e))
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
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read().decode("utf-8")
                    ans = json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise TokenRefused(f"the hub refused the token for {name} ({e.code}) — it "
                                       "may lack a permission, or have expired") from e
                raise BoardError(f"{name}: the hub said {e.code}: "
                                 f"{e.read().decode('utf-8', 'replace')[:150]}") from e
            except (urllib.error.URLError, OSError) as e:
                raise HubOff(f"the hub is not reachable ({getattr(e, 'reason', e)})") from e
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
        tasks = [self._task("list_tasks", i) for i in self._items("list_tasks", ans)]
        # a hub that can't filter on its side: filter here
        if assignee:
            tasks = [t for t in tasks if any(a["username"] == assignee for a in t["assignees"])]
        if done is not None:
            tasks = [t for t in tasks if t["done"] == done]
        return tasks

    def get_task(self, task_id) -> dict:
        t = self._task("get_task", self._run("get_task", task=task_id))
        if "blocked_by" in self.ops:
            try:
                ans = self._run("blocked_by", task=task_id)
                t["related_tasks"] = {"blocked": [self._task("blocked_by", i)
                                                  for i in self._items("blocked_by", ans)]}
            except Exception:                        # noqa: BLE001 - optional
                pass
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

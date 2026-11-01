"""engagement.py — Engagement init + status HUD.

`aura init <name>` creates the operator-contract skeleton (BLUEPRINT):
scope.json is THE source the mediator loads; everything engagement- shaped
lives under one root. `status` reads graph+budget+audit for the HUD.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

SCOPE_TEMPLATE = {
    "engagement": "",
    "hosts": [],
    "cidrs": [],
    "domains": [],
    "notes": "fill me — scope.json is THE source the mediator enforces",
}

STATE_TEMPLATE = """# Notebook State

## Objective
(fill: what does done look like?)

## Constraints
- scope: see scope.json (mediator-enforced)

## Current position
(created by init — update or let the agent journal handle it)
"""


def init_engagement(root: Path, name: str) -> Path:
    """Create the engagement skeleton. Idempotent — never overwrites."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for d in ("sessions", "output", "artifacts"):
        (root / d).mkdir(exist_ok=True)
    scope_p = root / "scope.json"
    if not scope_p.exists():
        tpl = dict(SCOPE_TEMPLATE)
        tpl["engagement"] = name
        scope_p.write_text(json.dumps(tpl, indent=2), encoding="utf-8")
    for fname, content in (
        ("state.md", STATE_TEMPLATE),
        ("findings.jsonl", ""),
        ("notes.md", None),   # Notes tool owns its header — create empty
    ):
        p = root / fname
        if not p.exists():
            p.write_text(content or "", encoding="utf-8")
    return root


# ------------------------------------------------------------------ status --
def engagement_status(root: Path) -> dict:
    """x status: graph summary + budget burn + last tool calls + open branches."""
    root = Path(root)
    out: dict = {"engagement": root.name, "exists": root.exists()}
    if not out["engagement"] and root.exists():
        pass

    # scope
    scope_p = root / "scope.json"
    if scope_p.exists():
        try:
            s = json.loads(scope_p.read_text(encoding="utf-8"))
            out["scope"] = {"hosts": len(s.get("hosts", [])),
                            "cidrs": len(s.get("cidrs", [])),
                            "domains": len(s.get("domains", []))}
        except json.JSONDecodeError:
            out["scope"] = {"error": "scope.json invalid"}

    # graph
    graph_p = root / "graph.json"
    if graph_p.exists():
        try:
            g = json.loads(graph_p.read_text(encoding="utf-8"))
            nodes = g.get("nodes", [])
            by: dict[str, int] = {}
            for n in nodes:
                by[n["status"]] = by.get(n["status"], 0) + 1
            out["graph"] = {"nodes": len(nodes), "by_status": by,
                            "active": g.get("active")}
        except (json.JSONDecodeError, KeyError):
            out["graph"] = {"error": "graph.json unreadable"}

    # findings
    f_p = root / "findings.jsonl"
    if f_p.exists():
        out["findings"] = sum(1 for l in f_p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip())

    # sessions
    sess = root / "sessions"
    if sess_dir_exists := (root / "sessions").exists():
        sessions = sorted((root / "sessions").glob("*.jsonl"))
        out["sessions"] = len(sessions)
        if sessions:
            out["last_session"] = sessions[-1].name

    # last tool calls from audit
    audit = root / "audit.jsonl"
    if audit.exists():
        recs = []
        for line in audit.read_text(encoding="utf-8").splitlines()[-10:]:
            try:
                r = json.loads(line)
                recs.append({"call": r["call"], "status": r["status"][:40]})
            except (json.JSONDecodeError, KeyError):
                continue
        out["last_calls"] = recs

    # artifacts count
    art = root / "artifacts"
    if art.exists():
        out["artifacts"] = len([d for d in art.iterdir() if d.is_dir()])

    out["generated_ts"] = round(time.time(), 3)
    return out


def render_status(st: dict) -> str:
    """One-screen HUD string."""
    lines = [f"notebook   : {st.get('engagement', '?')}"]
    if "assist" in st:
        lines.append(f"assist     : {st['assist']} (mode: "
                     f"{st.get('mediator_mode', 'act')})")
    if "scope" in st:
        s = st["scope"]
        lines.append(f"scope      : {s.get('hosts', 0)} hosts, "
                     f"{s.get('cidrs', 0)} cidrs, {s.get('domains', 0)} domains")
    if "graph" in st and "by_status" in st["graph"]:
        b = st["graph"]["by_status"]
        lines.append("graph      : " + ", ".join(f"{k}={v}" for k, v in sorted(b.items())))
    if "findings" in st:
        lines.append(f"findings   : {st['findings']}")
    if "sessions" in st:
        lines.append(f"sessions   : {st['sessions']} (last: {st.get('last_session', '-')})")
    if "artifacts" in st:
        lines.append(f"artifacts  : {st['artifacts']}")
    if st.get("last_calls"):
        lines.append("last calls :")
        for r in st["last_calls"][-5:]:
            lines.append(f"  {r['call']:14s} {r['status'][:40]}")
    return "\n".join(lines)


# ------------------------------------------------------------------ scope --
# Where the agent may reach on the network: websites (domains), single
# hosts/IPs and IP ranges (CIDRs). It lives in the project's notebook
# (scope.json), so it is edited with `aura scope` / `/scope`, not by hand.

def load_scope(root: Path) -> dict:
    try:
        d = json.loads((Path(root) / "scope.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _kind(item: str) -> str:
    import ipaddress
    if "/" in item:
        ipaddress.ip_network(item, strict=False)      # ValueError if bad
        return "cidrs"
    try:
        ipaddress.ip_address(item)
        return "hosts"
    except ValueError:
        pass
    if "." not in item and item != "localhost":
        raise ValueError(f"'{item}' is not a website, IP or IP range")
    return "domains" if "." in item else "hosts"


def _clean(item: str) -> str:
    from urllib.parse import urlparse
    s = item.strip().lower()
    if "://" in s:
        s = urlparse(s).hostname or s
    return s.strip("/").removeprefix("www.") if s.startswith("www.") else s.strip("/")


def scope_add(root: Path, item: str) -> str:
    """Allow a website (example.com — its subdomains too), a host/IP, or
    an IP range. Returns a plain sentence."""
    s = _clean(item)
    kind = _kind(s)
    d = load_scope(root) or dict(SCOPE_TEMPLATE)
    vals = list(d.get(kind) or [])
    if s in vals:
        return f"{s} is already allowed"
    vals.append(s)
    d[kind] = vals
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "scope.json").write_text(json.dumps(d, indent=2),
                                           encoding="utf-8")
    what = {"domains": "website (and its subdomains)",
            "hosts": "host", "cidrs": "IP range"}[kind]
    return f"allowed {what}: {s}"


def scope_remove(root: Path, item: str) -> str:
    s = _clean(item)
    d = load_scope(root)
    for kind in ("domains", "hosts", "cidrs"):
        if s in (d.get(kind) or []):
            d[kind] = [v for v in d[kind] if v != s]
            (Path(root) / "scope.json").write_text(json.dumps(d, indent=2),
                                                   encoding="utf-8")
            return f"removed {s}"
    return f"{s} was not allowed"


def set_web_all(root: Path, on: bool) -> str:
    """Let web readers open any public website in this project."""
    d = load_scope(root) or dict(SCOPE_TEMPLATE)
    d["web_all"] = bool(on)
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "scope.json").write_text(json.dumps(d, indent=2),
                                           encoding="utf-8")
    return ("all public websites may be read in this project (local and "
            "private addresses still need /scope add; commands are not widened)"
            if on else "only the websites listed in scope may be read")


def scope_lines(root: Path) -> list[str]:
    d = load_scope(root)
    rows = [("websites", d.get("domains") or []),
            ("hosts", d.get("hosts") or []),
            ("IP ranges", d.get("cidrs") or [])]
    out = ["where the agent may reach on the network (files are always "
           "limited to the project):"]
    for label, vals in rows:
        out.append(f"  {label:10s} {', '.join(vals) if vals else '—'}")
    if d.get("web_all", True):
        out.append("  all websites: ON — web pages anywhere public may be read "
                   "(/web all off)")
    if not any(v for _, v in rows) and not d.get("web_all", True):
        out.append("  nothing yet: web pages and network commands are "
                   "refused. Allow one: /scope add example.com — or all "
                   "websites for research: /web all on")
    return out

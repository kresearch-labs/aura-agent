"""mcp_registry.py — find and add MCP servers from the official registry.

Adding a server downloads and runs someone else's code when Aura starts — so it
is never automatic: you see what it runs and say yes; it lands in this profile
(or one project) and its tools are graded by their own hints (mcp_tools.py),
destructive or unlabelled ones ask first. Keys go in as ${VARIABLES}, never as
text.
"""
from __future__ import annotations

import json
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"   # The frozen, stable API


def search(words: str, limit: int = 10, fetch=None) -> list[dict]:
    """Servers matching the words: name, what it does, how it runs."""
    url = REGISTRY + "?" + urllib.parse.urlencode({"search": words, "limit": limit})
    raw = fetch(url) if fetch else urllib.request.urlopen(
        urllib.request.Request(url, headers={"Accept": "application/json",
                                             "User-Agent": "aura-agent"}), timeout=20).read()
    data = json.loads(raw)
    out = []
    for item in data.get("servers", []):
        srv = item.get("server", item)
        out.append({"name": srv.get("name", ""), "description": (srv.get("description") or "")[:200],
                    "version": srv.get("version", ""), "spec": spec_for(srv),
                    "needs": needed_env(srv)})
    return out


def needed_env(srv: dict) -> list[str]:
    names = []
    for p in srv.get("packages") or []:
        for e in p.get("environmentVariables") or p.get("environment_variables") or []:
            if e.get("isRequired") or e.get("is_required"):
                names.append(e.get("name", ""))
    for r in srv.get("remotes") or []:
        for h in r.get("headers") or []:
            if h.get("isRequired") or h.get("is_required"):
                names.append(h.get("name", ""))
    return [n for n in dict.fromkeys(names) if n]


def spec_for(srv: dict) -> dict | None:
    """Aura's mcp_servers entry for a registry server (None = can't run it here)."""
    env = {n: "${" + n.replace("-", "_").upper() + "}" for n in needed_env(srv)
           if not n.lower().startswith(("authorization", "x-"))}
    for p in srv.get("packages") or []:
        kind = (p.get("registryType") or p.get("registry_type") or "").lower()
        ident = p.get("identifier") or p.get("name") or ""
        ver = p.get("version") or srv.get("version") or ""
        if kind == "npm" and ident:
            return {"command": "npx", "args": ["-y", f"{ident}@{ver}" if ver else ident],
                    **({"env": env} if env else {})}
        if kind == "pypi" and ident:
            return {"command": "uvx", "args": [f"{ident}=={ver}" if ver else ident],
                    **({"env": env} if env else {})}
        if kind == "oci" and ident:
            return {"command": "docker", "args": ["run", "-i", "--rm", ident],
                    **({"env": env} if env else {})}
    for r in srv.get("remotes") or []:
        if r.get("url") and (r.get("type") or "").lower() in ("streamable-http", "http", "sse"):
            return {"url": r["url"]}
    return None


def runner_missing(spec: dict) -> str | None:
    """The program a server needs that isn't on this machine (npx, uvx, docker)."""
    cmd = spec.get("command")
    return cmd if cmd and shutil.which(cmd) is None else None


def short_name(registry_name: str) -> str:
    """A tool-friendly local name: 'io.github.org/server-x' → 'server-x'."""
    tail = registry_name.rsplit("/", 1)[-1]
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in tail)[:40] or "server"


def config_file(eng: Path | None, project: bool) -> Path:
    if project and eng is not None:
        return Path(eng) / ".aura" / "mcp.json"
    from .paths import profile_path
    return profile_path("profiles.json")


def add(name: str, spec: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        data = {}
    data.setdefault("mcp_servers", {})[name] = spec
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def remove(name: str, path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if name not in (data.get("mcp_servers") or {}):
        return False
    del data["mcp_servers"][name]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def listing(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("mcp_servers") or {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------- well-known servers (curated)
# The old reference servers (filesystem, git, fetch, memory, time) are left
# out: Aura has each built in. How each runs and its version come from the
# registry when added (never from this list).
POPULAR: dict[str, dict] = {
    "github":      {"name": "io.github.github/github-mcp-server", "by": "GitHub",
                    "what": "repositories, issues, pull requests, Actions",
                    "account": "a GitHub token (GITHUB_PERSONAL_ACCESS_TOKEN); runs in Docker"},
    "context7":    {"name": "io.github.upstash/context7", "by": "Upstash",
                    "what": "current, version-specific docs for libraries", "account": "none"},
    "sentry":      {"name": "io.github.getsentry/sentry-mcp", "by": "Sentry",
                    "what": "errors, issues and traces from Sentry", "account": "SENTRY_ACCESS_TOKEN"},
    "linear":      {"name": "app.linear/linear", "by": "Linear",
                    "what": "issues, projects and docs in Linear", "account": "sign-in in the browser"},
    "notion":      {"name": "com.notion/mcp", "by": "Notion",
                    "what": "pages and databases in Notion", "account": "sign-in in the browser"},
    "atlassian":   {"name": "com.atlassian/atlassian-mcp-server", "by": "Atlassian",
                    "what": "Jira, Confluence, Compass", "account": "sign-in in the browser"},
    "stripe":      {"name": "com.stripe/mcp", "by": "Stripe",
                    "what": "customers, payments, invoices — real money: every change asks first",
                    "account": "a Stripe key or sign-in"},
    "supabase":    {"name": "com.supabase/mcp", "by": "Supabase",
                    "what": "Supabase projects and databases", "account": "SUPABASE_ACCESS_TOKEN"},
    "huggingface": {"name": "co.huggingface/hf-mcp-server", "by": "Hugging Face",
                    "what": "models, datasets and Spaces on the Hub", "account": "optional sign-in"},
    "cloudflare-docs": {"name": "com.cloudflare.mcp/mcp", "by": "Cloudflare",
                        "what": "Cloudflare's documentation", "account": "none"},
    "playwright":  {"name": "io.github.microsoft/playwright-mcp", "by": "Microsoft",
                    "what": "a browser driven by page snapshots (Aura has its own browser tool)",
                    "account": "none"},
}


def _ver(v: str) -> tuple:
    out = []
    for part in str(v or "").replace("-", ".").split("."):
        out.append(int(part) if part.isdigit() else -1)
    return tuple(out)


def exact(name: str, fetch=None) -> dict | None:
    """The NEWEST registry entry with exactly this name (the registry keeps
    every version and doesn't sort them), or None. A short name from
    POPULAR is turned into its registry name first."""
    full = POPULAR.get(name.lower(), {}).get("name", name)
    hits = [h for h in search(full, limit=50, fetch=fetch) if h["name"] == full]
    return max(hits, key=lambda h: _ver(h["version"])) if hits else None

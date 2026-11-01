"""mcp_tools.py — MCP servers become agent tools, graded honestly.

The MCP client (mcp_client.py) existed but nothing ever loaded it, and the one
loader that did exist labelled every MCP tool read-only — whatever it did. Now:

Config — servers come from two places (workspace wins on a name clash):
  - the profile's profiles.json, top-level "mcp_servers" block
  - <project notebook>/.aura/mcp.json, {"mcp_servers": {...}}
  `.aura/` is write-protected from the agent, so it can't add servers.

Grading — each tool by the hints its server publishes (MCP annotations):
  readOnlyHint true -> read-only (runs freely, allowed in plan mode)
  destructiveHint false -> ordinary action (runs at L2)
  destructive, or no hints -> risky (G3): L2 asks first

Descriptions come from the server and land in the prompt, so they are
untrusted: capped, and withheld if they contain instruction-like text.
"""
from __future__ import annotations

import atexit
import json
import re
from pathlib import Path

from .mediator import BlastRadius, ToolSpec
from .tool_names import MCP_PREFIX

_DESC_CAP = 500
_started: list = []


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", part or "")


def tool_name(server: str, tool: str) -> str:
    """mcp__<server>__<tool>, valid for OpenAI-style tool names (≤ 64)."""
    return (MCP_PREFIX + _safe(server) + "__" + _safe(tool))[:64]


def load_config(eng: Path) -> dict:
    """Merged {"mcp_servers": {...}} from the profile store and the workspace."""
    servers: dict = {}
    try:
        from .keystore import unseal
        from .profile_store import _KS_PREFIX, _load_file
        servers.update(unseal((_load_file() or {}).get("mcp_servers") or {},
                              f"{_KS_PREFIX}.mcp_servers"))
    except Exception:
        pass
    p = Path(eng) / ".aura" / "mcp.json"
    if p.exists():
        try:
            servers.update(json.loads(p.read_text(encoding="utf-8"))
                           .get("mcp_servers") or {})
        except (OSError, json.JSONDecodeError):
            pass
    # MCP servers carried by TRUSTED packs (<pack>.<server>); the user's own
    # config wins on a name clash
    try:
        from .packs import pack_mcp_servers
        for n, spec in pack_mcp_servers(eng).items():
            servers.setdefault(n, spec)
    except Exception:
        pass
    return {"mcp_servers": servers}


def grade_for(annotations: dict) -> tuple[BlastRadius, str | None]:
    """(blast radius, grade override or None) from a tool's MCP hints."""
    a = annotations or {}
    if a.get("readOnlyHint") is True:
        return BlastRadius.READ, None
    if a.get("destructiveHint") is False:
        return BlastRadius.EXECUTE, None
    return BlastRadius.EXECUTE, "G3"         # Destructive, or nothing said


def _description(server: str, raw: str) -> str:
    from .secrets_guard import injection_flag, scan_untrusted
    sc = scan_untrusted(str(raw or ""))        # Tool poisoning hides text too
    text = " ".join(sc["text"].split())[:_DESC_CAP]
    if injection_flag(text) or sc["strong"]:
        text = ("(the server's description contained instruction-like text "
                "and was withheld)")
    return f"[MCP server '{server}'] {text or '(no description)'}"


def _parameters(schema) -> dict:
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}}


def register(mediator, dispatch: dict, eng: Path) -> list[dict]:
    """Start configured servers, register their tools, return their schemas.
    A server that fails to start or list contributes nothing."""
    from .mcp_client import expand_tools_for_mediator, load_servers_from_config
    servers = load_servers_from_config(load_config(eng))
    if not servers:
        return []
    overrides = getattr(mediator, "grade_overrides", None)
    if overrides is None:
        overrides = mediator.grade_overrides = {}
    schemas = []
    for server, tool in expand_tools_for_mediator(servers):
        name = tool_name(server.name, tool["name"])
        if name in mediator.tools:
            continue                          # Never shadow a native tool
        blast, grade = grade_for(tool.get("annotations"))

        def make_call(s=server, n=tool["name"]):
            def call(args: dict) -> str:
                return s.call_tool(n, args or {})
            return call

        mediator.register(ToolSpec(name, blast, lambda a: None),
                          plan_safe=blast == BlastRadius.READ)
        dispatch[name] = make_call()
        if grade:
            overrides[name] = grade
        schemas.append({"type": "function", "function": {
            "name": name,
            "description": _description(server.name, tool.get("description")),
            "parameters": _parameters(tool.get("schema"))}})
    for s in servers:
        if s not in _started:
            _started.append(s)
    return schemas


@atexit.register
def _stop_all() -> None:
    for s in _started:
        try:
            s.stop()
        except Exception:
            pass

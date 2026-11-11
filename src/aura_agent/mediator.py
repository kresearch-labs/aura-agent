"""Mediator: the deterministic Trusted Computing Base.

Every tool call, without exception, passes through execute. There is no bypass
path. The LLM proposes; the mediator disposes (BLUEPRINT Part 2.1).

Design laws enforced here:
  1. Scope is a deterministic predicate evaluated OUTSIDE the LLM, every call.
  2. Budgets are hard ceilings, not suggestions.
  3. Every call appends an audit line before the result is returned —
     refusals included. Silence is a bug.

Zero LLM involvement. Zero network calls. Fully property-testable.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from .eventlog import log_swallowed


class Violation(Exception):
    """A refused call. Refusal is audit-logged, never silent."""


class BlastRadius(Enum):
    READ = "read"        # Cannot change target or engagement state
    WRITE = "write"      # Changes files inside engagement root
    EXECUTE = "execute"  # Runs commands / touches the network


@dataclass(frozen=True)
class ToolSpec:
    """Static, code-authored metadata. The LLM never supplies these."""
    name: str
    blast_radius: BlastRadius
    # extract_target(args) -> host/ip/domain the call will touch, or None
    extract_target: Callable[[dict], str | None]
    default_timeout: float = 300.0
    # Ask the operator before EVERY call, at every assist level (L3 too), and
    # never offer "always" — e.g. changing the operator's rules
    always_ask: bool = False
    # describe(args) -> one extra line for the approval card (e.g. the text
    # of the rule a remove-by-number would delete)
    describe: Callable[[dict], str] | None = None
    # ask_when(args) -> reason to ask the operator for THIS call, or None to
    # let it through (e.g. memory text that reads like orders). Like
    # always_ask, it applies at every level and is never pre-approved.
    ask_when: Callable[[dict], str | None] | None = None


@dataclass
class Budgets:
    per_call_timeout: float = 300.0
    session_tool_caps: dict[str, int] = field(default_factory=dict)
    # Was a hard-coded 4M that nothing enforced. Now opt-in (the design law is
    # 'uncapped by default'); set via budget, saved settings or
    # AURA_TOKEN_CEILING. Passing it WARNS once — the loop never stops working
    # work for it (SETTINGS_DESIGN ruling).
    token_ceiling: int | None = None
    tokens_spent: int = 0
    warned_at: int | None = None          # Ceiling value already warned for
    _tool_counts: dict[str, int] = field(default_factory=dict)

    def check_and_count(self, spec: ToolSpec) -> None:
        n = self._tool_counts.get(spec.name, 0) + 1
        cap = self.session_tool_caps.get(spec.name)
        if cap is not None and n > cap:
            raise Violation(f"budget: tool '{spec.name}' exceeded session cap {cap}")
        self._tool_counts[spec.name] = n

    def add_tokens(self, n: int) -> bool:
        """Count tokens; False once a ceiling is set and crossed."""
        self.tokens_spent += max(0, int(n or 0))
        return not self.over_ceiling()

    def over_ceiling(self) -> bool:
        return bool(self.token_ceiling) and self.tokens_spent >= self.token_ceiling


class Scope:
    """Deterministic target membership. scope.json data is the ONLY source."""

    def __init__(self, hosts: set[str], cidrs: list[str], domains: set[str],
                 web_all: bool = False, web_all_set: bool = False):
        self.hosts = {h.lower() for h in hosts}
        self.cidrs = list(cidrs)
        self.domains = {d.lower() for d in domains}
        self.web_all = bool(web_all)
        self.web_all_set = bool(web_all_set)

    @classmethod
    def from_dict(cls, d: dict) -> Scope:
        return cls(
            hosts=set(d.get("hosts", [])),
            cidrs=list(d.get("cidrs", [])),
            domains=set(d.get("domains", [])),
            web_all=bool(d.get("web_all", True)),
            web_all_set=bool(d.get("web_all")),
        )

    def allows(self, tool: str, target: str | None) -> bool:
        """contains(), plus the all-websites switch for web readers."""
        if self.contains(target):
            return True
        from .tool_names import WEB_READERS
        return self.web_all and tool in WEB_READERS and is_public_host(target or "")

    def contains(self, target: str | None) -> bool:
        """Pure predicate. No DNS here — the RESOLVED value must be in scope."""
        if target is None:
            return True  # Call touches nothing network-reachable
        t = target.strip().lower()
        if not t:
            return True
        if t in self.hosts:
            return True
        if t in self.domains or any(t.endswith("." + d) for d in self.domains):
            return True
        return any(self._ip_in(c, t) for c in self.cidrs)

    @staticmethod
    def _ip_in(cidr: str, ip: str) -> bool:
        try:
            import ipaddress

            return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return False


_URL_RX = __import__("re").compile(r"\bhttps?://([^/\s:'\"`?#]+)", __import__("re").I)


def _url_hosts(cmd: str) -> list[str]:
    """The hosts of the web addresses written in a command."""
    out: list[str] = []
    for h in _URL_RX.findall(cmd or ""):
        h = h.lower().split("@")[-1]
        if h and h not in out:
            out.append(h)
    return out


def is_public_host(host: str) -> bool:
    """A name or address on the public internet: not localhost, not a
    private / loopback / link-local address, not a one-word or .local /
    .internal / .lan name."""
    import ipaddress
    h = host.strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal",
                                                ".lan", ".home", ".corp", ".intranet")):
        return False
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return ip.is_global
    except ValueError:
        pass
    return "." in h


class Mediator:
    """execute() is the only door. ~300 LOC by design; grow tests, not code."""

    def __init__(self, scope: Scope, budgets: Budgets, audit_path: Path,
                 engagement_root: Path):
        self.scope = scope
        self.budgets = budgets
        self.audit_path = audit_path
        self.root = engagement_root.resolve()
        self.tools: dict[str, ToolSpec] = {}
        self.mode = "act"          # 'plan' restricts to plan-safe tools
        self.assist = "L2"         # L1 every write · L2 risky (G3) only · L3 never
        self.grade_overrides: dict[str, str] = {}   # e.g. MCP hints
        # Set by an interactive surface (chat, `aura run` on a TTY):
        # approver(request) -> "once" | "always" | "deny". None = nobody to
        # ask, so a gated call is refused (headless, cron, piped input).
        self.approver: Callable[[dict], str] | None = None
        # asker(question, options) -> chosen option text | None: a
        # question for the user that isn't an approval (e.g. which free
        # search services may be used). None = nobody to ask.
        self.asker: Callable[[str, list[str]], str | None] | None = None
        self._declined: set[tuple[str, str]] = set()  # (tool, args digest)
        # Skill packs loaded in this conversation, with the limits their
        # manifests declare; saved next to the session (the loop points
        # pack_record at it). Every call must fit every active pack.
        self.pack_limits: dict = {}
        self.pack_record: Path | None = None
        # 2.9: workspace whose hooks/ (and the profile's hooks) fire on tool_start
        # / tool_done; None = hooks off (helpers, tests)
        self.hooks_root: Path | None = None
        self.plan_safe: set[str] = set()
        self.killed = False        # .kill observed by REPL flips this
        # Operator constraints root (engagement dir). Set by callers whose
        # work_dir differs (arena sandboxes); default = engagement root
        self.constraints_root: Path | None = engagement_root.resolve()
        # outside.py: folders outside the project the file tools may use —
        # saved for the project, plus the ones allowed in this chat
        from .outside import saved as _saved_dirs
        self.outside_ok: set = set(_saved_dirs(engagement_root))
        self.outside_exact: set = set()      # Exact paths (a file on a drive)

    def register(self, spec: ToolSpec, plan_safe: bool = False) -> None:
        self.tools[spec.name] = spec
        if plan_safe:
            self.plan_safe.add(spec.name)

    def execute(self, name: str, args: dict, run: Callable[[dict], Any]) -> Any:
        if self.killed:
            raise Violation("kill switch active (.kill present) — loop halted")
        spec = self.tools.get(name)
        if spec is None:
            raise Violation(f"unknown tool '{name}' — not registered, refused")
        # Changing one of Aura's own settings always asks the user with a card,
        # so it stays possible in plan and discuss mode
        _own_setting = name == "settings_set"
        if self.mode == "plan" and name not in self.plan_safe and not _own_setting:
            self._audit(name, args, "REFUSED_MODE", None)
            raise Violation(f"mode: tool '{name}' not allowed in plan mode")
        # Discuss: the user wants to talk, not to have the work done — reading
        # and searching to understand are fine, nothing that changes anything
        # (files, commands, plans) runs
        if self.mode == "discuss" and spec.blast_radius != BlastRadius.READ and not _own_setting:
            self._audit(name, args, "REFUSED_MODE", None)
            raise Violation(f"discuss mode: '{name}' would change something — the user only "
                            "wants to talk. Answer in words; if they want it done, say "
                            "/discuss off lets you do it")
        if getattr(self, "turn_readonly", False) and spec.blast_radius != BlastRadius.READ:
            self._audit(name, args, "REFUSED_MODE", None)
            raise Violation(f"'{name}' would change something, and the user only asked a "
                            "question — answer it in words; if they want it done they'll say so")
        # Assist levels, as the assist menu describes them: L1 asks before
        # every write/execute (a saved grant can pre-approve) L2 asks only
        # before G3 — can't be undone or reaches outside L3 never asks (what L2
        # actually did before; explicit opt-in): an active skill pack's
        # declared limits — checked before any question, so the operator is
        # never asked about a call that the pack's limits would refuse anyway
        for _lim in list(self.pack_limits.values()):
            from .pack_limits import check as _pack_check
            _why = _pack_check(_lim, name, spec.blast_radius.value, args)
            if _why:
                self._audit(name, args, f"REFUSED_PACK:{_lim.name}", None)
                raise Violation(
                    f"skill pack '{_lim.name}' is active and did not declare "
                    f"this: {_why}. Its limits hold for the rest of this "
                    "conversation; tell the operator if the task needs more")
        # scope before any question: the operator is never asked to approve
        # a call that scope would refuse anyway
        target = spec.extract_target(args)
        if not self.scope.allows(name, target) and \
                str(target or "").lower() not in getattr(self, "scope_ok", set()):
            if not self._ask_scope(name, args, str(target)):
                self._audit(name, args, "REFUSED_SCOPE", target)
                raise Violation(
                    f"scope: '{target}' is not in this project's allowed list and the "
                    "user didn't allow it — don't retry it; continue another way")
        # 2.9: the operator's tool_start hooks — before any question, so the
        # operator is never asked about a call their own hook blocks
        _pre = self._hook("tool_start", name, args)
        if _pre.get("blocked"):
            self._audit(name, args, "REFUSED_HOOK", None)
            raise Violation(f"a hook blocked '{name}': "
                            f"{_pre.get('reason') or 'exit 2'}")
        # Some calls always need the operator — no level skips them
        approved = False
        # outside.py: a file tool reaching a folder outside the project asks
        # first (yes = that folder for this chat, always = saved for the
        # project); L3 never asks
        _out = self._outside(name, args)
        if _out is not None:
            folder, path = _out
            if self.assist == "L3":
                self.outside_ok.add(folder)
            elif self._ask_outside(name, spec, args, folder, path):
                approved = True
            else:
                self._audit(name, args, "REFUSED_OUTSIDE", None)
                raise Violation(
                    f"'{path}' is outside the project and nobody is at the keyboard "
                    "to allow it — continue without it and say what you needed")
        ask_why = None
        if spec.always_ask:
            ask_why = "the agent proposes this; it can't be pre-approved"
        elif spec.ask_when is not None:
            try:
                ask_why = spec.ask_when(args)
            except Exception:
                ask_why = "this call could not be checked — asking to be safe"
        if not ask_why and name in ("run_command", "process_start") and \
                str((args or {}).get("network", "")).lower() in ("1", "true", "yes"):
            ask_why = "the command asks for full network (past this project's allowed sites)"
        if not ask_why and name in ("run_command", "process_start"):
            _out = [h for h in _url_hosts(str((args or {}).get("cmd") or ""))
                    if h not in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")   # This machine (dev servers)
                    and not self.scope.contains(h)
                    and not (getattr(self.scope, "web_all_set", False) and is_public_host(h))]
            if _out:
                ask_why = (f"the command reaches {', '.join(_out[:3])} — outside this "
                           "project's allowed sites; yes lets it reach them for this chat")
                self._sites_for_call = _out
        # promptware gate: outside text in this task tried to give
        # instructions — commands ask first at every level, full autonomy too
        if not ask_why and getattr(self, "tainted", False):
            from .autonomy import _TAINT_GATED
            if name in _TAINT_GATED:
                ask_why = ("outside text in this task tried to give instructions — "
                           "check this is what you asked for")
        if ask_why:
            if not self._ask(name, spec, args, None, ask_why,
                             allow_always=False,
                             heading=("needs your OK every time"
                                      if spec.always_ask
                                      else "needs your OK")):
                self._audit(name, args, "REFUSED_ASSIST", None)
                raise Violation(
                    f"assist: '{name}' needs the operator's OK ({ask_why}) "
                    "and nobody is here to answer — suggest it to the "
                    "operator in your answer instead")
            approved = True
            if getattr(self, "_sites_for_call", None):   # The yes covers those sites
                if not hasattr(self, "scope_ok"):
                    self.scope_ok = set()
                self.scope_ok |= {h.lower() for h in self._sites_for_call}
        self._sites_for_call = None
        if self.assist == "L2" and not approved:
            try:
                from .autonomy import grade_of as _g
                _is_g3 = _g(name, spec.blast_radius.value,
                            overrides=self.grade_overrides, args=args,
                            tainted=bool(getattr(self, "tainted", False))) == "G3"
            except Exception:
                _is_g3 = True        # Grading broke → ask rather than act
            if _is_g3 and self._ask(name, spec, args, "G3",
                                    "can't be undone or reaches outside"):
                _is_g3 = False                     # The operator said yes
            if _is_g3:
                self._audit(name, args, "REFUSED_ASSIST", None)
                raise Violation(
                    f"assist: '{name}' can't be undone or reaches outside "
                    "(G3) — needs operator approval (L2). Tell the operator "
                    "what you want to run; they can run it themselves or "
                    "allow it")
        if (self.assist == "L1" and not approved
                and spec.blast_radius in (BlastRadius.WRITE, BlastRadius.EXECUTE)):
            # BUILD 3: a persisted grant (action TYPE + TARGET, G3 never,
            # wildcard-execute banned) satisfies the L1 gate for this call —
            # audited as OK_GRANT:<id>, never silently
            _grant = None
            try:
                from .autonomy import grade_of, grant_target, match_grant
                # args matter: `git push` is G3 and must never match a grant
                _grade = grade_of(name, spec.blast_radius.value,
                                  overrides=self.grade_overrides, args=args,
                                  tainted=bool(getattr(self, "tainted", False)))
                _tgt = grant_target(name, args, spec)
                _grant = match_grant(self.root, _grade, name, str(_tgt))
            except Exception:
                _grant = None  # Grant machinery must never widen the gate
            if _grant is None and self._ask(
                    name, spec, args, None, "strict mode asks before every "
                    "write or command"):
                _grant = {"id": "operator"}
            if _grant is None:
                self._audit(name, args, "REFUSED_ASSIST", None)
                raise Violation(
                    f"assist: '{name}' needs operator approval (L1) — nobody "
                    "is here to answer (chat asks; /approvals saves a permission)")
            if _grant.get("id") != "operator":
                self._audit(name, args, f"OK_GRANT:{_grant.get('id', '?')}",
                            None)
        self.budgets.check_and_count(spec)
        if spec.blast_radius != BlastRadius.READ:
            # anything that could change files (a write, a command): the
            # counter run_checks compares to skip an identical re-run
            self._changes = getattr(self, "_changes", 0) + 1
            if name in ("write_file", "edit_file", "multi_edit", "apply_patch") \
                    and isinstance(args.get("path"), str):
                # the files this run changed: run_checks picks their tests
                if not hasattr(self, "_changed_paths"):
                    self._changed_paths = []
                if args["path"] not in self._changed_paths:
                    self._changed_paths.append(args["path"])
        timeout = min(spec.default_timeout, self.budgets.per_call_timeout)
        t0 = time.monotonic()
        try:
            if name in ("web_search", "web_crawl") and getattr(self, "_research_left", None) is not None:
                if self._research_left <= 0:        # find_way: the research the user allowed
                    raise Violation("the research the user allowed is used up — go on with "
                                    "what you found, or tell the user plainly")
                self._research_left -= 1
            result = run(args)
            if name in ("run_command", "process_start") and isinstance(result, str):
                from .find_way import affordance    # The way out rides in the failure
                result = result + affordance(name, result)
        except Violation as e:
            # tool-internal refusal (e.g. path escape): audit + re-raise
            self._audit(name, args, f"REFUSED_PATH:{e}", target)
            self._hook("tool_done", name, args, status="refused")
            raise
        except Exception as e:  # Tool failure ≠ violation; audited, re-raised
            self._audit(name, args, f"TOOL_ERROR:{type(e).__name__}:{e}", target)
            self._hook("tool_done", name, args, status="error")
            raise
        elapsed = time.monotonic() - t0
        # The time limit is enforced INSIDE the tools (a command is killed
        # at its timeout). Here the call has already finished: refusing it
        # now would report work that happened as not done — the model
        # would run it again. A slow success is audited as slow, and kept.
        self._audit(name, args,
                    f"OK_SLOW:{elapsed:.0f}s" if elapsed > timeout else "OK",
                    target)
        _post = self._hook("tool_done", name, args, status="ok")
        if _post.get("note"):
            self._audit(name, args, f"HOOK_NOTE:{str(_post['note'])[:200]}",
                        target)
        # Constraints: for write/execute tools, attach the operator's relevant
        # rules to the result so the model reads them NEXT TO the action it
        # just took (or is about to take) — not from memory.
        if (spec.blast_radius in (BlastRadius.WRITE, BlastRadius.EXECUTE)
                and self.constraints_root is not None):
            try:
                from .constraints import relevant_block
                cb = relevant_block(self.constraints_root, args)
                if cb and isinstance(result, dict):
                    result["constraints"] = cb
                elif cb:
                    result = {"result": result, "constraints": cb}
            except Exception as _swallowed:
                log_swallowed("mediator.constraints", _swallowed)  # Constraints must never break a tool result
        return result

    def _hook(self, event: str, name: str, args: dict, **extra) -> dict:
        """2.9: run the operator's hooks for one event. Never raises; a
        broken hook never blocks or breaks a call (only exit 2 blocks)."""
        if self.hooks_root is None:
            return {}
        try:
            from .cronops import run_hooks
            from .secrets_guard import minimize_args
            return run_hooks(self.hooks_root, event,
                             {"tool": name, "args": minimize_args(args),
                              **extra})
        except Exception as _swallowed:
            log_swallowed("mediator.hooks", _swallowed)
            return {}

    def activate_pack(self, limits) -> None:
        """From now on, every call must fit this pack's limits."""
        from .pack_limits import save_active
        self.pack_limits[limits.name] = limits
        try:
            save_active(self.pack_record, self.pack_limits)
        except OSError:
            pass                              # Limits still hold in memory

    def credential_filter(self) -> list[str] | None:
        """Secret names shell commands may receive: None = no pack active
        (no extra limit); otherwise only names every active pack allows."""
        if not self.pack_limits:
            return None
        allowed: set[str] | None = None
        for lim in self.pack_limits.values():
            names = set(lim.credential_access)
            allowed = names if allowed is None else allowed & names
        return sorted(allowed or ())

    _PATH_TOOLS = ("list_files", "read_file", "write_file", "edit_file", "search_files")

    def _outside(self, name: str, args: dict):
        """(folder, path) when this file-tool call reaches outside the project
        into a folder not allowed yet; None otherwise (inside, allowed, or a
        place that is never allowed — the tool refuses that with the reason)."""
        if not isinstance(args, dict):
            return None
        if name in ("run_command", "process_start") and not getattr(self, "_by_user", False):
            # A command names paths too; the user's own!cmd is theirs. A
            # key/credential place is refused outright.
            from .outside import folder_of, inside, never_reason, paths_in_command
            low = str(args.get("cmd") or "").lower()
            if any(t.lower() in low for t in getattr(self, "banned_tokens", ())):
                return None              # The tool refuses it as banned, with that reason
            work = Path(getattr(self, "work_dir", None) or self.root).resolve()
            for p in paths_in_command(str(args.get("cmd") or ""), work):
                why = never_reason(p)
                if why:
                    raise Violation(f"the command names '{p}' — it can't be used: {why}")
                if not inside(p, self.outside_ok) and p not in self.outside_exact:
                    return folder_of(p), p
            return None
        if name not in self._PATH_TOOLS:
            return None
        raw = args.get("path") or ("." if name in ("list_files", "search_files") else "")
        if not isinstance(raw, str) or not raw.strip():
            return None
        import os
        from .outside import folder_of, inside, never_reason
        _exact = getattr(self, "outside_exact", set())
        work = Path(getattr(self, "work_dir", None) or self.root).resolve()
        try:
            p = (work / os.path.expanduser(raw)).resolve()
        except (OSError, ValueError):
            return None
        if p == work or work in p.parents or never_reason(p):
            return None
        if inside(p, self.outside_ok) or p in _exact:
            return None
        return folder_of(p), p

    def _ask_scope(self, name: str, args: dict, target: str) -> bool:
        """A site or host outside the allowed list: ask. yes = this chat, always =
        saved to the project's list.
        """
        if self.approver is None or not target:
            return False
        from .approvals import _digest_args, ask_operator
        key = (name, "scope", target.lower())
        if key in self._declined:
            return False

        def _keep() -> str:
            from .engagement import scope_add
            return scope_add(self.root, target)
        ok, note = ask_operator(
            self.root, self.approver, name, args, "G2", target,
            f"{target} isn't in this project's allowed sites — yes allows it for this "
            "chat, always saves it for the project",
            allow_always=True, heading="a site outside the list", on_always=_keep)
        if ok:
            if not hasattr(self, "scope_ok"):
                self.scope_ok = set()
            self.scope_ok.add(target.lower())
            self._audit(name, args, f"OK_SCOPE:{note[:80]}", target)
            return True
        if "declined" in note:
            self._declined.add(key)
        return False

    def _ask_outside(self, name: str, spec: "ToolSpec", args: dict,
                     folder: Path, path: Path) -> bool:
        if self.approver is None:
            return False
        from .approvals import _digest_args, ask_operator
        key = (name, _digest_args(args or {}))
        if key in self._declined:
            raise Violation(f"the operator already said no to '{path}' this session — "
                            "do not ask again; continue another way")
        from .outside import addable, save
        # a whole drive or the home folder is too wide to allow: yes allows
        # exactly this file (or this listing) for the chat
        wide = addable(folder) is not None

        def _keep() -> str:
            save(self.root, folder)
            return f"{folder} saved for this project (/add-dir to see or remove)"
        command = name in ("run_command", "process_start")
        ok, note = ask_operator(
            self.root, self.approver, name, args, "G2", str(folder),
            (f"{path} is outside this project — yes allows this one call only "
             "(a whole drive or home folder is never allowed)" if wide and command else
             f"{path} is outside this project — yes allows this one file for this chat "
             "(a whole drive or home folder is never allowed)" if wide else
             f"{path} is outside this project — yes allows the folder {folder} "
             "for this chat, always saves it for this project"),
            allow_always=not wide, heading="outside the project", on_always=_keep)
        if ok:
            if wide and not command:
                self.outside_exact.add(Path(path).resolve())   # This file, for the chat
            elif wide:
                pass                       # A command over a whole drive: this call only
            else:
                self.outside_ok.add(folder)
            self._audit(name, args, f"OK_OUTSIDE:{note[:80]}", None)
            return True
        if "declined" in note:
            self._declined.add(key)
        self._audit(name, args, "REFUSED_OPERATOR", None)
        raise Violation(f"{note} — '{path}' was not used. Do not retry it; "
                        "continue another way or ask the user")

    def _ask(self, name: str, spec: "ToolSpec", args: dict,
             grade: str | None, why: str, allow_always: bool = True,
             heading: str | None = None) -> bool:
        """Put a gated call to the operator. True = run it. A refusal or an
        expired question raises the reason, so the model is told the operator
        said no (and does not just retry).
        """
        if self.approver is None:
            return False
        from .approvals import _digest_args, ask_operator
        key = (name, _digest_args(args or {}))
        if key in self._declined:            # Asked once already: no nagging
            self._audit(name, args, "REFUSED_OPERATOR", None)
            raise Violation(f"assist: the operator already said no to this "
                            f"exact '{name}' call this session — do not "
                            "ask again; continue another way")
        if grade is None:
            try:
                from .autonomy import grade_of
                grade = grade_of(name, spec.blast_radius.value,
                                 overrides=self.grade_overrides, args=args,
                                 tainted=bool(getattr(self, "tainted", False)))
            except Exception:
                grade = "G3"
        from .autonomy import AutonomyEngine, budget_exceeded_card, grant_target
        if getattr(self, "_approval_budget", None) is None:
            self._approval_budget = AutonomyEngine()
        hint = (None if self._approval_budget.approval_budget_ok()
                else budget_exceeded_card())
        detail = None
        if spec.describe is not None:
            try:
                detail = spec.describe(args)
            except Exception:
                detail = None
        ok, note = ask_operator(self.root, self.approver, name, args, grade,
                                grant_target(name, args, spec), why,
                                allow_always=allow_always, detail=detail,
                                heading=heading, hint=hint)
        if ok:
            self._audit(name, args, f"OK_APPROVED:{note[:80]}", None)
            return True
        if "declined" in note:
            self._declined.add(key)
        self._audit(name, args, "REFUSED_OPERATOR", None)
        raise Violation(f"assist: {note} — '{name}' was not run. Do not "
                        "retry it; continue another way or ask the operator")

    def _audit(self, name: str, args: dict, status: str, target: str | None) -> None:
        line = {
            "ts": round(time.time(), 3),
            "call": name,
            "args_hash": hashlib.sha256(
                json.dumps(args, sort_keys=True, default=str).encode()
            ).hexdigest()[:16],
            "target": target,
            "status": status,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # several Auras on one project (the fleet) share this log: locked
        from .sessions import locked_append
        locked_append(self.audit_path, json.dumps(line, ensure_ascii=False) + "\n")

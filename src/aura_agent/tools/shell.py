"""shell.py — run_command with scope enforcement + hardening.

BLUEPRINT: ToolSpec EXECUTE; the tool declares its target via extract_target
(args.host or args.target), so the mediator scope-checks it before anything
runs. Hardening: - banned first-tokens (rm -rf /, shutdown, mkfs, dd
of=/dev/...) with config allowlist override - per-call timeout (mediator
enforces; tool also passes subprocess timeout) - stdout/stderr captured, output
→ artifact when large - command executed LOCALLY by default; remote targets go
through the operator's ssh (the command string may embed ssh user@host — the
extracted target is what the mediator scope-checks)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from .artifacts import ARTIFACT_INLINE_LIMIT, head_tail_preview, store_artifact
from ..eventlog import log_swallowed
from ..tool_names import RUN_COMMAND

DEFAULT_BANNED = ("rm -rf /", "shutdown", "reboot", "mkfs", "dd if=",
                  ":(){", "chmod -R 777 /", "> /dev/sda")
MAX_STORED = 5_000_000     # Per stream: what the saved copy holds
# BUILD 1: commands whose effect must be verified from disk afterwards
# what a blocked network looks like from curl, wget, pip, npm, git, Python, Node
_NET_FAIL = re.compile(
    r"could not resolve host|temporary failure in name resolution|name or service not known|"
    r"getaddrinfo|network is unreachable|failed to establish a new connection|urlopen error|"
    r"nodename nor servname|enotfound|eai_again|connection refused|operation not permitted.*connect|"
    r"unable to access 'https?://|no route to host", re.I)
_MUTATING_WORDS = ("rm", "rmdir", "mv", "dd", "chmod", "chown", "truncate",
                   "ln", "cp", "tee", "shutdown", "reboot", "mkfs")


def _shell_inline_limit() -> int:
    """Dial 1 applies to shell output too — the active limit level's
    inline cap (the wrapper's artifact store keeps the full copy)."""
    try:
        from ..config import resolve_settings
        return resolve_settings().tool_output
    except Exception:
        return ARTIFACT_INLINE_LIMIT


def _site_ok_for(mediator: Mediator):
    """The proxy's allow rule: the project's list, plus the sites the user
    said yes to in this chat (the site card)."""
    def ok(host: str) -> bool:
        return mediator.scope.contains(host) or             str(host or "").lower() in getattr(mediator, "scope_ok", set())
    return ok


def register(mediator: Mediator, dispatch: dict,
             banned: tuple[str, ...] = (), allow_unbanned: bool = False) -> None:
    banned_tokens = () if allow_unbanned else tuple(banned or DEFAULT_BANNED)
    mediator.banned_tokens = banned_tokens     # The outside-folder check leaves these to this refusal

    def run_command(args) -> str:
        cmd = (args.get("cmd") or "").strip()
        if not cmd:
            raise Violation("run_command: empty command")
        low = cmd.lower()
        for tok in banned_tokens:
            if tok.lower() in low:
                raise Violation(
                    f"banned command token '{tok}' (allowlist override available)")
        timeout = float(args.get("timeout", mediator.budgets.per_call_timeout))
        _site_ok = _site_ok_for(mediator)
        try:
            # BUILD 4+5: OS containment — commands run inside bubblewrap
            # (fs: write only engagement root + /tmp; network off unless the
            # operator approves per call) with an honest fallback note when
            # bwrap is unavailable. AURA_SANDBOX=0 disables entirely.
            _network = str(args.get("network", "")).lower() in ("1", "true", "yes")
            # default: the workspace (where the file tools and tests work)
            _ws = getattr(mediator, "work_dir", None)
            _cwd = str(args.get("cwd") or _ws or Path.cwd())
            # BUILD 6: broker env (notary socket, git askpass, just-in-time
            # API-key injection) — active only when the engagement declares
            # a secrets map (.aura/secrets.map); valet cached per mediator.
            _extra_env = None
            try:
                from ..keybroker import ValetServer, sandbox_env, load_meta
                if load_meta(mediator.root):
                    _valet = getattr(mediator, "_valet", None)
                    if _valet is None:
                        _valet = ValetServer(mediator.root).start()
                        mediator._valet = _valet
                    _extra_env = sandbox_env(
                        mediator.root, _valet,
                        allowed=mediator.credential_filter())
            except Exception:
                _extra_env = None    # Broker must never kill the shell path
            from ..sandbox import run_sandboxed
            proc, sandbox_note = run_sandboxed(
                cmd, Path(_ws or mediator.root), cwd=_cwd,
                timeout=timeout, network=_network,
                extra_env=_extra_env, allowed=_site_ok)
            _refused = []
            if not _network:
                try:
                    from ..netproxy import _PROXY
                    _refused = _PROXY.take_refused() if _PROXY is not None else []
                except Exception:                       # noqa: BLE001
                    _refused = []
            out = (proc.stdout or "") + (proc.stderr or "")
            # Scrub secrets from command output before the tape
            try:
                from ..secrets_guard import scrub_secrets
                proc.stdout = scrub_secrets(proc.stdout or "")
                proc.stderr = scrub_secrets(proc.stderr or "")
            except Exception as _swallowed:
                # fail closed: exit code stays visible, raw output does not
                log_swallowed("shell.secrets_scrub", _swallowed)
                from ..secrets_guard import withheld
                proc.stdout = withheld("run_command", _swallowed)
                proc.stderr = ""
            so, se = proc.stdout or "", proc.stderr or ""
            payload = {"cmd": cmd, "exit_code": proc.returncode,
                       "stdout": so, "stderr": se, "sandbox": sandbox_note}
            if getattr(proc, "left_running", False):
                # the command ended but programs it started were still running:
                # they were stopped with it (nothing Aura runs outlives its call)
                payload["left_running"] = (
                    "programs this command started in the background were still running "
                    "when it ended and were stopped — start a program that must keep "
                    "running with process_start")
            # Say plainly that the sandbox has no network, once per call
            if _refused:
                _ok = [h for h in _refused[:3] if mediator._ask_scope("run_command", args, h)]
                if _ok:
                    payload["network_blocked"] = (
                        f"the command was refused {', '.join(_refused[:5])}; the user has now "
                        f"allowed {', '.join(_ok)} — run the command again")
                else:
                    payload["network_blocked"] = (
                        f"Aura's site list refused: {', '.join(_refused[:5])} and the user didn't "
                        "allow it (or nobody is at the keyboard) — retrying won't help; continue "
                        "another way (web_search / web_crawl read the web)")
            elif proc.returncode != 0 and "network=off" in (sandbox_note or "") \
                    and _NET_FAIL.search(so + se):
                payload["network_off"] = (
                    "this command has no network: the sandbox keeps it off. Retrying "
                    "won't help — use web_search / web_crawl to read the web, or ask "
                    "the user to allow network for this command (network: true asks them)")
            # BUILD 1 (shell side): mutating commands get a verification
            # directive in the result — the agent must confirm the effect
            # from disk before building on it (Gemini CLI lesson)
            _first = cmd.strip().split()[:1]
            if _first and _first[0] in _MUTATING_WORDS:
                payload["verify_directive"] = (
                    "mutating command — confirm the on-disk effect "
                    "(list_files / read_file) before building on it")
            # The start and end of each stream stay inline (errors sit at the
            # end); the WHOLE output is saved, reopenable with read_file.
            limit = _shell_inline_limit()
            ref = None
            if len(so) + len(se) > limit:
                so, se = so[:MAX_STORED], se[:MAX_STORED]
                art = store_artifact(Path(mediator.root), (
                    f"$ {cmd}\n[exit {proc.returncode}]\n--- stdout ---\n{so}"
                    f"\n--- stderr ---\n{se}").encode("utf-8", "replace"),
                    "run_command.txt")
                ref = art["artifact_ref"]
                se_room = min(len(se), max(limit // 3, limit - len(so)))
                payload["stdout"] = head_tail_preview(so.encode("utf-8", "replace"),
                                                      limit=max(1, limit - se_room))
                payload["stderr"] = head_tail_preview(se.encode("utf-8", "replace"),
                                                      limit=max(1, se_room))
                payload["full_output"] = (f"{len(so) + len(se):,} chars — the whole "
                                          f"output: read_file {ref}")
            data = json.dumps(payload, ensure_ascii=False)
            return json.dumps({"ok": proc.returncode == 0,
                               "exit_code": proc.returncode, "result": data,
                               "truncated": ref is not None,
                               "total_bytes": getattr(proc, "out_bytes", len(so) + len(se)),
                               "artifact_ref": ref, "duration_ms": 0},
                              ensure_ascii=False)
        except subprocess.TimeoutExpired as _te:
            from ..sandbox_os import Stopped
            if isinstance(_te, Stopped):
                raise Violation("run_command: stopped — the user stopped the task, the "
                                "command and everything it started were ended") from None
            raise Violation(f"run_command: timeout after {timeout:.0f}s — the command and "
                            "everything it started were stopped; run a smaller part "
                            "(for tests: only the ones related to the change)")

    mediator.register(ToolSpec(
        RUN_COMMAND, BlastRadius.EXECUTE,
        lambda a: a.get("host") or a.get("target")))
    dispatch[RUN_COMMAND] = run_command

    # per-tool timeout ceilings (pack config may override)
    mediator.tool_timeouts = {"run_command": mediator.budgets.per_call_timeout}

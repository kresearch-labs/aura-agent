"""autonomy.py — graded per-action autonomy."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from .tool_names import BROWSER_RUN, SHELL_TOOLS


# G3 = can't be undone, or reaches other people / systems. Reading a web page
# is neither, so web_crawl / web_search grade by blast radius like any other
# read.
_G3_VERBS = ("send", "notify", "webhook", "rules_remove")

# run_command is G3 when the command publishes, deploys, sends data or acts
# on a remote machine. Local work — editing, rm inside the workspace (auto-
# checkpointed first), installing packages, git commit — is not.
_G3_SHELL = [re.compile(p, re.I) for p in (
    r"\bgit\s+push\b",
    r"\b(npm|yarn|pnpm|cargo|poetry|gem)\s+publish\b",
    r"\btwine\s+upload\b",
    r"\bdocker\s+push\b",
    r"\bgh\s+(pr\s+(create|merge|close)|release\s+create|repo\s+(create|delete)|issue\s+create)\b",
    r"\bkubectl\s+(apply|delete|create|replace|patch|scale|rollout)\b",
    r"\bterraform\s+(apply|destroy)\b",
    r"\bhelm\s+(install|upgrade|uninstall|delete|rollback)\b",
    r"\b(ssh|scp|sftp)\s",
    r"\brsync\b[^|;&]*\s[\w.@-]+:",                 # Rsync to host:path
    r"\b(sendmail|mail|mutt)\b",
    # Changes to the SYSTEM, outside the project: things that run on their own
    # later or change the machine — every OS
    r"\bschtasks(\.exe)?\s+/(create|change|run|delete)\b",
    r"\b(Register|New|Set|Unregister|Enable|Start)-ScheduledTask\b",
    r"\bsc(\.exe)?\s+(create|config|delete|start|stop|failure)\b",
    r"\b(New|Set|Remove|Start|Stop|Restart)-Service\b",
    r"\breg(\.exe)?\s+(add|delete|import|restore|load)\b",
    r"\b(New|Set|Remove)-ItemProperty\b[^|;&]*\b(HKLM|HKCU|HKEY_)",
    r"\bsetx\b",
    r"\b(netsh\s+(advfirewall|firewall|interface)|Set-ExecutionPolicy|bcdedit|"
    r"New-NetFirewallRule|Set-NetFirewallProfile)\b",
    r"Start Menu[\\/]+Programs[\\/]+Startup|shell:startup",
    r"\bcrontab\b(?!\s+-l\b)",
    r"\bsystemctl\s+(enable|disable|start|stop|restart|mask|daemon-reload|edit)\b",
    r"\b(update-rc\.d|chkconfig|rc-update)\b",
    r"\bat\s+(now|\d{1,2}:\d\d|midnight|noon|tomorrow)\b",
    r"\blaunchctl\s+(load|unload|bootstrap|bootout|submit|enable|kickstart)\b",
    r"/(etc/(cron|systemd|init\.d|profile|sudoers)|Library/Launch(Agents|Daemons))",
    r"(^|[;&|]\s*)(sudo|doas|runas)\s",
)]
_SENDS_DATA = re.compile(
    r"\b(curl|wget)\b[^|;&]*(\s-X\s*(POST|PUT|PATCH|DELETE)\b|\s(-d|--data\S*|"
    r"-F|--form|-T|--upload-file|--post-data|--post-file)\b)", re.I)
# browser ops that act on a site (submit forms, run page JavaScript)


def _browser_submits(a) -> bool:
    """Only SUBMITTING asks first — a submit/send/buy/pay/post/delete/ sign-in
    button (known from the snapshot), Enter, page script, or an old
    selector-based click. Typing into a field or clicking a link doesn't.
    """
    if not isinstance(a, dict):
        return False
    op = a.get("op")
    if op in ("eval", "upload", "dialog", "login"):   # Sends a file out / confirms a dialog / fills a saved login
        return True
    if op == "press":
        return str(a.get("key", "Enter")).lower() in ("enter", "return", "numpadenter")
    if op == "click":
        if not a.get("ref"):
            return True                       # No ref: can't tell what it is
        try:
            from .tools.browser import ref_submits
            return ref_submits(str(a["ref"]).lstrip("@"))
        except Exception:                     # noqa: BLE001
            return True
    return False
_URL_HOST = re.compile(r"https?://([^/:\s'\"]+)", re.I)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")


# run_code. A script's calls to Aura's tools are checked one by one already; a
# script that reaches the OS DIRECTLY — starts programs, opens the network, raw
# system calls, files by absolute path — asks first, like the shell.
_RUN_CODE_RAW = re.compile(
    r"\b(subprocess|os\.(system|popen|exec\w*|spawn\w*|fork|kill|remove|unlink|rmdir|"
    r"chmod|chown|rename|replace|makedirs|mkdir|symlink|link|startfile)|pty|socket|"
    r"urllib|http\.client|httpx|requests|aiohttp|ftplib|smtplib|telnetlib|paramiko|"
    r"ctypes|cffi|winreg|_winapi|shutil|pathlib\.Path\s*\(\s*['\"](/|[A-Za-z]:)|"
    r"__import__|importlib|eval|exec|compile|Path\.home|expanduser|getenv|environ)\b|"
    r"open\s*\(\s*r?['\"](/|[A-Za-z]:[\\/]|~)", re.I)


_TAINT_GATED = frozenset({"run_command", "process_start", "process_input", "run_code",
                          "cronjob", "browser_run", "delegate"})


def run_code_is_g3(code: str) -> bool:
    return bool(_RUN_CODE_RAW.search(code or "")) or shell_is_g3(code or "")


def shell_is_g3(cmd: str) -> bool:
    """True when a shell command reaches outside or can't be undone."""
    if not cmd:
        return False
    if any(p.search(cmd) for p in _G3_SHELL):
        return True
    if _SENDS_DATA.search(cmd):
        hosts = _URL_HOST.findall(cmd)
        # sending to a local dev server is ordinary testing, not outreach
        return not hosts or any(h.lower() not in _LOCAL_HOSTS for h in hosts)
    return False


def grade_of(tool: str, blast_radius: str,
             overrides: dict | None = None, args: dict | None = None,
             tainted: bool = False) -> str:
    """Map one tool call to its autonomy grade. Explicit overrides win;
    then G3 for outside-world verbs and outside-reaching shell commands
    (needs `args`); then the blast-radius default."""
    if overrides and tool in overrides:
        return overrides[tool]
    if tool in _G3_VERBS:
        return "G3"
    # promptware defense: outside text tried to give instructions in this
    # task — from now on every command, script, background process and
    # scheduled job asks first (an attacker can't word around a human)
    if tainted and tool in _TAINT_GATED:
        return "G3"
    if tool == "run_code" and isinstance(args, dict) and run_code_is_g3(str(args.get("code", ""))):
        return "G3"
    if tool == "cronjob" and isinstance(args, dict) and \
            str(args.get("action", "")).lower() in ("create", "resume"):
        return "G3"
    if tool == BROWSER_RUN and isinstance(args, dict) and any(
            _browser_submits(a) for a in (args.get("actions") or [])):
        return "G3"
    if tool == "process_input" and isinstance(args, dict) and             shell_is_g3(str(args.get("text", ""))):
        return "G3"
    if tool in SHELL_TOOLS and isinstance(args, dict):
        cmd = str(args.get("cmd", ""))
        from .protected import shell_writes_protected
        if shell_is_g3(cmd) or shell_writes_protected(cmd):
            return "G3"
    return {"read": "G0", "write": "G1", "execute": "G2"}.get(
        blast_radius, "G2")


# --------------------------------------------------------- approval card

@dataclass
class ApprovalRequest:
    tool: str
    args: dict
    grade: str
    target: str
    intent: str = ""


def approval_card(req: ApprovalRequest) -> str:
    """The REAL action, not a summary. G3 verbs never offer always-approve."""
    lines = [f"APPROVAL — {req.grade} · {req.tool} → {req.target}"]
    if req.args:
        for k, v in list(req.args.items())[:3]:
            lines.append(f"  {k}: {str(v)[:80]}")
    if req.intent:
        lines.append(f"  intent: {req.intent[:80]}")
    lines.append("approve / reject"
                 + ("" if req.grade == "G3"
                    else " / always-approve-this-verb-for-this-target")
                 + (" (always-approve NOT available for G3 — irreversible)"
                    if req.grade == "G3" else ""))
    return "\n".join(lines)


# --------------------------------------------------------- volume budget

def budget_exceeded_card() -> str:
    return ("that's a lot of questions this hour — 'a' saves a permission, "
            "/approvals lists them, /assist L2 asks less")


class AutonomyEngine:
    """The approval volume budget (GUARDRAILS: approvals per run-hour).
    A HINT only: past the cap the approval card says so — nothing is
    refused, so it never stops working work (SETTINGS_DESIGN)."""

    def __init__(self, max_approvals_per_hour: int = 10):
        self.max_approvals = self.max_approvals_per_hour = max_approvals_per_hour
        self._approvals: list[float] = []

    def approval_budget_ok(self) -> bool:
        now = time.time()
        self._approvals = [t for t in self._approvals if now - t < 3600]
        if len(self._approvals) >= self.max_approvals_per_hour:
            return False
        self._approvals.append(now)
        return True


# ---------------------------------------------------- promotion --

def _rules_path(eng) -> Path:
    return Path(eng) / ".aura" / "rules.json"


def load_rules(eng) -> list[dict]:
    p = _rules_path(eng)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


# ------------------------------------------------- grants (BUILD 3) -------
# Oct-13 lesson (the wipe): a one-time session-wide allow-all approval
# silenced every later question and an rm harness deleted user files
# outside its sandbox. Grants therefore name an action TYPE + a TARGET,
# never "everything". G3 (irreversible/external) is NEVER grantable; a
# wildcard execute grant is banned outright.

GRANT_G3_BANNED = ("G3 verbs are irreversible/external — they can never "
                   "be pre-approved by a grant")
GRANT_WILDCARD_G2_BANNED = ("a G2 grant must name a concrete target — "
                            "'*' would be a session-wide allow-all, the "
                            "exact failure that caused the Oct-13 wipe")


def persist_grant(eng, grade: str, tool: str, target: str,
                  scope: str = "project", expires_s: float | None = None,
                  label: str = "") -> dict:
    """Persist a allow-grant: (grade, tool, target). Project-first by
    default; optional expiry; validated against the semantics above."""
    if grade not in ("G0", "G1", "G2"):
        raise ValueError(f"invalid grant grade '{grade}' — {GRANT_G3_BANNED}"
                         if grade == "G3" else f"invalid grade '{grade}'")
    if grade == "G3":
        raise ValueError(GRANT_G3_BANNED)
    target = (target or "").strip()
    if not target:
        raise ValueError("grant target: empty — a grant names its target")
    if target == "*" and grade == "G2":
        raise ValueError(GRANT_WILDCARD_G2_BANNED)
    rules = load_rules(eng)
    rec = {"kind": "grant", "id": f"g{len(rules):04d}-{int(time.time())}",
           "grade": grade, "tool": tool, "target": target,
           "scope": scope if scope == "project" else "global",
           "ts": round(time.time(), 3), "label": label}
    if expires_s:
        rec["expires"] = round(time.time() + float(expires_s), 3)
    rules.append(rec)
    p = _rules_path(eng)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return rec


def revoke_grant(eng, grant_id: str) -> bool:
    """Remove one saved permission by id (operator action)."""
    rules = load_rules(eng)
    keep = [r for r in rules
            if not (r.get("kind") == "grant" and r.get("id") == grant_id)]
    if len(keep) == len(rules):
        return False
    _rules_path(eng).write_text(json.dumps(keep, ensure_ascii=False,
                                           indent=1), encoding="utf-8")
    return True


def list_grants(eng) -> list[dict]:
    return [r for r in load_rules(eng) if _grant_live(r)]


def _grant_live(rule: dict, now: float | None = None) -> bool:
    if rule.get("kind") != "grant":
        return False
    exp = rule.get("expires")
    if exp and (now or time.time()) > float(exp):
        return False
    return True


def grant_target(tool: str, args: dict, spec=None) -> str:
    """What a saved permission names. File tools: the file path (their scope
    target is "?", which would make a grant cover every file). Shell: the exact
    command. Others: the tool's scope target.
    """
    args = args if isinstance(args, dict) else {}
    if isinstance(args.get("path"), str) and args["path"].strip():
        p = args["path"].strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p or "?"
    if isinstance(args.get("cmd"), str) and args["cmd"].strip():
        return " ".join(args["cmd"].split())
    try:
        t = spec.extract_target(args) if spec is not None else None
    except Exception:
        t = None
    return str(t or "?")


def match_grant(eng, grade: str, tool: str, target: str) -> dict | None:
    """Return the live grant satisfying (grade, tool, target), or None.
    Target match: exact, or fnmatch pattern from the grant — a bare '*'
    pattern is only ever valid for G1 (reversible writes, engagement-bound);
    G2 grants always name a concrete target."""
    import fnmatch
    target = (target or "").strip()
    for r in load_rules(eng):
        if not _grant_live(r):
            continue
        if r.get("grade") != grade or r.get("tool") != tool:
            continue
        pat = r.get("target", "")
        if pat == target or (pat != "*" and fnmatch.fnmatch(target, pat)):
            return r
        if pat == "*" and grade == "G1":
            return r
    return None

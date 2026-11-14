"""action_risk.py — what a command does, not which website it names.

  read / download reading or fetching for the work (curl -o, git clone, a
                    package the project already lists): never asks
  run_outside downloading AND running outside code (curl | sh, installing
                    a package the project doesn't list, npx <pkg>): asks once
                    per task
  send sending things out (uploads, POST/PUT, curl -d, scp/rsync to
                    a remote, git push, nc): asks every time
  unclear a command that touches the network in a way the rules
                    can't read (a script with urllib in it): a checker model
                    decides whether the user asked for it
  local no network, no install: the normal assist level decides
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_URL = re.compile(r"https?://[^\s'\"<>|)]+", re.I)
_SEND = [
    r"\bcurl\b[^|;&]*\s(-d\b|--data(?:-\w+)?\b|-F\b|--form\b|-T\b|--upload-file\b)",
    r"\bcurl\b[^|;&]*\s(-X|--request)\s*(POST|PUT|PATCH|DELETE)\b",
    r"\bwget\b[^|;&]*--(post-data|post-file|method=(POST|PUT|PATCH|DELETE))",
    r"\b(Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b[^|;&]*-(Method\s+(Post|Put|Patch|Delete)|Body|InFile)\b",
    r"\b(scp|sftp|ftp)\b",
    r"\brsync\b[^|;&]*\s[\w.-]+@?[\w.-]+:\S",
    r"\bgit\s+push\b",
    r"\b(nc|ncat|netcat)\b\s",
    r"\brequests\.(post|put|patch|delete)\(|urllib\.request\.Request\([^)]*data=|\bhttp\.client\b",
    r"\b(gh|glab)\s+(release\s+upload|gist\s+create|api\b[^|;&]*-X\s*(POST|PUT|PATCH|DELETE))",
    r"\btwine\s+upload\b|\bnpm\s+publish\b|\bcargo\s+publish\b|\bdocker\s+push\b",
]
_PIPE_RUN = re.compile(
    r"(curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|]*\|\s*(sudo\s+)?"
    r"(sh|bash|zsh|python3?|py|node|perl|ruby|iex|Invoke-Expression)\b", re.I)
_INSTALL = re.compile(
    r"\b(?:(?:python3?|py)\s+-m\s+)?(pip3?|uv\s+pip|pipx|uv\s+add|uv\s+tool\s+install|poetry\s+add|"
    r"npm|pnpm|yarn|bun|gem|cargo|go|composer|brew|apt(?:-get)?|winget|choco|scoop)\s+"
    r"(install|add|i|get)\b([^;&|]*)", re.I)
_NPX = re.compile(r"\b(npx|pnpx|bunx|uvx)\s+(?:-y\s+|--yes\s+)?([@\w./-]+)", re.I)
_DOWNLOAD = re.compile(
    r"\b(curl|wget|iwr|Invoke-WebRequest|git\s+clone|git\s+fetch|git\s+pull|pip3?\s+download|"
    r"npm\s+pack|svn\s+checkout)\b", re.I)
_NETWORKY = re.compile(r"urllib|requests\.|http\.client|socket\.|fetch\(|axios|aiohttp|httpx",
                       re.I)


def _declared(work_dir) -> set[str]:
    """The packages the project already lists (lower case)."""
    out: set[str] = set()
    w = Path(work_dir or ".")
    for f in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt"):
        try:
            for ln in (w / f).read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.-]+)", ln)
                if m and not ln.strip().startswith(("#", "-")):
                    out.add(m.group(1).lower())
        except OSError:
            pass
    try:
        txt = (w / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        out |= {m.lower() for m in re.findall(r'"([A-Za-z0-9_.-]+)\s*(?:[<>=!~;\[]|")', txt)}
    except OSError:
        pass
    try:
        pj = json.loads((w / "package.json").read_text(encoding="utf-8"))
        for k in ("dependencies", "devDependencies", "optionalDependencies"):
            out |= {n.lower() for n in (pj.get(k) or {})}
    except (OSError, ValueError):
        pass
    for f, rx in (("Cargo.toml", r'^\s*([A-Za-z0-9_-]+)\s*='), ("go.mod", r"^\s*([\w./-]+)\s+v")):
        try:
            out |= {m.lower() for m in re.findall(rx, (w / f).read_text(encoding="utf-8"), re.M)}
        except OSError:
            pass
    return out


def _packages(args: str) -> list[str]:
    """Package names in an install command's arguments (flags, files and
    -r/-e targets left out)."""
    words = args.split()
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        if w in ("-r", "--requirement", "-e", "--editable", "-c", "--constraint", "-t",
                 "--target", "--index-url", "-i", "--extra-index-url", "--prefix"):
            skip = True
            continue
        if w.startswith("-") or w in (".", "..") or "/" in w or "\\" in w or w.endswith(
                (".txt", ".toml", ".json", ".lock", ".whl", ".tar.gz", ".zip")):
            continue
        name = re.split(r"[<>=!~@\[]", w.strip("'\""))[0].lower()
        if name:
            out.append(name)
    return out


def classify(cmd: str, work_dir=None) -> tuple[str, str]:
    """(kind, what) for a shell command — see the module docstring."""
    c = str(cmd or "")
    for rx in _SEND:
        m = re.search(rx, c, re.I)
        if m:
            return "send", m.group(0)[:80]
    m = _PIPE_RUN.search(c)
    if m:
        return "run_outside", "downloads and runs a script (" + m.group(0)[:60] + ")"
    for m in _INSTALL.finditer(c):
        pkgs = _packages(m.group(3) or "")
        if not pkgs:
            continue                     # Npm install / pip install -r …: the project's own list
        new = [p for p in pkgs if p not in _declared(work_dir)]
        if new:
            return "run_outside", "installs " + ", ".join(new[:4]) + \
                (f" (+{len(new) - 4})" if len(new) > 4 else "")
    m = _NPX.search(c)
    if m and m.group(2).lower().split("@")[0] not in _declared(work_dir):
        return "run_outside", f"downloads and runs {m.group(2)}"
    if _DOWNLOAD.search(c) or _URL.search(c):
        if _URL.search(c) and _NETWORKY.search(c) and not _DOWNLOAD.search(c):
            return "unclear", "a script that reaches the network"
        return "download", ", ".join(sorted({u.split('/')[2] for u in _URL.findall(c)
                                             if u.count('/') >= 2}))[:120] or "the web"
    if _NETWORKY.search(c) and re.search(r"\b(python3?|py|node)\b\s+-(c|e)\b", c):
        return "unclear", "a script that reaches the network"
    return "local", ""


CHECK_PROMPT = (
    "You check one command an AI coding agent wants to run on the user's computer. "
    "Decide only this: did the user ask for work that needs this command, and is it "
    "safe to run without asking them? Running it is fine for ordinary work: reading, "
    "downloading, building, testing. It needs the user's OK when it sends data out of "
    "the computer, runs code downloaded from the internet, deletes or changes things "
    "outside the project, or does something the user didn't ask for. Reply with "
    "exactly ALLOW, or ASK: <one short reason in plain words>.")

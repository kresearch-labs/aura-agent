"""project_checks.py — how do I verify this project?

One shared answer for everything that needs to check work: the proof engine
(roadmap), the referee, and the in-loop test/lint feedback (2.5, 2.6).
Detection only — nothing here runs a command.

Every result carries the evidence it was inferred from, so a caller can show
*why* a command was chosen.

Operator override: `.aura/checks.json` in the engagement, e.g. {"test": "python
-m pytest -q tests/unit", "lint": "ruff check src"} It wins over detection.
`.aura/` is write-protected from the agent (protected.py), so the agent can
never change its own verification command.

Python commands use the interpreter running Aura (sys.executable), never a bare
"python3" — on Windows that name is often the Microsoft Store stub, which
prints "Python was not found" and fails.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("test", "lint", "typecheck")
_NPM_PLACEHOLDER = "no test specified"
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".aura",
              "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}


@dataclass
class Check:
    kind: str                 # Test | lint | typecheck
    cmd: list[str]
    why: str                  # Evidence, e.g. "pyproject.toml [tool.pytest]"
    source: str = "detected"  # Detected | operator
    needs: list[str] = field(default_factory=list)   # Tools that must exist

    def available(self) -> bool:
        """True when every program it needs is installed (best effort)."""
        return all(_installed(n) for n in self.needs)

    def display(self) -> str:
        """Exactly what runs (POSIX quoting shown on Windows was misleading)."""
        return self.command_line()

    def command_line(self) -> str:
        """A string the platform's shell runs correctly: run_command uses
        shell=True, i.e. cmd.exe on Windows, which does not understand
        POSIX single-quote quoting."""
        if sys.platform.startswith("win"):
            import subprocess
            return subprocess.list2cmdline(self.cmd)
        return shlex.join(self.cmd)


def _installed(name: str) -> bool:
    if name.startswith("py:"):                       # A Python module
        import importlib.util
        return importlib.util.find_spec(name[3:]) is not None
    return shutil.which(name) is not None


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has_python_tests(root: Path, limit: int = 4000) -> str | None:
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return "tests/ directory"
    seen = 0
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        seen += 1
        if seen > limit:
            break
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            return f"test file {p.relative_to(root).as_posix()}"
    return None


def _python(root: Path) -> list[Check]:
    out: list[Check] = []
    py = sys.executable or "python"
    pyproject = _read(root / "pyproject.toml")
    setup_cfg = _read(root / "setup.cfg")
    tox = _read(root / "tox.ini")
    if not (pyproject or setup_cfg or tox or (root / "setup.py").exists()
            or (root / "requirements.txt").exists()
            or _has_python_tests(root)):
        return out
    why = ("pytest.ini" if (root / "pytest.ini").exists()
           else "pyproject.toml [tool.pytest]" if "[tool.pytest" in pyproject
           else "setup.cfg [tool:pytest]" if "[tool:pytest]" in setup_cfg
           else "tox.ini [pytest]" if "[pytest]" in tox
           else "conftest.py" if (root / "conftest.py").exists() else None)
    tests = _has_python_tests(root)
    if why or tests:
        out.append(Check("test", [py, "-m", "pytest", "-q"], why or tests,
                         needs=["py:pytest"]))
    if "[tool.ruff" in pyproject or (root / "ruff.toml").exists() \
            or (root / ".ruff.toml").exists():
        out.append(Check("lint", ["ruff", "check", "."], "ruff config",
                         needs=["ruff"]))
    elif (root / ".flake8").exists() or "[flake8]" in setup_cfg:
        out.append(Check("lint", [py, "-m", "flake8"], "flake8 config",
                         needs=["py:flake8"]))
    if "[tool.mypy" in pyproject or (root / "mypy.ini").exists() \
            or "[mypy" in setup_cfg:
        out.append(Check("typecheck", [py, "-m", "mypy", "."], "mypy config",
                         needs=["py:mypy"]))
    return out


def _node(root: Path) -> list[Check]:
    pkg = root / "package.json"
    if not pkg.exists():
        return []
    try:
        scripts = (json.loads(_read(pkg)) or {}).get("scripts") or {}
    except json.JSONDecodeError:
        return []
    out = []
    test = str(scripts.get("test", ""))
    if test and _NPM_PLACEHOLDER not in test:
        out.append(Check("test", ["npm", "test"], "package.json scripts.test",
                         needs=["npm"]))
    if scripts.get("lint"):
        out.append(Check("lint", ["npm", "run", "lint"],
                         "package.json scripts.lint", needs=["npm"]))
    for name in ("typecheck", "type-check", "tsc"):
        if scripts.get(name):
            out.append(Check("typecheck", ["npm", "run", name],
                             f"package.json scripts.{name}", needs=["npm"]))
            break
    return out


def _rust(root: Path) -> list[Check]:
    if not (root / "Cargo.toml").exists():
        return []
    return [Check("test", ["cargo", "test"], "Cargo.toml", needs=["cargo"]),
            Check("lint", ["cargo", "clippy"], "Cargo.toml", needs=["cargo"])]


def _go(root: Path) -> list[Check]:
    if not (root / "go.mod").exists():
        return []
    return [Check("test", ["go", "test", "./..."], "go.mod", needs=["go"]),
            Check("lint", ["go", "vet", "./..."], "go.mod", needs=["go"])]


def _make(root: Path) -> list[Check]:
    mk = _read(root / "Makefile")
    out = []
    for kind in ("test", "lint"):
        if re.search(rf"^{kind}\s*:", mk, re.M):
            out.append(Check(kind, ["make", kind], f"Makefile '{kind}' target",
                             needs=["make"]))
    return out


def _operator(root: Path) -> list[Check]:
    p = root / ".aura" / "checks.json"
    if not p.exists():
        return []
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError:
        return []
    out = []
    for kind in KINDS:
        cmd = data.get(kind)
        if isinstance(cmd, str) and cmd.strip():
            out.append(Check(kind, shlex.split(cmd), ".aura/checks.json",
                             source="operator"))
    return out


def detect(root: Path) -> list[Check]:
    """All checks for the project, at most one per kind: operator override
    first, then the language's own tooling, then Makefile targets."""
    root = Path(root)
    chosen: dict[str, Check] = {}
    for found in (_operator(root), _python(root), _node(root), _rust(root),
                  _go(root), _make(root)):
        for c in found:
            chosen.setdefault(c.kind, c)
    return [chosen[k] for k in KINDS if k in chosen]


def check_for(root: Path, kind: str = "test") -> Check | None:
    return next((c for c in detect(root) if c.kind == kind), None)


def _python_name() -> str:
    """The Python command that works in this machine's shell: `python3` or
    `python` — never the Windows Store stub (it only prints "Python was not
    found") — else the interpreter running Aura, by its full path."""
    for name in ("python3", "python"):
        p = shutil.which(name)
        if p and "windowsapps" not in p.lower():
            return name
    return f'"{sys.executable}"' if " " in sys.executable else sys.executable


def environment_line(root: Path) -> str:
    """."""
    import platform
    system = platform.system()
    if system == "Darwin":
        osname = "macOS"
    elif system == "Linux" and "microsoft" in platform.release().lower():
        osname = "Linux (WSL)"
    else:
        osname = f"{system} {platform.release()}".strip()
    shell = "cmd.exe" if sys.platform.startswith("win") else "sh"
    parts = [f"{osname}; commands run in {shell}", f"Python: {_python_name()}"]
    test = check_for(Path(root), "test")
    if test is not None:
        parts.append(f"tests: {test.display()}")
    return "[ENVIRONMENT] " + " · ".join(parts)

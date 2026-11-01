"""deps_hint.py — a missing dependency → the right install command, offered, never
run.

So: the project's own dependency list first, the right package name for
well-known modules, a look-alike warning for unknown names, and a shadowing
check.
"""
from __future__ import annotations

import re
from pathlib import Path

# import name → install name (the common mismatches)
PY_NAMES = {
    "PIL": "Pillow", "cv2": "opencv-python", "sklearn": "scikit-learn", "yaml": "PyYAML",
    "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
    "jwt": "PyJWT", "Crypto": "pycryptodome", "OpenSSL": "pyOpenSSL", "magic": "python-magic",
    "docx": "python-docx", "pptx": "python-pptx", "fitz": "PyMuPDF", "serial": "pyserial",
    "usb": "pyusb", "git": "GitPython", "google.protobuf": "protobuf", "attr": "attrs",
    "lxml": "lxml", "win32api": "pywin32", "win32com": "pywin32", "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary", "skimage": "scikit-image", "Levenshtein": "python-Levenshtein",
}

_PY = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'")
_NODE = re.compile(r"Cannot find module '([^'./][^']*)'")


def _listed(work: Path, name: str, files: tuple[str, ...]) -> str | None:
    """The dependency file that lists `name`, if any."""
    low = name.lower().replace("_", "-")
    for f in files:
        p = work / f
        try:
            if p.is_file() and re.search(rf"(?<![\w-]){re.escape(low)}(?![\w-])",
                                         p.read_text(encoding="utf-8", errors="replace").lower().replace("_", "-")):
                return f
        except OSError:
            continue
    return None


_COMMON = ("requests", "numpy", "pandas", "flask", "django", "fastapi", "pytest", "httpx",
           "pydantic", "sqlalchemy", "matplotlib", "scipy", "torch", "boto3", "click", "rich",
           "jinja2", "aiohttp", "redis", "celery", "uvicorn", "openai", "anthropic")


def _near(work: Path, name: str) -> str | None:
    """A close spelling among the project's listed dependencies and common
    packages (not the name itself)."""
    import difflib
    names = set(_COMMON) | {v.lower() for v in PY_NAMES.values()} | set(PY_NAMES)
    for f in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        try:
            names |= set(re.findall(r"^\s*\"?([A-Za-z][\w.-]+)", (work / f).read_text(
                encoding="utf-8", errors="replace"), re.M))
        except OSError:
            continue
    names.discard(name)
    got = difflib.get_close_matches(name, sorted(names), n=1, cutoff=0.8)
    return got[0] if got else None


def hint(output: str, work_dir) -> str | None:
    """The note for a failed command's output, or None."""
    work = Path(work_dir)
    m = _PY.search(output or "")
    if m:
        mod = m.group(1)
        top = mod.split(".")[0]
        if (work / f"{top}.py").is_file() or (work / top / "__init__.py").is_file():
            return (f"[MISSING DEPENDENCY] '{mod}' looks missing, but the project has its own "
                    f"{top}.py / {top}/ — it may shadow the real package, or the command ran "
                    "from the wrong folder. Check that before installing anything.")
        pkg = PY_NAMES.get(mod) or PY_NAMES.get(top) or top
        where = _listed(work, pkg, ("requirements.txt", "requirements-dev.txt", "pyproject.toml",
                                    "setup.cfg", "Pipfile"))
        if where:
            cmd = ("python -m pip install -r " + where) if where.startswith("requirements") \
                else "python -m pip install -e ."
            return (f"[MISSING DEPENDENCY] '{mod}' is not installed, but {where} lists it — the "
                    f"project's dependencies aren't installed here. Offer the user: `{cmd}` "
                    "(in the project's virtual environment). Don't install without their OK.")
        known = mod in PY_NAMES or top in PY_NAMES
        near = _near(work, top)
        if near:
            return (f"[MISSING DEPENDENCY] '{mod}' is not installed — did you mean '{near}'? "
                    "A typo in the import is likelier than a missing package; don't install "
                    f"'{top}' (a look-alike name can be a malicious package).")
        return (f"[MISSING DEPENDENCY] '{mod}' is not installed. The package is `{pkg}`"
                + (f" (imported as {top})" if pkg != top else "")
                + f": offer the user `python -m pip install {pkg}` — it installs into the same "
                "Python that ran the command. "
                + ("" if known else "Check the name on pypi.org first: a mistyped or look-alike "
                   "package can be malicious. ")
                + "Don't install without their OK.")
    m = _NODE.search(output or "")
    if m:
        mod = m.group(1)
        pkg = "/".join(mod.split("/")[:2]) if mod.startswith("@") else mod.split("/")[0]
        if _listed(work, pkg, ("package.json",)):
            return (f"[MISSING DEPENDENCY] '{pkg}' is in package.json but not installed — offer "
                    "the user `npm install` (or the project's own package manager). Don't "
                    "install without their OK.")
        return (f"[MISSING DEPENDENCY] '{pkg}' is not installed: offer the user `npm install {pkg}` "
                "— check the name on npmjs.com first (look-alike packages can be malicious). "
                "Don't install without their OK.")
    return None

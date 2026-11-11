"""Foreign-pipeline hardening — final wiring.

Fixes applied (TDD against tests/test_foreign_pipeline.py):
        a silent global safe.directory '*')
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from ..eventlog import log_swallowed


def _git(repo: str | Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", repr(e)


def git_diff_checked(repo: str | Path) -> tuple[str, str]:
    """Layer-2: exit-code checked diff INCLUDING staged (diff HEAD) and untracked
    files. Returns (patch, error).

    "dubious ownership" (the folder belongs to another user.
    """
    code, out, err = _git(repo, "diff", "HEAD")
    if code != 0 and not out.strip():
        err = (err or "").strip() or f"git exit {code}"
        if "dubious ownership" in err.lower():
            here = Path(repo).resolve().as_posix()
            return "", ("git refuses this folder (dubious ownership: it "
                        "belongs to another user). To trust this folder "
                        "only, run: git config --global --add "
                        f"safe.directory \"{here}\"")
    if code != 0 and not out.strip():
        # zero-commit repo: diff HEAD fails by design; bare diff next
        code2, out2, err2 = _git(repo, "diff")
        if code2 == 0:
            out, code = out2, 0
        elif not out2.strip():
            return "", ((err2 or "").strip() or (err or "").strip()
                        or f"git exit {code2}")
        else:
            out = out2
    parts = [out] if out.strip() else []
    if code == 0:
        c, others, _ = _git(repo, "ls-files", "--others",
                            "--exclude-standard")
        if c == 0 and others.strip():
            for rel in others.strip().splitlines():
                if not rel.strip():
                    continue
                cc, blob, _ = _git(repo, "diff", "--no-index", "--",
                                   os.devnull, rel)
                # exit 1 = files differ (expected); content still on stdout
                if blob.strip():
                    parts.append(blob if blob.lstrip().startswith("diff")
                                 else "")
    return "\n".join(p for p in parts if p), ""


# ---------------------------------------------------------------- order
def extract_diff_block(text: str) -> str:
    """FP-1: pull the LAST ```diff ...``` fenced block out of a session
    transcript and return appliable diff content. Transcripts are JSONL —
    newlines inside the JSON string are ESCAPED (\\n), so we unescape
    (parse each line as JSON when possible) BEFORE matching fences."""
    candidates = [text]
    # JSONL: parse each line, collect string fields' content
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, str):
                    # JSON-escaped text may still hold literal \\n
                    cand = v.encode().decode("unicode_escape") \
                        if "\\n" in v and "\n" not in v else v
                    blocks = re.findall(r"```diff\s*\n(.*?)```",
                                        cand, flags=re.DOTALL)
                    if blocks:
                        return blocks[-1].rstrip() + "\n"
    blocks = re.findall(r"```diff\s*\n(.*?)```", text, flags=re.DOTALL)
    if blocks:
        return blocks[-1].rstrip() + "\n"
    # escaped-fence fallback for non-JSON text
    m = re.search(r"```diff\\n(.*?)```", text, flags=re.DOTALL)
    if m:
        return m.group(1).replace("\\n", "\n").rstrip() + "\n"
    return ""


# ---------------------------------------------------------------- salvage
def _is_git_repo(repo: str | Path) -> bool:
    """Cheap repo probe: outside a repo, git diff produces noisy usage
    text on stderr that must not leak into salvage files or error cards."""
    code, out, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def flush_partial_diff(repo: str | Path, out_path: str | Path) -> tuple[bool, str]:
    """On timeout/kill, flush a best-effort diff to a KNOWN file path."""
    if not _is_git_repo(repo):
        # not a repo (or .git wiped): say so cleanly — no git usage dump
        msg = "not a git repository — nothing to diff"
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(
                f"# aura: no diff recoverable\n# error: {msg}\n",
                encoding="utf-8")
        except Exception as _swallowed:
            log_swallowed("session_location.diff_fallback", _swallowed)
        return False, msg
    patch, err = git_diff_checked(repo)
    ok = bool(patch.strip())
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if ok:
            Path(out_path).write_text(patch, encoding="utf-8")
        else:
            Path(out_path).write_text(
                f"# aura: no diff recoverable\n# error: {err or 'none'}\n",
                encoding="utf-8")
        return ok, err
    except Exception as e:
        return False, repr(e)

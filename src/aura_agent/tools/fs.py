"""Filesystem tools: list/read/write/edit, engagement-root-jailed.

Every path argument is resolved and verified to stay inside the engagement root
BEFORE any I/O (path-escape guard — the LLM never gets to wander the
filesystem). write_file auto-creates parents. edit is exact-match
search/replace.

All tools go through the mediator like every other call; blast radius WRITE,
extract_target=None (local FS is scope-exempt by design).
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import time
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec, Violation
from ..eventlog import log_swallowed
from ..tool_names import EDIT_FILE, LIST_FILES, READ_FILE, WRITE_FILE

MAX_READ_BYTES = 256_000  # read_file hard cap; bigger → head+tail + artifact hint


# ---------------------------------------------------------------- helpers ---
def _check_locked(mediator, root: Path, p: Path) -> None:
    """State files are writable only through the state/finding tools; files that
    steer or record the agent (protected.py) are not writable through
    write_file / edit_file at all. Only when Aura's notes share the folder: in
    a project workspace these names are the user's own files (a `skills/` or
    `commands/` folder in their code).
    """
    if getattr(mediator, "same_folder", True) is False:
        return
    try:
        rel = p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return
    from ..protected import protected_reason
    why = protected_reason(rel)
    if why:
        raise Violation(f"'{rel}' is protected: {why}")
    locks = getattr(mediator, "state_file_locks", None)
    if locks and rel in locks:
        raise Violation(
            f"'{rel}' is a state file — write it via state/finding tools only")


# folders outside the project the user allowed (outside.py), per project
# root — the mediator's set, so an approval counts for the next call at once
ALLOWED: dict[Path, set] = {}
# single files (or a drive's own listing) the user allowed where the folder
# is too wide to allow (a drive root, the home folder) — exactly these paths
EXACT: dict[Path, set] = {}


def _resolve_in_root(root: Path, rel: str) -> Path:
    """Resolve rel against root and refuse any escape, except into a folder
    the user allowed (outside.py). Returns absolute path."""
    if not isinstance(rel, str) or not rel.strip():
        raise Violation("path: empty or non-string path argument")
    p = (root / os.path.expanduser(rel)).resolve()
    root_r = root.resolve()
    if p != root_r and root_r not in p.parents:
        from ..outside import inside, never_reason
        if p in EXACT.get(root_r, ()):
            return p                    # Exactly this file was given (a big paste kept aside)
        why = never_reason(p)
        if why:
            raise Violation(f"path: '{rel}' can't be used — {why}")
        if not inside(p, ALLOWED.get(root_r, ())) and p not in EXACT.get(root_r, ()):
            raise Violation(f"path: '{rel}' is outside the project folder and wasn't "
                            "allowed — Aura asks the user when a tool reaches it; if they "
                            "said no, don't retry: continue another way")
    return p


def _base(root: Path, p: Path) -> Path:
    """The folder paths are shown relative to: the project, or the allowed
    outside folder that holds p."""
    root_r = root.resolve()
    if p == root_r or root_r in p.parents:
        return root
    for f in ALLOWED.get(root_r, ()):
        f = Path(f).resolve()
        if p == f or f in p.parents:
            return f
    if p in EXACT.get(root_r, ()):                 # A file / a drive allowed exactly
        return p if p.is_dir() else p.parent
    return root


def _missing(root: Path, rel: str, what: str = "file") -> FileNotFoundError:
    """A wrong path names the real ones (symbols.check_path): models invent
    paths in the project far more often than anything else, so point them
    at what exists instead of a bare 'no such file'."""
    msg = f"no such {what}: {rel}"
    try:
        from ..symbols import SymbolTable
        sug = SymbolTable(root).check_path(rel).get("suggestions") or []
        if sug:
            msg += " — did you mean: " + ", ".join(sug) + "?"
    except Exception as _swallowed:
        log_swallowed("fs.path_hint", _swallowed)
    return FileNotFoundError(msg)


def _snapshot(root: Path, path: Path, shadow_dir: Path) -> str | None:
    """Shadow-git: copy current file content into .shadow/<hash>/ before a
    write/edit. Returns snapshot id (hash) or None if file didn't exist."""
    if not path.exists():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    dest = shadow_dir / digest
    if not dest.exists():  # Dedupe identical snapshots
        dest.mkdir(parents=True)
        shutil.copy2(path, dest / path.name)
        (dest / "meta.json").write_text(json.dumps({
            "file": (str(path.relative_to(root)) if root.resolve() in path.resolve().parents
                     else str(path.resolve())),
            "ts": round(time.time(), 3),
            "sha": digest,
        }))
    return digest


# ---------------------------------------------------------------- the tools --
def list_files(root: Path, args: dict) -> list[str]:
    sub = _resolve_in_root(root, args.get("path", "."))
    if not sub.exists():
        raise _missing(root, args.get("path", "."), "path")
    exact = sub in EXACT.get(root.resolve(), ())
    root = _base(root, sub)
    out = []
    if exact and sub.is_dir():
        # a drive or the home folder allowed for one look: its own entries
        # only — walking a whole drive would take minutes
        for p in sorted(sub.iterdir(), key=lambda x: x.name.lower())[:500]:
            try:
                out.append(f"{p.name}/" if p.is_dir() else p.name)
            except OSError:
                continue
        return out or [f"({args.get('path', '.')} has no files or folders)"]
    # dependency / build / tool folders flood the list (a node_modules can
    # fill all 500 entries): skipped like search_files skips them, unless
    # the listing starts inside one
    from .search import SKIP_DIRS
    inside = set(sub.relative_to(root.resolve()).parts) if sub != root.resolve() else set()
    skipped: set[str] = set()
    for p in sorted(sub.rglob("*")):
        hit = [x for x in p.relative_to(sub).parts if x in SKIP_DIRS and x not in inside]
        if hit:
            if p.is_dir() and p.name in SKIP_DIRS:
                skipped.add(p.relative_to(root).as_posix())
            continue
        rel = p.relative_to(root)
        out.append(f"{rel}/" if p.is_dir() else str(rel))
        if len(out) >= 500:
            out.append("... (truncated at 500 entries)")
            break
    if skipped:
        out.append(f"(not listed: {', '.join(sorted(skipped)[:8])}"
                   + (" …" if len(skipped) > 8 else "") + " — list one by its path to see inside)")
    if not out:
        # fix list #2: say what an empty answer means, so "blank" is a fact
        # about THIS folder, not a guess about the project
        out.append(f"({args.get('path', '.') or '.'} has no files or folders)")
    return out


def read_file(root: Path, args: dict, store: Path | None = None,
              seen: dict | None = None) -> str:
    p = _resolve_in_root(root, args["path"])
    # Big tool outputs are saved in the notebook's artifact store, outside the
    # project; their notice says "artifacts/<id>/<name>" — open those there
    # (read-only, jailed to the store), like any other file
    _rel = str(args["path"]).replace("\\", "/").lstrip("./")
    if store is not None and _rel.startswith("artifacts/") and not p.exists():
        _art = _resolve_in_root(Path(store), _rel)
        if (Path(store).resolve() / "artifacts") in _art.parents:
            p = _art
    # Never-read gate: known secret files never enter context
    try:
        from ..secrets_guard import is_never_read
        if is_never_read(str(p)) and str(
                args.get("force", "")).lower() not in ("1", "true", "yes"):
            return (f"[SECRETS GUARD] '{p.name}' is on the never-read list "
                    f"(secret material stays out of context). Pass "
                    f"force=true if you truly need it.")
    except Exception as _swallowed:
        # fail closed: without a working guard, refuse unless forced
        log_swallowed("fs.never_read_guard", _swallowed)
        if str(args.get("force", "")).lower() not in ("1", "true", "yes"):
            return (f"[SECRETS GUARD] could not check '{p.name}' against the "
                    f"never-read list ({type(_swallowed).__name__}); read "
                    f"refused. Pass force=true to read it anyway.")
    # Full-read bypass: operator override ("read the whole file") skips the
    # size cap — MAX_READ_BYTES stays the DEFAULT, not a law.
    full = str(args.get("full", "")).lower() in ("1", "true", "yes")
    if p.is_dir():
        raise IsADirectoryError(f"{args['path']} is a folder — use list_files")
    if not p.is_file():
        raise _missing(root, args["path"])
    data = p.read_bytes() if full else p.read_bytes()[:MAX_READ_BYTES]
    text = data.decode("utf-8", errors="replace")
    # Scrub pass: secrets that ride inside legitimate reads are redacted before
    # entering the session tape
    try:
        from ..secrets_guard import scrub_secrets
        text = scrub_secrets(text)
    except Exception as _swallowed:
        log_swallowed("fs.secrets_scrub", _swallowed)
        from ..secrets_guard import withheld
        return withheld(f"read_file {p.name}", _swallowed)   # Fail closed
    # the stale-read guard compares the next edit against this (full text,
    # LF line ends — what edit_file matches on)
    if seen is not None and p.stat().st_size <= 4_000_000:
        try:
            with open(p, encoding="utf-8", errors="replace", newline="") as _f:
                seen[str(p.resolve())] = _f.read().replace("\r\n", "\n")
        except OSError:
            pass
    try:
        first = max(1, int(args.get("offset") or 1))
        count = max(1, int(args.get("limit") or 2000))
    except (TypeError, ValueError):
        first, count = 1, 2000
    sel = text.splitlines()[first - 1: first - 1 + count]
    if line_tags_on():
        return "\n".join(f"{i + first}:{line_tag(ln)}|{ln}" for i, ln in enumerate(sel))
    return "\n".join(f"{i + first}|{ln}" for i, ln in enumerate(sel))


def _readback_verify(p: Path, expect_bytes: int | None = None,
                     contains: str | None = None,
                     absent: str | None = None) -> str:
    """BUILD 1 — read-after-write verification (the Gemini CLI lesson:
    a failed premise acted on unverified state = permanent data loss).
    Re-reads the file FROM DISK after the mutation and checks the expected
    effect actually landed. Returns a receipt line; the caller must treat
    a VERIFY-FAIL as a hard stop, never continue on it."""
    try:
        if not p.exists():
            return "VERIFY-FAIL: path missing after write"
        data = p.read_bytes()
        if expect_bytes is not None and len(data) != expect_bytes:
            return (f"VERIFY-FAIL: expected {expect_bytes} bytes on disk, "
                    f"read back {len(data)}")
        text = data.decode("utf-8", errors="replace")
        if contains is not None and contains not in text:
            return "VERIFY-FAIL: expected new content not present on disk"
        if absent is not None and absent in text:
            return "VERIFY-FAIL: replaced content still present on disk"
        import hashlib as _h
        # the bytes landed — that says nothing about whether the code
        # works, so the wording must not invite a "verified" claim
        # (eval: a 4B model read "verified" and reported a broken file
        # as done)
        return (f"on disk as written ({len(data)} bytes, "
                f"sha{_h.sha256(data).hexdigest()[:12]}) — not run or "
                f"checked yet")
    except Exception as e:  # Verification must never be silently skipped
        return f"VERIFY-FAIL: verification error {type(e).__name__}: {e}"


def syntax_note(p: Path) -> str:
    """The write stands (it may be one step of several); the note tells the model
    to fix it now instead of finding out at test time.
    """
    suffix = p.suffix.lower()
    if suffix not in (".py", ".pyi", ".json", ".toml"):
        return ""
    try:
        from ..features import enabled
        if not enabled("syntax_note"):          # 2.2: features
            return ""
    except Exception:                       # noqa: BLE001
        pass
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        if suffix in (".py", ".pyi"):
            compile(text, str(p.name), "exec", dont_inherit=True)
        elif suffix == ".json":
            json.loads(text)
        else:
            import tomllib
            tomllib.loads(text)
    except SyntaxError as e:
        where = f"line {e.lineno}" + (f", col {e.offset}" if e.offset else "")
        bad = (e.text or "").rstrip()
        return (f"\n[SYNTAX ERROR] {p.name} {where}: {e.msg}"
                + (f" → {bad.strip()[:120]}" if bad else "")
                + " — the file was written but does not parse; fix it now")
    except ValueError as e:                 # JSONDecodeError, TOMLDecodeError
        return (f"\n[SYNTAX ERROR] {p.name}: {e} — the file was written but "
                "does not parse; fix it now")
    except Exception:                       # noqa: BLE001 — a check, not a gate
        return ""
    return ""


def parse_problem(p: Path) -> str | None:
    """Why a Python/JSON/TOML file does not parse ("line 2: unmatched
    '}'"), or None. Always checks — unlike syntax_note it is not a
    /features switch: Aura uses it to keep its own words true (a result
    resting on a file that does not parse is never called proven)."""
    suffix = p.suffix.lower()
    if suffix not in (".py", ".pyi", ".json", ".toml") or not p.is_file():
        return None
    try:
        # utf-8-sig: a file saved with a BOM (common on Windows) is valid —
        # Python itself reads it that way
        text = p.read_text(encoding="utf-8-sig")
        if suffix in (".py", ".pyi"):
            compile(text, str(p.name), "exec", dont_inherit=True)
        elif suffix == ".json":
            json.loads(text)
        else:
            import tomllib
            tomllib.loads(text)
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except ValueError as e:                 # JSONDecodeError, TOMLDecodeError
        return str(e)
    except Exception:                       # noqa: BLE001 — unreadable: no claim
        return None
    return None


def _own_code(root: Path, p: Path) -> str | None:
    """The path inside Aura, when the project IS the Aura that is running
    right now (Aura developing Aura) and the file is part of its code —
    else None. A clone of Aura that isn't the running one is just a project."""
    try:
        from ..selfrepair import _live_package_root
        live = _live_package_root()
        if live is None or Path(root).resolve() != live:
            return None
        rel = p.resolve().relative_to(live).as_posix()
        return rel if rel.startswith(("src/aura_agent/", "scripts/")) else None
    except (ValueError, OSError, ImportError):
        return None


def _through_gate(root: Path, shadow: Path, rel: str, content: str, mediator) -> str:
    """Its own checkers (mediator, validator, selftest) and a new tool need the
    user's yes, asked like any gated call.
    """
    from ..selfrepair import SelfRepair, is_tcb
    approved = False
    new_tool = rel.startswith("src/aura_agent/tools/") and not (Path(root) / rel).exists()
    asker = getattr(mediator, "approver", None) if mediator is not None else None
    if (is_tcb(rel) or new_tool) and asker is not None:
        from ..approvals import ask_operator
        approved, _note = ask_operator(
            getattr(mediator, "root", root), asker, "change_own_code",
            {"path": rel}, "G3", rel,
            ("change one of the files that check Aura itself" if is_tcb(rel)
             else "add a new tool to Aura"), allow_always=False)
    assist = str(getattr(mediator, "assist", "L2") or "L2")
    r = SelfRepair(root, assist=assist, store=Path(shadow).parent / ".selfrepair").apply_change(
        rel, content, human_approved=approved)
    if r.get("refused"):
        raise Violation(f"{rel} is part of the Aura that is running: {r.get('why')}")
    if not r.get("ok"):
        failed = r.get("failed") or r.get("slice") or []
        raise ValueError(
            f"{rel}: the change broke Aura's own checks ({', '.join(failed)})"
            " — tried on a copy, nothing was changed. Fix the change and try again.")
    return f" [Aura's own code: tried on a copy, checks passed: {', '.join(r.get('slice') or [])}]"


def write_file(root: Path, shadow: Path, args: dict, mediator=None,
               seen: dict | None = None) -> str:
    p = _resolve_in_root(root, args["path"])
    if mediator is not None:
        _check_locked(mediator, root, p)
    snap = _snapshot(root, p, shadow)
    p.parent.mkdir(parents=True, exist_ok=True)
    own = _own_code(root, p)
    gate = ""
    if own:
        gate = _through_gate(root, shadow, own, args["content"], mediator)
    else:
        # newline="": write exactly what was asked - text mode on Windows would
        # turn every LF into CRLF and the byte-count read-back would fail
        p.write_text(args["content"], encoding="utf-8", newline="")
    verdict = _readback_verify(p, expect_bytes=len(args["content"].encode("utf-8")))
    if verdict.startswith("VERIFY-FAIL"):
        raise Violation(f"write_file {args['path']}: {verdict}")
    if seen is not None:
        seen[str(p.resolve())] = args["content"].replace("\r\n", "\n")
    return (f"wrote {args['path']} ({len(args['content'])} bytes)"
            + (f" [shadow:{snap}]" if snap else " [new file]")
            + gate + f" | {verdict}" + syntax_note(p))


def line_tag(line: str) -> str:
    """Two hex characters from the line's content."""
    import zlib
    return format(zlib.crc32(line.encode("utf-8", "replace")) & 0xFF, "02x")


def line_tags_on() -> bool:
    """Line tags in read_file. The option edit.line_tags (AURA_LINE_TAGS
    overrides): on / off, or auto = what was measured for the model in use
    (aura eval model) — tags when not measured.
    """
    try:
        from ..options import get
        v = get("edit.line_tags")
        if v in ("on", "off"):
            return v == "on"
        from ..model_facts import fact
        return fact("edit_form") != "plain"
    except Exception:                                # noqa: BLE001
        return os.environ.get("AURA_LINE_TAGS", "1").strip().lower() not in ("0", "off", "false", "no")


_PREFIX = re.compile(r"^\s*\d+(?::[0-9a-f]{2})?\|")


def _strip_copied_prefixes(s: str) -> str:
    """Models often copy read_file's `12|` / `12:a3|` prefixes into
    old_string. When every non-empty line has one, they're removed."""
    lines = s.split("\n")
    body = [ln for ln in lines if ln.strip()]
    if body and all(_PREFIX.match(ln) for ln in body):
        return "\n".join(_PREFIX.sub("", ln, count=1) if ln.strip() else ln
                         for ln in lines)
    return s


def _norm_block(s: str) -> str:
    return "\n".join(" ".join(ln.split()) for ln in s.strip("\n").split("\n"))


def _reindent(new: str, old_block: list[str], found_block: list[str]) -> str:
    """The match was found at another indentation."""
    def ind(ln: str) -> str:
        return ln[:len(ln) - len(ln.lstrip())]
    pairs = {ind(o): ind(f) for o, f in zip(old_block, found_block) if o.strip()}
    if all(k == v for k, v in pairs.items()):
        return new
    ratios = {len(v) / len(k) for k, v in pairs.items() if k and v and k.strip(" ") == ""}
    ratio = ratios.pop() if len(ratios) == 1 else None
    out = []
    for ln in new.split("\n"):
        i0 = ind(ln)
        if ln.strip():
            if i0 in pairs:
                ln = pairs[i0] + ln[len(i0):]
            elif ratio and i0.strip(" ") == "":
                ln = " " * round(len(i0) * ratio) + ln[len(i0):]
        out.append(ln)
    return "\n".join(out)


def _windows(lines: list[str], n: int):
    for i in range(len(lines) - n + 1):
        yield i, lines[i:i + n]


def _locate(text: str, old: str) -> tuple[int, int, str, str]:
    """Find old in text. Returns (start_line, end_line, matched_text, how);
    raises ValueError with what was found instead. Never guesses between
    two places."""
    lines = text.split("\n")
    old_lines = old.strip("\n").split("\n")
    n = len(old_lines)
    # 1. whitespace-tolerant (indentation and runs of spaces)
    target = _norm_block(old)
    hits = [i for i, w in _windows(lines, n) if _norm_block("\n".join(w)) == target]
    if len(hits) == 1:
        i = hits[0]
        return i, i + n, "\n".join(lines[i:i + n]), "whitespace differed"
    if len(hits) > 1:
        where = ", ".join(str(i + 1) for i in hits[:6])
        raise ValueError(f"old_string matches {len(hits)} places (ignoring "
                         f"whitespace) at lines {where} — add more context, or "
                         f"point at the lines: {_anchor_hint()}")
    # 2. close match: only one clearly best block
    import difflib
    scored = sorted(((difflib.SequenceMatcher(None, _norm_block("\n".join(w)), target).ratio(), i)
                     for i, w in _windows(lines, n)), reverse=True)
    if scored and scored[0][0] >= 0.92 and (len(scored) == 1 or scored[1][0] < 0.85):
        i = scored[0][1]
        return i, i + n, "\n".join(lines[i:i + n]), f"close match ({scored[0][0]:.0%} alike)"
    if scored:
        i = scored[0][1]
        shown = "\n".join(f"{j + 1}:{line_tag(lines[j])}|{lines[j]}"
                          for j in range(i, min(i + n, len(lines))))
        raise ValueError("old_string not found. Closest text "
                         f"({scored[0][0]:.0%} alike), current lines:\n{shown[:1200]}\n"
                         f"Copy the text exactly, or point at the lines: {_anchor_hint()}")
    raise ValueError("old_string not found (the file is shorter than old_string)")


def _anchor_hint() -> str:
    # Say the form read_file is showing now: plain lines have no tags
    if line_tags_on():
        return "lines='N:tag' as read_file shows it (e.g. 12:a3), or old_string"
    return "lines='N' with the line number read_file shows (e.g. 12), or old_string"


def _parse_anchor(spec: str, lines: list[str]) -> int:
    """'12:a3' → index 11, checked against the current line's tag."""
    m = re.fullmatch(r"\s*(\d+)(?::([0-9a-f]{2}))?\s*", str(spec))
    if not m:
        raise ValueError(f"line anchor {spec!r}: {_anchor_hint()}")
    no = int(m.group(1))
    if no == 0:
        return -1
    if not 1 <= no <= len(lines):
        raise ValueError(f"line {no} doesn't exist (the file has {len(lines)} lines)")
    cur = line_tag(lines[no - 1])
    if m.group(2) and m.group(2) != cur:
        raise ValueError(f"line {no} changed since you read it; it is now "
                         f"{no}:{cur}|{lines[no - 1][:160]} — read the file again")
    return no - 1


def _apply_one(text: str, e: dict) -> tuple[str, str]:
    """One edit on the text (LF line ends). Returns (new_text, note)."""
    new = str(e.get("new_string", "")).replace("\r\n", "\n")
    lines = text.split("\n")
    if e.get("lines") or e.get("insert_after") not in (None, ""):
        if e.get("lines"):
            spec = str(e["lines"])
            a, _, b = spec.partition("-")
            i = _parse_anchor(a, lines)
            j = _parse_anchor(b, lines) if b else i
            if i < 0 or j < i:
                raise ValueError(f"lines {spec!r}: give first-last, first ≤ last")
            body = new.split("\n") if new != "" else []
            out = lines[:i] + body + lines[j + 1:]
            return "\n".join(out), f"lines {i + 1}-{j + 1} replaced"
        i = _parse_anchor(e["insert_after"], lines)
        body = new.split("\n")
        out = lines[:i + 1] + body + lines[i + 1:]
        return "\n".join(out), f"inserted after line {i + 1}" if i >= 0 else "inserted at the top"
    if "old_string" not in e:
        raise ValueError("give old_string, or lines='N:tag', or insert_after='N:tag'")
    old = _strip_copied_prefixes(str(e["old_string"]).replace("\r\n", "\n"))
    if old == new:
        raise ValueError("old_string == new_string — nothing to do")
    if not old:
        raise ValueError("old_string is empty — use insert_after to add text")
    n = text.count(old)
    if n > 1 and not e.get("replace_all"):
        raise ValueError(f"old_string matches {n} times — pass replace_all=true "
                         "or add context")
    if n >= 1:
        return (text.replace(old, new) if e.get("replace_all")
                else text.replace(old, new, 1)), f"{n} match" + ("es" if n > 1 else "")
    i, j, found, how = _locate(text, old)
    new = _reindent(new, old.strip("\n").split("\n"), lines[i:j])
    out = lines[:i] + new.strip("\n").split("\n") + lines[j:]
    preview = "\n".join(f"  {k + 1}| {ln}" for k, ln in enumerate(found.split("\n"), start=i))[:600]
    return "\n".join(out), (
        f"lines {i + 1}-{j} matched ({how}); replaced this text:\n{preview}")


def edit_file(root: Path, shadow: Path, args: dict, mediator=None,
              seen: dict | None = None) -> str:
    """One or several edits (all or nothing): exact text, then
    whitespace-tolerant, then a single clear close match — never a guess
    between two places; or lines / insert_after by read_file's N:tag.
    Refused when the file changed since it was read (seen = path → text
    at the last read or write by Aura)."""
    p = _resolve_in_root(root, args["path"])
    if mediator is not None:
        _check_locked(mediator, root, p)
    if not p.exists():
        raise _missing(root, args["path"])
    # keep the file's own line endings: match on LF, write back as found
    with open(p, encoding="utf-8", newline="") as _f:
        raw_text = _f.read()
    crlf = "\r\n" in raw_text
    text = raw_text.replace("\r\n", "\n") if crlf else raw_text
    key = str(p.resolve())
    if seen is not None and key in seen and seen[key] != text:
        import difflib
        diff = list(difflib.unified_diff(seen[key].split("\n"), text.split("\n"),
                                         "as you read it", "now", n=1, lineterm=""))
        seen[key] = text           # Shown once; a retry after reading it goes through
        raise ValueError(
            f"{args['path']} changed since you last read or wrote it (a "
            "command, a formatter, or the user) — nothing was edited. What "
            "changed:\n" + "\n".join(diff[:40])
            + ("\n…" if len(diff) > 40 else "")
            + "\nRead the lines you want to change again, then edit.")
    edits = args.get("edits")
    if not edits:
        edits = [{k: args[k] for k in ("old_string", "new_string", "replace_all",
                                       "lines", "insert_after") if k in args}]
    if not isinstance(edits, list):
        raise ValueError("edits must be a list of {old_string, new_string} / {lines, new_string}")
    notes = []
    out = text
    for k, e in enumerate(edits, 1):
        try:
            out, note = _apply_one(out, e)
        except ValueError as ex:
            pre = f"edit {k} of {len(edits)}: " if len(edits) > 1 else ""
            raise ValueError(pre + str(ex) + (" — no edit was applied" if len(edits) > 1 else ""))
        notes.append(note)
    if out == text:
        raise ValueError("the edit changes nothing")
    snap = _snapshot(root, p, shadow)
    eol = (lambda t: t.replace("\n", "\r\n")) if crlf else (lambda t: t)
    own = _own_code(root, p)
    gate = _through_gate(root, shadow, own, eol(out), mediator) if own else ""
    if not own:
        p.write_text(eol(out), encoding="utf-8", newline="")
    verdict = _readback_verify(p, expect_bytes=len(eol(out).encode("utf-8")))
    if verdict.startswith("VERIFY-FAIL"):
        raise Violation(f"edit_file {args['path']}: {verdict}")
    if seen is not None:
        seen[key] = out
    summary = notes[0] if len(notes) == 1 else f"{len(notes)} edits: " + "; ".join(
        n.split("\n")[0] for n in notes)
    detail = "".join("\n" + n.split("\n", 1)[1] for n in notes if "\n" in n)
    tag = " [fuzzy-match]" if any("matched (" in n for n in notes) else ""
    return (f"edited {args['path']} ({summary}){tag} [shadow:{snap}]{gate} | "
            f"{verdict}{detail}" + syntax_note(p))


def restore(shadow: Path, root: Path, snapshot_id: str | None = None) -> str:
    """x undo — restore latest (or given) snapshot per file."""
    snaps = sorted(
        (d for d in shadow.glob("*") if d.is_dir()),
        key=lambda d: (d / "meta.json").read_text(encoding="utf-8", errors="replace") if (d / "meta.json").exists() else "",
    )
    if snapshot_id:
        snaps = [d for d in snaps if d.name == snapshot_id]
    if not snaps:
        return "no snapshots to restore"
    latest: dict[Path, Path] = {}
    skipped = 0
    for d in snaps:
        # a snapshot cut short by a crash has no (or a half-written)
        # meta.json — skip it instead of failing the whole undo
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            latest[root / meta["file"]] = d
        except (OSError, ValueError, KeyError, TypeError):
            skipped += 1
    out = []
    for target, d in latest.items():
        src = next((f for f in d.iterdir() if f.name != "meta.json"), None)
        if src and target.exists():  # Only restore over existing (no ghost files)
            shutil.copy2(src, target)
            try:
                out.append(str(target.relative_to(root)))
            except ValueError:                  # A file in an allowed outside folder
                out.append(str(target))
    note = (f" ({skipped} damaged snapshot(s) skipped)" if skipped else "")
    return ("restored: " + ", ".join(out) if out else "nothing restored") + note


# ---------------------------------------------------------------- registry ---
def register(mediator: Mediator, root: Path,
             shadow: Path | None = None) -> None:
    """Wire fs tools into the mediator, jailed to `root` (the workspace).
    Undo snapshots go to `shadow` — the notebook's .shadow, so nothing of
    Aura's lands in the user's project (default: root/.shadow)."""
    shadow = Path(shadow) if shadow else root / ".shadow"
    shadow.mkdir(parents=True, exist_ok=True)
    store = shadow.parent          # The notebook: where big outputs are saved

    mediator.register(ToolSpec(
        LIST_FILES, BlastRadius.READ, lambda a: None), plan_safe=True)
    mediator.register(ToolSpec(
        READ_FILE, BlastRadius.READ, lambda a: None), plan_safe=True)
    mediator.register(ToolSpec(
        WRITE_FILE, BlastRadius.WRITE, lambda a: None))
    mediator.register(ToolSpec(
        EDIT_FILE, BlastRadius.WRITE, lambda a: None))

    # What Aura last read or wrote per file: the stale-read guard
    seen: dict[str, str] = {}
    mediator._seen_files = seen
    ALLOWED[Path(root).resolve()] = mediator.outside_ok
    EXACT[Path(root).resolve()] = mediator.outside_exact
    mediator._fs = {  # Dispatcher closures used by loop.py
        LIST_FILES: lambda a: list_files(root, a),
        READ_FILE: lambda a: read_file(root, a, store, seen),
        WRITE_FILE: lambda a: write_file(root, shadow, a, mediator, seen),
        EDIT_FILE: lambda a: edit_file(root, shadow, a, mediator, seen),
    }

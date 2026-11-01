"""onboard.py — onboarding polish (import-agent, rich input).

`aura import-agent [path]`.

Rich input: @file mentions in REPL lines expand to inline file refs (path +
first N chars, marked) before the turn reaches the loop; multiline editor mode
via a trailing backslash (continuation until a line without it); image paste
detection where the terminal supports it (bracketed paste payload → saved to
output/ and referenced).

Provider niceties: response_format passthrough (json_object) when the provider
declares it — request built, provider-tolerant fallback kept.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

AGENT_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules",
               ".github/copilot-instructions.md")


# ------------------------------------------------------------- import-agent --
def find_agent_files(root: Path) -> list[Path]:
    """Instruction files we understand, existing, in priority order."""
    out = []
    for name in AGENT_FILES:
        p = Path(root) / name
        if p.is_file():
            out.append(p)
    return out


def import_agent(dest_eng: Path, source_root: Path) -> dict:
    """One command: source instruction files → engagement AGENTS.md. Returns
    {imported: [...], agents_md, profile_hint, merged_chars}. A settings-only
    source still yields a profile hint — the honest no-files case is empty.
    """
    dest_eng = Path(dest_eng)
    source_root = Path(source_root)
    found = find_agent_files(source_root)
    hint = _profile_hint(source_root)
    if not found:
        return {"imported": [], "agents_md": None,
                "profile_hint": hint, "merged_chars": 0}
    sections: list[str] = []
    imported: list[str] = []
    for p in found:
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            continue
        sections.append(f"<!-- imported from {p.name} -->\n{body}")
        imported.append(p.name)
    hint = _profile_hint(source_root)
    header = (f"<!-- imported by aura import-agent from "
              f"{source_root} at {time.strftime('%Y-%m-%d %H:%M')} -->")
    agents_md = dest_eng / "AGENTS.md"
    if agents_md.exists():
        existing = agents_md.read_text(encoding="utf-8").strip()
        if existing:
            sections.insert(0, existing)   # Never clobber local instructions
            imported.insert(0, "AGENTS.md (existing, preserved)")
    merged = header + "\n\n" + "\n\n".join(sections) + "\n"
    agents_md.write_text(merged, encoding="utf-8")
    return {"imported": imported, "agents_md": str(agents_md),
            "profile_hint": hint, "merged_chars": len(merged)}


def _profile_hint(root: Path) -> dict | None:
    """NEVER applied — a hint for `aura model` / `aura setup`."""
    for name in ("opencode.json", ".opencode.json"):
        p = Path(root) / name
        if not p.is_file():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        provider = (cfg.get("provider") or {})
        models = provider.get("models") or {}
        if models:
            first = next(iter(models))
            return {"source": name, "model": first,
                    "note": "verify endpoint — opencode config shape varies"}
    return None


# -------------------------------------------------------------- rich input --
def expand_mentions(text: str, eng: Path, budget: int = 600) -> str:
    """Rich input: expand @file mentions into inline refs (path + head of the
    file). Missing files are left as-written and noted. Injection-safe: content
    is wrapped in an explicit ref block the mediator/loop treat as plain text.
    """
    eng = Path(eng)

    def _sub(m: re.Match) -> str:
        rel = m.group(1).strip()
        p = (eng / rel) if not Path(rel).is_absolute() else Path(rel)
        try:
            resolved = p.resolve()
            resolved.relative_to(eng.resolve())     # Jail: engagement only
        except (ValueError, OSError):
            return f"{m.group(0)} (refuses: outside the notebook root)"
        if not resolved.is_file():
            return f"{m.group(0)} (missing)"
        data = resolved.read_text(encoding="utf-8", errors="replace")
        head = data[:budget]
        more = f" …[+{len(data) - budget}B]" if len(data) > budget else ""
        return (f"{m.group(0)}\n--- begin @{rel} ---\n{head}{more}\n"
                f"--- end @{rel} ---")

    return re.sub(r"@([A-Za-z0-9_./\-]+)", _sub, text)


def read_multiline(prompt: str, single_line: str) -> str:
    """Multiline editor mode: a trailing backslash continues the input.
    Returns the joined text. Pure-I/O helper (REPL calls it)."""
    if not single_line.endswith("\\"):
        return single_line
    parts = [single_line[:-1]]
    while True:
        try:
            nxt = input(prompt.replace("you>", "  …>"))
        except (KeyboardInterrupt, EOFError):
            break
        if nxt.endswith("\\"):
            parts.append(nxt[:-1])
            continue
        parts.append(nxt)
        break
    return "\n".join(parts)


def detect_image_paste(line: str, eng: Path) -> str | None:
    """Bracketed-paste image payloads (base64 data URLs) → saved to
    output/ and replaced by a reference. Returns the output ref or None."""
    m = re.search(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]{16,})", line)
    if not m:
        return None
    import base64
    outdir = Path(eng) / "output"
    outdir.mkdir(exist_ok=True)
    name = f"paste-{int(time.time())}.png"
    try:
        (outdir / name).write_bytes(base64.b64decode(m.group(1)))
    except Exception:
        return None
    ref = f"output/{name}"
    return line[:m.start()] + f"[image saved: {ref}]" + line[m.end():]


# ---------------------------------------------------- provider niceties ----
def response_format_kwargs(base_url: str, model: str) -> dict:
    """JSON mode for a model checked to support it; {} otherwise. Pass the result
    as llm.chat(..., extra=...).
    """
    from .model_facts import fact, model_key
    if fact("json", model_key(base_url, model)) == "yes":
        return {"response_format": {"type": "json_object"}}
    return {}


# ------------------------------------------------- naming gate
_KEY_PREFIX = re.compile(r"^(sk-|sk_|key-|api-|AIza|ghp_|gsk_|xoxb-|xai-)")


def profile_name_looks_like_key(name: str) -> bool:
    """Catch a key pasted into the profile-NAME field. A known key prefix, or a
    long run mixing upper, lower and digits — a long readable name
    ('openrouter-backup-profile') is not a key.
    """
    n = (name or "").strip()
    if not n:
        return False
    if _KEY_PREFIX.match(n):
        return True
    return (len(n) >= 24 and " " not in n
            and any(c.isdigit() for c in n)
            and any(c.isupper() for c in n) and any(c.islower() for c in n))


# ---------------------------------------------------------------------------
# N: masked secret input (Windows conhost showed nothing)
# ---------------------------------------------------------------------------


def _read_char() -> str:
    """One keypress. msvcrt on Windows, termios on posix, input fallback."""
    import sys
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):      # function/arrow keys — swallow
            msvcrt.getwch()
            return ""
        return ch
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch


def ask_secret(prompt: str = "API key: ") -> str:
    """Masked single-line secret entry: '*' per char, Backspace works,
    Enter submits. Falls back to getpass when no console (piped)."""
    import sys
    print(prompt, end="", flush=True)
    chars: list[str] = []
    try:
        if not sys.stdin.isatty():
            raise OSError("no console")
        while True:
            ch = _read_char()
            if ch in ("\r", "\n"):
                print()
                break
            if ch in ("\x08", "\x7f"):          # Backspace
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ch == "\x03":                    # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x04":                    # Ctrl+D
                break
            if ch and ch.isprintable():
                chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    except OSError:
        # piped / no console: read the line from the pipe — piped text is
        # never shown, so there is nothing to mask. (getpass read the
        # KEYBOARD on Windows, so a scripted setup hung or got no key.)
        print()
        return (sys.stdin.readline() or "").strip()
    return "".join(chars).strip()

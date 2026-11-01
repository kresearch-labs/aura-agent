"""notes.py — who wrote a message: the user, or Aura.

Aura talks to the model through the same "user" turns the user types in
(chat APIs have no third speaker), so every note Aura adds carries one
label the model is told about, and the session file stores who wrote it
(`by`). Summaries, past-chat search and the "your messages" block keep the
user's words only — a user message that happens to start with "[URGENT]"
is still the user's.
"""
from __future__ import annotations

import re

NOTE = "[AURA NOTE]"
_TYPED = "(typed by the user) "

# chats saved before `by` existed: Aura's notes are known by their tag
_LEGACY_TAGS = ("WRITE EARLY", "IMAGE", "HELPER DONE", "PAST CHATS", "CHECK FIRST",
                "DEEP RESEARCH MODE", "RESEARCH", "BACKGROUND", "LINT", "EMPTY REPLY",
                "CUT OFF", "SILENT-SKIP GUARD", "VERIFY", "SOURCES",
                "EXTERNAL OBSERVATION", "AURA NOTE")
_LEGACY = re.compile(r"\[(" + "|".join(re.escape(t) for t in _LEGACY_TAGS) + r")\b")


def aura_note(content):
    """Label a note for the model (idempotent). Text or text+image parts."""
    if isinstance(content, list):
        out = [dict(p) for p in content]
        for p in out:
            if p.get("type") == "text":
                p["text"] = aura_note(str(p.get("text", "")))
                return out
        return [{"type": "text", "text": NOTE}] + out
    text = str(content or "")
    return text if text.startswith(NOTE) else f"{NOTE} {text}"


def user_text(text: str) -> str:
    """The user's message as sent: never mistakable for a note."""
    return _TYPED + text if str(text).startswith(NOTE) else text


def text_of(content) -> str:
    """The text of a message; images become a short mention, never data."""
    if isinstance(content, list):
        bits, n = [], 0
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                bits.append(str(p.get("text", "")))
            elif p.get("type") in ("image_url", "image_ref"):
                n += 1
                bits.append(f"[attached image: {image_name(p) or n}]")
        return " ".join(b for b in bits if b)
    return str(content or "")


def image_name(part: dict) -> str:
    path = str(part.get("path") or "")
    return path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""


def is_note(m: dict) -> bool:
    """Aura's note (any role 'user' message Aura wrote, old or new)."""
    if m.get("role") != "user":
        return False
    if m.get("by"):
        return m["by"] == "aura"
    body = text_of(m.get("content")).lstrip()
    if body.startswith(_TYPED):
        return False
    return bool(_LEGACY.match(body))


def is_users_words(m: dict) -> bool:
    """The user's own message (a steer typed mid-run counts)."""
    return m.get("role") == "user" and not is_note(m) \
        and bool(text_of(m.get("content")).strip())


def image_ref(path: str, n: int = 0, by: str = "user") -> dict:
    """An image kept as a file: the chat stores the path, the data is read only
    when it is sent.
    """
    return {"type": "image_ref", "path": str(path), "n": n, "by": by}


def _image_data(path: str) -> str | None:
    import base64
    from pathlib import Path
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    kind = ("jpeg" if data[:3] == b"\xff\xd8\xff" else "gif" if data[:4] == b"GIF8"
            else "webp" if data[8:12] == b"WEBP" else "png")
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


def _stub(p: dict) -> dict:
    who = "you pasted" if p.get("by", "user") == "user" else "Aura attached"
    name = image_name(p) or "image"
    num = f" #{p['n']}" if p.get("n") else ""
    reopen = f" — reopen with view_image(path='{p['path']}')" if p.get("path") else ""
    return {"type": "text", "text": f"[image{num} {who}: {name}, not re-sent{reopen}]"}


def stamp(ts: float) -> str:
    """'[Wed 22 Oct 14:05] ' — when the user's message arrived."""
    import time as _t
    return f"[{_t.strftime('%a %d %b %H:%M', _t.localtime(ts))}] "


def for_wire(messages: list[dict]) -> list[dict]:
    """What the model server gets: `by` removed; images sent only in the
    current turn (from the user's newest message on) — older ones become a
    one-line stub the model can reopen. Never mutates the input."""
    last_user = max((i for i, m in enumerate(messages) if is_users_words(m)), default=-1)
    out = []
    for i, m in enumerate(messages):
        c = m.get("content")
        # when each of the user's messages arrived — a few tokens, the same
        # on every call (from the stored time), so the cached start holds
        if m.get("ts") and is_users_words(m):
            st = stamp(float(m["ts"]))
            if isinstance(c, str):
                c = st + c
            elif isinstance(c, list):
                c = [dict(p) for p in c]
                first = next((p for p in c if p.get("type") == "text"), None)
                if first is not None:
                    first["text"] = st + str(first.get("text", ""))
                else:
                    c = [{"type": "text", "text": st.strip()}] + c
            m = {**m, "content": c}
        if isinstance(c, list) and any(isinstance(p, dict) and
                                       p.get("type") in ("image_ref", "image_url") for p in c):
            parts = []
            for p in c:
                if not isinstance(p, dict) or p.get("type") not in ("image_ref", "image_url"):
                    parts.append(p)
                elif i < last_user:
                    parts.append(_stub(p))
                elif p.get("type") == "image_ref":
                    url = _image_data(p.get("path", ""))
                    parts.append({"type": "image_url", "image_url": {"url": url}} if url
                                 else {"type": "text", "text":
                                       f"[image {image_name(p)} is no longer on disk]"})
                else:
                    parts.append(p)
            m = {**m, "content": parts}
        if m.get("talk") and m.get("role") == "user":
            # said in discuss mode: talk, never a work request (legend.py)
            c = m.get("content")
            if isinstance(c, str):
                m = {**m, "content": "[TALK] " + c}
            elif isinstance(c, list):
                m = {**m, "content": [{"type": "text", "text": "[TALK]"}] + list(c)}
        if "by" in m or "ts" in m or "talk" in m:
            m = {k: v for k, v in m.items() if k not in ("by", "ts", "talk")}
        if m.get("role") == "assistant" and m.get("content") is None and not m.get("tool_calls"):
            # an empty reply saved with no content (older chats): Ollama refuses
            # the whole request over it — empty text is accepted
            m = {**m, "content": ""}
        out.append(m)
    return out


def users_words(m: dict) -> str:
    """The user's text, without Aura's wrapping."""
    body = text_of(m.get("content")).strip()
    for tag in ("[STEER]", "[ASK]", "[CHANGE]", "[LATER]"):    # Typed while it worked
        if body.startswith(tag):
            body = body[len(tag):].strip()
            break
    if body.startswith(_TYPED):
        body = body[len(_TYPED):]
    return body

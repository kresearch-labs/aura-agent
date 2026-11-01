"""view_image.py — look at an image file.

Aura decides from what was MEASURED: the main model sees images → the image is
attached right after the tool result (tool results can't carry images in the
OpenAI format); it doesn't, but a helper model checked to see images does → the
helper answers the question; neither → it says why and how to fix it.
"""
from __future__ import annotations

import base64
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec

MAX_BYTES = 5_000_000
KINDS = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif", b"RIFF": "webp"}


def _kind(data: bytes) -> str | None:
    for sig, k in KINDS.items():
        if data.startswith(sig) and (k != "webp" or data[8:12] == b"WEBP"):
            return k
    return None


def _resolve(root: Path, store: Path | None, path: str) -> Path | None:
    rel = str(path).replace("\\", "/").lstrip("./")
    for base in (root, store):
        if base is None:
            continue
        p = (Path(base) / rel).resolve()
        try:
            p.relative_to(Path(base).resolve())          # Jailed to the project / store
        except ValueError:
            continue
        if p.is_file():
            return p
    return None


def view_image(mediator: Mediator, root: Path, store: Path, args: dict) -> str:
    path = str(args.get("path", "")).strip()
    question = str(args.get("question", "") or "Describe this image.").strip()
    p = _resolve(root, store, path)
    if p is None:
        return f"error: no image file {path!r} in the project (or saved artifacts)"
    data = p.read_bytes()
    kind = _kind(data)
    if kind is None:
        return "error: not a png, jpeg, gif or webp image"
    if len(data) > MAX_BYTES:
        return f"error: the image is {len(data) // 1_000_000} MB — the limit is 5 MB"
    url = f"data:image/{kind};base64," + base64.b64encode(data).decode()
    from ..model_facts import fact, model_key
    llm = getattr(mediator, "_main_llm", None)
    v = fact("vision", model_key(llm.p.base_url, llm.p.model)) if llm is not None else None
    if llm is not None and v is None:          # Not known yet: Aura checks itself, once
        from ..image_paste import vision_check
        v = vision_check(type("L", (), {"llm": llm})())
    main_sees = v == "yes"
    if main_sees:
        q = getattr(mediator, "_image_queue", None)
        if q is None:
            q = mediator._image_queue = []
        q.append({"url": url, "path": path})
        return f"{path} ({len(data):,} bytes) — attached in the next message for you to look at"
    # the helper, when it was checked to see images
    from ..helper import profile as helper_profile
    hp = helper_profile(getattr(llm, "p", None))
    if hp is not None and fact("vision", model_key(hp.base_url, hp.model)) == "yes":
        from ..llm import LLMClient
        try:
            res = LLMClient(hp).chat([{"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": url}}]}], temperature=0)
            return f"[the helper model {hp.model} looked at {path}]\n{(res.content or '').strip()[:4000]}"
        except Exception as e:                           # noqa: BLE001
            return f"error: the helper model couldn't look at it: {str(e)[:120]}"
    why = ("this model can't see images (checked)" if v == "no"
           else "the check of whether this model sees images failed")
    return (f"can't look at {path}: {why} — a limit, not something to work around. Tell the "
            "user: a model that sees images (/model), or a helper model that does "
            "(helper.model) — they choose.")


def register(mediator: Mediator, root: Path, store: Path, dispatch: dict) -> None:
    mediator.register(ToolSpec("view_image", BlastRadius.READ, lambda a: None), plan_safe=True)
    dispatch["view_image"] = lambda a: view_image(
        mediator, Path(getattr(mediator, "work_dir", None) or root), store, a)

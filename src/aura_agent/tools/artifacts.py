"""artifacts.py — content-addressed artifact store, hardened.

2 research: silent truncation makes reasoners hallucinate. Every oversized tool
output becomes a content-addressed artifact:
engagement/artifacts/<sha256[:16]>/<name> hardening: crash-safe writes (temp →
rename), append-only manifest (engagement/artifacts/manifest.jsonl), digest
verification on every load (tampered artifact refuses), full sha256 in manifest
for external audit. The model always sees: head+tail preview + typed metadata
{truncated, total_bytes, artifact_ref, duration_ms}.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

ARTIFACT_INLINE_LIMIT = 8_000   # Bytes; beyond → artifact + preview
MANIFEST = "manifest.jsonl"


def full_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(data: bytes) -> str:
    return full_digest(data)[:16]


def _atomic_write(path: Path, data: bytes) -> None:
    """Crash-safe: write temp sibling → rename (same dir, same fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _manifest_append(root: Path, entry: dict) -> None:
    """Append-only manifest; one line per artifact, crash-tolerant reads."""
    p = Path(root) / "artifacts" / MANIFEST
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def store_artifact(root: Path, data: bytes, name: str) -> dict:
    """Content-addressed store; idempotent per digest; manifest-recorded."""
    d = _digest(data)
    dest = Path(root) / "artifacts" / d
    dest.mkdir(parents=True, exist_ok=True)
    f = dest / name
    if not f.exists():
        _atomic_write(f, data)
    _atomic_write(dest / "meta.json", json.dumps({
        "sha256_16": d, "sha256": full_digest(data),
        "bytes": len(data), "name": name,
        "ts": round(time.time(), 3)}).encode())
    _manifest_append(root, {"ref": f"artifacts/{d}/{name}",
                            "sha256": full_digest(data), "bytes": len(data),
                            "ts": round(time.time(), 3)})
    return {"artifact_ref": f"artifacts/{d}/{name}", "total_bytes": len(data),
            "sha256_16": d, "sha256": full_digest(data)}


def load_artifact(root: Path, ref: str) -> bytes:
    """Digest-verified read: ref tampering → refuse. Ref format is enforced:
    artifacts/<sha16>/<name> — the digest in the path must match the bytes."""
    p = Path(root) / ref
    if not p.exists():
        raise FileNotFoundError(f"artifact not found: {ref}")
    data = p.read_bytes()
    expect = ref.split("/")[1]
    if _digest(data) != expect:
        raise ValueError(f"artifact digest mismatch: {ref}")
    return data


def manifest_entries(root: Path) -> list[dict]:
    p = Path(root) / "artifacts" / MANIFEST
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def verify_manifest(root: Path) -> dict:
    """Every manifest entry must resolve + digest-match. Store integrity."""
    ok, bad = 0, []
    for e in manifest_entries(root):
        ref = e.get("artifact_ref") or e.get("ref")
        if not ref:
            continue
        try:
            load_artifact(root, ref)
        except ValueError as ex:
            if "mismatch" in str(ex):
                bad.append({"ref": ref, "error": str(ex)})
                continue
        except FileNotFoundError as ex:
            bad.append({"ref": ref, "error": str(ex)})
            continue
        ok += 1
    return {"ok": ok, "bad": bad, "total": ok + len(bad)}


def head_tail_preview(data: bytes, limit: int = ARTIFACT_INLINE_LIMIT,
                      head_frac: float = 2 / 3) -> str:
    """Head+tail inline preview with typed gap marker."""
    text = data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    head = int(limit * head_frac)
    tail = limit - head
    return (text[:head]
            + f"\n...[TRUNCATED {len(text)} chars total; "
              f"{len(text) - head - tail} omitted; full copy in artifact]...\n"
            + text[-tail:])


def wrap_result(root: Path, name: str, data: bytes,
                duration_ms: int = 0,
                inline_limit: int | None = None) -> dict:
    """THE contract: every tool result passes through here.

    `inline_limit` comes from the resolved RunSettings (SETTINGS_DESIGN): the
    preset's tool-output cap. Beyond it → artifact + LOUD notice in the result
    text itself, never a metadata-only pointer. Full's 1MB seatbelt is just its
    limit.
    """
    limit = inline_limit or ARTIFACT_INLINE_LIMIT
    small = len(data) <= limit
    if small:
        return {"result": data.decode("utf-8", errors="replace"),
                "truncated": False, "total_bytes": len(data),
                "artifact_ref": None, "duration_ms": duration_ms}
    art = store_artifact(root, data, name)
    preview = head_tail_preview(data, limit=limit)
    notice = (f"\n[TRUNCATED — {len(data):,} bytes total, showing head+tail. "
              f"Full copy archived at artifact '{art['artifact_ref']}' — "
              f"fetch it with read_file (artifact path) if the middle matters.]")
    return {"result": preview + notice, "truncated": True,
            "total_bytes": art["total_bytes"],
            "artifact_ref": art["artifact_ref"],
            "duration_ms": duration_ms}

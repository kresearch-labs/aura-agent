"""selftrain.py — the retrain reflex of pblocks_code.

The pblock is a limb of the agent: it should always know what to do AND how
to do it. Learning from lived runs is `pblock_learn` (store-level, always on).
This organ completes the body: when the seat's OWN recorded state says its
fitted judgment has gone stale, it retrains itself — programmatically, in
process, from its OWN recorded paths. No human, no CLI.

A seat is stale when ANY of:
  1. its corpus has grown past the head's n_cases (retrain absorbs the new
     confirmed outcomes), by more than the seat's declared growth margin
  2. its artifact declares retrain policy (gates.retrain_after_new_rows)
  3. its head is missing/untrainable while a corpus+template exist

Paths come from the artifact itself (corpus, template, gates) — the seat
carries its own recipe. The corpus-spec path (corpus_spec) is recorded too:
if present, the corpus is RE-ENUMERATED from the spec before retraining
(the territory grew -> the corpus grows with it), then trained.

Throttle: a seat-declared `retrain_after_new_cases` (default: max(500, 20%
of n_cases)) so the reflex fires when warranted, not on every run.
Env: AURA_SELFTRAIN=0 disables (default on). Never raises — a failed
retrain leaves the previous heads + store untouched (verify-then-swap).
"""
from __future__ import annotations

import json, os, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).parent


def _artifact_paths(root: str, name: str) -> dict:
    """Locate the seat's trained artifact (both layout conventions)."""
    for cand in (os.path.join(root, "aura_engine", "persona", "weights", f"{name}_heads.json"),
                 os.path.join(root, "weights", f"{name}_heads.json"),
                 os.path.join(root, f"{name}_heads.json")):
        if os.path.exists(cand):
            return {"artifact": cand,
                    "root": (os.path.dirname(os.path.dirname(os.path.dirname(cand)))
                             if "aura_engine" in cand else
                             os.path.dirname(os.path.dirname(cand)))}
    return {}


def seat_state(root: str, name: str) -> dict | None:
    """Read the seat's head artifact: n_cases, corpus, template, gates, spec."""
    loc = _artifact_paths(root, name)
    if not loc:
        return None
    try:
        art = json.load(open(loc["artifact"], encoding="utf-8"))
    except Exception:
        return None
    art["_root"] = loc["root"]
    art["_artifact_path"] = loc["artifact"] if "artifact" in loc else loc["artifact_path"] if "artifact" in loc else loc["artifact"]
    return art


def corpus_count(path: str) -> int:
    """Rows in the seat's corpus file (jsonl or json list)."""
    try:
        if path.endswith(".jsonl"):
            with open(path, encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        return len(json.load(open(path, encoding="utf-8")))
    except Exception:
        return -1


def staleness(root: str, name: str) -> dict:
    """Why (or why not) this seat should retrain now. Read-only assessment."""
    art = seat_state(root, name)
    if not art:
        return {"seat": name, "stale": False, "reason": "no trained artifact"}
    n_head = int(art.get("n_cases", 0))
    corpus = art.get("corpus", "")
    n_corpus = corpus_count(corpus) if (corpus := art.get("corpus")) else -1
    gates = art.get("gates") or {}
    threshold = int(gates.get("retrain_after_new_cases",
                              max(500, int(0.20 * max(n_head, 1)))))
    new_rows = max(0, n_corpus - n_head) if n_corpus >= 0 else 0
    reasons = []
    if n_corpus == -1:
        reasons.append("corpus file unreadable")
    if new_rows >= threshold:
        reasons.append(f"corpus grew +{new_rows} confirmed rows (threshold {threshold})")
    if n_head < int(gates.get("min_cases", 0)):
        reasons.append("head under its own declared floor")
    return {"seat": name, "stale": bool(reasons), "reasons": reasons,
            "n_head": n_head, "n_corpus": n_corpus,
            "threshold": threshold, "corpus_spec": art.get("corpus_spec")}


def maybe_retrain(root: str, name: str, *, force: bool = False) -> dict:
    """The seat's self-retrain reflex. Returns a receipt dict.

    Flow: assess staleness -> (if spec present) re-enumerate corpus from the
    seat's own spec -> invoke the trainer programmatically (same organs) ->
    report. The previous heads/store are only replaced on trainer PASS
    (train_pblock writes atomically per-file; a FAIL leaves them intact).
    """
    t0 = time.time()
    state = staleness(root, name)
    if not state["stale"] and not force:
        return {"seat": name, "retrained": False, "reason": "not stale"}
    art = seat_state(root, name)
    if not art:
        return {"seat": name, "retrained": False, "reason": "no artifact to refresh from"}

    # 1. re-enumerate the corpus from the seat's own spec (territory grew)
    spec_path = art.get("corpus_spec")
    _corpus_override = None
    if spec_path and os.path.exists(spec_path):
        try:
            from .corpus_builder import build_from_spec_file
            corpus_dir = os.path.join(art["_root"], "corpus")
            os.makedirs(corpus_dir, exist_ok=True)
            _corpus_override = os.path.join(corpus_dir, f"{name}.jsonl")
            build_from_spec_file(spec_path, out_path=_corpus_override)
        except Exception as e:
            return {"seat": name, "retrained": False,
                    "reason": f"corpus re-enumeration failed: {e}"}

    # 2. train programmatically — same organs, same math, no CLI
    import train_pblock

    class _Args:  # The trainer's argv, built from the seat's OWN recorded paths
        template = art.get("template")
        corpus = art.get("corpus")
        out_dir = art["_root"]
    if not _Args.template or not os.path.exists(_Args.template):
        return {"seat": name, "retrained": False, "reason": "template path missing"}

    old_n = int(art.get("n_cases", 0))
    rc = train_pblock.main() if False else None
    # invoke main() with patched argv (argparse reads sys.argv)
    import sys as _sys
    argv_backup = _sys.argv
    _sys.argv = ["train_pblock.py", "--template", _Args.template,
                 "--corpus", _corpus_override or _Args.corpus,
                 "--out-dir", _Args.out_dir]
    try:
        rc = train_pblock.main()
    finally:
        _sys.argv = argv_backup

    new_state = seat_state(root, name)
    n_new = int(new_state.get("n_cases", 0)) if new_state else 0
    return {"seat": name, "retrained": rc == 0, "old_n_cases": old_n,
            "new_n_cases": n_new, "rc": rc,
            "wall_s": round(time.time() - t0, 1)}


def selftrain_all(root: str, seats: list[str] | None = None) -> list[dict]:
    """Run the reflex for every seat under root (run-exit hook calls this)."""
    if os.environ.get("AURA_SELFTRAIN", "1") == "0":
        return []
    out = []
    for cand in ("aura_engine/persona/weights", "weights"):
        d = os.path.join(root, cand)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith("_heads.json"):
                    name = f[:-len("_heads.json")]
                    out.append(maybe_retrain(root, name))
            break
    return [r for r in out if r.get("retrained") or r.get("stale")]

"""persona_tool.py — Aura trains pblocks with the user.

Two tools, used by `/persona train`:

  pblock_propose ask the user about ONE proposed pblock with the five
                  fixed answers: Yes, train it · No, skip it · Change it ·
                  Tell me more · Other (the user types). Not counted against
                  clarify's per-task limit (a plan can hold up to 7+).
  pblock_train train an APPROVED pblock from the template + corpus Aura
                  wrote, then file it in the pblocks folder (store, trained
                  heads, template, packed form with its self-check) and turn
                  it on for this chat. Refuses anything the user did not
                  answer "Yes, train it" for in this task.

How to build the template and the corpus is in the guides shipped with the
engine (pblock_engine/docs): the decision space, real outcomes, no padding.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..mediator import BlastRadius, Mediator, ToolSpec

PROPOSE, TRAIN = "pblock_propose", "pblock_train"
ANSWERS = ("Yes, train it", "No, skip it", "Change it", "Tell me more", "Other")
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _state(mediator) -> dict:
    st = getattr(mediator, "_pblock_state", None)
    if st is None:
        st = mediator._pblock_state = {"approved": {}, "asked": 0}
    return st


def _build_corpus(spec: Path, work: Path, name: str):
    """The corpus comes from the plan (corpus_builder: the size falls out of
    the declared space), every real record carries its source, near-copies
    are dropped. Returns (corpus path, near-copies dropped, error)."""
    import json
    from ..pblock_engine import corpus_builder as CB
    try:
        import yaml
        raw = spec.read_text(encoding="utf-8")
        sp = yaml.safe_load(raw) if spec.suffix.lower() in (".yml", ".yaml") else json.loads(raw)
        if not isinstance(sp, dict):
            return None, 0, "the plan is not a mapping"
        sp = sp.get("corpus_spec", sp)
        sp["_spec_dir"] = str(spec.parent)
        sp["require_sources"] = True
        rows = CB.build_corpus(sp)
    except (ValueError, KeyError, OSError, TypeError) as e:
        return None, 0, str(e)
    except Exception as e:                           # noqa: BLE001 (yaml errors)
        return None, 0, f"{type(e).__name__}: {e}"
    seen, kept = set(), []
    for r in rows:
        k = " ".join(re.findall(r"[a-z]+", str(r.get("situation", "")).lower()))
        if k in seen:
            continue
        seen.add(k)
        kept.append(r)
    out = work / f"{name}_corpus.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r) + "\n")
    return out, len(rows) - len(kept), ""


def _worse(new_heads: Path, old_heads: Path) -> str:
    """'' when the new trained judges are at least as good as the old ones
    (outcome-head cross-validated accuracy); else why not."""
    import json

    def acc(p):
        try:
            return float(json.loads(p.read_text(encoding="utf-8"))["heads"]["outcome"]["acc"])
        except Exception:                            # noqa: BLE001
            return None
    n, o = acc(new_heads), acc(old_heads)
    if n is not None and o is not None and n < o:
        return f"outcome accuracy {n:.3f} vs the current {o:.3f}"
    return ""


def _carry_lived(name: str, lib: Path, new_store: Path) -> None:
    """A retrain re-seeds from the corpus; what the pblock LIVED since is
    carried over (lessons are never thrown away). The old packed pair goes,
    the new store replaces it (it is packed again right after)."""
    from ..pblock_engine.store import PersonaStore
    old = PersonaStore(name, str(lib))
    lived = [c for c in old.cases if c.get("origin") != "seed"]
    if lived:
        new = PersonaStore(name, str(new_store.parent))
        new.cases.extend(lived)
        new.save()
    for suffix in ("_embeddings.npy", "_meta.jsonl"):
        f = lib / f"{name}{suffix}"
        if f.exists():
            f.unlink()


def register(mediator: Mediator, dispatch: dict) -> None:
    def propose(args) -> str:
        name = str(args.get("name") or "").strip().lower()
        purpose = str(args.get("purpose") or "").strip()
        data = str(args.get("data") or "").strip()
        if not _NAME.match(name):
            return "error: name must be lowercase letters, digits and _ (e.g. clause_risk)"
        if not purpose or not data:
            return "error: give what this pblock judges (purpose) and the kind of training data (data)"
        st = _state(mediator)
        if st["asked"] >= 30:
            return "You have asked about 30 pblocks in this task; stop and summarise the plan."
        asker = getattr(mediator, "asker", None)
        if asker is None:
            return ("Nobody is here to answer. Do not train anything: end your turn and "
                    "list the proposed pblocks in your final answer.")
        st["asked"] += 1
        n, of = args.get("number"), args.get("of")
        head = f"Pblock {n} of {of} — {name}" if n and of else f"Pblock — {name}"
        q = f"{head}\n  {purpose}\n  data: {data}"
        try:
            try:
                answer = asker(q, list(ANSWERS), multi=False, free=True)
            except TypeError:
                answer = asker(q, list(ANSWERS))
        except Exception as e:                       # noqa: BLE001
            return f"The question could not be shown ({e}). Do not train it; say so."
        answer = str(answer or "").strip()
        if answer == ANSWERS[0]:
            st["approved"][name] = {"purpose": purpose, "data": data}
            return f"[USER ANSWER] Yes, train it — {name} is approved for training."
        st["approved"].pop(name, None)
        if answer == ANSWERS[1]:
            return f"[USER ANSWER] No, skip it — drop {name} and move on."
        if answer in (ANSWERS[2], ANSWERS[3], ANSWERS[4]):
            return (f"[USER ANSWER] {answer} — ask the user what they want "
                    "(a short question), then propose this pblock again.")
        return (f"[USER ANSWER — typed by the user] {answer} — apply it, then propose "
                "this pblock again.")

    def train(args) -> str:
        name = str(args.get("name") or "").strip().lower()
        st = _state(mediator)
        if name not in st["approved"]:
            return (f"refused: the user has not answered 'Yes, train it' for '{name}' in "
                    "this task. Propose it with pblock_propose first.")
        tpl = Path(str(args.get("template") or "")).expanduser()
        spec = Path(str(args.get("spec") or "")).expanduser()
        if not tpl.is_file():
            return f"error: no template at {tpl}"
        if not spec.is_file():
            return (f"error: no corpus plan at {spec}. Write the corpus plan (craft_truths, "
                    "dimensions, negations, vocabulary, real_records with a source each) "
                    "as the guides say; the size falls out of it.")
        from .. import pblocks as P
        lib = P.stores_dir()
        work = lib / "training" / name
        work.mkdir(parents=True, exist_ok=True)
        corpus, dropped, err = _build_corpus(spec, work, name)
        if err:
            return f"error: the corpus plan was refused: {err}"
        # the trainer names the pblock after the template's name:
        txt = tpl.read_text(encoding="utf-8")
        if re.search(r"^name:", txt, re.M):
            txt = re.sub(r"^name:.*$", f"name: {name}", txt, count=1, flags=re.M)
        else:
            txt = f"name: {name}\n" + txt
        if not re.search(r"^territory:", txt, re.M):
            txt += f'\nterritory: "{st["approved"][name]["purpose"]}"\n'
        tpl_out = work / f"{name}.yml"
        tpl_out.write_text(txt, encoding="utf-8")
        try:
            run = subprocess.run(
                [sys.executable, "-m", "aura_agent.pblock_engine.train_pblock",
                 "--template", str(tpl_out), "--corpus", str(corpus),
                 "--out-dir", str(work)],
                capture_output=True, text=True, timeout=3600,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except subprocess.TimeoutExpired:
            return "error: training ran over an hour and was stopped; nothing was added."
        report = (run.stdout or "")[-1500:] + (("\n" + run.stderr[-600:]) if run.returncode else "")
        report = f"corpus built from the plan ({dropped} near-copies dropped)\n" + report
        store = work / "stores" / f"{name}_store.jsonl"
        heads = work / "weights" / f"{name}_heads.json"
        if run.returncode != 0 or not store.exists():
            return ("Training did not pass its gates; nothing was added to the pblocks "
                    f"folder. Trainer report:\n{report}\nTell the user what failed "
                    "(usually too few cases or too little variety) and what data would fix it.")
        # A retrained pblock replaces the old one only if it judges at least as
        # well, and keeps everything the old one lived
        if name in P.store_seats():
            worse = _worse(heads, lib / "weights" / heads.name)
            if worse:
                return (f"Retrained '{name}', but the new version judged worse ({worse}); "
                        f"the current pblock stays. Trainer report:\n{report}")
            _carry_lived(name, lib, store)
        shutil.copy2(store, lib / store.name)
        if heads.exists():
            (lib / "weights").mkdir(exist_ok=True)
            shutil.copy2(heads, lib / "weights" / heads.name)
        shutil.copy2(tpl_out, lib / tpl_out.name)
        packed = P.pack(name)
        chat = os.environ.get("AURA_PB_CHAT", "")
        if chat:
            P.switch(chat, name, True)
        return (f"Trained and added '{name}' to the pblocks folder"
                + (" and turned it on for this chat" if chat else "")
                + f". Packed: {'yes' if packed.get('ok') else 'no — ' + packed.get('note', '')}."
                + f"\nTrainer report:\n{report}")

    mediator.register(ToolSpec(PROPOSE, BlastRadius.READ, lambda a: None), plan_safe=True)
    mediator.register(ToolSpec(TRAIN, BlastRadius.EXECUTE, lambda a: None))
    dispatch[PROPOSE] = propose
    dispatch[TRAIN] = train

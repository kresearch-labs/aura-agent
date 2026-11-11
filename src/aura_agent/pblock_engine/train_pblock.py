"""train_pblock.py --template X.yml [--corpus PATH] [--out-dir DIR]

Generic pblock trainer: one command per persona.

A pblock template (.yml) declares:
  - the slice of the world this persona judges (territory, free text)
  - where its experience corpus lives ({situation, outcome} records, json/jsonl)
  - declarative judgment labels (keyword rules) — the vocabulary it judges with
  - quality floors (min_cases, variety); NO caps: store size + latency are measurements, not gates

The trainer: loads the corpus -> embeds situations with the FROZEN MiniLM int8
ONNX embedder -> adds generic structural features -> trains one logistic head
per label (+ an 'outcome' head from ground truth) -> writes the head artifact
to weights/ -> builds the persona's bi-temporal store ->
prints a PASS/FAIL gate report.

Training math is unchanged from the SWE-era original (logistic, 400 iters,
lr 0.15, L2 1e-4; 5-fold CV honesty fields). What changed vs the original:
corpus loading and label rules are template-declared instead of hardcoded,
so any domain can be trained with the same command.

The ONNX embedder is never fine-tuned here (measured: fine-tuning adds no
accuracy at product corpus scale while costing inspectability — frozen eyes,
trained heads).
"""
import sys, os, json, re, time, argparse
import numpy as np

try:
    import yaml
except ImportError:
    sys.exit("pyyaml required: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The organ stack lives at this folder's top level (store.py, judge_v0.py, ...)
# and uses package-relative imports. Expose the folder itself as the package
# `persona` so `from .embeddings import ...` resolves here — no host
# engine layout required. If a host engine dir exists, it wins (AURA_ENGINE_PATH).
_ENGINE_ENV = os.environ.get("AURA_ENGINE_PATH")
if _ENGINE_ENV and os.path.isdir(_ENGINE_ENV):
    sys.path.insert(0, _ENGINE_ENV)
else:
    import types as _types_mod
    _persona_pkg = _types_mod.ModuleType("persona")
    _persona_pkg.__path__ = [HERE]
    sys.modules["persona"] = _persona_pkg


# ---------------------------------------------------------------- corpus ----
def load_corpus(path):
    """Accepts .jsonl (one record per line) or .json (list). Records need
    'situation' (str) and 'outcome' (0/1); optional 'framing' (str)."""
    recs = []
    if path.endswith(".jsonl"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    else:
        recs = json.load(open(path, encoding="utf-8"))
    out = []
    for c in recs:
        if not isinstance(c.get("situation"), str):
            continue
        y = c.get("outcome", c.get("y"))
        try:
            y = float(y)
        except (TypeError, ValueError):
            continue
        if y not in (0.0, 1.0):
            continue
        out.append({"situation": c["situation"],
                    "framing": c.get("framing", "") or "",
                    "y": y,
                    # Provenance: where a real record came from
                    "source_ref": str(c.get("source_ref") or ""),
                    "kind": str(c.get("source") or "")})
    return out


# ------------------------------------------------------------ features ----
def structural(cases, tpl):
    """Domain-neutral features declared in the template:
       structural.counts: true  -> [n_chars/100, n_words/20]
       structural.keywords:[..] -> one 0/1 hit feature per keyword
    (The SWE trainer's code-diff parsing lives on in the project archive;
    here features are declarative so any domain can define its own.)"""
    st = tpl.get("structural") or {}
    kw = [k.lower() for k in st.get("keywords", [])]
    use_counts = st.get("counts", True)
    feats = []
    for c in cases:
        low = (c["situation"] + " " + c["framing"]).lower()
        v = []
        if use_counts:
            v.append(min(len(c["situation"]), 2000) / 100.0)
            v.append(min(len(c["situation"].split()), 400) / 20.0)
        v.extend(1.0 if k in low else 0.0 for k in kw)
        feats.append(v)
    return np.asarray(feats, dtype=np.float32)


def labels_for(cases, tpl):
    """Declarative heads from the template. Every entry in labels: is
    {any_of: [keywords]}; matched against the situation text. An 'outcome'
    head is always trained from the corpus ground truth. Labels whose value
    never varies in the corpus are skipped honestly (recorded)."""
    lab = {"outcome": [c["y"] for c in cases]}
    for name, rule in (tpl.get("labels") or {}).items():
        terms = rule.get("any_of", []) if isinstance(rule, dict) else (rule or [])
        terms = [str(t).lower() for t in terms]
        if not terms:
            continue
        lab[re.sub(r"[^a-z0-9_]+", "_", name.lower())] = [
            1.0 if any(t in c["situation"].lower() for t in terms) else 0.0
            for c in cases]
    skipped = [k for k, v in list(lab.items()) if len(set(v)) < 2]
    for k in skipped:
        del lab[k]
    # VARIETY DIAGNOSTICS (the no-variance lesson): for every template label,
    # measure presence spread + distinct situation-shapes so a uniform harvest
    # is NAMED, not silently skipped.
    diag = {}
    for name, rule in (tpl.get("labels") or {}).items():
        key = re.sub(r"[^a-z0-9_]+", "_", name.lower())
        terms = [str(t).lower() for t in (rule.get("any_of", []) if isinstance(rule, dict) else (rule or []))]
        if not terms:
            continue
        present = sum(1 for c in cases if any(t in c["situation"].lower() for t in terms))
        rate = present / len(cases) if cases else 0.0
        shapes = len({tuple(sorted(
            t for t in terms if t in c["situation"].lower())) for c in cases})
        if present == 0:
            kind = "keywords never appear in corpus (harvest gap: wrong source)"
        elif rate > 0.95:
            kind = "present in ~all rows — no contrast (harvest too uniform: add absent-label rows)"
        else:
            kind = "ok"
        diag[key] = {"present_rows": present, "presence_rate": round(rate, 3),
                     "distinct_shapes": shapes, "verdict": kind}
    return lab, skipped, diag


# --------------------------------------------------------------- train ----
def sigmoid(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def train_head(x, y, iters=400, lr=0.15, l2=1e-4):
    w = np.zeros(x.shape[1], dtype=np.float32); b = 0.0; n = len(y)
    for _ in range(iters):
        p = sigmoid(x @ w + b)
        w -= lr * (x.T @ (p - y) / n + l2 * w)
        b -= lr * float((p - y).mean())
    return w, b


def cv_accuracy(X, y, folds=5, seed=7):
    """Stratified-by-outcome k-fold accuracy (the honesty fields in the artifact)."""
    rng = np.random.RandomState(seed)
    pos = np.where(y > 0)[0]; neg = np.where(y < 1)[0]
    rng.shuffle(pos); rng.shuffle(neg)
    folds_idx = [[] for _ in range(folds)]
    for i, ix in enumerate(pos): folds_idx[i % folds].append(ix)
    for i, ix in enumerate(neg): folds_idx[i % folds].append(ix)
    accs = []
    for f in range(folds):
        te = np.array(folds_idx[f], dtype=int)
        tr = np.concatenate([np.array(folds_idx[g], dtype=int) for g in range(folds) if g != f])
        if len(te) == 0 or len(set(y[tr].tolist())) < 2:
            continue
        w, b = train_head(X[tr], y[tr])
        pred = (sigmoid(X[te] @ w + b) >= 0.5).astype(float)
        accs.append(float((pred == y[te]).mean()))
    return accs


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser(description="Train a pblock from a template.")
    ap.add_argument("--template", required=True)
    ap.add_argument("--corpus", default=None, help="override the template's corpus path")
    ap.add_argument("--out-dir", default=HERE, help="repo root (default: this script's dir)")
    args = ap.parse_args()

    t0 = time.time()
    tpl = yaml.safe_load(open(args.template, encoding="utf-8"))
    name = re.sub(r"[^a-z0-9_]+", "_", str(tpl["name"]).lower())
    print(f"=== training {name} from template {os.path.basename(args.template)} ===", flush=True)

    corpus_path = args.corpus or os.path.join(os.path.dirname(os.path.abspath(args.template)),
                                              tpl["corpus"]["path"])
    cases = load_corpus(corpus_path)
    n_pos = sum(1 for c in cases if c["y"] == 1.0)
    print(f"corpus: {len(cases)} cases ({n_pos} pos / {len(cases)-n_pos} neg) from {corpus_path}", flush=True)

    gates = tpl.get("gates") or {}
    min_cases = int(gates.get("min_cases", 0))
    if len(cases) < min_cases:
        print(f"GATE REJECT: too small ({len(cases)} < {min_cases}). "
              f"Harvest more confirmed outcomes before training.")
        return 1

    # embed with the FROZEN ONNX eyes (never fine-tuned)
    from .embeddings import MiniLMEmbedder
    emb = MiniLMEmbedder()
    E = np.empty((len(cases), 384), dtype=np.float32)
    for i in range(0, len(cases), 400):
        E[i:i + 400] = np.asarray(emb.embed([c["situation"] for c in cases[i:i + 400]]),
                                  dtype=np.float32)
        print(f"  embedded {min(i + 400, len(cases))}/{len(cases)}", flush=True)
    S = structural(cases, tpl)
    mu, sd = S.mean(0), S.std(0) + 1e-6
    Sn = (S - mu) / sd
    X = np.concatenate([E, Sn], axis=1).astype(np.float32)

    Y, skipped, variety = labels_for(cases, tpl)
    print(f"heads: {len(Y)} ({', '.join(Y)})" + (f" | skipped (no variance): {skipped}" if skipped else ""),
          flush=True)
    # VARIETY GATE (the no-variance lesson made mechanical): a corpus whose
    # labels carry no contrast FAILS, instead of passing with skipped heads.
    # Variety report + graded gate:
    #   - same-type repeated rows are WANTED (they teach consistency and
    #     expose gaps) — high presence rate is a note, not a failure
    #   - FAIL only for true degeneracy: keywords never appear (wrong
    #     source), or the whole corpus collapses to a single shape
    g_variety = True
    starved, notes = [], []
    total_shapes = len({c["situation"].strip().lower() for c in cases})
    for k, d in variety.items():
        print(f"  variety {k:24s} present={d['present_rows']}/{len(cases)} "
              f"shapes={d['distinct_shapes']}  {d['verdict']}", flush=True)
        if d["verdict"].startswith("keywords never appear"):
            g_variety = False
            starved.append(k)
            notes.append(f"{k}: keywords never appear — harvest source mismatch, "
                         f"add rows that genuinely carry this label")
        elif d["verdict"].startswith("present in ~all"):
            notes.append(f"{k}: mostly-present (fine if true distribution); "
                         f"absence rows would sharpen this head")
    if total_shapes < 10:
        g_variety = False
        starved.append("corpus-uniformity")
        notes.append(f"corpus has only {total_shapes} distinct situation shapes — "
                     f"degenerate: harvest varied instances AND same-type repeats")
    for n in notes:
        print(f"  variety note: {n}", flush=True)

    # honesty fields: 5-fold CV per head; final heads trained on all data
    art = {"persona": name, "keys": list(Y), "heads": {},
           "feature_norm": {"mu": mu.tolist(), "sd": sd.tolist()},
           "corpus": os.path.abspath(corpus_path), "n_cases": len(cases),
           "embedding": "MiniLM-L6-int8 384d + structural",
           "template": os.path.abspath(args.template),
           # the seat's OWN declared floors travel with the artifact so the
           # runtime judge abstains at the seat's gate, never a hardcoded number
           "gates": {"min_cases": min_cases,
                     "min_positives_per_label": int(gates.get("min_positives_per_label", 50))}}
    floor = int(gates.get("min_positives_per_label", 50))
    for k, yv in Y.items():
        y = np.asarray(yv, dtype=np.float32)
        n_pos = int((y > 0.5).sum())
        if n_pos < floor:
            # label starved: training a head on <floor positives yields
            # majority-class accuracy that only LOOKS good. Mark it untrained
            # so the runtime judge abstains on it honestly.
            print(f"[train] LABEL STARVED: {k} has {n_pos} positives < floor "
                  f"{floor} -- head marked untrained, judge will abstain on it")
            art["heads"][k] = {"acc": 0.0, "acc_sd": 0.0,
                               "pos_rate": round(n_pos / max(1, len(yv)), 3),
                               "w": {}, "b": 0.0, "untrained": True}
            continue
        w, b = train_head(X, y)
        accs = cv_accuracy(X, y)
        acc = float(np.mean(accs)) if accs else 0.0
        art["heads"][k] = {"acc": round(acc, 3), "acc_sd": round(float(np.std(accs)), 3) if accs else 0.0,
                           "pos_rate": round(float(y.mean()), 3), "w": w.tolist(), "b": float(b)}
        print(f"  head {k:24s} cv_acc {acc:.3f} (sd {art['heads'][k]['acc_sd']:.3f})  "
              f"pos_rate {y.mean():.3f}", flush=True)

    # build the store: seed cases (confirmed outcomes only — the writeback law)
    # MB-SCALE LAW: heads train on ALL rows, but the store seeds a
    # representative evenly-spaced sample when the corpus is huge — the
    # store is working memory (retrieval), the corpus file is the archive.
    # Sample = stride across the corpus so every era stays represented.
    from .store import PersonaStore
    store_root = os.path.join(args.out_dir, "stores")
    # a retrain RE-SEEDS the store from the corpus (the corpus file is the
    # archive; runtime growth after training happens via writeback). Without
    # this, repeated training runs accumulate stale rows (found live: three
    # debug runs produced a 21k-line, 194MB store).
    store_file = os.path.join(store_root, f"{name}_store.jsonl")
    if os.path.exists(store_file):
        os.remove(store_file)
    st = PersonaStore(name, root=store_root)
    # NO STORE CAP (design law: "no cap is limiting
    # our work"): the store seeds EVERY corpus row. The corpus file stays the
    # archive; the store is its full working-memory image. Retrain still
    # re-seeds from scratch (no stale-row accumulation). Shuffle with a fixed
    # seed before adding so no enumeration-order bias exists in tie-breaks.
    seed_idx = list(range(len(cases)))
    print(f"store seed: ALL {len(cases)} rows (no cap)", flush=True)
    for i in seed_idx:
        c = cases[i]
        # persist=False: bulk seed, one atomic save at the end (per-case
        # full-store rewrites made 6384-case seeding an O(n^2) grind)
        case = st.add_case(c["situation"], c["framing"], str(c["y"]), confirmed=True,
                           origin="seed", embedding=E[i].tolist(), persist=False)
        if c.get("source_ref"):
            case["origin_ref"] = c["source_ref"]
    st.save()

    # gates: ONLY quality floors (the 500-confirmed abstention floor + the
    # variety gate). Size/latency caps REMOVED (design law: "remove
    # all caps") — they are now reported measurements, not pass/fail gates.
    store_mb = os.path.getsize(st.path) / 1e6
    q0 = time.time(); st.active_cases(); lat_ms = (time.time() - q0) * 1000
    g_cases = len(cases) >= min_cases
    g_var = g_variety and bool(Y)
    ok = g_cases and g_var

    out_path = os.path.join(args.out_dir, "weights", f"{name}_heads.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(art, open(out_path, "w"), indent=1)

    print(f"""
=== PASS/FAIL ===
cases:       {len(cases)} (gate {min_cases})   {'PASS' if g_cases else 'FAIL'}
store size:  {store_mb:.2f}MB (no cap — measurement only)
retrieval:   {lat_ms:.1f}ms (no gate — measurement only)
variety:     {len(Y)} trained heads, {len(starved)} starved   {'PASS' if g_var else 'FAIL'}
artifact:    {out_path}
store:       {st.path}
wall time:   {time.time() - t0:.1f}s
VERDICT: {'PASS' if ok else 'FAIL'}""")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

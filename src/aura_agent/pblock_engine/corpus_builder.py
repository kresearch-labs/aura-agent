"""corpus_builder.py — the enumeration engine of pblocks_code.

The doc law (CORPUS_TRAINING_METHODOLOGY.md step 5): "Never pick the number
first and fill toward it." This module is the code for that law. A seat
declares its OWN decision space in a corpus-spec (yml or dict):

    craft_truths: the seat's practice rules, written from the domain
    negations: the documented failure forms (optional; rotates)
    dimensions: name -> list of values (their product = decision cells)
    real_records: [(account, outcome)] — -1 published facts
    vocabulary: [(present_phrase, absent_phrase)] — label contrast rows

Rows = cells x truths + interaction + edge + vocabulary + real_records

...and build_corpus crosses them. NO row count appears anywhere in this file:
the count is a RESULT of the seat's declared space. A narrow territory lands
small; a broad one lands large. The trainer's floors (declared in the seat's
template) are the only thresholds, and size/latency are never gates.
"""
from __future__ import annotations
import json, random, itertools
from pathlib import Path


def _cell_label(cell: tuple) -> str:
    return "/".join(str(v) for v in cell)


def build_corpus(spec: dict, out_path: str | None = None) -> list[dict]:
    """Cross a seat's declared decision space into corpus rows.

    spec keys (all domain-declared, nothing hardcoded here):
      craft_truths: list[str] REQUIRED — the seat's practice rules
      negations: list[str] failure forms (optional; generic rotation if absent)
      dimensions: dict REQUIRED — name -> list of values (product = cells)
      real_records: list [(situation, outcome)] — -1 real facts
      real_framing: str framing text for real-record rows
      vocabulary: list [(present_phrase, absent_phrase)] pairs
      interaction_format / edge_format: str templates (optional)
      persona: str optional prefix in situation text
      seed: int fixed shuffle seed (default 20261021)
    """
    truths = [str(t) for t in (spec.get("craft_truths") or [])]
    if not truths:
        raise ValueError("corpus_spec.craft_truths is empty — a seat must declare "
                         "its own practice rules; the count falls out of the "
                         "enumeration, never from a hardcoded range")
    dims = spec.get("dimensions") or {}
    if not dims:
        raise ValueError("corpus_spec.dimensions is empty — declare the axes the "
                         "judgment actually varies across")
    negs = [str(n) for n in (spec.get("negations") or [])] or [
        "the account carries no verifiable source",
        "the record contradicts its own stated facts",
        "the claim cannot be traced to any documentation",
    ]
    persona = str(spec.get("persona", "")).strip()
    prefix = f"{persona}: " if persona else ""
    inter_fmt = spec.get("interaction_format",
                         "interaction: {cell} — the seat under review expects: {truth}")
    edge_fmt = spec.get("edge_format",
                        "edge case [{cell}]: {neg} — a documented failure mode of this territory")

    rows: list[dict] = []

    def add(situation, framing, outcome, source, ref=""):
        row = {"situation": situation, "framing": framing,
               "outcome": outcome, "source": source}
        if ref:
            row["source_ref"] = ref
        rows.append(row)

    # ---- 1: real records (published facts, outcome from the record) ----
    # accepted forms: [situation, outcome] pairs, or dicts with situation/outcome
    # and optional per-row framing (e.g. a source pmcid) — provenance stays with
    # the row it describes.
    default_real_framing = spec.get("real_framing",
        "real-record check (outcome resolved from the documented record)")
    # sidecar form: real_records_file points at a JSONL of dict rows
    # ({situation, outcome, framing?}) — keeps large real archives out of the
    # yml while provenance stays with each row.
    real_items = list(spec.get("real_records") or [])
    sidecar = spec.get("real_records_file")
    if sidecar:
        from pathlib import Path as _P
        p = _P(sidecar)
        if not p.is_absolute():
            # resolve relative to the spec file when build_from_spec_file used one
            base = spec.get("_spec_dir")
            if base:
                p = _P(base) / p
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    real_items.append(json.loads(line))
    for item in real_items:
        if isinstance(item, dict):
            sit, y = str(item["situation"]), float(item["outcome"])
            framing = str(item.get("framing") or default_real_framing)
            ref = str(item.get("source") or item.get("source_ref") or "")
        else:
            sit, y = item[0], float(item[1])
            framing = default_real_framing
            ref = str(item[2]) if len(item) > 2 else ""
        if spec.get("require_sources") and not ref:
            raise ValueError("a real record has no source — every real example must say "
                             f"where it came from: {sit[:80]!r}")
        add(sit, framing, y, "real-record", ref)

    # ---- 3: craft truths x decision cells, outcomes alternating ----
    dim_names = list(dims.keys())
    cells = list(itertools.product(*[list(v) for v in dims.values()]))
    for cell in cells:
        label = _cell_label(cell)
        for i, truth in enumerate(truths):
            good = (i % 2 == 0)
            if good:
                add(f"{prefix}check, {label}: a case under review {truth} "
                    f"— the practice holds in this record",
                    f"{label}: {truth}", 1.0, "constructed-wave3")
            else:
                neg = negs[i % len(negs)]
                add(f"{prefix}check, {label}: a case under review {neg} "
                    f"— the practice fails in this record",
                    f"{label}: {neg}", 0.0, "constructed-wave3")
        # interaction row per cell: the seat's first truth as expectation
        add(inter_fmt.format(cell=label, truth=truths[0]),
            "interaction (declared expectation)", 1.0, "constructed-wave3")
        # edge row per cell: documented failure modes, rotated deterministically
        edge_i = (cells.index(cell) + len(truths)) % len(negs)
        add(edge_fmt.format(cell=label, neg=negs[edge_i]),
            "edge lesson (declared failure mode)", 0.0, "constructed-wave3")

    # ---- vocabulary rows: present + absent per declared phrase pair ----
    for pair in (spec.get("vocabulary") or []):
        present, absent = pair[0], pair[1]
        for cell in cells:
            label = _cell_label(cell)
            add(f"vocabulary pass {label}: {present}",
                "vocabulary present row", 1.0, "constructed-wave3")
            add(f"vocabulary pass {label}: {absent}",
                "vocabulary absent row", 0.0, "constructed-wave3")

    # ---- fixed-seed shuffle: no enumeration-order bias in tie-breaks ----
    random.Random(int(spec.get("seed", 20261021))).shuffle(rows)

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return rows


def build_from_spec_file(spec_path: str, out_path: str | None = None) -> list[dict]:
    """Load a corpus-spec yml (the seat's own declared space) and build."""
    import yaml
    from pathlib import Path as _P
    spec = yaml.safe_load(open(spec_path, encoding="utf-8")) or {}
    spec["_spec_dir"] = str(_P(spec_path).resolve().parent)
    return build_corpus(spec, out_path=out_path)

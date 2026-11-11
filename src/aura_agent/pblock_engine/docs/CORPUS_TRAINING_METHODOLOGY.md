# How the Training Corpus Is Made — Generic Methodology

How a training corpus gets built for a small model trained on one specific
task — the three-wave method, in detail. Domain-agnostic: the same procedure
applies to any field; only the domain material changes. Nothing here depends
on any prior campaign, dataset, or worked example: the method is the core.

---

## 0. What the corpus must be

A training corpus is a list of examples, each one:

    situation:  the input the model will actually see at run time
    framing:    which lesson/lens this example teaches
    outcome:    1.0 or 0.0 — what actually happened, never invented

Three laws govern every row:

1. **Real outcomes only.** Every label comes from a measured result or a
   published record. If nobody actually verified the outcome, it does not
   go in. Outcomes are never scripted to fit the lesson.
2. **Variety.** Every lesson needs three row kinds:
     - PRESENT rows — the lesson's pattern genuinely there
     - ABSENT rows — genuine contrast (the head must learn absence)
     - SAME-TYPE repeats — teach consistency, expose gaps
   A corpus with fewer than 10 distinct situation shapes is degenerate
   and fails the gate mechanically.
3. **Provenance.** Every corpus file carries a sidecar record: source,
   generator + seed, harvest date, law followed ("fresh harvest, no
   reuse of previous snapshots").

---

## Wave 1 — The seed: prove you can generate verified truth

**Problem it solves:** hand-writing thousands of verified examples is
impossible; the corpus must be *produced* with outcomes nobody had to
make up.

**Method (two routes, use either or both):**

**1a. The walk (for domains with a checkable artifact or process).**
- Take the real artifact the judgment is about — an actual working thing
  (a schedule, a codebase, a plan, a process, a dataset).
- Build two small machine components:
    - a MUTATOR — perturbs the artifact systematically (swaps parameters,
      changes structure, breaks one thing at a time);
    - a GRADER — a deterministic checker scoring each variant with a real
      heuristic (does it still work? is quality better or worse?).
- Walk: mutate -> grade -> compare to parent -> record. Outcome is
  native ground truth ("grade improved 0.61 -> 0.68" is a measurement,
  not an opinion).
- Fix ONE random seed for the whole run — every step exactly reproducible.

**1b. The field's own records (for judgments about the world).**
- Harvest REAL records the field itself keeps (published outcomes, case
  reports, transaction histories, verified incident logs). Where the
  domain archives outcomes, Wave 1 is harvesting them — not inventing
  them.

**Yield:** a first verified slice. Proves the pipeline; not depth yet.

## Wave 2 — The repair: speak the model's actual vocabulary

**Problem it solves:** the label audit finds the declared judgment
keywords never appear in the harvested text — the heads would train on
nothing. Wrong source, wrong corpus.

**Method:**

- Audit the harvest against the template's declared labels (the trainer
  prints this: `keywords never appear`, presence rate per label).
- Re-run the Wave-1 machinery, but emit rows THROUGH each lesson's
  declared vocabulary, outcomes still resolved from measured behaviour:
    - long stretches pinned at the grade floor -> genuine "no progress"
      rows (outcome 0.0);
    - measured properties of the artifact/record (latency, error count,
      cost, utilization) -> genuine band rows;
    - repeated runs reproducing identical results -> genuine
      determinism rows.
- The fix is to the HARVEST, never to the labels after the fact.

**Yield:** rows whose vocabulary and source finally match; still lane-
deep, breadth-shallow.

## Wave 3 — The depth pass: enumerate the decision space

**Problem it solves:** coverage. Not "more rows" as an arbitrary number —
the whole space of situations the model will face.

**Method:**

1. ENUMERATE THE CRAFT TRUTHS. Write out the 25-30 real rules
   practitioners of the task actually use — each a genuine principle,
   drawn from real practice (published guidelines, expert procedure,
   the field's own standards), never invention.
2. ENUMERATE THE CONTEXT DIMENSIONS the judgment actually varies across —
   the factors that change which rule applies and how. Their cross
   product is the decision space: one CELL per combination.
3. CROSS THEM. Every craft truth instantiated in every decision cell,
   plus interaction rows and edge-case rows from a per-task table of
   REAL failure modes.
4. ALTERNATE OUTCOMES at generation time — each truth once in its good
   form, once in its failed form — so present/absent contrast is baked
   in at birth.
5. LET THE SIZE FALL OUT. "Not a grid cap: each task enumerates its
   actual decision space... Row count falls out of the enumeration."
   Nobody chooses the row count — it is what truths x cells + interaction
   + edge rows adds up to. A domain with a bigger decision space gets a
   bigger corpus automatically; a narrow territory may legitimately
   land near 1,000 rows, a broad one near 100,000. Never pick the
   number first and fill toward it.

**Harvesting from large real datasets (when the field's records exceed
what the corpus needs):** never take the first N rows — that is a
time- or source-biased slice. Stratify by the decision cells: sample
each cell until its confirmed cases fill the floors (the rarest label
sets the pace), drawing at random across the full source. The unused
remainder stays in the archive with provenance.

**The corpus is a bootstrap, not the education.** Heads are born from the
corpus; the pblock's real education is the live loop — confirmed outcomes
write back through the two-stage law, and the store retrains. The corpus
only has to be big enough that no head is born starving.

---

## What happens at training time (why the waves are shaped this way)

- **Frozen eyes.** Every situation is embedded to 384 dims by a fixed,
  never-fine-tuned encoder — the SAME converter at training and at run
  time, so the model's perception never drifts.
- **One small logistic head per lesson** (+ an outcome head), trained on
  ALL corpus rows; 5-fold cross-validation accuracy written into the
  artifact as an honesty field.
- **Variety gate, mechanical:** keywords-never-appear = FAIL (wrong
  source); whole-corpus one-shape = FAIL; mostly-present = note only
  (same-type repeats are wanted).
- **Archive vs working memory:** heads train on every row; the runtime
  retrieval store seeds a representative evenly-strided sample (1,200).
  The corpus file is the permanent archive; the store re-seeds from it
  on every retrain — nothing is lost.
- **Gates, mechanical:** minimum case count (500 confirmed floor),
  50-minority-per-label floor, variety gate, 10-shape floor, per-label
  presence <= 95%. PASS/FAIL printed. No pass-by-mercy. Size and latency
  are reported measurements, never gates.

---

## The one-line recipe

Wave 1 proves you can generate or harvest verified truth at all (real
artifact / real records + real grader + fixed seed). Wave 2 makes the
harvest speak the model's actual vocabulary. Wave 3 enumerates the entire
decision space so coverage — not a headcount — sets the depth, and the
row count falls out of the enumeration. Then the trainer turns the corpus
into small logistic heads with published CV scores, and the corpus file
stays as the archive the store re-seeds from.

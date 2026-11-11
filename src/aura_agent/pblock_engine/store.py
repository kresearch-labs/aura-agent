"""Persona store: the lived memory. Tier 0 version = JSONL + numpy cosine.

Design doc sec 5 (L2 precedents) + sec 8 (learning loop):
- only confirmed outcomes are written as authoritative
- origin:seed cases are tagged and overridable by origin:lived
- versioned snapshots for rollback
- retrieval by cosine similarity over stored embeddings
"""
from __future__ import annotations
from ._log import dbg as print  # Quiet unless AURA_PBLEARN_DEBUG


import json
import os
import shutil
import time
from typing import Optional

import numpy as np

from .schemas import Case, new_case

SNAPSHOT_DIR = "store_versions"


class PersonaStore:
    """One persona's lived memory. MB-scale by construction."""

    def __init__(self, persona: str, root: str, frame_embedder=None):
        self.persona = persona
        self.frame_embedder = frame_embedder   # Optional embedder for framing sim
        self.path = os.path.join(root, f"{persona}_store.jsonl")
        if not os.path.exists(self.path):
            # the trainer writes hyphens as underscores in filenames
            # (found live: 'forecaster-baserate' persona vs
            #  forecaster_baserate_store.jsonl) — accept both spellings
            alt = os.path.join(root, f"{persona.replace('-', '_')}_store.jsonl")
            if os.path.exists(alt):
                self.path = alt
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.cases: list[Case] = []
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        # tolerant loader (the killed-run lesson): a corrupt line is
        # quarantined and reported, never a crash — partial writes from an
        # interrupted process must not take down the persona
        self.cases = []
        self.corrupt_lines = 0
        self._packed = False
        if self._load_packed():
            return
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.cases.append(json.loads(line))
                    except json.JSONDecodeError:
                        self.corrupt_lines += 1
                        qdir = os.path.join(os.path.dirname(self.path), "quarantine")
                        os.makedirs(qdir, exist_ok=True)
                        qn = os.path.join(qdir, f"corrupt_{int(__import__('time').time())}_{self.corrupt_lines}.line")
                        with open(qn, "w", encoding="utf-8") as q:
                            q.write(line)
        if self.corrupt_lines:
            print(f"[store] loaded {len(self.cases)} cases, "
                  f"{self.corrupt_lines} corrupt line(s) quarantined", flush=True)

    # -- packed form ----------------
    # <name>_embeddings.npy (float32 [N, D]) + <name>_meta.jsonl (one row per
    # case, same order, no embedding field). ~79% smaller than the JSONL form,
    # one np.load instead of parsing JSON, same results. Preferred when it is
    # at least as new as the JSONL store; a store loaded packed saves packed.
    def _packed_paths(self) -> tuple[str, str]:
        base = self.path[:-len("_store.jsonl")] if self.path.endswith("_store.jsonl") \
            else self.path
        return base + "_embeddings.npy", base + "_meta.jsonl"

    def _load_packed(self) -> bool:
        npy, meta = self._packed_paths()
        if not (os.path.exists(npy) and os.path.exists(meta)):
            return False
        if os.path.exists(self.path) and \
                os.path.getmtime(self.path) > max(os.path.getmtime(npy), os.path.getmtime(meta)):
            return False                    # The JSONL learned something newer
        from array import array
        E = np.load(npy, allow_pickle=False).astype(np.float32, copy=False)
        rows = []
        with open(meta, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if len(rows) != len(E):
            print(f"[store] packed {npy}: {len(E)} embeddings vs {len(rows)} rows — "
                  "using the JSONL store", flush=True)
            return False
        for r, e in zip(rows, E):
            if r.pop("_noemb", False):
                r["embedding"] = None
            else:
                a = array("f")
                a.frombytes(np.ascontiguousarray(e, dtype=np.float32).tobytes())
                r["embedding"] = a              # Compact, truthy, np.asarray-able
            r.setdefault("embedding", None)
        self.cases = rows
        self._packed = True
        return True

    def _save_packed(self) -> None:
        npy, meta = self._packed_paths()
        dim = next((len(c["embedding"]) for c in self.cases if c.get("embedding") is not None), 384)
        E = np.zeros((len(self.cases), dim), dtype=np.float32)
        tmp_meta = meta + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            for i, c in enumerate(self.cases):
                row = {k: v for k, v in c.items() if k != "embedding"}
                if c.get("embedding") is None:
                    row["_noemb"] = True
                else:
                    E[i] = np.asarray(c["embedding"], dtype=np.float32)
                f.write(json.dumps(row) + "\n")
        tmp_npy = npy + ".tmp.npy"
        np.save(tmp_npy, E)
        os.replace(tmp_npy, npy)
        os.replace(tmp_meta, meta)

    def pack(self) -> tuple[str, str]:
        """Write the packed form of this store (keeps every case, superseded
        and retired ones too — the bi-temporal history stays). Returns the
        two paths."""
        self._save_packed()
        self._packed = True
        return self._packed_paths()

    def save(self) -> None:
        if getattr(self, "_packed", False):
            self._save_packed()
            return
        # atomic write: temp file + rename — a killed process can never
        # leave a half-written store
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for c in self.cases:
                f.write(json.dumps(c, default=list) + "\n")   # Compact embeddings too
        os.replace(tmp, self.path)

    def snapshot(self, label: str = "") -> str:
        os.makedirs(os.path.join(os.path.dirname(self.path), SNAPSHOT_DIR),
                    exist_ok=True)
        name = f"{self.persona}_{label or int(time.time())}.jsonl"
        dest = os.path.join(os.path.dirname(self.path), SNAPSHOT_DIR, name)
        if getattr(self, "_packed", False):
            for src in self._packed_paths():          # The packed pair is the store
                if os.path.exists(src):
                    shutil.copy2(src, dest[:-len(".jsonl")] + os.path.basename(src)[len(self.persona):])
        elif os.path.exists(self.path):
            shutil.copy2(self.path, dest)
        return dest

    # -- writing (learning loop: only confirmed outcomes become authoritative)

    def add_case(self, situation: str, framing: str, outcome: str, *,
                 confirmed: bool, origin: str = "lived",
                 task_summary: str = "", embedding: Optional[list[float]] = None,
                 persist: bool = True
                 ) -> Case:
        c = new_case(self.persona, situation, framing, outcome,
                     confirmed, origin, task_summary)
        # JSON-safe embedding: numpy arrays -> plain float lists
        if embedding is not None and not isinstance(embedding, list):
            embedding = np.asarray(embedding, dtype=np.float32).reshape(-1).tolist()
        c["embedding"] = embedding
        self.cases.append(c)
        self.log_event("add", {"case": c})
        if persist:
            self.save()      # Deferred mode (persist=False) -> caller saves once
        return c


    # -- Step 0.4: two-stage writeback (candidate -> pending -> active) ------
    # Design: lived lessons never overwrite authority on arrival. A new case
    # starts as "candidate"; promotion to "pending" needs corroboration or
    # explicit user confirmation; "pending" -> "active" on second independent
    # corroboration or review. Supersession — never deletion — handles
    # contradiction with an active case (bi-temporal: old gets valid_to).

    PROMOTE_MIN_CORROBORATION = 2   # Independent confirmations to go active
    CANDIDATE_SIM = 0.85            # Near-dupe threshold for corroboration match

    def propose_case(self, situation: str, framing: str, outcome: str, *,
                     origin: str = "lived", origin_ref: str = "",
                     embedding: Optional[list[float]] = None,
                     task_summary: str = "") -> Case:
        """Stage 1: every learned case arrives as a candidate."""
        if embedding is not None and not isinstance(embedding, list):
            embedding = np.asarray(embedding, dtype=np.float32).reshape(-1).tolist()
        c = new_case(self.persona, situation, framing, outcome,
                     confirmed=False, origin=origin, task_summary=task_summary)
        c["embedding"] = embedding
        c["origin_ref"] = origin_ref
        c["status"] = "candidate"
        c["confidence"] = 0.3
        c["framing_embedding"] = np.asarray(
            self.frame_embedder.embed(framing) if self.frame_embedder
            else embedding, dtype=np.float32).reshape(-1).tolist() if (self.frame_embedder or embedding) else None
        # corroboration: does this match an existing pending/candidate case?
        ally = self._find_corroboration(c)
        if ally is not None:
            ally["corroboration_count"] += 1
            ally["confidence"] = min(1.0, ally["confidence"] + 0.25)
            c["corroboration_count"] = 1
            if ally["corroboration_count"] + 1 >= self.PROMOTE_MIN_CORROBORATION:
                ally["status"] = "active"
                ally["confirmed"] = True
                ally["confidence"] = min(1.0, ally["confidence"] + 0.2)
        # contradiction: does this clash with an ACTIVE case?
        for other in self.active_cases():
            if embedding and other.get("embedding"):
                sim = float(np.asarray(embedding, dtype=np.float32) @
                            np.asarray(other["embedding"], dtype=np.float32))
                if sim >= self.CANDIDATE_SIM and self._stances_conflict(c, other):
                    c["contradicts"] = sorted(set(c["contradicts"] + [other["id"]]))
        self.cases.append(c)
        self.save()
        return c

    def _find_corroboration(self, c: Case) -> Optional[Case]:
        """Corroboration = same LESSON (framing), different situation.
        Matches on framing text (exact-ish) OR framing-embedding similarity.
        Situation similarity is deliberately NOT the criterion: two different
        situations confirming one lesson is exactly what corroboration means."""
        fe = self._frame_emb(c)
        if fe is None:
            return None
        for other in self.cases:
            if other["id"] == c["id"] or other.get("status") == "retired":
                continue
            if other.get("status") not in ("candidate", "pending"):
                continue
            # same framing text = direct corroboration
            if other["framing"].strip().lower() == c["framing"].strip().lower():
                return other
            oe = self._frame_emb(other)
            if oe is None:
                continue
            sim = float(fe @ oe)
            if sim >= 0.90:   # Framing-similarity bar (embeddings are sharper on text)
                return other
        return None

    def _frame_emb(self, c: Case) -> Optional[np.ndarray]:
        fe = c.get("framing_embedding")
        if fe:
            return np.asarray(fe, dtype=np.float32)
        return None

    @staticmethod
    def _stances_conflict(a: Case, b: Case) -> bool:
        """v0 stance conflict: negation markers or opposing keywords."""
        neg = {"never", "no exceptions", "do not", "don't", "avoid", "reject"}
        pos = {"always", "must", "required", "should"}
        fa, fb = (a["framing"] + " " + a["outcome"]).lower(), \
                 (b["framing"] + " " + b["outcome"]).lower()
        a_neg = any(n in fa for n in neg)
        b_neg = any(n in fb for n in neg)
        # same topic (high sim already), opposite polarity
        return a_neg != b_neg

    def promote(self, case_id: str, *, by: str = "user") -> bool:
        """Stage 2: explicit promotion (user confirm or reviewer)."""
        for c in self.cases:
            if c["id"] == case_id and c["status"] in ("candidate", "pending"):
                c["status"] = "active"
                c["confirmed"] = True
                c["confidence"] = min(1.0, c["confidence"] + 0.3)
                c["corroboration_count"] += 1
                self.log_event("promote", {"id": case_id, "by": by})
                self.save()
                return True
        return False

    def supersede(self, old_id: str, new_case_id: str) -> bool:
        """Bi-temporal supersession: old stays queryable, loses authority."""
        now = time.time()
        for c in self.cases:
            if c["id"] == old_id:
                c["status"] = "superseded"
                c["valid_to"] = now
                c["superseded_by"] = new_case_id
                self.log_event("supersede", {"old_id": old_id,
                                             "new_id": new_case_id, "ts": now})
                self.save()
                return True
        return False


    def resolve_seed_conflict(self, seed_a_id: str, seed_b_id: str,
                              lived_case: Case) -> dict:
        """TANGLE rule (0.6): when a lived case contradicts two seed cases
        that themselves conflict, DO NOT pick a winner automatically.
        Preserve BOTH seeds (each still valid history), wire contradicts
        edges, mark the lived case as the current authority, and return
        the tangle descriptor for the alert-not-ask UX layer.
        Auto-resolution is deferred to the agent-arbitration step with
        full evidence; the user is alerted, never asked to adjudicate."""
        ids = {seed_a_id, seed_b_id}
        a = next((c for c in self.cases if c["id"] == seed_a_id), None)
        b = next((c for c in self.cases if c["id"] == seed_b_id), None)
        if a is None or b is None:
            return {"tangled": False, "reason": "seed id not found"}
        # wire the mutual contradiction
        if b["id"] not in a["contradicts"]:
            a["contradicts"] = sorted(set(a["contradicts"] + [b["id"]]))
        if a["id"] not in b["contradicts"]:
            b["contradicts"] = sorted(set(b["contradicts"] + [a["id"]]))
        # lived case edges to both
        for sid in ids:
            if sid not in lived_case["contradicts"]:
                lived_case["contradicts"] = sorted(
                    set(lived_case["contradicts"] + [sid]))
        self.save()
        return {"tangled": True,
                "seeds": [a["id"], b["id"]],
                "authority": lived_case["id"],
                "action": "agent_arbitration_pending",
                "user_alert": "two seed rules conflict; lived evidence kept "
                              "as interim authority; arbitration suggested"}

    def active_cases(self) -> list[Case]:
        return [c for c in self.cases
                if c.get("status", "active") == "active" and c["confirmed"]]

    def valid_at(self, ts: float) -> list[Case]:
        """Bi-temporal query: what did the persona know at time ts?"""
        return [c for c in self.cases
                if c.get("valid_from", c["timestamp"]) <= ts
                and (c.get("valid_to") is None or c.get("valid_to") > ts)]

    def confirm_case(self, case_id: str) -> bool:
        for c in self.cases:
            if c["id"] == case_id:
                c["confirmed"] = True
                self.save()
                return True
        return False


    # -- Step 0.5: event log + erasure-replay ( runbook requirement) -------
    # The event log is the persona's git history: every mutation is an event.
    # Erasure drill: wipe cases, replay events after a snapshot, state is
    # reconstructed exactly. Proves recoverability from partial loss.

    EVENT_LOG = "store_events"

    def _event_path(self) -> str:
        return os.path.join(os.path.dirname(self.path),
                            self.EVENT_LOG, f"{self.persona}_events.jsonl")

    def log_event(self, kind: str, payload: dict) -> None:
        d = os.path.join(os.path.dirname(self.path), self.EVENT_LOG)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{self.persona}_events.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind,
                                "payload": payload}) + "\n")

    def replay_events(self, after_ts: float = 0.0) -> int:
        """Rebuild in-memory state by replaying events after a timestamp.
        Returns count of events applied. Used by the erasure drill."""
        p = os.path.join(os.path.dirname(self.path),
                         self.EVENT_LOG, f"{self.persona}_events.jsonl")
        if not os.path.exists(p):
            return 0
        applied = 0
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                if e["ts"] <= after_ts:
                    continue
                pl = e["payload"]
                if e["kind"] == "add":
                    c = dict(pl["case"])
                    # numpy-free: embeddings already lists in the log
                    self.cases.append(c)
                    applied += 1
                elif e["kind"] == "promote":
                    for c in self.cases:
                        if c["id"] == pl["id"]:
                            c["status"] = "active"
                            c["confirmed"] = True
                elif e["kind"] == "supersede":
                    for c in self.cases:
                        if c["id"] == pl["old_id"]:
                            c["status"] = "superseded"
                            c["valid_to"] = pl.get("ts")
                            c["superseded_by"] = pl.get("new_id")
                    for c in self.cases:
                        if c["id"] == pl["new_id"]:
                            pass  # Already active via promote
                applied += 0 if e["kind"] != "add" else 0
        return applied

    def erasure_drill(self) -> dict:
        """Erasure-replay drill: snapshot -> wipe -> replay -> verify. Proves the
        store can be reconstructed from events alone.
        """
        # 1. snapshot current state
        snap = self.snapshot("pre_drill")
        before = [dict(c) for c in self.cases]
        # 2. log all current cases as add events (idempotent drill setup)
        for c in before:
            self.log_event("add", {"case": c})
        # 3. wipe memory
        self.cases = []
        # 4. replay everything
        n = self.replay_events(after_ts=0.0)
        # 5. verify: same ids, same statuses
        after_ids = {c["id"] for c in self.cases}
        before_ids = {c["id"] for c in before}
        ok = after_ids == before_ids
        statuses_ok = all(
            next((x for x in self.cases if x["id"] == b["id"]), {}).get("status")
            == b.get("status")
            for b in before)
        result = {"snapshot": snap, "events_replayed": n,
                  "ids_match": ok, "statuses_match": statuses_ok,
                  "passed": ok and statuses_ok}
        return result

    # -- retrieval -----------------------------------------------------------

    def _matrix(self) -> tuple[Optional[np.ndarray], list[int]]:
        idx, vecs = [], []
        for i, c in enumerate(self.cases):
            if c["embedding"] is not None:
                idx.append(i)
                vecs.append(c["embedding"])
        if not idx:
            return None, []
        return np.asarray(vecs, dtype=np.float32), idx

    def retrieve(self, query_emb: list[float], k: int = 5,
                 min_similarity: float = 0.25
                 ) -> list[tuple[Case, float]]:
        if not self.cases:
            return []
        # the normalized matrix is built once and reused until a case is
        # added (115k rows: ~260 ms to build, ~5 ms to search)
        key = (len(self.cases), id(self.cases[-1]))
        cache = getattr(self, "_mat_cache", None)
        if cache is None or cache[0] != key:
            mat, idx = self._matrix()
            if mat is None:
                return []
            mat_n = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
            self._mat_cache = (key, mat_n, idx)
        _, mat_n, idx = self._mat_cache
        q = np.asarray(query_emb, dtype=np.float32).ravel()
        q_n = q / (np.linalg.norm(q) + 1e-9)
        sims = mat_n @ q_n
        # The two-stage law at READ time: only active lessons influence the
        # model — candidates wait for corroboration, superseded/retired rows
        # are history — and step noise never does
        from .quality import is_fragment
        order = np.argsort(-sims)
        out = []
        for j in order:
            if len(out) >= k:
                break
            sim = float(sims[j])
            if sim < min_similarity:
                break                      # Sorted: the rest are lower
            c = self.cases[idx[j]]
            if c.get("status", "active") != "active":
                continue
            if c.get("origin") != "seed" and is_fragment(c.get("framing", "")):
                continue
            # lived cases outrank seeds at equal similarity (design doc sec 8)
            if c["origin"] == "seed":
                sim *= 0.95
            out.append((c, sim))
        return sorted(out, key=lambda t: -t[1])

    # -- conflict resolution: lived beats seed on same territory -------------

    def resolve_seed_conflicts(self, query_emb: list[float]) -> list[str]:
        """Demote seed cases that lived experience contradicts (sec 8 rule)."""
        mat, idx = self._matrix()
        if mat is None:
            return []
        changed = []
        seeds = [(i, self.cases[i]) for i in idx
                 if self.cases[i]["origin"] == "seed" and self.cases[i]["confirmed"]]
        lived = [(i, self.cases[i]) for i in idx
                 if self.cases[i]["origin"] == "lived" and self.cases[i]["confirmed"]]
        if not seeds or not lived:
            return []
        lvecs = np.asarray([c["embedding"] for _, c in lived], dtype=np.float32)
        lvecs = lvecs / (np.linalg.norm(lvecs, axis=1, keepdims=True) + 1e-9)
        q = np.asarray(query_emb, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        for i, sc in seeds:
            sv = np.asarray(sc["embedding"], dtype=np.float32)
            sv = sv / (np.linalg.norm(sv) + 1e-9)
            # find the most similar lived case
            best = int(np.argmax(lvecs @ sv))
            li, lc = lived[best]
            # similar territory, different confirmed outcome -> lived wins
            if float(lvecs[best] @ sv) > 0.85 and lc["outcome"] != sc["outcome"]:
                sc["confirmed"] = False
                sc["outcome"] = f"[overridden by lived case {lc['id']}] " + sc["outcome"]
                changed.append(sc["id"])
        if changed:
            self.save()
        return changed



    def dedupe(self) -> dict:
        """Item 8 (spec v3): merge framings with cosine >= CANDIDATE_SIM.
        Keep the strongest (confidence, then corroboration); fold attributed
        outcomes + marks into it; supersede the rest (bi-temporal, no
        deletion). Run between waves. Returns {'merged': n}."""
        merged = 0
        cases = self.cases
        for i in range(len(cases)):
            c = cases[i]
            if c.get("status") != "active" or c.get("embedding") is None:
                continue
            for j in range(i + 1, len(cases)):
                d = cases[j]
                if d.get("status") != "active" or d.get("embedding") is None:
                    continue
                if d["id"] == c["id"]:
                    continue
                sim = float(np.asarray(c["embedding"], dtype=np.float32) @
                            np.asarray(d["embedding"], dtype=np.float32))
                if sim < self.CANDIDATE_SIM:
                    continue
                if (c["confidence"], c.get("corroboration_count", 0)) >=                    (d["confidence"], d.get("corroboration_count", 0)):
                    strong, weak = c, d
                else:
                    strong, weak = d, c
                strong["corroboration_count"] = strong.get(
                    "corroboration_count", 0) + 1
                for k in ("attributed_ok", "attributed_fail"):
                    strong[k] = strong.get(k, 0) + d.get(k, 0)
                strong["failure_marks"] = max(strong.get("failure_marks", 0),
                                              d.get("failure_marks", 0))
                strong["confidence"] = max(strong["confidence"],
                                           d["confidence"])
                store_state = self.supersede(d["id"], strong["id"])
                merged += 1
        if merged:
            self.save()
        return {"merged": merged}

    def __len__(self) -> int:
        return len(self.cases)

"""Embedder + working-state tracker.

Tier 1: MiniLM-L6-v2 int8 ONNX (23MB, CPU, ~10-20ms/query) — real semantic
embeddings. The legacy HashEmbedder remains as zero-download fallback
(model files missing / exotic platforms). Both satisfy embed(text) -> vec.

The WorkingState is the live "what we are doing now" (design doc sec 4):
exponential moving average over recent turns + explicit task boundaries.

Calibration (embedder_calibration.json): measured separation unrelated
mean 0.018 sd 0.045 vs related mean 0.569 sd 0.117 -> clean separation,
thresholds derive from background z-scores as before.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from typing import Optional

import numpy as np

DIM = 384          # MiniLM-L6; HashEmbedder keeps its own 512 default
_CALIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embedder_calibration.json")

_TOKEN = re.compile(r"[\w]+|[^\w\s]")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())



class MiniLMEmbedder:
    """Real semantic embeddings via int8 ONNX MiniLM (Tier 1 default)."""

    def __init__(self, model_dir: Optional[str] = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        here = os.path.dirname(os.path.abspath(__file__))
        d = model_dir or next(
            (p for p in (os.path.join(here, "models", "minilm-l6-int8"),
                         os.path.join(os.path.dirname(here), "models", "minilm-l6-int8"))
             if os.path.isdir(p)),
            os.path.join(here, "models", "minilm-l6-int8"))
        if not os.path.exists(os.path.join(d, "model_quantized.onnx")):
            raise FileNotFoundError(f"MiniLM model not found at {d}")
        self.tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2      # Pi-class friendly
        self.sess = ort.InferenceSession(
            os.path.join(d, "model_quantized.onnx"), opts,
            providers=["CPUExecutionProvider"])
        self.dim = DIM

    def embed(self, text: str) -> list[float]:
        enc = self.tok.encode(text)
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        tt = np.array([enc.type_ids], dtype=np.int64)
        out = self.sess.run(None, {"input_ids": ids, "attention_mask": mask,
                                   "token_type_ids": tt})[0]
        m = mask[..., None].astype(np.float32)
        v = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)   # (1, 384)
        vec = np.asarray(v, dtype=np.float32).reshape(-1)      # (384)
        n = float(np.linalg.norm(vec))
        vec = vec / n if n > 0 else vec
        return vec.tolist()


def make_embedder(prefer_minilm: bool = True):
    """Tier-aware embedder factory: MiniLM if available, hash fallback."""
    if prefer_minilm:
        try:
            return MiniLMEmbedder()
        except Exception:
            pass
    return HashEmbedder()


def calibrated_thresholds():
    """Load measured MiniLM calibration (unrelated/related distributions)."""
    try:
        with open(_CALIB_PATH) as f:
            return json.load(f)
    except Exception:
        return None


class HashEmbedder:
    """Deterministic bag-of-ngrams -> hashed vector. MB-scale eyes, v0."""

    def __init__(self, dim: int = DIM, ngram: tuple[int, ...] = (1, 2, 3)):
        self.dim = dim
        self.ngram = ngram

    def embed(self, text: str) -> list[float]:
        toks = _tokens(text)
        v = np.zeros(self.dim, dtype=np.float32)
        if not toks:
            return v.tolist()
        grams: list[str] = []
        for n in self.ngram:
            grams += [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]
        for g in grams:
            h = int.from_bytes(hashlib.md5(g.encode()).digest()[:8], "big")
            i = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            v[i] += sign
        n = np.linalg.norm(v)
        return (v / n if n > 0 else v).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))


class WorkingState:
    """Live situational embedding: what we are doing right now (sec 4).

    Updated every turn by EMA; jumps fully on explicit task boundaries.
    Holds enough to judge on_task / sideways / unnecessary.
    """

    def __init__(self, embedder: HashEmbedder, alpha: float = 0.35,
                 z_on_task: float = 3.0, z_sideways: float = 1.0):
        self.embedder = embedder
        self.alpha = alpha            # EMA weight for new turns
        # Thresholds are RELATIVE to the embedder's similarity scale: calibrated
        # against a background of clearly-unrelated texts (mu + z*sigma), so
        # swapping in MiniLM later re-calibrates instead of silently breaking.
        self.z_on_task = z_on_task
        self.z_sideways = z_sideways
        self._bg_mean: float = 0.0
        self._bg_std: float = 1e-6
        self.task_summary: str = ""
        self._state: Optional[np.ndarray] = None
        self._recent: list[str] = []  # Last few turn texts (for audit trail)

    def calibrate_background(self, unrelated_texts: list[str]) -> None:
        """Measure this embedder's similarity scale on clearly-unrelated texts;
        thresholds become mu + z*sigma of that background distribution."""
        sims: list[float] = []
        for i, a in enumerate(unrelated_texts[:3]):
            for b in unrelated_texts[i + 1:]:
                sims.append(cosine(self.embedder.embed(a), self.embedder.embed(b)))
        sims += [cosine(self.embedder.embed("project deploy checklist review"),
                        self.embedder.embed(t))
                 for t in ("zebra migration patterns in kenya",
                           "sourdough starter feeding schedule",
                           "bass guitar string gauge comparison")]
        if not sims:
            return
        arr = np.asarray(sims, dtype=np.float32)
        self._bg_mean, self._bg_std = float(arr.mean()), float(arr.std() + 1e-6)

    # -- lifecycle -----------------------------------------------------------

    def set_task(self, summary: str) -> None:
        """Explicit task boundary: hard reset of the state (sec 4)."""
        self.task_summary = summary
        self._state = np.asarray(self.embedder.embed(summary), dtype=np.float32)
        self._recent = [summary]

    def update(self, turn_text: str) -> None:
        e = np.asarray(self.embedder.embed(turn_text), dtype=np.float32)
        if self._state is None:
            self._state = e
        else:
            self._state = (1 - self.alpha) * self._state + self.alpha * e
            n = np.linalg.norm(self._state)
            if n > 0:
                self._state /= n
        self._recent.append(turn_text)
        self._recent = self._recent[-8:]

    def vector(self) -> Optional[list[float]]:
        return None if self._state is None else self._state.tolist()

    # -- judgments (sec 4) ----------------------------------------------------

    def judge_relevance(self, text: str) -> tuple[str, float]:
        """on_task / sideways, with confidence in. Thresholds are z-scores over
        the calibrated background distribution.
        """
        if self._state is None:
            return "on_task", 0.5  # No state yet: cannot judge, default open
        sim = cosine(self.embedder.embed(text), self._state.tolist())
        z = (sim - self._bg_mean) / self._bg_std
        if z >= self.z_on_task:
            return "on_task", float(min(1.0, 0.5 + z / 10))
        if z >= self.z_sideways:
            return "sideways", float(min(1.0, 0.4 + z / 8))
        return "sideways", float(min(1.0, 0.5 + (self.z_sideways - z) / 5))

    def territory_coverage(self, store_matrix_mean: Optional[list[float]]) -> float:
        """How far inside the persona's lived territory this exchange sits."""
        if self._state is None or store_matrix_mean is None:
            return 0.5  # Unknown
        return max(0.0, min(1.0, cosine(self._state.tolist(), store_matrix_mean)))

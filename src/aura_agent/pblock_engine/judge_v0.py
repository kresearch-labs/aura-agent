"""V0 judge: MB-scale, log-loss trained heads (Tier 0 of the design doc).

The borrowed idea (Jev/RLCD) at MB scale: typed questions, calibrated
probabilities, no generation. Training against log-loss IS training against a
strictly proper scoring — the mathematical core of RLCD — so honest
probabilities are the optimal strategy, exactly as in RLCD.

Each question template is a Noul (probability of yes) implemented as logistic
regression over [situation_emb || precedent_features]. Per-persona heads train
on confirmed store cases. When a persona has too few confirmed cases, the judge
abstains with wide uncertainty instead of guessing.
"""
from __future__ import annotations

import json

import numpy as np

from .schemas import Judgment, JudgeDecision
from .store import PersonaStore
from .working_state import HashEmbedder, cosine

# DEFAULT floors only — each pblock's template declares its own gates
# (gates.min_cases / gates.min_positives_per_label), and those flow through
# the orchestrator into fit_question(). A legitimately narrow territory may
# train AND run at its own declared floor; a broad one may declare more.
# These constants are fallbacks for seats whose template declares nothing
# ("blank starts empty; raise the gate as it lives" — blank-pblock.yml).
DEFAULT_MIN_TRAIN_CASES = 500
DEFAULT_MIN_POSITIVES_PER_LABEL = 50


class V0Judge:
    def __init__(self, persona: str, embedder: HashEmbedder, store: PersonaStore,
                 min_train_cases: int | None = None,
                 min_positives_per_label: int | None = None):
        self.persona = persona
        self.embedder = embedder
        self.store = store
        self._models: dict[str, dict] = {}   # question_id -> {w, b, temp}
        # floors are the seat's OWN declared gates (template) with the module
        # defaults as fallback — never a hardcoded number the seat cannot raise
        # or lower for its territory.
        self.min_train_cases = int(min_train_cases) if min_train_cases is not None else DEFAULT_MIN_TRAIN_CASES
        self.min_positives_per_label = int(min_positives_per_label) if min_positives_per_label is not None else DEFAULT_MIN_POSITIVES_PER_LABEL

    def load_artifact(self, path: str) -> int:
        """Load trained head weights from a train_pblock artifact. The runtime
        complement of fit_question: the heads the trainer wrote are the ones
        this judge consults. Missing T fields default to 1.0 (uncalibrated).
        Returns the number of heads loaded. Never raises."""
        try:
            art = json.load(open(path, encoding="utf-8"))
        except Exception:
            return 0
        heads = art.get("heads") or {}
        n = 0
        for qid, head in heads.items():
            if not isinstance(head, dict) or "w" not in head or "b" not in head:
                continue
            if head.get("untrained"):
                continue  # Label was starved at train time -- abstain, don't fake it
            self._models[qid] = {
                "w": head["w"], "b": head["b"],
                "T": head.get("T", 1.0), "ece": head.get("ece"),
                "acc": head.get("acc"), "pos_rate": head.get("pos_rate"),
            }
            n += 1
        return n

    # -- feature building -----------------------------------------------------

    def _features(self, situation_emb: list[float],
                  precedents: list[tuple[dict, float]]) -> list[float]:
        """Situation embedding + retrieval signals (sec 6: judgments are grounded
        in precedent, not vibes)."""
        if precedents:
            top_sim = precedents[0][1]
            # similarity-weighted mean of precedent embeddings, diff vs situation
            ws = np.asarray([p["embedding"] for p, _ in precedents], dtype=np.float32)
            wt = np.asarray([s for _, s in precedents], dtype=np.float32)
            mean_p = (ws * wt[:, None]).sum(0) / (wt.sum() + 1e-9)
            n = np.linalg.norm(mean_p := mean_p)
            mean_p = mean_p / n if n > 0 else mean_p
            diff = np.asarray(situation_emb, dtype=np.float32) - mean_p
        else:
            top_sim, mean_p, diff = 0.0, np.zeros(len(situation_emb)), np.zeros(len(situation_emb))
            top_sim = 0.0
        e = np.asarray(situation_emb, dtype=np.float32)
        return np.concatenate([e, diff, [top_sim, len(precedents) / 5.0]]).tolist()

    # -- training (learning loop step; log-loss = proper scoring rule) --------

    def fit_question(self, question_id: str, labeled: list[tuple[dict, float, int]],
                     epochs: int = 300, lr: float = 0.5, l2: float = 1e-3) -> bool:
        """labeled: list of (situation_emb, precedents, y in {0,1}).

        Pre-registered abstention: fewer than MIN_TRAIN_CASES confirmed
        examples -> no model -> judge reports uncertainty, never a guess.
        """
        if len(labeled) < self.min_train_cases:
            self._models.pop(question_id, None)
            return False
        y_all = np.asarray([lab for _, _, lab in labeled], dtype=np.float32)
        minority = int(min(y_all.sum(), len(y_all) - y_all.sum()))
        if minority < self.min_positives_per_label:
            # per-label floor: total cases can pass while ONE label starves
            # (measured case: pos_rate 0.01 x 100 = 1 positive case)
            self._models.pop(question_id, None)
            return False
        X = np.asarray([self._features(s, p) for s, p, _ in labeled], dtype=np.float32)
        y = np.asarray([lab for _, _, lab in labeled], dtype=np.float32)
        n = len(y)
        # hold out last 20% (chronological) for temperature calibration
        n_fit = max(1, int(n * 0.8))
        Xf, yf = X[:n_fit], y[:n_fit]
        Xc, yc = X[n_fit:], y[n_fit:]
        d = X.shape[1]
        w, b = np.zeros(d, dtype=np.float32), 0.0
        for _ in range(epochs):
            z = Xf @ w + b
            p = 1.0 / (1.0 + np.exp(-z))
            g = (p - yf) / len(yf)
            w -= lr * (Xf.T @ g + l2 * w)
            b -= lr * float(g.sum())
        # temperature scaling on held-out (proper calibration; ECE 0.47->0.08
        # lesson from research: refit on held-out, never on training set)
        T = self._fit_temperature(Xc, yc, w, b)
        ece = self._ece(Xc, yc, w, b, T)
        self._models[question_id] = {"w": w, "b": b, "T": T, "ece": ece,
                                     "n_train": int(n_fit), "n_cal": int(n - n_fit)}
        return True

    @staticmethod
    def _logit(X: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
        return X @ w + b

    def _fit_temperature(self, Xc: np.ndarray, yc: np.ndarray,
                         w: np.ndarray, b: float, iters: int = 200) -> float:
        """1-d optimization of T on held-out log-loss (NLL)."""
        if len(yc) < 2:
            return 1.0
        z = self._logit(Xc, w, b)
        best_T, best_nll = 1.0, float("inf")
        for T in np.linspace(0.25, 4.0, iters):
            p = 1.0 / (1.0 + np.exp(-z / T))
            p = np.clip(p, 1e-6, 1 - 1e-6)
            nll = -np.mean(yc * np.log(p) + (1 - yc) * np.log(1 - p))
            if nll < best_nll:
                best_T, best_nll = float(T), float(nll)
        return best_T

    @staticmethod
    def _ece(Xc: np.ndarray, yc: np.ndarray, w: np.ndarray, b: float,
             T: float, bins: int = 10) -> float:
        """Expected calibration error on the held-out calibration set."""
        if len(yc) < 2:
            return 0.0
        z = Xc @ w + b
        p = 1.0 / (1.0 + np.exp(-z / T))
        edges = np.linspace(0, 1, bins + 1)
        ece = 0.0
        for i in range(bins):
            m = (p >= edges[i]) & (p < edges[i + 1])
            if m.sum() == 0:
                continue
            ece += m.mean() * abs(float(p[m].mean()) - float(yc[m].mean()))
        return float(ece)

    # -- inference -------------------------------------------------------------

    # conformal-style abstention: if the nearest precedent similarity is below
    # the margin the head was trained on, this input is out-of-region -> escalate
    EDGE_SIM = 0.12   # MiniLM-calibrated: unrelated bg ~0.02, related ~0.57

    def ask_noul(self, question_id: str, situation_emb: list[float],
                 precedents: list[tuple[dict, float]]) -> JudgeDecision:
        m = self._models.get(question_id)
        if m is None:
            # no trained head: honest abstention (this IS the anti-hallucination
            # behavior — an untrained judge refuses to fabricate confidence)
            return JudgeDecision(
                question_id=question_id, qtype="noul", value=0.5,
                probabilities={"yes": 0.5, "no": 0.5}, confidence=0.0,
                persona=self.persona, cited_precedent=None,
                escalation=True, escalation_reason=(
                    f"head untrained (<{self.min_train_cases} confirmed cases "
                    f"or <{self.min_positives_per_label} minority per label)"))
        x = np.asarray(self._features(situation_emb, precedents), dtype=np.float32)
        # conformal check: out-of-region inputs escalate regardless of head output
        top_sim = float(precedents[0][1]) if precedents else 0.0
        if top_sim < self.EDGE_SIM:
            return JudgeDecision(
                question_id=question_id, qtype="noul", value=0.5,
                probabilities={"yes": 0.5, "no": 0.5}, confidence=0.0,
                persona=self.persona, cited_precedent=None,
                escalation=True,
                escalation_reason=f"edge-of-territory (top_sim {top_sim:.2f} "
                                  f"< {self.EDGE_SIM})")
        z = float(x @ m["w"] + m["b"])
        p = 1.0 / (1.0 + np.exp(-z / m.get("T", 1.0)))   # Temperature-calibrated
        cited = precedents[0][0]["id"] if precedents else None
        # calibrated confidence: distance from 0.5 shrunk by head's ECE
        conf = max(0.0, min(1.0, (abs(p - 0.5) * 2) * (1.0 - m.get("ece", 0.1))))
        return JudgeDecision(
            question_id=question_id, qtype="noul", value=p,
            probabilities={"yes": p, "no": 1 - p}, confidence=conf,
            persona=self.persona, cited_precedent=cited,
            escalation=False, escalation_reason="")

    def trained_questions(self) -> list[str]:
        return list(self._models.keys())

    def head_stats(self) -> dict[str, dict]:
        """Per-head calibration stats (ECE monitoring for live-ops)."""
        return {q: {"ece": round(m.get("ece", 0.0), 3),
                    "T": round(m.get("T", 1.0), 3),
                    "n_train": m.get("n_train", 0),
                    "n_cal": m.get("n_cal", 0)}
                for q, m in self._models.items()}


"""Real semantic embedder: MiniLM-L6-v2 int8 ONNX (23MB, CPU, ~10-20ms/query).
Drop-in replacement for the hash embedder in working_state.py — same embed()
contract (list[str] -> unit-normalized float32 matrix).
"""
import os
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
# flat layout: models/ sits beside this file; nested engine layouts fall back
# to the parent dir (persona/ inside a repo root)
_MODEL_DIR = next(
    (p for p in (os.path.join(_HERE, "models", "minilm-l6-int8"),
                 os.path.join(os.path.dirname(_HERE), "models", "minilm-l6-int8"))
     if os.path.isdir(p)),
    os.path.join(_HERE, "models", "minilm-l6-int8"))
_DIM = 384

class MiniLMEmbedder:
    def __init__(self, model_dir=None):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = model_dir or _MODEL_DIR
        self.tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2          # Pi-class friendly
        self.sess = ort.InferenceSession(
            os.path.join(d, "model_quantized.onnx"), opts,
            providers=["CPUExecutionProvider"])
        self.dim = _DIM

    def embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        enc = self.tok.encode_batch(list(texts))
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        tt = np.array([e.type_ids for e in enc], dtype=np.int64)
        out = self.sess.run(None, {"input_ids": ids, "attention_mask": mask,
                                   "token_type_ids": tt})[0]
        m = mask[..., None].astype(np.float32)
        emb = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)  # Mean pooling
        return (emb / np.linalg.norm(emb, axis=1, keepdims=True)).astype(np.float32)

# Singleton lazy loader
_emb = None
def get_embedder():
    global _emb
    if _emb is None:
        _emb = MiniLMEmbedder()
    return _emb

def embed(texts):
    return get_embedder().embed(texts)

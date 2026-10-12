"""Shared model wrappers for the embeddinggemma-2 vs multilingual-e5-small benchmark.

Both wrappers expose:
    encode_queries(texts) -> np.ndarray (L2-normalised, float32)
    encode_documents(texts) -> np.ndarray (L2-normalised, float32)
    encode_symmetric(texts) -> np.ndarray (for STS: same prompt on both sides)

e5-small goes through the production Embedder class (sai_memory/memory/recall.py),
so prefixes ("query: " / "passage: "), fastembed settings and the local ONNX
snapshot are exactly what SAIVerse uses. Embedder.embed() returns Python lists;
that conversion cost is part of the production path and is kept.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parent
DATA_DIR = BENCH_DIR / "data"
RESULTS_DIR = BENCH_DIR / "results"

# Read-only local ONNX snapshot used by the production install.
E5_LOCAL_PATH = Path(r"C:\Users\shuhe\workspace\SAIVerse\sbert\multilingual-e5-small")
E5_MODEL_NAME = "intfloat/multilingual-e5-small"
GEMMA_MODEL_NAME = "google/embeddinggemma-2"

MODEL_KEYS = ("e5", "gemma")


def prepare_env() -> None:
    os.environ.setdefault("SAIMEMORY_EMBED_CUDA", "0")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def _l2(x):
    import numpy as np

    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


class E5Wrapper:
    name = "multilingual-e5-small (fastembed/ONNX, production Embedder)"

    def __init__(self) -> None:
        prepare_env()
        from sai_memory.memory.recall import Embedder

        if E5_LOCAL_PATH.exists():
            self.embedder = Embedder(E5_MODEL_NAME, local_model_path=str(E5_LOCAL_PATH), cuda=False)
            self.source = f"local snapshot {E5_LOCAL_PATH}"
        else:
            self.embedder = Embedder(E5_MODEL_NAME, cuda=False)
            self.source = "fastembed auto-download"
        self.dim = 384

    def encode_queries(self, texts):
        return _l2(self.embedder.embed(list(texts), is_query=True))

    def encode_documents(self, texts):
        return _l2(self.embedder.embed(list(texts), is_query=False))

    def encode_symmetric(self, texts, side: str = "query"):
        return _l2(self.embedder.embed(list(texts), is_query=(side == "query")))

    def info(self) -> dict:
        import onnxruntime as ort

        return {
            "model": E5_MODEL_NAME,
            "source": self.source,
            "runtime": f"fastembed / onnxruntime {ort.__version__} (CPUExecutionProvider)",
            "prefix_query": "query: ",
            "prefix_document": "passage: ",
            "dim": self.dim,
        }


class GemmaWrapper:
    name = "embeddinggemma-2 (sentence-transformers, CPU)"

    def __init__(self, batch_size: int = 32, *, fp32: bool = False, text_only: bool = False) -> None:
        """fp32: load weights as float32 instead of the checkpoint default (bfloat16).
        text_only: after loading, drop the vision / audio towers (unused for text
        encoding) to measure the memory a text-only deployment would need. This is
        an experiment-only hack, not a supported sentence-transformers loading path;
        text embeddings were checked to be bit-identical before/after the drop."""
        prepare_env()
        from sentence_transformers import SentenceTransformer

        kwargs = {}
        if fp32:
            import torch

            kwargs["model_kwargs"] = {"dtype": torch.float32}
        self.model = SentenceTransformer(GEMMA_MODEL_NAME, device="cpu", **kwargs)
        self.param_breakdown = self._param_breakdown()
        self.text_only = text_only
        if text_only:
            import gc

            am = self._inner()
            for n in ("vision_tower", "audio_tower", "embed_vision", "embed_audio"):
                if hasattr(am, n):
                    setattr(am, n, None)
            gc.collect()
        self.batch_size = batch_size
        getter = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = getter()

    def _inner(self):
        t = self.model[0]
        return getattr(t, "model", None) or getattr(t, "auto_model")

    def _param_breakdown(self) -> dict:
        return {n: int(sum(p.numel() for p in c.parameters())) for n, c in self._inner().named_children()}

    def _enc(self, texts, prompt_name):
        import numpy as np

        v = self.model.encode(
            list(texts),
            prompt_name=prompt_name,
            normalize_embeddings=True,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(v, dtype=np.float32)

    def encode_queries(self, texts):
        return self._enc(texts, "SearchQuery")

    def encode_documents(self, texts):
        return self._enc(texts, "Document")

    def encode_symmetric(self, texts, side: str = "STS"):
        return self._enc(texts, side)

    def info(self) -> dict:
        import sentence_transformers
        import torch
        import transformers

        p = next(self.model.parameters())
        return {
            "model": GEMMA_MODEL_NAME,
            "runtime": (
                f"sentence-transformers {sentence_transformers.__version__} / transformers "
                f"{transformers.__version__} / torch {torch.__version__} (CPU, {torch.get_num_threads()} threads)"
            ),
            "dtype": str(p.dtype),
            "prompt_query": self.model.prompts.get("SearchQuery"),
            "prompt_document": self.model.prompts.get("Document"),
            "prompt_sts": self.model.prompts.get("STS"),
            "batch_size": self.batch_size,
            "dim": self.dim,
            "n_params_loaded_now": int(sum(x.numel() for x in self.model.parameters())),
            "param_breakdown_at_load": self.param_breakdown,
            "text_only_hack": self.text_only,
        }


def load_model(key: str):
    if key == "e5":
        return E5Wrapper()
    if key == "gemma":
        return GemmaWrapper()
    if key == "gemma_fp32":
        return GemmaWrapper(fp32=True)
    if key == "gemma_textonly":
        return GemmaWrapper(text_only=True)
    raise ValueError(key)


SPEED_ROLES = ("e5", "gemma", "gemma_fp32", "gemma_textonly")


def truncate_renorm(emb, dim: int):
    return _l2(emb[:, :dim])

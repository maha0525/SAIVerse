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


# ---------------------------------------------------------------- ONNX (onnx-community export)
GEMMA_ONNX_REPO = "onnx-community/embeddinggemma-2-ONNX"
# Snapshot downloaded on 2026-10-12; pinned so later re-uploads cannot silently change the numbers.
GEMMA_ONNX_REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
# role key -> ONNX file stem under onnx/ (text model only; vision/audio encoders are not used)
GEMMA_ONNX_VARIANTS = {
    "gemma_onnx_fp16": "model_fp16",
    "gemma_onnx_q8": "model_quantized",
    "gemma_onnx_q4": "model_q4",
}
# Same strings as config_sentence_transformers.json of google/embeddinggemma-2
# (identical in the ONNX repo's copy).
GEMMA_PROMPTS = {
    "SearchQuery": "task: search result | query: ",
    "Document": "title: none | text: ",
    "STS": "task: sentence similarity | query: ",
}


def gemma_onnx_files(stem: str) -> dict:
    """Return local paths of the cached ONNX text model + tokenizer (no network)."""
    from huggingface_hub import hf_hub_download

    def get(name: str) -> Path:
        return Path(hf_hub_download(GEMMA_ONNX_REPO, name, revision=GEMMA_ONNX_REVISION, local_files_only=True))

    model = get(f"onnx/{stem}.onnx")
    data = get(f"onnx/{stem}.onnx_data")
    tok = get("tokenizer.json")
    return {"model": model, "data": data, "tokenizer": tok}


class GemmaOnnxWrapper:
    """embeddinggemma-2 through onnxruntime directly (CPUExecutionProvider, default threads).

    The exported graph already contains the whole post-processing chain: the text
    backbone's `embedding_projection` (last_hidden_state is taken *after* it), masked
    mean pooling over attention_mask, and L2 normalisation, exposed as the
    `sentence_embedding` output [batch, 768]. sentence-transformers' module list for
    google/embeddinggemma-2 is Transformer -> Pooling(mean, include_prompt) -> Normalize
    (no Dense), so `sentence_embedding` is used as-is (re-normalised once more in fp32).

    The graph also declares image_features / video_features / audio_features inputs
    ([N, 512] float); text-only calls feed them as empty [0, 512] tensors.

    Batching mirrors SentenceTransformer.encode: inputs are sorted by length
    (longest first), split into batches of `batch_size`, right-padded with <pad>,
    and the results are put back in input order.
    """

    def __init__(self, role: str, batch_size: int = 32, max_length: int | None = None) -> None:
        prepare_env()
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.role = role
        self.stem = GEMMA_ONNX_VARIANTS[role]
        self.files = gemma_onnx_files(self.stem)
        self.tokenizer = Tokenizer.from_file(str(self.files["tokenizer"]))
        self.tokenizer.no_padding()
        self.max_length = max_length
        if max_length:
            self.tokenizer.enable_truncation(max_length)
        else:
            self.tokenizer.no_truncation()
        self.pad_id = self.tokenizer.token_to_id("<pad>")
        so = ort.SessionOptions()
        self.session = ort.InferenceSession(str(self.files["model"]), sess_options=so, providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.batch_size = batch_size
        self.dim = 768

    def tokenize(self, texts):
        return [e.ids for e in self.tokenizer.encode_batch(list(texts))]

    def _run(self, ids_list):
        import numpy as np

        n = len(ids_list)
        L = max(len(x) for x in ids_list)
        ids = np.full((n, L), self.pad_id, dtype=np.int64)
        mask = np.zeros((n, L), dtype=np.int64)
        for i, x in enumerate(ids_list):
            ids[i, : len(x)] = x
            mask[i, : len(x)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask}
        empty = np.zeros((0, 512), dtype=np.float32)
        for name in ("image_features", "video_features", "audio_features"):
            if name in self.input_names:
                feeds[name] = empty
        (emb,) = self.session.run(["sentence_embedding"], feeds)
        return emb

    def _enc(self, texts, prompt_name: str):
        import numpy as np

        prompt = GEMMA_PROMPTS[prompt_name]
        ids_list = self.tokenize([prompt + t for t in texts])
        order = sorted(range(len(ids_list)), key=lambda i: -len(ids_list[i]))
        out = np.zeros((len(ids_list), self.dim), dtype=np.float32)
        for s in range(0, len(order), self.batch_size):
            idx = order[s : s + self.batch_size]
            out[idx] = self._run([ids_list[i] for i in idx])
        return _l2(out)

    def encode_queries(self, texts):
        return self._enc(texts, "SearchQuery")

    def encode_documents(self, texts):
        return self._enc(texts, "Document")

    def encode_symmetric(self, texts, side: str = "STS"):
        return self._enc(texts, side)

    def info(self) -> dict:
        import onnxruntime as ort
        import tokenizers

        return {
            "model": f"{GEMMA_ONNX_REPO}@{GEMMA_ONNX_REVISION[:12]} onnx/{self.stem}.onnx",
            "runtime": (
                f"onnxruntime {ort.__version__} (CPUExecutionProvider, intra_op threads = default) / "
                f"tokenizers {tokenizers.__version__}"
            ),
            "file_bytes": {k: v.stat().st_size for k, v in self.files.items()},
            "output_used": "sentence_embedding (in-graph projection + mean pooling + L2)",
            "prompt_query": GEMMA_PROMPTS["SearchQuery"],
            "prompt_document": GEMMA_PROMPTS["Document"],
            "prompt_sts": GEMMA_PROMPTS["STS"],
            "batch_size": self.batch_size,
            "max_length": self.max_length,
            "dim": self.dim,
        }


def is_gemma(key: str) -> bool:
    return key.startswith("gemma")


def load_model(key: str):
    if key == "e5":
        return E5Wrapper()
    if key == "gemma":
        return GemmaWrapper()
    if key == "gemma_fp32":
        return GemmaWrapper(fp32=True)
    if key == "gemma_textonly":
        return GemmaWrapper(text_only=True)
    if key in GEMMA_ONNX_VARIANTS:
        return GemmaOnnxWrapper(key)
    raise ValueError(key)


SPEED_ROLES = ("e5", "gemma", "gemma_fp32", "gemma_textonly", *GEMMA_ONNX_VARIANTS)


def truncate_renorm(emb, dim: int):
    return _l2(emb[:, :dim])

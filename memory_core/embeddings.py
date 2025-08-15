from __future__ import annotations

from typing import List, Optional
import math
import hashlib


class EmbeddingProvider:
    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


class SimpleHashEmbedding(EmbeddingProvider):
    """
    Dependency-free embedding: feature hashing of tokens into fixed dims.
    Not semantically strong, but deterministic and fast for scaffolding/testing.
    """

    def __init__(self, dim: int = 384, normalize: bool = True) -> None:
        self.dim = dim
        self.normalize = normalize

    def _tokenize(self, text: str) -> List[str]:
        # very naive tokenizer; keeps basic CJK blocks together by character
        tokens: List[str] = []
        buf = []
        for ch in text:
            if ch.isalnum() or ord(ch) > 0x3000:  # simple heuristic for wide chars
                buf.append(ch)
            else:
                if buf:
                    tokens.append("".join(buf))
                    buf = []
        if buf:
            tokens.append("".join(buf))
        return tokens

    def _hash_token(self, tok: str) -> int:
        h = hashlib.sha1(tok.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "little", signed=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for t in texts:
            vec = [0.0] * self.dim
            for tok in self._tokenize(t.lower()):
                idx = self._hash_token(tok) % self.dim
                vec[idx] += 1.0
            if self.normalize:
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                vec = [x / norm for x in vec]
            out.append(vec)
        return out


def embed(texts: List[str], provider: EmbeddingProvider) -> List[List[float]]:
    return provider.embed(texts)


class SBERTEmbedding(EmbeddingProvider):
    """SentenceTransformers backend. Lazily imports to avoid hard deps.
    Use a local model path or a HF model id. Normalizes to unit length.
    """

    def __init__(self, model_name_or_path: Optional[str] = None, device: Optional[str] = None, dim_hint: Optional[int] = None) -> None:
        self.model_name = model_name_or_path or "sentence-transformers/all-MiniLM-L6-v2"
        self.device = device
        self.dim_hint = dim_hint
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                import torch
            except Exception as e:
                raise RuntimeError("sentence-transformers is not installed") from e
            dev = self.device or "cpu"
            if dev == "cuda" and not torch.cuda.is_available():
                print("Embedding notice: CUDA not available; falling back to CPU for SBERT")
                dev = "cpu"
            if dev == "mps":
                # sentence-transformers relies on torch; basic check for mps support
                if not getattr(torch.backends, "mps", None) or not torch.backends.mps.is_available():
                    print("Embedding notice: MPS not available; falling back to CPU for SBERT")
                    dev = "cpu"
            self._model = SentenceTransformer(self.model_name, device=dev)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._ensure()
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]


class HFTransformersEmbedding(EmbeddingProvider):
    """Pure HF transformers backend with mean pooling. For models that are not wrapped by sentence-transformers.
    Requires local model or internet to fetch (offline path recommended).
    """

    def __init__(self, model_name_or_path: Optional[str] = None, device: Optional[str] = None) -> None:
        self.model_name = model_name_or_path or "intfloat/multilingual-e5-small"
        self.device = device or "cpu"
        self._tok = None
        self._model = None

    def _ensure(self):
        if self._tok is None or self._model is None:
            try:
                from transformers import AutoTokenizer, AutoModel
                import torch
            except Exception as e:
                raise RuntimeError("transformers/torch are not installed") from e
            self._tok = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModel.from_pretrained(self.model_name)
            dev = self.device or "cpu"
            if dev == "cuda" and not torch.cuda.is_available():
                print("Embedding notice: CUDA not available; falling back to CPU for HF")
                dev = "cpu"
            if dev == "mps":
                if not getattr(torch.backends, "mps", None) or not torch.backends.mps.is_available():
                    print("Embedding notice: MPS not available; falling back to CPU for HF")
                    dev = "cpu"
            self._model.to(dev)

    def embed(self, texts: List[str]) -> List[List[float]]:
        self._ensure()
        import torch
        from torch.nn.functional import normalize as torch_normalize
        tok = self._tok
        model = self._model
        outs: List[List[float]] = []
        for t in texts:
            batch = tok([t], padding=True, truncation=True, return_tensors="pt").to(model.device)
            with torch.no_grad():
                out = model(**batch).last_hidden_state  # [1, L, H]
                mask = batch["attention_mask"].unsqueeze(-1)  # [1, L, 1]
                masked = out * mask
                summed = masked.sum(dim=1)
                counts = mask.sum(dim=1).clamp(min=1)
                mean = summed / counts
                mean = torch_normalize(mean, p=2, dim=-1)
                vec = mean[0].detach().cpu().tolist()
                outs.append(vec)
        return outs

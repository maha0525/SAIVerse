from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Literal
import os


@dataclass
class Weights:
    w_sim: float = 0.45
    w_time: float = 0.10
    w_topic: float = 0.20
    w_em: float = 0.20
    w_recency: float = 0.05


@dataclass
class Config:
    # Storage backend: currently only in-memory is implemented to avoid deps
    storage_backend: Literal["memory", "qdrant"] = "memory"

    # Embedding config
    embedding_provider: Literal["simple_hash", "sbert", "hf"] = "simple_hash"
    embedding_dim: int = 384
    normalize_embeddings: bool = True
    embedding_model: Optional[str] = None  # for sbert/hf
    embedding_device: Optional[str] = None  # cpu|cuda|mps (optional)

    # Topic assignment
    topic_match_threshold: float = 0.35  # heuristic similarity threshold
    recent_dialog_turns: int = 6

    # Retrieval
    retrieval_top_k: int = 10
    expand_by_topics: bool = True
    time_decay_tau_seconds: float = 60.0 * 60.0 * 24.0 * 14.0  # ~2 weeks
    weights: Weights = field(default_factory=Weights)

    # EMA for topic strength and centroid
    ema_alpha_strength: float = 0.2
    ema_alpha_centroid: float = 0.2

    # Qdrant settings (used when storage_backend == "qdrant")
    qdrant_url: Optional[str] = None  # e.g. http://localhost:6333
    qdrant_api_key: Optional[str] = None
    qdrant_location: Optional[str] = None  # embedded mode. e.g. ":memory:" or ~/.../qdrant
    qdrant_collection_prefix: str = "saiverse"

    # Topic assigner LLM backend
    assign_llm_backend: Literal["dummy", "ollama_http", "ollama_cli"] = "dummy"
    assign_llm_model: Optional[str] = None  # e.g. qwen2.5:3b

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        backend = os.getenv("SAIVERSE_MEMORY_BACKEND")
        if backend in ("memory", "qdrant"):
            cfg.storage_backend = backend  # type: ignore
        # embeddings
        dim = os.getenv("SAIVERSE_EMBED_DIM")
        if dim and dim.isdigit():
            cfg.embedding_dim = int(dim)
        norm = os.getenv("SAIVERSE_EMBED_NORMALIZE")
        if norm:
            cfg.normalize_embeddings = norm.lower() in ("1", "true", "yes")
        prov = os.getenv("SAIVERSE_EMBED_PROVIDER")
        if prov in ("simple_hash", "sbert", "hf"):
            cfg.embedding_provider = prov  # type: ignore
        model = os.getenv("SAIVERSE_EMBED_MODEL")
        if model:
            cfg.embedding_model = model
        dev = os.getenv("SAIVERSE_EMBED_DEVICE")
        if dev:
            cfg.embedding_device = dev
        # qdrant
        cfg.qdrant_url = os.getenv("QDRANT_URL") or os.getenv("QDRANT_HOST")
        cfg.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        cfg.qdrant_location = os.getenv("QDRANT_LOCATION")
        pref = os.getenv("QDRANT_COLLECTION_PREFIX")
        if pref:
            cfg.qdrant_collection_prefix = pref
        # LLM backend for topic assignment
        llm_backend = os.getenv("SAIVERSE_ASSIGN_LLM_BACKEND")
        if llm_backend in ("dummy", "ollama_http", "ollama_cli"):
            cfg.assign_llm_backend = llm_backend  # type: ignore
        llm_model = os.getenv("SAIVERSE_ASSIGN_LLM_MODEL")
        if llm_model:
            cfg.assign_llm_model = llm_model
        return cfg

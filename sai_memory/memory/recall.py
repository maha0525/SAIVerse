from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Tuple

import numpy as np
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType

from sai_memory.logging_utils import debug
from sai_memory.memory.storage import (
    Message,
    compose_message_content,
    get_embeddings_for_scope,
    get_messages_around,
    get_messages_last,
)


_REGISTERED_MODELS: set[tuple[str, str]] = set()
_EMBEDDING_MODEL_CACHE: dict[tuple[str, str | None, int | None, bool], TextEmbedding] = {}
_EMBEDDING_MODEL_CACHE_LOCK = RLock()


def _check_cuda_available() -> bool:
    """Check if CUDA is available for ONNX Runtime."""
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


class Embedder:
    def __init__(
        self,
        model: str = "intfloat/multilingual-e5-small",
        *,
        local_model_path: str | None = None,
        model_dim: int | None = None,
        cuda: bool | None = None,
    ):
        self.model_name = model
        logger = logging.getLogger(__name__)
        resolved_local_path: str | None = None
        if local_model_path:
            resolved_local_path = str(Path(local_model_path).expanduser().resolve())

        # Auto-detect CUDA if not specified
        # Note: Even if CUDAExecutionProvider is listed, it may fail at runtime
        # if CUDA libraries (cuBLAS, cuDNN) are not properly installed.
        # Set SAIMEMORY_EMBED_CUDA=1 to enable GPU, SAIMEMORY_EMBED_CUDA=0 to force CPU.
        cuda_env = os.getenv("SAIMEMORY_EMBED_CUDA")
        if cuda_env is not None:
            use_cuda = cuda_env.strip().lower() in {"1", "true", "yes", "on"}
        elif cuda is not None:
            use_cuda = cuda
        else:
            use_cuda = _check_cuda_available()

        cache_key = (self.model_name.lower(), resolved_local_path, model_dim, use_cuda)

        with _EMBEDDING_MODEL_CACHE_LOCK:
            cached = _EMBEDDING_MODEL_CACHE.get(cache_key)
            if cached is None:
                kwargs: Dict[str, Any] = {}
                if resolved_local_path:
                    kwargs = _build_local_model_kwargs(
                        model_name=self.model_name,
                        local_model_path=resolved_local_path,
                        explicit_dim=model_dim,
                    )
                    logger.warning(
                        "Embedder using local path '%s' for model '%s'.",
                        resolved_local_path,
                        self.model_name,
                    )
                else:
                    logger.warning(
                        "Embedder using remote model '%s' without local override.",
                        self.model_name,
                    )
                if use_cuda:
                    kwargs["cuda"] = True
                    logger.info("Embedder using CUDA for model '%s'.", self.model_name)
                
                try:
                    cached = TextEmbedding(model_name=self.model_name, **kwargs)
                except ValueError as e:
                    # Fallback to CPU if CUDA fails (common on Windows with partial CUDA setup)
                    if use_cuda:
                        logger.warning("CUDA initialization failed (%s); falling back to CPU.", e)
                        kwargs.pop("cuda", None)
                        try:
                            cached = TextEmbedding(model_name=self.model_name, **kwargs)
                        except ValueError as e2:
                            e = e2  # fall through to auto-download check below
                            cached = None
                    if cached is None:
                        # Model not natively supported by fastembed — try auto-download
                        if not resolved_local_path and "not supported" in str(e).lower():
                            logger.warning(
                                "Model '%s' not natively supported by fastembed. "
                                "Attempting auto-download from HuggingFace...",
                                self.model_name,
                            )
                            downloaded_path = _auto_download_model(self.model_name)
                            kwargs = _build_local_model_kwargs(
                                model_name=self.model_name,
                                local_model_path=downloaded_path,
                                explicit_dim=model_dim,
                            )
                            if use_cuda:
                                kwargs["cuda"] = True
                            cached = TextEmbedding(model_name=self.model_name, **kwargs)
                        else:
                            raise e

                _EMBEDDING_MODEL_CACHE[cache_key] = cached
            self.model = cached

    def embed(self, texts: List[str], *, is_query: bool = False) -> List[List[float]]:
        """
        is_query=True の場合はクエリ用のプレフィックス/タスクを適用する
        """
        # モデルごとのプレフィックス処理
        prefix = ""
        
        # E5系の場合
        if "e5" in self.model_name.lower():
            prefix = "query: " if is_query else "passage: "
        
        # Sarashina (v2) の場合 (sentence-transformers等で使う場合)
        # ※Sarashinaは「指示文」を入れるとより良いが、シンプルには query: / passage: でも機能する
        # elif "sarashina" in self.model_name.lower():
        #     prefix = "クエリ: " if is_query else "文章: "

        # テキストにプレフィックスを結合
        if prefix:
            texts = [prefix + t for t in texts]

        # Jina v3の場合は fastembed の task パラメータを使う必要がある
        # fastembed >= 0.3.0
        # kwargs = {}
        # if "jina-embeddings-v3" in self.model_name.lower():
        #     kwargs["task"] = "retrieval.query" if is_query else "retrieval.passage"
        # vectors = list(self.model.embed(texts, **kwargs))

        vectors = list(self.model.embed(texts))
        return [list(map(float, v)) for v in vectors]


def _auto_download_model(model_name: str) -> str:
    """Auto-download a model from HuggingFace to the sbert/ directory.

    Downloads only ONNX and tokenizer/config files (excludes PyTorch weights).
    Returns the path to the downloaded model directory.
    """
    logger = logging.getLogger(__name__)
    sbert_root = Path(__file__).resolve().parents[2] / "sbert"
    model_suffix = model_name.split("/")[-1]
    target_dir = sbert_root / model_suffix
    if target_dir.exists():
        return str(target_dir)

    logger.info(
        "Downloading model '%s' to %s (this may take a few minutes)...",
        model_name,
        target_dir,
    )
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            model_name,
            local_dir=str(target_dir),
            ignore_patterns=["*.bin", "*.safetensors", "*.h5", "openvino/*", "*.ot"],
        )
    except Exception:
        logger.exception("Failed to auto-download model '%s'", model_name)
        raise
    logger.info("Model '%s' downloaded successfully.", model_name)
    return str(target_dir)


def _build_local_model_kwargs(
    *,
    model_name: str,
    local_model_path: str,
    explicit_dim: int | None,
) -> Dict[str, Any]:
    model_dir = Path(local_model_path).expanduser().resolve()
    if not model_dir.exists():
        raise FileNotFoundError(f"SAIMemory embedding model path does not exist: {model_dir}")

    model_file = _resolve_model_file(model_dir)
    # Always use model_dir as the base: fastembed resolves model_file
    # (e.g. "onnx/model.onnx") relative to specific_model_path, so using
    # model_file_path.parent would double the "onnx/" prefix.
    model_base = model_dir
    embedding_dim = explicit_dim or _infer_embedding_dimension(model_dir)
    pooling = _infer_pooling(model_dir)
    normalization = _infer_normalization(model_dir)
    additional_files = _collect_additional_files(model_dir)

    key = (model_name.lower(), str(model_dir))

    supported_models = {
        entry["model"].lower()
        for entry in TextEmbedding.list_supported_models()
        if isinstance(entry, dict) and "model" in entry
    }

    if model_name.lower() not in supported_models and key not in _REGISTERED_MODELS:
        TextEmbedding.add_custom_model(
            model=model_name,
            pooling=pooling,
            normalization=normalization,
            sources=ModelSource(hf=f"local/{model_dir.name}"),
            dim=embedding_dim,
            model_file=model_file,
            additional_files=additional_files,
        )
        _REGISTERED_MODELS.add(key)

    return {
        "specific_model_path": str(model_base),
        "local_files_only": True,
    }


def _resolve_model_file(model_dir: Path) -> str:
    candidates = [
        "onnx/model.onnx",
        "onnx/model_optimized.onnx",
        "model.onnx",
        "model_optimized.onnx",
    ]
    for candidate in candidates:
        if (model_dir / candidate).exists():
            return candidate
    raise FileNotFoundError(f"Could not locate ONNX model inside {model_dir}")


def _infer_embedding_dimension(model_dir: Path) -> int:
    pooling_cfg = model_dir / "1_Pooling" / "config.json"
    if pooling_cfg.exists():
        try:
            data = json.loads(pooling_cfg.read_text(encoding="utf-8"))
            dim = data.get("word_embedding_dimension")
            if isinstance(dim, int) and dim > 0:
                return dim
        except Exception:
            logging.warning("Failed to read pooling config for embedding dimension at %s", pooling_cfg, exc_info=True)

    config_candidates = [
        model_dir / "config.json",
        model_dir / "sentence_bert_config.json",
    ]
    for cfg in config_candidates:
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except Exception:
                logging.warning("Failed to parse config file %s", cfg, exc_info=True)
                continue
            for key in ("word_embedding_dimension", "hidden_size", "embedding_size", "d_model"):
                value = data.get(key)
                if isinstance(value, int) and value > 0:
                    return value
    raise ValueError(f"Could not determine embedding dimension from files in {model_dir}")


def _infer_pooling(model_dir: Path) -> PoolingType:
    pooling_cfg = model_dir / "1_Pooling" / "config.json"
    if pooling_cfg.exists():
        try:
            data = json.loads(pooling_cfg.read_text(encoding="utf-8"))
            if data.get("pooling_mode_mean_tokens"):
                return PoolingType.MEAN
            if data.get("pooling_mode_cls_token"):
                return PoolingType.CLS
        except Exception:
            logging.warning("Failed to read pooling config at %s", pooling_cfg, exc_info=True)
    return PoolingType.MEAN


def _infer_normalization(model_dir: Path) -> bool:
    modules_path = model_dir / "modules.json"
    if modules_path.exists():
        try:
            data = json.loads(modules_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for module in data:
                    if isinstance(module, dict):
                        module_type = str(module.get("type", "")).lower()
                        if "normalize" in module_type:
                            return True
        except Exception:
            logging.warning("Failed to read modules.json at %s", modules_path, exc_info=True)
    # Default to True for sentence-transformer style checkpoints.
    return True


def _collect_additional_files(model_dir: Path) -> List[str]:
    candidates = [
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
        "vocab.txt",
        "modules.json",
        "config.json",
        "1_Pooling/config.json",
    ]
    extras: List[str] = []
    for rel in candidates:
        if (model_dir / rel).exists():
            extras.append(rel)
    return extras


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def semantic_recall(
    conn,
    embedder: Embedder,
    query_text: str,
    *,
    thread_id: str,
    resource_id: str | None,
    topk: int,
    range_before: int,
    range_after: int,
    scope: str,
    exclude_message_ids: set[str] | None = None,
    required_tags: list[str] | None = None,
) -> List[Message]:
    vectors: List[List[float]] = embedder.embed([query_text], is_query=True)
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id, required_tags=required_tags)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None, required_tags=required_tags)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
            continue
        if len(vec) != vector_dim:
            logging.warning(
                "semantic_recall: skipping message %s due to embedding dim mismatch (expected %s, got %s)",
                msg.id,
                vector_dim,
                len(vec),
            )
            continue
        v = np.array(vec, dtype=np.float32)
        s = _cosine_sim(q, v)
        current = scored_map.get(msg.id)
        if current is None or s > current[1]:
            scored_map[msg.id] = (msg, s, chunk_index)

    scored = list(scored_map.values())
    scored.sort(key=lambda x: x[1], reverse=True)
    picked = scored[: max(0, topk)]

    expanded: List[Message] = []
    seen = set()
    for msg, score, chunk_index in picked:
        around = get_messages_around(conn, msg.thread_id, msg.id, range_before, range_after)
        bundle = [*around[:range_before], msg, *around[range_before:]] if (range_before or range_after) else [msg]
        for m in bundle:
            if m.id in seen:
                continue
            seen.add(m.id)
            expanded.append(m)
            debug(
                "memory:recall:actual",
                id=m.id,
                role=m.role,
                thread_id=m.thread_id,
                resource_id=m.resource_id,
                created_at=m.created_at,
                score=(score if m.id == msg.id else None),
                chunk_index=(chunk_index if m.id == msg.id else None),
                preview=(m.content[:160] + "…" if len(m.content) > 160 else m.content),
            )

    expanded.sort(key=lambda m: m.created_at)
    return expanded


def semantic_recall_groups(
    conn,
    embedder: Embedder,
    query_text: str,
    *,
    thread_id: str,
    resource_id: str | None,
    topk: int,
    range_before: int,
    range_after: int,
    scope: str,
    exclude_message_ids: set[str] | None = None,
    required_tags: list[str] | None = None,
) -> List[Tuple[Message, List[Message], float]]:
    """Return top-k recall groups as (seed, group_messages_sorted, score).

    - seed: the message that matched semantically
    - group_messages_sorted: [before..., seed, after...] ordered by created_at
    - score: cosine similarity for the seed
    """
    vectors: List[List[float]] = embedder.embed([query_text], is_query=True)
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id, required_tags=required_tags)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None, required_tags=required_tags)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
            continue
        if len(vec) != vector_dim:
            logging.warning(
                "semantic_recall_groups: skipping message %s due to embedding dim mismatch (expected %s, got %s)",
                msg.id,
                vector_dim,
                len(vec),
            )
            continue
        v = np.array(vec, dtype=np.float32)
        s = _cosine_sim(q, v)
        current = scored_map.get(msg.id)
        if current is None or s > current[1]:
            scored_map[msg.id] = (msg, s, chunk_index)

    scored = list(scored_map.values())
    scored.sort(key=lambda x: x[1], reverse=True)
    picked = scored[: max(0, topk)]

    groups: List[Tuple[Message, List[Message], float]] = []
    for seed, score, chunk_index in picked:
        before_after = get_messages_around(conn, seed.thread_id, seed.id, range_before, range_after)
        # Stitch into ordered bundle
        bundle = [*before_after[:range_before], seed, *before_after[range_before:]] if (range_before or range_after) else [seed]
        # Ensure chronological
        bundle.sort(key=lambda m: m.created_at)
        groups.append((seed, bundle, score))
        debug(
            "memory:recall:group",
            seed_id=seed.id,
            seed_thread=seed.thread_id,
            seed_created_at=seed.created_at,
            size=len(bundle),
            score=score,
            chunk_index=chunk_index,
        )

    return groups


def build_context_payload(
    conn,
    embedder: Embedder,
    *,
    thread_id: str,
    resource_id: str | None,
    last_messages: int,
    semantic_enabled: bool,
    topk: int,
    range_before: int,
    range_after: int,
    scope: str,
    user_query: str,
) -> List[dict]:
    """Build context payload with the following order:
    [recall_group_system, recall_group_system, ..., recent_messages...,]

    - recent_messages are strictly the latest N messages in the current thread
    - recall groups are added as system messages before recent
    """
    # 1) recent: strictly from the same thread, chronological
    recent = get_messages_last(conn, thread_id, last_messages)

    # 2) recall groups: optional
    payload: List[dict] = []
    if semantic_enabled:
        groups = semantic_recall_groups(
            conn,
            embedder,
            user_query,
            thread_id=thread_id,
            resource_id=resource_id,
            topk=topk,
            range_before=range_before,
            range_after=range_after,
            scope=scope,
        )
        for _, bundle, score in groups:
            # Build a single system message summarizing the group
            lines: List[str] = ["以下はプロンプトから想起された過去のあなたの記憶です："]
            for m in bundle:
                role = ("assistant" if m.role == "model" else m.role)
                lines.append(f"- {role} @ {m.created_at}:")
                lines.append(compose_message_content(conn, m))
            content = "\n".join(lines)
            payload.append({"role": "system", "content": content})

    # 3) append recent in chronological order
    for m in recent:
        role = ("assistant" if m.role == "model" else m.role)
        payload.append({"role": role, "content": compose_message_content(conn, m)})

    return payload


def build_context(
    conn,
    embedder: Embedder,
    *,
    thread_id: str,
    resource_id: str | None,
    last_messages: int,
    semantic_enabled: bool,
    topk: int,
    range_before: int,
    range_after: int,
    scope: str,
    user_query: str,
) -> List[Message]:
    recent = get_messages_last(conn, thread_id, last_messages)
    if not semantic_enabled:
        return recent
    recalled = semantic_recall(
        conn,
        embedder,
        user_query,
        thread_id=thread_id,
        resource_id=resource_id,
        topk=topk,
        range_before=range_before,
        range_after=range_after,
        scope=scope,
    )
    merged: List[Message] = []
    seen = set()
    for m in recent + recalled:
        if m.id in seen:
            continue
        seen.add(m.id)
        merged.append(m)
    merged.sort(key=lambda m: m.created_at)
    return merged

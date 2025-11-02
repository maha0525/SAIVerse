from __future__ import annotations

import json
import logging
from pathlib import Path
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


class Embedder:
    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        *,
        local_model_path: str | None = None,
        model_dim: int | None = None,
    ):
        self.model_name = model
        kwargs: Dict[str, Any] = {}
        if local_model_path:
            kwargs = _build_local_model_kwargs(
                model_name=self.model_name,
                local_model_path=local_model_path,
                explicit_dim=model_dim,
            )
            logging.getLogger(__name__).warning(
                "Embedder using local path '%s' for model '%s'.",
                local_model_path,
                self.model_name,
            )
        else:
            logging.getLogger(__name__).warning(
                "Embedder using remote model '%s' without local override.",
                self.model_name,
            )
        self.model = TextEmbedding(model_name=self.model_name, **kwargs)

    def embed(self, texts: List[str]) -> List[List[float]]:
        vectors = list(self.model.embed(texts))
        return [list(map(float, v)) for v in vectors]


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
    model_file_path = (model_dir / model_file).resolve()
    model_base = model_file_path.parent if model_file_path.exists() else model_dir
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
            pass

    config_candidates = [
        model_dir / "config.json",
        model_dir / "sentence_bert_config.json",
    ]
    for cfg in config_candidates:
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except Exception:
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
            pass
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
            pass
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
) -> List[Message]:
    vectors: List[List[float]] = embedder.embed([query_text])
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None)

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
) -> List[Tuple[Message, List[Message], float]]:
    """Return top-k recall groups as (seed, group_messages_sorted, score).

    - seed: the message that matched semantically
    - group_messages_sorted: [before..., seed, after...] ordered by created_at
    - score: cosine similarity for the seed
    """
    vectors: List[List[float]] = embedder.embed([query_text])
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None)

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

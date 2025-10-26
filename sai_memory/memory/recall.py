from __future__ import annotations

from typing import List, Tuple

import numpy as np
from fastembed import TextEmbedding

from sai_memory.logging_utils import debug
from sai_memory.memory.storage import (
    Message,
    compose_message_content,
    get_embeddings_for_scope,
    get_messages_around,
    get_messages_last,
)


class Embedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        self.model_name = model
        self.model = TextEmbedding(model_name=self.model_name)

    def embed(self, texts: List[str]) -> List[List[float]]:
        vectors = list(self.model.embed(texts))
        return [list(map(float, v)) for v in vectors]


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

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
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

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
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

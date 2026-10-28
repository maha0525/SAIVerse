from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock, RLock
from typing import List, Tuple

import numpy as np

from sai_memory.config import (
    EMBED_DIM,
    EMBED_HF_REPO,
    EMBED_HF_REVISION,
    EMBED_MAX_TOKENS,
    EMBED_MODEL_FILES,
    EMBED_MODEL_ID,
    EMBED_MODEL_ONNX,
    EMBED_TOKENIZER,
    default_embed_model_dir,
    missing_embed_model_files,
)
from sai_memory.logging_utils import debug
from sai_memory.memory.storage import (
    Message,
    get_embeddings_for_scope,
    get_messages_around,
    get_messages_last,
)

LOGGER = logging.getLogger(__name__)

# Prompt strings of google/embeddinggemma-2 (config_sentence_transformers.json:
# "SearchQuery" / "Document"). They are part of the model's input contract.
QUERY_PROMPT = "task: search result | query: "
DOCUMENT_PROMPT = "title: none | text: "

# One session.run() call holds at most this many padded tokens (rows x the
# longest row). Activation memory grows with the padded size, so a fixed row
# count lets long texts blow up: 32 rows of 512 tokens peaked at 8.5GB, while
# 2048 tokens keeps the bulk peak near 650MB for both short and long chunks at
# the same throughput (measured 2026-10-14, docs/intent/embedding_gemma2_migration.md).
_MAX_BATCH_TOKENS = 2048
# Upper bound on rows even for very short texts (queries).
_MAX_BATCH_ROWS = 32
# <pad> of the EmbeddingGemma 2 tokenizer. Padded positions are masked out by
# attention_mask, so the value only has to be a valid token id.
_PAD_ID = 0
# The exported graph declares the image / video / audio inputs as required even
# for text-only calls; they are fed as empty [0, 512] tensors.
_MEDIA_INPUTS = ("image_features", "video_features", "audio_features")
_MEDIA_FEATURE_DIM = 512
_OUTPUT_NAME = "sentence_embedding"

# One loaded model per model directory, shared by every Embedder in the process
# (all personas). Loading takes the lock so a first-run download happens once.
_EMBEDDING_MODEL_CACHE: dict[str, "_OnnxTextEncoder"] = {}
_EMBEDDING_MODEL_CACHE_LOCK = RLock()
# One session.run() at a time across the process. The session is shared by
# every persona, so a bulk re-embed and an import's background embedding could
# otherwise each reach the per-call peak at once. Held per call (about a second
# at most), so a live message save waits behind one call, not a whole job.
_RUN_LOCK = Lock()


class _OnnxTextEncoder:
    """Tokenizer + onnxruntime session for the EmbeddingGemma 2 text model.

    The graph's ``sentence_embedding`` output already contains the projection,
    masked mean pooling and L2 normalisation, so no post-processing is
    reimplemented here beyond one fp32 re-normalisation.
    Runs on CPUExecutionProvider only (2026-10-12 ruling: keep the GPU free for
    local LLMs).
    """

    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(model_dir / EMBED_TOKENIZER))
        tokenizer.no_padding()
        tokenizer.enable_truncation(EMBED_MAX_TOKENS)
        self.tokenizer = tokenizer

        # The CPU memory arena keeps the largest batch's buffers for the life of
        # the process (resident stayed at 2.7GB after a bulk run). Without it the
        # buffers go back after each call (resident back to ~250MB) at no
        # measurable speed cost. Memory-pattern planning is per-shape and is
        # dropped with it.
        sess_options = ort.SessionOptions()
        sess_options.enable_cpu_mem_arena = False
        sess_options.enable_mem_pattern = False
        self.session = ort.InferenceSession(
            str(model_dir / EMBED_MODEL_ONNX),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        input_names = {i.name for i in self.session.get_inputs()}
        output_names = {o.name for o in self.session.get_outputs()}
        if _OUTPUT_NAME not in output_names:
            raise RuntimeError(
                f"Embedding model at {model_dir} has no '{_OUTPUT_NAME}' output "
                f"(outputs: {sorted(output_names)})"
            )
        self._media_inputs = [n for n in _MEDIA_INPUTS if n in input_names]

    def encode(self, texts: List[str]) -> np.ndarray:
        """Embed already-prompted texts; returns [len(texts), EMBED_DIM] float32, L2-normalised."""
        ids_list = [e.ids for e in self.tokenizer.encode_batch(texts)]
        # Longest first so each batch pads to a similar length; results are
        # written back to the original positions.
        order = sorted(range(len(ids_list)), key=lambda i: -len(ids_list[i]))
        out = np.zeros((len(ids_list), EMBED_DIM), dtype=np.float32)
        start = 0
        while start < len(order):
            # Sorted longest first, so the first row sets the padded length.
            longest = max(1, len(ids_list[order[start]]))
            rows = max(1, min(_MAX_BATCH_ROWS, _MAX_BATCH_TOKENS // longest))
            idx = order[start : start + rows]
            out[idx] = self._run([ids_list[i] for i in idx])
            start += rows
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms

    def _run(self, ids_list: List[List[int]]) -> np.ndarray:
        n = len(ids_list)
        length = max(len(x) for x in ids_list)
        ids = np.full((n, length), _PAD_ID, dtype=np.int64)
        mask = np.zeros((n, length), dtype=np.int64)
        for i, x in enumerate(ids_list):
            ids[i, : len(x)] = x
            mask[i, : len(x)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask}
        empty = np.zeros((0, _MEDIA_FEATURE_DIM), dtype=np.float32)
        for name in self._media_inputs:
            feeds[name] = empty
        with _RUN_LOCK:
            (emb,) = self.session.run([_OUTPUT_NAME], feeds)
        emb = np.asarray(emb, dtype=np.float32)
        if emb.shape != (n, EMBED_DIM):
            raise RuntimeError(
                f"Embedding model returned shape {emb.shape}, expected ({n}, {EMBED_DIM})"
            )
        return emb


class Embedder:
    """Text -> vector for recall: EmbeddingGemma 2, 4-bit ONNX, CPU only.

    The model is fixed (no selection; docs/intent/embedding_gemma2_migration.md
    §1). ``local_model_path`` points at a directory holding the files of
    ``EMBED_MODEL_FILES``; when omitted, the default directory
    ``sbert/embeddinggemma-2-q4`` is used and missing files are downloaded
    from the pinned HuggingFace revision.
    """

    def __init__(self, *, local_model_path: str | None = None) -> None:
        self.model_name = EMBED_MODEL_ID
        with _EMBEDDING_MODEL_CACHE_LOCK:
            if local_model_path:
                model_dir = Path(local_model_path).expanduser().resolve()
                missing = missing_embed_model_files(model_dir)
                if missing:
                    raise FileNotFoundError(
                        f"Embedding model directory {model_dir} is missing: {', '.join(missing)}"
                    )
            else:
                model_dir = default_embed_model_dir().resolve()
            cache_key = str(model_dir)
            encoder = _EMBEDDING_MODEL_CACHE.get(cache_key)
            if encoder is None:
                if missing_embed_model_files(model_dir):
                    _download_model(model_dir)
                LOGGER.info("Loading embedding model '%s' from %s (CPU).", self.model_name, model_dir)
                encoder = _OnnxTextEncoder(model_dir)
                _EMBEDDING_MODEL_CACHE[cache_key] = encoder
        self._encoder = encoder

    @property
    def embedding_size(self) -> int:
        """Dimension of the stored vectors (what re-embedding compares against)."""
        return EMBED_DIM

    def embed(self, texts: List[str], *, is_query: bool = False) -> List[List[float]]:
        """Embed texts. is_query=True applies the search-query prompt, else the document prompt."""
        if not texts:
            return []
        prompt = QUERY_PROMPT if is_query else DOCUMENT_PROMPT
        vectors = self._encoder.encode([prompt + t for t in texts])
        return vectors.tolist()


def _download_model(target_dir: Path) -> None:
    """Download the pinned model files into ``target_dir`` (first run / incomplete dir)."""
    LOGGER.info(
        "Downloading embedding model %s@%s to %s (about 210MB, this may take a few minutes)...",
        EMBED_HF_REPO,
        EMBED_HF_REVISION[:8],
        target_dir,
    )
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            EMBED_HF_REPO,
            revision=EMBED_HF_REVISION,
            local_dir=str(target_dir),
            allow_patterns=list(EMBED_MODEL_FILES),
        )
    except Exception:
        LOGGER.exception("Failed to download embedding model %s", EMBED_HF_REPO)
        raise
    missing = missing_embed_model_files(target_dir)
    if missing:
        raise FileNotFoundError(
            f"Embedding model download finished but {target_dir} is missing: {', '.join(missing)}"
        )
    LOGGER.info("Embedding model downloaded to %s.", target_dir)


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
    exclude_tags: list[str] | None = None,
) -> List[Message]:
    vectors: List[List[float]] = embedder.embed([query_text], is_query=True)
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if scope == "resource" and resource_id:
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id, required_tags=required_tags, exclude_tags=exclude_tags)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None, required_tags=required_tags, exclude_tags=exclude_tags)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    deferred = 0
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
            continue
        if len(vec) != vector_dim:
            if not vec:
                # The empty row of a deferred embedding (an import still being
                # embedded): expected for a while, so no warning per row.
                deferred += 1
                continue
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
    if deferred:
        LOGGER.debug("semantic_recall: skipped %d message(s) still waiting for their embeddings", deferred)

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
    exclude_tags: list[str] | None = None,
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
        corpus = get_embeddings_for_scope(conn, thread_id=None, resource_id=resource_id, required_tags=required_tags, exclude_tags=exclude_tags)
    else:
        corpus = get_embeddings_for_scope(conn, thread_id=thread_id, resource_id=None, required_tags=required_tags, exclude_tags=exclude_tags)

    scored_map: dict[str, Tuple[Message, float, int]] = {}
    deferred = 0
    for msg, vec, chunk_index in corpus:
        if exclude_message_ids and msg.id in exclude_message_ids:
            continue
        if len(vec) != vector_dim:
            if not vec:
                # The empty row of a deferred embedding (an import still being
                # embedded): expected for a while, so no warning per row.
                deferred += 1
                continue
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
    if deferred:
        LOGGER.debug("semantic_recall_groups: skipped %d message(s) still waiting for their embeddings", deferred)

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
        exclude_tags=["handy_tool"],
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

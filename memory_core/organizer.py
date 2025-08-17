from __future__ import annotations

from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone
import re
from collections import Counter

from .schemas import Topic, MemoryEntry
from .storage import StorageBackend
from .embeddings import EmbeddingProvider
from .llm import LLMClient

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokenize_ja_en(text: str) -> List[str]:
    """軽量なキーワード抽出（外部依存なし）。
    - 英語: 非英数で分割、lower、len>=3
    - カタカナ: 伸ばし棒含む連続塊をそのまま採用 len>=3（例: ソフィー、パラメータ）
    - 漢字: 連続塊をそのまま採用 len>=2（例: 旅行、会話、感情）
    余計なバイグラムは作らず、読みやすい語彙を優先する。
    """
    if not text:
        return []
    text = text.strip()
    tokens: List[str] = []
    # Katakana words (with long vowel mark)
    tokens += re.findall(r"[\u30A0-\u30FFー]{3,}", text)
    # Kanji sequences
    tokens += re.findall(r"[\u3400-\u9FFF]{2,}", text)
    # English words
    tokens += [t.lower() for t in re.split(r"[^A-Za-z0-9]+", text) if len(t) >= 3]
    # Small stoplist of generic tokens
    stop = {"こと", "それ", "これ", "ため", " さん", "よう", "です", "ます"}
    tokens = [t for t in tokens if t not in stop]
    # Dedup while preserving order
    seen = set()
    out: List[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _pick_common_keyword(topics: List[Topic], block_threshold: int) -> Tuple[str, List[str]]:
    """(keyword, candidate_topic_ids) を返す。対象は小規模トピック（< block_threshold）。
    スコアは『出現頻度 × 長さクリップ(最大6)』で、読みやすい語を優先。
    """
    small = [t for t in topics if len(t.entry_ids) < block_threshold and not getattr(t, "disabled", False)]
    if not small:
        return "", []
    freq = Counter()
    topic_map: Dict[str, List[str]] = {}
    for t in small:
        src = (t.title or "") + "\n" + (t.summary or "")
        toks = set(_tokenize_ja_en(src))
        for tok in toks:
            freq[tok] += 1
            topic_map.setdefault(tok, []).append(t.id)
    if not freq:
        return "", []
    # Score by freq * min(len, 6)
    best_tok = ""
    best_score = 0.0
    for tok, c in freq.items():
        score = float(c) * float(min(len(tok), 6))
        if c >= 2 and score > best_score:
            best_score = score
            best_tok = tok
    if not best_tok:
        return "", []
    return best_tok, topic_map.get(best_tok, [])


def run_topic_merge(
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    min_topics: int = 30,
    block_source_threshold: int = 10,
    force: bool = False,
    llm: Optional[LLMClient] = None,
) -> Dict:
    return _run_topic_merge(
        storage,
        embedder,
        min_topics,
        block_source_threshold,
        force,
        llm=llm,
    )


def _run_topic_merge(
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    min_topics: int = 30,
    block_source_threshold: int = 10,
    force: bool = False,
    sim_threshold: float = 0.15,
    max_sources: int = 8,
    llm: Optional[LLMClient] = None,
) -> Dict:
    """
    Topic merge protocol:
    - If number of topics > min_topics (or force), find most frequent shared keyword among small topics (< block_source_threshold entries).
    - Create a new generalized topic for that keyword.
    - Move all entries from the matching source topics into the new topic.
    - Set source topics to disabled once emptied (kept for history but unused).
    - Track entry.previous_topics for rollback.
    Returns a summary of the operation.
    """
    topics = storage.list_topics()
    if not force and len(topics) <= min_topics:
        return {"status": "skipped", "reason": "below_threshold", "topic_count": len(topics)}

    # ---- まず LLM による提案を試みる（オプション） ----
    keyword = ""
    candidate_ids: List[str] = []
    llm_title: Optional[str] = None
    llm_summary: Optional[str] = None
    if llm is not None:
        try:
            # LLM へ与える要約。ブロック対象（>= block_source_threshold）は最初から除外。
            # 形式: - [id=topic_x] "TITLE" — summary: ... (n_entries: N)
            lines = []
            for t in topics:
                if getattr(t, "disabled", False):
                    continue
                n = len(t.entry_ids or [])
                if n >= block_source_threshold:
                    continue
                title = (t.title or "").replace("\n", " ")
                summary = (t.summary or "").replace("\n", " ")
                lines.append(f"- [id={t.id}] \"{title}\" — summary: {summary} (n_entries: {n})")
            guide = (
                "You are a topic merger for a memory system. Given a list of small topics, "
                "propose a generalized theme that groups multiple topics, and select coherent source topics to merge.\n\n"
                "Output language policy: All strings MUST be in Japanese (keyword, title, summary).\n"
                "Return ONLY minified JSON with keys: keyword (string), sources (array of topic_id), "
                "title (string), summary (string). Rules: - Use only topics with n_entries < "
                f"{block_source_threshold}. - Select at least 2 sources. - Avoid disabled topics."
            )
            prompt = guide + "\n\nTOPICS:\n" + "\n".join(lines)
            res = llm.assign_topic(prompt)  # reuse interface; expect JSON dict
            if isinstance(res, dict):
                k = str(res.get("keyword") or "").strip()
                src = res.get("sources") or []
                if k and isinstance(src, list) and len(src) >= 2:
                    keyword = k
                    # sanitize and filter by current topics + threshold
                    valid_ids = {t.id for t in topics if not getattr(t, "disabled", False) and len(t.entry_ids or []) < block_source_threshold}
                    candidate_ids = [tid for tid in src if tid in valid_ids]
                    lt = res.get("title")
                    ls = res.get("summary")
                    if isinstance(lt, str) and lt.strip():
                        llm_title = lt.strip()
                    if isinstance(ls, str) and ls.strip():
                        llm_summary = ls.strip()
        except Exception:
            # LLM 提案に失敗したらヒューリスティックへフォールバック
            keyword = ""
            candidate_ids = []

    # ---- LLM で候補が取れない場合はヒューリスティック ----
    if not keyword or len(candidate_ids) < 2:
        keyword, candidate_ids = _pick_common_keyword(topics, block_source_threshold)
        if not keyword or len(candidate_ids) < 2:
            return {"status": "skipped", "reason": "no_common_keyword"}

    # Prepare new topic metadata
    src_topics_all = [t for t in topics if t.id in candidate_ids]
    # Similarity-based filtering to avoid over-merge
    centroids = [t.centroid_embedding for t in src_topics_all if t.centroid_embedding]
    if centroids:
        dim = len(centroids[0])
        avg = [0.0] * dim
        for v in centroids:
            for i in range(dim):
                avg[i] += v[i]
        avg = [v / len(centroids) for v in avg]
        def _cos(a: List[float], b: List[float]) -> float:
            import math
            if not a or not b or len(a) != len(b):
                return 0.0
            dot = sum(x*y for x, y in zip(a, b))
            na = math.sqrt(sum(x*x for x in a))
            nb = math.sqrt(sum(y*y for y in b))
            if na == 0.0 or nb == 0.0:
                return 0.0
            return dot / (na*nb)
        scored = []
        for t in src_topics_all:
            s = _cos(t.centroid_embedding, avg) if t.centroid_embedding else 0.0
            scored.append((s, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        src_topics = [t for s, t in scored if s >= sim_threshold][:max_sources]
    else:
        # If no centroids, just cap by size
        src_topics = src_topics_all[:max_sources]
    if len(src_topics) < 2:
        return {"status": "skipped", "reason": "insufficient_coherent_sources"}
    # Build title and summary
    # Prefer LLM-provided title/summary if available
    title = (llm_title or keyword)[:24]
    examples = " / ".join([t.title for t in src_topics[:3] if t.title])
    summary = (llm_summary or f"共通キーワード『{keyword}』を軸に統合。例: {examples}")[:160]

    # Collect distinct entry ids from sources
    all_entry_ids: List[str] = []
    for t in src_topics:
        for eid in t.entry_ids:
            if eid not in all_entry_ids:
                all_entry_ids.append(eid)

    # Compute centroid as average of existing topic centroids (if any)
    centroids = [t.centroid_embedding for t in src_topics if t.centroid_embedding]
    centroid = None
    if centroids:
        dim = len(centroids[0])
        centroid = [0.0] * dim
        for vec in centroids:
            for i in range(dim):
                centroid[i] += vec[i]
        centroid = [v / len(centroids) for v in centroid]

    # Create the new topic
    now = datetime.now(timezone.utc)
    new_topic = Topic(
        id=f"topic_merge_{now.timestamp():.0f}",
        title=title or "集約トピック",
        summary=summary,
        created_at=now,
        updated_at=now,
        strength=1.0,
        centroid_embedding=centroid,
        centroid_emotion=None,
        entry_ids=[],  # filled below
        parents=[],
        children=[t.id for t in src_topics],
        disabled=False,
    )
    storage.upsert_topic(new_topic)

    # Move entries: attach to new topic, unlink from old topics, record previous_topics
    moved = 0
    for eid in all_entry_ids:
        e = storage.get_entry(eid)
        if not e:
            continue
        # Record previous memberships
        for t in src_topics:
            if t.id in e.linked_topics and t.id not in e.previous_topics:
                e.previous_topics.append(t.id)
        # Attach to new topic
        if new_topic.id not in e.linked_topics:
            e.linked_topics.append(new_topic.id)
        # Remove from source topics
        e.linked_topics = [tid for tid in e.linked_topics if tid not in candidate_ids]
        storage.upsert_entry(e)
        new_topic.entry_ids.append(e.id)
        moved += 1
    new_topic.updated_at = datetime.now(timezone.utc)
    storage.update_topic(new_topic)

    # Disable emptied source topics
    disabled_ids: List[str] = []
    for t in src_topics:
        t.entry_ids = []
        t.disabled = True
        t.updated_at = datetime.now(timezone.utc)
        storage.update_topic(t)
        disabled_ids.append(t.id)

    return {
        "status": "merged",
        "keyword": keyword,
        "new_topic_id": new_topic.id,
        "moved_entries": moved,
        "source_topics": candidate_ids,
        "disabled_topics": disabled_ids,
    }


def nightly_reorganize(storage: StorageBackend) -> Dict:
    """
    Placeholder for offline reorganization. In a full implementation, this would:
      - Detect oversized topics and split
      - Merge isolated/near-duplicate topics
      - Update parent-child relationships
      - Recompute centroids and strengths
    Returns a summary journal for auditing.
    """
    now = _now_iso()
    return {
        "ran_at": now,
        "splits": [],
        "merges": [],
        "parents": [],
        "updates": 0,
    }

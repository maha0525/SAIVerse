from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
import math

from .schemas import MemoryEntry, Topic, EmotionVector
from .storage import StorageBackend, SearchResult
from .embeddings import EmbeddingProvider
from .config import Config


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _emotion_cosine(a: Optional[EmotionVector], b: Optional[EmotionVector]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a.values.keys()).union(b.values.keys())
    va = [a.values.get(k, 0.0) for k in keys]
    vb = [b.values.get(k, 0.0) for k in keys]
    return _cosine(va, vb)


class RetrievalEngine:
    def __init__(
        self,
        storage: StorageBackend,
        embedder: EmbeddingProvider,
        config: Config,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.cfg = config

    def _time_decay(self, dt_seconds: float) -> float:
        tau = self.cfg.time_decay_tau_seconds
        if tau <= 0:
            return 1.0
        return math.exp(-max(0.0, dt_seconds) / tau)

    def _topic_strength(self, entry: MemoryEntry) -> float:
        strengths = []
        for tid in entry.linked_topics:
            t = self.storage.get_topic(tid)
            if t:
                strengths.append(t.strength)
        if not strengths:
            return 0.0
        return sum(strengths) / len(strengths)

    def _score_entry(
        self,
        query_vec: List[float],
        now: datetime,
        current_emotion: Optional[EmotionVector],
        base_sim: float,
        e: MemoryEntry,
    ) -> float:
        w = self.cfg.weights
        sim_text = base_sim
        dt = (now - e.timestamp).total_seconds()
        s_time = self._time_decay(dt)
        s_topic = self._topic_strength(e)
        s_em = _emotion_cosine(current_emotion, e.emotion)
        s_rec = 0.0  # placeholder for activation tracking
        score = (
            w.w_sim * sim_text
            + w.w_time * s_time
            + w.w_topic * s_topic
            + w.w_em * s_em
            + w.w_recency * s_rec
        )
        return score

    def auto_recall(self, current_utterance: str, k: int = 10) -> List[MemoryEntry]:
        # E5系モデル最適化: クエリ側は `query: ` プレフィックス
        model_name = (self.cfg.embedding_model or "").lower() if hasattr(self.cfg, "embedding_model") else ""
        qtext = f"query: {current_utterance}" if "e5" in model_name else current_utterance
        query_vec = self.embedder.embed([qtext])[0]
        now = datetime.now(timezone.utc)
        initial = self.storage.search_entries(query_vec, k=max(k * 3, 20))

        # Re-rank with the full scoring
        scored = []
        for sr in initial:
            e = sr.entry
            score = self._score_entry(query_vec, now, None, sr.score, e)
            scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:k]]

    def explore(
        self,
        query: Dict,
        k: int = 20,
    ) -> Dict:
        """
        query: {"keywords": str, "topic_id": str, "time_range": [t0,t1]}
        """
        keywords = query.get("keywords") or ""
        topic_id = query.get("topic_id")
        time_range = query.get("time_range")
        t0 = t1 = None
        if time_range and len(time_range) == 2:
            t0, t1 = time_range
        model_name = (self.cfg.embedding_model or "").lower() if hasattr(self.cfg, "embedding_model") else ""
        ktext = f"query: {keywords}" if "e5" in model_name else keywords
        query_vec = self.embedder.embed([ktext])[0]
        sresults = self.storage.search_entries(
            query_vec,
            k=k,
            filter_topic_ids=[topic_id] if topic_id else None,
            time_range=(t0, t1) if t0 and t1 else None,
        )
        items = [sr.entry for sr in sresults]
        topics = []
        if topic_id:
            t = self.storage.get_topic(topic_id)
            if t:
                topics.append(t)
        return {
            "results": items,
            "topics": topics,
        }

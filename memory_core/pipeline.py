from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timezone
import uuid

from .schemas import MemoryEntry, Topic, EmotionVector
from .storage import StorageBackend, InMemoryStorage
from .embeddings import EmbeddingProvider, SimpleHashEmbedding
from .emotion import infer_emotion
from .topic_assigner import assign_topic, assign_topic_llm
from .retriever import RetrievalEngine
from .config import Config
from .llm import LLMClient, DummyLLM


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _summarize(text: str, max_len: int = 120) -> str:
    text = text.strip().replace("\n", " ")
    return text[:max_len]


@dataclass
class MemoryCore:
    storage: StorageBackend
    embedder: EmbeddingProvider
    config: Config
    llm: Optional[LLMClient] = None

    @classmethod
    def create_default(
        cls,
        config: Optional[Config] = None,
        with_dummy_llm: bool = True,
        llm_backend: Optional[str] = None,  # "dummy" | "ollama_http" | "ollama_cli"
    ) -> "MemoryCore":
        # Load config (from env if not provided)
        cfg = config or Config.from_env()
        # Choose storage backend
        if cfg.storage_backend == "qdrant":
            try:
                from .storage import QdrantStorage  # lazy import
                storage: StorageBackend = QdrantStorage(cfg)
            except Exception as e:
                # Fallback to in-memory with a notice
                print(f"Storage notice: Qdrant unavailable ({e}); falling back to InMemoryStorage")
                storage = InMemoryStorage()
        else:
            storage = InMemoryStorage()
        # Select embedding provider
        embedder = None
        if cfg.embedding_provider == "sbert":
            try:
                from .embeddings import SBERTEmbedding
                embedder = SBERTEmbedding(model_name_or_path=cfg.embedding_model, device=cfg.embedding_device, dim_hint=cfg.embedding_dim)
                # Probe once to get a vector and infer dim if needed
                if cfg.embedding_dim and cfg.embedding_dim > 0:
                    pass
            except Exception as e:
                print(f"Embedding notice: SBERT unavailable ({e}); falling back to SimpleHash")
        elif cfg.embedding_provider == "hf":
            try:
                from .embeddings import HFTransformersEmbedding
                embedder = HFTransformersEmbedding(model_name_or_path=cfg.embedding_model, device=cfg.embedding_device)
            except Exception as e:
                print(f"Embedding notice: HF transformers unavailable ({e}); falling back to SimpleHash")
        if embedder is None:
            embedder = SimpleHashEmbedding(dim=cfg.embedding_dim, normalize=cfg.normalize_embeddings)
        # Select LLM backend (env-configurable)
        from .llm import OllamaHTTPAssignLLM, OllamaLLM  # local imports
        llm: Optional[LLMClient]
        chosen = llm_backend or getattr(cfg, "assign_llm_backend", None) or ("dummy" if with_dummy_llm else None)
        if chosen == "ollama_http":
            try:
                model = getattr(cfg, "assign_llm_model", None) or "qwen2.5:3b"
                llm = OllamaHTTPAssignLLM(model=model)
            except Exception as e:
                print(f"Assign LLM notice: ollama_http unavailable ({e}); using DummyLLM")
                llm = DummyLLM() if with_dummy_llm else None
        elif chosen == "ollama_cli":
            try:
                model = getattr(cfg, "assign_llm_model", None) or "qwen2.5:3b"
                llm = OllamaLLM(model=model)
            except Exception as e:
                print(f"Assign LLM notice: ollama_cli unavailable ({e}); using DummyLLM")
                llm = DummyLLM() if with_dummy_llm else None
        else:
            llm = DummyLLM() if with_dummy_llm else None
        return cls(storage=storage, embedder=embedder, config=cfg, llm=llm)

    # -------------------- Pipeline --------------------
    def ingest_turn(
        self,
        conv_id: str,
        turn_index: int,
        speaker: str,
        text: str,
        meta: Dict,
    ) -> MemoryEntry:
        eid = _gen_id("entry")
        ts = _now()
        # E5系モデル最適化: 文書側は `passage: ` プレフィックス
        model_name = (self.config.embedding_model or "").lower()
        etext = f"passage: {text}" if "e5" in model_name else text
        emb = self.embedder.embed([etext])[0]
        emo = infer_emotion(text)
        entry = MemoryEntry(
            id=eid,
            conversation_id=conv_id,
            turn_index=turn_index,
            timestamp=ts,
            speaker=speaker,
            raw_text=text,
            summary=_summarize(text),
            embedding=emb,
            emotion=emo,
            meta=meta or {},
        )
        self.storage.upsert_entry(entry)

        # Topic assignment
        recent_dialog = self._collect_recent_dialog(conv_id, n=self.config.recent_dialog_turns)
        if self.llm is not None:
            decision = assign_topic_llm(
                recent_dialog=recent_dialog,
                candidate_topics=self.storage.list_topics(),
                llm=self.llm,
            )
        else:
            decision = assign_topic(
                recent_dialog=recent_dialog,
                candidate_topics=self.storage.list_topics(),
                embedder=self.embedder,
                threshold=self.config.topic_match_threshold,
            )
        if decision.get("decision") == "BEST_MATCH" and decision.get("topic_id"):
            topic = self.storage.get_topic(decision["topic_id"])  # type: ignore
            if topic:
                self._attach_entry_to_topic(entry, topic)
        else:
            nt = decision.get("new_topic") or {}
            # Accept both dict and string from LLM output
            if isinstance(nt, str):
                title = (nt[:24] + "…") if len(nt) > 24 else nt
                summary = nt[:160]
                nt = {"title": title or "新しい話題", "summary": summary or None}
            topic = Topic(
                id=_gen_id("topic"),
                title=(nt.get("title") if isinstance(nt, dict) else "新しい話題") or "新しい話題",
                summary=(nt.get("summary") if isinstance(nt, dict) else None),
                created_at=ts,
                updated_at=ts,
                strength=0.1,
                centroid_embedding=emb[:],
                centroid_emotion=emo,
                entry_ids=[eid],
            )
            self.storage.upsert_topic(topic)
            entry.linked_topics.append(topic.id)

        # Link with previous turn in same conversation
        if turn_index > 0:
            prev_id = self._find_entry_id(conv_id, turn_index - 1)
            if prev_id:
                self.storage.link_entries(entry.id, prev_id)

        # Upsert updated entry
        self.storage.upsert_entry(entry)
        return entry

    def link_entries(self, entry_id_a: str, entry_id_b: str, relation: str = "contextual") -> None:
        self.storage.link_entries(entry_id_a, entry_id_b)

    # -------------------- Retrieval --------------------
    def auto_recall(self, current_utterance: str, k: Optional[int] = None) -> List[MemoryEntry]:
        engine = RetrievalEngine(self.storage, self.embedder, self.config)
        return engine.auto_recall(current_utterance, k or self.config.retrieval_top_k)

    def explore(self, query: Dict, k: int = 20) -> Dict:
        engine = RetrievalEngine(self.storage, self.embedder, self.config)
        return engine.explore(query, k=k)

    # -------------------- Helpers --------------------
    def _find_entry_id(self, conv_id: str, turn_idx: int) -> Optional[str]:
        try:
            items = self.storage.list_entries_by_conversation(conv_id)
        except Exception:
            items = []
        for e in items:
            if e.turn_index == turn_idx:
                return e.id
        return None

    def _collect_recent_dialog(self, conv_id: str, n: int = 6) -> List[Dict]:
        try:
            items = self.storage.list_entries_by_conversation(conv_id)
        except Exception:
            items = []
        turns = []
        for e in items[-n:]:
            turns.append({"speaker": e.speaker, "text": e.raw_text})
        return turns

    def _attach_entry_to_topic(self, entry: MemoryEntry, topic: Topic) -> None:
        if entry.id not in topic.entry_ids:
            topic.entry_ids.append(entry.id)
        if topic.id not in entry.linked_topics:
            entry.linked_topics.append(topic.id)
        # EMA centroid + strength
        a = self.config.ema_alpha_centroid
        if topic.centroid_embedding and entry.embedding:
            ce = topic.centroid_embedding
            topic.centroid_embedding = [
                (1 - a) * ce[i] + a * entry.embedding[i] for i in range(len(ce))
            ]
        topic.strength = (1 - self.config.ema_alpha_strength) * topic.strength + self.config.ema_alpha_strength * 1.0
        topic.updated_at = _now()
        self.storage.update_topic(topic)

    # -------------------- High-level API --------------------
    def remember(self, text: str, conv_id: str = "default", speaker: str = "user", meta: Optional[Dict] = None) -> MemoryEntry:
        idx = self._next_turn_index(conv_id)
        return self.ingest_turn(conv_id, idx, speaker, text, meta or {})

    def recall(self, text: str, k: int = 5) -> Dict:
        entries = self.auto_recall(text, k=k)
        # collect topics linked to top entries
        topic_ids = []
        for e in entries:
            for tid in e.linked_topics:
                if tid not in topic_ids:
                    topic_ids.append(tid)
        topics = [self.storage.get_topic(tid) for tid in topic_ids]
        topics = [t for t in topics if t is not None]
        return {
            "texts": [e.raw_text for e in entries],
            "topics": topics,
            "entries": entries,
        }

    def _next_turn_index(self, conv_id: str) -> int:
        try:
            items = self.storage.list_entries_by_conversation(conv_id)
        except Exception:
            items = []
        if not items:
            return 0
        return max(e.turn_index for e in items) + 1

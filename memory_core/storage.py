from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from datetime import datetime, timezone
import math
from pathlib import Path
import os
import uuid

from .schemas import MemoryEntry, Topic
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


@dataclass
class SearchResult:
    entry: MemoryEntry
    score: float


class StorageBackend:
    def upsert_entry(self, entry: MemoryEntry) -> None:
        raise NotImplementedError

    def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        raise NotImplementedError

    def link_entries(self, a_id: str, b_id: str) -> None:
        raise NotImplementedError

    def search_entries(
        self,
        query_embedding: List[float],
        k: int = 10,
        filter_speaker: Optional[str] = None,
        filter_topic_ids: Optional[List[str]] = None,
        time_range: Optional[Tuple[datetime, datetime]] = None,
    ) -> List[SearchResult]:
        raise NotImplementedError

    def list_entries_by_conversation(self, conv_id: str) -> List[MemoryEntry]:
        """Return all entries for a conversation in increasing turn order.
        Implementations should avoid loading embeddings when not needed.
        """
        raise NotImplementedError

    def upsert_topic(self, topic: Topic) -> None:
        raise NotImplementedError

    def get_topic(self, topic_id: str) -> Optional[Topic]:
        raise NotImplementedError

    def list_topics(self) -> List[Topic]:
        raise NotImplementedError

    def update_topic(self, topic: Topic) -> None:
        raise NotImplementedError


class InMemoryStorage(StorageBackend):
    def __init__(self) -> None:
        self.entries: Dict[str, MemoryEntry] = {}
        self.topics: Dict[str, Topic] = {}

    def upsert_entry(self, entry: MemoryEntry) -> None:
        self.entries[entry.id] = entry

    def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        return self.entries.get(entry_id)

    def link_entries(self, a_id: str, b_id: str) -> None:
        a = self.entries.get(a_id)
        b = self.entries.get(b_id)
        if not a or not b:
            return
        if b_id not in a.linked_entries:
            a.linked_entries.append(b_id)
        if a_id not in b.linked_entries:
            b.linked_entries.append(a_id)

    def search_entries(
        self,
        query_embedding: List[float],
        k: int = 10,
        filter_speaker: Optional[str] = None,
        filter_topic_ids: Optional[List[str]] = None,
        time_range: Optional[Tuple[datetime, datetime]] = None,
    ) -> List[SearchResult]:
        candidates: List[MemoryEntry] = list(self.entries.values())

        if filter_speaker:
            candidates = [e for e in candidates if e.speaker == filter_speaker]
        if filter_topic_ids:
            tids = set(filter_topic_ids)
            candidates = [e for e in candidates if tids.intersection(set(e.linked_topics))]
        if time_range:
            t0, t1 = time_range
            candidates = [e for e in candidates if t0 <= e.timestamp <= t1]

        scored: List[SearchResult] = []
        for e in candidates:
            s = 0.0
            if e.embedding:
                s = _cosine(query_embedding, e.embedding)
            scored.append(SearchResult(entry=e, score=s))

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]

    def upsert_topic(self, topic: Topic) -> None:
        self.topics[topic.id] = topic

    def get_topic(self, topic_id: str) -> Optional[Topic]:
        return self.topics.get(topic_id)

    def list_topics(self) -> List[Topic]:
        return list(self.topics.values())

    def update_topic(self, topic: Topic) -> None:
        self.topics[topic.id] = topic

    def list_entries_by_conversation(self, conv_id: str) -> List[MemoryEntry]:
        items = [e for e in self.entries.values() if e.conversation_id == conv_id]
        items.sort(key=lambda x: x.turn_index)
        return items


class QdrantStorage(StorageBackend):
    """
    Qdrant-backed storage. Requires `qdrant-client` to be installed and a running Qdrant server.
    Collections:
      - {prefix}_entries (vector: embedding)
      - {prefix}_topics  (vector: centroid)
    """

    def __init__(self, config: Config) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as qmodels
        except Exception as e:
            raise RuntimeError("qdrant-client is not available. Please install it to use QdrantStorage.") from e

        self.cfg = config
        self._qmodels = qmodels
        # Prefer HTTP URL if provided, else run embedded (standalone) instance
        if config.qdrant_url:
            self.client = QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key)
        else:
            # Embedded mode: use provided location or default under ~/.saiverse/qdrant
            loc_raw = (config.qdrant_location or "").strip()
            if not loc_raw:
                loc_raw = str(Path.home() / ".saiverse" / "qdrant")
            # Expand ~ and env vars
            loc_expanded = os.path.expandvars(os.path.expanduser(loc_raw))
            # Special in-memory mode
            if loc_expanded == ":memory:":
                try:
                    self.client = QdrantClient(location=loc_expanded)
                except TypeError:
                    self.client = QdrantClient(path=loc_expanded)
            else:
                loc_path = Path(loc_expanded)
                loc_path.mkdir(parents=True, exist_ok=True)
                loc = str(loc_path)
                # qdrant-client changed parameter name across versions. Prefer path= first.
                try:
                    self.client = QdrantClient(path=loc)
                except TypeError:
                    self.client = QdrantClient(location=loc)
                except Exception as e:
                    # Permission or other local path error: try project-local fallback
                    if "Permission denied" in str(e) or "permission" in str(e).lower():
                        fallback = str((Path.cwd() / ".qdrant").resolve())
                        (Path(fallback)).mkdir(parents=True, exist_ok=True)
                        try:
                            self.client = QdrantClient(path=fallback)
                        except TypeError:
                            self.client = QdrantClient(location=fallback)
                    else:
                        raise
        self.entries_col = f"{config.qdrant_collection_prefix}_entries"
        self.topics_col = f"{config.qdrant_collection_prefix}_topics"
        self._ensure_collections()

    # -------------------- Collection management --------------------
    def _ensure_collections(self) -> None:
        qmodels = self._qmodels
        dim = self.cfg.embedding_dim
        # Entries
        if not self.client.collection_exists(self.entries_col):
            self.client.create_collection(
                collection_name=self.entries_col,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )
        # Topics
        if not self.client.collection_exists(self.topics_col):
            self.client.create_collection(
                collection_name=self.topics_col,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )

    # -------------------- Helpers --------------------
    def _dt_to_ts(self, dt: datetime) -> float:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    # -------------------- Entries --------------------
    def upsert_entry(self, entry: MemoryEntry) -> None:
        qmodels = self._qmodels
        # Ensure Qdrant-friendly point id (UUID). Keep original id as 'sid'.
        point_id = None
        try:
            point_id = uuid.UUID(str(entry.id))
        except Exception:
            point_id = uuid.uuid5(uuid.NAMESPACE_URL, f"saiverse:entry:{entry.id}")
        payload = {
            "sid": entry.id,
            "conversation_id": entry.conversation_id,
            "turn_index": entry.turn_index,
            "timestamp": entry.timestamp.isoformat(),
            "ts": self._dt_to_ts(entry.timestamp),
            "speaker": entry.speaker,
            "raw_text": entry.raw_text,
            "summary": entry.summary,
            "emotion": entry.emotion.to_dict() if entry.emotion else None,
            "linked_topics": entry.linked_topics,
            "linked_entries": entry.linked_entries,
            "meta": entry.meta,
            "raw_pointer": entry.raw_pointer,
        }
        vec = entry.embedding or []
        self.client.upsert(
            collection_name=self.entries_col,
            points=[qmodels.PointStruct(id=str(point_id), vector=vec, payload=payload)],
        )

    def get_entry(self, entry_id: str) -> Optional[MemoryEntry]:
        # Try by UUID id first
        p = None
        try:
            res = self.client.retrieve(collection_name=self.entries_col, ids=[str(uuid.UUID(str(entry_id)))])
            if res:
                p = res[0]
        except Exception:
            p = None
        # Fallback: search by sid
        if p is None:
            from qdrant_client.http import models as qmodels
            filt = qmodels.Filter(must=[qmodels.FieldCondition(key="sid", match=qmodels.MatchValue(value=str(entry_id)))])
            pts, _ = self.client.scroll(collection_name=self.entries_col, scroll_filter=filt, with_payload=True, with_vectors=True, limit=1)
            if pts:
                p = pts[0]
        if p is None:
            return None
        payload = p.payload or {}
        emo = payload.get("emotion")
        ev = None
        if emo:
            from .schemas import EmotionVector

            ev = EmotionVector(values=emo.get("values", {}), confidence=emo.get("confidence", 0.0))
        ts = payload.get("timestamp")
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.fromtimestamp(payload.get("ts", 0), tz=timezone.utc)
        return MemoryEntry(
            id=str(payload.get("sid") or p.id),
            conversation_id=payload.get("conversation_id", ""),
            turn_index=int(payload.get("turn_index", 0)),
            timestamp=dt,
            speaker=payload.get("speaker", ""),
            raw_text=payload.get("raw_text", ""),
            summary=payload.get("summary"),
            embedding=p.vector if isinstance(p.vector, list) else None,  # type: ignore[attr-defined]
            emotion=ev,
            linked_topics=list(payload.get("linked_topics", []) or []),
            linked_entries=list(payload.get("linked_entries", []) or []),
            meta=dict(payload.get("meta", {}) or {}),
            raw_pointer=payload.get("raw_pointer"),
        )

    def link_entries(self, a_id: str, b_id: str) -> None:
        a = self.get_entry(a_id)
        b = self.get_entry(b_id)
        if not a or not b:
            return
        if b_id not in a.linked_entries:
            a.linked_entries.append(b_id)
            from qdrant_client.http import models as qmodels
            self.client.set_payload(
                collection_name=self.entries_col,
                payload={"linked_entries": a.linked_entries},
                points=qmodels.Filter(must=[qmodels.FieldCondition(key="sid", match=qmodels.MatchValue(value=str(a_id)))]),
            )
        if a_id not in b.linked_entries:
            b.linked_entries.append(a_id)
            from qdrant_client.http import models as qmodels
            self.client.set_payload(
                collection_name=self.entries_col,
                payload={"linked_entries": b.linked_entries},
                points=qmodels.Filter(must=[qmodels.FieldCondition(key="sid", match=qmodels.MatchValue(value=str(b_id)))]),
            )

    def search_entries(
        self,
        query_embedding: List[float],
        k: int = 10,
        filter_speaker: Optional[str] = None,
        filter_topic_ids: Optional[List[str]] = None,
        time_range: Optional[Tuple[datetime, datetime]] = None,
    ) -> List[SearchResult]:
        qmodels = self._qmodels
        must: List[qmodels.Condition] = []
        if filter_speaker:
            must.append(qmodels.FieldCondition(key="speaker", match=qmodels.MatchValue(value=filter_speaker)))
        if filter_topic_ids:
            must.append(qmodels.FieldCondition(key="linked_topics", match=qmodels.MatchAny(any=filter_topic_ids)))
        if time_range:
            t0, t1 = time_range
            must.append(qmodels.FieldCondition(key="ts", range=qmodels.Range(gte=self._dt_to_ts(t0), lte=self._dt_to_ts(t1))))
        qfilter = qmodels.Filter(must=must) if must else None
        res = self.client.search(
            collection_name=self.entries_col,
            query_vector=query_embedding,
            limit=k,
            query_filter=qfilter,
        )
        out: List[SearchResult] = []
        for r in res:
            e = self.get_entry(str(r.id))
            if e is not None:
                out.append(SearchResult(entry=e, score=float(r.score)))
        return out

    # -------------------- Topics --------------------
    def upsert_topic(self, topic: Topic) -> None:
        qmodels = self._qmodels
        # Ensure UUID id; store original as 'sid'
        try:
            tid = uuid.UUID(str(topic.id))
        except Exception:
            tid = uuid.uuid5(uuid.NAMESPACE_URL, f"saiverse:topic:{topic.id}")
        payload = {
            "sid": topic.id,
            "title": topic.title,
            "summary": topic.summary,
            "created_at": topic.created_at.isoformat(),
            "updated_at": topic.updated_at.isoformat(),
            "strength": topic.strength,
            "centroid_emotion": topic.centroid_emotion.to_dict() if topic.centroid_emotion else None,
            "entry_ids": topic.entry_ids,
            "parents": topic.parents,
            "children": topic.children,
        }
        vec = topic.centroid_embedding or [0.0] * self.cfg.embedding_dim
        self.client.upsert(
            collection_name=self.topics_col,
            points=[qmodels.PointStruct(id=str(tid), vector=vec, payload=payload)],
        )

    def get_topic(self, topic_id: str) -> Optional[Topic]:
        p = None
        try:
            res = self.client.retrieve(collection_name=self.topics_col, ids=[str(uuid.UUID(str(topic_id)))])
            if res:
                p = res[0]
        except Exception:
            p = None
        if p is None:
            from qdrant_client.http import models as qmodels
            filt = qmodels.Filter(must=[qmodels.FieldCondition(key="sid", match=qmodels.MatchValue(value=str(topic_id)))])
            pts, _ = self.client.scroll(collection_name=self.topics_col, scroll_filter=filt, with_payload=True, with_vectors=True, limit=1)
            if pts:
                p = pts[0]
        if p is None:
            return None
        payload = p.payload or {}
        from .schemas import EmotionVector

        ce = payload.get("centroid_emotion")
        ev = None
        if ce:
            ev = EmotionVector(values=ce.get("values", {}), confidence=ce.get("confidence", 0.0))
        ca = payload.get("created_at")
        ua = payload.get("updated_at")
        created_at = datetime.fromisoformat(ca) if isinstance(ca, str) else datetime.now(timezone.utc)
        updated_at = datetime.fromisoformat(ua) if isinstance(ua, str) else datetime.now(timezone.utc)
        return Topic(
            id=str(payload.get("sid") or p.id),
            title=payload.get("title", ""),
            summary=payload.get("summary"),
            created_at=created_at,
            updated_at=updated_at,
            strength=float(payload.get("strength", 0.0)),
            centroid_embedding=p.vector if isinstance(p.vector, list) else None,  # type: ignore[attr-defined]
            centroid_emotion=ev,
            entry_ids=list(payload.get("entry_ids", []) or []),
            parents=list(payload.get("parents", []) or []),
            children=list(payload.get("children", []) or []),
        )

    def list_topics(self) -> List[Topic]:
        qmodels = self._qmodels
        out: List[Topic] = []
        next_page = None
        while True:
            pts, next_page = self.client.scroll(collection_name=self.topics_col, with_payload=True, with_vectors=True, limit=256, offset=next_page)
            for p in pts:
                t = self.get_topic(str(p.id))
                if t:
                    out.append(t)
            if next_page is None:
                break
        return out

    def update_topic(self, topic: Topic) -> None:
        self.upsert_topic(topic)

    def list_entries_by_conversation(self, conv_id: str) -> List[MemoryEntry]:
        qmodels = self._qmodels
        filt = qmodels.Filter(must=[qmodels.FieldCondition(key="conversation_id", match=qmodels.MatchValue(value=conv_id))])
        out: List[MemoryEntry] = []
        next_page = None
        while True:
            pts, next_page = self.client.scroll(
                collection_name=self.entries_col,
                scroll_filter=filt,
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=next_page,
            )
            for p in pts:
                payload = p.payload or {}
                emo = payload.get("emotion")
                ev = None
                if emo:
                    from .schemas import EmotionVector
                    ev = EmotionVector(values=emo.get("values", {}), confidence=emo.get("confidence", 0.0))
                ts = payload.get("timestamp")
                dt = datetime.fromisoformat(ts) if isinstance(ts, str) else datetime.now(timezone.utc)
                out.append(
                    MemoryEntry(
                        id=str(p.id),
                        conversation_id=payload.get("conversation_id", ""),
                        turn_index=int(payload.get("turn_index", 0)),
                        timestamp=dt,
                        speaker=payload.get("speaker", ""),
                        raw_text=payload.get("raw_text", ""),
                        summary=payload.get("summary"),
                        embedding=None,
                        emotion=ev,
                        linked_topics=list(payload.get("linked_topics", []) or []),
                        linked_entries=list(payload.get("linked_entries", []) or []),
                        meta=dict(payload.get("meta", {}) or {}),
                        raw_pointer=payload.get("raw_pointer"),
                    )
                )
            if next_page is None:
                break
        out.sort(key=lambda x: x.turn_index)
        return out

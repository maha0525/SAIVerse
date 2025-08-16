from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from datetime import datetime


EMO_KEYS = [
    "joy",
    "peace",
    "trust",
    "curiosity",
    "flutter",
    "playfulness",
    "empathy",
    "hope",
    "conflict",
    "anxiety",
    "sadness",
    "anger",
]


@dataclass
class EmotionVector:
    values: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {"values": self.values, "confidence": self.confidence}


@dataclass
class MemoryEntry:
    id: str
    conversation_id: str
    turn_index: int
    timestamp: datetime
    speaker: str  # "user" | "ai"
    raw_text: str
    summary: Optional[str] = None
    embedding: Optional[List[float]] = None
    emotion: Optional[EmotionVector] = None
    linked_topics: List[str] = field(default_factory=list)
    linked_entries: List[str] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)
    raw_pointer: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.emotion is not None:
            d["emotion"] = self.emotion.to_dict()
        return d


@dataclass
class Topic:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    summary: Optional[str] = None
    strength: float = 0.0
    centroid_embedding: Optional[List[float]] = None
    centroid_emotion: Optional[EmotionVector] = None
    entry_ids: List[str] = field(default_factory=list)
    parents: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.centroid_emotion is not None:
            d["centroid_emotion"] = self.centroid_emotion.to_dict()
        return d


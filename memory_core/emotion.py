from __future__ import annotations

from typing import Dict
from .schemas import EmotionVector, EMO_KEYS


_LEXICON: Dict[str, Dict[str, float]] = {
    # positive
    "嬉": {"joy": 0.9},
    "楽": {"playfulness": 0.8, "joy": 0.6},
    "安心": {"peace": 0.8},
    "信頼": {"trust": 0.9},
    "好奇": {"curiosity": 0.9},
    "希望": {"hope": 0.9},
    "共感": {"empathy": 0.9},
    # negative
    "不安": {"anxiety": 0.9},
    "悲": {"sadness": 0.9},
    "怒": {"anger": 0.9},
    "葛藤": {"conflict": 0.9},
}


def infer_emotion(text: str) -> EmotionVector:
    acc: Dict[str, float] = {k: 0.0 for k in EMO_KEYS}
    total = 0.0
    for key, vec in _LEXICON.items():
        if key in text:
            for k, v in vec.items():
                acc[k] += v
                total += abs(v)
    # normalize to [-1,1] scale where applicable; here we clamp to [0,1]
    if total > 0:
        for k in acc:
            acc[k] = max(-1.0, min(1.0, acc[k] / total))
        confidence = min(1.0, total / 3.0)
    else:
        confidence = 0.1
    return EmotionVector(values=acc, confidence=confidence)


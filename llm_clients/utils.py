"""Utility helpers shared by LLM client implementations."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

_log = logging.getLogger(__name__)


def content_to_text(content: Any) -> str:
    """Extract plain text from SDK message payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if text:
                texts.append(str(text))
        return "".join(texts)
    return ""


def obj_to_dict(obj: Any) -> Any:
    """Best-effort conversion to plain dict for SDK objects."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            _log.warning("obj_to_dict: model_dump() failed for %s", type(obj).__name__, exc_info=True)
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return obj.to_dict()
        except Exception:
            _log.warning("obj_to_dict: to_dict() failed for %s", type(obj).__name__, exc_info=True)
    return obj


def is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def merge_reasoning_strings(chunks: List[str]) -> List[Dict[str, str]]:
    if not chunks:
        return []
    text = "".join(chunks).strip()
    if not text:
        return []
    return [{"title": "", "text": text}]

"""OpenAI reasoning extraction and streaming merge helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .utils import obj_to_dict


def extract_reasoning_from_message(message: Any) -> Tuple[str, List[Dict[str, str]], Any]:
    """Extract text, reasoning entries, and raw reasoning_details from an OpenAI message."""
    msg_dict = obj_to_dict(message) or {}
    content = msg_dict.get("content")
    reasoning_entries: List[Dict[str, str]] = []
    text_segments: List[str] = []

    def append_reasoning(text: str, title: Optional[str] = None) -> None:
        text = (text or "").strip()
        if text:
            reasoning_entries.append({"title": title or "", "text": text})

    if isinstance(content, list):
        for part in content:
            part_dict = obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if not text:
                continue
            if ptype in {"reasoning", "thinking", "analysis"}:
                append_reasoning(text, part_dict.get("title"))
            elif ptype in {"output_text", "text", None}:
                text_segments.append(text)
    elif isinstance(content, str):
        text_segments.append(content)

    reasoning_content = msg_dict.get("reasoning_content")
    if isinstance(reasoning_content, str):
        append_reasoning(reasoning_content)

    if msg_dict.get("reasoning") and isinstance(msg_dict["reasoning"], dict):
        rc = msg_dict["reasoning"].get("content")
        if isinstance(rc, str):
            append_reasoning(rc)

    reasoning_details = msg_dict.get("reasoning_details")
    return "".join(text_segments), reasoning_entries, reasoning_details


def extract_reasoning_from_delta(delta: Any) -> List[str]:
    reasoning_chunks: List[str] = []
    delta_dict = obj_to_dict(delta)
    if not isinstance(delta_dict, dict):
        return reasoning_chunks

    raw_reasoning = delta_dict.get("reasoning")
    if isinstance(raw_reasoning, list):
        for item in raw_reasoning:
            item_dict = obj_to_dict(item) or {}
            text = item_dict.get("text") or item_dict.get("content") or ""
            if text:
                reasoning_chunks.append(text)
    elif isinstance(raw_reasoning, str):
        reasoning_chunks.append(raw_reasoning)

    reasoning_content = delta_dict.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        reasoning_chunks.append(reasoning_content)

    raw_rd = delta_dict.get("reasoning_details")
    if isinstance(raw_rd, list) and not reasoning_chunks:
        for item in raw_rd:
            item_dict = obj_to_dict(item) or item if isinstance(item, dict) else {}
            text = item_dict.get("text") or item_dict.get("summary") or ""
            if text:
                reasoning_chunks.append(text)

    return reasoning_chunks


def extract_raw_reasoning_details_from_delta(delta: Any) -> List[Dict[str, Any]]:
    """Extract raw reasoning_details objects from a streaming delta for multi-turn passback."""
    delta_dict = obj_to_dict(delta)
    if not isinstance(delta_dict, dict):
        return []
    raw_rd = delta_dict.get("reasoning_details")
    if not isinstance(raw_rd, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in raw_rd:
        d = obj_to_dict(item) if not isinstance(item, dict) else item
        if isinstance(d, dict):
            result.append(d)
    return result


def merge_streaming_reasoning_details(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge incremental reasoning_details chunks from streaming into consolidated entries."""
    by_index: Dict[int, Dict[str, Any]] = {}
    for item in items:
        idx = item.get("index", 0)
        if idx not in by_index:
            by_index[idx] = dict(item)
        else:
            existing = by_index[idx]
            for text_key in ("text", "summary"):
                chunk_text = item.get(text_key, "")
                if chunk_text:
                    existing[text_key] = existing.get(text_key, "") + chunk_text
    return [by_index[k] for k in sorted(by_index.keys())]


def process_openai_stream_content(content: Any) -> Tuple[str, List[str]]:
    reasoning_chunks: List[str] = []
    text_fragments: List[str] = []

    if isinstance(content, list):
        for part in content:
            part_dict = obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if not text:
                continue
            if ptype in {"reasoning", "thinking", "analysis"}:
                reasoning_chunks.append(text)
            else:
                text_fragments.append(text)
    elif isinstance(content, str):
        text_fragments.append(content)

    return "".join(text_fragments), reasoning_chunks

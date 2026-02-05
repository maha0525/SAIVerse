"""Search memory with brief snippets and message IDs for selection."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.recall import semantic_recall_groups
from sai_memory.memory.storage import (
    get_all_messages_for_search,
    get_messages_last,
    Message,
)
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def _extract_snippet(content: str, keywords: Optional[List[str]], max_chars: int) -> str:
    """Extract a snippet centered on the first keyword match, or from the start."""
    if not content:
        return ""

    # Try to find a keyword match position
    if keywords:
        content_lower = content.lower()
        best_pos = -1
        for kw in keywords:
            pos = content_lower.find(kw.lower())
            if pos >= 0:
                if best_pos < 0 or pos < best_pos:
                    best_pos = pos

        if best_pos >= 0:
            # Center the snippet around the match
            half = max_chars // 2
            start = max(0, best_pos - half)
            end = min(len(content), start + max_chars)
            # Adjust start if we're near the end
            if end - start < max_chars:
                start = max(0, end - max_chars)
            snippet = content[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(content) else ""
            return f"{prefix}{snippet}{suffix}"

    # No keyword match or no keywords: show from beginning
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def memory_search_brief(
    query: str = "",
    keywords: Optional[List[str]] = None,
    topk: int = 10,
    max_snippet_chars: int = 100,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """Search memory and return brief snippets with message IDs.

    Returns a numbered list of results with message IDs, timestamps,
    roles, scores, and short content snippets centered on keyword matches.

    - query: semantic search query
    - keywords: keywords for exact substring matching (combined with semantic via RRF)
    - topk: number of results to return
    - max_snippet_chars: max characters per snippet
    - start_date: filter by start date (YYYY-MM-DD)
    - end_date: filter by end date (YYYY-MM-DD)
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    if not query and not keywords:
        return "(no query or keywords provided)"

    # Parse date range
    start_ts = None
    end_ts = None
    if start_date:
        try:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        except ValueError:
            pass
    if end_date:
        try:
            end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400 - 1
        except ValueError:
            pass

    rrf_k = 60
    message_scores: Dict[str, float] = defaultdict(float)
    message_data: Dict[str, Message] = {}

    # Guard: exclude recent messages
    thread_id = adapter._thread_id(None)
    guard_ids: set = set()
    guard_count = max(0, adapter.settings.last_messages)
    if guard_count > 0:
        with adapter._db_lock:
            recent_msgs = get_messages_last(adapter.conn, thread_id, guard_count)
            guard_ids = {m.id for m in recent_msgs}

    # 1. Keyword search
    keyword_matches: Dict[str, List[str]] = {}  # msg_id -> matched keywords
    if keywords:
        with adapter._db_lock:
            all_msgs = get_all_messages_for_search(
                adapter.conn,
                required_tags=["conversation"],
            )
        keyword_scored = []
        for msg in all_msgs:
            if msg.id in guard_ids:
                continue
            if start_ts and msg.created_at < start_ts:
                continue
            if end_ts and msg.created_at > end_ts:
                continue
            content_lower = (msg.content or "").lower()
            matched = [kw for kw in keywords if kw.lower() in content_lower]
            if matched:
                keyword_scored.append((msg, len(matched)))
                keyword_matches[msg.id] = matched

        keyword_scored.sort(key=lambda x: x[1], reverse=True)
        for rank, (msg, _count) in enumerate(keyword_scored[:topk * 2], start=1):
            if msg.id not in message_data:
                message_data[msg.id] = msg
            message_scores[msg.id] += 1.0 / (rrf_k + rank)

    # 2. Semantic search
    if query and query.strip():
        search_topk = topk * 2 + len(guard_ids)
        with adapter._db_lock:
            groups_raw = semantic_recall_groups(
                adapter.conn,
                adapter.embedder,
                query,
                thread_id=None,
                resource_id=None,
                topk=search_topk,
                range_before=0,
                range_after=0,
                scope=adapter.settings.scope,
                exclude_message_ids=guard_ids,
                required_tags=["conversation"],
            )
        rank_counter = 0
        for seed, _bundle, _score in groups_raw:
            if start_ts and seed.created_at < start_ts:
                continue
            if end_ts and seed.created_at > end_ts:
                continue
            rank_counter += 1
            if seed.id not in message_data:
                message_data[seed.id] = seed
            message_scores[seed.id] += 1.0 / (rrf_k + rank_counter)

    if not message_scores:
        return "(no results found)"

    # Sort by RRF score and take top-k
    sorted_ids = sorted(
        message_scores.keys(),
        key=lambda x: message_scores[x],
        reverse=True,
    )
    top_ids = sorted_ids[:topk]

    # Format as brief snippets with IDs
    lines = []
    for i, msg_id in enumerate(top_ids, start=1):
        msg = message_data[msg_id]
        score = message_scores[msg_id]
        dt = datetime.fromtimestamp(msg.created_at)
        ts = dt.strftime("%Y-%m-%d %H:%M")
        role = msg.role if msg.role != "model" else "assistant"
        content = (msg.content or "").strip().replace("\n", " ")

        # Extract snippet centered on keyword match
        matched_kws = keyword_matches.get(msg_id)
        snippet = _extract_snippet(content, matched_kws or keywords, max_snippet_chars)

        lines.append(f"[{i}] ({msg_id}) {ts} {role}: {snippet} (score:{score:.4f})")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_search_brief",
        description=(
            "Search memory and return brief snippets with message IDs. "
            "Use this for finding relevant messages before reading full context. "
            "Combine query (semantic) and keywords (exact matching) for best results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords for exact substring matching",
                },
                "topk": {
                    "type": "integer",
                    "description": "Number of results to return (default: 10)",
                },
                "max_snippet_chars": {
                    "type": "integer",
                    "description": "Max characters per snippet (default: 100)",
                },
                "start_date": {
                    "type": "string",
                    "description": "Filter from this date (YYYY-MM-DD)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter until this date (YYYY-MM-DD)",
                },
            },
            "required": [],
        },
        result_type="string",
    )

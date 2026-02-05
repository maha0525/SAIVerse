from __future__ import annotations

from typing import List, Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_recall(
    query: str = "",
    keywords: Optional[List[str]] = None,
    max_chars: int = 1200,
    topk: int = 4,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """Recall relevant messages from SAIMemory for the active persona.

    - query: semantic search query (what to recall conceptually)
    - keywords: keywords for exact substring matching (combined with semantic via RRF)
    - max_chars: truncate output to this many characters
    - topk: number of recall seeds
    - start_date: filter by start date (YYYY-MM-DD)
    - end_date: filter by end date (YYYY-MM-DD)
    """

    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set (use tools.context.persona_context)")

    persona_dir = get_active_persona_path()
    adapter: Optional[SAIMemoryAdapter]
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    # Parse date range to timestamps
    start_ts = None
    end_ts = None
    if start_date:
        try:
            from datetime import datetime
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        except ValueError:
            pass
    if end_date:
        try:
            from datetime import datetime
            end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp()) + 86400 - 1
        except ValueError:
            pass

    # Use hybrid recall if keywords provided, otherwise fallback to standard recall
    if keywords:
        return adapter.recall_hybrid(
            query_text=query,
            keywords=keywords,
            max_chars=max_chars,
            topk=topk,
            start_ts=start_ts,
            end_ts=end_ts,
        ) or "(no relevant memory)"
    else:
        return adapter.recall_snippet(
            None,
            query_text=query,
            max_chars=max_chars,
            topk=topk,
        ) or "(no relevant memory)"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_recall",
        description=(
            "Recall relevant past messages from long-term memory. "
            "Use 'query' for semantic (meaning-based) search and 'keywords' for exact word matching. "
            "Combining both gives the best results. "
            "You can also filter by date range using start_date and end_date."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Semantic search query. Describe what you want to recall in natural language. "
                        "Example: 'the conversation where we celebrated together'"
                    ),
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Keywords for exact substring matching. "
                        "Use specific words, names, dates, or phrases that would appear in the message. "
                        "Example: ['birthday', 'January 14']"
                    ),
                },
                "max_chars": {"type": "integer", "default": 1200},
                "topk": {"type": "integer", "default": 4},
                "start_date": {
                    "type": "string",
                    "description": "Filter results from this date (YYYY-MM-DD)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter results until this date (YYYY-MM-DD)",
                },
            },
            "required": [],
        },
        result_type="string",
    )

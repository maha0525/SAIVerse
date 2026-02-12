"""Search Chronicle entries by keyword, time range, and/or level."""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Optional

from sai_memory.arasuji.storage import search_entries
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def chronicle_search(
    query: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    level: Optional[int] = None,
    max_results: int = 10,
) -> str:
    """Search Chronicle entries by keyword and/or time range.

    Args:
        query: Keyword to search in Chronicle content
        start_date: Filter from this date (YYYY-MM-DD)
        end_date: Filter until this date (YYYY-MM-DD)
        level: Filter by specific level (1, 2, ...)
        max_results: Maximum results to return

    Returns:
        Formatted search results
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

    # Convert date strings to unix timestamps
    start_time = None
    end_time = None

    if start_date:
        try:
            dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=dt_timezone.utc)
            start_time = int(dt.timestamp())
        except ValueError:
            return f"(invalid start_date format: {start_date}, expected YYYY-MM-DD)"

    if end_date:
        try:
            dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=dt_timezone.utc
            )
            end_time = int(dt.timestamp())
        except ValueError:
            return f"(invalid end_date format: {end_date}, expected YYYY-MM-DD)"

    # level=0 means "any level" (used by playbook prompts)
    if level is not None and level == 0:
        level = None

    if not query and start_time is None and end_time is None and level is None:
        return "(少なくとも query, start_date, end_date, level のいずれかを指定してください)"

    with adapter._db_lock:
        entries = search_entries(
            adapter.conn,
            query=query,
            start_time=start_time,
            end_time=end_time,
            level=level,
            limit=max_results,
        )

    if not entries:
        criteria = []
        if query:
            criteria.append(f"keyword='{query}'")
        if start_date:
            criteria.append(f"from={start_date}")
        if end_date:
            criteria.append(f"to={end_date}")
        if level is not None:
            criteria.append(f"level={level}")
        return f"(Chronicle検索結果なし: {', '.join(criteria)})"

    # Format results
    lines = [f"Chronicle検索結果 ({len(entries)}件)"]
    if query:
        lines.append(f"Keyword: {query}")
    if start_date or end_date:
        lines.append(f"Period: {start_date or '...'} ~ {end_date or '...'}")
    if level is not None:
        lines.append(f"Level: {level}")
    lines.append("")

    for i, entry in enumerate(entries, 1):
        start = datetime.fromtimestamp(entry.start_time).strftime("%Y-%m-%d %H:%M") if entry.start_time else "?"
        end = datetime.fromtimestamp(entry.end_time).strftime("%Y-%m-%d %H:%M") if entry.end_time else "?"

        lines.append(f"[{i}] ({entry.id}) Lv.{entry.level} | {start} ~ {end} | {entry.message_count}msg")

        # Show full content (chronicles are summaries, not long)
        lines.append(f"    {entry.content.strip()}")
        lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="chronicle_search",
        description=(
            "Search Chronicle (arasuji) entries by keyword, time range, and/or level. "
            "Returns a list of matching entries with IDs and content snippets. "
            "Use chronicle_read_detail to drill into a specific entry."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search in Chronicle content",
                },
                "start_date": {
                    "type": "string",
                    "description": "Filter from this date (YYYY-MM-DD format)",
                },
                "end_date": {
                    "type": "string",
                    "description": "Filter until this date (YYYY-MM-DD format)",
                },
                "level": {
                    "type": "integer",
                    "description": "Filter by specific Chronicle level (1=arasuji, 2=consolidated, ...)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return. Default: 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
        result_type="string",
    )

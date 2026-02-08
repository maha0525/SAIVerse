"""Search Memopedia pages by keyword with optional category filter."""

from __future__ import annotations

import json
import logging
from typing import Optional

from sai_memory.memopedia.core import Memopedia
from sai_memory.memopedia.storage import search_pages_filtered
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def memopedia_search(
    query: str,
    category: Optional[str] = None,
    max_results: int = 10,
) -> str:
    """Search Memopedia pages by keyword (title, summary, content).

    Args:
        query: Search keyword
        category: Optional category filter ("people", "terms", "plans")
        max_results: Maximum results to return

    Returns:
        Formatted search results with page IDs, titles, and summaries
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

    with adapter._db_lock:
        pages = search_pages_filtered(
            adapter.conn,
            query,
            category=category,
            limit=max_results,
        )

    if not pages:
        cat_info = f" (category={category})" if category else ""
        return f"(Memopedia検索結果なし: '{query}'{cat_info})"

    lines = [f"Memopedia検索結果 ({len(pages)}件)"]
    lines.append(f"Keyword: {query}")
    if category:
        lines.append(f"Category: {category}")
    lines.append("")

    for i, page in enumerate(pages, 1):
        # Parse keywords
        try:
            keywords = json.loads(page.keywords) if isinstance(page.keywords, str) else (page.keywords or [])
        except (json.JSONDecodeError, TypeError):
            keywords = []

        kw_str = f" [{', '.join(keywords)}]" if keywords else ""

        lines.append(f"[{i}] ({page.id}) [{page.category}] {page.title}{kw_str}")
        if page.summary:
            lines.append(f"    {page.summary}")

        # Show content snippet if summary is sparse
        if page.content and (not page.summary or len(page.summary) < 20):
            snippet = page.content.strip()[:150]
            if len(page.content.strip()) > 150:
                snippet += "..."
            lines.append(f"    Content: {snippet}")

        lines.append(f"    URI: saiverse://self/memopedia/page/{page.id}")
        lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_search",
        description=(
            "Search Memopedia knowledge pages by keyword. Matches against page titles, "
            "summaries, and content. Optionally filter by category (people/terms/plans). "
            "Returns page IDs and summaries. Use memopedia_get_page to read full content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for in page titles, summaries, and content",
                },
                "category": {
                    "type": "string",
                    "enum": ["people", "terms", "plans"],
                    "description": "Optional: filter by category",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return. Default: 10.",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        result_type="string",
    )

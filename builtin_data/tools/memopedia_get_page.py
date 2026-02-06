"""Get a Memopedia page by title or ID."""

from __future__ import annotations

from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memopedia_get_page(
    title: Optional[str] = None,
    page_id: Optional[str] = None,
) -> str:
    """Get a Memopedia page's full content.

    Specify either title or page_id. Returns the page content as formatted text.
    """
    if not title and not page_id:
        return "Error: Either title or page_id must be specified"

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

    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

    page = None
    if page_id:
        page = memopedia.get_page(page_id)
    elif title:
        page = memopedia.find_by_title(title)

    if not page:
        search_key = page_id if page_id else title
        return f"Page not found: {search_key}"

    # Format page content
    keywords_str = ", ".join(page.keywords) if page.keywords else "(なし)"
    result = f"""# {page.title}

**ID**: {page.id}
**カテゴリ**: {page.category or "未分類"}
**キーワード**: {keywords_str}
**更新日時**: {page.updated_at}

## 要約
{page.summary or "(要約なし)"}

## 内容
{page.content or "(内容なし)"}
"""
    return result


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_get_page",
        description=(
            "Get a Memopedia page's full content by title or ID. "
            "Use this to read existing pages before editing them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Page title to search for (exact match)",
                },
                "page_id": {
                    "type": "string",
                    "description": "Page ID (if known)",
                },
            },
        },
        result_type="string",
    )

"""Tool to close a Memopedia page."""

from __future__ import annotations

from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolSchema


def memopedia_close_page(page_id: str) -> str:
    """Close a Memopedia page.

    - page_id: The ID of the page to close

    The page will no longer be included in the context.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    adapter: Optional[SAIMemoryAdapter]
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.memopedia import Memopedia

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

    # Get thread_id from adapter's active state
    thread_id = adapter._thread_id(None)

    # Close the page
    result = memopedia.close_page(thread_id, page_id)

    if result.get("success"):
        return f"ページを閉じました: {page_id}"
    else:
        return f"ページを閉じられませんでした: {page_id}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_close_page",
        description="Close a Memopedia page. The page content will no longer be included in the conversation context.",
        parameters={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The ID of the page to close",
                },
            },
            "required": ["page_id"],
        },
        result_type="string",
    )

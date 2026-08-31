"""Tool to get the Memopedia page tree structure.

P4 庭仕事ワーカーの素材として内部専用化 (concept_consolidation.md)。
"""

from __future__ import annotations

from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema


def memopedia_get_tree() -> str:
    """Get the Memopedia page tree as Markdown outline.

    Returns the tree structure showing all pages organized by category (人物/用語/計画/出来事).
    Open pages are marked with [OPEN], closed with [-].
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")

        # Import here to avoid circular imports
        from sai_memory.memopedia import Memopedia

        memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)

        # Get thread_id from adapter's active state
        thread_id = adapter._thread_id(None)

        return memopedia.get_tree_markdown(thread_id)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_get_tree",
        description="Get the Memopedia knowledge page tree structure. Shows all pages organized by category (人物/用語/計画/出来事) with open/closed status.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        result_type="string",
    )

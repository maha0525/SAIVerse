"""Tool to open a Memopedia page and get its content."""

from __future__ import annotations

from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolSchema


def memopedia_open_page(page_id: str) -> str:
    """Open a Memopedia page and return its full content.

    - page_id: The ID of the page to open

    Returns the page content in Markdown format, including title, summary, and full content.
    Also lists any child pages.
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

    # Open the page (marks it as open for this thread)
    result = memopedia.open_page(thread_id, page_id)

    if "error" in result:
        return f"Error: {result['error']}"

    # Format as Markdown
    lines = [f"# {result['title']}"]
    if result.get("summary"):
        lines.append(f"\n*{result['summary']}*")
    if result.get("content"):
        lines.append(f"\n{result['content']}")

    children = result.get("children", [])
    if children:
        lines.append("\n## 子ページ")
        for child in children:
            lines.append(f"- **{child['title']}** (id: {child['id']}): {child.get('summary', '')}")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_open_page",
        description="Open a Memopedia page to read its full content. The page will be marked as 'open' and its content will be included in context.",
        parameters={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The ID of the page to open",
                },
            },
            "required": ["page_id"],
        },
        result_type="string",
    )
